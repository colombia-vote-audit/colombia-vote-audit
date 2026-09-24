from datetime import UTC, datetime, timedelta

from cva import db
from cva.jobs import congreso


def detail(bill_id, states):
    return {"id": bill_id, "proyecto_ley_estado": states}


def state(sid, fecha, type_id, texto=None):
    return {
        "id": sid,
        "fecha": fecha,
        "estado_proyecto_ley_id": type_id,
        "tipo_estado": {"nombre": f"type {type_id}"},
        "corporacion_id": 1,
        "gaceta_texto": texto,
        "gaceta_url": None,
    }


def seed_bill(conn, bill_id, fecha):
    conn.execute(
        "INSERT INTO bills (id, fecha_radicacion, first_seen_at, last_seen_at) VALUES (?, ?, ?, ?)",
        (bill_id, fecha, db.now(), db.now()),
    )


def test_store_detail_links_gazettes_and_marks_final():
    conn = db.connect(":memory:")
    seed_bill(conn, 1, "2012-10-04")
    congreso.store_bill_detail(
        conn,
        1,
        detail(
            1, [state(10, "2012-12-19", 37, "948/12, 950/12"), state(11, "2012-12-25", 40, "09/13")]
        ),
    )
    bill = conn.execute("SELECT * FROM bills WHERE id = 1").fetchone()
    assert bill["latest_state_type"] == 40 and bill["is_final"] == 1
    refs = conn.execute("SELECT year, number FROM bill_state_gazettes ORDER BY 1, 2").fetchall()
    assert [tuple(r) for r in refs] == [(2012, "948"), (2012, "950"), (2013, "9")]

    # Re-storing replaces the timeline instead of duplicating it.
    congreso.store_bill_detail(conn, 1, detail(1, [state(10, "2012-12-19", 37, "948/12")]))
    assert conn.execute("SELECT count(*) FROM bill_state_gazettes").fetchone()[0] == 1
    assert conn.execute("SELECT is_final FROM bills").fetchone()[0] == 0


def test_detail_refresh_policy():
    conn = db.connect(":memory:")
    at = datetime(2026, 9, 24, tzinfo=UTC)
    iso = lambda d: d.isoformat(timespec="seconds")  # noqa: E731
    seed_bill(conn, 1, "2025-01-01")  # never fetched
    seed_bill(conn, 2, "2025-01-01")  # active, fetched 2 days ago -> due
    seed_bill(conn, 3, "2025-01-01")  # active but final -> not due
    seed_bill(conn, 4, "2010-01-01")  # old, fetched 10 days ago -> not due
    seed_bill(conn, 5, "2010-01-01")  # old, fetched 40 days ago -> due
    rows = {
        2: (at - timedelta(days=2), 0),
        3: (at - timedelta(days=2), 1),
        4: (at - timedelta(days=10), 0),
        5: (at - timedelta(days=40), 0),
    }
    for bid, (fetched, final) in rows.items():
        conn.execute(
            "UPDATE bills SET detail_fetched_at = ?, is_final = ? WHERE id = ?",
            (iso(fetched), final, bid),
        )
    assert congreso.select_detail_due(conn, None, at=at) == [1, 5, 2]
