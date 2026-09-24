"""Sync the Imprenta gazette index and download gazette PDFs."""

from __future__ import annotations

import logging
import shutil
import sqlite3
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from itertools import batched

import httpx

from cva.db import now
from cva.gazette import normalize_number
from cva.sources.imprenta import ImprentaError, ImprentaSession
from cva.store import BlobStore

log = logging.getLogger(__name__)

PAGE_SIZE = 50
MAX_ATTEMPTS = 3


_UPSERT_GAZETTE = """
    INSERT INTO gazettes (number, number_norm, year, chamber, date, doc_type, downloadable,
                          row_index, first_seen_at, last_seen_at, fetch_status)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT (number, chamber, date) DO UPDATE SET
        doc_type = excluded.doc_type,
        downloadable = excluded.downloadable,
        row_index = excluded.row_index,
        last_seen_at = excluded.last_seen_at,
        fetch_status = CASE
            WHEN gazettes.fetch_status = 'unavailable' AND excluded.downloadable
            THEN 'pending' ELSE gazettes.fetch_status END
"""


def sync_index(conn: sqlite3.Connection, session: ImprentaSession) -> dict:
    ts = now()
    before = conn.execute("SELECT count(*) FROM gazettes").fetchone()[0]
    seen = 0
    # Write in batches so the crawl never holds the write lock for long.
    for batch in batched(session.iter_index(), 1000):
        seen += len(batch)
        with conn:
            conn.executemany(
                _UPSERT_GAZETTE,
                [
                    (
                        row.number,
                        normalize_number(row.number),
                        row.year,
                        row.chamber,
                        row.date,
                        row.doc_type,
                        int(row.downloadable),
                        row.row_index,
                        ts,
                        ts,
                        "pending" if row.downloadable else "unavailable",
                    )
                    for row in batch
                ],
            )
    new = conn.execute("SELECT count(*) FROM gazettes").fetchone()[0] - before
    # Rows that dropped out of the listing keep their data but lose their
    # position so fetch doesn't chase a stale index.
    with conn:
        gone = conn.execute(
            "UPDATE gazettes SET row_index = NULL WHERE last_seen_at < ?", (ts,)
        ).rowcount
    log.info("gazette index: %d rows, %d new, %d no longer listed", seen, new, gone)
    return {"seen": seen, "new": new, "gone": gone}


@dataclass
class FetchSelection:
    limit: int | None = None
    years: tuple[int, int] | None = None
    doc_type_like: str | None = None
    ids: list[int] = field(default_factory=list)
    retry_errors: bool = True


def select_pending(conn: sqlite3.Connection, sel: FetchSelection) -> list[sqlite3.Row]:
    statuses = ["pending"] + (["error"] if sel.retry_errors else [])
    where = [
        f"fetch_status IN ({','.join('?' * len(statuses))})",
        "row_index IS NOT NULL",
        "fetch_attempts < ?",
    ]
    args: list = [*statuses, MAX_ATTEMPTS]
    if sel.years:
        where.append("year BETWEEN ? AND ?")
        args += list(sel.years)
    if sel.doc_type_like:
        where.append("doc_type LIKE ?")
        args.append(sel.doc_type_like)
    if sel.ids:
        where.append(f"id IN ({','.join('?' * len(sel.ids))})")
        args += sel.ids
    # Session records (actas) carry the roll calls, so they go first.
    sql = f"""
        SELECT * FROM gazettes WHERE {" AND ".join(where)}
        ORDER BY doc_type LIKE 'Acta%' DESC, row_index
    """
    if sel.limit:
        sql += " LIMIT ?"
        args.append(sel.limit)
    return conn.execute(sql, args).fetchall()


@dataclass
class Result:
    gazette_id: int
    status: str  # ok | missing | error | drifted
    error: str | None = None
    sha256: str | None = None
    size: int | None = None


