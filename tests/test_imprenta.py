import re
import threading
from pathlib import Path

import httpx
import pytest

from cva import db
from cva.http import PoliteClient
from cva.jobs import gazettes
from cva.sources.imprenta import ImprentaSession, parse_rows
from cva.store import BlobStore

FIXTURE = (Path(__file__).parent / "fixtures" / "imprenta_page.xml").read_bytes()
LANDING = b'<form><input type="hidden" name="javax.faces.ViewState" value="000:111" /></form>'


def test_parse_rows_decodes_latin1_and_dates():
    rows = parse_rows(FIXTURE.decode("latin-1"))
    assert len(rows) == 5
    first = rows[0]
    assert first.chamber in ("Cámara de Representantes", "Senado de la República")
    assert first.date.startswith("2026-")
    acta = next(r for r in rows if r.date == "2011-12-29")
    assert acta.doc_type == "Acta de Plenaria"
    assert acta.downloadable
    assert not rows[-1].downloadable


def pdf(ri: int) -> bytes:
    return b"%PDF-1.4 row " + str(ri).encode() + b"\n%%EOF\n"


class FakeImprenta:
    """Serves the fixture rows as the archive listing, and a PDF per row.

    `offset` shifts every row's position, like new gazettes being added at
    the top of the real listing.
    """

    def __init__(self, empty_rows=(), truncated_rows=()):
        self.empty_rows = set(empty_rows)
        self.truncated_rows = set(truncated_rows)
        self.offset = 0
        self.downloads = 0
        self.lock = threading.Lock()

    def page(self, first: int, rows: int) -> bytes:
        text = FIXTURE.decode("latin-1")
        head, _, rest = text.partition("<tr")
        body = "<tr" + rest[: rest.rindex("</tr>") + 5]
        tail = rest[rest.rindex("</tr>") + 5 :]
        kept = []
        for tr in re.findall(r'<tr[^>]*data-ri="\d+".*?</tr>', body, re.S):
            ri = int(re.search(r'data-ri="(\d+)"', tr).group(1)) + self.offset
            if first <= ri < first + rows:
                tr = re.sub(r'data-ri="\d+"', f'data-ri="{ri}"', tr)
                kept.append(re.sub(r":\d+:btnDescargarPdf", f":{ri}:btnDescargarPdf", tr))
        return (head + "".join(kept) + tail).encode("latin-1")

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, content=LANDING)
        form = dict(httpx.QueryParams(request.content.decode()))
        if form.get("javax.faces.partial.ajax"):
            first = int(form["formResumen:dataTableResumen_first"])
            rows = int(form["formResumen:dataTableResumen_rows"])
            return httpx.Response(200, content=self.page(first, rows))
        button = next(k for k in form if k.endswith(":btnDescargarPdf"))
        ri = int(button.split(":")[2]) - self.offset
        with self.lock:
            self.downloads += 1
        body = b"" if ri in self.empty_rows else pdf(ri)
        if ri in self.truncated_rows:
            body = body[:-8]
        return httpx.Response(200, content=body, headers={"Content-Type": "application/pdf"})


@pytest.fixture
def env(tmp_path):
    conn = db.connect(":memory:")
    return conn, BlobStore(tmp_path / "pdfs")


def session_factory(fake):
    def make():
        http = PoliteClient(
            min_interval=0, transport=httpx.MockTransport(fake), sleep=lambda s: None
        )
        return ImprentaSession(http)

    return make


def status_counts(conn):
    return dict(conn.execute("SELECT fetch_status, count(*) FROM gazettes GROUP BY 1").fetchall())


def test_fetch_downloads_validates_and_records(env):
    conn, store = env
    fake = FakeImprenta(empty_rows={1}, truncated_rows={2})
    make = session_factory(fake)

    stats = gazettes.fetch(
        conn, make, store, gazettes.FetchSelection(), gazettes.FetchOptions(workers=2)
    )
    assert stats["ok"] == 2
    assert stats["missing"] == 1
    assert stats["error"] == 1
    assert status_counts(conn) == {"ok": 2, "missing": 1, "error": 1, "unavailable": 1}
    err = conn.execute("SELECT last_error FROM gazettes WHERE fetch_status = 'error'").fetchone()
    assert "truncated" in err[0]
    for g in conn.execute("SELECT * FROM gazettes WHERE fetch_status = 'ok'"):
        data = store.path_for(g["sha256"]).read_bytes()
        assert data.startswith(b"%PDF") and data.rstrip().endswith(b"%%EOF")
        assert len(data) == g["size"]


