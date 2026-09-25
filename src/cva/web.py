"""Web API over a votes database, and the site that uses it.

    uv run python -m cva.web votes.db [--pdfs data/pdfs] [--static web/dist]

The votes database is the one written by extract_votes.py, after
cva.attendance has added the legislator tables. It is only read, apart from
the comments posted on legislators' pages (cva.comments), which are saved in
it. Committee votes (is_committee = 1) are left out everywhere; votes with no
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
    GET /api/legislators/{id}/comments      notes and links posted about them (cva.comments)
    POST /api/legislators/{id}/comments     {organization, author, body}: post one, as
                                            JSON or as form data with up to 5 files
    GET /pdf/{document id}      the gazette PDF, when --pdfs is given
    GET /files/{sha256}/{name}  a file attached to a comment
    GET /download-db            the votes database itself, gzipped, for anyone to use
    GET /api/barcode            every House member's ballot on every checked roll call
                                (cva.barcode), and whether the question box is on
    POST /api/ask               {turns: [{role, content}], view, lang}: a question about
                                the barcode, answered by a language model with text and
                                a view to show (cva.ask)

The question box is on when CVA_LLM_BASE_URL, CVA_LLM_API_KEY and
CVA_LLM_MODEL are set; each visitor gets ASK_LIMIT questions per ASK_WINDOW
seconds, since every one is paid for.

Built frontend files under /assets/ have content hashes in their names and
are cached for a year; API answers (GET) for five minutes, since they only
change on a restart, except comments, which aren't cached; index.html is
revalidated on every load, so deploys show up.
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import shutil
import sqlite3
import time
from collections import defaultdict, deque
from datetime import UTC, datetime
from pathlib import Path

import httpx
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from cva import ask as asking
from cva import comments as commenting
from cva.barcode import Grid
from cva.gazette import publication_date
from cva.store import BlobStore
from cva.text import fold

ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
ASK_LIMIT = 15
ASK_WINDOW = 600
REQUIRED_TABLES = {
    "legislators",
    "legislator_terms",
    "legislator_service",
    "vote_absences",
    "vote_attendance",
    "text_record_legislators",
}


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
        self.grid = Grid(conn, self.party_on)
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
    if path.startswith("/files/"):
        return "public, max-age=31536000, immutable"
    if path.startswith("/pdf/"):
        return "public, max-age=86400"
    if path.startswith("/api/") and path.endswith("/comments"):
        return "no-store"
    if path.startswith("/api/"):
        return "public, max-age=300"
    return "no-cache"


class CacheHeaders:
    """Adds Cache-Control, by path, to successful responses."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] not in ("GET", "HEAD"):
            return await self.app(scope, receive, send)
        policy = cache_policy(scope["path"]).encode()

        async def send_with_policy(message):
            if message["type"] == "http.response.start" and message["status"] in (200, 206):
                message["headers"] = [*message.get("headers", []), (b"cache-control", policy)]
            await send(message)

        await self.app(scope, receive, send_with_policy)


def gzipped(path: Path, name: str) -> Path:
    """A gzip copy of the database for download, kept next to it and made
    again when the database is newer. `name` is what gunzip -N restores."""
    out = path.with_name(path.name + ".gz")
    if not out.exists() or out.stat().st_mtime < path.stat().st_mtime:
        tmp = out.with_name(out.name + ".tmp")
        with open(path, "rb") as src, open(tmp, "wb") as raw:
            with gzip.GzipFile(name, "wb", 9, raw) as dst:
                shutil.copyfileobj(src, dst, 1 << 20)
        tmp.replace(out)
    return out


def summary(v: dict) -> dict:
    return {k: val for k, val in v.items() if not k.startswith("_")}


def query_int(request: Request, key: str, default: int, most: int) -> int:
    try:
        return max(0, min(int(request.query_params.get(key, default)), most))
    except ValueError:
        raise HTTPException(400, f"{key} must be a number") from None


