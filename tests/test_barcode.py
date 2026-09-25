import json
import sqlite3

import pytest

from cva import ask
from cva.barcode import clean_view, family
from cva.web import Data
from test_web import SCHEMA

TABLES = SCHEMA.split("INSERT", 1)[0]

# Ten Pacto members (1-10), ten Liberals (11-20), four from Centro Democrático
# (21-24). Liberal 11 breaks with the party; CD 24 misses the second vote.
PARTIES = {**dict.fromkeys(range(1, 11), "Pacto Histórico Coalición")}
PARTIES |= dict.fromkeys(range(11, 21), "Liberal Colombiano")
PARTIES |= dict.fromkeys(range(21, 25), "Centro Democrático")
VOTES = [
    # id, date, bill, bill title, how each block voted
    (1, "2024-06-13", "Proyecto de Ley 433 de 2024", "reforma pensional", "y", "y", "n"),
    (2, "2024-10-01", "Proyecto de Ley 166 de 2023", "reforma laboral", "y", "y", "n"),
    (3, "2024-10-02", "Proyecto de Ley 166 de 2023", "reforma laboral", "y", "n", "n"),
]


@pytest.fixture
def grid(tmp_path):
    path = tmp_path / "votes.db"
    conn = sqlite3.connect(path)
    conn.executescript(TABLES)
    conn.execute("INSERT INTO documents VALUES (1, 'aa', '514', '3 de octubre de 2024', 'Cámara')")
    word = {"y": "yes", "n": "no"}
    for lid, party in PARTIES.items():
        conn.execute("INSERT INTO legislators VALUES (?, ?, NULL)", (lid, f"Member {lid}"))
        conn.execute(
            "INSERT INTO legislator_terms VALUES (?, 'Cámara de Representantes',"
            " '2022-07-20', '2026-07-19', ?)",
            (lid, party),
        )
    for vid, date, bill, title, pacto, lib, cd in VOTES:
        conn.execute(
            "INSERT INTO votes VALUES (?, 1, ?, NULL, ?, ?, 'artículo', NULL, 'approved',"
            " 'articles', ?, 0, 'record', 1, NULL)",
            (vid, date, bill, title, 60 + vid),
        )
        conn.execute("INSERT INTO vote_attendance VALUES (?, ?, 'vote', 24, 24, 0, 0)", (vid, date))
        for lid in PARTIES:
            block = pacto if lid <= 10 else lib if lid <= 20 else cd
            if lid == 11:
                block = "n" if block == "y" else "y"
            if lid == 24 and vid == 2:
                conn.execute("INSERT INTO vote_absences VALUES (2, 24, 'Member 24', 0)")
                continue
            conn.execute(
                "INSERT INTO vote_records VALUES (?, ?, ?, ?)",
                (vid, f"Member {lid}", word[block], lid),
            )
    conn.commit()
    conn.close()
    return Data(path).grid


def test_rows_are_grouped_by_party_family_from_most_aligned():
    assert family("Alianza Verde Centro Esperanza Coalición") == "Alianza Verde"
    assert family("Partido de la U - Partido de la Unión por la Gente") == "La U"
    assert family("Nuevo Partido") == "Other parties"


def test_grid_cells_and_the_pacto_line(grid):
    assert grid.parties == ["Pacto Histórico", "Liberal", "Centro Democrático"]
    assert [c["gov"] for c in grid.cols] == ["yes", "yes", "yes"]
    assert grid.by_id[11]["cells"] == "nny"
    assert grid.by_id[24]["cells"] == "nan"
    assert all(c["contested"] for c in grid.cols)
    payload = grid.payload()
    assert payload["cols"][0]["title"] == "reforma pensional"
    assert not any(k.startswith("_") for k in payload["cols"][0])


def test_query_counts_a_members_breaks_on_a_topic(grid):
    q = grid.query("laboral", member=11)
    assert (q["votes_matched"], q["first"], q["last"]) == (2, "2024-10-01", "2024-10-02")
    assert q["parties"]["Pacto Histórico"] == {"with_pacto_pct": 100, "with_own_party_pct": 100}
    m = q["member"]
    assert (m["cast"], m["with_pacto_pct"], m["with_own_party_pct"]) == (2, 50, 0)
    assert m["loyalty_rank_in_party"] == "1 of 10 (1 = least loyal)"
    assert [
        (b["vote_id"], b["member_voted"], b["party_majority"], b["page"])
        for b in m["recent_breaks_with_party"]
    ] == [(3, "yes", "no", 63), (2, "no", "yes", 62)]


