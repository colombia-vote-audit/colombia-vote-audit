# colombia-vote-audit

Fetch pipeline for Colombian congressional voting records.

## Sources

**Imprenta Nacional, Gaceta del Congreso**
(`svrpubindc.imprenta.gov.co/senado/`)
- Every gazette issue from 2000 on (~31.6k PDFs). Plenary and committee session records (actas) contain the roll-call votes.
- It is a JSF app with no API. `cva.sources.imprenta` drives its data table: it loads a page of rows, then downloads rows from that page.
- A gazette is identified by (number, chamber, date). (year, number) is almost unique, but the archive has a few data-entry duplicates.

**Congreso Visible** (`apicongresovisible.uniandes.edu.co`, Universidad de los Andes)
- Bills back to 1998. Each bill's status timeline cites gazettes as `number/yy`; these are parsed into `bill_state_gazettes`.
- Legislators of both chambers for every term from 1998–2002 on (`cva sync-legislators`). `legislators` has one row per person. Its `id` is Congreso Visible's `persona_id`, so it stays the same across syncs, and it is the key that vote data should reference. `legislator_terms` holds each chamber, term and party. Term dates are the constitutional ones (20 July to 19 July), so a replacement who sat for only part of a term is listed as serving the whole term.
- Votes, including per-legislator roll calls for ~5.3k votes (mostly 2011–2018) in `datos_importacion`.
- The API is undocumented; the endpoints are listed in `cva.sources.congresovisible`.

Gazettes from 1998–1999 are in neither source.

## Usage

```sh
nix develop        # or: uv sync
uv run cva --help

uv run cva sync-gazettes             # Imprenta index, ~10 s
uv run cva fetch-gazettes            # download every pending PDF, session records first
uv run cva sync-bills --details 100  # bill index + up to 100 bill timelines
uv run cva sync-votes                # Congreso Visible votes, ~1 min
uv run cva sync-legislators          # legislators and their terms, ~20 s
uv run cva status
uv run cva daily                     # what the scheduled job runs
```

State lives in `data/` (override with `--data-dir` or `CVA_DATA_DIR`):
- `data/cva.db` is SQLite.
- `data/pdfs/` holds PDFs, stored by sha256.

## Fetching

The full archive is roughly 100–140 GB. With the default 3 workers recent gazettes download at about 13 MB/s. Old gazettes are small, so they are limited by round trips instead, at about 1.7 files per second. A complete backfill takes about 5–6 hours.

- **Rate:** each worker keeps its own archive session and waits 1 s between requests (`--interval`). `--workers` sets the number of parallel sessions.
- **Verification:** a file is stored only if its Content-Type is PDF, its size matches Content-Length, it starts with `%PDF` and it ends with `%%EOF`. Files are written to a temp file, synced, then renamed into place, so a crash can't leave a partial file under a valid name.
- **Resumable:** every gazette has a `fetch_status` (`pending`, `ok`, `missing`, `unavailable` or `error`). Stopping and rerunning picks up where it left off. Failures are retried up to 3 times across runs.
- **Moving rows:** new gazettes shift row positions in the archive listing during a long run. The index is re-read every 20 minutes, and gazettes that moved are retried without counting as failures.
- **Stopping:** Ctrl-C or SIGTERM finishes in-flight downloads, then exits. `--max-minutes` sets a time budget. `--min-free-gb` (default 20) stops the run before the disk fills.
- **One run at a time:** a lock file in the data dir enforces this. `cva status` doesn't take the lock.

## Parser interface

A parser is a function `parse(pdf_path: Path, gazette: dict) -> dict` that returns JSON-serializable output. `gazette` is the gazette's row from the `gazettes` table.

```sh
uv run cva process --parser mypkg.parser:parse --version v1 --limit 20
```

- Results go to the `parses` table, one row per (gazette, parser version).
- A new `--version` re-runs over everything already downloaded.
- `--retry-errors` re-runs only the failures of that version.

## Development sample

Testing the parser against all 31k gazettes is slow. Test it against a stratified sample instead:

```sh
uv run cva sample-build          # ~130 gazettes into data/sample/
```

`sample-build` covers every (era, document category, chamber) cell, plus edge cases. Examples: leading-zero numbers, non-numeric numbers, duplicate numbers, files the archive lists but can't serve.

It writes these files to `data/sample/`:
- `manifest.json` / `manifest.csv`: why each gazette was picked, its page count, PDF producer, and text-layer quality (`ok`, `garbled` or `no_text`).
- `SUMMARY.md`: distribution tables.

`samples/manifest.json` is a committed copy. To download the same set on another machine:

```sh
uv run cva sample-fetch samples/manifest.json
```

## Attendance

After extraction, a second stage works out each legislator's time in office and who didn't vote:

```sh
uv run python -m cva.attendance votes.db   # reads data/cva.db read-only; --cva to change
```

