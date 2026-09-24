"""Build a small, stratified set of gazettes for parser development.

The full archive is ~31k gazettes. Layout, tooling and text quality change
over the years and differ by document type and chamber, so the sample
covers every (era, document category, chamber) cell plus known edge cases.
Downloads go through the normal fetch pipeline, so building the sample
also exercises it.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import random
import re
import sqlite3
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

# Boundaries follow changes seen in the files: PageMaker-era PDFs with
# gaps in the archive (2000-03), InDesign output with broken font encoding
# (seen in 2016), and much larger recent issues (2021+).
ERAS = [(2000, 2003), (2004, 2009), (2010, 2015), (2016, 2019), (2020, 2022), (2023, 2030)]

# Categories that are likely to contain roll-call votes get extra samples.
VOTE_BEARING = {"acta_plenaria", "acta_comision", "unlabeled"}


def doc_category(doc_type: str) -> str:
    d = doc_type.lower()
    if not d:
        return "unlabeled"
    if d.startswith("acta de plenaria") or d.startswith("acta de congreso pleno"):
        return "acta_plenaria"
    if d.startswith("acta de comisi"):
        return "acta_comision"
    if d.startswith("acta"):
        return "acta_other"
    if "conciliaci" in d:
        return "conciliacion"
    if "ponencia" in d:
        return "ponencia"
    if d.startswith("proyecto"):
        return "proyecto"
    if d.startswith(("ley", "texto")):
        return "ley_texto"
    return "other"


def era_of(year: int) -> str | None:
    for lo, hi in ERAS:
        if lo <= year <= hi:
            return f"{lo}-{hi}"
    return None


def chamber_short(chamber: str) -> str:
    return "senado" if chamber.lower().startswith("senado") else "camara"


@dataclass
class SampleItem:
    gazette_id: int
    number: str
    chamber: str
    date: str
    doc_type: str
    category: str
    era: str | None
    reasons: list[str] = field(default_factory=list)
    fetch_status: str | None = None
    file: str | None = None
    sha256: str | None = None
    size: int | None = None
    pages: int | None = None
    producer: str | None = None
    chars_per_page: float | None = None
    text_quality: str | None = None
    note: str | None = None


def select(conn: sqlite3.Connection, seed: int = 7) -> list[SampleItem]:
    rng = random.Random(seed)
    rows = conn.execute("SELECT * FROM gazettes").fetchall()
    cells: dict[tuple, list[sqlite3.Row]] = {}
    for r in rows:
        era = era_of(r["year"])
        if era is None or not r["downloadable"]:
            continue
        key = (era, doc_category(r["doc_type"]), chamber_short(r["chamber"]))
        cells.setdefault(key, []).append(r)

    picked: dict[int, SampleItem] = {}

    def add(r: sqlite3.Row, reason: str):
        item = picked.get(r["id"])
        if item is None:
            item = picked[r["id"]] = SampleItem(
                gazette_id=r["id"],
                number=r["number"],
                chamber=r["chamber"],
                date=r["date"],
                doc_type=r["doc_type"],
                category=doc_category(r["doc_type"]),
                era=era_of(r["year"]),
            )
        item.reasons.append(reason)

    for key in sorted(cells):
        era, cat, chamber = key
        n = 2 if cat in VOTE_BEARING else 1
        for r in rng.sample(cells[key], min(n, len(cells[key]))):
            add(r, f"stratum:{era}/{cat}/{chamber}")

    edge_queries = {
        "edge:leading_zero_number": (
            "SELECT * FROM gazettes WHERE number LIKE '0%' AND downloadable"
        ),
        "edge:non_numeric_number": "SELECT * FROM gazettes WHERE number GLOB '*[^0-9]*'",
        "edge:bad_year": "SELECT * FROM gazettes WHERE year < 1990",
        "edge:not_downloadable": "SELECT * FROM gazettes WHERE NOT downloadable",
        "edge:duplicate_year_number": """
            SELECT g.* FROM gazettes g JOIN (
                SELECT year, number_norm FROM gazettes GROUP BY 1, 2 HAVING count(*) > 1
                ORDER BY year DESC LIMIT 2
            ) d USING (year, number_norm)
        """,
        "edge:known_empty_file": (
            "SELECT * FROM gazettes WHERE number = '1018' AND date = '2011-12-29'"
        ),
    }
    for reason, sql in edge_queries.items():
        found = conn.execute(sql).fetchall()
        limit = 4 if "duplicate" in reason else 2
        for r in rng.sample(found, min(limit, len(found))):
            add(r, reason)
    return list(picked.values())


# Common Spanish function words; real text is full of them, mis-encoded
# text has almost none.
_STOPWORDS = set("de la el que y en los las del por con para se a al no su es lo como".split())
_WORD = re.compile(r"[a-záéíóúñü]+", re.I)


def text_quality(text: str, pages: int) -> tuple[float, str]:
    chars_per_page = len(text.strip()) / max(pages, 1)
    if chars_per_page < 200:
        return chars_per_page, "no_text"
    words = _WORD.findall(text.lower())
    ratio = sum(w in _STOPWORDS for w in words) / max(len(words), 1)
    return chars_per_page, "ok" if ratio >= 0.15 else "garbled"


def analyze(path: Path) -> dict:
    from pypdf import PdfReader

    reader = PdfReader(path)
    n = len(reader.pages)
    # First pages hold the masthead and index; later ones the body.
    probe = sorted({0, 1, 2, n // 2, n - 1} & set(range(n)))
    text = "".join((reader.pages[i].extract_text() or "") for i in probe)
    cpp, quality = text_quality(text, len(probe))
    meta = reader.metadata or {}
    producer = " / ".join(str(meta.get(k)) for k in ("/Creator", "/Producer") if meta.get(k))
    return {"pages": n, "chars_per_page": round(cpp), "text_quality": quality, "producer": producer}


def materialize(conn, store, items: list[SampleItem], out: Path):
    """Link downloaded files into `out` with readable names and analyze them."""
    pdf_dir = out / "pdfs"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    for item in items:
        g = conn.execute("SELECT * FROM gazettes WHERE id = ?", (item.gazette_id,)).fetchone()
        item.fetch_status = g["fetch_status"]
        if g["fetch_status"] != "ok":
            item.note = g["last_error"]
            continue
        name = f"{g['date'][:4]}-{g['number']}-{chamber_short(g['chamber'])}-{item.category}.pdf"
        dest = pdf_dir / name
        if not dest.exists():
            os.link(store.path_for(g["sha256"]), dest)
        item.file = f"pdfs/{name}"
        item.sha256, item.size = g["sha256"], g["size"]
        try:
            for k, v in analyze(dest).items():
                setattr(item, k, v)
        except Exception as e:
            item.note = f"analysis failed: {type(e).__name__}: {e}"


def write_manifest(items: list[SampleItem], out: Path):
    out.mkdir(parents=True, exist_ok=True)
    rows = [asdict(i) for i in sorted(items, key=lambda i: (i.date, i.number))]
    (out / "manifest.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1))
    with open(out / "manifest.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        for r in rows:
            w.writerow({**r, "reasons": ";".join(r["reasons"])})


def summarize(items: list[SampleItem]) -> str:
    def table(title, counter):
        lines = [f"### {title}", "", "| value | count |", "|---|---|"]
        lines += [f"| {k} | {v} |" for k, v in sorted(counter.items(), key=lambda kv: str(kv[0]))]
        return "\n".join(lines) + "\n"

    ok = [i for i in items if i.fetch_status == "ok"]
    sizes = sorted(i.size for i in ok)
    pages = sorted(i.pages for i in ok if i.pages)
    parts = [
        "# Gazette sample\n",
        f"{len(items)} gazettes selected, {len(ok)} downloaded, {sum(sizes) / 1e6:.0f} MB total.\n",
        f"Size: min {sizes[0] / 1e6:.2f} MB, median {sizes[len(sizes) // 2] / 1e6:.2f} MB, "
        f"max {sizes[-1] / 1e6:.2f} MB. Pages: min {pages[0]}, median "
        f"{pages[len(pages) // 2]}, max {pages[-1]}.\n"
        if sizes and pages
        else "",
        table("Era", Counter(i.era for i in items)),
        table("Document category", Counter(i.category for i in items)),
        table("Chamber", Counter(chamber_short(i.chamber) for i in items)),
        table("Fetch status", Counter(i.fetch_status for i in items)),
        table("Text layer (downloaded only)", Counter(i.text_quality for i in ok)),
        table(
            "Text layer by era",
            Counter(f"{i.era} {i.text_quality}" for i in ok),
        ),
        table("PDF producer", Counter((i.producer or "?")[:60] for i in ok)),
    ]
    return "\n".join(parts)
