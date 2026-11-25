"""Require a newer connector version and unpublished release artifacts."""

import os
import re
import sys

import httpx

MANIFEST_TYPES = ", ".join(
    (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)


def numeric_version(version: str) -> tuple[int, ...]:
    if not re.fullmatch(
        r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", version
    ):
        raise ValueError("Connector releases require numeric major.minor.patch")
    return tuple(int(component) for component in version.split("."))


def require_newer_release(
    client: httpx.Client, repository: str, version: str, token: str
) -> None:
    response = client.get(
        f"https://api.github.com/repos/{repository}/releases/latest",
        headers={"Authorization": f"Bearer {token}"},
    )
    if response.status_code == 404:
        payload = response.json()
        if not isinstance(payload, dict) or payload.get("message") != "Not Found":
            raise ValueError("Unrecognized missing latest-release response")
        return
    if response.status_code != 200:
        response.raise_for_status()
        raise ValueError(f"Unexpected latest-release status: {response.status_code}")

    payload = response.json()
    tag = payload.get("tag_name") if isinstance(payload, dict) else None
    if (
        not isinstance(tag, str)
        or payload.get("draft") is not False
        or payload.get("prerelease") is not False
    ):
        raise ValueError("GitHub did not return a valid latest stable release")
    if numeric_version(version) <= numeric_version(tag.removeprefix("v")):
        raise ValueError(
            f"Connector release {version} must be newer than latest release {tag}"
        )


def artifact_exists(
    client: httpx.Client, repository: str, version: str, auth: httpx.BasicAuth
) -> bool:
    token_response = client.get(
        "https://ghcr.io/token",
        params={"service": "ghcr.io", "scope": f"repository:{repository}:pull,push"},
        auth=auth,
    )
    token_response.raise_for_status()
    payload = token_response.json()
    token = payload.get("token") if isinstance(payload, dict) else None
    if not isinstance(token, str) or not token:
        raise ValueError("GHCR did not return an authentication token")

    response = client.get(
        f"https://ghcr.io/v2/{repository}/manifests/{version}",
        headers={"Authorization": f"Bearer {token}", "Accept": MANIFEST_TYPES},
    )
    if response.status_code == 200:
        return True
    if response.status_code != 404:
        response.raise_for_status()
        raise ValueError(f"Unexpected manifest status: {response.status_code}")

    payload = response.json()
    errors = payload.get("errors") if isinstance(payload, dict) else None
    if (
        not isinstance(errors, list)
        or not errors
        or not all(
            isinstance(error, dict)
            and error.get("code") in {"MANIFEST_UNKNOWN", "NAME_UNKNOWN"}
            for error in errors
        )
    ):
        raise ValueError("GHCR returned an unrecognized missing-artifact response")
    return False


def main() -> int:
    try:
        version = os.environ["CONNECTOR_VERSION"]
        image = os.environ["IMAGE"]
        charts = os.environ["CHARTS"].removeprefix("oci://")
        actor, password = os.environ["GH_ACTOR"], os.environ["GH_TOKEN"]
        if not actor or not password:
            raise ValueError("GHCR credentials are required")
        numeric_version(version)
        source_repository = os.environ["GITHUB_REPOSITORY"]
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", source_repository):
            raise ValueError("Invalid source repository")
        repositories = []
        for reference in (image, f"{charts}/private-connector"):
            if not re.fullmatch(r"ghcr\.io/(?:[a-z0-9._-]+/)+[a-z0-9._-]+", reference):
                raise ValueError("Release artifacts must be GHCR repository names")
            repositories.append(reference.removeprefix("ghcr.io/"))

        with httpx.Client(timeout=30, follow_redirects=False) as client:
            release = client.get(
                f"https://api.github.com/repos/{source_repository}/releases/tags/v{version}",
                headers={"Authorization": f"Bearer {password}"},
            )
            if release.status_code == 200:
                raise ValueError("GitHub release already exists; choose a new version")
            if release.status_code != 404:
                release.raise_for_status()
                raise ValueError("Unexpected GitHub release status")
            require_newer_release(client, source_repository, version, password)
            published = [
                f"ghcr.io/{repository}:{version}"
                for repository in repositories
                if artifact_exists(
                    client, repository, version, httpx.BasicAuth(actor, password)
                )
            ]
        if published:
            raise ValueError(
                "Release version already exists; choose a new version: "
                + ", ".join(published)
            )
    except (httpx.HTTPError, ValueError, KeyError) as error:
        print(f"Release availability check failed: {error}", file=sys.stderr)
        return 1
    print(f"Neither GHCR artifact exists for connector release {version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
