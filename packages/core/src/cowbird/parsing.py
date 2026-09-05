from __future__ import annotations

import re
from html import unescape
from html.parser import HTMLParser

_DROP_TAGS = {"script", "style", "head", "title"}
_SAFE_SCHEMES = ("http://", "https://")

# A standalone 6-digit run is overwhelmingly the code; anything longer is
# usually an order or reference number. Try the strong signal, then widen.
_OTP_STRICT = re.compile(r"(?<!\d)(\d{6})(?!\d)")
_OTP_LOOSE = re.compile(r"(?<!\d)(\d{4,8})(?!\d)")


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.chunks: list[str] = []
        self.links: list[str] = []
        self._suppress = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _DROP_TAGS:
            self._suppress += 1
        if tag == "a":
            for name, value in attrs:
                if name == "href" and value and value.startswith(_SAFE_SCHEMES):
                    self.links.append(unescape(value))

    def handle_endtag(self, tag: str) -> None:
        if tag in _DROP_TAGS and self._suppress:
            self._suppress -= 1

    def handle_data(self, data: str) -> None:
        if not self._suppress and data.strip():
            self.chunks.append(data.strip())


def _parse(html: str) -> _TextExtractor:
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    return parser


def html_to_text(html: str) -> str:
    return " ".join(_parse(html).chunks)


def extract_links(html: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(_parse(html).links))


def extract_otp(text: str, pattern: str | None = None) -> str | None:
    if pattern is not None:
        match = re.search(pattern, text)
        return match.group(0) if match else None
    for regex in (_OTP_STRICT, _OTP_LOOSE):
        match = regex.search(text)
        if match:
            return match.group(1)
    return None
