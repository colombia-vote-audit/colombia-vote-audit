#!/usr/bin/env python3
"""Extract roll-call votes from Gaceta del Congreso PDFs into a SQLite database.

Usage:
    export LLM_API_KEY=...
    python3 extract_votes.py gaceta_103.pdf gaceta_429.pdf [--db votes.db]

The database has one row per PDF in `documents`, one per vote in `votes` and
one per (vote, legislator) in `vote_records`; see SCHEMA below. Each PDF's
results are saved as soon as its call(s) finish, and re-running a PDF replaces
its earlier results.

Only dependency: PyMuPDF (pip install pymupdf). LLM calls go to an OpenAI-compatible gateway (LLM_BASE_URL).
"""
import argparse
import hashlib
import json
import math
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
# "anthropic" -> /messages (Claude models), "openai" -> /chat/completions (most others)
API_STYLE = os.environ.get("LLM_API_STYLE", "anthropic" if MODEL.startswith("claude") else "openai")
# Some gateways (e.g. OpenCode's) sit behind Cloudflare, which rejects urllib's default User-Agent.
USER_AGENT = "colombia-vote-audit/0.1"

# Each gazette goes to the model in one call. Only gazettes too big for that are
# split, into the fewest roughly equal parts, overlapping by one page.
MAX_PART_TOKENS = 200_000
CHARS_PER_TOKEN = 3.5  # rough, for Spanish text
MAX_OUTPUT_TOKENS = int(os.environ.get("LLM_MAX_TOKENS", "65536"))
REQUEST_TIMEOUT = 900  # seconds; a whole session record can take minutes
WORKERS = 8
MAX_ATTEMPTS = 6

COLUMN_HEADERS = {"SÍ", "SI", "NO", "ABST", "ABSTENCIÓN", "ABSTENCION", "ABSTENIDO"}

