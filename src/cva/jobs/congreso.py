"""Sync bills, bill status timelines, votes and legislators from Congreso Visible."""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from collections import Counter
from datetime import UTC, datetime, timedelta
from itertools import batched
from urllib.parse import quote

from cva.db import now
from cva.gazette import parse_refs
from cva.sources.congresovisible import UPLOADS, CongresoVisible

log = logging.getLogger(__name__)

# State types after which a bill's timeline stops changing. Ids come from
# /api/utils/getComboEstadoProyecto.
FINAL_STATE_TYPES = {
    32,  # Archivado en Debate
    33,  # Archivado por Vencimiento de Términos
    34,  # Retirado por el Autor
    38,  # Declarado Inexequible Parcial
    39,  # Declarado Inexequible Total
    40,  # Sancionado como Ley
    41,  # Acto Legislativo
    68,  # Acumulado
    70,  # Archivado por Tránsito de Legislatura
    81,  # Declarado Exequible Total
    82,  # Declarado Exequible Parcial
}

# Bills filed from the start of the previous congressional term on can
# still move; older ones are only re-checked occasionally.
ACTIVE_SINCE = "2022-07-20"
ACTIVE_REFRESH = timedelta(days=1)
STALE_REFRESH = timedelta(days=30)


def _sha(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True).encode()).hexdigest()


def sync_bill_index(conn: sqlite3.Connection, cv: CongresoVisible) -> dict:
    ts = now()
    bills = cv.bill_index()
    with conn:
        before = conn.execute("SELECT count(*) FROM bills").fetchone()[0]
        conn.executemany(
            """
            INSERT INTO bills (id, titulo, alias, fecha_radicacion, numero_camara,
                               numero_senado, activo, first_seen_at, last_seen_at)
            VALUES (:id, :titulo, :alias, :fecha_radicacion, :numero_camara,
                    :numero_senado, :activo, :ts, :ts)
            ON CONFLICT (id) DO UPDATE SET
                titulo = excluded.titulo, alias = excluded.alias,
                fecha_radicacion = excluded.fecha_radicacion,
                numero_camara = excluded.numero_camara,
                numero_senado = excluded.numero_senado,
                activo = excluded.activo, last_seen_at = excluded.last_seen_at
            """,
            [{**b, "ts": ts} for b in bills],
        )
        after = conn.execute("SELECT count(*) FROM bills").fetchone()[0]
    log.info("bill index: %d bills, %d new", len(bills), after - before)
    return {"seen": len(bills), "new": after - before}


def select_detail_due(conn: sqlite3.Connection, limit: int | None, at: datetime | None = None):
    at = at or datetime.now(UTC)
    active_cutoff = (at - ACTIVE_REFRESH).isoformat(timespec="seconds")
    stale_cutoff = (at - STALE_REFRESH).isoformat(timespec="seconds")
    sql = """
        SELECT id FROM bills
        WHERE detail_fetched_at IS NULL
           OR (NOT is_final AND fecha_radicacion >= ? AND detail_fetched_at < ?)
           OR detail_fetched_at < ?
        ORDER BY detail_fetched_at IS NOT NULL, detail_fetched_at, id DESC
    """
    args: list = [ACTIVE_SINCE, active_cutoff, stale_cutoff]
    if limit:
        sql += " LIMIT ?"
        args.append(limit)
    return [r[0] for r in conn.execute(sql, args)]


