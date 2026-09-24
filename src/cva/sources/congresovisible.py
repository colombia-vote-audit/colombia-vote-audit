"""Client for the Congreso Visible (Universidad de los Andes) backend API.

The public site is a Next.js frontend over a Laravel API. None of these
endpoints are documented; they were found in the site's JS bundles and by
probing. Bills link to gazettes through their status timeline
(proyecto_ley_estado[].gaceta_texto, e.g. "1283/22, 1309/22").
"""

from __future__ import annotations

from cva.http import PoliteClient

BASE = "https://apicongresovisible.uniandes.edu.co"

VOTE_FILTERS = {
    "idFilter": -1,
    "corporacion": -1,
    "legislatura": -1,
    "cuatrienio": -1,
    "comision": -1,
    "tipoVotacion": -1,
    "search": "",
}


class CongresoVisible:
    def __init__(self, http: PoliteClient):
        self.http = http

    def bill_index(self) -> list[dict]:
        """Every bill (id, title, filing date, bill numbers) in one call."""
        resp = self.http.post(
            f"{BASE}/api/utils/getProyectoLeyFilter",
            json={"search": "", "page": 1, "rows": 100_000},
            headers={"Accept": "application/json"},
        )
        resp.raise_for_status()
        return resp.json()

    def bill_detail(self, bill_id: int) -> dict | None:
        resp = self.http.get(f"{BASE}/apicliente/proyectoley/{bill_id}")
        resp.raise_for_status()
        data = resp.json()
        # Unknown ids come back as an empty body/list rather than a 404.
        return data if isinstance(data, dict) and data.get("id") else None

    def bill_states(self) -> list[dict]:
        resp = self.http.get(
            f"{BASE}/api/utils/getComboEstadoProyecto", headers={"Accept": "application/json"}
        )
        resp.raise_for_status()
        return resp.json()

    def iter_votes(self, rows: int = 500):
        page = 1
        while True:
            resp = self.http.get(
                f"{BASE}/apicliente/actividadeslegislativas/getVotaciones",
                params={**VOTE_FILTERS, "page": page, "rows": rows},
            )
            resp.raise_for_status()
            batch = resp.json()
            yield from batch
            if len(batch) < rows:
                return
            page += 1
