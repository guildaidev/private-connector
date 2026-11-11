"""Persistent outbound WebSocket carrying requests, responses, and heartbeats."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import ssl
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from unicodedata import category
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidStatus, WebSocketException

from .models import Delivery, ProtocolError, ProxyResponse
from .protocol import MAX_FRAME_BYTES, STREAM_WINDOW, encode_response

log = logging.getLogger(__name__)
DISCOVERY_SUBPROTOCOL = "guild-tunnel-v5"


class GatewayConnection(connect):
    def process_redirect(self, exc: Exception) -> Exception:
        # Only the enrolled endpoint may receive credentials or issue requests.
        return exc


class TunnelClient:
    def __init__(
        self,
        *,
        base_url: str,
        credential: Callable[[], tuple[str, datetime]],
        ca_file: str | None = None,
        max_in_flight: int = 16,
        rotated: asyncio.Event | None = None,
        refresh_requested: asyncio.Event | None = None,
        target_names: tuple[str, ...] = (),
    ):
        parsed = urlsplit(base_url)
        if (
            parsed.scheme not in {"https", "wss"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or any(category(char) == "Cc" for char in base_url)
        ):
            raise ProtocolError(
                "Gateway URL must use HTTPS/WSS, include a hostname, "
                "and have no credentials, fragment, or control characters"
            )
        if type(max_in_flight) is not int or max_in_flight < 1:
            raise ProtocolError("max_in_flight must be a positive integer")
        self._url = urlunsplit(
            (
                "wss",
                parsed.netloc,
                f"{parsed.path.rstrip('/')}/tunnel",
                parsed.query,
                "",
            )
        )
        self._ssl_context = ssl.create_default_context(cafile=ca_file)
        self._rotated = rotated
        self._refresh_requested = refresh_requested
        self._credential = credential
        self._max_in_flight = max_in_flight
        self._target_names = target_names
        self.connected = False

    async def run(
        self,
        forward: Callable[[Delivery], Awaitable[ProxyResponse]],
        stop: asyncio.Event,
        stream_forward: Callable[[Delivery], AsyncIterator[dict[str, object]]]
        | None = None,
    ) -> None:
        retry_seconds = 1
        while not stop.is_set():
            started = asyncio.get_running_loop().time()
            disconnect_reason = "session_ended"
            token, expires_at = self._credential()
            if expires_at <= datetime.now(UTC):
                try:
                    await asyncio.wait_for(stop.wait(), timeout=1)
                except TimeoutError:
                    pass
                continue
            try:
                if self._rotated:
                    self._rotated.clear()
                async with GatewayConnection(
                    self._url,
                    ssl=self._ssl_context,
                    additional_headers={"Authorization": f"Bearer {token}"},
                    subprotocols=[DISCOVERY_SUBPROTOCOL],
                    proxy=None,
                    compression=None,
                    open_timeout=10,
                    close_timeout=5,
                    ping_interval=20,
                    ping_timeout=20,
                    max_size=MAX_FRAME_BYTES,
                ) as socket:
                    try:
                        await self._serve(
                            socket, forward, stop, expires_at, stream_forward
                        )
                        disconnect_reason = (
                            f"websocket_close_{socket.close_code or 'unknown'}"
                        )
                    finally:
                        self.connected = False
            except InvalidStatus as error:
                disconnect_reason = f"gateway_http_{error.response.status_code}"
                if 300 <= error.response.status_code < 400:
                    raise ProtocolError("Gateway redirects are not allowed") from None
                if error.response.status_code in {401, 403} and self._refresh_requested:
                    self._refresh_requested.set()
                    # Renewal can repair a rejected token without re-enrollment.
                    expired_during_connect = True
                else:
                    expired_during_connect = (
                        error.response.status_code == 401
                        and expires_at <= datetime.now(UTC) + timedelta(seconds=10)
                    )
                if (
                    error.response.status_code in {400, 401, 403, 404}
                    and not expired_during_connect
                ):
                    raise ProtocolError(
                        "Guild tunnel gateway rejected the connector connection"
                    ) from None
            except ConnectionClosed as error:
                close = error.rcvd or error.sent
                disconnect_reason = (
                    f"websocket_close_{close.code if close else 'unknown'}"
                )
                if (
                    error.rcvd is not None
                    and error.rcvd.code == 4001
                    and self._refresh_requested
                ):
                    self._refresh_requested.set()
                if error.rcvd is not None and error.rcvd.code == 1008:
                    raise ProtocolError(
                        "Guild tunnel gateway rejected the connector protocol"
                    ) from None
            except ssl.SSLCertVerificationError:
                raise ProtocolError(
                    "Gateway TLS certificate verification failed"
                ) from None
            except (OSError, WebSocketException, TimeoutError) as error:
                disconnect_reason = type(error).__name__
            if stop.is_set():
                return
            if asyncio.get_running_loop().time() - started >= 30:
                retry_seconds = 1
            delay = retry_seconds + random.uniform(0, 1)
            log.warning(
                "Connector tunnel disconnected: reason=%s duration=%.1fs; "
                "reconnecting in %.1fs",
                disconnect_reason,
                asyncio.get_running_loop().time() - started,
                delay,
            )
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except TimeoutError:
                pass
            retry_seconds = min(retry_seconds * 2, 30)

    async def _serve(
        self,
        socket: ClientConnection,
        forward: Callable[[Delivery], Awaitable[ProxyResponse]],
        stop: asyncio.Event,
        expires_at: datetime,
        stream_forward: Callable[[Delivery], AsyncIterator[dict[str, object]]] | None,
    ) -> None:
        version = 5 if socket.subprotocol == DISCOVERY_SUBPROTOCOL else 4
        await socket.send(
            json.dumps(
                {
                    "type": "hello",
                    "protocol_version": version,
                    "max_in_flight": self._max_in_flight,
                }
            )
        )
        ready = _parse_frame(await asyncio.wait_for(socket.recv(), timeout=10))
        if (
            ready.get("type") != "ready"
            or type(ready.get("protocol_version")) is not int
            or ready.get("protocol_version") != version
            or type(ready.get("max_in_flight")) is not int
            or not 1 <= ready["max_in_flight"] <= self._max_in_flight
        ):
            raise ProtocolError("Invalid Guild tunnel gateway connection handshake")
        capacity = ready["max_in_flight"]
        self.connected = True
        log.info("Connector tunnel connected")
        active: dict[str, asyncio.Task[None]] = {}
        credits: dict[str, asyncio.BoundedSemaphore] = {}
        failed: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        authenticated = asyncio.Event()
        expiry_changed = asyncio.Event()
        pending_expiry: datetime | None = None

        async def process(delivery: Delivery) -> None:
            if expires_at <= datetime.now(UTC):
                raise TimeoutError("Tunnel authorization expired")
            await socket.send(json.dumps({"type": "ack", "id": delivery.id}))
            if expires_at <= datetime.now(UTC):
                raise TimeoutError("Tunnel authorization expired")
            if delivery.stream_response:
                if stream_forward is None:
                    raise ProtocolError("Streaming forwarder is not configured")
                async for frame in stream_forward(delivery):
                    if frame["type"] == "response_chunk":
                        await credits[delivery.id].acquire()
                    await socket.send(json.dumps(frame))
            else:
                result = await forward(delivery)
                await socket.send(encode_response(result))

        def finished(request_id: str, task: asyncio.Task[None]) -> None:
            active.pop(request_id, None)
            credits.pop(request_id, None)
            if not task.cancelled() and (error := task.exception()) is not None:
                if not failed.done():
                    failed.set_exception(error)

        async def receive() -> None:
            nonlocal expires_at
            async for data in socket:
                frame = _parse_frame(data)
                kind = frame.get("type")
                if kind == "authenticated":
                    try:
                        acknowledged_expiry = datetime.fromisoformat(
                            str(frame.get("expires_at"))
                        )
                    except ValueError:
                        raise ProtocolError(
                            "Invalid authentication acknowledgement"
                        ) from None
                    if pending_expiry is None or acknowledged_expiry != pending_expiry:
                        raise ProtocolError("Unexpected authentication acknowledgement")
                    expires_at = pending_expiry
                    authenticated.set()
                    expiry_changed.set()
                elif kind == "ping":
                    nonce = frame.get("nonce")
                    if not isinstance(nonce, str) or len(nonce) > 128:
                        raise ProtocolError("Invalid heartbeat")
                    await socket.send(json.dumps({"type": "pong", "nonce": nonce}))
                elif kind == "list_targets":
                    if version < 5:
                        raise ProtocolError(
                            "Unexpected Guild tunnel gateway frame type"
                        )
                    try:
                        request_id = str(UUID(str(frame.get("id"))))
                    except ValueError as error:
                        raise ProtocolError("Invalid target list request ID") from error
                    response = json.dumps(
                        {
                            "type": "targets",
                            "id": request_id,
                            "names": self._target_names,
                        }
                    )
                    if (
                        len(self._target_names) > 2048
                        or len(response.encode()) > 4 * 1024 * 1024
                    ):
                        response = json.dumps(
                            {
                                "type": "error",
                                "id": request_id,
                                "error_code": "targets_unavailable",
                            }
                        )
                    await socket.send(response)
                elif kind == "cancel":
                    try:
                        request_id = str(UUID(str(frame.get("id"))))
                    except ValueError as error:
                        raise ProtocolError(
                            "Invalid cancellation request ID"
                        ) from error
                    if task := active.get(request_id):
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                elif kind == "credit":
                    try:
                        request_id = str(UUID(str(frame.get("id"))))
                    except ValueError as error:
                        raise ProtocolError("Invalid stream credit ID") from error
                    if frame.get("chunks") != 1:
                        raise ProtocolError("Invalid stream credit")
                    if credit := credits.get(request_id):
                        try:
                            credit.release()
                        except ValueError as error:
                            raise ProtocolError("Excess stream credit") from error
                elif kind == "request":
                    if expires_at <= datetime.now(UTC):
                        raise TimeoutError("Tunnel authorization expired")
                    delivery = Delivery.from_payload(frame)
                    active_count = sum(not task.done() for task in active.values())
                    if delivery.id in active or active_count >= capacity:
                        raise ProtocolError(
                            "Guild tunnel gateway exceeded negotiated request capacity"
                        )
                    if delivery.stream_response:
                        credits[delivery.id] = asyncio.BoundedSemaphore(STREAM_WINDOW)
                    task = asyncio.create_task(process(delivery))
                    active[delivery.id] = task
                    task.add_done_callback(
                        lambda done, key=delivery.id: finished(key, done)
                    )
                else:
                    raise ProtocolError("Unexpected Guild tunnel gateway frame type")

        receiver = asyncio.create_task(receive())
        stopping = asyncio.create_task(stop.wait())

        async def reauthenticate() -> None:
            nonlocal pending_expiry
            if self._rotated is None:
                await asyncio.Future()
                return
            while True:
                await self._rotated.wait()
                self._rotated.clear()
                token, pending_expiry = self._credential()
                authenticated.clear()
                await socket.send(json.dumps({"type": "authenticate", "token": token}))
                await asyncio.wait_for(authenticated.wait(), timeout=10)
                pending_expiry = None

        async def enforce_expiry() -> None:
            while True:
                expiry_changed.clear()
                remaining = (expires_at - datetime.now(UTC)).total_seconds()
                if remaining <= 0:
                    raise TimeoutError("Tunnel authorization expired")
                await asyncio.wait_for(expiry_changed.wait(), timeout=remaining)

        rotating = asyncio.create_task(reauthenticate())
        expiring = asyncio.create_task(enforce_expiry())
        try:
            done, _ = await asyncio.wait(
                [receiver, stopping, rotating, expiring, failed],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in done:
                task.result()
        finally:
            receiver.cancel()
            stopping.cancel()
            rotating.cancel()
            expiring.cancel()
            workers = list(active.values())
            for task in workers:
                task.cancel()
            failed.cancel()
            await asyncio.gather(
                receiver,
                stopping,
                rotating,
                expiring,
                failed,
                *workers,
                return_exceptions=True,
            )


def _parse_frame(data: str | bytes) -> dict[str, object]:
    try:
        frame = json.loads(data)
    except (ValueError, UnicodeError) as error:
        raise ProtocolError("Invalid JSON frame from Guild tunnel gateway") from error
    if not isinstance(frame, dict):
        raise ProtocolError("Guild tunnel gateway frame must be an object")
    return frame
