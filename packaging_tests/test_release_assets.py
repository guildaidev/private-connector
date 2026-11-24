import hashlib
import json
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

import yaml

from scripts.release_assets import ROOT, build_assets

IMAGE = "ghcr.io/guildaidev/private-connector"
DIGEST = "sha256:" + "a" * 64


class ReleaseAssetsTests(unittest.TestCase):
    def setUp(self):
        files = tempfile.TemporaryDirectory()
        self.addCleanup(files.cleanup)
        self.output = Path(files.name) / "release"

    def build(self):
        build_assets(ROOT, self.output, IMAGE, DIGEST, "b" * 40)

    def test_downloads_pin_one_image_and_have_valid_checksums(self):
        self.build()
        manifest = json.loads((self.output / "release.json").read_text())
        compose = yaml.safe_load((self.output / "compose.yaml").read_text())
        self.assertEqual(compose["services"]["connector"]["image"], f"{IMAGE}@{DIGEST}")
        self.assertEqual(manifest["image"], f"{IMAGE}@{DIGEST}")
        for line in (self.output / "SHA256SUMS").read_text().splitlines():
            expected, name = line.split("  ")
            self.assertEqual(
                hashlib.sha256((self.output / name).read_bytes()).hexdigest(), expected
            )
        bundle = self.output / f"private-connector-{manifest['version']}-compose.tar.gz"
        with tarfile.open(bundle) as archive:
            self.assertEqual(
                set(archive.getnames()),
                {
                    "compose.yaml",
                    "connector.example.json",
                    "INSTALL.md",
                    "LICENSE",
                    "VERSION",
                    "release.json",
                },
            )
            self.assertEqual(
                archive.extractfile("compose.yaml").read(),
                (self.output / "compose.yaml").read_bytes(),
            )
        with self.assertRaisesRegex(ValueError, "must be empty"):
            self.build()

    def test_downloaded_chart_renders_with_the_release_digest(self):
        self.build()
        manifest = json.loads((self.output / "release.json").read_text())
        result = subprocess.run(
            [
                "helm",
                "template",
                "test",
                str(self.output / manifest["helm_chart"]),
                "--values",
                str(self.output / "connector-values.yaml"),
                "--set",
                "connector.id=00000000-0000-0000-0000-000000000001",
                "--set",
                "connector.controlPlaneUrl=https://core.example.test",
            ],
            text=True,
            capture_output=True,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        deployment = next(
            item
            for item in yaml.safe_load_all(result.stdout)
            if item and item["kind"] == "Deployment"
        )
        spec = deployment["spec"]["template"]["spec"]
        for container in spec["initContainers"] + spec["containers"]:
            self.assertEqual(container["image"], f"{IMAGE}@{DIGEST}")

    def test_invalid_digest_produces_no_downloads(self):
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            build_assets(ROOT, self.output, IMAGE, "latest", "b" * 40)
        self.assertFalse(self.output.exists())

    def test_chart_packaging_refuses_local_credentials_and_symbolic_links(self):
        source = self.output.parent / "source"
        source.mkdir()
        for name in ("pyproject.toml", "LICENSE"):
            shutil.copyfile(ROOT / name, source / name)
        for name in ("deploy", "helm"):
            shutil.copytree(ROOT / name, source / name)
        private_file = source / "helm/private-connector/identity.json"
        private_file.write_text('{"connector_token": "local-test-identity"}')
        with self.assertRaisesRegex(ValueError, "Unexpected chart file"):
            build_assets(source, self.output, IMAGE, DIGEST, "b" * 40)
        private_file.unlink()
        private_file = source / "identity.json"
        private_file.write_text('{"connector_token": "local-test-identity"}')
        (source / "helm/private-connector/templates/identity.yaml").symlink_to(
            private_file
        )
        with self.assertRaisesRegex(ValueError, "symbolic links"):
            build_assets(
                source, self.output.parent / "other-output", IMAGE, DIGEST, "b" * 40
            )
