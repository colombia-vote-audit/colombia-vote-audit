"""The House "barcode": every representative's ballot on every checked roll call.

Rows are representatives, columns are House plenary votes read from scanned
voting records that passed their checks (source = 'record', verified = 1) and
have a session date from the attendance stage. Each cell is one character:

    y  yes         n  no         b  abstained
    s  didn't vote, but voted on another checked vote that day (in session)
    a  absent: no vote recorded that day
    .  outside the member's time in the chamber

`gov` on a column is how most Pacto Histórico members voted, the reference
for "with the government". Parties are grouped into families, so the
coalition lists (e.g. "Alianza Verde Centro Esperanza Coalición") sit with
their main party.

`Grid.query` computes the figures the site's question box (cva.ask) quotes,
so every number it states comes from here rather than from the model.
"""

from __future__ import annotations

import re
import sqlite3
from collections import Counter

from cva.text import fold

FAMILIES = [
    ("Pacto Histórico", r"Pacto"),
    ("Comunes", r"Comunes"),
    ("Alianza Verde", r"Verde|Oxígeno"),
    ("Conservador", r"Conservador"),
    ("Liberal", r"Liberal"),
    ("Centro Democrático", r"Centro Democrático"),
    ("Cambio Radical", r"Cambio Radical"),
    ("La U", r"Partido de la U"),
    ("MIRA · Justa Libres", r"MIRA|Justa Libres"),
]
OTHER = "Other parties"
GOV = "Pacto Histórico"
# A party's majority is only meaningful with a few members to make it.
MIN_PARTY = 4
CODES = {"yes": "y", "no": "n", "abstain": "b"}
WORDS = {"y": "yes", "n": "no", "b": "abstained"}
GOV_CODE = {"yes": "y", "no": "n", "abstain": "b"}
METRICS = ("breaks_with_party", "with_pacto", "against_pacto", "absent", "in_session_not_voting")


def family(party: str | None) -> str:
    return next((f for f, rx in FAMILIES if re.search(rx, party or "")), OTHER)


def contested(yes: int, no: int, min_share: float = 15) -> bool:
    """At least 20 yes/no ballots, and the losing side got at least
    `min_share` percent of them. A threshold of 0 counts every vote."""
    if min_share <= 0:
        return True
    return yes + no >= 20 and min(yes, no) / (yes + no) * 100 >= min_share


def pct(part: int, whole: int) -> int | None:
    return round(part / whole * 100) if whole else None


