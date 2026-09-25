import sqlite3

import pytest
from starlette.testclient import TestClient

from cva import web
from cva.store import BlobStore

SCHEMA = """
CREATE TABLE documents (id INTEGER PRIMARY KEY, sha256 TEXT, gaceta_number TEXT,
                        publication_date TEXT, chamber TEXT);
CREATE TABLE votes (id INTEGER PRIMARY KEY, document_id INTEGER, session_date TEXT, acta TEXT,
                    bill_name TEXT, bill_title TEXT, subject TEXT, description TEXT,
                    result TEXT, vote_type TEXT, page INTEGER, is_committee INTEGER,
                    source TEXT, verified INTEGER, check_note TEXT);
CREATE TABLE vote_records (vote_id INTEGER, legislator TEXT, vote TEXT, legislator_id INTEGER);
CREATE VIEW vote_totals AS
SELECT v.id AS vote_id,
       count(*) FILTER (WHERE r.vote = 'yes') AS yes_count,
       count(*) FILTER (WHERE r.vote = 'no') AS no_count,
       count(*) FILTER (WHERE r.vote = 'abstain') AS abstain_count
FROM votes v LEFT JOIN vote_records r ON r.vote_id = v.id GROUP BY v.id;
CREATE TABLE legislators (id INTEGER PRIMARY KEY, name TEXT, photo_url TEXT);
CREATE TABLE legislator_terms (legislator_id INTEGER, chamber TEXT, start_date TEXT,
                               end_date TEXT, party TEXT);
CREATE TABLE legislator_service (legislator_id INTEGER, chamber TEXT, term_start TEXT,
                                 first_vote TEXT, last_vote TEXT, votes INTEGER);
CREATE TABLE vote_absences (vote_id INTEGER, legislator_id INTEGER, legislator TEXT,
                            in_session INTEGER);
CREATE TABLE text_record_legislators (vote_id INTEGER, legislator TEXT,
                                      legislator_id INTEGER, how TEXT);
CREATE TABLE vote_attendance (vote_id INTEGER PRIMARY KEY, session_date TEXT,
                              date_source TEXT, eligible INTEGER, voted INTEGER,
                              absent INTEGER, absent_in_session INTEGER);

INSERT INTO documents VALUES
    (1, 'aa', '100', '3 de octubre de 2024', 'Cámara'),
    (2, 'bb', '200', '9 de junio de 2025', 'Senado'),
    (3, 'cc', '300', '1 de julio de 2025', 'Cámara');
INSERT INTO votes VALUES
    (1, 1, '2024-10-01', '12', 'Proyecto de Ley 1', NULL, 'orden del día', 'Votación',
     'approved', 'procedural', 5, 0, 'record', 1, NULL),
    (2, 1, '', NULL, NULL, NULL, 'proposición', NULL, 'approved', 'procedural', 9, 0,
     'text', NULL, NULL),
    (3, 2, NULL, NULL, 'Proyecto de Ley 2', NULL, 'impedimento de Pérez', NULL,
     'rejected', 'impedimento', NULL, NULL, 'text', NULL, NULL),
    (4, 3, '2025-06-30', NULL, NULL, NULL, 'comisión primera', NULL, 'approved',
     'articles', 2, 1, 'text', NULL, NULL);
INSERT INTO vote_records VALUES
    (1, 'Pérez Ana', 'yes', 1),
    (1, 'Ruiz Beto', 'no', 2),
    (2, 'Beto Ruiz', 'yes', NULL),
    (3, 'Zapata Luis', 'no', NULL),
    (4, 'Ana Pérez', 'no', 1);
INSERT INTO legislators VALUES
    (1, 'Ana Pérez', 'https://example.org/1.jpg'), (2, 'Beto Ruiz', NULL),
    (3, 'Carla Díaz', NULL);
INSERT INTO legislator_terms VALUES
    (1, 'Cámara de Representantes', '2022-07-20', '2026-07-19', 'Liberal'),
    (1, 'Senado de la República', '2026-07-20', '2030-07-19', 'Verde'),
    (2, 'Cámara de Representantes', '2022-07-20', '2026-07-19', NULL),
    (3, 'Cámara de Representantes', '2022-07-20', '2026-07-19', 'Conservador');
INSERT INTO legislator_service VALUES
    (1, 'Cámara', '2022-07-20', '2024-10-01', '2025-06-30', 2);
INSERT INTO text_record_legislators VALUES (2, 'Beto Ruiz', 2, 'exact');
INSERT INTO vote_absences VALUES (1, 3, 'Carla Díaz', 1);
INSERT INTO vote_attendance VALUES (1, '2024-10-01', 'vote', 3, 2, 1, 1);
"""


@pytest.fixture
def votes_db(tmp_path):
    path = tmp_path / "votes.db"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def client(votes_db, tmp_path):
    store = BlobStore(tmp_path / "pdfs")
    store.path_for("aa").parent.mkdir(parents=True)
    store.path_for("aa").write_bytes(b"%PDF-1.4\n%%EOF")
    return TestClient(web.create_app(votes_db, pdfs=tmp_path / "pdfs"))


