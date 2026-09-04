"""Fetching a page so its price can be read off it.

Deliberately small and deliberately dull: an HTTP GET, a wait, and enough HTML
stripping that the reader sees the words rather than the markup. It does not
execute JavaScript and it does not log in. A supplier whose prices only appear
after a script runs, or behind a trade account, cannot be read this way — and
saying so plainly is the right outcome, because the alternative is a system
that reports a price it did not find.

Where this runs matters. The fetch goes out from wherever the app is hosted,
across the business's own connection, so what is reachable is whatever that
network can reach. A development sandbox with restricted egress will fail here
while the same code on the office network succeeds; the error says which, so
"blocked" is never mistaken for "the supplier has no price".
"""

from __future__ import annotations

import re
from html import unescape
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

#: Long enough for a slow stockholder, short enough that a refresh of a dozen
#: sources does not become a coffee break.
TIMEOUT_SECONDS = 25

#: Pages carry navigation, cookie banners and footers. Past this much text the
#: price is not going to be found anyway, and the reader gets slower and less
#: accurate the more chaff it wades through.
MAX_CHARS = 60_000

USER_AGENT = "EDMZoneQuoting/1.0 (+material price check; contact the sender of this request)"

_SCRIPT_OR_STYLE = re.compile(r"<(script|style|noscript|svg)[^>]*>.*?</\1>", re.S | re.I)
_TAG = re.compile(r"<[^>]+>")
_BLANK_LINES = re.compile(r"\n\s*\n+")
_SPACES = re.compile(r"[ \t ]+")


class FetchError(Exception):
    """The page could not be fetched. Always says why, in words."""


def fetch_text(url: str, *, timeout: int = TIMEOUT_SECONDS) -> str:
    """Fetch a URL and return its readable text.

    Raises `FetchError` with a plain-English reason rather than returning
    something empty that would read downstream as "the page had no price".
    """
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*"})
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - configured URLs only
            charset = response.headers.get_content_charset() or "utf-8"
            raw = response.read(4_000_000).decode(charset, errors="replace")
    except HTTPError as exc:
        if exc.code in (401, 403):
            raise FetchError(
                f"{url} refused the request ({exc.code}). This usually means the "
                "prices are behind a trade login, or the site blocks automated "
                "access. A source like that needs a supplier feed or a typed price."
            ) from exc
        if exc.code == 404:
            raise FetchError(
                f"{url} does not exist ({exc.code}). The page has probably moved."
            ) from exc
        raise FetchError(f"{url} returned HTTP {exc.code}.") from exc
    except URLError as exc:
        raise FetchError(
            f"Could not reach {url}: {exc.reason}. This is a network problem at "
            "this end, not a problem with the supplier — check whether outbound "
            "access to this site is allowed from wherever the app is running."
        ) from exc
    except TimeoutError as exc:
        raise FetchError(f"{url} did not respond within {timeout} seconds.") from exc

    text = to_text(raw)
    if not text.strip():
        raise FetchError(
            f"{url} returned a page with no readable text. Its prices are most "
            "likely drawn by JavaScript, which this cannot read."
        )
    return text[:MAX_CHARS]


def to_text(html: str) -> str:
    """Strip HTML down to the words, keeping the line structure a table gives."""
    text = _SCRIPT_OR_STYLE.sub(" ", html)
    text = re.sub(r"</(tr|p|div|li|h[1-6]|table)>", "\n", text, flags=re.I)
    text = re.sub(r"</t[dh]>", " | ", text, flags=re.I)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = _TAG.sub(" ", text)
    text = unescape(text)
    text = _SPACES.sub(" ", text)
    text = "\n".join(line.strip() for line in text.splitlines())
    return _BLANK_LINES.sub("\n\n", text).strip()
