"""Second stage after extraction: service windows and absences.

    uv run python -m cva.attendance votes.db [--cva data/cva.db]

Reads a votes database written by extract_votes.py and adds six tables to
it, rebuilt from scratch on every run:

- legislators: name and photo link for every legislator the votes refer to,
  copied from the pipeline database. photo_url points at Congreso Visible's
  server and is NULL when it has no photo.
- legislator_terms: those legislators' chambers, terms and parties, copied
  from the pipeline database.
- text_record_legislators: the legislator behind each name on a plenary vote
  read from the text, where one could be told apart (see below).
- legislator_service: each legislator's time in office per chamber and term,
  taken as the span from their first to their last recorded vote. Replacements,
  resignations and suspensions show up as windows that start late or end early.
- vote_absences: for each verified plenary vote, the legislators whose window in that
  chamber covers the vote's date but who are not on the vote's record. The
  records list only members who voted, so this means absent, or in the chamber
  without voting. in_session marks those who voted on another of these votes
  in the same chamber that day, so were in session but skipped this one.
- vote_attendance: for each verified plenary vote, the date used and where it came
  from, and how many legislators were eligible, voted, were absent, and were
  absent but in session.

Only votes read from scanned voting records (source = 'record') with
verified = 1 and flagged plenary (is_committee = 0) get absences: their names
add up to the printed totals and every row is tied to a legislator, so a
misread row can't make someone who voted look absent. Everyone in the chamber
is expected to vote in a plenary; committee votes are left out because
committee membership isn't known.

A vote without a session date takes the date of the other votes in its gazette
when they all share one (a House plenary acta records a single session);
otherwise it is skipped.

Names on votes read from the text are printed as the gazette has them, not
tied to a legislator. Each is compared, as words without accents or small
words like "de", with the members sitting in either chamber on the vote's date
(chamber labels are sometimes wrong), else on its gazette's publication date.
It matches when one name's words contain the other's and they share at least
two ("Luna Sánchez David Andrés" is David Luna Sanchez); failing that, when
all but one word are the same and that one is a typo away ("Benedeti" for
Benedetti). A name is tied to a legislator only if exactly one member fits,
and not if another name on the same vote fits that member too. These matches
don't affect service windows or absences, which come from checked records
only.

The pipeline database is opened read-only, for terms, names and photo links
only.
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import unicodedata
from collections import Counter, defaultdict
from difflib import SequenceMatcher

from cva.gazette import publication_date

SCHEMA = """
DROP TABLE IF EXISTS legislators;
DROP TABLE IF EXISTS legislator_terms;
DROP TABLE IF EXISTS legislator_service;
DROP TABLE IF EXISTS vote_absences;
DROP TABLE IF EXISTS vote_attendance;
DROP TABLE IF EXISTS text_record_legislators;

CREATE TABLE legislators (
    id            INTEGER PRIMARY KEY,  -- legislators.id in the pipeline database
    name          TEXT NOT NULL,
    photo_url     TEXT                  -- image on Congreso Visible's server; NULL if none
);

CREATE TABLE legislator_terms (
    legislator_id INTEGER NOT NULL,
    chamber       TEXT NOT NULL,      -- as in the pipeline database, e.g. 'Senado de la República'
    start_date    TEXT NOT NULL,      -- constitutional term dates, 20 July to 19 July
    end_date      TEXT NOT NULL,
    party         TEXT,
    PRIMARY KEY (legislator_id, chamber, start_date)
);

CREATE TABLE text_record_legislators (
    vote_id       INTEGER NOT NULL REFERENCES votes (id) ON DELETE CASCADE,
    legislator    TEXT NOT NULL,      -- vote_records.legislator: the name as printed
    legislator_id INTEGER NOT NULL,
    how           TEXT NOT NULL CHECK (how IN ('exact', 'close')),  -- close: one word misspelled
    UNIQUE (vote_id, legislator)
);

CREATE TABLE legislator_service (
    legislator_id INTEGER NOT NULL,   -- legislators.id in the pipeline database
    chamber       TEXT NOT NULL,      -- as in documents.chamber
    term_start    TEXT,               -- start of the term the votes fall in, if known
    first_vote    TEXT NOT NULL,
    last_vote     TEXT NOT NULL,
    votes         INTEGER NOT NULL,   -- recorded votes in the window
    PRIMARY KEY (legislator_id, chamber, term_start)
);

CREATE TABLE vote_absences (
    vote_id       INTEGER NOT NULL REFERENCES votes (id) ON DELETE CASCADE,
    legislator_id INTEGER NOT NULL,
    legislator    TEXT NOT NULL,      -- name from the pipeline database
    in_session    INTEGER NOT NULL,   -- 1 if they voted on another checked plenary vote in
                                      -- the same chamber that day
    UNIQUE (vote_id, legislator_id)
);
CREATE INDEX vote_absences_legislator ON vote_absences (legislator_id);