def store_bill_detail(conn: sqlite3.Connection, bill_id: int, detail: dict):
    states = detail.get("proyecto_ley_estado") or []
    latest = max(states, key=lambda s: (s.get("fecha") or "", s["id"]), default=None)
    latest_type = latest.get("estado_proyecto_ley_id") if latest else None
    with conn:
        conn.execute(
            """
            UPDATE bills SET detail_json = ?, detail_sha = ?, detail_fetched_at = ?,
                   latest_state_type = ?, is_final = ?
            WHERE id = ?
            """,
            (
                json.dumps(detail, ensure_ascii=False),
                _sha(detail),
                now(),
                latest_type,
                int(latest_type in FINAL_STATE_TYPES),
                bill_id,
            ),
        )
        conn.execute("DELETE FROM bill_states WHERE bill_id = ?", (bill_id,))
        for s in states:
            conn.execute(
                """
                INSERT INTO bill_states (id, bill_id, fecha, state_type_id, state_type,
                                         corporacion_id, gaceta_texto, gaceta_url, raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    s["id"],
                    bill_id,
                    s.get("fecha"),
                    s.get("estado_proyecto_ley_id"),
                    (s.get("tipo_estado") or {}).get("nombre"),
                    s.get("corporacion_id"),
                    s.get("gaceta_texto"),
                    s.get("gaceta_url"),
                    json.dumps(s, ensure_ascii=False),
                ),
            )
            conn.executemany(
                "INSERT INTO bill_state_gazettes (state_id, year, number) VALUES (?, ?, ?)",
                [(s["id"], y, n) for y, n in parse_refs(s.get("gaceta_texto"))],
            )


def sync_bill_details(conn: sqlite3.Connection, cv: CongresoVisible, limit: int | None) -> dict:
    stats = Counter()
    for bill_id in select_detail_due(conn, limit):
        try:
            detail = cv.bill_detail(bill_id)
        except Exception as e:  # keep going; the bill stays due for next run
            log.warning("bill %d: %s", bill_id, e)
            stats["error"] += 1
            continue
        if detail is None:
            with conn:
                conn.execute(
                    "UPDATE bills SET detail_fetched_at = ? WHERE id = ?", (now(), bill_id)
                )
            stats["empty"] += 1
            continue
        store_bill_detail(conn, bill_id, detail)
        stats["ok"] += 1
    log.info("bill details: %s", dict(stats))
    return dict(stats)


def sync_votes(conn: sqlite3.Connection, cv: CongresoVisible) -> dict:
    ts = now()
    stats = Counter()
    for batch in batched(cv.iter_votes(), 500):
        with conn:
            for v in batch:
                _upsert_vote(conn, v, ts, stats)
    log.info("votes: %s", dict(stats))
    return dict(stats)


def _upsert_vote(conn: sqlite3.Connection, v: dict, ts: str, stats: Counter):
    stats["seen"] += 1
    sha = _sha(v)
    prev = conn.execute("SELECT raw_sha FROM votes WHERE id = ?", (v["id"],)).fetchone()
    if prev is None:
        stats["new"] += 1
    elif prev[0] != sha:
        stats["changed"] += 1
    conn.execute(
        """
        INSERT INTO votes (id, bill_id, fecha, es_plenaria, es_comision, acta,
                           url_gaceta, has_roll_call, activo, raw_json, raw_sha,
                           first_seen_at, last_seen_at, changed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (id) DO UPDATE SET
            bill_id = excluded.bill_id, fecha = excluded.fecha,
            es_plenaria = excluded.es_plenaria, es_comision = excluded.es_comision,
            acta = excluded.acta, url_gaceta = excluded.url_gaceta,
            has_roll_call = excluded.has_roll_call, activo = excluded.activo,
            raw_json = excluded.raw_json, last_seen_at = excluded.last_seen_at,
            changed_at = CASE WHEN votes.raw_sha = excluded.raw_sha
                              THEN votes.changed_at ELSE excluded.changed_at END,
            raw_sha = excluded.raw_sha
        """,
        (
            v["id"],
            v.get("proyecto_de_ley_id"),
            v.get("fecha"),
            v.get("esPlenaria"),
            v.get("esComision"),
            v.get("acta"),
            v.get("urlGaceta"),
            int(bool(v.get("datos_importacion"))),
            v.get("activo"),
            json.dumps(v, ensure_ascii=False),
            sha,
            ts,
            ts,
            ts,
        ),
    )


def _term_rows(chamber: str, term: dict, members: list[dict]) -> list[dict]:
    start = f"{term['fechaInicio']}-07-20"
    end = f"{term['fechaFin']}-07-19"
    return [
        {
            "persona_id": m["persona_id"],
            "name": " ".join(filter(None, [m.get("nombres"), m.get("apellidos")])).strip(),
            "photo_url": UPLOADS + quote(m["persona_imagen"]) if m.get("persona_imagen") else None,
            "chamber": chamber,
            "start_date": start,
            "end_date": end,
            "party": m.get("partido"),
            "raw_json": json.dumps(m, ensure_ascii=False),
        }
        for m in members
    ]


def sync_legislators(conn: sqlite3.Connection, cv: CongresoVisible) -> dict:
    ts = now()
    rows = []
    for chamber in cv.chambers():
        for term in cv.terms():
            members = list(cv.iter_legislators(chamber["id"], term["id"]))
            rows += _term_rows(chamber["nombre"], term, members)
    stats = store_legislators(conn, rows, ts)
    log.info("legislators: %s", stats)
    return stats


def store_legislators(conn: sqlite3.Connection, rows: list[dict], ts: str) -> dict:
    with conn:
        before = conn.execute("SELECT count(*) FROM legislators").fetchone()[0]
        # Name and photo are refreshed from the listing on every sync.
        conn.executemany(
            """
            INSERT INTO legislators (id, name, photo_url)
            VALUES (:persona_id, :name, :photo_url)
            ON CONFLICT (id) DO UPDATE SET
                name = excluded.name,
                photo_url = coalesce(excluded.photo_url, legislators.photo_url)
            """,
            rows,
        )
        conn.executemany(
            """
            INSERT INTO legislator_terms (legislator_id, chamber, start_date, end_date,
                                          party, raw_json, last_seen_at)
            VALUES (:persona_id, :chamber, :start_date, :end_date, :party, :raw_json, :ts)
            ON CONFLICT (legislator_id, chamber, start_date) DO UPDATE SET
                end_date = excluded.end_date, party = excluded.party,
                raw_json = excluded.raw_json, last_seen_at = excluded.last_seen_at
            """,
            [{**r, "ts": ts} for r in rows],
        )
        conn.execute(
            """
            UPDATE legislators SET
                start_date = (SELECT min(start_date) FROM legislator_terms t
                              WHERE t.legislator_id = legislators.id),
                end_date = (SELECT max(end_date) FROM legislator_terms t
                            WHERE t.legislator_id = legislators.id),
                party = (SELECT party FROM legislator_terms t
                         WHERE t.legislator_id = legislators.id
                         ORDER BY start_date DESC, chamber LIMIT 1)
            """
        )
        after = conn.execute("SELECT count(*) FROM legislators").fetchone()[0]
    return {"terms": len(rows), "legislators": after, "new": after - before}
