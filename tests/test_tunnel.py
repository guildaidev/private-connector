from __future__ import annotations

import asyncio
import json
import ssl
import subprocess
import tempfile
import threading
import unittest
from base64 import b64decode
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

from guild_connector.connector import PrivateConnector
from guild_connector.models import Destination, ProtocolError
from guild_connector.tunnel_client import TunnelClient


class TunnelTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.files = tempfile.TemporaryDirectory(prefix="guild-connector-tls-")
        cls.cert = Path(cls.files.name) / "cert.pem"
        cls.key = Path(cls.files.name) / "key.pem"
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-days",
                "1",
                "-subj",
                "/CN=localhost",
                "-addext",
                "subjectAltName=DNS:localhost,IP:127.0.0.1",
                "-keyout",
                str(cls.key),
                "-out",
                str(cls.cert),
            ],
            check=True,
            capture_output=True,
        )

    @classmethod
    def tearDownClass(cls):
        cls.files.cleanup()

    async def asyncSetUp(self):
        self.requests = []
        requests = self.requests
        self.release_slow = threading.Event()
        self.slow_started = threading.Event()
        release_slow, slow_started = self.release_slow, self.slow_started

        class LocalService(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append(self.path)
                if self.path == "/stream":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    self.wfile.write(b"data: first\n\n")
                    self.wfile.flush()
                    slow_started.set()
                    release_slow.wait(timeout=5)
                    self.wfile.write(b"data: second\n\n")
                    self.wfile.flush()
                    return
                if self.path == "/slow":
                    slow_started.set()
                    release_slow.wait(timeout=5)
                data = self.path.encode()
                if self.path == "/large":
                    data = b"x" * (12 * 1024 * 1024)
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                try:
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, format, *args):
                pass

        self.http = ThreadingHTTPServer(("127.0.0.1", 0), LocalService)
        self.http_thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.http_thread.start()
        self.tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.tls.load_cert_chain(self.cert, self.key)

    async def asyncTearDown(self):
        self.release_slow.set()
        await asyncio.to_thread(self.http.shutdown)
        self.http.server_close()
        self.http_thread.join()

    def request(self, path):
        return {
            "type": "request",
            "id": str(uuid4()),
            "target_id": "local",
            "method": "GET",
            "endpoint": path,
            "query": None,
            "headers": {},
            "body_base64": "",
            "expires_at": (datetime.now(UTC) + timedelta(seconds=20)).isoformat(),
            "timeout": {"connect": 30, "read": 30, "write": 30, "pool": 30},
        }

    def connector(
        self, port, max_in_flight=2, ca_file=None, rotated=None, credential=None
    ):
        return PrivateConnector(
            tunnel=TunnelClient(
                base_url=f"https://127.0.0.1:{port}",
                ca_file=ca_file or str(self.cert),
                credential=credential or self.credential,
                max_in_flight=max_in_flight,
                rotated=rotated,
                target_names=("local",),
            ),
            destinations={
                "local": Destination(
                    "local", f"http://127.0.0.1:{self.http.server_port}"
                )
            },
        )

    def credential(self):
        return "test-credential", datetime.now(UTC) + timedelta(minutes=5)

    async def test_gateway_upgrade_and_rollback_preserve_forwarding_and_streaming(self):
        stop = asyncio.Event()
        completed = asyncio.get_running_loop().create_future()
        versions = []

        def select_protocol(_connection, offered):
            return "guild-tunnel-v5" if len(versions) == 1 else None

        async def handler(socket):
            try:
                version = 5 if socket.subprotocol else 4
                hello = json.loads(await socket.recv())
                self.assertEqual(hello["protocol_version"], version)
                self.assertEqual(
                    socket.request.headers["Authorization"], "Bearer test-credential"
                )
                versions.append(version)
                await socket.send(
                    json.dumps(
                        {
                            "type": "ready",
                            "protocol_version": version,
                            "max_in_flight": 2,
                        }
                    )
                )
                if version == 5:
                    request_id = str(uuid4())
                    await socket.send(
                        json.dumps({"type": "list_targets", "id": request_id})
                    )
                    self.assertEqual(
                        json.loads(await socket.recv())["names"], ["local"]
                    )
                buffered = self.request("/buffered")
                await socket.send(json.dumps(buffered))
                self.assertEqual(json.loads(await socket.recv())["type"], "ack")
                response = json.loads(await socket.recv())
                self.assertEqual(response["type"], "response")
                self.assertEqual(
                    b64decode(response["response_body_base64"]), b"/buffered"
                )
                request = self.request("/fast")
                request["stream_response"] = True
                await socket.send(json.dumps(request))
                frames = []
                while not frames or frames[-1]["type"] != "response_end":
                    frames.append(json.loads(await asyncio.wait_for(socket.recv(), 3)))
                self.assertEqual(
                    [frame["type"] for frame in frames],
                    ["ack", "response_start", "response_chunk", "response_end"],
                )
                self.assertEqual(b64decode(frames[2]["response_body_base64"]), b"/fast")
                if len(versions) == 3:
                    completed.set_result(None)
                    await stop.wait()
            except Exception as error:
                if not completed.done():
                    completed.set_exception(error)

        async with serve(
            handler,
            "127.0.0.1",
            0,
            ssl=self.tls,
            subprotocols=["guild-tunnel-v5"],
            select_subprotocol=select_protocol,
        ) as server:
            task = asyncio.create_task(
                self.connector(server.sockets[0].getsockname()[1]).run_forever(stop)
            )
            try:
                await asyncio.wait_for(completed, 12)
                self.assertEqual(versions, [4, 5, 4])
            finally:
                stop.set()
                await asyncio.wait_for(task, 3)

    async def test_protocol_rejection_does_not_trigger_downgrade(self):
        stop = asyncio.Event()
        versions = []

        async def handler(socket):
            versions.append(json.loads(await socket.recv())["protocol_version"])
            await socket.close(code=1008, reason="protocol rejected")

        async with serve(
            handler, "127.0.0.1", 0, ssl=self.tls, subprotocols=["guild-tunnel-v5"]
        ) as server:
            with self.assertRaisesRegex(
                ProtocolError, "rejected the connector protocol"
            ):
                await asyncio.wait_for(
                    self.connector(server.sockets[0].getsockname()[1]).run_forever(
                        stop
                    ),
                    3,
                )
        self.assertEqual(versions, [5])

    async def test_live_target_listing_uses_configured_names(self):
        stop = asyncio.Event()
        reported = asyncio.get_running_loop().create_future()
        request_id = str(uuid4())

        async def handler(socket):
            await socket.recv()
            await socket.send(
                json.dumps({"type": "ready", "protocol_version": 5, "max_in_flight": 2})
            )
            await socket.send(
                json.dumps(
                    {"type": "list_targets", "id": request_id, "future_option": True}
                )
            )
            response = json.loads(await socket.recv())
            await socket.send(json.dumps({"type": "ping", "nonce": "still-open"}))
            heartbeat = json.loads(await socket.recv())
            reported.set_result((response, heartbeat))
            await stop.wait()

        async with serve(
            handler, "127.0.0.1", 0, ssl=self.tls, subprotocols=["guild-tunnel-v5"]
        ) as server:
            task = asyncio.create_task(
                self.connector(server.sockets[0].getsockname()[1]).run_forever(stop)
            )
            try:
                assert await asyncio.wait_for(reported, 3) == (
                    {"type": "targets", "id": request_id, "names": ["local"]},
                    {"type": "pong", "nonce": "still-open"},
                )
                self.assertEqual(self.requests, [])
            finally:
                stop.set()
                await asyncio.wait_for(task, 3)

    async def test_target_listing_after_expiry_closes_without_names(self):
        stop = asyncio.Event()
        finished = asyncio.get_running_loop().create_future()
        initial = datetime.now(UTC)
        expiry = initial + timedelta(minutes=1)

        class Clock(datetime):
            current = initial

            @classmethod
            def now(cls, tz=None):
                return cls.current

        async def handler(socket):
            try:
                await socket.recv()
                await socket.send(
                    json.dumps(
                        {"type": "ready", "protocol_version": 5, "max_in_flight": 2}
                    )
                )
                await socket.send(json.dumps({"type": "ping", "nonce": "ready"}))
                self.assertEqual(json.loads(await socket.recv())["type"], "pong")
                Clock.current = expiry
                await socket.send(
                    json.dumps({"type": "list_targets", "id": str(uuid4())})
                )
                with self.assertRaises(ConnectionClosed):
                    await socket.recv()
                finished.set_result(None)
            except Exception as error:
                if not finished.done():
                    finished.set_exception(error)

        with patch("guild_connector.tunnel_client.datetime", Clock):
            async with serve(
                handler, "127.0.0.1", 0, ssl=self.tls, subprotocols=["guild-tunnel-v5"]
            ) as server:
                connector = self.connector(
                    server.sockets[0].getsockname()[1],
                    credential=lambda: ("token", expiry),
                )
                task = asyncio.create_task(connector.run_forever(stop))
                try:
                    with self.assertLogs(
                        "guild_connector.tunnel_client", level="WARNING"
                    ):
                        await asyncio.wait_for(finished, 3)
                finally:
                    stop.set()
                    await asyncio.wait_for(task, 3)

    async def test_target_listing_uses_acknowledged_renewal_expiry(self):
        stop, rotated = asyncio.Event(), asyncio.Event()
        finished = asyncio.get_running_loop().create_future()
        initial = datetime.now(UTC)
        old_expiry = initial + timedelta(minutes=1)
        new_expiry = initial + timedelta(minutes=5)
        credential = ["old", old_expiry]
        request_id = str(uuid4())

        class Clock(datetime):
            current = initial

            @classmethod
            def now(cls, tz=None):
                return cls.current

        async def handler(socket):
            try:
                await socket.recv()
                await socket.send(
                    json.dumps(
                        {"type": "ready", "protocol_version": 5, "max_in_flight": 2}
                    )
                )
                credential[:] = ["new", new_expiry]
                rotated.set()
                self.assertEqual(
                    json.loads(await socket.recv()),
                    {"type": "authenticate", "token": "new"},
                )
                await socket.send(
                    json.dumps(
                        {"type": "authenticated", "expires_at": new_expiry.isoformat()}
                    )
                )
                await socket.send(json.dumps({"type": "ping", "nonce": "renewed"}))
                self.assertEqual(json.loads(await socket.recv())["type"], "pong")
                Clock.current = old_expiry
                await socket.send(
                    json.dumps({"type": "list_targets", "id": request_id})
                )
                self.assertEqual(
                    json.loads(await socket.recv()),
                    {"type": "targets", "id": request_id, "names": ["local"]},
                )
                finished.set_result(None)
                await stop.wait()
            except Exception as error:
                if not finished.done():
                    finished.set_exception(error)

        with patch("guild_connector.tunnel_client.datetime", Clock):
            async with serve(
                handler, "127.0.0.1", 0, ssl=self.tls, subprotocols=["guild-tunnel-v5"]
            ) as server:
                connector = self.connector(
                    server.sockets[0].getsockname()[1],
                    rotated=rotated,
                    credential=lambda: tuple(credential),
                )
                task = asyncio.create_task(connector.run_forever(stop))
                try:
                    await asyncio.wait_for(finished, 3)
                finally:
                    stop.set()
                    await asyncio.wait_for(task, 3)

    async def test_target_listing_rejects_invalid_ids(self):
        for request in (
            {"type": "list_targets"},
            {"type": "list_targets", "id": "not-a-uuid", "future_option": True},
            {"type": "list_targets", "id": None},
        ):
            with self.subTest(request=request):
                stop = asyncio.Event()

                async def handler(socket):
                    await socket.recv()
                    await socket.send(
                        json.dumps(
                            {"type": "ready", "protocol_version": 5, "max_in_flight": 2}
                        )
                    )
                    await socket.send(json.dumps(request))
                    await socket.wait_closed()

                async with serve(
                    handler,
                    "127.0.0.1",
                    0,
                    ssl=self.tls,
                    subprotocols=["guild-tunnel-v5"],
                ) as server:
                    with self.assertRaisesRegex(
                        ProtocolError, "Invalid target list request ID"
                    ):
                        await asyncio.wait_for(
                            self.connector(
                                server.sockets[0].getsockname()[1]
                            ).run_forever(stop),
                            3,
                        )

    async def test_large_target_list_does_not_disconnect_tunnel(self):
        await self.check_target_list_limit(
            tuple(f"private-name-{i}" for i in range(2049))
        )

    async def test_large_encoded_target_list_does_not_disconnect_tunnel(self):
        await self.check_target_list_limit(("private-name-" + "x" * (4 * 1024 * 1024),))

    async def check_target_list_limit(self, names):
        stop = asyncio.Event()
        reported = asyncio.get_running_loop().create_future()
        request_id = str(uuid4())

        async def handler(socket):
            await socket.recv()
            await socket.send(
                json.dumps({"type": "ready", "protocol_version": 5, "max_in_flight": 2})
            )
            await socket.send(json.dumps({"type": "list_targets", "id": request_id}))
            response = json.loads(await socket.recv())
            await socket.send(json.dumps({"type": "ping", "nonce": "still-open"}))
            heartbeat = json.loads(await socket.recv())
            reported.set_result((response, heartbeat))
            await stop.wait()

        async with serve(
            handler, "127.0.0.1", 0, ssl=self.tls, subprotocols=["guild-tunnel-v5"]
        ) as server:
            port = server.sockets[0].getsockname()[1]
            connector = PrivateConnector(
                tunnel=TunnelClient(
                    base_url=f"https://127.0.0.1:{port}",
                    ca_file=str(self.cert),
                    credential=self.credential,
                    target_names=names,
                ),
                destinations={},
            )
            task = asyncio.create_task(connector.run_forever(stop))
            try:
                with self.assertLogs(
                    "guild_connector.tunnel_client", level="WARNING"
                ) as logs:
                    assert await asyncio.wait_for(reported, 3) == (
                        {
                            "type": "error",
                            "id": request_id,
                            "error_code": "targets_unavailable",
                        },
                        {"type": "pong", "nonce": "still-open"},
                    )
                encoded_size = len(
                    json.dumps(
                        {"type": "targets", "id": request_id, "names": names}
                    ).encode()
                )
                self.assertEqual(len(logs.records), 1)
                message = logs.records[0].getMessage()
                self.assertIn(f"{len(names)} targets", message)
                self.assertIn(f"{encoded_size} bytes", message)
                self.assertNotIn("private-name", message)
            finally:
                stop.set()
                await asyncio.wait_for(task, 3)

    async def test_gateway_base_url_preserves_optional_path_and_query(self):
        for scheme in ("https", "wss"):
            for path, query in (
                ("", ""),
                ("", "region=us"),
                ("/", ""),
                ("/api", ""),
                ("/v1/", ""),
                ("/proxy/connectors/", ""),
                ("/proxy/connectors/", "region=us&hint=a%2Fb&region=eu"),
            ):
                with self.subTest(scheme=scheme, path=path, query=query):
                    stop = asyncio.Event()
                    connected = asyncio.get_running_loop().create_future()

                    async def handler(socket):
                        try:
                            await socket.recv()
                            await socket.send(
                                json.dumps(
                                    {
                                        "type": "ready",
                                        "protocol_version": 5,
                                        "max_in_flight": 1,
                                    }
                                )
                            )
                            connected.set_result(
                                (
                                    socket.request.path,
                                    socket.request.headers["Authorization"],
                                )
                            )
                            await stop.wait()
                        except Exception as error:
                            if not connected.done():
                                connected.set_exception(error)

                    async with serve(
                        handler,
                        "127.0.0.1",
                        0,
                        ssl=self.tls,
                        subprotocols=["guild-tunnel-v5"],
                    ) as server:
                        port = server.sockets[0].getsockname()[1]
                        connector = PrivateConnector(
                            tunnel=TunnelClient(
                                base_url=(
                                    f"{scheme}://127.0.0.1:{port}{path}"
                                    f"{'?' + query if query else ''}"
                                ),
                                ca_file=str(self.cert),
                                credential=self.credential,
                            ),
                            destinations={},
                        )
                        task = asyncio.create_task(connector.run_forever(stop))
                        try:
                            observed_path, authorization = await asyncio.wait_for(
                                connected, 3
                            )
                            self.assertEqual(
                                observed_path,
                                f"{path.rstrip('/')}/tunnel"
                                f"{'?' + query if query else ''}",
                            )
                            self.assertEqual(authorization, "Bearer test-credential")
                        finally:
                            stop.set()
                            await asyncio.wait_for(task, 3)

    def test_gateway_base_url_rejects_insecure_or_ambiguous_urls(self):
        for base_url in (
            "http://gateway.test",
            "ws://gateway.test",
            "https:///tunnel",
            "https://user:password@gateway.test",
            "https://@gateway.test",
            "https://gateway.test#fragment",
            "https://gateway.test/path?region=us#fragment",
            "https://gateway.test/path?region=us\n",
        ):
            with self.subTest(base_url=base_url):
                with self.assertRaisesRegex(ProtocolError, "Gateway URL"):
                    TunnelClient(
                        base_url=base_url,
                        ca_file=str(self.cert),
                        credential=self.credential,
                    )

    def test_requested_capacity_requires_positive_integer(self):
        for requested in (0, -1, True, 1.5, "128"):
            with self.subTest(requested=requested):
                with self.assertRaisesRegex(ProtocolError, "positive integer"):
                    TunnelClient(
                        base_url="https://gateway.test",
                        ca_file=str(self.cert),
                        credential=self.credential,
                        max_in_flight=requested,
                    )

    async def test_client_uses_negotiated_lower_capacity(self):
        slow, fast = self.request("/slow"), self.request("/fast")

        async def handler(socket):
            hello = json.loads(await socket.recv())
            self.assertEqual(hello["max_in_flight"], 128)
            await socket.send(
                json.dumps({"type": "ready", "protocol_version": 5, "max_in_flight": 1})
            )
            await socket.send(json.dumps(slow))
            self.assertEqual(json.loads(await socket.recv())["type"], "ack")
            self.assertTrue(await asyncio.to_thread(self.slow_started.wait, 2))
            await socket.send(json.dumps(fast))
            await socket.wait_closed()

        async with serve(
            handler, "127.0.0.1", 0, ssl=self.tls, subprotocols=["guild-tunnel-v5"]
        ) as server:
            try:
                connector = self.connector(
                    server.sockets[0].getsockname()[1], max_in_flight=128
                )
                with self.assertRaisesRegex(
                    ProtocolError, "exceeded negotiated request capacity"
                ):
                    await asyncio.wait_for(connector.run_forever(asyncio.Event()), 3)
            finally:
                self.release_slow.set()
        self.assertEqual(self.requests, ["/slow"])

    async def test_legacy_handshake_is_rejected_without_forwarding(self):
        async def handler(socket):
            await socket.recv()
            await socket.send(
                json.dumps({"type": "ready", "protocol_version": 1, "max_in_flight": 2})
            )
            await socket.wait_closed()

        async with serve(
            handler, "127.0.0.1", 0, ssl=self.tls, subprotocols=["guild-tunnel-v5"]
        ) as server:
            connector = self.connector(server.sockets[0].getsockname()[1])
            with self.assertRaisesRegex(
                ProtocolError, "Invalid Guild tunnel gateway connection handshake"
            ):
                await asyncio.wait_for(connector.run_forever(asyncio.Event()), 3)
            self.assertEqual(self.requests, [])

    async def test_token_refresh_keeps_requests_and_same_connection_alive(self):
        stop, rotated = asyncio.Event(), asyncio.Event()
        refreshed = asyncio.get_running_loop().create_future()
        connections = 0
        request = self.request("/slow")
        credential = ["old-credential", datetime.now(UTC) + timedelta(minutes=1)]

        async def handler(socket):
            nonlocal connections
            connections += 1
            try:
                await socket.recv()
                await socket.send(
                    json.dumps(
                        {"type": "ready", "protocol_version": 5, "max_in_flight": 2}
                    )
                )
                await socket.send(json.dumps(request))
                self.assertEqual(json.loads(await socket.recv())["type"], "ack")
                self.assertTrue(await asyncio.to_thread(self.slow_started.wait, 2))
                credential[:] = [
                    "new-credential",
                    datetime.now(UTC) + timedelta(minutes=5),
                ]
                rotated.set()
                auth = json.loads(await socket.recv())
                self.assertEqual(
                    auth, {"type": "authenticate", "token": "new-credential"}
                )
                await socket.send(
                    json.dumps(
                        {
                            "type": "authenticated",
                            "expires_at": credential[1].isoformat(),
                        }
                    )
                )
                await socket.send(
                    json.dumps({"type": "ping", "nonce": "during-renewal"})
                )
                self.assertEqual(json.loads(await socket.recv())["type"], "pong")
                self.release_slow.set()
                self.assertEqual(json.loads(await socket.recv())["type"], "response")
                refreshed.set_result(None)
                await stop.wait()
            except Exception as error:
                if not refreshed.done():
                    refreshed.set_exception(error)

        async with serve(
            handler, "127.0.0.1", 0, ssl=self.tls, subprotocols=["guild-tunnel-v5"]
        ) as server:
            task = asyncio.create_task(
                self.connector(
                    server.sockets[0].getsockname()[1],
                    rotated=rotated,
                    credential=lambda: tuple(credential),
                ).run_forever(stop)
            )
            try:
                await asyncio.wait_for(refreshed, 4)
                self.assertEqual(self.requests, ["/slow"])
                self.assertEqual(connections, 1)
                self.assertFalse(rotated.is_set())
            finally:
                stop.set()
                await asyncio.wait_for(task, 2)

    async def test_tls_socket_pushes_concurrent_requests_and_heartbeats(self):
        finished = asyncio.get_running_loop().create_future()
        stop = asyncio.Event()
        slow, fast = self.request("/slow"), self.request("/fast")

        async def handler(socket):
            try:
                self.assertFalse(socket.transport.get_extra_info("peercert"))
                self.assertEqual(
                    socket.request.headers["Authorization"],
                    "Bearer test-credential",
                )
                hello = json.loads(await socket.recv())
                self.assertEqual(
                    hello, {"type": "hello", "protocol_version": 5, "max_in_flight": 2}
                )
                await socket.send(
                    json.dumps(
                        {"type": "ready", "protocol_version": 5, "max_in_flight": 2}
                    )
                )
                await socket.send(json.dumps(slow))
                await socket.send(json.dumps(fast))
                await socket.send(json.dumps({"type": "ping", "nonce": "live"}))
                responses, acknowledgements, pong = [], set(), False
                while len(responses) < 2 or not pong:
                    frame = json.loads(await asyncio.wait_for(socket.recv(), 3))
                    if frame["type"] == "ack":
                        acknowledgements.add(frame["id"])
                    elif frame["type"] == "pong":
                        self.assertEqual(frame["nonce"], "live")
                        pong = True
                    else:
                        self.assertEqual(frame["type"], "response")
                        self.assertIn(frame["id"], acknowledgements)
                        responses.append(frame)
                        self.release_slow.set()
                self.assertEqual(
                    [frame["id"] for frame in responses], [fast["id"], slow["id"]]
                )
                self.assertEqual(
                    b64decode(responses[0]["response_body_base64"]), b"/fast"
                )
                finished.set_result(None)
                await stop.wait()
            except Exception as error:
                if not finished.done():
                    finished.set_exception(error)

        async with serve(
            handler, "127.0.0.1", 0, ssl=self.tls, subprotocols=["guild-tunnel-v5"]
        ) as server:
            task = asyncio.create_task(
                self.connector(server.sockets[0].getsockname()[1]).run_forever(stop)
            )
            try:
                await asyncio.wait_for(finished, 5)
            finally:
                stop.set()
                await asyncio.wait_for(task, 3)

    async def test_stream_delivers_first_token_before_upstream_finishes(self):
        stop = asyncio.Event()
        first_chunk = asyncio.get_running_loop().create_future()
        finished = asyncio.get_running_loop().create_future()
        request = self.request("/stream")
        request["stream_response"] = True

        async def handler(socket):
            try:
                await socket.recv()
                await socket.send(
                    json.dumps(
                        {"type": "ready", "protocol_version": 5, "max_in_flight": 2}
                    )
                )
                await socket.send(json.dumps(request))
                ack = json.loads(await socket.recv())
                self.assertEqual(ack, {"type": "ack", "id": request["id"]})
                start = json.loads(await socket.recv())
                self.assertEqual(start["type"], "response_start")
                self.assertEqual(start["response_status"], 200)
                self.assertEqual(
                    start["response_headers"]["content-type"], "text/event-stream"
                )
                chunk = json.loads(await socket.recv())
                self.assertEqual(chunk["type"], "response_chunk")
                self.assertEqual(
                    b64decode(chunk["response_body_base64"]), b"data: first\n\n"
                )
                first_chunk.set_result(None)
                chunk = json.loads(await socket.recv())
                self.assertEqual(chunk["type"], "response_chunk")
                self.assertEqual(
                    b64decode(chunk["response_body_base64"]), b"data: second\n\n"
                )
                end = json.loads(await socket.recv())
                self.assertEqual(end, {"type": "response_end", "id": request["id"]})
                finished.set_result(None)
                await stop.wait()
            except Exception as error:
                for future in (first_chunk, finished):
                    if not future.done():
                        future.set_exception(error)

        async with serve(
            handler, "127.0.0.1", 0, ssl=self.tls, subprotocols=["guild-tunnel-v5"]
        ) as server:
            task = asyncio.create_task(
                self.connector(server.sockets[0].getsockname()[1]).run_forever(stop)
            )
            try:
                await asyncio.wait_for(first_chunk, 5)
                self.assertFalse(self.release_slow.is_set())
                self.release_slow.set()
                await asyncio.wait_for(finished, 5)
            finally:
                self.release_slow.set()
                stop.set()
                await asyncio.wait_for(task, 3)

    async def test_expired_credential_closes_connection_without_forwarding_or_replay(
        self,
    ):
        stop = asyncio.Event()
        closed = asyncio.get_running_loop().create_future()
        credential = ("short-credential", datetime.now(UTC) + timedelta(seconds=0.4))
        connections = 0

        async def handler(socket):
            nonlocal connections
            connections += 1
            await socket.recv()
            await socket.send(
                json.dumps({"type": "ready", "protocol_version": 5, "max_in_flight": 2})
            )
            await socket.wait_closed()
            if not closed.done():
                closed.set_result(None)

        async with serve(
            handler, "127.0.0.1", 0, ssl=self.tls, subprotocols=["guild-tunnel-v5"]
        ) as server:
            connector = self.connector(
                server.sockets[0].getsockname()[1], credential=lambda: credential
            )
            task = asyncio.create_task(connector.run_forever(stop))
            try:
                with self.assertLogs("guild_connector.tunnel_client", level="WARNING"):
                    await asyncio.wait_for(closed, 2)
                    await asyncio.sleep(1.2)
                self.assertEqual(connections, 1)
                self.assertFalse(connector._tunnel.connected)
                self.assertEqual(self.requests, [])
            finally:
                stop.set()
                await asyncio.wait_for(task, 2)

    async def test_unsolicited_authentication_acknowledgement_is_rejected(self):
        async def handler(socket):
            await socket.recv()
            await socket.send(
                json.dumps({"type": "ready", "protocol_version": 5, "max_in_flight": 2})
            )
            await socket.send(
                json.dumps(
                    {
                        "type": "authenticated",
                        "expires_at": self.credential()[1].isoformat(),
                    }
                )
            )
            await socket.wait_closed()

        async with serve(
            handler, "127.0.0.1", 0, ssl=self.tls, subprotocols=["guild-tunnel-v5"]
        ) as server:
            connector = self.connector(server.sockets[0].getsockname()[1])
            with self.assertRaisesRegex(ProtocolError, "Unexpected authentication"):
                await asyncio.wait_for(connector.run_forever(asyncio.Event()), 2)
        self.assertEqual(self.requests, [])

    async def test_cancel_keeps_connection_available_for_next_request(self):
        finished = asyncio.get_running_loop().create_future()
        stop = asyncio.Event()
        slow, fast = self.request("/slow"), self.request("/fast")

        async def handler(socket):
            try:
                await socket.recv()
                await socket.send(
                    json.dumps(
                        {"type": "ready", "protocol_version": 5, "max_in_flight": 1}
                    )
                )
                await socket.send(json.dumps(slow))
                self.assertEqual(json.loads(await socket.recv())["type"], "ack")
                self.assertTrue(await asyncio.to_thread(self.slow_started.wait, 2))
                await socket.send(json.dumps({"type": "cancel", "id": slow["id"]}))
                await socket.send(json.dumps(fast))
                while True:
                    frame = json.loads(await asyncio.wait_for(socket.recv(), 3))
                    if frame["type"] == "response":
                        self.assertEqual(frame["id"], fast["id"])
                        break
                finished.set_result(None)
                await stop.wait()
            except Exception as error:
                if not finished.done():
                    finished.set_exception(error)

        async with serve(
            handler, "127.0.0.1", 0, ssl=self.tls, subprotocols=["guild-tunnel-v5"]
        ) as server:
            task = asyncio.create_task(
                self.connector(
                    server.sockets[0].getsockname()[1], max_in_flight=1
                ).run_forever(stop)
            )
            try:
                await asyncio.wait_for(finished, 5)
            finally:
                stop.set()
                await asyncio.wait_for(task, 3)

    async def test_large_response_preserves_other_requests_heartbeat_and_connection(
        self,
    ):
        finished = asyncio.get_running_loop().create_future()
        stop = asyncio.Event()
        slow, large, fast = (
            self.request("/slow"),
            self.request("/large"),
            self.request("/fast"),
        )
        connections = 0

        async def handler(socket):
            nonlocal connections
            connections += 1
            try:
                await socket.recv()
                await socket.send(
                    json.dumps(
                        {"type": "ready", "protocol_version": 5, "max_in_flight": 2}
                    )
                )
                await socket.send(json.dumps(slow))
                self.assertEqual(json.loads(await socket.recv())["type"], "ack")
                self.assertTrue(await asyncio.to_thread(self.slow_started.wait, 2))
                await socket.send(json.dumps(large))
                await socket.send(json.dumps({"type": "ping", "nonce": "during-large"}))
                failed, pong, acknowledged = False, False, False
                while not (failed and pong):
                    frame = json.loads(await asyncio.wait_for(socket.recv(), 3))
                    if frame["type"] == "ack":
                        self.assertEqual(frame["id"], large["id"])
                        acknowledged = True
                    elif frame["type"] == "pong":
                        self.assertEqual(frame["nonce"], "during-large")
                        pong = True
                    else:
                        self.assertTrue(acknowledged)
                        self.assertEqual(
                            frame,
                            {
                                "type": "error",
                                "id": large["id"],
                                "error_code": "response_too_large",
                            },
                        )
                        failed = True
                self.release_slow.set()
                await socket.send(json.dumps(fast))
                responses = {}
                while len(responses) < 2:
                    frame = json.loads(await asyncio.wait_for(socket.recv(), 3))
                    if frame["type"] == "ack":
                        self.assertEqual(frame["id"], fast["id"])
                    else:
                        self.assertEqual(frame["type"], "response")
                        responses[frame["id"]] = b64decode(
                            frame["response_body_base64"]
                        )
                self.assertEqual(
                    responses, {slow["id"]: b"/slow", fast["id"]: b"/fast"}
                )
                self.assertEqual(connections, 1)
                finished.set_result(None)
                await stop.wait()
            except Exception as error:
                if not finished.done():
                    finished.set_exception(error)

        async with serve(
            handler, "127.0.0.1", 0, ssl=self.tls, subprotocols=["guild-tunnel-v5"]
        ) as server:
            task = asyncio.create_task(
                self.connector(server.sockets[0].getsockname()[1]).run_forever(stop)
            )
            try:
                await asyncio.wait_for(finished, 8)
                self.assertFalse(task.done())
            finally:
                self.release_slow.set()
                stop.set()
                await asyncio.wait_for(task, 3)

    async def test_disconnect_reconnects_without_replaying_in_flight_request(self):
        finished = asyncio.get_running_loop().create_future()
        stop = asyncio.Event()
        slow, fast = self.request("/slow"), self.request("/fast")
        connections = 0

        async def handler(socket):
            nonlocal connections
            connections += 1
            number = connections
            try:
                await socket.recv()
                await socket.send(
                    json.dumps(
                        {"type": "ready", "protocol_version": 5, "max_in_flight": 2}
                    )
                )
                if number == 1:
                    await socket.send(json.dumps(slow))
                    self.assertEqual(json.loads(await socket.recv())["type"], "ack")
                    self.assertTrue(await asyncio.to_thread(self.slow_started.wait, 2))
                    await socket.close(code=1011)
                    return
                self.assertEqual(number, 2)
                await socket.send(json.dumps(fast))
                while True:
                    frame = json.loads(await asyncio.wait_for(socket.recv(), 3))
                    self.assertEqual(frame["id"], fast["id"])
                    if frame["type"] == "response":
                        self.assertEqual(self.requests.count("/slow"), 1)
                        self.assertEqual(self.requests.count("/fast"), 1)
                        break
                finished.set_result(None)
                await stop.wait()
            except Exception as error:
                if not finished.done():
                    finished.set_exception(error)

        async with serve(
            handler, "127.0.0.1", 0, ssl=self.tls, subprotocols=["guild-tunnel-v5"]
        ) as server:
            task = asyncio.create_task(
                self.connector(server.sockets[0].getsockname()[1]).run_forever(stop)
            )
            try:
                with self.assertLogs(
                    "guild_connector.tunnel_client", level="WARNING"
                ) as logs:
                    await asyncio.wait_for(finished, 8)
                self.assertTrue(
                    any("reason=websocket_close_1011" in line for line in logs.output)
                )
            finally:
                stop.set()
                await asyncio.wait_for(task, 3)

    async def test_connector_rejects_untrusted_gateway_before_forwarding(self):
        await self.reject_invalid_gateway(trust_server=False)

    async def test_connector_rejects_gateway_redirects_before_sending_credentials(self):
        reached = []

        async def handler(socket):
            reached.append(socket.request.path)

        async with serve(handler, "127.0.0.1", 0, ssl=self.tls) as destination:
            port = destination.sockets[0].getsockname()[1]
            for status in (300, 301, 302, 303, 307, 308):
                for cross_origin in (False, True):
                    with self.subTest(status=status, cross_origin=cross_origin):
                        paths = []

                        def redirect(connection, request):
                            paths.append(request.path)
                            response = connection.respond(
                                HTTPStatus(status), "redirect"
                            )
                            response.headers["Location"] = (
                                f"wss://127.0.0.1:{port}/redirected"
                                if cross_origin
                                else "/redirected"
                            )
                            return response

                        async with serve(
                            handler,
                            "127.0.0.1",
                            0,
                            ssl=self.tls,
                            process_request=redirect,
                        ) as origin:
                            rotated = asyncio.Event()
                            rotated.set()
                            connector = self.connector(
                                origin.sockets[0].getsockname()[1], rotated=rotated
                            )
                            with self.assertRaisesRegex(
                                ProtocolError, "redirects are not allowed"
                            ):
                                await asyncio.wait_for(
                                    connector.run_forever(asyncio.Event()), 3
                                )
                        self.assertEqual(len(paths), 1)
                        self.assertNotEqual(paths[0], "/redirected")
        self.assertEqual(reached, [])
        self.assertEqual(self.requests, [])

    async def test_default_trust_rejects_private_gateway_before_sending_credential(
        self,
    ):
        connected = []

        async def handler(socket):
            connected.append(socket)

        async with serve(
            handler, "127.0.0.1", 0, ssl=self.tls, subprotocols=["guild-tunnel-v5"]
        ) as server:
            connector = PrivateConnector(
                tunnel=TunnelClient(
                    base_url=f"https://127.0.0.1:{server.sockets[0].getsockname()[1]}",
                    credential=self.credential,
                ),
                destinations={},
            )
            with self.assertRaisesRegex(
                ProtocolError, "certificate verification failed"
            ):
                await asyncio.wait_for(connector.run_forever(asyncio.Event()), 3)
        self.assertEqual(connected, [])
        self.assertEqual(self.requests, [])

    async def test_connector_rejects_wrong_gateway_hostname_before_forwarding(self):
        await self.reject_invalid_gateway(trust_server=True)

    async def reject_invalid_gateway(self, *, trust_server):
        with tempfile.TemporaryDirectory(prefix="guild-untrusted-gateway-") as folder:
            certificate, key = Path(folder) / "server.pem", Path(folder) / "key.pem"
            subprocess.run(
                [
                    "openssl",
                    "req",
                    "-x509",
                    "-newkey",
                    "rsa:2048",
                    "-nodes",
                    "-days",
                    "1",
                    "-subj",
                    "/CN=wrong-gateway.invalid",
                    "-addext",
                    "subjectAltName=DNS:wrong-gateway.invalid",
                    "-keyout",
                    str(key),
                    "-out",
                    str(certificate),
                ],
                check=True,
                capture_output=True,
            )
            self.tls.load_cert_chain(certificate, key)
            connections = []

            async def handler(socket):
                connections.append(socket)
                await socket.recv()
                await socket.send(
                    json.dumps(
                        {
                            "type": "ready",
                            "protocol_version": 5,
                            "max_in_flight": 2,
                        }
                    )
                )
                await socket.send(json.dumps(self.request("/unauthorized")))

            ca_file = str(certificate if trust_server else self.cert)
            async with serve(
                handler, "127.0.0.1", 0, ssl=self.tls, subprotocols=["guild-tunnel-v5"]
            ) as server:
                connector = self.connector(
                    server.sockets[0].getsockname()[1], ca_file=ca_file
                )
                with self.assertRaisesRegex(
                    ProtocolError, "certificate verification failed"
                ):
                    await asyncio.wait_for(connector.run_forever(asyncio.Event()), 3)
            self.assertEqual(connections, [])
            self.assertEqual(self.requests, [])