CREATE TABLE vote_attendance (
    vote_id      INTEGER PRIMARY KEY REFERENCES votes (id) ON DELETE CASCADE,
    session_date TEXT,                -- date used; NULL if none could be found
    date_source  TEXT NOT NULL,       -- vote | gazette | none
    eligible     INTEGER NOT NULL,    -- legislators whose window covers the date
    voted        INTEGER NOT NULL,
    absent       INTEGER NOT NULL,
    absent_in_session INTEGER NOT NULL  -- absent, but voted on another of these votes that day
);
"""


def vote_dates(votes: sqlite3.Connection) -> dict[int, tuple[str | None, str]]:
    """(date, source) for every vote: its own session date, else the one date
    shared by the other votes in its gazette, else (None, 'none')."""
    rows = votes.execute(
        "SELECT id, document_id, nullif(trim(session_date), '') FROM votes"
    ).fetchall()
    by_doc = defaultdict(set)
    for _, doc, date in rows:
        if date:
            by_doc[doc].add(date)
    out = {}
    for vid, doc, date in rows:
        if date:
            out[vid] = (date, "vote")
        elif len(by_doc[doc]) == 1:
            out[vid] = (next(iter(by_doc[doc])), "gazette")
        else:
            out[vid] = (None, "none")
    return out


def load_terms(cva: sqlite3.Connection) -> dict[int, list[tuple[str, str]]]:
    terms = defaultdict(list)
    for lid, start, end in cva.execute(
        "SELECT legislator_id, start_date, end_date FROM legislator_terms"
    ):
        terms[lid].append((start, end))
    return terms


def term_of(terms: list[tuple[str, str]], date: str) -> str | None:
    return next((start for start, end in terms if start <= date <= end), None)


SMALL_WORDS = {"de", "del", "la", "las", "los", "y"}


def name_words(name: str) -> frozenset[str]:
    """A name's words, lowercase, without accents or small words like 'de'."""
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    return frozenset(w for w in re.findall(r"[a-z]+", s) if w not in SMALL_WORDS)


def same_person(printed: frozenset[str], full: frozenset[str]) -> bool:
    """One name's words contain the other's, and they share at least two."""
    return len(printed & full) >= 2 and (printed <= full or full <= printed)


def misspelled(printed: frozenset[str], full: frozenset[str]) -> bool:
    """All but one printed word are in the full name, and that one is about 80%
    like one of its words."""

    def close(w):
        return any(
            w == f or len(w) > 3 and SequenceMatcher(None, w, f).ratio() >= 0.8 for f in full
        )

    return (
        len(printed) >= 2 and len(printed & full) >= len(printed) - 1 and all(map(close, printed))
    )


def match_text_names(
    votes: sqlite3.Connection, cva: sqlite3.Connection, dates: dict
) -> tuple[list[tuple], int, int]:
    """(vote_id, printed name, legislator_id, 'exact' | 'close') for the names on
    plenary text votes that fit exactly one sitting member, and the numbers of
    names that fit several members and none."""
    full = {lid: name_words(name) for lid, name in cva.execute("SELECT id, name FROM legislators")}
    terms = cva.execute(
        "SELECT legislator_id, start_date, end_date FROM legislator_terms"
    ).fetchall()
    published = dict(
        votes.execute(
            "SELECT v.id, d.publication_date FROM votes v JOIN documents d ON d.id = v.document_id"
        )
    )
    sitting: dict[str, list[int]] = {}
    found: dict[tuple, tuple[list[int], str]] = {}

    def fits(printed, date):
        if date not in sitting:
            sitting[date] = sorted({lid for lid, start, end in terms if start <= date <= end})
        for how, test in (("exact", same_person), ("close", misspelled)):
            hits = [lid for lid in sitting[date] if lid in full and test(printed, full[lid])]
            if hits:
                return hits, how
        return [], ""

    by_vote = defaultdict(list)
    ambiguous = unmatched = 0
    for vid, name in votes.execute(
        "SELECT r.vote_id, r.legislator FROM vote_records r JOIN votes v ON v.id = r.vote_id"
        " WHERE v.source = 'text' AND coalesce(v.is_committee, 0) = 0"
        " AND r.legislator_id IS NULL"
    ).fetchall():
        date = dates[vid][0] or publication_date(published[vid])
        key = (name_words(name), date)
        if key not in found:
            found[key] = fits(*key) if date else ([], "")
        hits, how = found[key]
        if len(hits) == 1:
            by_vote[vid].append((name, hits[0], how))
        elif hits:
            ambiguous += 1
        else:
            unmatched += 1
    matches = []
    for vid, named in by_vote.items():
        times = Counter(lid for _, lid, _ in named)
        for name, lid, how in named:
            if times[lid] > 1:  # two names on the vote fit the same member
                ambiguous += 1
            else:
                matches.append((vid, name, lid, how))
    return matches, ambiguous, unmatched


