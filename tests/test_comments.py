import asyncio

import httpx
import pytest

from cva import comments


def test_links_drop_trailing_punctuation_and_unmatched_parens():
    body = (
        "See https://a.org/x. And (https://b.org/y), "
        "https://en.wikipedia.org/wiki/Foo_(bar) and https://a.org/x again"
    )
    assert comments.links(body) == [
        "https://a.org/x",
        "https://b.org/y",
        "https://en.wikipedia.org/wiki/Foo_(bar)",
    ]


def test_preview_prefers_open_graph_and_falls_back_to_the_title():
    html = """<html><head><title> Page
        title </title>
        <meta property="og:title" content="OG title">
        <meta name="description" content="Plain description">
        <meta property="og:image" content="/img/card.jpg">
        <meta property="og:site_name" content="El Diario">
        </head></html>"""
    assert comments.parse_preview(html, "https://news.example/a/b") == {
        "title": "OG title",
        "description": "Plain description",
        "site_name": "El Diario",
        "image": "https://news.example/img/card.jpg",
    }
    bare = comments.parse_preview("<title>Only &amp; this</title>", "https://x.org/")
    assert bare == {"title": "Only & this", "description": None, "site_name": None, "image": None}


@pytest.mark.parametrize("url", ["http://127.0.0.1:8765/api/stats", "http://localhost/"])
def test_previews_are_not_fetched_from_this_machine(url):
    request = httpx.Request("GET", url)
    with pytest.raises(httpx.ConnectError, match="public"):
        asyncio.run(comments.public_only(request))
