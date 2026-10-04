from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def render(mode: str) -> list[dict]:
    completed = subprocess.run(
        ["scripts/render-inference-gateway.sh", mode],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return [document for document in yaml.safe_load_all(completed.stdout) if document]


class GatewayRenderTests(unittest.TestCase):
    def test_base_contains_fd_and_scaling_guards(self) -> None:
        documents = render("base")
        by_kind_name = {
            (document["kind"], document["metadata"]["name"]): document
            for document in documents
        }
        params = by_kind_name[("AgentgatewayParameters", "inference-gateway-params")]["spec"]
        self.assertEqual(params["image"]["tag"], "v1.4.1")
        self.assertEqual(
            next(item["value"] for item in params["env"] if item["name"] == "AGENTGATEWAY_NOFILE_LIMIT"),
            "131072",
        )
        self.assertEqual(params["rawConfig"]["config"]["backend"]["poolMaxSize"], 64)
        self.assertEqual(params["deployment"]["spec"]["replicas"], 2)
        pod_spec = params["deployment"]["spec"]["template"]["spec"]
        self.assertTrue(pod_spec["shareProcessNamespace"])
        self.assertIn("fd-metrics", [container["name"] for container in pod_spec["containers"]])
        self.assertEqual(params["podDisruptionBudget"]["spec"]["minAvailable"], 1)
        self.assertEqual(params["horizontalPodAutoscaler"]["spec"]["maxReplicas"], 8)

        policy = by_kind_name[("AgentgatewayPolicy", "daedalus-dynamo-model-header")]["spec"]
        self.assertEqual(policy["frontend"]["http"]["http1IdleTimeout"], "60s")
        self.assertEqual(policy["frontend"]["http"]["maxConnectionDuration"], "10m")
        self.assertIn(("Service", "inference-gateway-fd-metrics"), by_kind_name)
        self.assertIn(("ConfigMap", "inference-gateway-fd-metrics"), by_kind_name)

    def test_monitoring_contains_headroom_and_availability_alerts(self) -> None:
        documents = render("monitoring")
        by_kind = {document["kind"]: document for document in documents}
        self.assertIn("ServiceMonitor", by_kind)
        rule = by_kind["PrometheusRule"]
        names = {
            item["alert"]
            for group in rule["spec"]["groups"]
            for item in group["rules"]
        }
        self.assertIn("DaedalusGatewayFileDescriptorHeadroomLow", names)
        self.assertIn("DaedalusGatewayFileDescriptorsCritical", names)
        self.assertIn("DaedalusGatewayReplicasUnavailable", names)


if __name__ == "__main__":
    unittest.main()
