"""The barcode's question box: a language model that reads the grid through
one tool and answers with text and a view to show.

The model gets the member list and the reader's current view in its
instructions, and can call `query` (cva.barcode.Grid.query) for figures, so
every number it states was computed here. It ends with one JSON object:

    {"answer": "...", "view": {...} | null}

`view` is cleaned by cva.barcode.clean_view, so the model can only point at
members, parties and votes that exist.

The model is any OpenAI-compatible chat completions endpoint, set by
environment variables; the question box is off unless all three are set:

    CVA_LLM_BASE_URL   e.g. https://gateway.example.com (".../v1" is added if missing)
    CVA_LLM_API_KEY
    CVA_LLM_MODEL      a model that calls tools and answers after them; with the
                       TokenFactory gateway, glm-5.3-flash-uncensored-fp8 does (its
                       deepseek flash model leaves the answer empty after tool calls)
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable

import httpx

from cva.barcode import METRICS, VOTE_TYPES, Grid, clean_view

ROUNDS = 5  # model calls per question; the last one may not use the tool
MAX_TURNS = 8
MAX_CHARS = 600

Complete = Callable[[list[dict], list[dict] | None], dict]


def from_env() -> Complete | None:
    base = os.environ.get("CVA_LLM_BASE_URL", "").rstrip("/")
    key = os.environ.get("CVA_LLM_API_KEY", "")
    model = os.environ.get("CVA_LLM_MODEL", "")
    if not (base and key and model):
        return None
    if not base.endswith("/v1"):
        base += "/v1"
    client = httpx.Client(
        base_url=base, headers={"Authorization": f"Bearer {key}"}, timeout=httpx.Timeout(90)
    )

    def complete(messages: list[dict], tools: list[dict] | None) -> dict:
        body = {"model": model, "messages": messages, "temperature": 0.2, "max_tokens": 5000}
        if tools:
            body["tools"] = tools
        res = client.post("/chat/completions", json=body)
        res.raise_for_status()
        return res.json()["choices"][0]["message"]

    return complete


def query_tool(grid: Grid) -> dict:
    return {
        "type": "function",
        "function": {
            "name": "query",
            "description": (
                "Figures from the House roll calls for one filter. Returns how many votes "
                "match, their dates and outcomes, each party's share of ballots on the Pacto's "
                "side and with its own majority, and the closest votes (with vote_id, gazette and "
                "page to cite). Add `member` for one representative's record and their breaks "
                "with their party; add `metric` to rank members."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string",
                        "description": "Text in the vote's subject or bill, Spanish, e.g. "
                        "'pensional', 'salud', '166 de 2023'. Empty for all votes.",
                    },
                    "member": {"type": "integer", "description": "Member id from the list."},
                    "party": {"enum": grid.parties, "description": "Limit a ranking to a party."},
                    "metric": {"enum": list(METRICS)},
                    "from": {"type": "string", "description": "YYYY-MM-DD"},
                    "to": {"type": "string", "description": "YYYY-MM-DD"},
                    "min_share": {
                        "type": "integer",
                        "description": "Only votes whose losing side got at least this percent "
                        "of yes+no ballots, 0-50. Default 15; 0 includes every vote.",
                    },
                    "types": {
                        "type": "array",
                        "items": {"enum": list(VOTE_TYPES)},
                        "description": "Only these kinds of vote.",
                    },
                },
            },
        },
    }


def instructions(grid: Grid, view: dict, lang: str) -> str:
    members = "\n".join(f"{r['id']}|{r['name']}|{r['party']}" for r in grid.rows)
    contested = sum(c["contested"] for c in grid.cols)
    return f"""You help activists and journalists explore how Colombia's House of Representatives \
(Cámara) voted from late 2023 to mid-2026. The site shows a "barcode": one row per representative \
({len(grid.rows)}), one column per checked plenary roll call ({len(grid.cols)}, of which \
{contested} where the losing side got at least 15%, the default threshold).

