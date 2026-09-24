"""Client for the Congreso Visible (Universidad de los Andes) backend API.

The public site is a Next.js frontend over a Laravel API. None of these
endpoints are documented; they were found in the site's JS bundles and by
probing. Bills link to gazettes through their status timeline
(proyecto_ley_estado[].gaceta_texto, e.g. "1283/22, 1309/22").
"""

from __future__ import annotations

from cva.http import PoliteClient

BASE = "https://apicongresovisible.uniandes.edu.co"
# Relative image paths (e.g. persona_imagen) are served from here.
UPLOADS = f"{BASE}/uploads/"

VOTE_FILTERS = {
    "idFilter": -1,
    "corporacion": -1,
    "legislatura": -1,
    "cuatrienio": -1,
    "comision": -1,
    "tipoVotacion": -1,
    "search": "",
}

# -1 means "any". corporacion and cuatrienio have no "any" value: the
# listing only returns rows for one chamber and term at a time.
LEGISLATOR_FILTERS = {
    "idFilter": -1,
    "partido": -1,
    "gradoEstudio": -1,
    "genero": -1,
    "circunscripcion": -1,
    "grupoEdad": -1,
    "comision": -1,
    "departamento": -1,
    "profesion": -1,
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

    def chambers(self) -> list[dict]:
        """[{id, nombre}]; 1 = Cámara de Representantes, 2 = Senado de la República."""
        resp = self.http.get(
            f"{BASE}/api/utils/getComboCorporacion", headers={"Accept": "application/json"}
        )
        resp.raise_for_status()
        return resp.json()

    def terms(self) -> list[dict]:
        """Four-year terms (cuatrienios): [{id, nombre, fechaInicio, fechaFin}] with years."""
        resp = self.http.get(
            f"{BASE}/api/utils/getComboCuatrienio", headers={"Accept": "application/json"}
        )
        resp.raise_for_status()
        return resp.json()

    def iter_legislators(self, chamber_id: int, term_id: int, rows: int = 500):
        """Members of one chamber in one term, one row per person, with that
        term's party. Replacements who sat part of the term are included."""
        page = 1
        while True:
            resp = self.http.get(
                f"{BASE}/apicliente/congresistas",
                params={
                    **LEGISLATOR_FILTERS,
                    "corporacion": chamber_id,
                    "cuatrienio": term_id,
                    "page": page,
                    "rows": rows,
                },
            )
            resp.raise_for_status()
            batch = resp.json()
            yield from batch
            if len(batch) < rows:
                return
            page += 1
