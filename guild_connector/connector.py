"""Forward customer requests received on the persistent Guild connection."""

from __future__ import annotations

import asyncio
import logging
import os
import ssl
from collections.abc import AsyncIterator

import httpx

from .http_forwarder import HttpForwarder
from .identity import http_error_message
from .models import Delivery, Destination, ProxyResponse
from .tunnel_client import TunnelClient

log = logging.getLogger(__name__)


class PrivateConnector:
    def __init__(self, *, tunnel: TunnelClient, destinations: dict[str, Destination]):
        self._tunnel = tunnel
        self._destinations = destinations
        self._forwarder = HttpForwarder()

    async def forward(self, delivery: Delivery) -> ProxyResponse:
        destination = self._destinations.get(delivery.target_id)
        if delivery.is_expired():
            error_code = "delivery_expired"
        elif destination is None:
            error_code = "unknown_target"
        else:
            try:
                return await self._forwarder.forward(destination, delivery)
            except (httpx.HTTPError, OSError, ValueError) as error:
                error_code = self._forward_error(error, destination, delivery)
        return ProxyResponse(delivery.id, 502, {}, b"", error_code)

    async def stream_forward(
        self, delivery: Delivery
    ) -> AsyncIterator[dict[str, object]]:
        destination = self._destinations.get(delivery.target_id)
        if delivery.is_expired():
            error_code = "delivery_expired"
        elif destination is None:
            error_code = "unknown_target"
        else:
            try:
                async for frame in self._forwarder.stream_forward(
                    destination, delivery
                ):
                    yield frame
                return
            except (httpx.HTTPError, OSError, ValueError) as error:
                error_code = self._forward_error(error, destination, delivery)
        yield {"type": "error", "id": delivery.id, "error_code": error_code}

    @staticmethod
    def _forward_error(
        error: Exception, destination: Destination, delivery: Delivery
    ) -> str:
        if isinstance(error, httpx.TimeoutException):
            log.warning(
                "Local forwarding timed out (delivery=%r target=%r)",
                delivery.id,
                destination.id,
            )
            return {
                httpx.ConnectTimeout: "connect_timeout",
                httpx.ReadTimeout: "read_timeout",
                httpx.WriteTimeout: "write_timeout",
                httpx.PoolTimeout: "pool_timeout",
            }.get(type(error), "local_forward_error")
        if isinstance(error, httpx.HTTPError):
            reason = http_error_message(error, "Local forwarding")
        elif isinstance(error, ssl.SSLError):
            reason = "TLS handshake failed; check the local service certificate"
        elif isinstance(error, TimeoutError):
            reason = "Local request timed out"
        elif isinstance(error, OSError):
            reason = os.strerror(error.errno) if error.errno else "I/O error"
        else:
            reason = "Invalid local request or response"
        log.warning(
            "Local forwarding failed (delivery=%r target=%r): %s",
            delivery.id,
            destination.id,
            reason,
        )
        return "local_forward_error"

    async def run_forever(self, stop: asyncio.Event) -> None:
        try:
            await self._tunnel.run(self.forward, stop, self.stream_forward)
        finally:
            await self._forwarder.aclose()
