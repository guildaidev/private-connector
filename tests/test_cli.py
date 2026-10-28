from __future__ import annotations

import errno
import io
import json
import ssl
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from datetime import UTC, datetime, timedelta
from pathlib import Path
from secrets import token_urlsafe
from unittest.mock import patch
from uuid import uuid4

import httpx

from guild_connector.__main__ import main
from guild_connector.identity import Identity, identity_lock


class ConfigurationCliTests(unittest.TestCase):
    def setUp(self):
        self.files = tempfile.TemporaryDirectory()
        self.addCleanup(self.files.cleanup)
        self.path = Path(self.files.name) / "connector.json"
        self.directory = Path(self.files.name) / "identity"
        self.config = {
            "connector_id": str(uuid4()),
            "control_plane_url": "https://core.example.test",
            "identity_directory": str(self.directory),
            "destinations": {"foo": "https://foo.internal"},
        }

    def assert_config_error(self, expected):
        stderr = io.StringIO()
        with patch("sys.argv", ["guild-private-connector", "--config", str(self.path)]):
            with redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
                main()
        self.assertEqual(raised.exception.code, 2)
        self.assertEqual(stderr.getvalue(), expected + "\n")
        self.assertFalse(self.directory.exists())

    def test_missing_file_reports_read_failure(self):
        self.assert_config_error(
            "Cannot read connector configuration: No such file or directory"
        )

    def test_lifecycle_logs_include_a_timestamp_and_timezone(self):
        with patch("guild_connector.__main__.logging.basicConfig") as configure:
            self.assert_config_error(
                "Cannot read connector configuration: No such file or directory"
            )
        self.assertIn("%(asctime)s", configure.call_args.kwargs["format"])
        self.assertIn("%z", configure.call_args.kwargs["datefmt"])

    def test_read_error_does_not_print_exception_contents(self):
        with patch.object(
            Path,
            "read_text",
            side_effect=PermissionError(errno.EACCES, "private-token", "/private-path"),
        ):
            self.assert_config_error(
                "Cannot read connector configuration: Permission denied"
            )

    def test_invalid_json_reports_location_without_contents(self):
        self.path.write_text('{\n  "token": private-token\n}')
        self.assert_config_error(
            "Invalid JSON in connector configuration at line 2, column 12"
        )

    def test_invalid_encoding_reports_expected_encoding(self):
        self.path.write_bytes(b"\xffprivate-token")
        self.assert_config_error("Connector configuration must use UTF-8 encoding")

    def test_configuration_must_be_an_object(self):
        for value in ([], "private-token", None):
            with self.subTest(value=value):
                self.path.write_text(json.dumps(value))
                self.assert_config_error(
                    "Connector configuration must be a JSON object"
                )

    def test_missing_required_settings_are_named(self):
        for field in self.config:
            with self.subTest(field=field):
                config = self.config.copy()
                del config[field]
                self.path.write_text(json.dumps(config))
                self.assert_config_error(f"Missing required setting: {field}")

    def test_required_strings_must_be_non_empty(self):
        for field in ("connector_id", "control_plane_url", "identity_directory"):
            for value in (None, [], 123, "", " "):
                with self.subTest(field=field, value=value):
                    self.path.write_text(json.dumps(self.config | {field: value}))
                    self.assert_config_error(
                        f"Setting {field} must be a non-empty string"
                    )

    def test_destinations_must_be_a_non_empty_object(self):
        for value in (None, [], {}, "private-token"):
            with self.subTest(value=value):
                self.path.write_text(json.dumps(self.config | {"destinations": value}))
                self.assert_config_error(
                    "Setting destinations must be a non-empty JSON object"
                )

    def test_invalid_destination_identifies_entry_without_exposing_values(self):
        for target_id, origin in (
            (
                "foo",
                "https://user:private-token@service.internal/private-data?token=secret",
            ),
            ("foo", "file:///private-data"),
            ("foo", None),
            ("foo", []),
            ("", "https://service.internal"),
            ("x" * 256, "https://service.internal"),
        ):
            with self.subTest(target_id=target_id, origin=origin):
                self.path.write_text(
                    json.dumps(
                        self.config
                        | {
                            "destinations": {
                                "valid": "https://foo.internal",
                                target_id: origin,
                            }
                        }
                    )
                )
                self.assert_config_error(
                    "Invalid destinations entry 2: use a target ID of 1-255 "
                    "characters and an HTTP(S) URL with a hostname and optional port; "
                    "no credentials, path, query, or fragment"
                )


