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
    "CONNECTOR_VERSION": "0.2.0",
    "GITHUB_REPOSITORY": "guildaidev/private-connector",
    "IMAGE": f"ghcr.io/{IMAGE}",
    "CHARTS": "oci://ghcr.io/guildaidev/charts",
    "GH_ACTOR": "release-bot",
    "GH_TOKEN": "workflow-secret",
}


class ReleaseAvailabilityTests(unittest.TestCase):
    def test_first_release_is_allowed_when_no_latest_exists(self):
        requests = []

        def respond(request):
            requests.append(request)
            return self.missing_response(request)

        status, output = self.run_check(respond)
        self.assertEqual(status, 0, output)
        self.assertEqual(
            [request.url.path for request in requests[:2]],
            [
                "/repos/guildaidev/private-connector/releases/tags/v0.2.0",
                "/repos/guildaidev/private-connector/releases/latest",
            ],
        )
        self.assertEqual(
            [
                request.url.path
                for request in requests
                if "/manifests/" in request.url.path
            ],
            [f"/v2/{repo}/manifests/0.2.0" for repo in (IMAGE, CHART)],
        )

    def test_newer_versions_are_allowed_in_numeric_order(self):
        for requested, latest in (
            ("0.2.1", "v0.2.0"),
            ("0.10.0", "v0.9.0"),
            ("1.0.0", "v0.99.99"),
            ("0.3.0", "0.2.0"),
        ):
            with self.subTest(requested=requested, latest=latest):
                manifests = []

                def respond(request):
                    if request.url.path.endswith("/releases/latest"):
                        return httpx.Response(
                            200,
                            json={
                                "tag_name": latest,
                                "draft": False,
                                "prerelease": False,
                            },
                        )
                    if "/manifests/" in request.url.path:
                        manifests.append(request.url.path)
                    return self.missing_response(request)

                status, output = self.run_check(
                    respond, ENV | {"CONNECTOR_VERSION": requested}
                )
                self.assertEqual(status, 0, output)
                self.assertEqual(
                    manifests,
                    [f"/v2/{repo}/manifests/{requested}" for repo in (IMAGE, CHART)],
                )

    def test_equal_or_older_versions_stop_before_registry_requests(self):
        for requested, latest in (
            ("0.2.0", "v0.2.0"),
            ("0.1.9", "v0.2.0"),
            ("0.9.0", "v0.10.0"),
            ("0.99.99", "v1.0.0"),
        ):
            with self.subTest(requested=requested, latest=latest):
                requests = []

                def respond(request):
                    requests.append(request)
                    self.assertEqual(request.url.host, "api.github.com")
                    if request.url.path.endswith("/releases/latest"):
                        return httpx.Response(
                            200,
                            json={
                                "tag_name": latest,
                                "draft": False,
                                "prerelease": False,
                            },
                        )
                    return self.missing_response(request)

                status, output = self.run_check(
                    respond, ENV | {"CONNECTOR_VERSION": requested}
                )
                self.assertEqual(status, 1)
                self.assertIn("must be newer", output)
                self.assertEqual(len(requests), 2)

    def test_malformed_latest_release_stops_before_registry_requests(self):
        for payload in (
            {},
            [],
            None,
            "not json",
            {"tag_name": "v0.1.0"},
            {"tag_name": "v0.1.0", "draft": True, "prerelease": False},
            {"tag_name": "v0.1.0", "draft": False, "prerelease": True},
            *(
                {"tag_name": version, "draft": False, "prerelease": False}
                for version in (
                    None,
                    123,
                    "",
                    "v0.1",
                    "v0.1.0-rc.1",
                    "v0.1.0+build",
                    "v00.1.0",
                    "v0.1.0\n",
                    "release-0.1.0",
                )
            ),
        ):
            with self.subTest(payload=payload):
                requests = []

                def respond(request):
                    requests.append(request)
                    self.assertEqual(request.url.host, "api.github.com")
                    if request.url.path.endswith("/releases/latest"):
                        if isinstance(payload, str):
                            return httpx.Response(200, text=payload)
                        return httpx.Response(200, json=payload)
                    return self.missing_response(request)

                status, _ = self.run_check(respond)
                self.assertEqual(status, 1)
                self.assertEqual(len(requests), 2)

    def test_latest_lookup_errors_stop_before_registry_requests(self):
        for status_code in (202, 204, 301, 302, 401, 403, 429, 500, 503):
            with self.subTest(status=status_code):
                requests = []

                def respond(request):
                    requests.append(request)
                    self.assertEqual(request.url.host, "api.github.com")
                    if request.url.path.endswith("/releases/latest"):
                        return httpx.Response(
                            status_code,
                            headers={"location": "https://elsewhere.test"},
                        )
                    return self.missing_response(request)

                status, _ = self.run_check(respond)
                self.assertEqual(status, 1)
                self.assertEqual(len(requests), 2)

    def test_malformed_missing_latest_response_stops_publication(self):
        for payload in ({}, [], {"message": "Unauthorized"}, "not json"):
            with self.subTest(payload=payload):

                def respond(request):
                    self.assertEqual(request.url.host, "api.github.com")
                    if request.url.path.endswith("/releases/latest"):
                        if isinstance(payload, str):
                            return httpx.Response(404, text=payload)
                        return httpx.Response(404, json=payload)
                    return self.missing_response(request)

                status, _ = self.run_check(respond)
                self.assertEqual(status, 1)

    def test_existing_release_or_unknown_github_status_stops_publication(self):
        for status in (200, 403, 500):
            with self.subTest(status=status):
                requests = []

                def respond(request):
                    requests.append(request)
                    self.assertEqual(request.url.host, "api.github.com")
                    return httpx.Response(status, json={})

                result, _ = self.run_check(
                    respond, ENV | {"GITHUB_REPOSITORY": "guildaidev/private-connector"}
                )
                self.assertEqual(result, 1)
                self.assertEqual(len(requests), 1)

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

    def missing_response(self, request):
        if request.url.host == "api.github.com":
            self.assertEqual(request.method, "GET")
            self.assertEqual(request.headers["authorization"], "Bearer workflow-secret")
            return httpx.Response(404, json={"message": "Not Found"})
        return self.token_response(request) or httpx.Response(
            404, json={"errors": [{"code": "MANIFEST_UNKNOWN"}]}
        )

    def test_release_requires_both_versioned_artifacts_to_be_absent(self):
        for existing in ((), (IMAGE,), (CHART,), (IMAGE, CHART)):
            with self.subTest(existing=existing):
                checked = []

                def respond(request):
                    if request.url.host == "api.github.com":
                        return httpx.Response(404, json={"message": "Not Found"})
                    if response := self.token_response(request):
                        return response
                    checked.append(request.url.path)
                    if request.url.path in [
                        f"/v2/{repo}/manifests/0.2.0" for repo in existing
                    ]:
                        return httpx.Response(200, json={"schemaVersion": 2})
                    code = (
                        "NAME_UNKNOWN"
                        if CHART in request.url.path
                        else "MANIFEST_UNKNOWN"
                    )
                    return httpx.Response(404, json={"errors": [{"code": code}]})

                status, output = self.run_check(
                    respond, ENV | {"GITHUB_REPOSITORY": "guildaidev/private-connector"}
                )
                self.assertEqual(status, 1 if existing else 0, output)
                self.assertEqual(
                    checked, [f"/v2/{repo}/manifests/0.2.0" for repo in (IMAGE, CHART)]
                )
                for repo in existing:
                    self.assertIn(f"ghcr.io/{repo}:0.2.0", output)

    def test_authentication_registry_and_redirect_errors_fail_closed(self):
        for phase in (
            "/token",
            f"/v2/{IMAGE}/manifests/0.2.0",
            f"/v2/{CHART}/manifests/0.2.0",
        ):
            for status_code in (302, 401, 403, 429, 500, 503):
                with self.subTest(phase=phase, status=status_code):

                    def respond(request):
                        if request.url.path == phase:
                            return httpx.Response(
                                status_code,
                                headers={"location": "https://elsewhere.test"},
                            )
                        return self.missing_response(request)

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
                    if request.url.host == "api.github.com":
                        return self.missing_response(request)
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
                    if request.url.host == "api.github.com":
                        return self.missing_response(request)
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
            "/repos/guildaidev/private-connector/releases/tags/v0.2.0",
            "/repos/guildaidev/private-connector/releases/latest",
            "/token",
            f"/v2/{IMAGE}/manifests/0.2.0",
            f"/v2/{CHART}/manifests/0.2.0",
        ):
            for error in (httpx.ConnectError, httpx.ReadTimeout):
                with self.subTest(phase=phase, error=error):

                    def respond(request):
                        if request.url.path == phase:
                            raise error("registry unavailable", request=request)
                        return self.missing_response(request)

                    status, _ = self.run_check(respond)
                    self.assertEqual(status, 1)

    def test_missing_credentials_or_invalid_release_input_abort_before_requests(self):
        for field in ENV:
            with self.subTest(field=field):
                status, _ = self.run_check(
                    lambda _: self.fail("Unexpected request"), {**ENV, field: ""}
                )
                self.assertEqual(status, 1)
