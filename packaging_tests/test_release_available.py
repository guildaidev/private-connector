from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

import httpx

from scripts.check_release_available import main

IMAGE = "guildaidev/private-connector"
CHART = "guildaidev/charts/private-connector"
ENV = {
    "CONNECTOR_VERSION": "0.1.0",
    "IMAGE": f"ghcr.io/{IMAGE}",
    "CHARTS": "oci://ghcr.io/guildaidev/charts",
    "GH_ACTOR": "release-bot",
    "GH_TOKEN": "workflow-secret",
}


class ReleaseAvailabilityTests(unittest.TestCase):
    def run_check(self, respond, env=ENV):
        output = io.StringIO()
        client = httpx.Client(transport=httpx.MockTransport(respond))
        self.addCleanup(client.close)
        with (
            patch("scripts.check_release_available.httpx.Client", return_value=client),
            patch.dict("os.environ", env, clear=True),
            redirect_stderr(output),
            redirect_stdout(output),
        ):
            status = main()
        self.assertNotIn("workflow-secret", output.getvalue())
        self.assertNotIn("registry-secret", output.getvalue())
        return status, output.getvalue()

    def token_response(self, request):
        self.assertEqual(request.method, "GET")
        self.assertEqual(request.url.host, "ghcr.io")
        if request.url.path == "/token":
            self.assertEqual(request.url.params["service"], "ghcr.io")
            self.assertIn(
                request.url.params["scope"],
                [f"repository:{repo}:pull,push" for repo in (IMAGE, CHART)],
            )
            self.assertTrue(request.headers["authorization"].startswith("Basic "))
            return httpx.Response(200, json={"token": "registry-secret"})
        self.assertEqual(request.headers["authorization"], "Bearer registry-secret")
        self.assertIn(
            "application/vnd.oci.image.manifest.v1+json", request.headers["accept"]
        )
        return None

    def test_release_requires_both_versioned_artifacts_to_be_absent(self):
        for existing in ((), (IMAGE,), (CHART,), (IMAGE, CHART)):
            with self.subTest(existing=existing):
                checked = []

                def respond(request):
                    if response := self.token_response(request):
                        return response
                    checked.append(request.url.path)
                    if request.url.path in [
                        f"/v2/{repo}/manifests/0.1.0" for repo in existing
                    ]:
                        return httpx.Response(200, json={"schemaVersion": 2})
                    code = (
                        "NAME_UNKNOWN"
                        if CHART in request.url.path
                        else "MANIFEST_UNKNOWN"
                    )
                    return httpx.Response(404, json={"errors": [{"code": code}]})

                status, output = self.run_check(respond)
                self.assertEqual(status, 1 if existing else 0, output)
                self.assertEqual(
                    checked, [f"/v2/{repo}/manifests/0.1.0" for repo in (IMAGE, CHART)]
                )
                for repo in existing:
                    self.assertIn(f"ghcr.io/{repo}:0.1.0", output)

    def test_authentication_registry_and_redirect_errors_fail_closed(self):
        for phase in (
            "/token",
            f"/v2/{IMAGE}/manifests/0.1.0",
            f"/v2/{CHART}/manifests/0.1.0",
        ):
            for status_code in (302, 401, 403, 429, 500, 503):
                with self.subTest(phase=phase, status=status_code):

                    def respond(request):
                        if request.url.path == phase:
                            return httpx.Response(
                                status_code,
                                headers={"location": "https://elsewhere.test"},
                            )
                        return self.token_response(request) or httpx.Response(
                            404, json={"errors": [{"code": "MANIFEST_UNKNOWN"}]}
                        )

                    status, _ = self.run_check(respond)
                    self.assertEqual(status, 1)

    def test_unrecognized_or_malformed_responses_fail_closed(self):
        for payload in (
            {},
            {"errors": []},
            {"errors": [{"code": "DENIED"}]},
            {"errors": [{"code": "MANIFEST_UNKNOWN"}, {"code": "UNAUTHORIZED"}]},
            [],
            "not json",
        ):
            with self.subTest(payload=payload):

                def respond(request):
                    return self.token_response(request) or (
                        httpx.Response(404, text=payload)
                        if isinstance(payload, str)
                        else httpx.Response(404, json=payload)
                    )

                status, _ = self.run_check(respond)
                self.assertEqual(status, 1)

        for payload in ({}, {"token": ""}, {"token": 123}, [], "not json"):
            with self.subTest(token_payload=payload):

                def respond(request):
                    self.assertEqual(request.url.path, "/token")
                    return (
                        httpx.Response(200, text=payload)
                        if isinstance(payload, str)
                        else httpx.Response(200, json=payload)
                    )

                status, _ = self.run_check(respond)
                self.assertEqual(status, 1)

    def test_network_failures_abort_instead_of_counting_as_missing(self):
        for phase in (
            "/token",
            f"/v2/{IMAGE}/manifests/0.1.0",
            f"/v2/{CHART}/manifests/0.1.0",
        ):
            for error in (httpx.ConnectError, httpx.ReadTimeout):
                with self.subTest(phase=phase, error=error):

                    def respond(request):
                        if request.url.path == phase:
                            raise error("registry unavailable", request=request)
                        return self.token_response(request) or httpx.Response(
                            404, json={"errors": [{"code": "MANIFEST_UNKNOWN"}]}
                        )

                    status, _ = self.run_check(respond)
                    self.assertEqual(status, 1)

    def test_missing_credentials_or_invalid_release_input_abort_before_requests(self):
        for field in ENV:
            with self.subTest(field=field):
                status, _ = self.run_check(
                    lambda _: self.fail("Unexpected request"), {**ENV, field: ""}
                )
                self.assertEqual(status, 1)
