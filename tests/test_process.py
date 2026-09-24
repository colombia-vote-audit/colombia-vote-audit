import json

from cva import db
from cva.jobs import process
from cva.store import BlobStore


def seed(conn, store):
    sha = store.put(b"%PDF-1.4 fake")
    for i, status in enumerate(["ok", "ok", "pending"], start=1):
        conn.execute(
            """
            INSERT INTO gazettes (id, number, number_norm, year, chamber, date, downloadable,
                                  first_seen_at, last_seen_at, fetch_status, sha256)
            VALUES (?, ?, ?, 2012, 'Senado', '2012-01-01', 1, '', '', ?, ?)
            """,
            (i, str(i), str(i), status, sha if status == "ok" else None),
        )


def test_process_runs_parser_per_version(tmp_path):
    conn = db.connect(":memory:")
    store = BlobStore(tmp_path)
    seed(conn, store)
    calls = []

    def parser(path, gazette):
        calls.append(gazette["id"])
        if gazette["id"] == 2:
            raise ValueError("bad table")
        return {"bytes": len(path.read_bytes())}

    assert process.process(conn, store, parser, "v1") == {"ok": 1, "error": 1}
    assert calls == [1, 2]
    row = conn.execute("SELECT * FROM parses WHERE gazette_id = 1").fetchone()
    assert json.loads(row["result_json"]) == {"bytes": 13}

    # Same version: nothing left, unless retrying errors.
    assert process.process(conn, store, parser, "v1") == {}
    assert process.process(conn, store, parser, "v1", retry_errors=True) == {"error": 1}
    # New version re-processes everything that's downloaded.
    assert process.process(conn, store, parser, "v2") == {"ok": 1, "error": 1}


def test_load_parser():
    assert process.load_parser("json:dumps") is json.dumps