PROMPT = """You extract roll-call votes from the official record of the Colombian Congress for a public transparency project. Citizens will use your output to see how each legislator voted, so record only what the text supports: never guess a name, a vote or a result. If a name is unreadable, leave it out.

## What you are reading

The Gaceta del Congreso is the Congress's official gazette. Many issues contain actas: the minutes of plenary or committee sessions of the Senado or the Cámara de Representantes. In a nominal vote the secretary calls the roll and each member answers "Sí" or "No". The minutes then list the names, often in a table, and announce the result (for example "por el Sí: 45, por el No: 12").

The user message has two parts:
- GACETA: metadata about the gazette issue.
- TEXT: the pages of the gazette, each starting with "--- page N ---". Usually this is the whole gazette. Very large gazettes are sent in parts that overlap by one page; then the TEXT says which part it is, and a vote cut off at its start or end should be skipped, because the neighboring part contains it whole.

A gazette can contain several actas and many votes. Include every vote in the TEXT, in the order they appear. Take each vote's session date and acta from the acta it appears in.

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
  "description": "string",             // 1-2 sentence plain-language summary of what the vote was about, written in Spanish
  "result": "approved" | "rejected" | "unknown",
  "yes": ["Full Name", ...],           // members who voted Sí
  "no": ["Full Name", ...],            // members who voted No
  "abstain": ["Full Name", ...]        // members recorded as abstaining; [] if none are named
}

Rules:
- Use names exactly as written in the roll call (surnames first is fine).
- Write "description", "subject" and "bill_title" in Spanish.

## Example

This example only shows the format. Never copy its names or details into your output.

TEXT (whole gazette):
--- page 1 ---
ACTA NÚMERO 45 DE 2021 (septiembre 7) Sesión plenaria del Senado de la República
...
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
{"votes": [{"session_date": "2021-09-07", "acta": "Acta 45 de 2021", "bill_name": "Proyecto de Ley 123 de 2021 Senado", "bill_title": "por medio de la cual se crea el registro nacional de cuidadores", "subject": "proposición con que termina el informe de ponencia", "description": "La plenaria del Senado aprobó la proposición con que termina el informe de ponencia del proyecto que crea el registro nacional de cuidadores.", "result": "approved", "yes": ["Pérez Gómez Ana María", "Rodríguez Díaz Luis Alberto", "Torres Muñoz Carlos"], "no": ["Vargas Ruiz Marta Lucía"], "abstain": []}]}
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


def split_pages(pages):
    """Page ranges (start, end) to send, one call each: the whole gazette if it
    fits in MAX_PART_TOKENS, otherwise the fewest roughly equal parts that do,
    each overlapping the previous by one page."""
    sizes = [len(p) / CHARS_PER_TOKEN for p in pages]
    n = max(1, math.ceil(sum(sizes) / MAX_PART_TOKENS))
    target = sum(sizes) / n
    parts, start, acc = [], 0, 0.0
    for i, size in enumerate(sizes):
        acc += size
        if acc >= target and len(parts) < n - 1 and i + 1 < len(pages):
            parts.append((start, i + 1))
            start, acc = i, size  # the next part starts again at this page
    parts.append((start, len(pages)))
    return parts


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
    else:
        url = f"{API_BASE}/chat/completions"
        payload = {"model": MODEL, "response_format": {"type": "json_object"},
                   "max_tokens": MAX_OUTPUT_TOKENS,
                   "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        headers = {}
    headers |= {"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                "User-Agent": USER_AGENT}
    req = urllib.request.Request(url, json.dumps(payload).encode(), headers)
    for attempt in range(MAX_ATTEMPTS):
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as r:
                data = json.load(r)
            if API_STYLE == "anthropic":
                text, stop = data["content"][0]["text"], data.get("stop_reason")
            else:
                choice = data["choices"][0]
                text, stop = choice["message"]["content"], choice.get("finish_reason")
            if stop in ("length", "max_tokens"):
                raise OutputTruncated(f"output hit max_tokens={MAX_OUTPUT_TOKENS}")
            return parse_json(text)
        except OutputTruncated:
            raise
        except Exception as e:
            # OpenCode reports an exhausted account budget as a 429.
            if isinstance(e, urllib.error.HTTPError) and e.code == 429:
                body = e.read().decode(errors="replace")
                if "budget" in body.lower():
                    raise BudgetExceeded(body) from e
            if attempt == MAX_ATTEMPTS - 1:
                raise
            delay = retry_delay(e, attempt)
            print(f"  retry in {delay}s ({e})", file=sys.stderr)
            time.sleep(delay)


def parse_json(text):
    text = text.strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    return json.loads(text[text.find("{"): text.rfind("}") + 1])


def extract_part(meta, part):
    """Votes the model found in one part of a gazette, and the error if the call failed."""
    label, body = part
    user = (f"GACETA: {json.dumps(meta, ensure_ascii=False)}\n\n"
            f"TEXT ({label}):\n{body}")
    try:
        return call_llm(PROMPT, user).get("votes", []), None
    except BudgetExceeded:
        raise
    except Exception as e:
        print(f"  {meta['source_file']} ({label}) failed: {e}", file=sys.stderr)
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
    parts            INTEGER NOT NULL,      -- calls to the model: 1 unless the gazette was split
    parts_failed     INTEGER NOT NULL,      -- > 0 means this document's votes are incomplete
    model            TEXT NOT NULL,
    prompt_sha256    TEXT NOT NULL,         -- which version of PROMPT produced the rows
    processed_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS votes (
    id           INTEGER PRIMARY KEY,
    document_id  INTEGER NOT NULL REFERENCES documents (id) ON DELETE CASCADE,
    fingerprint  TEXT NOT NULL,             -- drops the same vote found in two overlapping parts
    session_date TEXT,
    acta         TEXT,
    bill_name    TEXT,
    bill_title   TEXT,
    subject      TEXT,
    description  TEXT,
    result       TEXT NOT NULL CHECK (result IN ('approved', 'rejected', 'unknown')),
    source_page  INTEGER NOT NULL,          -- first page of the part the vote came from
    raw_json     TEXT NOT NULL,             -- the model's output for this vote, unmodified
    UNIQUE (document_id, fingerprint)
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


def norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def fingerprint(v):
    key = "|".join([v.get("session_date") or "", norm(v.get("bill_name")), norm(v.get("subject")),
                    str(len(v.get("yes") or [])), str(len(v.get("no") or []))])
    return hashlib.sha1(key.encode()).hexdigest()[:12]


def open_db(path):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


def save_document(conn, doc, found):
    """Replace everything stored for this PDF. `found` is [(source_page, vote), ...].
    Returns the number of votes saved after dropping duplicates."""
    saved = 0
    with conn:
        conn.execute("DELETE FROM documents WHERE sha256 = ?", (doc["sha256"],))
        doc_id = conn.execute(
            """INSERT INTO documents (sha256, source_file, gaceta_number, publication_date, chamber,
                   pages, parts, parts_failed, model, prompt_sha256, processed_at)
               VALUES (:sha256, :source_file, :gaceta_number, :publication_date, :chamber,
                   :pages, :parts, :parts_failed, :model, :prompt_sha256, :processed_at)""",
            {**doc, "processed_at": datetime.now(UTC).isoformat(timespec="seconds")},
        ).lastrowid
        for page, v in sorted(found, key=lambda pv: pv[0]):
            cur = conn.execute(
                """INSERT OR IGNORE INTO votes (document_id, fingerprint, session_date, acta, bill_name,
                       bill_title, subject, description, result, source_page, raw_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (doc_id, fingerprint(v), v.get("session_date"), v.get("acta"), v.get("bill_name"),
                 v.get("bill_title"), v.get("subject"), v.get("description"),
                 v.get("result") if v.get("result") in RESULTS else "unknown",
                 page, json.dumps(v, ensure_ascii=False)),
            )
            if not cur.rowcount:
                continue  # the same vote from the overlapping page of two parts
            saved += 1
            conn.executemany(
                "INSERT OR IGNORE INTO vote_records (vote_id, legislator, vote) VALUES (?, ?, ?)",
                [(cur.lastrowid, name.strip(), k) for k in ("yes", "no", "abstain")
                 for name in v.get(k) or [] if isinstance(name, str) and name.strip()],
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
        with open(path, "rb") as f:
            sha256 = hashlib.sha256(f.read()).hexdigest()
        meta = {**gaceta_meta(pages[0]), "source_file": os.path.basename(path)}
        # Scanned gazettes have no text to send.
        ranges = split_pages(pages) if sum(len(p.strip()) for p in pages) > 200 else []
        parts = []
        for k, (a, b) in enumerate(ranges, start=1):
            label = "whole gazette" if len(ranges) == 1 else f"part {k} of {len(ranges)}, pages {a + 1}-{b}"
            body = "\n\n".join(f"--- page {j + 1} ---\n{pages[j]}" for j in range(a, b))
            parts.append((a, (label, body)))
        doc = {**meta, "sha256": sha256, "pages": len(pages), "parts": len(parts),
               "parts_failed": 0, "model": MODEL, "prompt_sha256": prompt_sha256,
               "pending": len(parts), "found": []}
        docs.append(doc)
        jobs += [(doc, meta, start, part) for start, part in parts]
        print(f"{path}: {len(pages)} pages, {len(parts)} part(s)", file=sys.stderr)

    total = 0
    for doc in docs:
        if not doc["pending"]:  # nothing to send; record it as processed
            save_document(conn, doc, [])

    print(f"Calling {MODEL} ({API_STYLE}) on {len(jobs)} parts...", file=sys.stderr)
    ex = ThreadPoolExecutor(WORKERS)
    futures = {ex.submit(extract_part, meta, part): (doc, start) for doc, meta, start, part in jobs}
    try:
        for fut in as_completed(futures):
            doc, start = futures[fut]
            votes, error = fut.result()
            doc["parts_failed"] += error is not None
            doc["found"] += [(start + 1, v) for v in votes if isinstance(v, dict)]
            doc["pending"] -= 1
            if not doc["pending"]:
                total += save_document(conn, doc, doc["found"])
    except BudgetExceeded as e:
        ex.shutdown(wait=False, cancel_futures=True)
        sys.exit(f"Stopping: the LLM account is out of budget ({e}). "
                 f"Documents finished so far are saved in {args.db}.")
    ex.shutdown()
    failed = sum(d["parts_failed"] for d in docs)
    print(f"Wrote {total} votes from {len(docs)} PDFs to {args.db}"
          + (f"; {failed} parts failed, see documents.parts_failed" if failed else ""),
          file=sys.stderr)

if __name__ == "__main__":
    main()
