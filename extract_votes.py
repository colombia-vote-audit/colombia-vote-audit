#!/usr/bin/env python3
"""Extract roll-call votes from Gaceta del Congreso PDFs into a SQLite database.

Usage:
    export LLM_API_KEY=...
    python3 extract_votes.py gaceta_103.pdf gaceta_429.pdf [--db votes.db] [--legislators data/cva.db]

The database has one row per PDF in `documents`, one per vote in `votes` and
one per (vote, legislator) in `vote_records`; see SCHEMA below. Each PDF's
results are saved as soon as its chunks finish, and re-running a PDF replaces
its earlier results.

House plenary sessions vote electronically, and their full roll calls are only
in scanned voting records printed as images. Those pages are read from the
image (LLM_VISION_MODEL, default LLM_MODEL), and each result is checked against
the record's printed totals and row numbers; votes that don't add up are
stored with verified = 0 and the reason. With --legislators
(the pipeline database), the model gives each row as a number from a list of
the House members sitting that day instead of spelling the name from the scan,
and each number is checked against the surnames printed on the row.

Only dependency: PyMuPDF (pip install pymupdf). LLM calls go to an OpenAI-compatible gateway (LLM_BASE_URL).
"""
import argparse
import base64
import hashlib
import json
import os
import re
import sqlite3
import sys
import threading
import time
import unicodedata
from collections import deque
import urllib.error
import urllib.request
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, ThreadPoolExecutor, wait
from datetime import UTC, datetime

import pymupdf

API_BASE = os.environ.get("LLM_BASE_URL", "https://gateway.mircloud.trytokenfactory.dev/v1")
MODEL = os.environ.get("LLM_MODEL", "deepseek-v4.1-flash-uncensored-fp8")
# Some gateways (e.g. OpenCode's) sit behind Cloudflare, which rejects urllib's default User-Agent.
USER_AGENT = "colombia-vote-audit/0.1"

# Gazettes go to the model as 5-page windows overlapping by one page. Sending
# whole gazettes was tried and found ~30% fewer votes on the sample.
PAGES_PER_CHUNK = 5
PAGE_OVERLAP = 1
MAX_OUTPUT_TOKENS = int(os.environ.get("LLM_MAX_TOKENS", "65536"))
# Sampling temperature; unset uses the provider's default. Some reasoning models
# (e.g. GPT-6 Luna) reject it.
TEMPERATURE = float(os.environ["LLM_TEMPERATURE"]) if os.environ.get("LLM_TEMPERATURE") else None
# Reasoning effort ("none", "low", "medium", "high"...); unset uses the provider's
# default. Accepted values differ by model. Not sent for the anthropic style.
REASONING_EFFORT = os.environ.get("LLM_REASONING_EFFORT") or None
REQUEST_TIMEOUT = 600  # seconds; a chunk with long roll calls can take minutes
WORKERS = int(os.environ.get("LLM_WORKERS", "8"))  # parallel calls
# Documents parsed but not yet saved: enough queued calls to keep the workers
# busy, without holding every PDF's text in memory on a large run.
OPEN_DOCUMENTS = 4 * WORKERS
# House plenary sessions vote electronically: the text names only members who
# voted by hand, and everyone else is in a scanned voting record printed as an
# image ("PUBLICACIÓN REGISTRO DE VOTACIÓN"), next to a scanned "REGISTRO
# MANUAL". Their text layer is garbled or missing, so those pages are read from
# the image by a model that accepts images.
VISION_MODEL = os.environ.get("LLM_VISION_MODEL", MODEL)
RECORD_DPI = 200
RECORD_RETRY_DPI = 250  # a page is read again at this resolution if a row number is skipped
RECORD_IMAGE_PIXELS = 500_000  # smaller images are logos, signatures and seals
MAX_ATTEMPTS = 6

# Only windows with signs that a roll call took place go to the model: an announced
# result ("Por el Sí: 45", "Por el SÍ veintiún (21)"), a closed vote ("se cierra la
# votación", "Cierre Registro", "cerrado el registro"), a "votación nominal" heading or
# a labeled vote table. Mentions of voting alone are far more common (agendas,
# speeches) and made ~4x as many calls without adding gazettes with roll calls.
ROLL_CALL_HINT = re.compile(
    r"por\s+el\s+s[ií]\b\W{0,6}(?:[a-záéíóúñ]+\s+){0,3}\(?\d"
    r"|(?:cierr[ae]n?|cerrad[oa])\s+(?:el\s+|la\s+)?(?:registro|votaci)"
    r"|votaci[oó]n\s+nominal|X\[S[ÍI]\]|X\[NO\]", re.I)
ACTA_RE = re.compile(r"ACTA\s+N[ÚU]MERO\s+\d+\s+DE\s+\d{4}", re.I)
# A bill read for debate starts its own line; agenda items are numbered ("1. Proyecto...") or bulleted ("•").
BILL_RE = re.compile(r"(?<![•\n]\n)^Proyecto\s+de\s+(?:ley|acto\s+legislativo)\s+n[úu]mero\s+\d+\s+de\s+\d{4}", re.I | re.M)
COLUMN_HEADERS = {"SÍ", "SI", "NO", "ABST", "ABSTENCIÓN", "ABSTENCION", "ABSTENIDO"}

