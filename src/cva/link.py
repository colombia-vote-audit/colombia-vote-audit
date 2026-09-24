"""Link extracted vote records to legislators.

    uv run python -m cva.link votes.db [--cva data/cva.db]

Adds legislator_id and legislator_match to vote_records in the votes
database written by extract_votes.py, and fills them in. Legislators are
read from the pipeline database, which is opened read-only and without
the pipeline lock, so this can run while a download is going.

A name in a roll call is matched against everyone who held a seat on the
vote's session date. Gazettes write names in many forms ("Pérez Gómez Ana
María", "Ana María Pérez", "Pérez G. Ana M.", "Pacheco Alvar. Álvaro") and
with typos, and Congreso Visible sometimes leaves out a middle name. A name
matches a legislator when:
- every abbreviation ("G.", "Alvar.") starts one of the legislator's names,
- every full word is one of their names, allowing a typo (one in words of
  4+ letters, two in 8+) or a dropped period ("Ros" for "Rosales"),
- at most one word is unknown, and only if all of their surnames and at
  least one given name are matched,
- at least one full word is a surname, and the name has 2+ parts.
When several legislators match, those in the vote's chamber win, then
those matched without typos or extra words. Chamber is only a preference
because Congreso Visible lists some terms under the wrong chamber.

legislator_match is exact, fuzzy (a typo, abbreviation without a period or
unknown word was allowed), ambiguous (more than one legislator fits;
legislator_id is left empty) or none.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import unicodedata
from collections import Counter
from dataclasses import dataclass
from functools import cache

PARTICLES = {"de", "del", "la", "las", "los", "y"}


def fold(s: str | None) -> str:
    """Lowercase ASCII, with names like D'Arce and Jay-Pang joined into one
    word, as Congreso Visible writes them."""
    s = re.sub(r"(?<=\w)['´’-](?=\w)", "", s or "")
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()


def words(s: str | None) -> list[str]:
    return [w for w in re.split(r"[^a-z0-9]+", fold(s)) if w and w not in PARTICLES]


def name_parts(s: str | None) -> list[tuple[str, bool]]:
    """(word, is_abbreviation) for each word; single letters and words
    followed by a period are abbreviations."""
    parts = []
    for m in re.finditer(r"([a-z0-9]+)(\.?)", fold(s)):
        w, dot = m.groups()
        if w not in PARTICLES:
            parts.append((w, bool(dot) or len(w) == 1))
    return parts


def edit_distance(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


@cache
def near(w: str, p: str) -> bool:
    """w is p with a typo (one in words of 4+ letters, two in 8+), or p
    abbreviated without its period ("ros" for "rosales")."""
    if len(w) >= 3 and len(p) > len(w) and p.startswith(w):
        return True
    n = min(len(w), len(p))
    allowed = 2 if n >= 8 else 1 if n >= 4 else 0
    return abs(len(w) - len(p)) <= allowed and 0 < edit_distance(w, p) <= allowed


@dataclass(frozen=True)
class Candidate:
    id: int
    chamber: str
    given: tuple[str, ...]
    surnames: tuple[str, ...]


def match_quality(name: list[tuple[str, bool]], c: Candidate) -> str | None:
    """'exact', 'fuzzy' or None if the name can't be this candidate."""
    if len(name) < 2:
        return None
    pool = [(w, True) for w in c.surnames] + [(w, False) for w in c.given]
    used = [False] * len(pool)
    by_full_word = [False] * len(pool)

    def take(w: str, how: str) -> bool:
        for i, (p, _) in enumerate(pool):
            if not used[i] and (
                (how == "exact" and p == w)
                or (how == "near" and near(w, p))
                or (how == "prefix" and p.startswith(w))
            ):
                used[i] = True
                by_full_word[i] = how != "prefix"
                return True
        return False

    full = [w for w, abbrev in name if not abbrev]
    # Exact words first, so "Alvar" can't take "Alvaro" from a later "Alvaro".
    rest = [w for w in full if not take(w, "exact")]
    unknown = [w for w in rest if not take(w, "near")]
    if any(abbrev and not take(w, "prefix") for w, abbrev in name):
        return None
    if len(unknown) > 1:
        return None
    if not any(hit and is_surname for hit, (_, is_surname) in zip(by_full_word, pool, strict=True)):
        return None  # a surname must be written out
    if unknown:
        # Only trust an unknown word when the rest of the name is complete.
        surnames_done = all(u for u, (_, sur) in zip(used, pool, strict=True) if sur)
        if not (
            surnames_done and any(u for u, (_, sur) in zip(used, pool, strict=True) if not sur)
        ):
            return None
    return "fuzzy" if rest else "exact"