def build(votes: sqlite3.Connection, cva: sqlite3.Connection) -> dict:
    dates = vote_dates(votes)
    terms = load_terms(cva)
    people = {
        lid: (name, photo)
        for lid, name, photo in cva.execute("SELECT id, name, photo_url FROM legislators")
    }
    names = {lid: name for lid, (name, _) in people.items()}
    chamber_of = dict(
        votes.execute(
            "SELECT v.id, d.chamber FROM votes v JOIN documents d ON d.id = v.document_id"
        )
    )
    voters = defaultdict(set)
    for vid, lid in votes.execute(
        "SELECT vote_id, legislator_id FROM vote_records WHERE legislator_id IS NOT NULL"
    ):
        voters[vid].add(lid)

    # Service windows: first and last recorded vote per legislator, chamber and term.
    windows: dict[tuple, list] = {}
    for vid, lids in voters.items():
        date = dates[vid][0]
        if not date:
            continue
        for lid in lids:
            key = (lid, chamber_of[vid], term_of(terms[lid], date))
            w = windows.setdefault(key, [date, date, 0])
            w[0], w[1], w[2] = min(w[0], date), max(w[1], date), w[2] + 1

    def eligible(chamber: str, date: str) -> set[int]:
        return {
            lid
            for (lid, ch, term), (first, last, _) in windows.items()
            if ch == chamber and first <= date <= last and term == term_of(terms[lid], date)
        }

    checked = [
        vid
        for (vid,) in votes.execute(
            "SELECT id FROM votes WHERE verified = 1 AND source = 'record' AND is_committee = 0"
            " ORDER BY id"
        )
    ]
    # Who voted on at least one checked vote, per chamber and day.
    in_session = defaultdict(set)
    for vid in checked:
        if dates[vid][0]:
            in_session[chamber_of[vid], dates[vid][0]] |= voters[vid]

    matches, ambiguous, unmatched = match_text_names(votes, cva, dates)

    absences, attendance = [], []
    for vid in checked:
        date, source = dates[vid]
        if not date:
            attendance.append((vid, None, source, 0, len(voters[vid]), 0, 0))
            continue
        pool = eligible(chamber_of[vid], date)
        missing = sorted(pool - voters[vid], key=lambda lid: names.get(lid, ""))
        present = in_session[chamber_of[vid], date]
        absences += [(vid, lid, names.get(lid, str(lid)), int(lid in present)) for lid in missing]
        skipped = sum(lid in present for lid in missing)
        attendance.append((vid, date, source, len(pool), len(voters[vid]), len(missing), skipped))

    with votes:
        votes.executescript(SCHEMA)
        referenced = {lid for lids in voters.values() for lid in lids}
        referenced |= {lid for _, _, lid, _ in matches}
        legislators = [(lid, *people[lid]) for lid in sorted(referenced) if lid in people]
        votes.executemany("INSERT INTO legislators VALUES (?, ?, ?)", legislators)
        votes.executemany(
            "INSERT INTO legislator_terms VALUES (?, ?, ?, ?, ?)",
            [
                row
                for row in cva.execute(
                    "SELECT legislator_id, chamber, start_date, end_date, party"
                    " FROM legislator_terms ORDER BY 1, 3"
                )
                if row[0] in referenced
            ],
        )
        votes.executemany(
            "INSERT INTO legislator_service VALUES (?, ?, ?, ?, ?, ?)",
            [(lid, ch, term, *w) for (lid, ch, term), w in windows.items()],
        )
        votes.executemany("INSERT INTO vote_absences VALUES (?, ?, ?, ?)", absences)
        votes.executemany("INSERT INTO vote_attendance VALUES (?, ?, ?, ?, ?, ?, ?)", attendance)
        votes.executemany("INSERT INTO text_record_legislators VALUES (?, ?, ?, ?)", matches)
    return {
        "legislators": len(legislators),
        "service_windows": len(windows),
        "verified_votes": len(attendance),
        "undated_skipped": sum(1 for a in attendance if a[1] is None),
        "dated_from_gazette": sum(1 for a in attendance if a[2] == "gazette"),
        "absences": len(absences),
        "absent_in_session": sum(a[3] for a in absences),
        "text_names_matched": len(matches),
        "of_them_misspelled": sum(m[3] == "close" for m in matches),
        "text_names_ambiguous": ambiguous,
        "text_names_unmatched": unmatched,
    }


def main(argv: list[str] | None = None):
    ap = argparse.ArgumentParser(
        prog="python -m cva.attendance", description=__doc__.split("\n")[0]
    )
    ap.add_argument("votes_db", help="database written by extract_votes.py")
    ap.add_argument("--cva", default="data/cva.db", help="pipeline database with legislators")
    args = ap.parse_args(argv)
    cva = sqlite3.connect(f"file:{args.cva}?mode=ro", uri=True)
    votes = sqlite3.connect(args.votes_db)
    for k, v in build(votes, cva).items():
        print(f"{k.replace('_', ' '):20} {v}")


if __name__ == "__main__":
    main()