It adds six tables to the votes database and rebuilds them on every run:
- `legislators`: the name and `photo_url` of every legislator the votes refer to, copied from the pipeline database. `photo_url` is a link to the image on Congreso Visible's server, for the frontend to use directly in an `<img>` tag. It is NULL when Congreso Visible has no photo.
- `legislator_terms`: those legislators' chambers, terms and parties, copied from the pipeline database.
- `text_record_legislators`: the legislator behind each name on a plenary vote read from the gazette text. Those names are printed as the gazette has them, so each is compared with the members sitting in either chamber on the vote's date, ignoring accents and small words like "de". A name is linked only if exactly one member fits: their words contain one another's and share at least two, or all but one word match and that one is a typo away (`how = 'close'`). About 99% of names are linked. These matches don't affect service windows or absences.
- `legislator_service`: each legislator's time in office per chamber and term, from their first recorded vote to their last. A replacement's window starts when they begin voting, and the window of the member they replaced ends at that member's last vote.
- `vote_absences`: for each verified plenary vote, the legislators whose window covers the vote's date but who aren't on its record. `in_session` marks those who voted on another checked vote in the same chamber that day: they were in session but skipped this one.
- `vote_attendance`: for each of those votes, the date used and where it came from, and how many legislators were eligible, voted, were absent, and were absent but in session.

Absences are only computed for votes read from scanned voting records with `verified = 1` and `is_committee = 0`. On those, every row is tied to a legislator and the names add up to the printed totals, so a misread row can't make a voter look absent. Committee votes are left out because committee membership isn't known. Senate votes and House votes taken from the text are left out because they can't be verified.

A vote with no session date takes the date of the other votes in its gazette, since a House plenary acta records a single session. If none of them has a date, the vote is skipped.

Limits:
- The records list only members who voted, so an absence means the member was either not there or in the chamber without voting.
- Nobody can be marked absent before their first recorded vote or after their last. Absences at the first and last sessions in a dataset are undercounted.

## Website

A read-only site for browsing votes and legislators, as a Python API (`cva.web`) and a React frontend in `web/`. It reads a votes database after the attendance stage has run on it. Committee votes are left out.

```sh
cd web && npm ci && npm run build && cd ..
uv run python -m cva.web votes.db --pdfs data/pdfs --static web/dist   # http://127.0.0.1:8000
```

- `--pdfs` points at the pipeline's PDF store. Each vote then links to its page in the gazette PDF.
- `/download-db` serves the votes database itself, gzipped (about 14 MB for 69 MB), committee votes included, so anyone can work with the data. The site links to it in its footer. The server writes the gzip copy next to the database on startup, when it's missing or older than the database.
- The API holds every vote's summary in memory. Restart it after the votes database is rebuilt.
- A vote with no session date takes the date of the other votes in its gazette, else the gazette's publication date. The site marks these dates.
- For frontend work, run the API as above and `npm run dev` in `web/`. Vite forwards `/api` and `/pdf` to port 8000.

### The House barcode

`#/barcode` shows every representative's ballot on every checked House roll call as one grid (`cva.barcode`). Rows are grouped by party, columns are votes, and each cell is one ballot. Readers can do the following:
- Choose which votes to show: a threshold for how close a vote was (the least share the losing side got, 0–50%, 15% by default), kinds of vote, a bill or topic, and a date range (drag across the grid to zoom).
- Color the cells by party A vs party B (any two parties), agreement with party A, the member's own party majority, yes/no, the winning side, or attendance.
- Order columns by date, by party A's vote or closest first.
- Group rows by party, or rank every member by agreement with party A on the votes shown.
- Pin members and show only chosen parties.

Every setting is kept in the URL, so any view can be shared as a link.

A question box lets readers ask about the grid in plain language (`cva.ask`). A language model looks up figures through one tool (`Grid.query`), so the numbers it quotes are computed from the data. It answers with text that links to votes and members, and a view to show. It needs an OpenAI-compatible endpoint:

```sh
export CVA_LLM_BASE_URL=https://gateway.example.com
export CVA_LLM_API_KEY=...            # keep it out of the repo; .env is gitignored
export CVA_LLM_MODEL=glm-5.3-flash-uncensored-fp8
uv run python -m cva.web votes.db --pdfs data/pdfs --static web/dist
```

- If any of the three variables is missing, the question box is hidden.
- Each visitor gets 15 questions per 10 minutes (`ASK_LIMIT` and `ASK_WINDOW` in `cva.web`), because each question is paid for. Behind a proxy, the limit keys on the first `X-Forwarded-For` address.
- The model must answer after calling tools. On the TokenFactory gateway, `glm-5.3-flash-uncensored-fp8` answers after its tool calls in about 3–8 s. Its `deepseek-v4.1-flash` model returns empty replies after tool calls.

## Deployment

`flake.nix` exports `nixosModules.default`:

```nix
services.cva-pipeline = {
  enable = true;
  user = "ben";
  dataDir = "/var/lib/cva";
};
```

This runs `cva daily` every night: votes, bills (up to 2000 bill details per run), legislators and then PDFs, with a 10-hour download budget. The first run downloads most of the archive, and later runs pick up the ~5–10 new gazettes a day.
