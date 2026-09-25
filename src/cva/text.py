import re
import unicodedata


def fold(s: str | None) -> str:
    """Lowercase without accents, for search."""
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()
