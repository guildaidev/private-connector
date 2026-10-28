"""Asynchronous HTTP forwarding to customer-configured local origins."""

from __future__ import annotations

import re
from base64 import b64encode
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from http.cookiejar import Cookie, CookieJar, DefaultCookiePolicy
from urllib.request import Request

import httpx

from .models import Delivery, Destination, ProxyResponse
from .protocol import (
    MAX_STREAM_BYTES,
    MAX_STREAM_CHUNK_BYTES,
    response_body_limit,
)
from .urls import request_url

_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")


class _RejectCookies(DefaultCookiePolicy):
    def set_ok(self, cookie: Cookie, request: Request) -> bool:
        # A pooled client must not retain authentication state between callers.
        return False


class HttpForwarder:
    def __init__(self) -> None:
        self._client = httpx.AsyncClient(
            trust_env=False,
            follow_redirects=False,
            timeout=None,
            cookies=CookieJar(policy=_RejectCookies()),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def forward(
        self, destination: Destination, delivery: Delivery
    ) -> ProxyResponse:
        prepared = self._prepare(destination, delivery)
        if isinstance(prepared, ProxyResponse):
            return prepared
        url, headers = prepared
        # Redirects are responses; following one here could send credentials to
        # an origin that the customer did not configure for this target.
        async with self._client.stream(
            delivery.method,
            url,
            content=delivery.body,
            headers=headers,
            timeout=delivery.timeout,
        ) as response:
            result = ProxyResponse(
                delivery.id, response.status_code, dict(response.headers), b""
            )
            limit = response_body_limit(result)
            too_large = ProxyResponse(delivery.id, 502, {}, b"", "response_too_large")
            if limit < 0:
                return too_large
            body = bytearray()
            async for chunk in response.aiter_raw():
                if len(body) + len(chunk) > limit:
                    return too_large
                body.extend(chunk)
            return ProxyResponse(
                delivery.id, result.status, result.headers, bytes(body)
            )

    async def stream_forward(
        self, destination: Destination, delivery: Delivery
    ) -> AsyncIterator[dict[str, object]]:
        prepared = self._prepare(destination, delivery)
        if isinstance(prepared, ProxyResponse):
            yield {
                "type": "error",
                "id": delivery.id,
                "error_code": prepared.error_code or "local_forward_error",
            }
            return
        url, headers = prepared
        async with self._client.stream(
            delivery.method,
            url,
            content=delivery.body,
            headers=headers,
            timeout=delivery.timeout,
        ) as response:
            result = ProxyResponse(
                delivery.id, response.status_code, dict(response.headers), b""
            )
            if response_body_limit(result) < 0:
                yield {
                    "type": "error",
                    "id": delivery.id,
                    "error_code": "response_too_large",
                }
                return
            yield {
                "type": "response_start",
                "id": delivery.id,
                "response_status": result.status,
                "response_headers": result.headers,
            }
            total = 0
            async for raw in response.aiter_raw():
                for offset in range(0, len(raw), MAX_STREAM_CHUNK_BYTES):
                    chunk = raw[offset : offset + MAX_STREAM_CHUNK_BYTES]
                    total += len(chunk)
                    if total > MAX_STREAM_BYTES:
                        yield {
                            "type": "error",
                            "id": delivery.id,
                            "error_code": "response_too_large",
                        }
                        return
                    yield {
                        "type": "response_chunk",
                        "id": delivery.id,
                        "response_body_base64": b64encode(chunk).decode(),
                    }
            yield {"type": "response_end", "id": delivery.id}

    def _prepare(
        self, destination: Destination, delivery: Delivery
    ) -> tuple[httpx.URL, dict[str, str]] | ProxyResponse:
        if delivery.target_id != destination.id:
            return ProxyResponse(delivery.id, 502, {}, b"", "unknown_target")
        try:
            url = request_url(destination.url, delivery.endpoint, delivery.query)
        except ValueError:
            return ProxyResponse(delivery.id, 502, {}, b"", "destination_not_allowed")
        remaining = (delivery.expires_at - datetime.now(UTC)).total_seconds()
        if remaining <= 0:
            return ProxyResponse(delivery.id, 502, {}, b"", "delivery_expired")
        if (
            not re.fullmatch(r"[A-Z]{1,16}", delivery.method)
            or delivery.method == "CONNECT"
        ):
            return ProxyResponse(delivery.id, 502, {}, b"", "invalid_request")
        hop_headers = {
            "connection",
            "keep-alive",
            "proxy-authenticate",
            "proxy-authorization",
            "te",
            "trailer",
            "transfer-encoding",
            "upgrade",
            "host",
            "content-length",
        }
        for name, value in delivery.headers.items():
            if (
                not _HEADER_NAME.fullmatch(name)
                or not value.isascii()
                or any(
                    (ord(char) < 32 and char != "\t") or ord(char) == 127
                    for char in value
                )
            ):
                return ProxyResponse(delivery.id, 502, {}, b"", "invalid_request")
            if name.lower() == "connection":
                hop_headers.update(part.strip().lower() for part in value.split(","))
        headers = {
            key: value
            for key, value in delivery.headers.items()
            if key.lower() not in hop_headers
        }
        return url, headers
