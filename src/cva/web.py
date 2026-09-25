"""Read-only web API over a votes database, and the site that uses it.

    uv run python -m cva.web votes.db [--pdfs data/pdfs] [--static web/dist]

The votes database is the one written by extract_votes.py, after
cva.attendance has added the legislator tables. It is opened read-only.
Committee votes (is_committee = 1) are left out everywhere; votes with no
committee flag are kept.

Every vote gets a date for sorting and filtering: its own session date, else
the one date shared by the other votes in its gazette, else the gazette's
publication date. date_source says which ('vote', 'gazette' or
'publication'), so the site can mark dates that aren't the vote's own.

The summary of every vote is held in memory, so search, filters and sorting
don't touch the database. Restart the server after the database is rebuilt.

Endpoints:

    GET /api/stats
    GET /api/votes?q=&from=&to=&chamber=&offset=&limit=
    GET /api/votes/{id}
    GET /api/legislators?q=
    GET /api/legislators/{id}
    GET /pdf/{document id}      the gazette PDF, when --pdfs is given
    GET /download-db            the votes database itself, for anyone to use

Built frontend files under /assets/ have content hashes in their names and
are cached for a year; API answers for five minutes, since they only change
on a restart; index.html is revalidated on every load, so deploys show up.
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import unicodedata
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from cva.gazette import publication_date
from cva.store import BlobStore

ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
REQUIRED_TABLES = {
    "legislators",
    "legislator_terms",
    "legislator_service",
    "vote_absences",
    "vote_attendance",
    "text_record_legislators",
}


def fold(s: str | None) -> str:
    """Lowercase without accents, for search."""
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def iso_date(s: str | None) -> str | None:
    s = (s or "").strip()
    return s if ISO_DATE.fullmatch(s) else None


def chamber_matches(short: str, full: str) -> bool:
    """documents.chamber says 'Cámara' or 'Senado'; the terms say
    'Cámara de Representantes' or 'Senado de la República'."""
    return bool(short) and full.startswith(short)


class Data:
    """Vote summaries, legislators and terms, loaded once from the votes database."""

    def __init__(self, path: Path):
        self.path = path
        conn = self.connect()
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
        if missing := REQUIRED_TABLES - tables:
            raise SystemExit(
                f"{path} has no {', '.join(sorted(missing))}; run python -m cva.attendance on it"
            )
        if "in_session" not in {r[1] for r in conn.execute("PRAGMA table_info(vote_absences)")}:
            raise SystemExit(f"{path} is from an older cva.attendance; run it again")
        self.legislators = {
            lid: {"id": lid, "name": name, "photo_url": photo}
            for lid, name, photo in conn.execute("SELECT id, name, photo_url FROM legislators")
        }
        self.terms = defaultdict(list)
        for lid, chamber, start, end, party in conn.execute(
            "SELECT legislator_id, chamber, start_date, end_date, party"
            " FROM legislator_terms ORDER BY start_date"
        ):
            self.terms[lid].append({"chamber": chamber, "start": start, "end": end, "party": party})
        self.votes = self.load_votes(conn)
        self.order = sorted(
            self.votes.values(), key=lambda v: (v["date"] or "", v["id"]), reverse=True
        )
        self.records = conn.execute(
            "SELECT count(*) FROM vote_records WHERE vote_id IN"
            " (SELECT id FROM votes WHERE coalesce(is_committee, 0) = 0)"
        ).fetchone()[0]
        conn.close()

    def connect(self) -> sqlite3.Connection:
        return sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)

    def load_votes(self, conn: sqlite3.Connection) -> dict[int, dict]:
        rows = conn.execute(
            """
            SELECT v.id, v.document_id, v.session_date, v.is_committee, v.subject,
                   v.bill_name, v.bill_title, v.description, v.acta, v.result, v.vote_type,
                   v.source, v.verified, d.chamber, d.publication_date, d.gaceta_number,
                   t.yes_count, t.no_count, t.abstain_count,
                   (SELECT count(*) FROM vote_absences a WHERE a.vote_id = v.id),
                   (SELECT count(*) FROM vote_absences a WHERE a.vote_id = v.id AND a.in_session),
                   EXISTS (SELECT 1 FROM vote_attendance a WHERE a.vote_id = v.id)
            FROM votes v
            JOIN documents d ON d.id = v.document_id
            JOIN vote_totals t ON t.vote_id = v.id
            """
        ).fetchall()
        doc_dates = defaultdict(set)
        for r in rows:
            if date := iso_date(r[2]):
                doc_dates[r[1]].add(date)
        votes = {}
        for (vid, doc, session, committee, subject, bill_name, bill_title, description,
             acta, result, vote_type, source, verified, chamber, published, gaceta,
             yes, no, abstain, absent, in_session, has_attendance) in rows:  # fmt: skip
            if committee == 1:
                continue
            if date := iso_date(session):
                date_source = "vote"
            elif len(doc_dates[doc]) == 1:
                date, date_source = next(iter(doc_dates[doc])), "gazette"
            elif date := publication_date(published):
                date_source = "publication"
            else:
                date_source = None
            votes[vid] = {
                "id": vid,
                "document_id": doc,
                "date": date,
                "date_source": date_source,
                "chamber": chamber or None,
                "subject": subject or None,
                "bill_name": bill_name or None,
                "gaceta_number": gaceta or None,
                "vote_type": vote_type,
                "result": result,
                "source": source,
                "verified": verified,
                "counts": {
                    "yes": yes,
                    "no": no,
                    "abstain": abstain,
                    # Known only where the attendance stage could check the record.
                    "absent": absent if has_attendance else None,
                    # Of those, how many voted on another checked vote that day.
                    "absent_in_session": in_session if has_attendance else None,
                },
                "_search": fold(
                    " ".join(filter(None, [subject, bill_name, bill_title, description, acta]))
                ),
            }
        return votes

    def party_on(self, lid: int, chamber: str | None, date: str | None) -> str | None:
        """The legislator's party in the term covering the date, preferring the
        term in the vote's chamber."""
        terms = self.terms.get(lid, [])
        if date:
            covering = [t for t in terms if t["start"] <= date <= t["end"]]
            here = [t for t in covering if chamber_matches(chamber or "", t["chamber"])]
            if here or covering:
                return (here or covering)[0]["party"]
        return terms[-1]["party"] if terms else None

    def person(self, lid: int | None, name: str, chamber, date) -> dict:
        if lid is None or lid not in self.legislators:
            return {"id": None, "name": name, "photo_url": None, "party": None}
        return {**self.legislators[lid], "party": self.party_on(lid, chamber, date)}


