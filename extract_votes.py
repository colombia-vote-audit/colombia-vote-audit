#!/usr/bin/env python3
"""Extract roll-call votes from Gaceta del Congreso PDFs into a SQLite database.

Usage:
    export LLM_API_KEY=...
    python3 extract_votes.py gaceta_103.pdf gaceta_429.pdf [--db votes.db]

The database has one row per PDF in `documents`, one per vote in `votes` and
one per (vote, legislator) in `vote_records`; see SCHEMA below. Each PDF's
results are saved as soon as its chunks finish, and re-running a PDF replaces
its earlier results.

Only dependency: PyMuPDF (pip install pymupdf). LLM calls go to an OpenAI-compatible gateway (LLM_BASE_URL).
"""
import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime

import pymupdf

API_BASE = os.environ.get("LLM_BASE_URL", "https://gateway.mircloud.trytokenfactory.dev/v1")
MODEL = os.environ.get("LLM_MODEL", "deepseek-v4.1-flash-uncensored-fp8")
# "anthropic" -> /messages (Claude models), "responses" -> /responses (GPT models on
# OpenCode), "openai" -> /chat/completions (most others)
API_STYLE = os.environ.get("LLM_API_STYLE", "anthropic" if MODEL.startswith("claude")
                           else "responses" if MODEL.startswith("gpt-") else "openai")
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


# ---------- PDF -> text ----------

