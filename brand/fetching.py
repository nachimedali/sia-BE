"""Reading a website — the port, its real adapter and its fake (S1-01).

**The real adapter is a guest on someone else's server and on our network.**
It reads public pages only, says who it is, honours `robots.txt`, stops at a
size and a page budget, and — the part that matters most — refuses any address
that is not on the public internet. A URL the user types is untrusted input, and
a crawler that will fetch `http://169.254.169.254/` or `http://localhost:5432/`
because someone typed it is a server-side request forgery waiting to be found.
Every hop of every redirect is re-checked, because a public URL that redirects
to a private one is the usual way around a check made only once.

**The fake serves fixture sites from disk** (`brand/fixtures/sites/<domain>/`),
so a fresh checkout and the test suite read "websites" with no network and no
third-party account (Part 7 rule 6).
"""

from __future__ import annotations

import ipaddress
import mimetypes
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

from django.conf import settings

USER_AGENT = "ComposeVisionBot/1.0 (+https://composevision.app/bot; brand import)"
#: Per response. A product page is tens of kilobytes; anything near this is not
#: a page the import needs, and reading it would only cost memory.
MAX_BYTES = 2_000_000
MAX_REDIRECTS = 4
TIMEOUT_SECONDS = 8.0
ALLOWED_PORTS = frozenset({80, 443})
#: Product photos (S1 product import). Read on demand, never stored by the fetcher.
MAX_IMAGE_BYTES = 8_000_000
IMAGE_TYPES = ("image/jpeg", "image/png", "image/webp", "image/gif")
#: What a page read can use. Video and archives are never downloaded.
READABLE_TYPES = (
    "text/html",
    "application/json",
    "text/css",
    "text/plain",
    "application/xml",
    "text/xml",
)


@dataclass(frozen=True)
class FetchedPage:
    url: str
    status: int
    content_type: str
    text: str

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


@dataclass(frozen=True)
class FetchedImage:
    url: str
    content_type: str
    data: bytes


class UnsafeUrlError(ValueError):
    """The URL is not a public http(s) address this crawler may read."""


class SiteFetcher(Protocol):
    def fetch(self, url: str) -> FetchedPage | None:
        """The page, or `None` when it could not be read at all (unreachable,
        too large, not text, refused by robots.txt)."""
        ...

    def fetch_image(self, url: str) -> FetchedImage | None:
        """An image's bytes, or `None` for anything that is not a readable
        image within `MAX_IMAGE_BYTES` — same address guards as a page."""
        ...


def _is_public(host: str) -> bool:
    """Every address the name resolves to must be globally routable."""
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    if not infos:
        return False
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if not address.is_global or address.is_multicast:
            return False
    return True


def assert_public_url(url: str) -> None:
    """Raise `UnsafeUrlError` unless `url` is http(s), on 80/443, and public."""
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise UnsafeUrlError("Only http and https websites can be imported.")
    if parts.username or parts.password:
        raise UnsafeUrlError("A website address cannot carry credentials.")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    if port not in ALLOWED_PORTS:
        raise UnsafeUrlError("Only standard web ports can be imported.")
    try:
        literal = ipaddress.ip_address(parts.hostname)
    except ValueError:
        literal = None
    if literal is not None and not literal.is_global:
        raise UnsafeUrlError("That address is not on the public internet.")
    if not _is_public(parts.hostname):
        raise UnsafeUrlError("That address is not on the public internet.")