PROMPT = """You extract roll-call votes from the official record of the Colombian Congress for a public transparency project. Citizens will use your output to see how each legislator voted, so record only what the text supports: never guess a name, a vote or a result. If a name is unreadable, leave it out.

## What you are reading

The Gaceta del Congreso is the Congress's official gazette. Many issues contain actas: the minutes of plenary or committee sessions of the Senado or the Cámara de Representantes. In a nominal vote the secretary calls the roll and each member answers "Sí" or "No". The minutes then list the names, often in a table, and announce the result (for example "por el Sí: 45, por el No: 12").

The user message has three parts:
- GACETA: metadata about the gazette issue.
- ACTA HEADER / ROLL CALL: the start of the acta the text belongs to (session date, chamber, attendance), and the bill under discussion when the text begins, if known. Use it for context only; never extract votes from it.
- TEXT: a few consecutive pages, each starting with "--- page N ---". Extract votes only from this part. Chunks overlap by one page, so a vote may be cut off at the start or end of the TEXT; skip a vote unless both its start and its result are in the TEXT.

The text was extracted automatically from a PDF, so expect broken lines, merged columns, hyphenated words and page headers in the middle of sentences. In vote tables, each "X" has been labeled with the column it falls under, e.g. "X[SÍ]" or "X[NO]".

## Which votes to include

Include only nominal votes where the TEXT records how individual members voted. Most pages contain no votes; that is normal. Do not include:
- votes that report only a tally or a result, without names (for example "por el Sí: 45, por el No: 12" with no list of who voted how),
- votes by "unanimidad" or "votación ordinaria" without names,
- attendance or quorum calls,
- votes that are only mentioned or planned, or that took place in another session.

## Output

Return only a json object: {"votes": [ ... ]}. If the TEXT contains no votes, return {"votes": []}. Each vote is:
{
  "session_date": "YYYY-MM-DD",        // date of the session (from the acta), not the publication date
  "acta": "string",                    // e.g. "Acta 32 de 2022"
  "bill_name": "string",               // e.g. "Proyecto de Ley 53 de 2022 Senado", or "Proposición 140 y 141", or "Actas 026 y 030"
  "bill_title": "string",              // official title ("por la cual se ...") if stated, else ""
  "subject": "string",                 // what exactly was voted: e.g. "articulado", "título", "proposición con que termina el informe de ponencia", "orden del día", "aprobación de actas"
  "vote_type": "final_passage" | "articles" | "report_motion" | "impedimento" | "procedural",
  "description": "string",             // 1-2 sentence plain-language summary of what the vote was about, written in Spanish
  "result": "approved" | "rejected" | "unknown",
  "page": int,                         // the "--- page N ---" number where the vote begins
  "yes": ["Full Name", ...],           // members who voted Sí
  "no": ["Full Name", ...],            // members who voted No
  "abstain": ["Full Name", ...]        // members recorded as abstaining; [] if none are named
}

vote_type is one of:
- "final_passage": the bill as a whole at the end of a debate: its title and/or the question of whether it should become law or go to the next debate, or approval of a conciliation report.
- "articles": the articulado, or particular articles with or without amendments.
- "report_motion": the proposición con que termina el informe de ponencia (whether to debate the bill at all), including motions to archive it.
- "impedimento": whether a member with a conflict of interest may abstain from the bill (impedimentos and recusaciones).
- "procedural": anything else, such as the order of the day, skipping the reading of the articulado, approval of minutes, or motions not about a bill's text.

Rules:
- Use names exactly as written in the roll call (surnames first is fine).
- Write "description", "subject" and "bill_title" in Spanish.

## Example

This example only shows the format. Never copy its names or details into your output.

ACTA HEADER / ROLL CALL:
ACTA NÚMERO 45 DE 2021 (septiembre 7) Sesión plenaria del Senado de la República

TEXT:
--- page 12 ---
La Presidencia abre la votación nominal de la proposición con que termina el informe de ponencia del Proyecto de ley número 123 de 2021 Senado, "por medio de la cual se crea el registro nacional de cuidadores".
Votación nominal
Por el Sí:
Pérez Gómez Ana María
Rodríguez Díaz Luis Alberto
Torres Muñoz Carlos
Por el No:
Vargas Ruiz Marta Lucía
La Secretaría informa el resultado: por el Sí, 3 votos; por el No, 1 voto. En consecuencia, ha sido aprobada la proposición.

JSON output:
{"votes": [{"session_date": "2021-09-07", "acta": "Acta 45 de 2021", "bill_name": "Proyecto de Ley 123 de 2021 Senado", "bill_title": "por medio de la cual se crea el registro nacional de cuidadores", "subject": "proposición con que termina el informe de ponencia", "vote_type": "report_motion", "description": "La plenaria del Senado aprobó la proposición con que termina el informe de ponencia del proyecto que crea el registro nacional de cuidadores.", "result": "approved", "page": 12, "yes": ["Pérez Gómez Ana María", "Rodríguez Díaz Luis Alberto", "Torres Muñoz Carlos"], "no": ["Vargas Ruiz Marta Lucía"], "abstain": []}]}
"""


RECORD_PROMPT = """This page image is from Colombia's Gaceta del Congreso. It may contain voting records: electronic records ("PUBLICACIÓN REGISTRO DE VOTACIÓN": a numbered list of members with Sí or No, and a small totals box) and manual records ("REGISTRO MANUAL": a table with SI and NO columns marked with X and a TOTAL row). Attendance lists, agendas and other scanned pages are not voting records.

MEMBERS is a numbered list of the members sitting on the date of the vote, surnames first, like the records print them. For every row, give the number of the member it refers to. Printed names may be abbreviated or misspelled; if no member fits, use null.

Return only a json object: {"records": [...]}, or {"records": []} if the page has no voting record. Each record is:
{"kind": "electronic" | "manual",
 "title": "the vote's title as printed, or ''",
 "date": "DD/MM/YYYY as printed, or ''",
 "totals": {"si": int, "no": int},   // exactly as printed in the record's totals box or TOTAL row
 "rows": [[row, member, "SURNAMES", "si" | "no"], ...]}
Each row is [the row number as printed (null for manual records), the MEMBERS number or null, the surnames exactly as printed on that row (both of them, e.g. "RINCON TRUJILLO"), the vote]. For a row whose member is null, add the full printed name as a fifth item. Include every row of every record on the page, in order.
A record's rows can continue from the previous page without its title or totals: return those rows as a record with kind "electronic", title "" and totals {"si": 0, "no": 0}."""


# ---------- PDF -> text ----------

