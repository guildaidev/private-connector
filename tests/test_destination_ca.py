from __future__ import annotations

import asyncio
import base64
import ssl
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from unittest.mock import Mock
from uuid import uuid4

from guild_connector.connector import PrivateConnector
from guild_connector.models import Delivery, Destination


class DestinationCaTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.files = tempfile.TemporaryDirectory()
        root = Path(cls.files.name)
        cls.ca = root / "ca.pem"
        for command in (
            [
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-keyout",
                str(root / "ca.key"),
                "-out",
                str(cls.ca),
                "-days",
                "1",
                "-subj",
                "/CN=Test CA",
                "-addext",
                "basicConstraints=critical,CA:TRUE",
                "-addext",
                "keyUsage=critical,keyCertSign,cRLSign",
            ],
            [
                "req",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-keyout",
                str(root / "server.key"),
                "-out",
                str(root / "server.csr"),
                "-subj",
                "/CN=localhost",
            ],
            [
                "x509",
                "-req",
                "-in",
                str(root / "server.csr"),
                "-CA",
                str(cls.ca),
                "-CAkey",
                str(root / "ca.key"),
                "-CAcreateserial",
                "-out",
                str(root / "server.pem"),
                "-days",
                "1",
                "-extfile",
                str(root / "extensions"),
            ],
        ):
            (root / "extensions").write_text(
                "subjectAltName=DNS:localhost\nextendedKeyUsage=serverAuth\n"
            )
            subprocess.run(
                ["openssl", *command],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        cls.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        cls.context.load_cert_chain(root / "server.pem", root / "server.key")

    @classmethod
    def tearDownClass(cls):
        cls.files.cleanup()

    async def asyncSetUp(self):
        self.calls = []
        calls = self.calls

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                calls.append(self.path)
                body = b"data: hello\n\n"
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.socket = self.context.wrap_socket(
            self.server.socket, server_side=True
        )
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    async def asyncTearDown(self):
        await asyncio.to_thread(self.server.shutdown)
        self.server.server_close()
        self.thread.join()

    async def forward(self, *, trust=True, hostname="localhost", stream=False):
        destination = Destination(
            "api", f"https://{hostname}:{self.server.server_port}"
        )
        client = PrivateConnector(
            tunnel=Mock(),
            destinations={"api": destination},
            destination_ca=str(self.ca) if trust else None,
        )
        request = Delivery(
            str(uuid4()),
            "api",
            "GET",
            "/",
            None,
            {},
            b"",
            datetime.now(UTC) + timedelta(seconds=30),
            stream_response=stream,
        )
        try:
            if stream:
                return [frame async for frame in client.stream_forward(request)]
            return await client.forward(request)
        finally:
            await client._forwarder.aclose()

    async def test_private_ca_allows_buffered_and_streaming_https(self):
        response = await self.forward()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body, b"data: hello\n\n")
        frames = await self.forward(stream=True)
        self.assertEqual(frames[0]["response_status"], 200)
        body = b"".join(
            base64.b64decode(f["response_body_base64"])
            for f in frames
            if f["type"] == "response_chunk"
        )
        self.assertEqual(body, b"data: hello\n\n")
        self.assertEqual(frames[-1]["type"], "response_end")
        self.assertEqual(len(self.calls), 2)

    async def test_untrusted_ca_and_wrong_hostname_stay_rejected(self):
        for options in ({"trust": False}, {"hostname": "127.0.0.1"}):
            with self.subTest(options=options):
                response = await self.forward(**options)
                self.assertEqual(response.status, 502)
                self.assertIsNotNone(response.error_code)
                frames = await self.forward(stream=True, **options)
                self.assertEqual(frames[0]["type"], "error")
        self.assertEqual(self.calls, [])