def test_committee_votes_are_left_out(client):
    stats = client.get("/api/stats").json()
    assert {k: stats[k] for k in ("votes", "records", "legislators")} == {
        "votes": 3,
        "records": 4,
        "legislators": 3,
    }
    assert [v["id"] for v in client.get("/api/votes").json()["votes"]] == [3, 2, 1]
    assert client.get("/api/votes/4").status_code == 404
    record = client.get("/api/legislators/1").json()["record"]
    assert [r["id"] for r in record] == [1]


def test_undated_votes_fall_back_to_the_gazette_then_publication(client):
    dates = {
        v["id"]: (v["date"], v["date_source"]) for v in client.get("/api/votes").json()["votes"]
    }
    assert dates == {
        1: ("2024-10-01", "vote"),
        2: ("2024-10-01", "gazette"),
        3: ("2025-06-09", "publication"),
    }


def test_search_ignores_accents_and_case_and_dates_filter(client):
    assert [v["id"] for v in client.get("/api/votes?q=PEREZ").json()["votes"]] == [3]
    assert [v["id"] for v in client.get("/api/votes?q=proyecto ley").json()["votes"]] == [3, 1]
    got = client.get("/api/votes?from=2025-01-01&to=2025-12-31").json()
    assert got["total"] == 1 and got["votes"][0]["id"] == 3
    assert [v["id"] for v in client.get("/api/votes?chamber=Senado").json()["votes"]] == [3]


def test_vote_groups_members_with_their_party_at_the_time(client):
    v = client.get("/api/votes/1").json()
    assert v["counts"] == {
        "yes": 1,
        "no": 1,
        "abstain": 0,
        "absent": 1,
        "absent_in_session": 1,
    }
    assert v["groups"]["yes"] == [
        {"id": 1, "name": "Ana Pérez", "photo_url": "https://example.org/1.jpg", "party": "Liberal"}
    ]
    assert [(p["name"], p["in_session"]) for p in v["groups"]["absent"]] == [("Carla Díaz", True)]
    assert v["gazette"] == {"number": "100", "published": "2024-10-03", "page": 5, "pdf": "/pdf/1"}


def test_text_votes_link_matched_names_and_have_no_absent_list(client):
    v = client.get("/api/votes/2").json()
    assert v["counts"]["absent"] is None and v["groups"]["absent"] is None
    assert v["groups"]["yes"] == [{"id": 2, "name": "Beto Ruiz", "photo_url": None, "party": None}]
    assert v["gazette"]["pdf"] == "/pdf/1"
    unmatched = client.get("/api/votes/3").json()
    assert unmatched["groups"]["no"] == [
        {"id": None, "name": "Zapata Luis", "photo_url": None, "party": None}
    ]
    assert unmatched["gazette"]["pdf"] is None
    record = client.get("/api/legislators/2").json()["record"]
    assert [(r["id"], r["position"]) for r in record] == [(2, "yes"), (1, "no")]


def test_legislator_profile(client):
    p = client.get("/api/legislators/1").json()
    assert p["party"] == "Verde"
    assert p["totals"] == {"yes": 1, "no": 0, "abstain": 0, "absent": 0, "absent_in_session": 0}
    assert p["record"][0]["in_session"] is None
    assert p["record"][0]["position"] == "yes" and p["record"][0]["party_then"] == "Liberal"
    absent = client.get("/api/legislators/3").json()["record"]
    assert [(r["id"], r["position"], r["in_session"]) for r in absent] == [(1, "absent", True)]
    assert client.get("/api/legislators/3").json()["totals"]["absent_in_session"] == 1
    assert [p["id"] for p in client.get("/api/legislators?q=diaz").json()["legislators"]] == [3]


def test_the_whole_database_can_be_downloaded(client, votes_db):
    r = client.get("/download-db")
    assert r.headers["content-disposition"].startswith('attachment; filename="colombia-vote-audit-')
    assert r.content == votes_db.read_bytes()
    assert client.get("/api/stats").json()["database_bytes"] == len(r.content)


def test_pdf_is_served_inline(client):
    r = client.get("/pdf/1")
    assert r.headers["content-type"] == "application/pdf"
    assert r.headers["content-disposition"].startswith("inline")
    assert client.get("/pdf/2").status_code == 404


def test_refuses_a_database_from_an_older_attendance_stage(votes_db):
    conn = sqlite3.connect(votes_db)
    conn.execute("ALTER TABLE vote_absences DROP COLUMN in_session")
    conn.commit()
    with pytest.raises(SystemExit, match="older cva.attendance"):
        web.Data(votes_db)


def test_refuses_a_database_without_the_attendance_tables(votes_db):
    sqlite3.connect(votes_db).execute("DROP TABLE legislator_terms")
    with pytest.raises(SystemExit, match="cva.attendance"):
        web.Data(votes_db)


def test_cache_headers_follow_how_often_things_change(votes_db, tmp_path):
    static = tmp_path / "dist"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text("<html></html>")
    (static / "assets" / "index-abc123.js").write_text("")
    client = TestClient(web.create_app(votes_db, static=static))
    assert client.get("/").headers["cache-control"] == "no-cache"
    assert "immutable" in client.get("/assets/index-abc123.js").headers["cache-control"]
    assert client.get("/api/votes").headers["cache-control"] == "public, max-age=300"
    assert "cache-control" not in client.get("/api/votes/999").headers
