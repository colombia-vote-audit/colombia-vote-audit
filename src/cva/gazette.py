"""Gazette (Gaceta del Congreso) identifiers.

Gazettes are numbered per calendar year in a single sequence shared by the
Senate and the Cámara, so (year, number) identifies one in almost every case.
Congreso Visible writes references as "number/yy", comma separated.
"""

from __future__ import annotations

import re

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