class CliTests(unittest.TestCase):
    def test_help_describes_connecting_customer_services_to_guild(self):
        result = subprocess.run(
            [sys.executable, "-m", "guild_connector", "--help"],
            capture_output=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn(
            "Connect services in your network to Guild.", result.stdout.decode()
        )

    def test_enrolled_restart_needs_no_grant_and_preserves_tunnel(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder) / "identity"
            identity = Identity.create(
                str(uuid4()),
                "https://127.0.0.1:1",
                f"gce:{uuid4()}:{token_urlsafe(32)}",
            )
            identity.enrollment_token = None
            identity.tunnel_id = str(uuid4())
            identity.connector_token = "gldc_private-test-tunnel-token"
            identity.expires_at = (datetime.now(UTC) + timedelta(days=1)).isoformat()
            with identity_lock(directory):
                identity.save(directory)
            before = (directory / "identity.json").read_bytes()
            config = Path(folder) / "connector.json"
            config.write_text(
                json.dumps(
                    {
                        "connector_id": identity.connector_id,
                        "control_plane_url": identity.control_plane_url,
                        "identity_directory": str(directory),
                        "destinations": {"foo": "https://foo.internal"},
                    }
                )
            )
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "guild_connector",
                    "enroll",
                    "--if-needed",
                    "--config",
                    str(config),
                    "--enrollment-token-file",
                    "/nonexistent",
                ],
                capture_output=True,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            self.assertEqual((directory / "identity.json").read_bytes(), before)
            self.assertNotIn(identity.connector_token.encode(), result.stderr)

            configuration = json.loads(config.read_text())
            configuration["connector_id"] = str(uuid4())
            config.write_text(json.dumps(configuration))
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "guild_connector",
                    "enroll",
                    "--if-needed",
                    "--config",
                    str(config),
                ],
                capture_output=True,
                timeout=10,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(
                b"Identity belongs to another connector or control plane", result.stderr
            )
            self.assertEqual((directory / "identity.json").read_bytes(), before)

    def test_missing_token_file_fails_without_creating_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder) / "identity"
            config = Path(folder) / "connector.json"
            config.write_text(
                json.dumps(
                    {
                        "connector_id": str(uuid4()),
                        "control_plane_url": "https://127.0.0.1:1",
                        "identity_directory": str(directory),
                        "destinations": {"foo": "https://foo.internal"},
                    }
                )
            )
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "guild_connector",
                    "enroll",
                    "--if-needed",
                    "--config",
                    str(config),
                    "--enrollment-token-file",
                    "/nonexistent",
                ],
                capture_output=True,
                timeout=10,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(
                result.stderr.decode(),
                "Cannot access connector files: No such file or directory\n",
            )
            self.assertFalse((directory / "identity.json").exists())


