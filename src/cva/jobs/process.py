"""Run a gazette parser over downloaded PDFs.

The parser is any callable `parse(pdf_path: Path, gazette: dict) -> dict`
that returns JSON-serializable output, loaded from a "module:function"
spec. Results are stored per parser version, so a new version re-processes
everything without disturbing earlier results.
"""

from __future__ import annotations

import importlib
import json
import logging
import sqlite3
from collections import Counter
from collections.abc import Callable
from pathlib import Path

from cva.db import now
from cva.store import BlobStore

log = logging.getLogger(__name__)

Parser = Callable[[Path, dict], dict]


def load_parser(spec: str) -> Parser:
    module, _, attr = spec.partition(":")
    if not attr:
        raise ValueError(f"parser spec must be 'module:function', got {spec!r}")
    return getattr(importlib.import_module(module), attr)


def select_unprocessed(
    conn: sqlite3.Connection, version: str, limit: int | None, retry_errors: bool
) -> list[sqlite3.Row]:
    sql = """
        SELECT g.* FROM gazettes g
        LEFT JOIN parses p ON p.gazette_id = g.id AND p.parser_version = ?
        WHERE g.fetch_status = 'ok' AND (p.gazette_id IS NULL {retry})
        ORDER BY g.year, g.id
    """.format(retry="OR p.status = 'error'" if retry_errors else "")
    args: list = [version]
    if limit:
        sql += " LIMIT ?"
        args.append(limit)
    return conn.execute(sql, args).fetchall()


def process(
    conn: sqlite3.Connection,
    store: BlobStore,
    parser: Parser,
    version: str,
    limit: int | None = None,
    retry_errors: bool = False,
) -> dict:
    stats = Counter()
    for g in select_unprocessed(conn, version, limit, retry_errors):
        gazette = dict(g)
        try:
            result = parser(store.path_for(g["sha256"]), gazette)
            status, error, payload = "ok", None, json.dumps(result, ensure_ascii=False)
        except Exception as e:
            log.exception("gazette %s/%s failed", g["year"], g["number"])
            status, error, payload = "error", f"{type(e).__name__}: {e}", None
        with conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO parses (gazette_id, parser_version, status, error,
                                               result_json, processed_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (g["id"], version, status, error, payload, now()),
            )
        stats[status] += 1
    log.info("process %s: %s", version, dict(stats))
    return dict(stats)
