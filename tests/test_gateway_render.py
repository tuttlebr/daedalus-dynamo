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
        self.assertEqual(params["image"]["tag"], "v1.6.0")
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

    def test_public_gateway_requires_tls_and_all_listeners_require_api_key(self) -> None:
        documents = render("base")
        gateway = next(d for d in documents if d["kind"] == "Gateway")
        listener = next(d for d in gateway["spec"]["listeners"] if d["name"] == "https")
        self.assertEqual(listener["port"], 32359)
        self.assertEqual(listener["protocol"], "HTTPS")
        self.assertEqual(listener["tls"]["mode"], "Terminate")
        self.assertTrue(listener["tls"]["certificateRefs"])
        policy = next(d for d in documents if d["kind"] == "AgentgatewayPolicy")
        self.assertEqual(policy["spec"]["targetRefs"], [{
            "group": "gateway.networking.k8s.io",
            "kind": "Gateway",
            "name": gateway["metadata"]["name"],
        }])
        auth = policy["spec"]["traffic"]["apiKeyAuthentication"]
        self.assertEqual(auth["mode"], "Strict")
        self.assertEqual(auth["secretRef"]["name"], "inference-gateway-api-keys")
        self.assertNotIn("key", auth)

    def test_internal_listener_is_exposed_only_by_clusterip_service(self) -> None:
        documents = render("base")
        gateway = next(d for d in documents if d["kind"] == "Gateway")
        listener = next(d for d in gateway["spec"]["listeners"] if d["name"] == "internal-http")
        self.assertEqual(listener["protocol"], "HTTP")
        params = next(d for d in documents if d["kind"] == "AgentgatewayParameters")
        public = params["spec"]["service"]["spec"]
        self.assertEqual(public["type"], "LoadBalancer")
        self.assertEqual(public["ports"], [
            {"$patch": "replace"},
            {"name": "listener-32359", "port": 32359, "protocol": "TCP",
             "targetPort": 32359, "nodePort": 32359},
        ])
        internal = next(d for d in documents if d["kind"] == "Service"
                        and d["metadata"]["name"] == "inference-gateway-internal")["spec"]
        self.assertEqual(internal["type"], "ClusterIP")
        self.assertEqual(internal["selector"], {
            "gateway.networking.k8s.io/gateway-name": gateway["metadata"]["name"],
        })
        self.assertEqual(internal["ports"], [{
            "name": "internal-http", "port": listener["port"],
            "targetPort": listener["port"], "protocol": "TCP",
        }])

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
