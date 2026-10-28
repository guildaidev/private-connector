"""Construct requests with the customer's configured HTTP authority."""

import httpx


def parse_origin(value: str) -> httpx.URL:
    try:
        url = httpx.URL(value)
    except (TypeError, httpx.InvalidURL):
        raise ValueError("Invalid HTTP destination") from None
    if (
        url.scheme not in {"http", "https"}
        or not url.host
        or url.userinfo
        or url.raw_path != b"/"
        or "#" in value
        or (url.port is not None and not 1 <= url.port <= 65535)
    ):
        raise ValueError("A destination must be an HTTP(S) origin without credentials")
    return url


def request_url(origin: httpx.URL, endpoint: str, query: str | None) -> httpx.URL:
    if (
        not endpoint.startswith("/")
        or endpoint.startswith("//")
        or "?" in endpoint
        or "\\" in endpoint
    ):
        raise ValueError("An absolute endpoint path is required")
    try:
        raw_path = (endpoint + ("?" + query if query is not None else "")).encode(
            "ascii"
        )
        # Component replacement cannot change the configured scheme or authority.
        url = origin.copy_with(raw_path=raw_path)
    except (UnicodeEncodeError, httpx.InvalidURL):
        raise ValueError("Invalid encoded endpoint or query") from None
    if url.raw_path != raw_path:
        raise ValueError("The endpoint and query must already be URL-encoded")
    return url