def page_text(page):
    """Page text with vote-table X marks labeled by column (SÍ / NO / ABST)."""
    words = page.get_text("words")  # x0, y0, x1, y1, text, block, line, word
    headers = [w for w in words if w[4].strip().upper() in COLUMN_HEADERS]
    # Gazette pages have two text columns, and a vote table that starts in one can
    # continue in the other without repeating its header row.
    mid = page.rect.width / 2
    center = lambda w: (w[0] + w[2]) / 2
    column = lambda w: 0 if center(w) < mid else 1
    left = {c: min((w[0] for w in words if column(w) == c), default=0) for c in (0, 1)}
    lines, out = {}, []
    for w in words:
        txt = w[4]
        if txt.upper() == "X" and headers:
            cx = center(w)
            above = [h for h in headers if column(h) == column(w) and h[1] < w[1]]
            if above:
                # nearest header above by x; prefer closest vertically among ties
                h = min(above, key=lambda h: (abs(center(h) - cx) // 8, w[1] - h[1]))
            else:
                # The table continues from the other column: compare positions
                # measured from each column's left edge.
                rel = cx - left[column(w)]
                h = min(headers, key=lambda h: (abs(center(h) - left[column(h)] - rel) // 8, abs(w[1] - h[1])))
            txt = f"X[{h[4].strip().upper()}]"
        lines.setdefault((w[5], w[6]), []).append(txt)
    for key in sorted(lines):
        out.append(" ".join(lines[key]))
    return "\n".join(out)


def load_pages(path):
    doc = pymupdf.open(path)
    pages = [page_text(p) for p in doc]
    empty = [i + 1 for i, t in enumerate(pages) if len(t.strip()) < 200]
    if empty:
        print(f"  {os.path.basename(path)}: pages {empty} have little or no text (likely scanned images)",
              file=sys.stderr)
    return pages


def gaceta_meta(first_page):
    num = re.search(r"N[ºo°]\s*(\d+)", first_page)
    date = re.search(r"(\w+),\s+(\d{1,2}\s+de\s+\w+\s+de\s+\d{4})", first_page)
    chamber = "Senado" if "S E N A D O" in first_page else ("Cámara" if "C Á M A R A" in first_page else "")
    return {
        "gaceta_number": num.group(1) if num else "",
        "publication_date": date.group(2) if date else "",
        "chamber": chamber,
    }


def build_chunks(pages):
    """Page-window chunks that contain vote keywords, each prefixed with the current acta's header/roll call."""
    acta_starts = []  # (page_idx, header_context)
    for i, t in enumerate(pages):
        for m in ACTA_RE.finditer(t):
            ctx = (t[m.start():] + "\n" + (pages[i + 1] if i + 1 < len(pages) else ""))[:3500]
            acta_starts.append((i, ctx))

    bill_starts = []  # (page_idx, heading + title)
    for i, t in enumerate(pages):
        for m in BILL_RE.finditer(t):
            if not t[:m.start()].rstrip().endswith("•"):
                bill_starts.append((i, " ".join(t[m.start():m.start() + 400].split())))

    chunks = []
    step = PAGES_PER_CHUNK - PAGE_OVERLAP
    for start in range(0, len(pages), step):
        body = "\n\n".join(f"--- page {start + j + 1} ---\n{pages[start + j]}"
                           for j in range(min(PAGES_PER_CHUNK, len(pages) - start)))
        if not ROLL_CALL_HINT.search(body):
            continue
        ctx = next((c for i, c in reversed(acta_starts) if i <= start), "")
        bill = next((b for i, b in reversed(bill_starts) if i < start), "")
        if bill:
            ctx += f"\n\nBILL UNDER DISCUSSION WHEN THIS TEXT BEGINS (unless another one is introduced in the TEXT):\n{bill}"
        chunks.append((start, ctx, body))
        if start + PAGES_PER_CHUNK >= len(pages):
            break
    return chunks


# House plenary results mention the electronic and manual votes they add up
# ("83 votos electrónicos", "han votado manualmente"). Other chambers print the
# names in the text, and their scanned images are proposals and letters.
ELECTRONIC_VOTE = re.compile(r"votos?\s+(?:electr[oó]nicos?|digitales?)|votado\s+manualmente|votos?\s+manuales", re.I)


def record_pages(path, pages):
    """1-based pages that may hold a scanned voting record: a large image within
    two pages of a result that counts electronic or manual votes, and a
    large-image page right after one of those, since records run onto the next
    page."""
    doc = pymupdf.open(path)
    big = {i + 1 for i, p in enumerate(doc) if any(im[2] * im[3] >= RECORD_IMAGE_PIXELS for im in p.get_images())}
    electronic = [i + 1 for i, t in enumerate(pages) if ELECTRONIC_VOTE.search(t)]
    found = {p for p in big if any(abs(p - r) <= 2 for r in electronic)}
    return sorted(found | {p + 1 for p in found if p + 1 in big})


MONTHS = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
          "septiembre", "octubre", "noviembre", "diciembre"]
RECORD_DATE = re.compile(r"Inicio\W{0,3}de\s+la\s+votaci[oó]n\W{0,3}(\d{2})/(\d{2})/(\d{4})", re.I)


def spanish_date(text):
    """'8 de abril de 2026' -> '2026-04-08', or None."""
    m = re.search(r"(\d{1,2})\s+de\s+(\w+)\s+de\s+(\d{4})", text or "", re.I)
    if m and m.group(2).lower() in MONTHS:
        return f"{m.group(3)}-{MONTHS.index(m.group(2).lower()) + 1:02d}-{int(m.group(1)):02d}"
    return None


def fold(s):
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()


def session_day(pages):
    """The session date from the acta header near the start of the gazette, as
    '2025-12-03', or None."""
    m = re.search(r"sesi[oó]n[^.]{0,80}?(\d{1,2}\s+de\s+\w+\s+de\s+\d{4})", "\n".join(pages[:5]), re.I)
    return spanish_date(m.group(1)) if m else None


def members_on(legislators, day, chamber="Cámara de Representantes"):
    """Members of `chamber` sitting on `day`, sorted by surname, as dicts with
    id, surnames and given names. `legislators` is the pipeline database with
    its legislators tables, or None."""
    if legislators is None or day is None:
        return []
    rows = legislators.execute(
        """SELECT DISTINCT t.legislator_id, json_extract(t.raw_json, '$.apellidos'), json_extract(t.raw_json, '$.nombres')
           FROM legislator_terms t WHERE t.chamber = ? AND t.start_date <= ? AND t.end_date >= ?""",
        (chamber, day, day)).fetchall()
    members = [{"id": i, "surnames": (a or "").strip(), "given": (n or "").strip()} for i, a, n in rows]
    return sorted(members, key=lambda m: fold(m["surnames"] + " " + m["given"]))


def members_text(members):
    return "\n".join(f"{i}) {m['surnames'].upper()} {m['given']}" for i, m in enumerate(members, start=1))


PARTICLES = {"de", "del", "la", "las", "los", "y"}


def edit_distance(a, b):
    row = list(range(len(b) + 1))
    for i, x in enumerate(a, start=1):
        prev, row[0] = row[0], i
        for j, y in enumerate(b, start=1):
            prev, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, prev + (x != y))
    return row[-1]


def surname_fits(printed, member, members=()):
    """Whether the surnames printed on a row are the member's, allowing one
    misread letter. All the member's surnames count: a third of the House
    shares a first surname with a colleague, and those sit next to each other
    in MEMBERS, where the model sometimes picks the neighbour. The printed
    words are compared, joined, with the member's surnames, optionally followed
    by given names, so "JAY-PANG DIAZ" fits "Jaypang Díaz" and "GONZALEZ
    HERNANDO" fits a member whose only surname is González. When no other
    member in `members` has a first surname within a letter of the printed
    one, a swap is impossible and the first surname is enough: the records sometimes print the second one
    differently from the legislators list ("CADAVID MARTINEZ" for Cadavid
    Márquez)."""
    words = lambda s: [t for t in re.findall(r"[a-z]+", fold(s).replace("-", "")) if t not in PARTICLES]
    got, surnames = words(printed), words(member["surnames"])
    full = surnames + words(member.get("given") or "")
    if got and any(edit_distance("".join(got), "".join(full[:k])) <= 1
                   for k in range(max(1, len(surnames)), len(full) + 1)):
        return True
    first = lambda m: (words(m["surnames"]) or [""])[0]
    return (bool(got) and bool(surnames) and edit_distance(got[0], surnames[0]) <= 1
            and not any(edit_distance(got[0], first(m)) <= 1 for m in members if m is not member))


def read_rows(record, members):
    """Replace the model's rows, [row, member, surnames, vote, printed name?],
    with dicts: n, vote, name, legislator_id, and problem saying why the row
    couldn't be tied to a member (None if it was, or if there is no list)."""
    rows = []
    for row in record.get("rows") or []:
        if not isinstance(row, list) or len(row) < 4:
            continue
        n, number, surnames, vote = row[:4]
        printed = str(row[4] if len(row) > 4 and row[4] else surnames or "").strip()
        member = members[number - 1] if isinstance(number, int) and 1 <= number <= len(members) else None
        problem = None
        if members and member is None:
            problem = f"no member found for {printed!r}"
        elif member and not surname_fits(surnames, member, members):
            problem = f"member {number} is {member['surnames']}, but the row reads {surnames!r}"
            member = None
        if isinstance(n, str) and n.strip(" .").isdigit():
            n = int(n.strip(" ."))
        rows.append({"n": n if isinstance(n, int) else None, "vote": vote,
                     "name": " ".join(filter(None, (member["surnames"], member["given"]))) if member else printed,
                     "legislator_id": member["id"] if member else None, "problem": problem})
    record["rows"] = rows
    return record


# ---------- LLM ----------

class OutputTruncated(Exception):
    """The model hit MAX_OUTPUT_TOKENS, so its JSON is incomplete. Not retried."""


class BudgetExceeded(Exception):
    """The API account is out of budget. Retrying won't help, so the run stops."""


def retry_delay(error, attempt):
    """Seconds to wait before retrying, honoring Retry-After on 429."""
    after = getattr(error, "headers", None) and error.headers.get("Retry-After")
    if after and after.isdigit():
        return int(after)
    return min(5 * 2**attempt, 120)


def api_style(model):
    """"anthropic" -> /messages (Claude models), "responses" -> /responses (GPT
    models on OpenCode), "openai" -> /chat/completions (most others)."""
    if os.environ.get("LLM_API_STYLE"):
        return os.environ["LLM_API_STYLE"]
    return "anthropic" if model.startswith("claude") else "responses" if model.startswith("gpt-") else "openai"


def call_llm(system, user, image_png=None, model=MODEL):
    """Send one request and return the parsed json. `image_png` (bytes) is
    attached to the user message for models that read images."""
    key = os.environ["LLM_API_KEY"]
    style = api_style(model)
    image = base64.b64encode(image_png).decode() if image_png else None
    if style == "anthropic":
        url = f"{API_BASE}/messages"
        content = [{"type": "text", "text": user}]
        if image:
            content.insert(0, {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": image}})
        payload = {"model": model, "max_tokens": MAX_OUTPUT_TOKENS, "system": system,
                   "messages": [{"role": "user", "content": content}]}
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
    elif style == "responses":
        url = f"{API_BASE}/responses"
        # The Responses API wants "json" in the input itself, not just the instructions.
        content = [{"type": "input_text", "text": user + "\n\nReply with the json object described in the instructions."}]
        if image:
            content.append({"type": "input_image", "image_url": f"data:image/png;base64,{image}"})
        payload = {"model": model, "instructions": system, "input": [{"role": "user", "content": content}],
                   "max_output_tokens": MAX_OUTPUT_TOKENS, "text": {"format": {"type": "json_object"}}}
        if REASONING_EFFORT:
            payload["reasoning"] = {"effort": REASONING_EFFORT}
        headers = {}
    else:
        url = f"{API_BASE}/chat/completions"
        content = [{"type": "text", "text": user}]
        if image:
            content.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image}"}})
        payload = {"model": model, "response_format": {"type": "json_object"},
                   "max_tokens": MAX_OUTPUT_TOKENS,
                   "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}]}
        if REASONING_EFFORT:
            payload["reasoning_effort"] = REASONING_EFFORT
        headers = {}
    if TEMPERATURE is not None:
        payload["temperature"] = TEMPERATURE
    headers |= {"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                "User-Agent": USER_AGENT}
    req = urllib.request.Request(url, json.dumps(payload).encode(), headers)
    for attempt in range(MAX_ATTEMPTS):
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as r:
                data = json.load(r)
            if style == "anthropic":
                text, stop = data["content"][0]["text"], data.get("stop_reason")
            elif style == "responses":
                text = "".join(c.get("text", "") for o in data.get("output", [])
                               if o.get("type") == "message" for c in o.get("content", []))
                stop = (data.get("incomplete_details") or {}).get("reason")
                stop = "length" if stop == "max_output_tokens" else stop
            else:
                choice = data["choices"][0]
                text, stop = choice["message"]["content"], choice.get("finish_reason")
            if stop in ("length", "max_tokens"):
                raise OutputTruncated(f"output hit max_tokens={MAX_OUTPUT_TOKENS}")
            return parse_json(text)
        except OutputTruncated:
            raise
        except Exception as e:
            if isinstance(e, urllib.error.HTTPError):
                body = e.read().decode(errors="replace")
                # OpenCode reports an exhausted account budget as a 429.
                if e.code == 429 and "budget" in body.lower():
                    raise BudgetExceeded(body) from e
                # Other client errors won't succeed on retry.
                if 400 <= e.code < 500 and e.code not in (408, 429):
                    raise RuntimeError(f"HTTP {e.code}: {body[:300]}") from e
            if attempt == MAX_ATTEMPTS - 1:
                raise
            delay = retry_delay(e, attempt)
            print(f"  retry in {delay}s ({e})", file=sys.stderr)
            time.sleep(delay)


def parse_json(text):
    text = text.strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    return json.loads(text[text.find("{"): text.rfind("}") + 1])


def extract_chunk(meta, chunk):
    """Votes the model found in one chunk, and the error if the call failed."""
    start, ctx, body = chunk
    user = (f"GACETA: {json.dumps(meta, ensure_ascii=False)}\n\n"
            f"ACTA HEADER / ROLL CALL (context only):\n{ctx}\n\n"
            f"TEXT:\n{body}")
    try:
        votes = call_llm(PROMPT, user).get("votes")
        return (votes if isinstance(votes, list) else []), None
    except BudgetExceeded:
        raise
    except Exception as e:
        print(f"  {meta['source_file']}: chunk at page {start + 1} failed: {e}", file=sys.stderr)
        return [], str(e)


def skipped_rows(records):
    """Row numbers missing from the electronic records on a page."""
    missing = 0
    for r in records:
        if r.get("kind") != "electronic":
            continue
        seen = {row["n"] for row in r.get("rows", []) if isinstance(row.get("n"), int)}
        if seen:
            missing += len(set(range(min(seen), max(seen) + 1)) - seen)
    return missing


def clean_record(record):
    """A record from the model with its fields in the types the rest of the
    code expects; anything unusable becomes empty or 0."""
    totals = record.get("totals") if isinstance(record.get("totals"), dict) else {}
    as_int = lambda v: int(v) if isinstance(v, (int, float)) else int(v) if isinstance(v, str) and v.strip().isdigit() else 0
    return {**record, "kind": str(record.get("kind") or "").strip().lower(), "title": str(record.get("title") or ""),
            "date": str(record.get("date") or ""), "totals": {"si": as_int(totals.get("si")), "no": as_int(totals.get("no"))}}


# PyMuPDF isn't thread-safe, and record pages are rendered from worker threads.
PDF_LOCK = threading.Lock()


def render_page(path, page_no, dpi):
    """PNG bytes of a 1-based page."""
    with PDF_LOCK, pymupdf.open(path) as doc:
        return doc[page_no - 1].get_pixmap(dpi=dpi).tobytes("png")


def extract_record_page(meta, path, page_no, members, dpis=(RECORD_DPI, RECORD_RETRY_DPI)):
    """Voting records the model read from one page image, and the error if the
    call failed. A page read as having no records, or whose electronic record
    skips a row number, is read once more at a higher resolution, keeping the
    better reading. Pages only get here when they sit next to a House result, so
    "no records" is more likely a misreading than a real answer."""
    user = (f"GACETA: {json.dumps(meta, ensure_ascii=False)}\nPAGE: {page_no}\n\n"
            f"MEMBERS:\n{members_text(members) or '(not available)'}")
    best = None
    try:
        for dpi in dpis:
            out = call_llm(RECORD_PROMPT, user, render_page(path, page_no, dpi), VISION_MODEL)
            found = out.get("records")
            records = [read_rows(clean_record(r), members) for r in found if isinstance(r, dict)] if isinstance(found, list) else []
            records = [r for r in records if r["rows"]]
            better = lambda a, b: (bool(a), -skipped_rows(a)) > (bool(b), -skipped_rows(b))
            if best is None or better(records, best):
                best = records
            if best and not skipped_rows(best):
                break
    except BudgetExceeded:
        raise
    except Exception as e:
        if best is None:
            print(f"  {meta['source_file']}: voting record on page {page_no} failed: {e}", file=sys.stderr)
            return [], str(e)
    for r in best:
        r["page"] = page_no
    return best, None


BILL_ID = re.compile(r"(\d{1,4})\s*(?:/|\bdel?\b)\s*(\d{4}|\d{2})(?!\d)", re.I)


def bill_ids(s):
    """(number, year) of the bills named in a title or bill name, so that
    "PLE.083/25", "PL.083/25C" and "Proyecto de Ley 83 de 2025" all give
    (83, 2025). Bill numbers restart every year and differ by chamber, so a
    number means nothing without its year."""
    year = lambda y: int(y) if len(y) == 4 else 1900 + int(y) if int(y) >= 90 else 2000 + int(y)
    return {(int(n), year(y)) for n, y in BILL_ID.findall(s or "")}


def vote_side(value):
    v = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode().strip().lower()
    return "si" if v.startswith("s") else "no" if v.startswith("n") else None


def record_names(record, side):
    return [row["name"] for row in record.get("rows", []) if vote_side(row["vote"]) == side and row["name"]]


def check_members(parts):
    """Rows of a vote's records that couldn't be tied to a member (no member,
    or the wrong surname)."""
    return [f"{r.get('kind')} record on page {r.get('page')} row {row['n'] or '?'}: {row['problem']}"
            for r in parts for row in r["rows"] if row["problem"]]


def twice_in(rows):
    """Names of members tied to more than one of `rows`."""
    ids = [row["legislator_id"] for row in rows if row["legislator_id"] is not None]
    return sorted({row["name"] for row in rows if row["legislator_id"] is not None
                   and ids.count(row["legislator_id"]) > 1})


def check_repeats(parts):
    """Members a single record lists twice. A record never does: the model
    gave some row the number of a member who has a row of their own, and
    whoever that row was is missing."""
    return [f"{r.get('kind')} record on {record_pages_text(r)} lists {name} twice"
            for r in parts for name in twice_in(r["rows"])]


def listed_twice(parts):
    """A note naming members who appear in both of a vote's records. The
    records themselves do this: a member who votes electronically and by hand
    is printed in both, sometimes on opposite sides."""
    both = sorted(set(twice_in([row for r in parts for row in r["rows"]]))
                  - {name for r in parts for name in twice_in(r["rows"])})
    return [f"listed in both records: {', '.join(both)}"] if both else []


def check_row_numbers(record):
    """Why an electronic record's row numbers don't run 1..N without gaps or
    repeats, or None. Manual records aren't numbered reliably."""
    if record.get("kind") != "electronic":
        return None
    ns = [row["n"] for row in record["rows"]]
    if None in ns:
        return f"electronic record on {record_pages_text(record)}: {ns.count(None)} rows without a row number"
    if sorted(ns) != list(range(1, len(ns) + 1)):
        return (f"electronic record on {record_pages_text(record)}: row numbers {min(ns)}-{max(ns)} "
                f"for {len(ns)} rows, expected 1-{len(ns)}")
    return None


def check_record(record):
    """Why a record's rows don't match its own printed totals, or None."""
    totals = record.get("totals") or {}
    got = (len(record_names(record, "si")), len(record_names(record, "no")))
    want = (totals.get("si"), totals.get("no"))
    return None if got == want else f"{record.get('kind')} record on {record_pages_text(record)}: rows {got[0]}-{got[1]}, printed totals {want[0]}-{want[1]}"


def record_pages_text(record):
    """'page 31', or 'page 31 continued on page 32' for a record joined across pages."""
    return " continued on ".join(f"page {p}" for p in record.get("pages") or [record.get("page")])


# The House announces a plenary result as manual plus electronic ("digital") votes, e.g.
# "Para un total de 6 votos manuales por el sí, 83 votos electrónicos para un
# total de 89 votos por el sí. Por el No, 0 votos manuales, 12 votos electrónicos."
HOUSE_RESULT = re.compile(r"por\s+el\s+s[ií]\b(?P<si>.{0,900}?)por\s+el\s+no\b(?P<no>.{0,500}?)"
                          r"(?:ha(?:n)?\s+sido|se\s+ha|señor|honorables|PUBLICACI|$)", re.I | re.S)
COUNT = r"(\d+|un[oa]?|cero)"


def count(word):
    return {"un": 1, "uno": 1, "una": 1, "cero": 0}.get(word.lower()) if not word.isdigit() else int(word)


def house_result(text):
    """The last House result announced in `text`: {"si": (manual, electronic, total), "no": (...)}
    with None for parts it doesn't state, or None if there is no such result."""
    found = [m for m in HOUSE_RESULT.finditer(text) if re.search(r"electr[oó]nic|digital", m.group(0), re.I)]
    if not found:
        return None
    result = {}
    for side in ("si", "no"):
        seg = found[-1].group(side)
        manual = re.search(COUNT + r"\s+votos?\s+manual", seg, re.I)
        electronic = re.search(COUNT + r"\s+votos?\s+(?:electr|digital)", seg, re.I)
        # "Para un total de 6 votos manuales ..., para un total de 89 votos": the
        # overall total is the last one that isn't a count of manual votes.
        totals = [m for m in re.finditer(r"total\s+(?:por\s+el\s+(?:s[ií]|no)\s+)?de\s+" + COUNT, seg, re.I)
                  if not re.match(r"\s+votos?\s+manual", seg[m.end():], re.I)]
        total = totals[-1] if totals else None
        parts = [count(m.group(1)) if m else None for m in (manual, electronic, total)]
        if parts[2] is None and parts[0] is not None and parts[1] is not None:
            parts[2] = parts[0] + parts[1]
        if parts[0] is None and parts[1] is not None and parts[2] is not None:
            parts[0] = parts[2] - parts[1]
        result[side] = tuple(parts)
    return result


def check_announced(electronic, manual, announced):
    """Problems comparing records with the result announced in the text."""
    if announced is None:
        return []
    problems = []
    for side in ("si", "no"):
        want_manual, want_electronic, want_total = announced[side]
        got_electronic = len(record_names(electronic, side))
        got_manual = len(record_names(manual, side)) if manual else 0
        if want_electronic is not None and got_electronic != want_electronic:
            problems.append(f"electronic {side}: record has {got_electronic}, text announces {want_electronic}")
        if want_total is not None and got_electronic + got_manual != want_total:
            problems.append(f"total {side}: records have {got_electronic + got_manual}, text announces {want_total}")
    return problems


def attach_records(votes, records, pages=()):
    """Give votes their full name lists from scanned voting records.

    Each electronic record is paired with a manual record on the same page, or
    on the next page naming the same bill, and with the text vote that begins up
    to four pages before it, preferring one for the same bill (number and
    year). The text still says what was voted;
    the records say who voted how. A record with no text vote becomes a vote of
    its own. Votes get verified = 1 when every record's rows match its printed
    totals, the electronic record's rows are numbered 1..N and every row is tied
    to a member, 0 with the reasons in check_note otherwise. check_note also
    notes, without failing the vote, members listed twice and differences from
    the result announced in the text (`pages`)."""
    # Work on copies: the same records are assembled again after rechecks.
    records = [{**r, "rows": list(r["rows"])} for r in records]
    electronic = sorted((r for r in records if r.get("kind") == "electronic"), key=lambda r: r["page"])
    manual = sorted((r for r in records if r.get("kind") == "manual"), key=lambda r: r["page"])
    # Join records that run onto the following pages. The rows there come
    # without totals (0-0) and continue the numbering (44 after 43); the model
    # sometimes repeats the title on them, so the title can't be relied on. The
    # same scan is often placed on both pages, so a row at the edge can be read
    # twice; a repeated row number is dropped.
    first_n = lambda r: next((row["n"] for row in r["rows"] if row["n"] is not None), None)
    last_n = lambda r: next((row["n"] for row in reversed(r["rows"]) if row["n"] is not None), None)
    is_continuation = lambda r: not any(r["totals"].values())
    joined = set()
    for e in electronic:
        if is_continuation(e):
            continue
        e["pages"] = [e["page"]]
        for c in electronic:  # sorted by page, so a record can run over several pages
            if len(e["rows"]) >= sum(e["totals"].values()):
                break
            if c["page"] != e["pages"][-1] + 1 or not is_continuation(c) or id(c) in joined:
                continue
            # The rows must carry on the numbering; the first may repeat the last.
            end, start = last_n(e), first_n(c)
            if start is None or start == 1 or (end is not None and not start <= end + 1 <= last_n(c)):
                continue
            e["rows"] += [row for row in c["rows"] if end is None or row["n"] is None or row["n"] > end]
            e["pages"].append(c["page"])
            joined.add(id(c))
    # Continuation rows that joined nothing mean a record was misread. Next to
    # a record they fail it (and get both pages read again); elsewhere, or when
    # numbered from 1 (a new record whose totals were misread), they stay a
    # record of their own, which fails its checks.
    heads = [e for e in electronic if "pages" in e]
    for c in electronic:
        if "pages" not in c and id(c) not in joined:
            before = [e for e in heads if 0 <= c["page"] - e["pages"][-1] <= 1]
            if before and (first_n(c) or 1) > 1:
                before[-1].setdefault("stray", []).append(
                    f"electronic record on page {c['page']}: rows {first_n(c)}-{last_n(c)} not joined to a record")
            else:
                c["pages"] = [c["page"]]
                heads.append(c)
    electronic = sorted(heads, key=lambda r: r["page"])
    used_votes, used_manual, extra = set(), set(), []
    for e in electronic:
        after = e["pages"][-1] + 1
        next_page_has_own = any(o["page"] == after for o in electronic)

        def manual_fit(m):
            """How well a manual record goes with e, lower is better, or None.
            One on e's own pages always fits: the House prints them together,
            and titles are sometimes misread ("36 DEL 2024" for 336). One on the
            next page fits if it names the same bill, or if no other record
            starts there. Same bill first, then same page."""
            ids = bill_ids(m["title"]), bill_ids(e["title"])
            same_bill = bool(ids[0] & ids[1])
            if m["page"] in e["pages"] or (m["page"] == after and (
                    same_bill or not next_page_has_own and not (all(ids) and not same_bill))):
                return (not same_bill, m["page"] not in e["pages"], m["page"])
            return None

        fits = sorted((manual_fit(m), i) for i, m in enumerate(manual) if i not in used_manual and manual_fit(m))
        parts = [e] + ([manual[fits[0][1]]] if fits else [])
        used_manual.update(i for _, i in fits[:1])
        cands = [i for i, v in enumerate(votes) if i not in used_votes and page_of(v) is not None
                 and 0 <= e["page"] - page_of(v) <= 4]
        best = max(cands, default=None, key=lambda i: (
            bool(bill_ids(votes[i].get("bill_name")) & bill_ids(e["title"])), page_of(votes[i])))
        problems = ([p for p in [*map(check_record, parts), check_row_numbers(e)] if p]
                    + e.get("stray", []) + check_members(parts) + check_repeats(parts))
        # The announced result is only a note: the secretary sometimes misstates
        # it and corrects it later in a "nota aclaratoria", while the records
        # come from the voting system.
        announced = house_result("\n".join(pages[max(0, e["page"] - 3):e["page"]]))
        notes = listed_twice(parts) + [f"differs from the announced result: {p}" for p in
                                       check_announced(e, parts[1] if len(parts) > 1 else None, announced)]
        filled = {"yes": sum((record_names(r, "si") for r in parts), []),
                  "no": sum((record_names(r, "no") for r in parts), []), "abstain": [],
                  "legislator_ids": {row["name"]: row["legislator_id"] for r in parts for row in r["rows"]
                                     if row["legislator_id"] is not None},
                  "source": "record", "verified": int(not problems), "check_note": "; ".join(problems + notes) or None}
        if best is not None:
            used_votes.add(best)
            votes[best].update(filled)
        else:
            day = re.match(r"(\d{2})/(\d{2})/(\d{4})", e.get("date") or "")
            extra.append({"session_date": f"{day.group(3)}-{day.group(2)}-{day.group(1)}" if day else None,
                          "bill_name": e.get("title"), "subject": e.get("title"), "result": "unknown",
                          "page": e["page"], **filled})
    # A House plenary vote whose result counts electronic votes but got no record
    # has only the members who voted by hand, so it can't be taken as complete.
    for i, v in enumerate(votes):
        if i in used_votes or page_of(v) is None or not pages:
            continue
        announced = house_result("\n".join(pages[max(0, page_of(v) - 1):page_of(v) + 2]))
        if announced and any((announced[side][1] or 0) > 0 for side in ("si", "no")):
            v.update(verified=0, check_note="the text counts electronic votes, but no voting record was read for "
                                            "this vote, so only members who voted by hand are listed")
    return votes + extra


def pages_to_recheck(doc, votes):
    """Record pages behind votes whose rows don't add up, to read again at a
    higher resolution."""
    bad = {int(p) for v in votes if v.get("verified") == 0
           for p in re.findall(r"\bpage (\d+)", v.get("check_note") or "")}
    return sorted(bad & set(doc["record_args"]))


def with_rechecks(doc, votes, retry):
    """The votes with the pages in `retry` ({page: records}) read again, if that
    verifies more of them; otherwise `votes` unchanged."""
    records = [r for r in doc["records"] if r["page"] not in retry] + [r for rs in retry.values() for r in rs]
    again = attach_records([dict(v) for v in doc["found"]], records, doc["page_texts"])
    verified = lambda vs: sum(v.get("verified") == 1 for v in vs)
    return again if verified(again) > verified(votes) else votes


# ---------- output ----------

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id               INTEGER PRIMARY KEY,
    sha256           TEXT NOT NULL UNIQUE,  -- the fetch pipeline stores PDFs under this hash
    source_file      TEXT NOT NULL,
    gaceta_number    TEXT,
    publication_date TEXT,
    chamber          TEXT,
    pages            INTEGER NOT NULL,
    chunks           INTEGER NOT NULL,      -- 5-page windows sent to the model
    chunks_failed    INTEGER NOT NULL,      -- > 0 means this document's votes are incomplete
    record_pages     INTEGER NOT NULL DEFAULT 0,  -- page images read for voting records
    record_pages_failed INTEGER NOT NULL DEFAULT 0,
    model            TEXT NOT NULL,
    prompt_sha256    TEXT NOT NULL,         -- which version of PROMPT produced the rows
    processed_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS votes (
    id           INTEGER PRIMARY KEY,
    document_id  INTEGER NOT NULL REFERENCES documents (id) ON DELETE CASCADE,
    session_date TEXT,
    acta         TEXT,
    bill_name    TEXT,
    bill_title   TEXT,
    subject      TEXT,
    description  TEXT,
    result       TEXT NOT NULL CHECK (result IN ('approved', 'rejected', 'unknown')),
    vote_type    TEXT CHECK (vote_type IN ('final_passage', 'articles', 'report_motion',
                                           'impedimento', 'procedural')),  -- NULL if the model gave none
    page         INTEGER,                   -- gazette page where the vote begins, as reported by the model
    source       TEXT NOT NULL DEFAULT 'text' CHECK (source IN ('text', 'record')),
                                            -- where the names come from: the text, or scanned voting records
    verified     INTEGER,                   -- records only: 1 if the rows match the printed totals, are
                                            -- numbered 1..N and all match a member, else 0
    check_note   TEXT,                      -- why verified is 0, and notes that don't fail the vote
    raw_json     TEXT NOT NULL              -- the model's output for this vote, unmodified
);

CREATE TABLE IF NOT EXISTS vote_records (
    vote_id    INTEGER NOT NULL REFERENCES votes (id) ON DELETE CASCADE,
    legislator TEXT NOT NULL,               -- name as written in the gazette; for records, the
                                            -- member's name from the legislators list when matched
    vote       TEXT NOT NULL CHECK (vote IN ('yes', 'no', 'abstain')),
    legislator_id INTEGER,                  -- legislators.id in the pipeline database, for records
                                            -- read with --legislators whose row matched a member
    UNIQUE (vote_id, legislator, vote)
);
CREATE INDEX IF NOT EXISTS vote_records_legislator ON vote_records (legislator);

CREATE VIEW IF NOT EXISTS vote_totals AS
SELECT v.id AS vote_id,
       count(*) FILTER (WHERE r.vote = 'yes') AS yes_count,
       count(*) FILTER (WHERE r.vote = 'no') AS no_count,
       count(*) FILTER (WHERE r.vote = 'abstain') AS abstain_count
FROM votes v LEFT JOIN vote_records r ON r.vote_id = v.id
GROUP BY v.id;
"""

RESULTS = {"approved", "rejected", "unknown"}
VOTE_TYPES = {"final_passage", "articles", "report_motion", "impedimento", "procedural"}


def norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def names(v, key):
    return frozenset(n.strip() for n in v.get(key) or [] if isinstance(n, str) and n.strip())


def page_of(v):
    """The page the model says the vote begins on, or None if it gave no usable one."""
    try:
        return int(v.get("page"))
    except (TypeError, ValueError):
        return None


def owned_pages(start, n_pages):
    """1-based pages whose votes the chunk starting at `start` keeps. Its last
    page is the first page of the next chunk, which owns votes beginning there;
    the last chunk owns everything to the end."""
    step = PAGES_PER_CHUNK - PAGE_OVERLAP
    last = start + PAGES_PER_CHUNK >= n_pages
    return range(start + 1, (n_pages if last else start + step) + 1)


def keep_vote(v, start, n_pages):
    """Keep a vote only in the chunk that owns the page it begins on, so a vote
    on the page two chunks share is stored once. Votes without a usable page
    in this chunk are kept, since we can't tell which chunk should have them."""
    page = page_of(v)
    shown = range(start + 1, min(start + PAGES_PER_CHUNK, n_pages) + 1)
    return page not in shown or page in owned_pages(start, n_pages)


def open_db(path):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    for table, column, decl in [
            ("documents", "record_pages", "INTEGER NOT NULL DEFAULT 0"),
            ("documents", "record_pages_failed", "INTEGER NOT NULL DEFAULT 0"),
            ("votes", "source", "TEXT NOT NULL DEFAULT 'text'"),
            ("votes", "verified", "INTEGER"),
            ("votes", "check_note", "TEXT"),
            ("vote_records", "legislator_id", "INTEGER")]:
        if column not in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    conn.execute("CREATE INDEX IF NOT EXISTS vote_records_legislator_id ON vote_records (legislator_id)")
    return conn


def save_document(conn, doc, found):
    """Replace everything stored for this PDF with the votes in `found`.
    Returns the number of votes saved."""
    saved = 0
    with conn:
        conn.execute("DELETE FROM documents WHERE sha256 = ?", (doc["sha256"],))
        doc_id = conn.execute(
            """INSERT INTO documents (sha256, source_file, gaceta_number, publication_date, chamber,
                   pages, chunks, chunks_failed, record_pages, record_pages_failed,
                   model, prompt_sha256, processed_at)
               VALUES (:sha256, :source_file, :gaceta_number, :publication_date, :chamber,
                   :pages, :chunks, :chunks_failed, :record_pages, :record_pages_failed,
                   :model, :prompt_sha256, :processed_at)""",
            {**doc, "processed_at": datetime.now(UTC).isoformat(timespec="seconds")},
        ).lastrowid
        for v in sorted(found, key=lambda v: page_of(v) or 0):
            if not (names(v, "yes") or names(v, "no")):
                continue  # the prompt asks only for votes with named voters
            cur = conn.execute(
                """INSERT INTO votes (document_id, session_date, acta, bill_name, bill_title,
                       subject, description, result, vote_type, page, source, verified,
                       check_note, raw_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (doc_id, *(v.get(k) if isinstance(v.get(k), str) else None for k in
                           ("session_date", "acta", "bill_name", "bill_title", "subject", "description")),
                 v.get("result") if v.get("result") in RESULTS else "unknown",
                 v.get("vote_type") if v.get("vote_type") in VOTE_TYPES else None,
                 page_of(v), v.get("source", "text"), v.get("verified"), v.get("check_note"),
                 json.dumps(v, ensure_ascii=False)),
            )
            saved += 1
            conn.executemany(
                "INSERT OR IGNORE INTO vote_records (vote_id, legislator, vote, legislator_id) VALUES (?, ?, ?, ?)",
                [(cur.lastrowid, name, k, (v.get("legislator_ids") or {}).get(name))
                 for k in ("yes", "no", "abstain") for name in names(v, k)],
            )
    return saved


def prepare(path):
    """What the model calls need from one PDF: its text, chunks, record pages,
    hash and header. Runs in a separate process, so PDFs are parsed in
    parallel with each other and with the model calls."""
    pages = load_pages(path)
    with open(path, "rb") as f:
        sha256 = hashlib.sha256(f.read()).hexdigest()
    return {"path": path, "pages": pages, "chunks": build_chunks(pages), "recs": record_pages(path, pages),
            "sha256": sha256, "meta": {**gaceta_meta(pages[0] if pages else ""), "source_file": os.path.basename(path)}}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdfs", nargs="+")
    ap.add_argument("--db", default="votes.db", help="SQLite database to write (created if missing)")
    ap.add_argument("--legislators", help="pipeline database with the legislators tables (cva.db); "
                    "lets the model name members exactly when reading scanned voting records")
    args = ap.parse_args()
    if "LLM_API_KEY" not in os.environ:
        sys.exit("Set LLM_API_KEY")

    conn = open_db(args.db)
    legislators = sqlite3.connect(f"file:{args.legislators}?mode=ro", uri=True) if args.legislators else None
    prompt_sha256 = hashlib.sha256((PROMPT + RECORD_PROMPT).encode()).hexdigest()
    print(f"Calling {MODEL} ({api_style(MODEL)}) on text chunks"
          + (f" and {VISION_MODEL} ({api_style(VISION_MODEL)}) on voting-record pages" if VISION_MODEL != MODEL else "")
          + "...", file=sys.stderr)

    ex = ThreadPoolExecutor(WORKERS)
    parser = ProcessPoolExecutor(min(os.cpu_count() or 1, OPEN_DOCUMENTS))
    futures = {}    # model call -> (kind, doc, chunk start or page)
    preparing = {}  # parse -> path
    queue = deque(args.pdfs)
    open_docs, total, failed, unsaved = 0, 0, 0, []

    def start(prepared):
        """Queue the model calls for a parsed PDF, or save it straight away if
        nothing in it looks like a vote. Returns whether it's now open."""
        pages, meta, path = prepared["pages"], prepared["meta"], prepared["path"]
        chunks, recs = prepared["chunks"], prepared["recs"]
        doc = {**meta, "sha256": prepared["sha256"], "pages": len(pages), "chunks": len(chunks),
               "chunks_failed": 0, "record_pages": len(recs), "record_pages_failed": 0,
               "model": MODEL, "prompt_sha256": prompt_sha256,
               "pending": len(chunks) + len(recs), "found": [], "records": [], "page_texts": pages,
               "record_args": {}}
        print(f"{path}: {len(pages)} pages, {len(chunks)} chunks"
              + (f", {len(recs)} voting-record pages" if recs else ""), file=sys.stderr)
        for c in chunks:
            futures[ex.submit(extract_chunk, meta, c)] = ("chunk", doc, c[0])
        for page_no in recs:
            m = RECORD_DATE.search(pages[page_no - 1])
            day = (f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else
                   session_day(pages) or spanish_date(meta["publication_date"]))
            doc["record_args"][page_no] = (meta, path, page_no, members_on(legislators, day))
            futures[ex.submit(extract_record_page, *doc["record_args"][page_no])] = ("record", doc, page_no)
        if not doc["pending"]:  # no pages that look like votes; record it as processed
            save_document(conn, doc, [])
        return bool(doc["pending"])

    def finish(doc):
        """Assemble and save a document whose calls have all returned, or first
        queue rechecks of the pages of votes that don't add up. Returns the
        number of votes saved, or None while rechecks are pending."""
        if "votes" not in doc:
            doc["votes"] = attach_records([dict(v) for v in doc["found"]], doc["records"], doc["page_texts"])
            doc["retry"] = {}
            for page_no in pages_to_recheck(doc, doc["votes"]):
                futures[ex.submit(extract_record_page, *doc["record_args"][page_no],
                                  dpis=(RECORD_RETRY_DPI,))] = ("recheck", doc, page_no)
                doc["pending"] += 1
            if doc["pending"]:
                return None
        return save_document(conn, doc, with_rechecks(doc, doc["votes"], doc["retry"])
                             if doc["retry"] else doc["votes"])

    try:
        while True:
            while queue and len(preparing) + open_docs < OPEN_DOCUMENTS:  # parse ahead
                path = queue.popleft()
                preparing[parser.submit(prepare, path)] = path
            if not futures and not preparing:
                break
            done, _ = wait([*futures, *preparing], return_when=FIRST_COMPLETED)
            for fut in done:
                if fut in preparing:
                    path = preparing.pop(fut)
                    try:
                        open_docs += start(fut.result())
                    except Exception as e:
                        print(f"  {path}: couldn't read: {e!r}", file=sys.stderr)
                        unsaved.append(os.path.basename(path))
                    continue
                kind, doc, key = futures.pop(fut)
                result, error = fut.result()
                if kind == "chunk":
                    doc["chunks_failed"] += error is not None
                    doc["found"] += [v for v in result
                                     if isinstance(v, dict) and keep_vote(v, key, doc["pages"])]
                elif kind == "record":
                    doc["record_pages_failed"] += error is not None
                    doc["records"] += result
                elif error is None:  # a recheck; a failed one leaves the first reading
                    doc["retry"][key] = result
                doc["pending"] -= 1
                if doc["pending"]:
                    continue
                # Model output that trips up assembly or saving costs only its
                # own document, which isn't saved and so is processed again on
                # the next run.
                try:
                    saved = finish(doc)
                except Exception as e:
                    print(f"  {doc['source_file']}: not saved: {e!r}", file=sys.stderr)
                    unsaved.append(doc["source_file"])
                    saved = 0
                if saved is not None:
                    open_docs -= 1
                    total += saved
                    failed += doc["chunks_failed"] + doc["record_pages_failed"]
    except BudgetExceeded as e:
        sys.exit(f"Stopping: the LLM account is out of budget ({e}). "
                 f"Documents finished so far are saved in {args.db}.")
    finally:
        # Don't let queued calls run (and cost money) after the loop is gone.
        ex.shutdown(wait=False, cancel_futures=True)
        parser.shutdown(wait=False, cancel_futures=True)
    print(f"Wrote {total} votes from {len(args.pdfs) - len(unsaved)} PDFs to {args.db}"
          + (f"; {failed} calls failed, see documents.chunks_failed and record_pages_failed" if failed else "")
          + (f"; not saved: {', '.join(unsaved)}" if unsaved else ""),
          file=sys.stderr)


if __name__ == "__main__":
    main()
