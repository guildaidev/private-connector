from __future__ import annotations

import asyncio
import base64
import unittest
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from unittest.mock import Mock
from uuid import uuid4

from guild_connector.connector import PrivateConnector
from guild_connector.models import Delivery, Destination


class MethodPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls = []
        calls = self.calls

        class Handler(BaseHTTPRequestHandler):
            def respond(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                calls.append((self.command, body))
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = do_DELETE = respond

            def log_message(self, *_args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        origin = f"http://127.0.0.1:{self.server.server_port}"
        self.destinations = {
            name: Destination(name, origin) for name in ("first", "second")
        }

    async def asyncTearDown(self):
        await asyncio.to_thread(self.server.shutdown)
        self.server.server_close()
        self.thread.join()

    async def call(
        self, method, *, blocked=("delete",), target="first", stream=False, headers=None
    ):
        client = PrivateConnector(
            tunnel=Mock(), destinations=self.destinations, blocked_methods=blocked
        )
        request = Delivery(
            str(uuid4()),
            target,
            method,
            "/",
            None,
            headers or {},
            b"payload",
            datetime.now(UTC) + timedelta(seconds=30),
            stream_response=stream,
        )
        try:
            if not stream:
                response = await client.forward(request)
                return response.status, response.body
            frames = [f async for f in client.stream_forward(request)]
            self.assertEqual(frames[-1]["type"], "response_end")
            body = b"".join(
                base64.b64decode(f["response_body_base64"])
                for f in frames
                if f["type"] == "response_chunk"
            )
            return frames[0]["response_status"], body
        finally:
            await client._forwarder.aclose()

    async def test_blocked_method_never_reaches_any_target_in_either_mode(self):
        for target in self.destinations:
            for stream in (False, True):
                with self.subTest(target=target, stream=stream):
                    status, body = await self.call(
                        "DELETE", target=target, stream=stream
                    )
                    self.assertEqual(status, 403)
                    self.assertIn(b"HTTP method policy", body)
        self.assertEqual(self.calls, [])

    async def test_allowed_methods_and_unconfigured_policy_preserve_requests(self):
        self.assertEqual(await self.call("GET"), (200, b"payload"))
        self.assertEqual(await self.call("POST", stream=True), (200, b"payload"))
        self.assertEqual(await self.call("DELETE", blocked=()), (200, b"payload"))
        self.assertEqual(
            self.calls,
            [("GET", b"payload"), ("POST", b"payload"), ("DELETE", b"payload")],
        )

    async def test_method_override_headers_cannot_bypass_policy(self):
        for header in ("X-hTtP-MeThOd-Override", "X-Method-Override", "X-HTTP-Method"):
            for stream in (False, True):
                with self.subTest(header=header, stream=stream):
                    status, _body = await self.call(
                        "POST", headers={header: "DELETE"}, stream=stream
                    )
                    self.assertEqual(status, 403)
        self.assertEqual(self.calls, [])
