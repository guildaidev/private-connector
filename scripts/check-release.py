"""Validate the independently versioned connector release before publishing."""

import os
import re
import tomllib
from pathlib import Path

import yaml

root = Path(__file__).resolve().parents[1]
project = tomllib.loads((root / "pyproject.toml").read_text())
version = os.environ.get("CONNECTOR_VERSION", project["project"]["version"])
if not re.fullmatch(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)", version):
    raise SystemExit("Connector releases require a numeric major.minor.patch version")
chart = yaml.safe_load((root / "helm/private-connector/Chart.yaml").read_text())
lock = tomllib.loads((root / "uv.lock").read_text())
locked_version = next(
    package["version"]
    for package in lock["package"]
    if package["name"] == "guild-private-connector"
)
docker_version = re.search(
    r"^ARG VERSION=(.+)$", (root / "Dockerfile").read_text(), re.MULTILINE
)
if (
    not version
    == project["project"]["version"]
    == chart["version"]
    == chart["appVersion"]
    == locked_version
    == (docker_version.group(1) if docker_version else None)
):
    raise SystemExit(
        "Requested version must match pyproject, uv.lock, Dockerfile, "
        "Chart.version and Chart.appVersion"
    )
print(f"Validated connector release {version}")
