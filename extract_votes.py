#!/usr/bin/env python3
"""Extract roll-call votes from Gaceta del Congreso PDFs into SQLite-ready CSV/JSONL.

Usage:
    export LLM_API_KEY=...
    python3 extract_votes.py gaceta_103.pdf gaceta_429.pdf [-o out/]

Outputs (in out/):
    votes.csv          one row per vote event
    vote_records.csv   one row per (vote, legislator) with yes/no/abstain/absent
    votes.jsonl        raw structured output, one vote per line

Only dependency: PyMuPDF (pip install pymupdf). LLM calls go to an OpenAI-compatible gateway (LLM_BASE_URL).
"""
import argparse
import csv
import hashlib
import json
import os
import re
import sys
import unicodedata
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import pymupdf

API_BASE = os.environ.get("LLM_BASE_URL", "https://gateway.mircloud.trytokenfactory.dev/v1")
MODEL = os.environ.get("LLM_MODEL", "deepseek-v4.1-flash-uncensored-fp8")
# "anthropic" -> /messages (Claude models), "openai" -> /chat/completions (most others)
API_STYLE = os.environ.get("LLM_API_STYLE", "anthropic" if MODEL.startswith("claude") else "openai")

PAGES_PER_CHUNK = 5
PAGE_OVERLAP = 1
WORKERS = 8

VOTE_HINT = re.compile(r"votaci[oó]n|\bvot[oa]n? s[ií]\b|por el s[ií]|por el no|total votos|abstenci", re.I)
ACTA_RE = re.compile(r"ACTA\s+N[ÚU]MERO\s+\d+\s+DE\s+\d{4}", re.I)
# A bill read for debate starts its own line; agenda items are numbered ("1. Proyecto...") or bulleted ("•").
BILL_RE = re.compile(r"(?<![•\n]\n)^Proyecto\s+de\s+(?:ley|acto\s+legislativo)\s+n[úu]mero\s+\d+\s+de\s+\d{4}", re.I | re.M)
COLUMN_HEADERS = {"SÍ", "SI", "NO", "ABST", "ABSTENCIÓN", "ABSTENCION", "ABSTENIDO"}

