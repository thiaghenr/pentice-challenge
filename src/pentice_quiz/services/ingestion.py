import hashlib
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from pentice_quiz.domain.schemas import Section
from pentice_quiz.errors import InvalidRequestError, SourceUnavailableError

ALLOWED_CONTENT_TYPES = frozenset(
    {"", "text/plain", "text/markdown", "text/x-markdown", "application/octet-stream"}
)
_H1 = re.compile(r"^#\s+(.+?)\s*#*\s*$")
_H2 = re.compile(r"^##\s+(.+?)\s*#*\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")


@dataclass(frozen=True, slots=True)
class FetchedSource:
    url: str
    content: str
    content_hash: str
    etag: str | None
    last_modified: str | None


def content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def extract_title(markdown: str) -> str | None:
    for line in markdown.splitlines():
        match = _H1.match(line)
        if match:
            return match.group(1).strip()
    return None


def split_sections(markdown: str) -> list[Section]:
    sections: list[Section] = []
    heading: str | None = None
    body: list[str] = []
    in_fence = False

    def flush() -> None:
        text = "\n".join(body).strip()
        if heading is not None and text:
            sections.append(Section(heading=heading, text=text))

    for line in markdown.splitlines():
        if _FENCE.match(line):
            in_fence = not in_fence
        match = None if in_fence else _H2.match(line)
        if match:
            flush()
            heading = match.group(1).strip()
            body = []
        elif heading is not None:
            body.append(line)
    flush()

    if not sections:
        text = "\n".join(line for line in markdown.splitlines() if not _H1.match(line)).strip()
        if text:
            sections.append(Section(heading=extract_title(markdown) or "General", text=text))
    return sections


def fetch_markdown(client: httpx.Client, url: str, *, max_bytes: int) -> FetchedSource:
    if urlsplit(url).scheme not in ("http", "https"):
        raise InvalidRequestError(f"source_url must be http(s): {url!r}", code="invalid_source_url")

    try:
        with client.stream(
            "GET", url, headers={"Accept": "text/markdown, text/plain;q=0.9, */*;q=0.1"}
        ) as response:
            if response.status_code >= 400:
                raise SourceUnavailableError(
                    f"house rules source returned HTTP {response.status_code}"
                )
            content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
            if content_type not in ALLOWED_CONTENT_TYPES:
                raise SourceUnavailableError(
                    f"house rules source returned unsupported content type {content_type!r}; "
                    "use a raw file URL",
                    code="source_invalid",
                )
            declared = response.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > max_bytes:
                raise _too_large(max_bytes)
            payload = bytearray()
            for chunk in response.iter_bytes():
                payload.extend(chunk)
                if len(payload) > max_bytes:
                    raise _too_large(max_bytes)
            etag = response.headers.get("etag")
            last_modified = response.headers.get("last-modified")
    except httpx.TimeoutException as exc:
        raise SourceUnavailableError("house rules source timed out") from exc
    except httpx.HTTPError as exc:
        raise SourceUnavailableError(f"house rules source is unreachable: {exc}") from exc

    try:
        content = bytes(payload).decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise SourceUnavailableError(
            "house rules source is not valid UTF-8 text", code="source_invalid"
        ) from exc
    if not content.strip():
        raise SourceUnavailableError("house rules source is empty", code="source_invalid")

    return FetchedSource(
        url=url,
        content=content,
        content_hash=content_hash(content),
        etag=etag,
        last_modified=last_modified,
    )


def _too_large(max_bytes: int) -> SourceUnavailableError:
    return SourceUnavailableError(
        f"house rules source exceeds {max_bytes} bytes", code="source_too_large"
    )
