"""Fetch a link and turn it into plain text the agent can read (HTML pages, Confluence, text)."""

from __future__ import annotations

import re
import time
from html.parser import HTMLParser
from typing import Dict, Tuple
from urllib.parse import parse_qs, urlparse

import httpx

from backend.models import LinkSettings

_CACHE: Dict[str, Tuple[float, str]] = {}
_TTL = 600
_MAX_BYTES = 3_000_000


class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "head", "nav", "footer"}
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section",
             "article", "pre", "table", "ul", "ol"}

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.skip = 0
        self.title = ""
        self._in_title = False
        self._lists: list[list] = []  # stack of [tag, counter]

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._in_title = True
        if tag in ("ul", "ol"):
            self._lists.append([tag, 0])
        if tag in self.SKIP:
            self.skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")
        if tag == "li":
            indent = "  " * max(len(self._lists) - 1, 0)
            if self._lists and self._lists[-1][0] == "ol":
                self._lists[-1][1] += 1
                self.parts.append(f"{indent}{self._lists[-1][1]}. ")
            else:
                self.parts.append(f"{indent}- ")
        if tag in ("h1", "h2", "h3"):
            self.parts.append("#" * int(tag[1]) + " ")

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag in ("ul", "ol") and self._lists:
            self._lists.pop()
        if tag in self.SKIP and self.skip:
            self.skip -= 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if not self.skip:
            self.parts.append(data)


class _LinkExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            a = dict(attrs)
            href = a.get("href") or ""
            person = "userlink" in (a.get("class") or "") or "user-mention" in (a.get("class") or "") \
                or "/display/~" in href or "viewuserprofile" in href
            self._href = None if person else href  # people mentions are not documents to read
            self._text = []

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href:
            self.links.append((" ".join("".join(self._text).split()), self._href.strip()))
            self._href = None


def extract_links(html: str) -> list[tuple[str, str]]:
    """(text, url) of every <a href> in step content (tiptap HTML)."""
    p = _LinkExtractor()
    p.feed(html or "")
    return p.links


def html_to_text(html: str) -> str:
    p = _TextExtractor()
    p.feed(html)
    text = "".join(p.parts)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"(^|\n)( *(?:-|\d+\.)) *\n\s*", r"\1\2 ", text)  # <li><p>…</p></li>
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    text = text.strip()
    if p.title.strip() and not text.startswith(p.title.strip()):
        text = f"# {p.title.strip()}\n\n{text}"
    return text


def _confluence_page_id(url: str) -> str | None:
    u = urlparse(url)
    q = parse_qs(u.query)
    if "pageId" in q:
        return q["pageId"][0]
    m = re.search(r"/pages/(\d+)", u.path)
    return m.group(1) if m else None


def _auth_for(url: str, cfg: LinkSettings) -> dict:
    base = cfg.confluence_base_url.rstrip("/")
    if not base or not cfg.confluence_token or not url.startswith(base):
        return {}
    if cfg.confluence_email:
        return {"auth": (cfg.confluence_email, cfg.confluence_token)}
    return {"headers": {"Authorization": f"Bearer {cfg.confluence_token}"}}


def fetch_text(url: str, cfg: LinkSettings) -> str:
    """Return the readable text of a link. Raises on network/HTTP errors."""
    hit = _CACHE.get(url)
    if hit and time.time() - hit[0] < _TTL:
        return hit[1]
    auth = _auth_for(url, cfg)
    headers = {"User-Agent": "TrackPlatform/2.0", **auth.pop("headers", {})}
    with httpx.Client(timeout=cfg.timeout_s, follow_redirects=True, headers=headers,
                      **auth) as client:
        text = None
        page_id = _confluence_page_id(url) if auth or "confluence" in url or "/wiki/" in url else None
        if page_id and cfg.confluence_base_url:
            api = f"{cfg.confluence_base_url.rstrip('/')}/rest/api/content/{page_id}"
            r = client.get(api, params={"expand": "body.storage"})
            if r.status_code == 200:
                data = r.json()
                body = data.get("body", {}).get("storage", {}).get("value", "")
                text = f"# {data.get('title', '')}\n\n{html_to_text(body)}"
        if text is None:
            r = client.get(url)
            r.raise_for_status()
            raw = r.content[:_MAX_BYTES].decode(r.encoding or "utf-8", errors="replace")
            ctype = r.headers.get("content-type", "")
            if "html" in ctype or raw.lstrip()[:15].lower().startswith(("<!doctype", "<html")):
                text = html_to_text(raw)
            elif ctype.startswith(("text/", "application/json", "application/xml")) or not ctype:
                text = raw.strip()
            else:
                text = f"[документ типа {ctype} не может быть прочитан как текст]"
    _CACHE[url] = (time.time(), text)
    return text


def clear_cache() -> None:
    _CACHE.clear()
