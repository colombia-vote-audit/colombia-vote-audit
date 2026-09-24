"""Client for the Imprenta Nacional Gaceta del Congreso archive.

The archive is a JSF/PrimeFaces app with no API. Everything happens inside
one server-side session: the listing is a paginated data table, and a PDF is
downloaded by "clicking" the download button of a row that is on the page
the session currently has loaded. So downloads go: load a page of rows, then
download rows from that page.

Responses are Latin-1, not the UTF-8 the headers claim.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass

from cva.http import PoliteClient

BASE = "https://svrpubindc.imprenta.gov.co/senado/index.xhtml"
TABLE = "formResumen:dataTableResumen"
ENCODING = "latin-1"

_VIEWSTATE_PAGE = re.compile(r'name="javax\.faces\.ViewState"[^>]*value="([^"]+)"')
_VIEWSTATE_PARTIAL = re.compile(r"ViewState[^>]*><!\[CDATA\[([^\]]+)\]\]")
_ROW = re.compile(r'<tr[^>]*data-ri="(\d+)"[^>]*>(.*?)</tr>', re.S)
_CELL = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
_TAG = re.compile(r"<[^>]+>")
_DATE = re.compile(r"(\d{2})/(\d{2})/(\d{4})")


class ImprentaError(Exception):
    pass


@dataclass(frozen=True)
class GazetteRow:
    """One row of the archive listing.

    The archive has no stable id. (number, chamber, date) is unique in
    practice; (year, number) is not, because of data-entry errors.
    """

    row_index: int
    number: str
    chamber: str
    date: str  # ISO yyyy-mm-dd; year can be garbage like 0012 upstream
    doc_type: str
    downloadable: bool

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.number, self.chamber, self.date)

    @property
    def year(self) -> int:
        return int(self.date[:4])


def parse_rows(markup: str) -> list[GazetteRow]:
    rows = []
    for ri, tr in _ROW.findall(markup):
        cells = [html.unescape(_TAG.sub("", c)).strip() for c in _CELL.findall(tr)]
        if len(cells) < 4:
            continue
        m = _DATE.fullmatch(cells[2])
        if not m:
            continue
        dd, mm, yyyy = m.groups()
        rows.append(
            GazetteRow(
                row_index=int(ri),
                number=cells[0],
                chamber=cells[1],
                date=f"{yyyy}-{mm}-{dd}",
                doc_type=" ".join(cells[3].split()),
                downloadable="btnDescargarPdf" in tr,
            )
        )
    return rows


class ImprentaSession:
    def __init__(self, http: PoliteClient):
        self.http = http
        self.viewstate: str | None = None
        self.page_first: int | None = None

    def open(self):
        self.http.client.cookies.clear()
        resp = self.http.get(BASE)
        resp.raise_for_status()
        m = _VIEWSTATE_PAGE.search(resp.content.decode(ENCODING))
        if not m:
            raise ImprentaError("no ViewState on landing page")
        self.viewstate = m.group(1)
        self.page_first = None

    def _post(self, data: dict, ajax: bool = False):
        if self.viewstate is None:
            self.open()
        data = {**data, "formResumen": "formResumen", "javax.faces.ViewState": self.viewstate}
        headers = (
            {"Faces-Request": "partial/ajax", "X-Requested-With": "XMLHttpRequest"} if ajax else {}
        )
        return self.http.post(BASE, data=data, headers=headers)

    def load_page(self, first: int, rows: int) -> list[GazetteRow]:
        """Make rows [first, first+rows) the session's current page."""
        resp = self._post(
            {
                "javax.faces.partial.ajax": "true",
                "javax.faces.source": TABLE,
                "javax.faces.partial.execute": TABLE,
                "javax.faces.partial.render": TABLE,
                TABLE: TABLE,
                f"{TABLE}_pagination": "true",
                f"{TABLE}_first": str(first),
                f"{TABLE}_rows": str(rows),
                f"{TABLE}_encodeFeature": "true",
            },
            ajax=True,
        )
        resp.raise_for_status()
        body = resp.content.decode(ENCODING)
        if "ViewExpiredException" in body:
            raise ImprentaError("session expired")
        m = _VIEWSTATE_PARTIAL.search(body)
        if m:
            self.viewstate = m.group(1)
        self.page_first = first
        return parse_rows(body)

    def iter_index(self, chunk: int = 5000):
        """Yield every row in the archive, newest first."""
        first = 0
        while True:
            rows = self.load_page(first, chunk)
            yield from rows
            if len(rows) < chunk:
                return
            first += chunk

    def download(self, row: GazetteRow) -> tuple[bytes | None, int | None]:
        """Download a row on the currently loaded page.

        Returns (body, Content-Length). The body is None when the archive
        lists the gazette but has no file (it answers with an empty PDF).
        """
        resp = self._post({f"{TABLE}:{row.row_index}:btnDescargarPdf": ""})
        resp.raise_for_status()
        ctype = resp.headers.get("Content-Type", "")
        if not ctype.startswith("application/pdf"):
            raise ImprentaError(f"expected PDF for row {row.row_index}, got {ctype}")
        length = resp.headers.get("Content-Length")
        return resp.content or None, int(length) if length and length.isdigit() else None