class StartupCliTests(unittest.TestCase):
    def setUp(self):
        self.files = tempfile.TemporaryDirectory()
        self.addCleanup(self.files.cleanup)
        self.directory = Path(self.files.name) / "identity"
        self.tunnel_id = str(uuid4())
        self.identity = Identity.create(
            str(uuid4()),
            "https://core.example.test",
            "future-format:with:separators",
        )
        with identity_lock(self.directory):
            self.identity.save(self.directory)
        self.path = Path(self.files.name) / "connector.json"
        self.path.write_text(
            json.dumps(
                {
                    "connector_id": self.identity.connector_id,
                    "control_plane_url": self.identity.control_plane_url,
                    "identity_directory": str(self.directory),
                    "destinations": {"foo": "https://foo.internal"},
                }
            )
        )

    def assert_startup_error(self, expected, command="run", options=()):
        stderr = io.StringIO()
        with patch(
            "sys.argv",
            ["guild-private-connector", command, "--config", str(self.path), *options],
        ):
            with redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
                main()
        self.assertEqual(raised.exception.code, 1)
        self.assertEqual(stderr.getvalue(), expected + "\n")

    def test_identity_directory_must_be_private(self):
        self.directory.chmod(0o755)
        self.assert_startup_error("Identity directory must be private (mode 0700)")

    def test_identity_directory_in_use_reports_conflict(self):
        with identity_lock(self.directory):
            self.assert_startup_error(
                "Another connector is using this identity directory"
            )

    def test_missing_identity_requires_enrollment(self):
        (self.directory / "identity.json").unlink()
        self.assert_startup_error(
            "No saved identity; supply --enrollment-token-file to enroll on startup"
        )

    def test_config_url_change_cannot_send_saved_token_without_migration(self):
        config = json.loads(self.path.read_text())
        config["control_plane_url"] = "https://new.example.test/api/v2"
        self.path.write_text(json.dumps(config))
        before = (self.directory / "identity.json").read_bytes()
        with patch.object(Identity, "issue") as issue:
            for command in ("run", "enroll"):
                with self.subTest(command=command):
                    self.assert_startup_error(
                        "Saved control plane URL differs from config; "
                        "run migrate-control-plane to update it",
                        command,
                    )
            issue.assert_not_called()
        self.assertEqual((self.directory / "identity.json").read_bytes(), before)

    def test_migration_requires_matching_source_target_and_connector(self):
        old_url = self.identity.control_plane_url
        new_url = "https://new.example.test/api/v2"
        config = json.loads(self.path.read_text())
        config["control_plane_url"] = new_url
        self.path.write_text(json.dumps(config))
        before = (self.directory / "identity.json").read_bytes()
        cases = (
            (
                "https://wrong.example.test",
                new_url,
                "Saved control plane URL does not match --from-control-plane-url",
            ),
            (
                old_url,
                "https://wrong.example.test",
                "Configured control plane URL does not match --to-control-plane-url",
            ),
        )
        with patch.object(Identity, "issue") as issue:
            for source, target, message in cases:
                with self.subTest(source=source, target=target):
                    self.assert_startup_error(
                        message,
                        "migrate-control-plane",
                        (
                            "--from-control-plane-url",
                            source,
                            "--to-control-plane-url",
                            target,
                        ),
                    )
            config["connector_id"] = str(uuid4())
            self.path.write_text(json.dumps(config))
            self.assert_startup_error(
                "Identity belongs to another connector or control plane",
                "migrate-control-plane",
                (
                    "--from-control-plane-url",
                    old_url,
                    "--to-control-plane-url",
                    new_url,
                ),
            )
            issue.assert_not_called()
        self.assertEqual((self.directory / "identity.json").read_bytes(), before)

    def test_migration_requires_explicit_source_and_target(self):
        before = (self.directory / "identity.json").read_bytes()
        for options in (
            (),
            ("--from-control-plane-url", self.identity.control_plane_url),
            ("--to-control-plane-url", "https://new.example.test/api/v2"),
        ):
            with self.subTest(options=options):
                stderr = io.StringIO()
                with (
                    patch(
                        "sys.argv",
                        [
                            "guild-private-connector",
                            "migrate-control-plane",
                            "--config",
                            str(self.path),
                            *options,
                        ],
                    ),
                    redirect_stderr(stderr),
                    self.assertRaises(SystemExit) as raised,
                ):
                    main()
                self.assertEqual(raised.exception.code, 2)
                self.assertIn(
                    "requires --from-control-plane-url and --to-control-plane-url",
                    stderr.getvalue(),
                )
                self.assertEqual(
                    (self.directory / "identity.json").read_bytes(), before
                )

    def test_migration_validates_target_and_does_not_create_identity(self):
        old_url = self.identity.control_plane_url
        before = (self.directory / "identity.json").read_bytes()
        config = json.loads(self.path.read_text())
        for target in (
            "http://new.example.test/api/v2",
            "https://user:pass@new.example.test/api/v2",
            "https://new.example.test/api/v2?private=secret",
        ):
            with self.subTest(target=target):
                config["control_plane_url"] = target
                self.path.write_text(json.dumps(config))
                self.assert_startup_error(
                    "Control plane URL must be an HTTPS API base",
                    "migrate-control-plane",
                    (
                        "--from-control-plane-url",
                        old_url,
                        "--to-control-plane-url",
                        target,
                    ),
                )
                self.assertEqual(
                    (self.directory / "identity.json").read_bytes(), before
                )
        (self.directory / "identity.json").unlink()
        config["control_plane_url"] = "https://new.example.test/api/v2"
        self.path.write_text(json.dumps(config))
        self.assert_startup_error(
            "No saved identity to migrate",
            "migrate-control-plane",
            (
                "--from-control-plane-url",
                old_url,
                "--to-control-plane-url",
                config["control_plane_url"],
            ),
        )
        self.assertFalse((self.directory / "identity.json").exists())

    def test_deliberate_migration_preserves_credentials_without_network(self):
        old_url = self.identity.control_plane_url
        new_url = "https://new.example.test/api/v2"
        self.identity.enrollment_token = None
        self.identity.tunnel_id = self.tunnel_id
        self.identity.connector_token = "gldc_private-migration-token"
        self.identity.expires_at = (datetime.now(UTC) + timedelta(days=90)).isoformat()
        self.identity.gateway_url = "https://tunnel.example.test"
        self.identity.save(self.directory)
        config = json.loads(self.path.read_text())
        config["control_plane_url"] = new_url
        self.path.write_text(json.dumps(config))
        before = json.loads((self.directory / "identity.json").read_text())
        with (
            patch(
                "sys.argv",
                [
                    "guild-private-connector",
                    "migrate-control-plane",
                    "--config",
                    str(self.path),
                    "--from-control-plane-url",
                    old_url,
                    "--to-control-plane-url",
                    new_url,
                ],
            ),
            patch.object(Identity, "issue") as issue,
            patch("guild_connector.__main__._run") as run,
            self.assertLogs(level="INFO") as logs,
        ):
            main()
            issue.assert_not_called()
            run.assert_not_called()
        saved = json.loads((self.directory / "identity.json").read_text())
        self.assertEqual(saved, before | {"control_plane_url": new_url})
        self.assertNotIn(before["connector_token"], " ".join(logs.output))

        async def resumed(identity, *_args, **_kwargs):
            self.assertEqual(identity.control_plane_url, new_url)

        with (
            patch("sys.argv", ["guild-private-connector", "--config", str(self.path)]),
            patch("guild_connector.__main__._run", resumed),
        ):
            main()

    def test_pending_identity_resumes_enrollment_before_starting(self):
        async def issue(identity, directory, **kwargs):
            self.assertIsNone(identity.tunnel_id)
            identity.tunnel_id = self.tunnel_id
            identity.enrollment_token = None
            identity.connector_token = "private-token"
            identity.expires_at = (datetime.now(UTC) + timedelta(days=90)).isoformat()
            identity.gateway_url = "https://tunnel.example.test"
            identity.save(directory)
            return True

        async def connected(connector, stop):
            saved = Identity(
                **json.loads((self.directory / "identity.json").read_text())
            )
            self.assertIsNone(saved.enrollment_token)
            self.assertEqual(saved.tunnel_id, self.tunnel_id)

        with (
            patch("sys.argv", ["guild-private-connector", "--config", str(self.path)]),
            patch.object(Identity, "issue", issue),
            patch("guild_connector.__main__.PrivateConnector.run_forever", connected),
            patch("guild_connector.__main__.renew_identity"),
        ):
            main()

    def test_first_start_enrolls_from_file_then_restart_needs_no_token(self):
        (self.directory / "identity.json").unlink()
        token = Path(self.files.name) / "token"
        token.write_text(self.identity.enrollment_token + "\n")
        calls = []

        async def issue(identity, directory, **kwargs):
            calls.append(identity.request_id)
            self.assertIsNone(identity.tunnel_id)
            self.assertEqual(identity.enrollment_token, self.identity.enrollment_token)
            identity.tunnel_id = self.tunnel_id
            identity.enrollment_token = None
            identity.connector_token = "private-token"
            identity.expires_at = (datetime.now(UTC) + timedelta(days=90)).isoformat()
            identity.gateway_url = "https://tunnel.example.test"
            identity.save(directory)
            return True

        with (
            patch(
                "sys.argv",
                [
                    "guild-private-connector",
                    "run",
                    "--config",
                    str(self.path),
                    "--enrollment-token-file",
                    str(token),
                ],
            ),
            patch.object(Identity, "issue", issue),
            patch("guild_connector.__main__.PrivateConnector.run_forever") as run,
            patch("guild_connector.__main__.renew_identity"),
        ):
            main()
            token.unlink()
            main()
        self.assertEqual(len(calls), 1)
        self.assertEqual(run.await_count, 2)
        saved = Identity(**json.loads((self.directory / "identity.json").read_text()))
        self.assertEqual(saved.tunnel_id, self.tunnel_id)

    def test_rejected_automatic_enrollment_does_not_start_tunnel(self):
        request = httpx.Request("POST", "https://core.example.test")
        response = httpx.Response(403, request=request)
        error = httpx.HTTPStatusError("expired", request=request, response=response)
        with (
            patch.object(Identity, "issue", side_effect=error),
            patch("guild_connector.__main__.PrivateConnector.run_forever") as run,
        ):
            self.assert_startup_error("Enrollment failed with HTTP 403")
        run.assert_not_called()

    def test_startup_log_identifies_guild_as_the_connection_target(self):
        self.identity.enrollment_token = None
        self.identity.tunnel_id = self.tunnel_id
        self.identity.connector_token = "private-token"
        self.identity.gateway_url = "https://tunnel.example.test"
        self.identity.expires_at = (datetime.now(UTC) + timedelta(days=1)).isoformat()
        self.identity.save(self.directory)
        with (
            patch("sys.argv", ["guild-private-connector", "--config", str(self.path)]),
            patch("guild_connector.__main__.PrivateConnector.run_forever"),
            patch("guild_connector.__main__.renew_identity"),
            self.assertLogs(level="INFO") as logs,
        ):
            main()
        self.assertIn(
            "Private connector started; connecting to Guild",
            [record.getMessage() for record in logs.records],
        )

    def test_expired_identity_reports_reenrollment_action(self):
        self.identity.enrollment_token = None
        self.identity.tunnel_id = self.tunnel_id
        self.identity.connector_token = "private-token"
        self.identity.expires_at = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        self.identity.save(self.directory)
        self.assert_startup_error(
            "Connector credential expired; create a new enrollment in Guild", "enroll"
        )

    def test_http_status_reports_operation_without_url_or_body(self):
        request = httpx.Request(
            "POST", "https://core.example.test/private-path?token=private-token"
        )
        response = httpx.Response(418, request=request, text="private-response")
        error = httpx.HTTPStatusError(
            "private-error", request=request, response=response
        )
        for command, operation in (("enroll", "Enrollment"), ("run", "Enrollment")):
            with self.subTest(command=command):
                with patch("guild_connector.__main__._run", side_effect=error):
                    self.assert_startup_error(
                        f"{operation} failed with HTTP 418", command
                    )

    def test_transport_errors_report_safe_categories(self):
        tls_error = httpx.ConnectError("private-error")
        tls_error.__cause__ = ssl.SSLCertVerificationError("private-certificate")
        cases = (
            (httpx.ReadTimeout("private-error"), "Enrollment request timed out"),
            (tls_error, "Enrollment TLS handshake failed; check the CA and hostname"),
            (
                httpx.ConnectError("private-error"),
                "Enrollment connection failed; check the service URL and network access",
            ),
            (
                httpx.RemoteProtocolError("private-error"),
                "Enrollment received an invalid HTTP response",
            ),
            (
                httpx.ReadError("private-error"),
                "Enrollment HTTP transfer failed; check network access",
            ),
            (
                ssl.SSLError("private-certificate"),
                "Cannot configure TLS; check the configured CA certificate files",
            ),
            (
                TimeoutError("private-error"),
                "Connector operation timed out; check the preceding connection errors",
            ),
            (
                ValueError("private-identity-data"),
                "Invalid connector identity or enrollment data; check identity.json, "
                "the enrollment token, and connector settings",
            ),
        )
        for error, expected in cases:
            with self.subTest(error=type(error).__name__):
                with patch("guild_connector.__main__._run", side_effect=error):
                    self.assert_startup_error(expected, "enroll")

    def test_missing_interactive_token_explains_token_file_option(self):
        (self.directory / "identity.json").unlink()
        with patch(
            "guild_connector.__main__.getpass", side_effect=EOFError("private-error")
        ):
            self.assert_startup_error(
                "No enrollment token supplied; use --enrollment-token-file or an interactive terminal",
                "enroll",
            )