class Grid:
    def __init__(self, conn: sqlite3.Connection, party_on):
        """`party_on(legislator_id, chamber, date)` gives a member's party then."""
        rows = conn.execute(
            """
            SELECT v.id, a.session_date, v.result, v.vote_type, v.subject, v.bill_name,
                   v.bill_title, t.yes_count, t.no_count, t.abstain_count, d.gaceta_number,
                   v.page, d.id
            FROM votes v
            JOIN documents d ON d.id = v.document_id
            JOIN vote_totals t ON t.vote_id = v.id
            JOIN vote_attendance a ON a.vote_id = v.id
            WHERE d.chamber = 'Cámara' AND v.source = 'record' AND v.verified = 1
              AND coalesce(v.is_committee, 0) = 0 AND a.session_date IS NOT NULL
            ORDER BY a.session_date, v.id
            """
        ).fetchall()
        self.cols = [
            {
                "id": vid,
                "date": date,
                "res": res,
                "type": vtype,
                "subj": subj or "",
                "bill": bill or "",
                "title": title or "",
                "y": y,
                "n": n,
                "b": b,
                "gac": gac or None,
                "page": page,
                "doc": doc,
                "contested": contested(y, n),
                "_j": j,
            }
            for j, (vid, date, res, vtype, subj, bill, title, y, n, b, gac, page, doc) in enumerate(
                rows
            )
        ]
        index = {c["id"]: j for j, c in enumerate(self.cols)}
        cells: dict[int, list[str]] = {}
        first: dict[int, str] = {}

        def cell(lid: int) -> list[str]:
            return cells.setdefault(lid, ["."] * len(self.cols))

        ids = ",".join(map(str, index)) or "NULL"
        for vid, lid, vote in conn.execute(
            f"SELECT vote_id, legislator_id, vote FROM vote_records"
            f" WHERE legislator_id IS NOT NULL AND vote_id IN ({ids})"
        ):
            j = index[vid]
            cell(lid)[j] = CODES[vote]
            first[lid] = min(first.get(lid, self.cols[j]["date"]), self.cols[j]["date"])
        for vid, lid, in_session in conn.execute(
            f"SELECT vote_id, legislator_id, in_session FROM vote_absences WHERE vote_id IN ({ids})"
        ):
            cell(lid)[index[vid]] = "s" if in_session else "a"
        names = dict(conn.execute("SELECT id, name FROM legislators"))

        self.rows = []
        for lid, cs in cells.items():
            party = party_on(lid, "Cámara", first.get(lid))
            self.rows.append(
                {
                    "id": lid,
                    "name": names.get(lid, str(lid)),
                    "party": family(party),
                    "party_name": party,
                    "cells": "".join(cs),
                }
            )
        size = Counter(r["party"] for r in self.rows)
        self.majority: list[dict[str, str | None]] = []
        for j, c in enumerate(self.cols):
            tally: dict[str, Counter] = {}
            for r in self.rows:
                v = r["cells"][j]
                if v in "ynb" and size[r["party"]] >= MIN_PARTY:
                    tally.setdefault(r["party"], Counter())[v] += 1
            maj = {}
            for p, t in tally.items():
                (top, k), *rest = t.most_common(2) + [(None, 0)]
                maj[p] = top if k > rest[0][1] else None
            self.majority.append(maj)
            c["gov"] = {"y": "yes", "n": "no", "b": "abstain"}.get(maj.get(GOV) or "")
        # Parties from most to least aligned with the Pacto, members likewise.
        for r in self.rows:
            r["align"] = self.stats(r, [c for c in self.cols if c["contested"]])["with_pacto_pct"]
        mean = {
            p: sum(r["align"] or 0 for r in self.rows if r["party"] == p) / n
            for p, n in size.items()
        }
        self.parties = sorted(size, key=lambda p: (p == OTHER, -mean[p]))
        self.rows.sort(key=lambda r: (self.parties.index(r["party"]), -(r["align"] or 0)))
        self.by_id = {r["id"]: r for r in self.rows}
        self.col_by_id = {c["id"]: c for c in self.cols}
        self._search = {
            c["id"]: fold(f"{c['subj']} {c['bill']} {c['title']} {c['type']}") for c in self.cols
        }
        self.size = size

    # ----- filters and figures -----

    def select(
        self, topic: str = "", min_share: float = 15, since=None, until=None, types=None
    ) -> list[dict]:
        """Votes on a topic whose losing side got at least `min_share` percent."""
        q = fold(topic)
        pool = [
            c
            for c in self.cols
            if contested(c["y"], c["n"], min_share) and (not types or c["type"] in types)
        ]
        found = [c for c in pool if q in self._search[c["id"]]] if q else pool
        if q and not found:  # every word, in any order
            words = q.split()
            found = [c for c in pool if all(w in self._search[c["id"]] for w in words)]
        return [c for c in found if (since or "") <= c["date"] <= (until or "9999")]

    def stats(self, row: dict, cols: list[dict]) -> dict:
        cast = yes = no = absent = skipped = wp = wpn = wo = won = 0
        breaks = []
        for c in cols:
            j = c["_j"]
            v = row["cells"][j]
            absent += v == "a"
            skipped += v == "s"
            if v not in "ynb":
                continue
            cast += 1
            yes += v == "y"
            no += v == "n"
            if c.get("gov"):
                wpn += 1
                wp += v == GOV_CODE[c["gov"]]
            m = self.majority[j].get(row["party"])
            if m:
                won += 1
                if v == m:
                    wo += 1
                else:
                    breaks.append(c)
        return {
            "cast": cast,
            "yes": yes,
            "no": no,
            "absent": absent,
            "in_session_not_voting": skipped,
            "with_pacto_pct": pct(wp, wpn),
            "with_own_party_pct": pct(wo, won),
            "breaks": breaks,
            "_raw": (wp, wpn, wo, won),
        }

    def topics(self, n: int = 15) -> list[dict]:
        """The bills with the most contested roll calls, by bill number, to
        suggest filters that match them."""
        groups: dict[str, list[dict]] = {}
        for c in self.cols:
            if c["contested"] and (m := re.search(r"(\d+) de (20\d\d)", c["bill"])):
                groups.setdefault(f"{m[1]} de {m[2]}", []).append(c)
        top = sorted(groups.items(), key=lambda kv: -len(kv[1]))[:n]
        return [
            {
                "filter": key,
                "contested_votes": len(cs),
                "title": next((c["title"] for c in cs if c["title"]), cs[0]["bill"])[:110],
                "dates": f"{cs[0]['date']} to {cs[-1]['date']}",
            }
            for key, cs in top
        ]

    def brief(self, c: dict) -> dict:
        return {
            "vote_id": c["id"],
            "date": c["date"],
            "subject": c["subj"][:140],
            "bill": c["bill"][:90],
            "yes": c["y"],
            "no": c["n"],
            "result": c["res"],
            "pacto_voted": c["gov"],
            "gazette": c["gac"],
            "page": c["page"],
        }

    def find(self, name) -> list[dict]:
        if isinstance(name, int) or str(name).isdigit():
            r = self.by_id.get(int(name))
            return [r] if r else []
        words = fold(str(name)).split()
        return [r for r in self.rows if words and all(w in fold(r["name"]) for w in words)]

    def query(
        self,
        topic: str = "",
        member=None,
        party: str | None = None,
        metric: str | None = None,
        since: str | None = None,
        until: str | None = None,
        min_share: float = 15,
        types: list[str] | None = None,
    ) -> dict:
        """Everything the question box can cite, for one topic filter."""
        cols = self.select(topic or "", min_share, since, until, types)
        out: dict = {"votes_matched": len(cols)}
        if not cols:
            out["note"] = (
                "No roll calls match. Try a shorter term or a bill number like '166 de 2023'."
            )
            return out
        dates = sorted(c["date"] for c in cols)
        out |= {
            "first": dates[0],
            "last": dates[-1],
            "approved": sum(c["res"] == "approved" for c in cols),
            "rejected": sum(c["res"] == "rejected" for c in cols),
        }
        parties = {}
        for p in self.parties:
            wp = wpn = wo = won = 0
            for r in self.rows:
                if r["party"] == p:
                    a, b, c2, d = self.stats(r, cols)["_raw"]
                    wp, wpn, wo, won = wp + a, wpn + b, wo + c2, won + d
            if wpn:
                parties[p] = {"with_pacto_pct": pct(wp, wpn), "with_own_party_pct": pct(wo, won)}
        out["parties"] = parties
        out["closest_votes"] = [
            self.brief(c) for c in sorted(cols, key=lambda c: abs(c["y"] - c["n"]))[:8]
        ]
        if member is not None:
            found = self.find(member)
            if len(found) != 1:
                out["member_candidates"] = [
                    {"id": r["id"], "name": r["name"], "party": r["party"]} for r in found[:10]
                ] or "no member matches"
            else:
                r = found[0]
                s = self.stats(r, cols)
                peers = sorted(
                    x
                    for x in (
                        self.stats(o, cols)["with_own_party_pct"]
                        for o in self.rows
                        if o["party"] == r["party"]
                    )
                    if x is not None
                )
                mine = s["with_own_party_pct"]
                out["member"] = {
                    "id": r["id"],
                    "name": r["name"],
                    "party": r["party"],
                    **{k: v for k, v in s.items() if k not in ("breaks", "_raw")},
                    "loyalty_rank_in_party": (
                        f"{sum(x < mine for x in peers) + 1} of {len(peers)} (1 = least loyal)"
                        if mine is not None
                        else None
                    ),
                    "recent_breaks_with_party": [
                        {
                            **self.brief(c),
                            "member_voted": WORDS[r["cells"][c["_j"]]],
                            "party_majority": WORDS[self.majority[c["_j"]][r["party"]]],
                        }
                        for c in s["breaks"][-8:][::-1]
                    ],
                }
        if metric in METRICS:
            least = max(3, round(len(cols) * 0.3))
            ranked = []
            for r in self.rows:
                if party and r["party"] != party:
                    continue
                s = self.stats(r, cols)
                value = {
                    "breaks_with_party": s["with_own_party_pct"],
                    "with_pacto": s["with_pacto_pct"],
                    "against_pacto": s["with_pacto_pct"],
                    "absent": s["absent"],
                    "in_session_not_voting": s["in_session_not_voting"],
                }[metric]
                counts_ballots = metric not in ("absent", "in_session_not_voting")
                if value is None or (counts_ballots and s["cast"] < least):
                    continue
                ranked.append((value, r, s))
            ascending = metric in ("breaks_with_party", "against_pacto")
            ranked.sort(key=lambda x: x[0] if ascending else -x[0])
            out["ranking"] = {
                "metric": metric,
                "min_ballots": least,
                "top": [
                    {
                        "id": r["id"],
                        "name": r["name"],
                        "party": r["party"],
                        "ballots": s["cast"],
                        "with_own_party_pct": s["with_own_party_pct"],
                        "with_pacto_pct": s["with_pacto_pct"],
                        "absent": s["absent"],
                        "in_session_not_voting": s["in_session_not_voting"],
                    }
                    for _, r, s in ranked[:8]
                ],
            }
        return out

    # ----- what the site gets -----

    def payload(self) -> dict:
        return {
            "parties": self.parties,
            "rows": [
                {k: r[k] for k in ("id", "name", "party", "party_name", "cells")} for r in self.rows
            ],
            "cols": [{k: v for k, v in c.items() if not k.startswith("_")} for c in self.cols],
        }


