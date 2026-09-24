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

## Linking votes to legislators

`extract_votes.py` records each voter's name as the gazette prints it. To link those names to `legislators.id`:

```sh
uv run cva sync-legislators                  # if data/cva.db has no legislators yet
uv run python -m cva.link votes.db           # fills vote_records.legislator_id
```

- The linker adds `legislator_id` and `legislator_match` (`exact`, `fuzzy`, `ambiguous` or `none`) to `vote_records`. Re-running it is safe.
- A name is compared with everyone seated on the vote's date. Chamber is used only to break ties, because Congreso Visible lists some terms under the wrong chamber.
- The rules for abbreviations, typos and missing middle names are in `cva.link`.
- `data/cva.db` is opened read-only and the pipeline lock isn't taken, so the linker can run during a download.

On the 116-gazette sample, 98% of records link (93% exact, 5% fuzzy), none are ambiguous, and 2% don't match. Most unmatched names belong to people Congreso Visible doesn't list.

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