def resolve(name: str, chamber: str | None, candidates: list[Candidate]):
    """(legislator_id or None, legislator_match)."""
    parts = name_parts(name)
    hits = [(c, q) for c in candidates if (q := match_quality(parts, c))]
    if not hits:
        return None, "none"
    by_person: dict[int, tuple[Candidate, str]] = {}
    for c, q in hits:  # a person can hold two terms on one date in bad source data
        if c.id not in by_person or (
            c.chamber == chamber and by_person[c.id][0].chamber != chamber
        ):
            by_person[c.id] = (c, q)
    hits = list(by_person.values())
    for keep in (lambda c, q: c.chamber == chamber, lambda c, q: q == "exact"):
        narrowed = [h for h in hits if keep(*h)]
        if narrowed:
            hits = narrowed
    if len(hits) > 1:
        return None, "ambiguous"
    return hits[0][0].id, hits[0][1]


def load_terms(cva: sqlite3.Connection) -> list[tuple[str, str, Candidate]]:
    rows = cva.execute(
        "SELECT legislator_id, chamber, start_date, end_date, raw_json FROM legislator_terms"
    )
    out = []
    for lid, chamber, start, end, raw in rows:
        m = json.loads(raw)
        cand = Candidate(
            lid, chamber, tuple(words(m.get("nombres"))), tuple(words(m.get("apellidos")))
        )
        out.append((start, end, cand))
    return out


def vote_chamber(chamber: str | None, source_file: str) -> str | None:
    """Chamber in cva's naming. The extractor sometimes leaves it blank; the
    sample's file names carry it."""
    key = words(chamber or "") or words(source_file)
    if "senado" in key:
        return "Senado de la República"
    if "camara" in key:
        return "Cámara de Representantes"
    return None


def ensure_columns(conn: sqlite3.Connection):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(vote_records)")}
    if "legislator_id" not in cols:
        # References legislators.id in the pipeline database (a different
        # file, so SQLite can't enforce it).
        conn.execute("ALTER TABLE vote_records ADD COLUMN legislator_id INTEGER")
    if "legislator_match" not in cols:
        conn.execute("ALTER TABLE vote_records ADD COLUMN legislator_match TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS vote_records_legislator_id ON vote_records (legislator_id)"
    )


def link(votes: sqlite3.Connection, cva: sqlite3.Connection) -> Counter:
    terms = load_terms(cva)

    @cache
    def seated(date: str) -> list[Candidate]:
        return [c for start, end, c in terms if start <= date <= end]

    @cache
    def lookup(name: str, date: str, chamber: str | None):
        return resolve(name, chamber, seated(date))

    rows = votes.execute(
        """
        SELECT r.rowid, r.legislator, v.session_date, d.chamber, d.source_file
        FROM vote_records r JOIN votes v ON v.id = r.vote_id
        JOIN documents d ON d.id = v.document_id
        """
    ).fetchall()
    stats = Counter()
    updates = []
    for rowid, name, date, chamber, source_file in rows:
        lid, how = (
            lookup(name, date, vote_chamber(chamber, source_file)) if date else (None, "none")
        )
        stats[how] += 1
        updates.append((lid, how, rowid))
    with votes:
        ensure_columns(votes)
        votes.executemany(
            "UPDATE vote_records SET legislator_id = ?, legislator_match = ? WHERE rowid = ?",
            updates,
        )
    return stats


def main(argv: list[str] | None = None):
    ap = argparse.ArgumentParser(prog="python -m cva.link", description=__doc__.split("\n")[0])
    ap.add_argument("votes_db", help="database written by extract_votes.py")
    ap.add_argument("--cva", default="data/cva.db", help="pipeline database with legislators")
    args = ap.parse_args(argv)
    cva = sqlite3.connect(f"file:{args.cva}?mode=ro", uri=True)
    votes = sqlite3.connect(args.votes_db)
    stats = link(votes, cva)
    total = sum(stats.values())
    for how in ("exact", "fuzzy", "ambiguous", "none"):
        print(f"{how:10} {stats[how]:7}  {stats[how] / total:6.1%}")


if __name__ == "__main__":
    main()