class HttpSiteFetcher:
    """Reads real websites with `httpx`, inside the guards above."""

    def __init__(self) -> None:
        self._robots: dict[str, RobotFileParser | None] = {}

    def _client(self) -> Any:
        import httpx

        return httpx.Client(
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/json;q=0.9,*/*;q=0.5",
            },
            timeout=TIMEOUT_SECONDS,
            follow_redirects=False,
        )

    def _allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._robots:
            robots_page = self._get(urljoin(origin, "/robots.txt"), check_robots=False)
            parser: RobotFileParser | None = None
            if robots_page is not None and robots_page.ok:
                parser = RobotFileParser()
                parser.parse(robots_page.text.splitlines())
            self._robots[origin] = parser
        parser = self._robots[origin]
        return parser is None or parser.can_fetch(USER_AGENT, url)

    def fetch(self, url: str) -> FetchedPage | None:
        try:
            assert_public_url(url)
        except UnsafeUrlError:
            return None
        if not self._allowed(url):
            return None
        return self._get(url, check_robots=True)

    def fetch_image(self, url: str) -> FetchedImage | None:
        try:
            assert_public_url(url)
        except UnsafeUrlError:
            return None
        if not self._allowed(url):
            return None
        return self._get_image(url)

    def _get_image(self, url: str) -> FetchedImage | None:
        read = self._stream(url, check_robots=True, accept=IMAGE_TYPES, max_bytes=MAX_IMAGE_BYTES)
        if read is None:
            return None
        final, status, content_type, body, _ = read
        if status != 200 or body is None:
            return None
        return FetchedImage(final, content_type, body)

    def _get(self, url: str, *, check_robots: bool) -> FetchedPage | None:
        read = self._stream(
            url, check_robots=check_robots, accept=READABLE_TYPES, max_bytes=MAX_BYTES, lenient=True
        )
        if read is None:
            return None
        final, status, content_type, body, encoding = read
        return FetchedPage(
            final, status, content_type, (body or b"").decode(encoding, errors="replace")
        )

    def _stream(
        self,
        url: str,
        *,
        check_robots: bool,
        accept: tuple[str, ...],
        max_bytes: int,
        lenient: bool = False,
    ) -> tuple[str, int, str, bytes | None, str] | None:
        """The one redirect-following read every fetch goes through:
        `(final url, status, content type, body, encoding)`, or `None` when the
        address, a redirect, robots.txt or the size cap refuses it. Every hop
        is re-checked as a public address (and against robots.txt when asked).
        A type outside `accept` has `body=None`; `lenient` also lets a response
        with no stated type through."""
        import httpx

        current = url
        try:
            with self._client() as client:
                for _hop in range(MAX_REDIRECTS + 1):
                    assert_public_url(current)
                    with client.stream("GET", current) as response:
                        if response.is_redirect:
                            location = response.headers.get("location", "")
                            if not location:
                                return None
                            current = urljoin(current, location)
                            if check_robots and not self._allowed(current):
                                return None
                            continue
                        content_type = (
                            response.headers.get("content-type", "").split(";")[0].strip()
                        )
                        encoding = response.encoding or "utf-8"
                        if not (content_type.startswith(accept) or (lenient and not content_type)):
                            return current, response.status_code, content_type, None, encoding
                        body = bytearray()
                        for chunk in response.iter_bytes():
                            body.extend(chunk)
                            if len(body) > max_bytes:
                                return None
                        return current, response.status_code, content_type, bytes(body), encoding
        except (UnsafeUrlError, httpx.HTTPError, OSError):
            return None
        return None


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "sites"


class FakeSiteFetcher:
    """Serves `brand/fixtures/sites/<domain>/…` as if it were the web.

    `/` is `index.html`; `/about` is `about.html` (or `about/index.html`); any
    other path is served verbatim. A domain with no fixture folder is
    unreachable, exactly as a dead site would be — the fake never invents one.
    `fetched` records every URL asked for, so tests can assert what was read.
    """

    def __init__(self, root: Path = FIXTURES) -> None:
        self.root = root
        self.fetched: list[str] = []

    def _file(self, url: str) -> Path | None:
        parts = urlsplit(url)
        host = (parts.hostname or "").removeprefix("www.")
        site = self.root / host
        if not site.is_dir():
            return None
        path = parts.path.strip("/")
        candidates = (
            [site / "index.html"]
            if not path
            else [site / path, site / f"{path}.html", site / path / "index.html"]
        )
        return next((c for c in candidates if c.is_file() and site in c.resolve().parents), None)

    def fetch(self, url: str) -> FetchedPage | None:
        self.fetched.append(url)
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            return None
        if not (self.root / (parts.hostname or "").removeprefix("www.")).is_dir():
            return None
        file = self._file(url)
        if file is None:
            return FetchedPage(url, 404, "text/html", "")
        suffix = file.suffix.lower()
        content_type = {
            ".json": "application/json",
            ".css": "text/css",
            ".txt": "text/plain",
            ".xml": "application/xml",
        }.get(suffix, "text/html")
        return FetchedPage(url, 200, content_type, file.read_text(encoding="utf-8"))

    def fetch_image(self, url: str) -> FetchedImage | None:
        self.fetched.append(url)
        file = self._file(url)
        content_type = mimetypes.guess_type(file.name)[0] if file else None
        if file is None or content_type not in IMAGE_TYPES:
            return None
        return FetchedImage(url, content_type, file.read_bytes())


_override: SiteFetcher | None = None


def set_override(fetcher: SiteFetcher | None) -> None:
    """Tests install a specific fetcher; `None` restores resolution."""
    global _override
    _override = fetcher


def get_site_fetcher() -> SiteFetcher:
    if _override is not None:
        return _override
    if getattr(settings, "USE_FAKE_SITE_FETCHER", True):
        return FakeSiteFetcher()
    return HttpSiteFetcher()