def test_query_ranks_and_finds_members(grid):
    top = grid.query("", metric="breaks_with_party", party="Liberal")["ranking"]["top"]
    assert top[0]["id"] == 11
    absent = grid.query("", metric="absent")["ranking"]["top"]
    assert (absent[0]["id"], absent[0]["absent"]) == (24, 1)
    assert grid.query("", member="member 2")["member_candidates"][0]["id"] == 2
    assert grid.query("nada que ver")["votes_matched"] == 0


def test_views_keep_only_known_fields_and_ids(grid):
    view = {
        "color": "outcome", "a": "Liberal", "b": "Nope", "order": "margin", "sort": "align",
        "min": 72, "types": ["articles", "bogus"], "pins": [11, 999, "x"], "col": 3,
        "rows": "big", "parties": ["Liberal", "Nope"], "from": "2024-10-01", "to": "soon",
    }  # fmt: skip
    assert clean_view(grid, view) == {
        "color": "outcome", "a": "Liberal", "order": "margin", "sort": "align", "min": 50,
        "types": ["articles"], "pins": [11], "col": 3, "parties": ["Liberal"],
        "from": "2024-10-01",
    }  # fmt: skip
    assert clean_view(grid, {"min": True}) == {}
    assert clean_view(grid, "zoom") is None


def test_the_threshold_and_vote_types_choose_the_votes(grid):
    # Losing sides: vote 1 5 of 24, vote 2 4 of 23, vote 3 11 of 24.
    assert [c["id"] for c in grid.select(min_share=15)] == [1, 2, 3]
    assert [c["id"] for c in grid.select(min_share=20)] == [1, 3]
    assert [c["id"] for c in grid.select(min_share=45)] == [3]
    assert [c["id"] for c in grid.select(min_share=0, types=["procedural"])] == []
    assert grid.query("laboral", min_share=40)["votes_matched"] == 1


class Script:
    """A stand-in model that replays replies and records what it was sent."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.seen = []

    def __call__(self, messages, tools):
        self.seen.append((messages, tools))
        return self.replies.pop(0)


def call(args):
    return {
        "content": "",
        "tool_calls": [
            {
                "id": "c1",
                "type": "function",
                "function": {"name": "query", "arguments": json.dumps(args)},
            }
        ],
    }


def test_ask_runs_the_query_and_cleans_the_reply(grid):
    final = {
        "answer": "Member 11 broke ranks [m:11].",
        "view": {"q": "laboral", "pins": [11, 12345], "color": "party", "min": 30},
        "story": {"title": "Ignored", "steps": []},
    }
    query = {"topic": "laboral", "member": 11, "min_share": 0}
    model = Script(call(query), {"content": json.dumps(final)})
    out = ask.ask(grid, model, [{"role": "user", "content": "Who broke ranks?"}], {"q": ""}, "es")
    assert out == {
        "answer": "Member 11 broke ranks [m:11].",
        "view": {"q": "laboral", "pins": [11], "color": "party", "min": 30},
        "queries": [query],
    }
    assert out["queries"] == [{"topic": "laboral", "member": 11, "min_share": 0}]
    tool_result = json.loads(model.seen[1][0][-1]["content"])
    assert tool_result["member"]["with_own_party_pct"] == 0
    assert '"es"' in model.seen[0][0][0]["content"]


def test_ask_asks_again_when_the_reply_is_empty_or_not_json(grid):
    model = Script(
        {"content": ""},
        {"content": "Sure! Here you go."},
        {"content": '```json\n{"answer": "ok", "view": null}\n```'},
    )
    out = ask.ask(grid, model, [{"role": "user", "content": "hi"}], {})
    assert (out["answer"], out["view"]) == ("ok", None)
    assert model.seen[-1][0][-1]["content"] == "Reply again with only the JSON object."


def test_ask_gives_up_when_the_model_only_calls_tools(grid):
    model = Script(*[call({"topic": "x"})] * ask.ROUNDS)
    with pytest.raises(RuntimeError):
        ask.ask(grid, model, [{"role": "user", "content": "hi"}], {})
    assert model.seen[-1][1] is None  # the last round offers no tool