def page_text(page):
    """Page text with vote-table X marks labeled by column (SÍ / NO / ABST)."""
    words = page.get_text("words")  # x0, y0, x1, y1, text, block, line, word
    headers = [w for w in words if w[4].strip().upper() in COLUMN_HEADERS]
    lines, out = {}, []
    for w in words:
        txt = w[4]
        if txt.upper() == "X" and headers:
            cx = (w[0] + w[2]) / 2
            above = [h for h in headers if h[1] < w[1]] or headers
            # nearest header above by x; prefer closest vertically among ties
            h = min(above, key=lambda h: (abs((h[0] + h[2]) / 2 - cx) // 8, w[1] - h[1]))
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


def call_llm(system, user):
    key = os.environ["LLM_API_KEY"]
    if API_STYLE == "anthropic":
        url = f"{API_BASE}/messages"
        payload = {"model": MODEL, "max_tokens": MAX_OUTPUT_TOKENS, "system": system,
                   "messages": [{"role": "user", "content": user}]}
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
    elif API_STYLE == "responses":
        url = f"{API_BASE}/responses"
        # The Responses API wants "json" in the input itself, not just the instructions.
        payload = {"model": MODEL, "instructions": system,
                   "input": user + "\n\nReply with the json object described in the instructions.",
                   "max_output_tokens": MAX_OUTPUT_TOKENS, "text": {"format": {"type": "json_object"}}}
        if REASONING_EFFORT:
            payload["reasoning"] = {"effort": REASONING_EFFORT}
        headers = {}
    else:
        url = f"{API_BASE}/chat/completions"
        payload = {"model": MODEL, "response_format": {"type": "json_object"},
                   "max_tokens": MAX_OUTPUT_TOKENS,
                   "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
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
            if API_STYLE == "anthropic":
                text, stop = data["content"][0]["text"], data.get("stop_reason")
            elif API_STYLE == "responses":
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
        return call_llm(PROMPT, user).get("votes", []), None
    except BudgetExceeded:
        raise
    except Exception as e:
        print(f"  {meta['source_file']}: chunk at page {start + 1} failed: {e}", file=sys.stderr)
        return [], str(e)


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
    raw_json     TEXT NOT NULL              -- the model's output for this vote, unmodified
);

CREATE TABLE IF NOT EXISTS vote_records (
    vote_id    INTEGER NOT NULL REFERENCES votes (id) ON DELETE CASCADE,
    legislator TEXT NOT NULL,               -- name as written in the gazette
    vote       TEXT NOT NULL CHECK (vote IN ('yes', 'no', 'abstain')),
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
    return conn


def save_document(conn, doc, found):
    """Replace everything stored for this PDF with the votes in `found`.
    Returns the number of votes saved."""
    saved = 0
    with conn:
        conn.execute("DELETE FROM documents WHERE sha256 = ?", (doc["sha256"],))
        doc_id = conn.execute(
            """INSERT INTO documents (sha256, source_file, gaceta_number, publication_date, chamber,
                   pages, chunks, chunks_failed, model, prompt_sha256, processed_at)
               VALUES (:sha256, :source_file, :gaceta_number, :publication_date, :chamber,
                   :pages, :chunks, :chunks_failed, :model, :prompt_sha256, :processed_at)""",
            {**doc, "processed_at": datetime.now(UTC).isoformat(timespec="seconds")},
        ).lastrowid
        for v in sorted(found, key=lambda v: page_of(v) or 0):
            if not (names(v, "yes") or names(v, "no")):
                continue  # the prompt asks only for votes with named voters
            cur = conn.execute(
                """INSERT INTO votes (document_id, session_date, acta, bill_name, bill_title,
                       subject, description, result, vote_type, page, raw_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (doc_id, v.get("session_date"), v.get("acta"), v.get("bill_name"),
                 v.get("bill_title"), v.get("subject"), v.get("description"),
                 v.get("result") if v.get("result") in RESULTS else "unknown",
                 v.get("vote_type") if v.get("vote_type") in VOTE_TYPES else None,
                 page_of(v), json.dumps(v, ensure_ascii=False)),
            )
            saved += 1
            conn.executemany(
                "INSERT OR IGNORE INTO vote_records (vote_id, legislator, vote) VALUES (?, ?, ?)",
                [(cur.lastrowid, name, k) for k in ("yes", "no", "abstain") for name in names(v, k)],
            )
    return saved


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdfs", nargs="+")
    ap.add_argument("--db", default="votes.db", help="SQLite database to write (created if missing)")
    args = ap.parse_args()
    if "LLM_API_KEY" not in os.environ:
        sys.exit("Set LLM_API_KEY")

    conn = open_db(args.db)
    prompt_sha256 = hashlib.sha256(PROMPT.encode()).hexdigest()
    docs, jobs = [], []
    for path in args.pdfs:
        pages = load_pages(path)
        chunks = build_chunks(pages)
        with open(path, "rb") as f:
            sha256 = hashlib.sha256(f.read()).hexdigest()
        meta = {**gaceta_meta(pages[0]), "source_file": os.path.basename(path)}
        doc = {**meta, "sha256": sha256, "pages": len(pages), "chunks": len(chunks),
               "chunks_failed": 0, "model": MODEL, "prompt_sha256": prompt_sha256,
               "pending": len(chunks), "found": []}
        docs.append(doc)
        jobs += [(doc, meta, c) for c in chunks]
        print(f"{path}: {len(pages)} pages, {len(chunks)} chunks", file=sys.stderr)

    total = 0
    for doc in docs:
        if not doc["pending"]:  # no pages that look like votes; record it as processed
            save_document(conn, doc, [])

    print(f"Calling {MODEL} ({API_STYLE}) on {len(jobs)} chunks...", file=sys.stderr)
    ex = ThreadPoolExecutor(WORKERS)
    futures = {ex.submit(extract_chunk, meta, c): (doc, c[0]) for doc, meta, c in jobs}
    try:
        for fut in as_completed(futures):
            doc, start = futures[fut]
            votes, error = fut.result()
            doc["chunks_failed"] += error is not None
            doc["found"] += [v for v in votes
                             if isinstance(v, dict) and keep_vote(v, start, doc["pages"])]
            doc["pending"] -= 1
            if not doc["pending"]:
                total += save_document(conn, doc, doc["found"])
    except BudgetExceeded as e:
        ex.shutdown(wait=False, cancel_futures=True)
        sys.exit(f"Stopping: the LLM account is out of budget ({e}). "
                 f"Documents finished so far are saved in {args.db}.")
    ex.shutdown()
    failed = sum(d["chunks_failed"] for d in docs)
    print(f"Wrote {total} votes from {len(docs)} PDFs to {args.db}"
          + (f"; {failed} chunks failed, see documents.chunks_failed" if failed else ""),
          file=sys.stderr)

if __name__ == "__main__":
    main()