class RateLimit:
    """At most `limit` calls per `window` seconds for each key."""

    def __init__(self, limit: int, window: float):
        self.limit, self.window = limit, window
        self.calls: dict[str, deque] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now, calls = time.monotonic(), self.calls[key]
        while calls and calls[0] < now - self.window:
            calls.popleft()
        if len(calls) >= self.limit:
            return False
        calls.append(now)
        return True


def create_app(
    votes_db: Path,
    pdfs: Path | None = None,
    static: Path | None = None,
    llm: asking.Complete | None = None,
    preview=commenting.fetch_preview,
    uploads: Path | None = None,
):
    """`llm` answers the question box; without it the box is off. `preview`
    fetches a link's preview for comments. Files attached to comments are kept
    in `uploads`, by default an uploads directory beside the database."""
    data = Data(votes_db)
    commenting.setup(votes_db)
    uploads = BlobStore(uploads or votes_db.with_name("uploads"), suffix="")
    barcode_json = json.dumps(
        {**data.grid.payload(), "ask": llm is not None}, ensure_ascii=False, separators=(",", ":")
    ).encode()
    barcode_gz = gzip.compress(barcode_json, 6)
    limits = RateLimit(ASK_LIMIT, ASK_WINDOW)
    download_name = (
        f"colombia-vote-audit-{datetime.fromtimestamp(votes_db.stat().st_mtime, UTC).date()}.db"
    )
    download = gzipped(votes_db, download_name)
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
                "download_bytes": download.stat().st_size,
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

    def comments(request: Request):
        lid = request.path_params["id"]
        if lid not in data.legislators:
            raise HTTPException(404, "no such legislator")
        conn = data.connect()
        try:
            return JSONResponse({"comments": commenting.listing(conn, lid)})
        finally:
            conn.close()

    async def post_comment(request: Request):
        lid = request.path_params["id"]
        if lid not in data.legislators:
            raise HTTPException(404, "no such legislator")
        if int(request.headers.get("content-length") or 0) > commenting.MAX_UPLOAD_BYTES:
            raise HTTPException(413, "attach at most 90 MB per comment")
        files = []
        try:
            if request.headers.get("content-type", "").startswith("multipart/form-data"):
                # Files come as multipart form data, with the fields beside them.
                async with request.form(max_files=commenting.MAX_FILES, max_fields=10) as got:
                    for f in got.getlist("files"):
                        if isinstance(f, str) or not f.filename:
                            continue
                        content = await f.read(commenting.MAX_FILE_BYTES + 1)
                        if len(content) > commenting.MAX_FILE_BYTES:
                            raise HTTPException(413, f"{f.filename} is over 25 MB")
                        files.append((f.filename, f.content_type, content))
                    got = dict(got)
            else:
                got = await request.json()
            org = " ".join(str(got["organization"]).split())
            author = " ".join(str(got.get("author") or "").split()) or None
            body = str(got.get("body") or "").strip()
        except (ValueError, KeyError, TypeError, AttributeError):
            raise HTTPException(400, "send {organization, author, body} and files") from None
        if not org or not (body or files):
            raise HTTPException(400, "a comment needs an organization and some text or a file")
        if sum(len(f[2]) for f in files) > commenting.MAX_UPLOAD_BYTES:
            raise HTTPException(413, "attach at most 90 MB per comment")
        if len(org) > commenting.MAX_NAME or len(author or "") > commenting.MAX_NAME:
            raise HTTPException(400, f"keep names under {commenting.MAX_NAME} characters")
        if len(body) > commenting.MAX_BODY:
            raise HTTPException(400, f"keep comments under {commenting.MAX_BODY} characters")
        shas = await run_in_threadpool(lambda: [uploads.put(f[2]) for f in files])
        conn = sqlite3.connect(votes_db, timeout=15)
        try:
            urls = commenting.links(body)[: commenting.MAX_LINKS]
            if urls:
                await commenting.previews_for(conn, urls, preview)
            cid = conn.execute(
                "INSERT INTO comments (legislator_id, organization, author, body, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (lid, org, author, body, commenting.now()),
            ).lastrowid
            commenting.attach(conn, cid, files, shas)
            conn.commit()
            posted = next(c for c in commenting.listing(conn, lid) if c["id"] == cid)
        finally:
            conn.close()
        return JSONResponse(posted, status_code=201)

    def attachment(request: Request):
        sha = request.path_params["sha"]
        conn = data.connect()
        try:
            row = conn.execute(
                "SELECT name, content_type FROM comment_files WHERE sha256 = ? LIMIT 1", (sha,)
            ).fetchone()
        finally:
            conn.close()
        path = uploads.path_for(sha)
        if not row or not path.is_file():
            raise HTTPException(404, "no such file")
        name, kind = row
        inline = kind in commenting.INLINE_TYPES
        return FileResponse(
            path,
            media_type=kind if inline else "application/octet-stream",
            filename=request.path_params["name"] or name,
            content_disposition_type="inline" if inline else "attachment",
            headers={"x-content-type-options": "nosniff"},
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

    def barcode(request: Request):
        if "gzip" in request.headers.get("accept-encoding", ""):
            return Response(
                barcode_gz,
                media_type="application/json",
                headers={"content-encoding": "gzip", "vary": "accept-encoding"},
            )
        return Response(barcode_json, media_type="application/json")

    async def ask(request: Request):
        if llm is None:
            raise HTTPException(503, "questions are turned off on this server")
        try:
            body = await request.json()
            turns = [
                {"role": t["role"], "content": str(t["content"])}
                for t in body["turns"]
                if t["role"] in ("user", "assistant") and str(t["content"]).strip()
            ]
        except (ValueError, KeyError, TypeError):
            raise HTTPException(400, "send {turns: [{role, content}], view, lang}") from None
        if not turns or turns[-1]["role"] != "user":
            raise HTTPException(400, "the last turn must be the reader's question")
        if len(turns[-1]["content"]) > asking.MAX_CHARS:
            raise HTTPException(400, f"keep questions under {asking.MAX_CHARS} characters")
        client = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        if not limits.allow(client or (request.client.host if request.client else "")):
            raise HTTPException(429, "too many questions; try again in a few minutes")
        view = body.get("view") if isinstance(body.get("view"), dict) else {}
        lang = "es" if body.get("lang") == "es" else "en"
        try:
            answer = await run_in_threadpool(asking.ask, data.grid, llm, turns, view, lang)
        except (httpx.HTTPError, RuntimeError, KeyError, IndexError):
            raise HTTPException(502, "the model didn't answer; try again") from None
        return JSONResponse(answer)

    def download_db(request: Request):
        """The whole database the site reads, committee votes included."""
        return FileResponse(download, media_type="application/gzip", filename=f"{download_name}.gz")

    async def error(request: Request, exc: HTTPException):
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)

    routes = [
        Route("/api/stats", stats),
        Route("/api/votes", votes),
        Route("/api/votes/{id:int}", vote),
        Route("/api/legislators", legislators),
        Route("/api/legislators/{id:int}", legislator),
        Route("/api/legislators/{id:int}/comments", comments),
        Route("/api/legislators/{id:int}/comments", post_comment, methods=["POST"]),
        Route("/pdf/{id:int}", pdf),
        Route("/files/{sha:str}/{name:str}", attachment),
        Route("/download-db", download_db),
        Route("/api/barcode", barcode),
        Route("/api/ask", ask, methods=["POST"]),
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
    ap.add_argument(
        "--uploads",
        type=Path,
        help="where files attached to comments are kept (default: beside votes_db)",
    )
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args(argv)
    llm = asking.from_env()
    if llm is None:
        print("question box off: set CVA_LLM_BASE_URL, CVA_LLM_API_KEY and CVA_LLM_MODEL")
    uvicorn.run(
        create_app(args.votes_db, args.pdfs, args.static, llm, uploads=args.uploads),
        host=args.host,
        port=args.port,
    )


if __name__ == "__main__":
    main()
