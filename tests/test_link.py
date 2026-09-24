import json
import sqlite3

from cva import db
from cva.link import Candidate, link, resolve, words

CAMARA, SENADO = "Cámara de Representantes", "Senado de la República"


def cand(id, given, surnames, chamber=CAMARA):
    return Candidate(id, chamber, tuple(words(given)), tuple(words(surnames)))


ANA = cand(1, "Ana María", "Pérez Gómez")
ROSMERY = cand(2, "Rosmery", "Martínez Rosales")
GINA = cand(3, "Gina", "Parody Decheona")
ANGELICA = cand(4, "Angélica", "Lozano Correa")


def test_name_forms():
    for name in [
        "Pérez Gómez Ana María",
        "Ana María Pérez Gómez",
        "Ana Pérez",
        "Pérez G. Ana M.",
        "PEREZ GOMEZ ANA MARIA",
    ]:
        assert resolve(name, CAMARA, [ANA, ROSMERY]) == (1, "exact"), name


def test_typos_abbreviations_and_joined_names():
    people = [ANA, ROSMERY, GINA]
    assert resolve("Peres Gómez Ana", CAMARA, people) == (1, "fuzzy")
    assert resolve("Martínez Ros Rosmery", CAMARA, people) == (2, "fuzzy")
    assert resolve("Martínez Ros. Rosmery", CAMARA, people) == (2, "exact")
    assert resolve("Parody D´Echeona Gina", CAMARA, people) == (3, "exact")


def test_extra_word_only_when_rest_of_name_is_complete():
    # Congreso Visible leaves out her middle name.
    assert resolve("Lozano Correa Angélica Lisbeth", SENADO, [ANGELICA]) == (4, "fuzzy")
    assert resolve("Lozano Angélica Lisbeth", SENADO, [ANGELICA]) == (None, "none")


def test_needs_a_written_out_surname():
    assert resolve("Ana María", CAMARA, [ANA]) == (None, "none")
    assert resolve("Pérez", CAMARA, [ANA]) == (None, "none")
    assert resolve("P. G. Ana", CAMARA, [ANA]) == (None, "none")


def test_chamber_breaks_ties_and_ambiguity_is_reported():
    senator = cand(5, "Ana Lucía", "Pérez Díaz", SENADO)
    assert resolve("Ana Pérez", CAMARA, [ANA, senator]) == (1, "exact")
    assert resolve("Ana Pérez", SENADO, [ANA, senator]) == (5, "exact")
    other = cand(6, "Ana Isabel", "Pérez Ruiz")
    assert resolve("Ana Pérez", CAMARA, [ANA, other]) == (None, "ambiguous")
    # A wrong chamber in the source doesn't prevent a match.
    assert resolve("Ana Pérez", SENADO, [ANA]) == (1, "exact")


def test_link_fills_vote_records_by_session_date(tmp_path):
    cva = db.connect(":memory:")
    for pid, chamber, start, end, nombres, apellidos in [
        (1, CAMARA, "2010-07-20", "2014-07-19", "Ana María", "Pérez Gómez"),
        (2, CAMARA, "2014-07-20", "2018-07-19", "Ana Lucía", "Pérez Díaz"),
    ]:
        cva.execute("INSERT INTO legislators (id, name) VALUES (?, ?)", (pid, nombres))
        cva.execute(
            "INSERT INTO legislator_terms VALUES (?, ?, ?, ?, NULL, ?, '')",
            (pid, chamber, start, end, json.dumps({"nombres": nombres, "apellidos": apellidos})),
        )
    votes = sqlite3.connect(tmp_path / "votes.db")
    votes.executescript(
        """
        CREATE TABLE documents (id INTEGER PRIMARY KEY, chamber TEXT, source_file TEXT);
        CREATE TABLE votes (id INTEGER PRIMARY KEY, document_id INTEGER, session_date TEXT);
        CREATE TABLE vote_records (vote_id INTEGER, legislator TEXT, vote TEXT);
        INSERT INTO documents VALUES (1, NULL, '2012-100-camara-acta_plenaria.pdf');
        INSERT INTO votes VALUES (1, 1, '2012-05-02'), (2, 1, '2016-05-02');
        INSERT INTO vote_records VALUES (1, 'Pérez Ana', 'yes'), (2, 'Pérez Ana', 'no'),
                                        (2, 'Nadie Nunca', 'no');
        """
    )
    stats = link(votes, cva)
    assert stats == {"exact": 2, "none": 1}
    rows = votes.execute(
        "SELECT vote_id, legislator_id, legislator_match FROM vote_records ORDER BY rowid"
    ).fetchall()
    assert rows == [(1, 1, "exact"), (2, 2, "exact"), (2, None, "none")]
    # Re-running is safe: the columns already exist.
    assert link(votes, cva) == stats