VOTE_TYPES = ("final_passage", "articles", "report_motion", "procedural", "impedimento")


def clean_view(grid: Grid, v) -> dict | None:
    """A view the site can apply, keeping only fields and ids it knows."""
    if not isinstance(v, dict):
        return None
    out: dict = {}
    if isinstance(v.get("q"), str):
        out["q"] = v["q"][:80]
    for key, allowed in (
        ("color", ("ab", "with", "party", "vote", "outcome", "attend")),
        ("order", ("date", "a", "margin")),
        ("sort", ("party", "align")),
        ("rows", ("compact", "tall", "named")),
        ("a", grid.parties),
        ("b", grid.parties),
    ):
        if v.get(key) in allowed:
            out[key] = v[key]
    if isinstance(v.get("min"), int | float) and not isinstance(v["min"], bool):
        out["min"] = max(0, min(50, round(v["min"])))
    if isinstance(v.get("types"), list):
        out["types"] = [t for t in v["types"] if t in VOTE_TYPES]
    for key in ("from", "to"):
        if isinstance(v.get(key), str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", v[key]):
            out[key] = v[key]
    if isinstance(v.get("pins"), list):
        out["pins"] = [int(p) for p in v["pins"] if str(p).isdigit() and int(p) in grid.by_id][:12]
    if isinstance(v.get("parties"), list):
        out["parties"] = [p for p in v["parties"] if p in grid.parties]
    if str(v.get("col", "")).isdigit() and int(v["col"]) in grid.col_by_id:
        out["col"] = int(v["col"])
    return out