def test_errors_are_retried_then_given_up(env):
    conn, store = env
    fake = FakeImprenta(truncated_rows={2})
    make = session_factory(fake)
    for _ in range(gazettes.MAX_ATTEMPTS + 2):
        gazettes.fetch(conn, make, store, gazettes.FetchSelection())
    g = conn.execute("SELECT * FROM gazettes WHERE last_error LIKE 'truncated%'").fetchone()
    assert g["fetch_attempts"] == gazettes.MAX_ATTEMPTS


def test_nothing_left_means_no_downloads(env):
    conn, store = env
    fake = FakeImprenta()
    make = session_factory(fake)
    gazettes.fetch(conn, make, store, gazettes.FetchSelection())
    before = fake.downloads
    assert gazettes.fetch(conn, make, store, gazettes.FetchSelection()) == {}
    assert fake.downloads == before


def test_rows_that_moved_are_refetched_after_reindex(env):
    conn, store = env
    fake = FakeImprenta()
    make = session_factory(fake)
    gazettes.sync_index(conn, make())
    # New gazettes were published since the index was read: every row has
    # moved down a page.
    fake.offset = 60
    stats = gazettes.fetch(conn, make, store, gazettes.FetchSelection(), indexed=True)
    assert stats["ok"] == 4
    assert status_counts(conn) == {"ok": 4, "unavailable": 1}
    # Moving isn't the gazette's fault, so it doesn't use up attempts.
    assert conn.execute("SELECT max(fetch_attempts) FROM gazettes").fetchone()[0] == 1


def test_stop_event_prevents_new_work(env):
    conn, store = env
    fake = FakeImprenta()
    opts = gazettes.FetchOptions()
    opts.stop.set()
    stats = gazettes.fetch(conn, session_factory(fake), store, gazettes.FetchSelection(), opts)
    assert stats == {"stopped_early": 1}
    assert fake.downloads == 0


def test_low_disk_space_stops_fetch(env):
    conn, store = env
    fake = FakeImprenta()
    opts = gazettes.FetchOptions(min_free_bytes=10**18)
    stats = gazettes.fetch(conn, session_factory(fake), store, gazettes.FetchSelection(), opts)
    assert stats["stopped_early"] == 1
    assert fake.downloads == 0


def test_resync_is_idempotent(env):
    conn, _ = env
    make = session_factory(FakeImprenta())
    gazettes.sync_index(conn, make())
    stats = gazettes.sync_index(conn, make())
    assert stats["new"] == 0
    assert conn.execute("SELECT count(*) FROM gazettes").fetchone()[0] == 5


def test_validate_pdf():
    good = b"%PDF-1.7\n...\n%%EOF\n"
    assert gazettes.validate_pdf(good, len(good)) is None
    assert gazettes.validate_pdf(good, None) is None
    assert "truncated" in gazettes.validate_pdf(good, len(good) + 10)
    assert "truncated" in gazettes.validate_pdf(good[:-7], None)
    assert "not a PDF" in gazettes.validate_pdf(b"<html>", None)


def test_blob_store_concurrent_puts(tmp_path):
    store = BlobStore(tmp_path)
    data = [pdf(i) for i in range(20)] * 5
    threads = [threading.Thread(target=store.put, args=(d,)) for d in data]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    files = list(tmp_path.rglob("*"))
    assert len([f for f in files if f.suffix == ".pdf"]) == 20
    assert not [f for f in files if f.suffix == ".tmp"]


class FlakyImprenta(FakeImprenta):
    """Fails the download of one row every time, after others succeed."""

    def __init__(self, bad_row):
        super().__init__()
        self.bad_row = bad_row

    def __call__(self, request):
        form = dict(httpx.QueryParams(request.content.decode())) if request.method == "POST" else {}
        if any(k.endswith(f":{self.bad_row}:btnDescargarPdf") for k in form):
            return httpx.Response(
                200, content=b"<html>error</html>", headers={"Content-Type": "text/html"}
            )
        return super().__call__(request)


def test_page_failure_keeps_earlier_successes(env):
    conn, store = env
    # Rows 0-3 are downloadable; row 2 always fails mid-page.
    fake = FlakyImprenta(bad_row=2)
    stats = gazettes.fetch(conn, session_factory(fake), store, gazettes.FetchSelection())
    assert stats["ok"] == 3
    assert stats["error"] == 1
    bad = conn.execute("SELECT * FROM gazettes WHERE fetch_status = 'error'").fetchone()
    assert bad["row_index"] == 2