def cache_policy(path: str) -> str:
    if path.startswith("/assets/"):
        return "public, max-age=31536000, immutable"
    if path.startswith("/pdf/"):
        return "public, max-age=86400"
    if path.startswith("/api/"):
        return "public, max-age=300"
    return "no-cache"


class CacheHeaders:
    """Adds Cache-Control, by path, to successful responses."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        policy = cache_policy(scope["path"]).encode()

        async def send_with_policy(message):
            if message["type"] == "http.response.start" and message["status"] in (200, 206):
                message["headers"] = [*message.get("headers", []), (b"cache-control", policy)]
            await send(message)

        await self.app(scope, receive, send_with_policy)


def summary(v: dict) -> dict:
    return {k: val for k, val in v.items() if not k.startswith("_")}


def query_int(request: Request, key: str, default: int, most: int) -> int:
    try:
        return max(0, min(int(request.query_params.get(key, default)), most))
    except ValueError:
        raise HTTPException(400, f"{key} must be a number") from None


def create_app(votes_db: Path, pdfs: Path | None = None, static: Path | None = None):
    data = Data(votes_db)
    store = BlobStore(pdfs) if pdfs else None

    def pdf_path(sha256: str) -> Path | None:
        path = store and store.path_for(sha256)
        return path if path and path.is_file() else None

    def stats(request: Request):
        return JSONResponse(
            {
                "votes": len(data.votes),
                "records": data.records,
                "legislators": len(data.legislators),
                "database_bytes": votes_db.stat().st_size,
            }
        )

    def votes(request: Request):
        words = fold(request.query_params.get("q")).split()
        since = iso_date(request.query_params.get("from"))
        until = iso_date(request.query_params.get("to"))
        chamber = request.query_params.get("chamber")
        offset = query_int(request, "offset", 0, 10**9)
        limit = query_int(request, "limit", 50, 200)
        found = [
            v
            for v in data.order
            if all(w in v["_search"] for w in words)
            and (not since or (v["date"] and v["date"] >= since))
            and (not until or (v["date"] and v["date"] <= until))
            and (not chamber or v["chamber"] == chamber)
        ]
        return JSONResponse(
            {"total": len(found), "votes": [summary(v) for v in found[offset : offset + limit]]}
        )

    def vote(request: Request):
        vid = request.path_params["id"]
        if vid not in data.votes:
            raise HTTPException(404, "no such vote")
        v = data.votes[vid]
        conn = data.connect()
        try:
            acta, bill_title, description, page, check_note = conn.execute(
                "SELECT acta, bill_title, description, page, check_note FROM votes WHERE id = ?",
                (vid,),
            ).fetchone()
            published, sha256 = conn.execute(
                "SELECT publication_date, sha256 FROM documents WHERE id = ?",
                (v["document_id"],),
            ).fetchone()
            # Names on text votes are tied to legislators by the attendance stage.
            records = conn.execute(
                "SELECT r.legislator, r.vote, coalesce(r.legislator_id, m.legislator_id)"
                " FROM vote_records r LEFT JOIN text_record_legislators m"
                " ON m.vote_id = r.vote_id AND m.legislator = r.legislator WHERE r.vote_id = ?",
                (vid,),
            ).fetchall()
            absent = conn.execute(
                "SELECT legislator_id, legislator, in_session FROM vote_absences WHERE vote_id = ?",
                (vid,),
            ).fetchall()
        finally:
            conn.close()
        groups = {"yes": [], "no": [], "abstain": [], "absent": []}
        for name, position, lid in records:
            groups[position].append(data.person(lid, name, v["chamber"], v["date"]))
        for lid, name, in_session in absent:
            person = data.person(lid, name, v["chamber"], v["date"])
            groups["absent"].append({**person, "in_session": bool(in_session)})
        for people in groups.values():
            people.sort(key=lambda p: fold(p["name"]))
        if v["counts"]["absent"] is None:
            groups["absent"] = None
        return JSONResponse(
            {
                **summary(v),
                "acta": acta or None,
                "bill_title": bill_title or None,
                "description": description or None,
                "check_note": check_note,
                "gazette": {
                    "number": v["gaceta_number"],
                    "published": publication_date(published),
                    "page": page,
                    "pdf": f"/pdf/{v['document_id']}" if pdf_path(sha256) else None,
                },
                "groups": groups,
            }
        )

    def legislators(request: Request):
        words = fold(request.query_params.get("q")).split()
        out = []
        for p in data.legislators.values():
            if not all(w in fold(p["name"]) for w in words):
                continue
            terms = data.terms.get(p["id"], [])
            out.append({**p, "party": terms[-1]["party"] if terms else None, "terms": terms})
        out.sort(key=lambda p: fold(p["name"]))
        return JSONResponse({"total": len(out), "legislators": out})

    def legislator(request: Request):
        lid = request.path_params["id"]
        if lid not in data.legislators:
            raise HTTPException(404, "no such legislator")
        conn = data.connect()
        try:
            positions = conn.execute(
                "SELECT vote_id, vote, NULL FROM vote_records WHERE legislator_id = ?"
                " UNION ALL SELECT r.vote_id, r.vote, NULL FROM text_record_legislators m"
                " JOIN vote_records r ON r.vote_id = m.vote_id AND r.legislator = m.legislator"
                " WHERE m.legislator_id = ?"
                " UNION ALL SELECT vote_id, 'absent', in_session FROM vote_absences"
                " WHERE legislator_id = ?",
                (lid, lid, lid),
            ).fetchall()
            service = [
                {
                    "chamber": ch,
                    "term_start": term,
                    "first_vote": first,
                    "last_vote": last,
                    "votes": n,
                }  # fmt: skip
                for ch, term, first, last, n in conn.execute(
                    "SELECT chamber, term_start, first_vote, last_vote, votes"
                    " FROM legislator_service WHERE legislator_id = ? ORDER BY first_vote",
                    (lid,),
                )
            ]
        finally:
            conn.close()
        record = []
        totals = {"yes": 0, "no": 0, "abstain": 0, "absent": 0, "absent_in_session": 0}
        for vid, position, in_session in positions:
            if vid not in data.votes:  # a committee vote
                continue
            v = data.votes[vid]
            totals[position] += 1
            totals["absent_in_session"] += bool(in_session)
            record.append(
                {
                    **summary(v),
                    "position": position,
                    "in_session": None if in_session is None else bool(in_session),
                    "party_then": data.party_on(lid, v["chamber"], v["date"]),
                }
            )
        record.sort(key=lambda r: (r["date"] or "", r["id"]), reverse=True)
        terms = data.terms.get(lid, [])
        return JSONResponse(
            {
                **data.legislators[lid],
                "party": terms[-1]["party"] if terms else None,
                "terms": terms,
                "service": service,
                "totals": totals,
                "record": record,
            }
        )

    def pdf(request: Request):
        doc = request.path_params["id"]
        conn = data.connect()
        try:
            row = conn.execute(
                "SELECT sha256, gaceta_number FROM documents WHERE id = ?", (doc,)
            ).fetchone()
        finally:
            conn.close()
        path = row and pdf_path(row[0])
        if not path:
            raise HTTPException(404, "PDF not available")
        return FileResponse(
            path,
            media_type="application/pdf",
            filename=f"gaceta-{row[1] or doc}.pdf",
            content_disposition_type="inline",
        )

    def download_db(request: Request):
        """The whole database the site reads, committee votes included."""
        day = datetime.fromtimestamp(votes_db.stat().st_mtime, UTC).date()
        return FileResponse(
            votes_db,
            media_type="application/vnd.sqlite3",
            filename=f"colombia-vote-audit-{day}.db",
        )

    async def error(request: Request, exc: HTTPException):
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)

    routes = [
        Route("/api/stats", stats),
        Route("/api/votes", votes),
        Route("/api/votes/{id:int}", vote),
        Route("/api/legislators", legislators),
        Route("/api/legislators/{id:int}", legislator),
        Route("/pdf/{id:int}", pdf),
        Route("/download-db", download_db),
    ]
    if static:
        routes.append(Mount("/", StaticFiles(directory=static, html=True)))
    return Starlette(
        routes=routes,
        middleware=[Middleware(CacheHeaders)],
        exception_handlers={HTTPException: error},
    )


def main(argv: list[str] | None = None):
    import uvicorn

    ap = argparse.ArgumentParser(prog="python -m cva.web", description=__doc__.split("\n")[0])
    ap.add_argument("votes_db", type=Path, help="votes database, after cva.attendance")
    ap.add_argument("--pdfs", type=Path, help="PDF store of the pipeline, e.g. data/pdfs")
    ap.add_argument("--static", type=Path, help="built frontend to serve at /, e.g. web/dist")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args(argv)
    uvicorn.run(create_app(args.votes_db, args.pdfs, args.static), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
