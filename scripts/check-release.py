"""Validate the independently versioned connector release before publishing."""

import os
import re
import tomllib
from pathlib import Path

import yaml

root = Path(__file__).resolve().parents[1]
version = os.environ["CONNECTOR_VERSION"]
if not re.fullmatch(r"\d+\.\d+\.\d+", version):
    raise SystemExit("Connector releases require a numeric major.minor.patch version")
project = tomllib.loads((root / "pyproject.toml").read_text())
chart = yaml.safe_load((root / "helm/private-connector/Chart.yaml").read_text())
if (
    not version
    == project["project"]["version"]
    == chart["version"]
    == chart["appVersion"]
):
    raise SystemExit(
        "Requested version must match pyproject, Chart.version and Chart.appVersion"
    )
print(f"Validated connector release {version}")
