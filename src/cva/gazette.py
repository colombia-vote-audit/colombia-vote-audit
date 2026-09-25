"""Gazette (Gaceta del Congreso) identifiers.

Gazettes are numbered per calendar year in a single sequence shared by the
Senate and the Cámara, so (year, number) identifies one in almost every case.
Congreso Visible writes references as "number/yy", comma separated.
"""

from __future__ import annotations

import re

MONTHS = {
    m: i + 1
    for i, m in enumerate(
        "enero febrero marzo abril mayo junio julio agosto septiembre octubre noviembre"
        " diciembre".split()
    )
}
MONTHS["setiembre"] = 9

_REF = re.compile(r"(\d{1,4}[A-Za-z]?)\s*/\s*(\d{2}(?:\d{2})?)\b")


def expand_year(yy: str) -> int:
    y = int(yy)
    if len(yy) == 4:
        return y
    return 1900 + y if y >= 90 else 2000 + y


def normalize_number(number: str) -> str:
    """'0012' -> '12'. Keeps suffixes like '242B'."""
    stripped = number.strip().lstrip("0")
    return stripped.upper() or "0"


def parse_refs(text: str | None) -> list[tuple[int, str]]:
    """Parse '1283/22, 1309/22' -> [(2022, '1283'), (2022, '1309')]."""
    if not text:
        return []
    seen = []
    for number, yy in _REF.findall(text):
        ref = (expand_year(yy), normalize_number(number))
        if ref not in seen:
            seen.append(ref)
    return seen


def publication_date(s: str | None) -> str | None:
    """A gazette's printed date as ISO: '25 de septiembre de 2025' -> '2025-09-25'."""
    m = re.fullmatch(r"(\d{1,2})\s+de\s+(\w+)\s+de\s+(\d{4})", (s or "").strip(), re.I)
    if not m or m[2].lower() not in MONTHS:
        return None
    return f"{m[3]}-{MONTHS[m[2].lower()]:02d}-{int(m[1]):02d}"
