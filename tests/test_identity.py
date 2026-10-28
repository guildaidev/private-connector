from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from secrets import token_urlsafe
from unittest.mock import patch
from uuid import uuid4

import httpx

from guild_connector.identity import Identity, identity_lock, renew_identity
from guild_connector.models import ProtocolError


class IdentityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.files = tempfile.TemporaryDirectory()
        self.addCleanup(self.files.cleanup)
        self.directory = Path(self.files.name) / "identity"
        self.tunnel_id = str(uuid4())
        self.identity = Identity.create(
            str(uuid4()),
            "https://core.example.test",
            "future-token-format.with:separators",
        )

    def token_response(self):
        return {
            "status": "issued",
            "tunnel_id": self.tunnel_id,
            "connector_token": "gldc_private-tunnel-token",
            "expires_at": (datetime.now(UTC) + timedelta(days=90)).isoformat(),
            "gateway_url": "https://tunnel.example.test",
        }

    def test_private_state_and_exclusive_lock(self):
        with identity_lock(self.directory):
            self.identity.save(self.directory)
            self.assertIsNone(self.identity.tunnel_id)
            with self.assertRaisesRegex(ProtocolError, "Run connector enrollment"):
                self.identity.credential()
            self.assertEqual(
                os.stat(self.directory / "identity.json").st_mode & 0o777, 0o600
            )
            self.assertNotIn(self.identity.enrollment_token, repr(self.identity))
            with self.assertRaises(ProtocolError):
                with identity_lock(self.directory):
                    pass
        self.directory.chmod(0o755)
        with self.assertRaises(ProtocolError):
            with identity_lock(self.directory):
                pass

    def test_create_rejects_empty_or_control_character_tokens(self):
        for token in (
            "",
            "  ",
            "private\nsecret",
            "private\x00secret",
            "private\x7f",
            "private\x85secret",
        ):
            with self.subTest(token=repr(token)):
                with self.assertRaisesRegex(ProtocolError, "Invalid enrollment token"):
                    Identity.create(str(uuid4()), "https://core.example.test", token)

    async def issue(self, response):
        client = httpx.AsyncClient(transport=httpx.MockTransport(response))
        with patch("guild_connector.identity.httpx.AsyncClient", return_value=client):
            return await self.identity.issue(self.directory)

    async def test_enrollment_persists_tokens_and_removes_grant(self):
        issued, observed = self.token_response(), []
        grant = self.identity.enrollment_token

        def respond(request):
            observed.append(request)
            return httpx.Response(200, json=issued)

        with identity_lock(self.directory):
            self.identity.save(self.directory)
            self.assertTrue(await self.issue(respond))
        self.assertEqual(
            json.loads(observed[0].content), {"request_id": self.identity.request_id}
        )
        self.assertEqual(observed[0].headers["Authorization"], f"Bearer {grant}")
        self.assertEqual(observed[0].url.path, "/v1/customer_connectors/enroll")
        persisted = Identity(
            **json.loads((self.directory / "identity.json").read_text())
        )
        self.assertIsNone(persisted.enrollment_token)
        self.assertEqual(persisted.tunnel_id, issued["tunnel_id"])
        self.assertEqual(self.identity.tunnel_id, issued["tunnel_id"])
        self.assertEqual(persisted.connector_token, issued["connector_token"])
        self.assertEqual(persisted.credential()[0], issued["connector_token"])
        self.assertGreater(persisted.seconds_until_renewal(), 82 * 24 * 3600)

    async def test_lost_response_retries_same_persisted_request_id(self):
        observed = []

        def lost(request):
            observed.append(request)
            raise httpx.ReadError("response lost", request=request)

        with identity_lock(self.directory):
            self.identity.save(self.directory)
            with self.assertRaises(httpx.ReadError):
                await self.issue(lost)
            self.identity = Identity(
                **json.loads((self.directory / "identity.json").read_text())
            )
            self.assertIsNone(self.identity.tunnel_id)

            def recovered(request):
                observed.append(request)
                return httpx.Response(200, json=self.token_response())

            self.assertTrue(await self.issue(recovered))
        self.assertEqual(observed[0].content, observed[1].content)
        self.assertEqual(
            observed[0].headers["Authorization"], observed[1].headers["Authorization"]
        )
        self.assertEqual(self.identity.tunnel_id, self.tunnel_id)

    async def test_enrollment_persists_gateway_path_and_query(self):
        gateway_url = (
            "https://tunnel.example.test/edge/connectors/"
            "?region=us&hint=a%2Fb&region=eu"
        )
        with identity_lock(self.directory):
            self.identity.save(self.directory)
            await self.issue(
                lambda _: httpx.Response(
                    200, json=self.token_response() | {"gateway_url": gateway_url}
                )
            )
        persisted = Identity(
            **json.loads((self.directory / "identity.json").read_text())
        )
        self.assertEqual(persisted.gateway_url, gateway_url)
        self.assertEqual(self.identity.gateway_url, gateway_url)

    async def test_older_pending_identity_requires_matching_tunnel_id(self):
        legacy_tunnel_id = str(uuid4())
        self.identity.tunnel_id = legacy_tunnel_id
        self.identity.enrollment_token = f"gce:{legacy_tunnel_id}:{token_urlsafe(32)}"
        with identity_lock(self.directory):
            self.identity.save(self.directory)
            self.identity = Identity(
                **json.loads((self.directory / "identity.json").read_text())
            )
            request_id = self.identity.request_id
            before = (self.directory / "identity.json").read_bytes()
            with self.assertRaisesRegex(ProtocolError, "does not match this tunnel"):
                await self.issue(
                    lambda _: httpx.Response(200, json=self.token_response())
                )
            self.assertEqual((self.directory / "identity.json").read_bytes(), before)
            self.assertEqual(self.identity.tunnel_id, legacy_tunnel_id)
            self.assertIsNone(self.identity.connector_token)
            self.assertTrue(
                await self.issue(
                    lambda _: httpx.Response(
                        200,
                        json=self.token_response() | {"tunnel_id": legacy_tunnel_id},
                    )
                )
            )
        persisted = Identity(
            **json.loads((self.directory / "identity.json").read_text())
        )
        self.assertEqual(persisted.request_id, request_id)
        self.assertEqual(persisted.tunnel_id, legacy_tunnel_id)

    async def test_renewal_reuses_the_connector_credential(self):
        with identity_lock(self.directory):
            await self.issue(lambda _: httpx.Response(200, json=self.token_response()))
            old = self.identity.connector_token
            observed = []

            def respond(request):
                observed.append(request)
                result = self.token_response() | {"connector_token": "gldc_replacement"}
                return httpx.Response(200, json=result)

            await self.issue(respond)
        self.assertEqual(
            observed[0].url.path,
            "/v1/customer_connectors/renew",
        )
        self.assertEqual(observed[0].content, b"")
        self.assertNotIn("Content-Type", observed[0].headers)
        self.assertEqual(observed[0].headers["Authorization"], f"Bearer {old}")
        self.assertEqual(self.identity.connector_token, "gldc_replacement")

    async def test_issued_connector_tokens_accept_changed_prefixes(self):
        observed = []
        first = "next_private-tunnel-token"
        replacement = "v2_replacement-token"

        def respond(request):
            observed.append(request)
            token = first if len(observed) == 1 else replacement
            return httpx.Response(
                200, json=self.token_response() | {"connector_token": token}
            )

        with identity_lock(self.directory):
            await self.issue(respond)
            persisted = Identity(
                **json.loads((self.directory / "identity.json").read_text())
            )
            self.assertEqual(persisted.credential()[0], first)
            await self.issue(respond)
        self.assertEqual(observed[1].headers["Authorization"], f"Bearer {first}")
        persisted = Identity(
            **json.loads((self.directory / "identity.json").read_text())
        )
        self.assertEqual(persisted.credential()[0], replacement)

    async def test_invalid_or_foreign_response_preserves_private_state(self):
        cases = [
            {"tunnel_id": None},
            {"tunnel_id": 123},
            {"tunnel_id": "not-a-uuid"},
            {"tunnel_id": "00000000-0000-0000-0000-000000000000"},
            {"status": "rejected"},
            {"expires_at": "not-a-date"},
            {"expires_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat()},
            {"gateway_url": "http://insecure.test"},
            {"gateway_url": "https://[bad"},
            {"gateway_url": "https://user@gateway.test/path?region=us"},
            {"gateway_url": "https://gateway.test/path?region=us#fragment"},
            {"gateway_url": "https://gateway.test/path?region=us\n"},
            {"connector_token": None},
            {"connector_token": ""},
            {"connector_token": "bad\nheader"},
            {"connector_token": "bad\x00header"},
            {"connector_token": "bad\x7fheader"},
            {"connector_token": "bad header"},
        ]
        with identity_lock(self.directory):
            self.identity.save(self.directory)
            before = (self.directory / "identity.json").read_bytes()
            for invalid in cases:
                with self.subTest(invalid=invalid):
                    with self.assertRaises(ProtocolError):
                        await self.issue(
                            lambda _: httpx.Response(
                                200, json=self.token_response() | invalid
                            )
                        )
                    self.assertEqual(
                        (self.directory / "identity.json").read_bytes(), before
                    )
                    self.assertIsNone(self.identity.tunnel_id)

            for missing in ("tunnel_id", "expires_at", "gateway_url"):
                with self.subTest(missing=missing):
                    malformed = self.token_response()
                    del malformed[missing]
                    with self.assertRaises(ProtocolError):
                        await self.issue(lambda _: httpx.Response(200, json=malformed))
                    self.assertEqual(
                        (self.directory / "identity.json").read_bytes(), before
                    )
            with self.assertRaisesRegex(ProtocolError, "Invalid enrollment response"):
                await self.issue(lambda _: httpx.Response(200, text="not-json"))
            self.assertEqual((self.directory / "identity.json").read_bytes(), before)

    async def test_renewal_rejects_tunnel_mismatch(self):
        with identity_lock(self.directory):
            await self.issue(lambda _: httpx.Response(200, json=self.token_response()))
            before = (self.directory / "identity.json").read_bytes()
            with self.assertRaisesRegex(ProtocolError, "does not match this tunnel"):
                await self.issue(
                    lambda _: httpx.Response(
                        200,
                        json=self.token_response() | {"tunnel_id": str(uuid4())},
                    )
                )
            self.assertEqual((self.directory / "identity.json").read_bytes(), before)
            self.assertEqual(self.identity.tunnel_id, self.tunnel_id)

    async def test_enrollment_rejection_includes_status_without_response_contents(self):
        with identity_lock(self.directory):
            for status in (401, 403, 404, 409):
                with self.subTest(status=status):
                    with self.assertRaises(ProtocolError) as raised:
                        await self.issue(
                            lambda _: httpx.Response(status, text="private-response")
                        )
                    self.assertEqual(
                        str(raised.exception),
                        f"Connector enroll request was rejected with HTTP {status}; "
                        "check the tunnel in Guild",
                    )

    async def test_renewal_notifies_live_tunnel(self):
        stop, rotated = asyncio.Event(), asyncio.Event()
        with identity_lock(self.directory):
            client = httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda _: httpx.Response(200, json=self.token_response())
                )
            )
            with patch(
                "guild_connector.identity.httpx.AsyncClient", return_value=client
            ):
                task = asyncio.create_task(
                    renew_identity(self.identity, self.directory, stop, rotated)
                )
                await asyncio.wait_for(rotated.wait(), 2)
                stop.set()
                await task
        self.assertEqual(self.identity.credential()[0], "gldc_private-tunnel-token")

    async def test_authorization_loss_requests_db_checked_renewal_before_expiry(self):
        stop, rotated, refresh = asyncio.Event(), asyncio.Event(), asyncio.Event()
        observed = []
        with identity_lock(self.directory):
            await self.issue(lambda _: httpx.Response(200, json=self.token_response()))
            previous = self.identity.connector_token

            def respond(request):
                observed.append(request)
                return httpx.Response(200, json=self.token_response())

            client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
            with patch(
                "guild_connector.identity.httpx.AsyncClient", return_value=client
            ):
                task = asyncio.create_task(
                    renew_identity(
                        self.identity,
                        self.directory,
                        stop,
                        rotated,
                        refresh_requested=refresh,
                    )
                )
                try:
                    refresh.set()
                    await asyncio.wait_for(rotated.wait(), 2)
                finally:
                    stop.set()
                    await task
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0].url.path, "/v1/customer_connectors/renew")
        self.assertEqual(observed[0].headers["Authorization"], f"Bearer {previous}")
        self.assertEqual(observed[0].content, b"")

    async def test_expired_credential_requires_explicit_reenrollment_without_network(
        self,
    ):
        self.identity.enrollment_token = None
        self.identity.connector_token = "gldc_expired"
        self.identity.expires_at = (
            datetime.now(UTC) - timedelta(seconds=1)
        ).isoformat()
        with patch("guild_connector.identity.httpx.AsyncClient") as client:
            with self.assertRaisesRegex(ProtocolError, "expired"):
                await self.identity.issue(self.directory)
            client.assert_not_called()

    async def test_authorization_repair_retries_after_temporary_renewal_failure(self):
        stop, rotated, refresh = asyncio.Event(), asyncio.Event(), asyncio.Event()
        observed = []
        with identity_lock(self.directory):
            await self.issue(lambda _: httpx.Response(200, json=self.token_response()))

            def respond(request):
                observed.append(request)
                return (
                    httpx.Response(503)
                    if len(observed) == 1
                    else httpx.Response(200, json=self.token_response())
                )

            clients = [
                httpx.AsyncClient(transport=httpx.MockTransport(respond))
                for _ in range(2)
            ]
            with patch(
                "guild_connector.identity.httpx.AsyncClient", side_effect=clients
            ):
                task = asyncio.create_task(
                    renew_identity(
                        self.identity,
                        self.directory,
                        stop,
                        rotated,
                        refresh_requested=refresh,
                    )
                )
                try:
                    refresh.set()
                    await asyncio.wait_for(rotated.wait(), 5)
                finally:
                    stop.set()
                    await task
        self.assertEqual(len(observed), 2)

    async def test_insecure_enrollment_url_rejected_before_network(self):
        self.identity.control_plane_url = "http://core.example.test"
        with self.assertRaisesRegex(ProtocolError, "HTTPS"):
            await self.identity.issue(self.directory)

    async def test_enrollment_and_renewal_use_configured_api_base(self):
        for suffix, expected in (
            ("", "/v1"),
            ("/", "/v1"),
            ("/v1", "/v1"),
            ("/v1/", "/v1"),
            ("/api/v2", "/api/v2"),
            ("/api/v2/", "/api/v2"),
        ):
            with self.subTest(suffix=suffix):
                self.identity = Identity.create(
                    str(uuid4()),
                    f"https://core.example.test{suffix}",
                    "next-format:with:multiple:parts",
                )
                observed = []

                def respond(request):
                    observed.append(request)
                    return httpx.Response(200, json=self.token_response())

                with identity_lock(self.directory):
                    await self.issue(respond)
                    await self.issue(respond)
                self.assertEqual(
                    str(observed[0].url),
                    f"https://core.example.test{expected}/customer_connectors/enroll",
                )
                self.assertEqual(
                    str(observed[1].url),
                    f"https://core.example.test{expected}/customer_connectors/renew",
                )

    async def test_invalid_api_bases_rejected_before_network(self):
        for url in (
            "https:///api/v2",
            "https://user:pass@core.example.test/api/v2",
            "https://core.example.test/api/v2?token=secret",
            "https://core.example.test/api/v2?",
            "https://core.example.test/api/v2#fragment",
            "https://core.example.test/api/v2#",
            "https://core.example.test:invalid/api/v2",
            "https://core.example.test/api\n/v2",
        ):
            with self.subTest(url=url):
                self.identity.control_plane_url = url
                with patch("guild_connector.identity.httpx.AsyncClient") as client:
                    with self.assertRaisesRegex(ProtocolError, "HTTPS API base"):
                        await self.identity.issue(self.directory)
                    client.assert_not_called()