def _record(conn: sqlite3.Connection, r: Result):
    with conn:
        conn.execute(
            """
            UPDATE gazettes SET fetch_status = ?, last_error = ?, sha256 = ?, size = ?,
                   fetch_attempts = fetch_attempts + 1, fetched_at = ?
            WHERE id = ?
            """,
            (r.status, r.error, r.sha256, r.size, now(), r.gazette_id),
        )


def validate_pdf(data: bytes, expected_length: int | None) -> str | None:
    """Why `data` isn't a complete PDF, or None if it looks whole."""
    if expected_length is not None and expected_length != len(data):
        return f"truncated: got {len(data)} of {expected_length} bytes"
    if not data.startswith(b"%PDF"):
        return f"not a PDF (starts {data[:16]!r})"
    if b"%%EOF" not in data[-2048:]:
        return "truncated: no %%EOF trailer"
    return None


@dataclass
class FetchOptions:
    workers: int = 3
    batch_size: int = 500
    # Re-read the index when it's older than this; new gazettes shift row
    # positions while a long backfill runs.
    reindex_after: float = 20 * 60
    deadline: float | None = None  # time.monotonic() value to stop at
    min_free_bytes: int = 20 * 10**9
    stop: threading.Event = field(default_factory=threading.Event)


class Fetcher:
    """Downloads gazettes with a pool of workers, one archive session each.

    Workers only download and write blobs; all database writes happen on the
    calling thread.
    """

    def __init__(self, conn, make_session, store: BlobStore, opts: FetchOptions):
        self.conn = conn
        self.make_session = make_session
        self.store = store
        self.opts = opts
        self.local = threading.local()
        self.stats: dict[str, int] = defaultdict(int)
        self.indexed_at: float | None = None
        self.halted: str | None = None

    # -- worker side -------------------------------------------------------

    def _session(self) -> ImprentaSession:
        if not hasattr(self.local, "session"):
            self.local.session = self.make_session()
        return self.local.session

    def _page(self, first: int, targets: list[dict]) -> list[Result]:
        session = self._session()
        results: list[Result] = []
        for _ in range(2):
            try:
                self._download_page(session, first, targets, results)
                return results
            except (ImprentaError, httpx.HTTPError) as e:
                log.warning("page %d: %s; reopening session", first, e)
                error = f"{type(e).__name__}: {e}"[:500]
                try:
                    session.open()
                except (ImprentaError, httpx.HTTPError):
                    pass
        done = {r.gazette_id for r in results}
        return results + [Result(t["id"], "error", error) for t in targets if t["id"] not in done]

    def _download_page(self, session, first, targets, results: list[Result]):
        """Append a Result per target to `results`, skipping ones already
        there from an earlier attempt."""
        done = {r.gazette_id for r in results}
        rows = {r.key: r for r in session.load_page(first, PAGE_SIZE)}
        for t in targets:
            if t["id"] in done:
                continue
            if self._stop_reason():
                return
            row = rows.get((t["number"], t["chamber"], t["date"]))
            if row is None:
                results.append(Result(t["id"], "drifted"))
                continue
            try:
                data, length = session.download(row)
            except (ImprentaError, httpx.HTTPError) as e:
                # One bad file shouldn't sink the rest of the page: note it,
                # get a fresh session on the same page, and carry on.
                log.warning("gazette %s: %s", t["id"], e)
                results.append(Result(t["id"], "error", f"{type(e).__name__}: {e}"[:500]))
                session.open()
                rows = {r.key: r for r in session.load_page(first, PAGE_SIZE)}
                continue
            if data is None:
                results.append(Result(t["id"], "missing", "archive returned an empty file"))
                continue
            problem = validate_pdf(data, length)
            if problem:
                results.append(Result(t["id"], "error", problem))
                continue
            sha = self.store.put(data)
            results.append(Result(t["id"], "ok", sha256=sha, size=len(data)))

    # -- coordinator side --------------------------------------------------

    def _stop_reason(self) -> str | None:
        """Why no new download should start, if any. Checked by workers
        before every download and by the coordinator before every batch."""
        if self.opts.stop.is_set():
            reason = "stop requested"
        elif self.opts.deadline and time.monotonic() >= self.opts.deadline:
            reason = "time budget used"
        elif (free := shutil.disk_usage(self.store.root).free) < self.opts.min_free_bytes:
            reason = f"only {free / 1e9:.1f} GB free"
        else:
            return None
        self.halted = reason
        return reason

    def _reindex(self):
        sync_index(self.conn, self.make_session())
        self.indexed_at = time.monotonic()

    def _current(self, ids: list[int]) -> list[dict]:
        """Targets still waiting, with their latest row positions."""
        rows = []
        for chunk in batched(ids, 500):
            rows += self.conn.execute(
                f"""
                SELECT id, number, chamber, date, row_index FROM gazettes
                WHERE id IN ({",".join("?" * len(chunk))})
                  AND fetch_status IN ('pending', 'error') AND row_index IS NOT NULL
                """,
                chunk,
            ).fetchall()
        return [dict(r) for r in rows]

    def _run_batch(self, ids: list[int]) -> list[int]:
        """Fetch one batch; return ids that had moved off their page."""
        by_page: dict[int, list[dict]] = defaultdict(list)
        for t in self._current(ids):
            by_page[t["row_index"] // PAGE_SIZE * PAGE_SIZE].append(t)
        drifted = []
        with ThreadPoolExecutor(self.opts.workers, thread_name_prefix="fetch") as pool:
            futures = [pool.submit(self._page, first, ts) for first, ts in by_page.items()]
            for fut in as_completed(futures):
                for r in fut.result():
                    if r.status == "drifted":
                        drifted.append(r.gazette_id)
                    else:
                        _record(self.conn, r)
                        self.stats[r.status] += 1
                        self.stats["bytes"] += r.size or 0
        return drifted

    def _ensure_index(self):
        stale = self.indexed_at is None or (
            time.monotonic() - self.indexed_at > self.opts.reindex_after
        )
        if stale:
            self._reindex()

    def run(self, sel: FetchSelection) -> dict:
        # Targets need row positions, so index before selecting.
        self._ensure_index()
        ids = [r["id"] for r in select_pending(self.conn, sel)]
        log.info("fetch: %d gazettes to download with %d workers", len(ids), self.opts.workers)
        started = time.monotonic()
        moved: list[int] = []
        for batch in batched(ids, self.opts.batch_size):
            if self._stop_reason():
                break
            self._ensure_index()
            drifted = self._run_batch(list(batch))
            if drifted:
                moved += drifted
                self.indexed_at = None
            self._log_progress(len(ids), started)
        else:
            # Rows that moved get one more try against a fresh index.
            if moved:
                self.indexed_at = None
                self._ensure_index()
                for gid in self._run_batch(moved):
                    _record(self.conn, Result(gid, "error", "not found on its index page"))
                    self.stats["error"] += 1
        if self.halted:
            log.warning("fetch stopped early: %s", self.halted)
            self.stats["stopped_early"] = 1
        log.info("fetch: %s", dict(self.stats))
        return dict(self.stats)

    def _log_progress(self, total: int, started: float):
        done = sum(self.stats[k] for k in ("ok", "missing", "error"))
        elapsed = max(time.monotonic() - started, 1)
        log.info(
            "fetch progress: %d/%d, %.1f GB, %.1f MB/s",
            done,
            total,
            self.stats["bytes"] / 1e9,
            self.stats["bytes"] / 1e6 / elapsed,
        )


def fetch(
    conn,
    make_session,
    store: BlobStore,
    sel: FetchSelection,
    opts: FetchOptions | None = None,
    indexed: bool = False,
) -> dict:
    """Download the selected gazettes. Pass indexed=True if the caller has
    just run sync_index, to skip a second index crawl."""
    fetcher = Fetcher(conn, make_session, store, opts or FetchOptions())
    if indexed:
        fetcher.indexed_at = time.monotonic()
    return fetcher.run(sel)
