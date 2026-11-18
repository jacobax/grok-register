# -*- coding: utf-8 -*-
"""选择策略测试：低延迟优先 + 窗口避让。"""
import os
import tempfile
import time
import unittest
from unittest.mock import patch

import proxy_pool_v3

VLESS_A = (
    "vless://495c7194-85b8-498a-bf20-2ea9ce9175b5@cmin2.pjq.cc.cd:443"
    "?path=%2Fabc%3D162.230.251.4%3A443%3Fauto%3D0%26ed%3D2560"
    "&security=tls&encryption=none&insecure=0&host=snp.fxk.kdns.fr"
    "&fp=chrome&type=ws&allowInsecure=0&sni=snp.fxk.kdns.fr"
)
VLESS_B = VLESS_A.replace("162.230.251.4", "68.43.17.231")
VLESS_C = VLESS_A.replace("162.230.251.4", "24.136.24.254")


class SelectionPolicyTests(unittest.TestCase):
    def _manager(self, tmp):
        pool_path = os.path.join(tmp, "pool.txt")
        with open(pool_path, "w", encoding="utf-8") as h:
            h.write("\n".join([VLESS_A, VLESS_B, VLESS_C]) + "\n")
        cfg = {
            "proxy_mode": "pool",
            "proxy_pool_file": pool_path,
            "proxy_pool_preflight_enabled": False,
            "proxy_pool_prefer_low_latency": True,
            "proxy_pool_spread_window_sec": 180,
            "proxy_pool_state_file": os.path.join(tmp, "state.json"),
            "proxy_pool_refresh_interval_sec": 0,
            "proxy_pool_probe_interval_sec": 0,
        }
        return proxy_pool_v3.ProxyPoolManager(cfg)

    def _latencies(self, manager, mapping):
        for node in manager._nodes.values():
            for ip, ms in mapping.items():
                if ip in node.descriptor.raw_uri:
                    node.probe_latency_ms = ms

    def test_low_latency_first_then_deferral(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = self._manager(tmp)
            try:
                self._latencies(manager, {
                    "162.230.251.4": 200,
                    "68.43.17.231": 500,
                    "24.136.24.254": 900,
                })
                with patch.object(manager, "_save_state_file", lambda: None), \
                     patch.object(manager._runtime, "acquire",
                                  return_value=("http://127.0.0.1:59999", None)):
                    lease1 = manager.acquire("a", "w", 1, 1, "s1", timeout=5)
                    self.assertIn("162.230.251.4", lease1.source_uri)
                    manager.release(lease1)
                    # 180s 窗口内：最低延迟节点让位，次低延迟接上
                    lease2 = manager.acquire("b", "w", 1, 1, "s2", timeout=5)
                    self.assertIn("68.43.17.231", lease2.source_uri)
                    manager.release(lease2)
                    lease3 = manager.acquire("c", "w", 1, 1, "s3", timeout=5)
                    self.assertIn("24.136.24.254", lease3.source_uri)
                    manager.release(lease3)
                    # 窗口过期后：回到最低延迟节点
                    now = time.time()
                    for node in manager._nodes.values():
                        if node.lease_marks:
                            node.lease_marks[-1] = now - 200
                    lease4 = manager.acquire("d", "w", 1, 1, "s4", timeout=5)
                    self.assertIn("162.230.251.4", lease4.source_uri)
                    manager.release(lease4)
            finally:
                manager.shutdown(persist=False)

    def test_unhealthy_node_deferred(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = self._manager(tmp)
            try:
                self._latencies(manager, {
                    "162.230.251.4": 200,
                    "68.43.17.231": 900,
                })
                low = next(n for n in manager._nodes.values() if "162.230.251.4" in n.descriptor.raw_uri)
                low.health = 0.3  # 低延迟但健康度差
                with patch.object(manager, "_save_state_file", lambda: None), \
                     patch.object(manager._runtime, "acquire",
                                  return_value=("http://127.0.0.1:59999", None)):
                    lease = manager.acquire("a", "w", 1, 1, "s1", timeout=5)
                    self.assertIn("68.43.17.231", lease.source_uri)
                    manager.release(lease)
            finally:
                manager.shutdown(persist=False)

    def test_single_node_pool_keeps_working(self):
        with tempfile.TemporaryDirectory() as tmp:
            pool_path = os.path.join(tmp, "pool.txt")
            with open(pool_path, "w", encoding="utf-8") as h:
                h.write(VLESS_A + "\n")
            cfg = {
                "proxy_mode": "pool",
                "proxy_pool_file": pool_path,
                "proxy_pool_preflight_enabled": False,
                "proxy_pool_prefer_low_latency": True,
                "proxy_pool_state_file": os.path.join(tmp, "state.json"),
                "proxy_pool_refresh_interval_sec": 0,
                "proxy_pool_probe_interval_sec": 0,
            }
            manager = proxy_pool_v3.ProxyPoolManager(cfg)
            try:
                with patch.object(manager, "_save_state_file", lambda: None), \
                     patch.object(manager._runtime, "acquire",
                                  return_value=("http://127.0.0.1:59999", None)):
                    lease = manager.acquire("a", "w", 1, 1, "s1", timeout=5)
                    self.assertIsNotNone(lease)
                    manager.release(lease)
            finally:
                manager.shutdown(persist=False)


if __name__ == "__main__":
    unittest.main()
