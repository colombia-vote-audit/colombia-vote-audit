"""Comments on legislators' pages: notes and links posted by organizations.

Anyone can post; each comment names the organization or group it's from.
Comments live in the votes database itself, in three tables this module adds
(so replacing the database loses them):

    comments        one row per comment, tied to legislators.id
    link_previews   title, description, site and image of each linked page,
                    fetched once when a comment first links it
    comment_files   files attached to a comment

A comment's links are the URLs in its text; the first MAX_LINKS get previews.
Preview images are the page's og:image, linked, not copied.

Attached files are kept outside the database, in a BlobStore under their
sha256. Only images and PDFs, recognized by their first bytes rather than
what the uploader claims, are shown in the browser (INLINE); anything else
is served as a download, so an uploaded page or script never runs as part
of the site.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import sqlite3
import unicodedata
from datetime import UTC, datetime
from html.parser import HTMLParser
from urllib.parse import quote, urljoin, urlsplit

import httpx

SCHEMA = """
CREATE TABLE IF NOT EXISTS comments (
    id INTEGER PRIMARY KEY,
    legislator_id INTEGER NOT NULL,
    organization TEXT NOT NULL,
    author TEXT,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS comments_legislator ON comments (legislator_id, id);
CREATE TABLE IF NOT EXISTS link_previews (
    url TEXT PRIMARY KEY,
    title TEXT,
    description TEXT,
    site_name TEXT,
    image TEXT,
    fetched_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS comment_files (
    comment_id INTEGER NOT NULL REFERENCES comments(id),
    position INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    name TEXT NOT NULL,
    content_type TEXT NOT NULL,
    size INTEGER NOT NULL,
    PRIMARY KEY (comment_id, position)
);
CREATE INDEX IF NOT EXISTS comment_files_sha256 ON comment_files (sha256);
"""

MAX_BODY = 4000
MAX_NAME = 120
MAX_LINKS = 3
MAX_PAGE_BYTES = 2 * 1024 * 1024  # YouTube puts its og: tags 700 KB in
TIMEOUT = 6
MAX_FILES = 5
MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_UPLOAD_BYTES = 90 * 1024 * 1024  # Cloudflare refuses request bodies over 100 MB

# Types shown in the browser, by their first bytes.
INLINE = {
    b"\x89PNG\r\n\x1a\n": "image/png",
    b"\xff\xd8\xff": "image/jpeg",
    b"GIF87a": "image/gif",
    b"GIF89a": "image/gif",
    b"%PDF-": "application/pdf",
}
INLINE_TYPES = {*INLINE.values(), "image/webp"}

URL = re.compile(r"https?://[^\s<>\"']+")


def setup(path) -> None:
    """Adds the comment tables if they aren't there; writes nothing otherwise."""
    conn = sqlite3.connect(path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


def links(body: str) -> list[str]:
    """The URLs in a comment, in order, without trailing punctuation or an
    unmatched closing parenthesis (as in "(see https://x.org/a)")."""
    out = []
    for m in URL.finditer(body):
        url = m.group().rstrip(".,;:!?")
        while url.endswith(")") and url.count(")") > url.count("("):
            url = url[:-1].rstrip(".,;:!?")
        if url not in out:
            out.append(url)
    return out


def sniff(data: bytes) -> str | None:
    """The type of an image or PDF the browser may show, else None."""
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return next((kind for magic, kind in INLINE.items() if data.startswith(magic)), None)


def file_name(name: str | None) -> str:
    """The uploader's filename without any path, control characters or excess length."""
    name = re.split(r"[/\\]", name or "")[-1]
    name = "".join(c for c in name if unicodedata.category(c)[0] != "C").strip(" .")
    if len(name) > 150:
        stem, dot, ext = name.rpartition(".")
        name = f"{stem[: 140 - len(ext)]}.{ext}" if dot and len(ext) <= 10 else name[:150]
    return name or "file"


def attach(conn: sqlite3.Connection, comment_id: int, files, shas: list[str]) -> None:
    """Ties (name, declared type, bytes) files, already in the store under
    `shas`, to a comment."""
    rows = []
    for position, ((name, declared, data), sha) in enumerate(zip(files, shas, strict=True)):
        # A claimed image or PDF that isn't one is stored as neither.
        kind = sniff(data) or (declared or "").split(";")[0].strip().lower()
        if kind in INLINE_TYPES and kind != sniff(data):
            kind = ""
        rows.append(
            (comment_id, position, sha, file_name(name),
             kind or "application/octet-stream", len(data))
        )  # fmt: skip
    conn.executemany("INSERT INTO comment_files VALUES (?, ?, ?, ?, ?, ?)", rows)


def file_url(sha256: str, name: str) -> str:
    return f"/files/{sha256}/{quote(name)}"


def now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class Meta(HTMLParser):
    """Collects <title> and the <meta> tags a link preview is made from."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.title = ""
        self.in_title = False

    def handle_starttag(self, tag, attrs):
        a = {k: v for k, v in attrs if v is not None}
        if tag == "meta":
            key = (a.get("property") or a.get("name") or "").lower()
            if key and "content" in a and key not in self.meta:
                self.meta[key] = a["content"].strip()
        elif tag == "title":
            self.in_title = True

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False

    def handle_data(self, data):
        if self.in_title:
            self.title += data


def parse_preview(html: str, url: str) -> dict:
    p = Meta()
    p.feed(html)
    m = p.meta

    def first(*keys):
        for k in keys:
            if v := " ".join(m.get(k, "").split()):
                return v
        return None

    image = first("og:image", "og:image:url", "twitter:image", "twitter:image:src")
    image = urljoin(url, image) if image else None
    if image and urlsplit(image).scheme not in ("http", "https"):
        image = None
    return {
        "title": first("og:title", "twitter:title") or " ".join(p.title.split()) or None,
        "description": first("og:description", "twitter:description", "description"),
        "site_name": first("og:site_name"),
        "image": image,
    }


async def public_only(request: httpx.Request) -> None:
    """Refuses to fetch from this machine or its network, on every redirect hop."""
    host = request.url.host
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, request.url.port or 443)
    except OSError as e:
        raise httpx.ConnectError(f"can't resolve {host}") from e
    for info in infos:
        if not ipaddress.ip_address(info[4][0].split("%")[0]).is_global:
            raise httpx.ConnectError(f"{host} isn't a public address")


async def fetch_preview(url: str) -> dict:
    """A preview of one page; empty fields where it can't be read."""
    empty = {"title": None, "description": None, "site_name": None, "image": None}
    try:
        async with httpx.AsyncClient(
            timeout=TIMEOUT,
            follow_redirects=True,
            max_redirects=5,
            event_hooks={"request": [public_only]},
            headers={
                # Some sites only give Open Graph tags to link-preview bots.
                "user-agent": "Mozilla/5.0 (compatible; cva-link-preview; facebookexternalhit/1.1)",
                "accept": "text/html,application/xhtml+xml,*/*;q=0.5",
            },
        ) as client:
            async with client.stream("GET", url) as res:
                if res.status_code >= 400:
                    return empty
                kind = res.headers.get("content-type", "")
                if kind.startswith("image/"):
                    return {**empty, "image": str(res.url)}
                if "html" not in kind:
                    return empty
                body = bytearray()
                async for chunk in res.aiter_bytes():
                    body += chunk
                    if len(body) >= MAX_PAGE_BYTES:
                        break
                text = body.decode(res.charset_encoding or "utf-8", errors="replace")
                return parse_preview(text, str(res.url))
    except (httpx.HTTPError, ValueError, UnicodeError, LookupError):
        return empty


async def previews_for(conn: sqlite3.Connection, urls: list[str], fetch) -> None:
    """Fetches and stores previews for the URLs that don't have one yet."""
    have = {
        r[0]
        for r in conn.execute(
            f"SELECT url FROM link_previews WHERE url IN ({','.join('?' * len(urls))})", urls
        )
    }
    todo = [u for u in urls if u not in have]
    got = await asyncio.gather(*(fetch(u) for u in todo))
    conn.executemany(
        "INSERT OR REPLACE INTO link_previews VALUES (?, ?, ?, ?, ?, ?)",
        [
            (u, p["title"], p["description"], p["site_name"], p["image"], now())
            for u, p in zip(todo, got, strict=True)
        ],
    )


def listing(conn: sqlite3.Connection, legislator_id: int) -> list[dict]:
    """A legislator's comments, newest first, with their links' previews."""
    rows = conn.execute(
        "SELECT id, organization, author, body, created_at FROM comments"
        " WHERE legislator_id = ? ORDER BY id DESC",
        (legislator_id,),
    ).fetchall()
    urls = {u for r in rows for u in links(r[3])[:MAX_LINKS]}
    previews = {}
    if urls:
        for url, title, description, site, image in conn.execute(
            "SELECT url, title, description, site_name, image FROM link_previews"
            f" WHERE url IN ({','.join('?' * len(urls))})",
            list(urls),
        ):
            if title or image:
                previews[url] = {
                    "url": url,
                    "title": title,
                    "description": description,
                    "site_name": site,
                    "image": image,
                }
    files = {}
    for cid, sha, name, kind, size in conn.execute(
        "SELECT f.comment_id, f.sha256, f.name, f.content_type, f.size FROM comment_files f"
        " JOIN comments c ON c.id = f.comment_id WHERE c.legislator_id = ?"
        " ORDER BY f.comment_id, f.position",
        (legislator_id,),
    ):
        files.setdefault(cid, []).append(
            {"name": name, "url": file_url(sha, name), "content_type": kind, "size": size}
        )
    return [
        {
            "id": cid,
            "organization": org,
            "author": author,
            "body": body,
            "created_at": created,
            "previews": [previews[u] for u in links(body)[:MAX_LINKS] if u in previews],
            "files": files.get(cid, []),
        }
        for cid, org, author, body, created in rows
    ]
