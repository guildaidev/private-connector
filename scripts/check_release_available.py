"""Require both versioned GHCR artifacts to be absent before publication."""

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
        if not re.fullmatch(r"\d+\.\d+\.\d+", version):
            raise ValueError("Connector releases require major.minor.patch")
        repositories = []
        for reference in (image, f"{charts}/private-connector"):
            if not re.fullmatch(r"ghcr\.io/(?:[a-z0-9._-]+/)+[a-z0-9._-]+", reference):
                raise ValueError("Release artifacts must be GHCR repository names")
            repositories.append(reference.removeprefix("ghcr.io/"))

        with httpx.Client(timeout=30, follow_redirects=False) as client:
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