Rules:
- Every number you state must come from a `query` result in this conversation. Never estimate. \
You may add brief, neutral political context, saying it isn't from this data.
- "With the Pacto" means the same side as most Pacto Histórico members (Gustavo Petro's governing \
coalition). "With own party" compares a ballot with the member's party majority.
- In `answer`, write a roll call as [v:VOTE_ID] and a member as [m:MEMBER_ID]; the site makes them \
links. Name the gazette and page for key votes.
- Absence only means no vote was recorded. This is House plenary roll calls from scanned records, \
not the Senate or committees.
- Be factual and fair. Answer in the reader's language (the site is in "{lang}"). Keep `answer` \
under 150 words unless asked for more.

Showing things: set `view` to change what the reader sees. Fields (all optional; omitted ones \
reset to defaults): q (topic filter text), min (0-50: only votes whose losing side got at least \
this percent; default 15, 0 = every vote), types (kinds of vote: {", ".join(VOTE_TYPES)}), \
a and b (two party names, default "Pacto Histórico" and "Centro Democrático"), color ("ab" \
sided with party a or party b where they disagreed | "with" same side as party a or not | "party" \
with/against own party majority, shows rebels | "vote" yes/no | "outcome" winning or losing side \
| "attend" absences), order (columns: "date" | "a" grouped by party a's vote | "margin" closest \
first), sort (rows: "party" grouped | "align" every member ranked by agreement with party a on \
the votes shown), from/to (YYYY-MM-DD zoom), parties (party names to show only those rows), \
pins (member ids shown as labeled rows on top, max 12), col (vote_id to highlight), rows \
("compact" | "tall" | "named" shows every name; use with a parties filter). Party names: \
{", ".join(grid.parties)}. Keep the reader's current settings unless they ask to change them.
When the reader asks about a person, pin them. Match `query`'s min_share to the view's min.

Bills with the most contested roll calls (use `filter` as the topic; words like 'laboral', \
'salud', 'pensional' also match bill titles):
{json.dumps(grid.topics(), ensure_ascii=False)}

Reply, after any tool calls, with only this JSON object:
{{"answer": "...", "view": {{...}} or null}}

The reader's current view: {json.dumps(view, ensure_ascii=False)}

Members (id|name|party):
{members}"""


def run_query(grid: Grid, args: dict) -> dict:
    member = args.get("member")
    return grid.query(
        topic=str(args.get("topic") or "")[:80],
        member=member if isinstance(member, int | str) and str(member).strip() else None,
        party=args.get("party") if args.get("party") in grid.parties else None,
        metric=args.get("metric") if args.get("metric") in METRICS else None,
        since=args.get("from") if isinstance(args.get("from"), str) else None,
        until=args.get("to") if isinstance(args.get("to"), str) else None,
        min_share=max(0, min(50, args["min_share"]))
        if isinstance(args.get("min_share"), int | float)
        else 15,
        types=[t for t in args.get("types") or [] if t in VOTE_TYPES] or None,
    )


def parse_reply(text: str) -> dict | None:
    """The final JSON object, read tolerantly: a bare object, one inside a code
    fence, or the span from the first { to the last }. None if there is none."""
    text = (text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    for candidate in (text, fenced and fenced.group(1), text[text.find("{") : text.rfind("}") + 1]):
        if not candidate:
            continue
        try:
            value = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return None


def ask(grid: Grid, complete: Complete, turns: list[dict], view: dict, lang: str = "en") -> dict:
    """Answer the last user turn. `turns` alternate user/assistant, oldest first."""
    messages = [{"role": "system", "content": instructions(grid, view, lang)}]
    messages += [
        {"role": t["role"], "content": str(t["content"])[:4000]} for t in turns[-MAX_TURNS:]
    ]
    tools = [query_tool(grid)]
    queries = []
    for round_ in range(ROUNDS):
        message = complete(messages, tools if round_ < ROUNDS - 1 else None)
        calls = message.get("tool_calls") or []
        if not calls:
            content = message.get("content") or ""
            reply = parse_reply(content)
            if reply is None:
                # Empty (some models answer only in their hidden reasoning after
                # tool results) or not JSON: ask once more.
                messages += [
                    {"role": "assistant", "content": content or "(no reply)"},
                    {"role": "user", "content": "Reply again with only the JSON object."},
                ]
                continue
            return {
                "answer": str(reply.get("answer") or "").strip(),
                "view": clean_view(grid, reply.get("view")),
                "queries": queries,
            }
        messages.append(
            {"role": "assistant", "content": message.get("content") or "", "tool_calls": calls}
        )
        for call in calls:
            try:
                args = json.loads(call["function"].get("arguments") or "{}")
                result = run_query(grid, args if isinstance(args, dict) else {})
                queries.append(
                    {k: v for k, v in args.items() if v is not None and v != "" and v is not False}
                )
            except (ValueError, KeyError, TypeError) as e:
                result = {"error": f"bad arguments: {e}"}
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.get("id", ""),
                    "content": json.dumps(result, ensure_ascii=False),
                }
            )
    raise RuntimeError("the model kept calling tools without answering")
