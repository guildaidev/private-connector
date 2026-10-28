from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path

import yaml

CHART = Path(__file__).resolve().parents[1] / "helm/private-connector"


def render(*settings, release="test"):
    args = [
        "helm",
        "template",
        release,
        str(CHART),
        "--set",
        "connector.id=00000000-0000-0000-0000-000000000001",
        "--set",
        "connector.controlPlaneUrl=https://core.example.test",
        "--set",
        "connector.destinations.foo=https://foo.internal:8443",
    ]
    for setting in settings:
        args += ["--set", setting]
    return subprocess.run(args, text=True, capture_output=True, timeout=20)


class ChartTests(unittest.TestCase):
    def manifests(self, *settings, release="test"):
        result = render(*settings, release=release)
        self.assertEqual(result.returncode, 0, result.stderr)
        return {doc["kind"]: doc for doc in yaml.safe_load_all(result.stdout) if doc}

    def test_no_hosted_services_or_inbound_listener_and_scoped_secret(self):
        docs = self.manifests("enrollment.existingSecret=install-a")
        self.assertEqual(
            set(docs), {"Deployment", "ConfigMap", "PersistentVolumeClaim"}
        )
        deployment = docs["Deployment"]
        self.assertEqual(deployment["spec"]["replicas"], 1)
        self.assertEqual(deployment["spec"]["strategy"]["type"], "Recreate")
        pod = deployment["spec"]["template"]["spec"]
        self.assertFalse(pod["automountServiceAccountToken"])
        for container in pod["containers"] + pod["initContainers"]:
            self.assertFalse(container.get("ports"))
            self.assertTrue(container["securityContext"]["readOnlyRootFilesystem"])
            self.assertEqual(
                container["securityContext"]["capabilities"]["drop"], ["ALL"]
            )
        self.assertNotIn(
            "enrollment", [v["name"] for v in pod["containers"][0]["volumeMounts"]]
        )
        self.assertIn(
            "enrollment", [v["name"] for v in pod["initContainers"][0]["volumeMounts"]]
        )
        self.assertNotEqual(
            pod["containers"][0]["readinessProbe"]["exec"],
            pod["containers"][0]["livenessProbe"]["exec"],
        )
        config = json.loads(docs["ConfigMap"]["data"]["connector.json"])
        self.assertEqual(config["destinations"], {"foo": "https://foo.internal:8443"})
        self.assertEqual(config["max_in_flight"], 4)
        self.assertEqual(
            docs["PersistentVolumeClaim"]["metadata"]["annotations"][
                "helm.sh/resource-policy"
            ],
            "keep",
        )

    def test_separate_tunnels_have_separate_storage_and_existing_claim_is_respected(
        self,
    ):
        first, second = self.manifests(release="one"), self.manifests(release="two")
        self.assertNotEqual(
            first["PersistentVolumeClaim"]["metadata"]["name"],
            second["PersistentVolumeClaim"]["metadata"]["name"],
        )
        reused = self.manifests("persistence.existingClaim=retained")
        self.assertNotIn("PersistentVolumeClaim", reused)
        volumes = reused["Deployment"]["spec"]["template"]["spec"]["volumes"]
        self.assertEqual(
            next(v for v in volumes if v["name"] == "identity")[
                "persistentVolumeClaim"
            ]["claimName"],
            "retained",
        )

    def test_digest_pinning_and_private_control_plane_ca(self):
        digest = "sha256:" + "a" * 64
        docs = self.manifests(
            f"image.digest={digest}", "controlPlaneCA.existingConfigMap=trust"
        )
        pod = docs["Deployment"]["spec"]["template"]["spec"]
        for container in pod["containers"] + pod["initContainers"]:
            self.assertTrue(container["image"].endswith("@" + digest))
        config = json.loads(docs["ConfigMap"]["data"]["connector.json"])
        self.assertEqual(config["control_plane_ca"], "/etc/guild/ca/ca.pem")

    def test_private_gateway_ca_is_explicit_and_mounted_read_only(self):
        docs = self.manifests(
            "gatewayCA.existingConfigMap=gateway-trust", "gatewayCA.key=root.pem"
        )
        config = json.loads(docs["ConfigMap"]["data"]["connector.json"])
        self.assertEqual(config["gateway_ca"], "/etc/guild/gateway-ca/ca.pem")
        self.assertNotIn("control_plane_ca", config)
        pod = docs["Deployment"]["spec"]["template"]["spec"]
        trust_volume = next(v for v in pod["volumes"] if v["name"] == "gateway-ca")
        self.assertEqual(
            trust_volume["configMap"],
            {
                "name": "gateway-trust",
                "items": [{"key": "root.pem", "path": "ca.pem"}],
            },
        )
        mounts = pod["containers"][0]["volumeMounts"]
        self.assertIn(
            {
                "name": "gateway-ca",
                "mountPath": "/etc/guild/gateway-ca",
                "readOnly": True,
            },
            mounts,
        )

    def test_unsafe_or_incomplete_values_are_rejected(self):
        for setting in (
            "connector.id=",
            "connector.controlPlaneUrl=http://core.test",
            "connector.controlPlaneUrl=https://core.test/api?region=us",
            "connector.controlPlaneUrl=https://core.test/api#fragment",
            "connector.destinations.foo=https://foo/path",
            "replicaCount=2",
            "enrollment.token=do-not-put-secrets-in-values",
            "connector.maxInFlight=0",
            "connector.maxInFlight=-1",
            "connector.maxInFlight=not-an-integer",
        ):
            with self.subTest(setting=setting):
                self.assertNotEqual(render(setting).returncode, 0)

    def test_chart_accepts_requested_capacity_above_gateway_ceiling(self):
        docs = self.manifests("connector.maxInFlight=128")
        config = json.loads(docs["ConfigMap"]["data"]["connector.json"])
        self.assertEqual(config["max_in_flight"], 128)

    def test_chart_accepts_https_control_plane_api_base_path(self):
        docs = self.manifests(
            "connector.controlPlaneUrl=https://core.example.test/api/v2"
        )
        config = json.loads(docs["ConfigMap"]["data"]["connector.json"])
        self.assertEqual(
            config["control_plane_url"], "https://core.example.test/api/v2"
        )
