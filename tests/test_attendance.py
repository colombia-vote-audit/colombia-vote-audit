import json
import sqlite3

from cva import attendance, db

CAMARA = "Cámara de Representantes"


def pipeline_db():
    cva = db.connect(":memory:")
    for lid, name in [(1, "Ana Pérez"), (2, "Beto Ruiz"), (3, "Carla Díaz")]:
        cva.execute("INSERT INTO legislators (id, name) VALUES (?, ?)", (lid, name))
        cva.execute(
            "INSERT INTO legislator_terms VALUES (?, ?, '2022-07-20', '2026-07-19', NULL, ?, '')",
            (lid, CAMARA, json.dumps({})),
        )
    return cva


def votes_db(tmp_path, votes, records):
    conn = sqlite3.connect(tmp_path / "votes.db")
    conn.executescript(
        """
        CREATE TABLE documents (id INTEGER PRIMARY KEY, chamber TEXT);
        CREATE TABLE votes (id INTEGER PRIMARY KEY, document_id INTEGER,
                            session_date TEXT, verified INTEGER,
                            source TEXT NOT NULL DEFAULT 'record');
        CREATE TABLE vote_records (vote_id INTEGER, legislator TEXT, vote TEXT,
                                   legislator_id INTEGER);
        INSERT INTO documents VALUES (1, 'Cámara'), (2, 'Cámara'), (3, 'Cámara');
        """
    )
    conn.executemany(
        "INSERT INTO votes VALUES (?, ?, ?, ?, ?)", [(*v, "record")[:5] for v in votes]
    )
    conn.executemany(
        "INSERT INTO vote_records VALUES (?, '', 'yes', ?)",
        [(vid, lid) for vid, lids in records.items() for lid in lids],
    )
    return conn


def test_absences_use_first_to_last_vote_windows(tmp_path):
    # Ana votes throughout; Beto's last vote is the first session (he left);
    # Carla's first vote is the second session (she replaced him).
    votes = votes_db(
        tmp_path,
        [(1, 1, "2024-10-01", 1), (2, 2, "2025-03-01", 1), (3, 3, "2025-09-01", 1)],
        {1: [1, 2], 2: [3], 3: [1, 3]},
    )
    stats = attendance.build(votes, pipeline_db())
    assert stats["absences"] == 1
    assert votes.execute(
        "SELECT vote_id, legislator_id, legislator FROM vote_absences"
    ).fetchall() == [(2, 1, "Ana Pérez")]
    service = votes.execute(
        "SELECT legislator_id, first_vote, last_vote, votes FROM legislator_service ORDER BY 1"
    ).fetchall()
    assert service == [
        (1, "2024-10-01", "2025-09-01", 2),
        (2, "2024-10-01", "2024-10-01", 1),
        (3, "2025-03-01", "2025-09-01", 2),
    ]
    assert votes.execute("SELECT * FROM vote_attendance WHERE vote_id = 2").fetchone() == (
        2,
        "2025-03-01",
        "vote",
        2,
        1,
        1,
    )


def test_undated_votes_take_their_gazettes_date_and_unverified_get_none(tmp_path):
    votes = votes_db(
        tmp_path,
        [
            (1, 1, "2024-10-01", 1),
            (2, 1, "", 1),  # same gazette as vote 1: takes its date
            (3, 2, None, 1),  # gazette with no dated vote: skipped
            (4, 1, "2024-10-01", 0),  # not verified: no absences
            (5, 3, "2025-01-01", 1),
        ],
        {1: [1, 2], 2: [1], 3: [1], 4: [1], 5: [1, 2]},
    )
    attendance.build(votes, pipeline_db())
    rows = votes.execute(
        "SELECT vote_id, session_date, date_source, absent FROM vote_attendance ORDER BY 1"
    ).fetchall()
    assert rows == [
        (1, "2024-10-01", "vote", 0),
        (2, "2024-10-01", "gazette", 1),
        (3, None, "none", 0),
        (5, "2025-01-01", "vote", 0),
    ]
    assert votes.execute("SELECT vote_id, legislator_id FROM vote_absences").fetchall() == [(2, 2)]


def test_rerunning_rebuilds_the_tables(tmp_path):
    votes = votes_db(
        tmp_path, [(1, 1, "2024-10-01", 1), (2, 2, "2024-11-01", 1)], {1: [1, 2], 2: [1]}
    )
    cva = pipeline_db()
    first = attendance.build(votes, cva)
    assert attendance.build(votes, cva) == first
    assert votes.execute("SELECT count(*) FROM vote_absences").fetchone()[0] == 0


def test_only_plenary_records_get_absences(tmp_path):
    # A verified vote read from the text (e.g. a committee) is left out, even
    # though two members in office didn't appear on it.
    votes = votes_db(
        tmp_path,
        [(1, 1, "2024-10-01", 1), (2, 1, "2024-10-01", 1, "text"), (3, 2, "2024-11-01", 1)],
        {1: [1, 2, 3], 2: [1], 3: [1, 2, 3]},
    )
    attendance.build(votes, pipeline_db())
    assert votes.execute("SELECT vote_id FROM vote_attendance ORDER BY 1").fetchall() == [
        (1,),
        (3,),
    ]
    assert votes.execute("SELECT count(*) FROM vote_absences").fetchone()[0] == 0
