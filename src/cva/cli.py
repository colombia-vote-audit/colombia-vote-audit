"""Command line entry point: `cva <command>`."""

from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import signal
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from cva import db
from cva.http import PoliteClient
from cva.jobs import congreso, gazettes, process
from cva.sources.congresovisible import CongresoVisible
from cva.sources.imprenta import ImprentaSession
from cva.store import BlobStore

log = logging.getLogger("cva")


class Context:
    def __init__(self, data_dir: Path, interval: float):
        self.data_dir = data_dir
        self.conn = db.connect(data_dir / "cva.db")
        self.store = BlobStore(data_dir / "pdfs")
        self.interval = interval
        self.stop = threading.Event()

    def lock(self):
        """Hold an exclusive lock on the data dir for this process's lifetime,
        so a manual run and the timer never crawl at the same time."""
        self._lock_file = open(self.data_dir / "cva.lock", "w")
        try:
            fcntl.flock(self._lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            sys.exit(f"another cva run is using {self.data_dir}")

    def stop_on_signals(self):
        """First SIGINT/SIGTERM finishes in-flight downloads and stops;
        a second SIGINT aborts immediately."""

        def handler(signum, frame):
            if self.stop.is_set() and signum == signal.SIGINT:
                raise KeyboardInterrupt
            log.warning(
                "%s received, stopping after in-flight downloads", signal.Signals(signum).name
            )
            self.stop.set()

        signal.signal(signal.SIGINT, handler)
        signal.signal(signal.SIGTERM, handler)

    def congreso(self) -> CongresoVisible:
        return CongresoVisible(PoliteClient(min_interval=self.interval))

    def imprenta(self) -> ImprentaSession:
        return ImprentaSession(PoliteClient(min_interval=self.interval))

    @contextmanager
    def run(self, job: str):
        with self.conn:
            run_id = self.conn.execute(
                "INSERT INTO runs (job, started_at) VALUES (?, ?)", (job, db.now())
            ).lastrowid
        stats: dict = {}
        status = "failed"
        try:
            yield stats
            status = "stopped" if self.stop.is_set() else "ok"
        finally:
            with self.conn:
                self.conn.execute(
                    "UPDATE runs SET finished_at = ?, status = ?, stats_json = ? WHERE id = ?",
                    (db.now(), status, json.dumps(stats), run_id),
                )


def _years(value: str) -> tuple[int, int]:
    lo, _, hi = value.partition("-")
    return int(lo), int(hi or lo)


def cmd_sync_gazettes(ctx: Context, args):
    with ctx.run("sync-gazettes") as stats:
        stats.update(gazettes.sync_index(ctx.conn, ctx.imprenta()))


def fetch_options(ctx: Context, args) -> gazettes.FetchOptions:
    return gazettes.FetchOptions(
        workers=args.workers,
        deadline=time.monotonic() + args.max_minutes * 60 if args.max_minutes else None,
        min_free_bytes=int(args.min_free_gb * 1e9),
        stop=ctx.stop,
    )


def cmd_fetch_gazettes(ctx: Context, args):
    sel = gazettes.FetchSelection(limit=args.limit, years=args.years, doc_type_like=args.doc_type)
    with ctx.run("fetch-gazettes") as stats:
        stats.update(
            gazettes.fetch(ctx.conn, ctx.imprenta, ctx.store, sel, fetch_options(ctx, args))
        )


def cmd_sync_bills(ctx: Context, args):
    with ctx.run("sync-bills") as stats:
        cv = ctx.congreso()
        stats["index"] = congreso.sync_bill_index(ctx.conn, cv)
        if args.details != 0:
            stats["details"] = congreso.sync_bill_details(ctx.conn, cv, args.details)


def cmd_sync_votes(ctx: Context, args):
    with ctx.run("sync-votes") as stats:
        stats.update(congreso.sync_votes(ctx.conn, ctx.congreso()))


def cmd_daily(ctx: Context, args):
    """Everything the scheduled job does, in order. One failing step
    doesn't stop the others."""
    steps = [
        ("sync-bills", lambda: cmd_sync_bills(ctx, argparse.Namespace(details=args.bill_details))),
        ("sync-votes", lambda: cmd_sync_votes(ctx, args)),
        (
            "fetch-gazettes",
            lambda: cmd_fetch_gazettes(
                ctx, argparse.Namespace(**vars(args), limit=None, years=None, doc_type=None)
            ),
        ),
    ]
    failed = []
    for name, step in steps:
        if ctx.stop.is_set():
            break
        try:
            step()
        except Exception:
            log.exception("%s failed", name)
            failed.append(name)
    if failed:
        sys.exit(f"failed steps: {', '.join(failed)}")


def cmd_process(ctx: Context, args):
    parser = process.load_parser(args.parser)
    with ctx.run("process") as stats:
        stats.update(
            process.process(
                ctx.conn, ctx.store, parser, args.version, args.limit, args.retry_errors
            )
        )


def cmd_status(ctx: Context, args):
    q = ctx.conn.execute
    print("gazettes by fetch status:")
    for r in q("SELECT fetch_status, count(*), sum(size) FROM gazettes GROUP BY 1 ORDER BY 2 DESC"):
        print(f"  {r[0]:12} {r[1]:7}  {(r[2] or 0) / 1e9:6.1f} GB")
    bills = q("SELECT count(*), count(detail_fetched_at), sum(is_final) FROM bills").fetchone()
    print(f"bills: {bills[0]} indexed, {bills[1]} with detail, {bills[2]} final")
    votes = q("SELECT count(*), sum(has_roll_call) FROM votes").fetchone()
    print(f"votes: {votes[0]} ({votes[1]} with roll-call text)")
    print("recent runs:")
    for r in q("SELECT job, started_at, status, stats_json FROM runs ORDER BY id DESC LIMIT 8"):
        print(f"  {r[1]}  {r[0]:15} {r[2]:7} {r[3]}")


def cmd_sample_build(ctx: Context, args):
    from cva import sample

    if not ctx.conn.execute("SELECT 1 FROM gazettes LIMIT 1").fetchone():
        sys.exit("gazette index is empty; run `cva sync-gazettes` first")
    items = sample.select(ctx.conn, seed=args.seed)
    _sample_download_and_write(ctx, items, args)


def cmd_sample_fetch(ctx: Context, args):
    """Re-create a sample from a committed manifest on another machine."""
    from cva import sample

    wanted = json.loads(Path(args.manifest).read_text())
    gazettes.sync_index(ctx.conn, ctx.imprenta())
    items = []
    for w in wanted:
        row = ctx.conn.execute(
            "SELECT id FROM gazettes WHERE number = ? AND chamber = ? AND date = ?",
            (w["number"], w["chamber"], w["date"]),
        ).fetchone()
        if row is None:
            log.warning("gazette %s %s %s no longer listed", w["number"], w["chamber"], w["date"])
            continue
        w = {k: v for k, v in w.items() if k in sample.SampleItem.__dataclass_fields__}
        items.append(sample.SampleItem(**{**w, "gazette_id": row["id"]}))
    _sample_download_and_write(ctx, items, args, indexed=True)


def _sample_download_and_write(ctx: Context, items, args, indexed: bool = False):
    from cva import sample

    out = args.out
    sel = gazettes.FetchSelection(ids=[i.gazette_id for i in items])
    with ctx.run("sample") as stats:
        stats.update(
            gazettes.fetch(
                ctx.conn, ctx.imprenta, ctx.store, sel, fetch_options(ctx, args), indexed=indexed
            )
        )
    sample.materialize(ctx.conn, ctx.store, items, out)
    sample.write_manifest(items, out)
    summary = sample.summarize(items)
    (out / "SUMMARY.md").write_text(summary)
    print(summary)


def add_fetch_args(p: argparse.ArgumentParser):
    p.add_argument("--workers", type=int, default=3, help="parallel archive sessions (default 3)")
    p.add_argument("--max-minutes", type=float, help="stop starting new downloads after this")
    p.add_argument("--min-free-gb", type=float, default=20, help="stop if the disk gets this full")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cva", description=__doc__)
    p.add_argument(
        "--data-dir",
        type=Path,
        default=Path(os.environ.get("CVA_DATA_DIR", "data")),
        help="where the database and PDFs live (env CVA_DATA_DIR, default ./data)",
    )
    p.add_argument(
        "--interval",
        type=float,
        default=1.0,
        help="minimum seconds between requests to a source (default 1)",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("sync-gazettes", help="refresh the Imprenta gazette index").set_defaults(
        func=cmd_sync_gazettes
    )

    s = sub.add_parser("fetch-gazettes", help="download pending gazette PDFs")
    s.add_argument("--limit", type=int)
    s.add_argument("--years", type=_years, help="e.g. 2010-2015 or 2012")
    s.add_argument("--doc-type", help="SQL LIKE pattern on document type, e.g. 'Acta%%'")
    add_fetch_args(s)
    s.set_defaults(func=cmd_fetch_gazettes)

    s = sub.add_parser("sync-bills", help="refresh the bill index and due bill details")
    s.add_argument(
        "--details", type=int, default=None, help="max bill details to fetch (0 = index only)"
    )
    s.set_defaults(func=cmd_sync_bills)

    sub.add_parser("sync-votes", help="refresh Congreso Visible votes").set_defaults(
        func=cmd_sync_votes
    )

    s = sub.add_parser("daily", help="scheduled run: bills, votes, gazettes")
    s.add_argument("--bill-details", type=int, default=2000)
    add_fetch_args(s)
    s.set_defaults(func=cmd_daily, max_minutes=600)

    s = sub.add_parser("process", help="run a parser over downloaded gazettes")
    s.add_argument("--parser", required=True, help="module:function")
    s.add_argument("--version", required=True, help="parser version label")
    s.add_argument("--limit", type=int)
    s.add_argument("--retry-errors", action="store_true")
    s.set_defaults(func=cmd_process)

    sub.add_parser("status", help="show pipeline state").set_defaults(func=cmd_status)

    s = sub.add_parser("sample-build", help="select and download a stratified sample")
    s.add_argument("--out", type=Path, default=Path("data/sample"))
    s.add_argument("--seed", type=int, default=7)
    add_fetch_args(s)
    s.set_defaults(func=cmd_sample_build)

    s = sub.add_parser("sample-fetch", help="download the gazettes listed in a manifest")
    s.add_argument("manifest", type=Path)
    s.add_argument("--out", type=Path, default=Path("data/sample"))
    add_fetch_args(s)
    s.set_defaults(func=cmd_sample_fetch)
    return p


def main(argv: list[str] | None = None):
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.INFO if args.verbose else logging.WARNING)
    ctx = Context(args.data_dir, args.interval)
    if args.func is not cmd_status:
        ctx.lock()
        ctx.stop_on_signals()
    args.func(ctx, args)


if __name__ == "__main__":
    main()
