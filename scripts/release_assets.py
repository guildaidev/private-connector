"""Build customer installation files from a published connector image digest."""

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import tarfile
import tempfile
import tomllib
from pathlib import Path

import yaml
from ruamel.yaml import YAML

ROOT = Path(__file__).resolve().parents[1]


def build_assets(
    root: Path, output: Path, image: str, digest: str, revision: str
) -> None:
    version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    if not re.fullmatch(r"ghcr\.io/(?:[a-z0-9._-]+/)+[a-z0-9._-]+", image):
        raise ValueError("The release image must be a GHCR repository")
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", digest):
        raise ValueError("The release image requires a SHA-256 digest")
    if not re.fullmatch(r"[a-f0-9]{40}", revision):
        raise ValueError("The release requires the full source commit SHA")
    if output.exists() and any(output.iterdir()):
        raise ValueError("Release output directory must be empty")
    output.mkdir(parents=True, exist_ok=True)
    image_ref = f"{image}@{digest}"
    compose = yaml.safe_load((root / "deploy/compose/compose.yaml").read_text())
    compose["services"]["connector"]["image"] = image_ref
    (output / "compose.yaml").write_text(yaml.safe_dump(compose, sort_keys=False))
    for source, destination in (
        ("deploy/compose/connector.example.json", "connector.example.json"),
        ("LICENSE", "LICENSE"),
        ("deploy/INSTALL.md", "INSTALL.md"),
    ):
        (output / destination).write_bytes((root / source).read_bytes())
    values = {
        "image": {"repository": image, "digest": digest},
        "connector": {
            "id": "CONNECTOR_ID_FROM_GUILD",
            "controlPlaneUrl": "https://CONTROL_PLANE_FROM_GUILD",
            "destinations": {"foo": "https://foo.internal:8443"},
        },
        "enrollment": {"existingSecret": "guild-connector-enrollment"},
    }
    (output / "connector-values.yaml").write_text(
        yaml.safe_dump(values, sort_keys=False)
    )
    (output / "VERSION").write_text(f"{version}\n")
    chart_name = f"private-connector-{version}.tgz"
    chart_root = root / "helm/private-connector"
    if (root / "helm").is_symlink() or chart_root.is_symlink():
        raise ValueError("Chart packaging does not accept symbolic links")
    chart = yaml.safe_load((chart_root / "Chart.yaml").read_text())
    if chart["version"] != version or chart["appVersion"] != version:
        raise ValueError("The chart and connector versions must match")
    chart_files = []
    for path in sorted(chart_root.rglob("*")):
        if path.is_symlink():
            raise ValueError("Chart packaging does not accept symbolic links")
        if not path.is_file():
            continue
        relative = path.relative_to(chart_root)
        if str(relative) not in {
            "Chart.yaml",
            "values.yaml",
            "values.schema.json",
            "README.md",
            "LICENSE",
            ".helmignore",
        } and not (
            relative.parts[0] == "templates"
            and path.suffix in {".yaml", ".yml", ".tpl", ".txt"}
        ):
            raise ValueError(
                "Unexpected chart file; remove local configuration before packaging"
            )
        chart_files.append(path)
    with tempfile.TemporaryDirectory(prefix="private-connector-chart-") as temporary:
        staged_chart = Path(temporary) / "private-connector"
        for path in chart_files:
            staged_file = staged_chart / path.relative_to(chart_root)
            staged_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, staged_file)
            if path == chart_root / "values.yaml":
                chart_yaml = YAML()
                chart_yaml.preserve_quotes = True
                chart_values = chart_yaml.load(path)
                chart_values["image"].update(repository=image, digest=digest)
                chart_yaml.dump(chart_values, staged_file)
        subprocess.run(
            ["helm", "package", str(staged_chart), "--destination", str(output)],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    (output / "release.json").write_text(
        json.dumps(
            {
                "version": version,
                "source_revision": revision,
                "image": image_ref,
                "platforms": ["linux/amd64", "linux/arm64"],
                "helm_chart": chart_name,
            },
            indent=2,
        )
        + "\n"
    )
    bundle_name = f"private-connector-{version}-compose.tar.gz"
    bundle_files = [
        "compose.yaml",
        "connector.example.json",
        "INSTALL.md",
        "LICENSE",
        "VERSION",
        "release.json",
    ]
    with tarfile.open(output / bundle_name, "w:gz") as archive:
        for name in bundle_files:
            archive.add(output / name, arcname=name)
    (output / "SHA256SUMS").write_text(
        "".join(
            f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
            for path in sorted(output.iterdir())
            if path.is_file()
        )
    )
    (output / "release-notes.md").write_text(
        f"Guild private connector {version}\n\n"
        "Download the Compose bundle below, or use the Helm chart. "
        "No repository checkout or source build is needed. "
        "Follow INSTALL.md and obtain your connector configuration and "
        "enrollment token from Guild.\n\n"
        f"- Image: `{image_ref}`\n"
        "- Platforms: Linux amd64 and arm64\n"
        f"- Helm: `oci://{image.rsplit('/', 1)[0]}/charts/private-connector` "
        f"version `{version}`; the attached chart pins the image digest.\n"
        f"- Source: `{revision}`\n"
        "- Verify downloads with `sha256sum --check --ignore-missing SHA256SUMS`.\n\n"
        "Keep your existing identity volume when upgrading. "
        "Updates can interrupt active calls; avoid removing identity volumes.\n"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--digest", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    build_assets(ROOT, args.output, args.image, args.digest, args.revision)


if __name__ == "__main__":
    main()
