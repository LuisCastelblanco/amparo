"""Fetch Constitutional Court ruling pages and restore accents lost in Tavily excerpts.

The relatoria serves its HTML as windows-1252 (declared only in a <meta> tag) and Tavily's
extracted content drops every non-ASCII character ("acción" -> "accin"). Tavily stays the
search engine; for the rulings we keep, we download the page, decode it with its real charset
and use its words to repair the excerpt.
"""

import asyncio
import logging
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from html.parser import HTMLParser

import httpx

from app import citations

logger = logging.getLogger("amparo.sources")

FETCH_TIMEOUT_S = 10.0
MAX_PAGE_BYTES = 4_000_000
_META_CHARSET_RE = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?([\w.:-]+)""", re.IGNORECASE)
# Browsers decode latin-1 labels as windows-1252 (a superset); do the same.
_CHARSET_ALIASES = {"iso-8859-1": "windows-1252", "latin-1": "windows-1252", "latin1": "windows-1252",
                    "us-ascii": "windows-1252", "ascii": "windows-1252"}
_ASCII_WORD_RE = re.compile(r"[A-Za-z0-9]+")
_WORD_RE = re.compile(r"\w+")


@dataclass
class RulingPage:
    url: str
    charset: str
    text: str
    cited: list[str] = field(default_factory=list)  # rulings cited inside the full text


class _TextExtractor(HTMLParser):
    _SKIP = {"script", "style", "head"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in self._SKIP:
            self._skip += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP and self._skip:
            self._skip -= 1
        elif tag in ("p", "div", "br", "li", "tr", "h1", "h2", "h3"):
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def detect_charset(content_type: str | None, body: bytes) -> str:
    """Charset from the Content-Type header, else the <meta> tag, else UTF-8 if valid, else windows-1252."""
    candidates = []
    if content_type and "charset=" in content_type.lower():
        candidates.append(content_type.lower().split("charset=", 1)[1].split(";")[0].strip(" \"'"))
    meta = _META_CHARSET_RE.search(body[:5000])
    if meta:
        candidates.append(meta.group(1).decode("ascii", "ignore").lower())
    for name in candidates:
        name = _CHARSET_ALIASES.get(name, name)
        try:
            "".encode(name)
            return name
        except LookupError:
            continue
    try:
        body.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        return "windows-1252"


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    text = "".join(parser.parts).replace("\xa0", " ")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


async def fetch_ruling(client: httpx.AsyncClient, url: str) -> RulingPage | None:
    try:
        response = await client.get(url)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning("ruling fetch failed url=%s error=%r", url, exc)
        return None
    body = response.content[:MAX_PAGE_BYTES]
    charset = detect_charset(response.headers.get("content-type"), body)
    raw = body.decode(charset, errors="replace")
    text = html_to_text(raw) if "<" in raw[:2000] else raw
    return RulingPage(url=url, charset=charset, text=text, cited=citations.find_citations(text))


async def fetch_rulings(urls: list[str]) -> dict[str, RulingPage]:
    """Download several ruling pages in parallel; failures are skipped."""
    async with httpx.AsyncClient(
        follow_redirects=True, timeout=FETCH_TIMEOUT_S, headers={"User-Agent": "Amparo/0.1 (+github.com/LuisCastelblanco/amparo)"}
    ) as client:
        pages = await asyncio.gather(
            *(asyncio.wait_for(fetch_ruling(client, u), FETCH_TIMEOUT_S * 2) for u in urls),
            return_exceptions=True,
        )
    return {u: p for u, p in zip(urls, pages) if isinstance(p, RulingPage)}


def _strip_non_ascii(word: str) -> str:
    return "".join(c for c in word if ord(c) < 128)


def restore_accents(excerpt: str, page_text: str) -> tuple[str, int]:
    """Repair words whose non-ASCII letters were dropped, using the words of the original page.

    "accin" -> "acción" when the page contains "acción" and "accin" is not itself a word there.
    Returns the repaired text and the number of words changed.
    """
    exact: dict[str, Counter] = defaultdict(Counter)
    lower: dict[str, Counter] = defaultdict(Counter)
    ascii_words: set[str] = set()
    for word in _WORD_RE.findall(page_text):
        if word.isascii():
            ascii_words.add(word.lower())
            continue
        key = _strip_non_ascii(word)
        if len(key) >= 1:
            exact[key][word] += 1
            lower[key.lower()][word.lower()] += 1

    changed = 0

    def fix(m: re.Match) -> str:
        nonlocal changed
        token = m[0]
        if token.lower() in ascii_words:
            return token
        if token in exact:
            changed += 1
            return exact[token].most_common(1)[0][0]
        if token.lower() in lower:
            repaired = lower[token.lower()].most_common(1)[0][0]
            changed += 1
            if token.isupper():
                return repaired.upper()
            if token[0].isupper():
                return repaired[0].upper() + repaired[1:]
            return repaired
        return token

    return _ASCII_WORD_RE.sub(fix, excerpt), changed