PROMPT = """You extract roll-call votes from Colombian congressional session minutes (Gaceta del Congreso).

In vote tables, an "X" has been annotated with its column, e.g. "X[SÍ]" or "X[NO]".

Return ONLY JSON: {"votes": [ ... ]} where each vote is:
{
  "session_date": "YYYY-MM-DD",        // date of the session (from the acta), not the publication date
  "acta": "string",                    // e.g. "Acta 32 de 2022"
  "bill_name": "string",               // e.g. "Proyecto de Ley 53 de 2022 Senado", or "Proposición 140 y 141", or "Actas 026 y 030"
  "bill_title": "string",              // official title ("por la cual se ...") if stated, else ""
  "subject": "string",                 // what exactly was voted: e.g. "articulado", "título", "proposición con que termina el informe de ponencia", "orden del día", "aprobación de actas"
  "description": "string",             // 1-2 sentence plain-language summary of what the vote was about, written in Spanish
  "result": "approved" | "rejected" | "unknown",
  "yes": ["Full Name", ...],
  "no": ["Full Name", ...],
  "abstained": ["Full Name", ...],
  "absent": ["Full Name", ...],        // commission members from the roll call who did not vote; [] if not determinable
  "yes_count": int, "no_count": int, "abstained_count": int   // as announced by the secretary; null if not stated
}

Rules:
- Only include NOMINAL votes (individual names or an announced tally) whose tally/result appears in the TEXT section. Skip votes by "unanimidad" with no names or counts unless a count is given.
- Use names exactly as written in the roll call (surnames first is fine).
- Write "description", "subject" and "bill_title" in Spanish.
- If there are no votes, return {"votes": []}.
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
        print(f"  {os.path.basename(path)}: pages {empty} have little/no text (likely scanned images, skipped)",
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
        if not VOTE_HINT.search(body):
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

def call_llm(system, user):
    key = os.environ["LLM_API_KEY"]
    if API_STYLE == "anthropic":
        url = f"{API_BASE}/messages"
        payload = {"model": MODEL, "max_tokens": 8192, "system": system,
                   "messages": [{"role": "user", "content": user}]}
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
    else:
        url = f"{API_BASE}/chat/completions"
        payload = {"model": MODEL, "response_format": {"type": "json_object"},
                   "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        headers = {}
    headers |= {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    req = urllib.request.Request(url, json.dumps(payload).encode(), headers)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                data = json.load(r)
            text = (data["content"][0]["text"] if API_STYLE == "anthropic"
                    else data["choices"][0]["message"]["content"])
            return parse_json(text)
        except Exception as e:
            if attempt == 2:
                raise
            print(f"  retry ({e})", file=sys.stderr)


def parse_json(text):
    text = text.strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    return json.loads(text[text.find("{"): text.rfind("}") + 1])


def extract_chunk(meta, chunk):
    start, ctx, body = chunk
    user = (f"GACETA: {json.dumps(meta, ensure_ascii=False)}\n\n"
            f"ACTA HEADER / ROLL CALL (context only):\n{ctx}\n\n"
            f"TEXT:\n{body}")
    try:
        votes = call_llm(PROMPT, user).get("votes", [])
    except Exception as e:
        print(f"  chunk at page {start + 1} failed: {e}", file=sys.stderr)
        return []
    for v in votes:
        v.update(meta)
        v["source_page"] = start + 1
    return votes


# ---------- output ----------

def norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def vote_id(v):
    key = "|".join([v.get("gaceta_number", ""), v.get("session_date", ""), norm(v.get("bill_name")),
                    norm(v.get("subject")), str(len(v.get("yes", []))), str(len(v.get("no", []))),
                    str(v.get("yes_count")), str(v.get("no_count"))])
    return hashlib.sha1(key.encode()).hexdigest()[:12]


def write_outputs(votes, out):
    os.makedirs(out, exist_ok=True)
    seen, uniq = set(), []
    for v in votes:
        v["vote_id"] = vote_id(v)
        if v["vote_id"] not in seen:
            seen.add(v["vote_id"])
            uniq.append(v)

    with open(os.path.join(out, "votes.jsonl"), "w", encoding="utf-8") as f:
        for v in uniq:
            f.write(json.dumps(v, ensure_ascii=False) + "\n")

    cols = ["vote_id", "gaceta_number", "publication_date", "chamber", "acta", "session_date",
            "bill_name", "bill_title", "subject", "description", "result",
            "yes_count", "no_count", "abstained_count", "absent_count", "source_page"]
    with open(os.path.join(out, "votes.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, cols, extrasaction="ignore")
        w.writeheader()
        for v in uniq:
            row = dict(v)
            for k in ("yes", "no", "abstained"):
                if row.get(f"{k}_count") is None:
                    row[f"{k}_count"] = len(v.get(k) or [])
            row["absent_count"] = len(v.get("absent") or [])
            w.writerow(row)

    with open(os.path.join(out, "vote_records.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["vote_id", "legislator", "vote"])
        for v in uniq:
            for k in ("yes", "no", "abstained", "absent"):
                for name in v.get(k) or []:
                    w.writerow([v["vote_id"], name.strip(), k])
    return uniq


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdfs", nargs="+")
    ap.add_argument("-o", "--out", default="out")
    args = ap.parse_args()
    if "LLM_API_KEY" not in os.environ:
        sys.exit("Set LLM_API_KEY")

    jobs = []
    for path in args.pdfs:
        pages = load_pages(path)
        meta = gaceta_meta(pages[0])
        meta["source_file"] = os.path.basename(path)
        chunks = build_chunks(pages)
        print(f"{path}: {len(pages)} pages, {len(chunks)} chunks", file=sys.stderr)
        jobs += [(meta, c) for c in chunks]

    print(f"Calling {MODEL} ({API_STYLE}) on {len(jobs)} chunks...", file=sys.stderr)
    with ThreadPoolExecutor(WORKERS) as ex:
        results = ex.map(lambda j: extract_chunk(*j), jobs)
    votes = [v for r in results for v in r]
    uniq = write_outputs(votes, args.out)
    print(f"Wrote {len(uniq)} votes to {args.out}/", file=sys.stderr)


if __name__ == "__main__":
    main()
