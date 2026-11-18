# -*- coding: utf-8 -*-
"""租约预检门禁单元测试：缓存行为 + 失败节点跳过租约。"""
import os
import tempfile
import time
import unittest
from unittest.mock import patch

import proxy_pool_v3

VLESS_LINE = (
    "vless://495c7194-85b8-498a-bf20-2ea9ce9175b5@cmin2.pjq.cc.cd:443"
    "?path=%2Fabc%3D162.230.251.4%3A443%3Fauto%3D0%26ed%3D2560"
    "&security=tls&encryption=none&insecure=0&host=snp.fxk.kdns.fr"
    "&fp=chrome&type=ws&allowInsecure=0&sni=snp.fxk.kdns.fr"
)


class PreflightGateTests(unittest.TestCase):
    def _manager(self, tmp):
        pool_path = os.path.join(tmp, "pool.txt")
        with open(pool_path, "w", encoding="utf-8") as h:
            h.write(VLESS_LINE + "\n")
        cfg = {
            "proxy_mode": "pool",
            "proxy_pool_file": pool_path,
            "proxy_pool_preflight_enabled": True,
            "proxy_pool_state_file": os.path.join(tmp, "state.json"),
            "proxy_pool_refresh_interval_sec": 0,
            "proxy_pool_probe_interval_sec": 0,
            "proxy_pool_acquire_timeout_sec": 3,
        }
        return proxy_pool_v3.ProxyPoolManager(cfg)

    def test_gate_caches_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = self._manager(tmp)
            try:
                calls = []

                def fake_preflight(node_id):
                    calls.append(node_id)
                    return {
                        "id": node_id,
                        "ok": False,
                        "targets": [
                            {"url": "https://grok.com/", "usable": False,
                             "error": "timeout", "status_code": 0},
                        ],
                    }

                node_id = next(iter(manager._nodes))
                node = manager._nodes[node_id]
                with patch.object(manager, "preflight_node", side_effect=fake_preflight), \
                     patch.object(manager, "_save_state_file", lambda: None):
                    ok, reason = manager._preflight_gate(node, "sess")
                    self.assertFalse(ok)
                    self.assertIn("grok.com", reason)
                    # 命中失败缓存，不再探测
                    manager._preflight_cache[node_id]["at"] = time.time()
                    ok2, _ = manager._preflight_gate(node, "sess")
                    self.assertFalse(ok2)
                    self.assertEqual(len(calls), 1)

                # ok 结果走缓存：不再探测
                manager._preflight_cache.clear()
                with patch.object(manager, "preflight_node",
                                  return_value={"id": node_id, "ok": True, "targets": []}), \
                     patch.object(manager, "_save_state_file", lambda: None):
                    ok3, _ = manager._preflight_gate(node, "sess")
                    self.assertTrue(ok3)
                    ok4, _ = manager._preflight_gate(node, "sess")
                    self.assertTrue(ok4)
                    self.assertEqual(len(calls), 1)  # fake_preflight 未被再次调用
            finally:
                manager.shutdown(persist=False)

    def test_acquire_skips_preflight_failed_node(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = self._manager(tmp)
            try:
                def fake_preflight(node_id):
                    return {"id": node_id, "ok": False, "targets": []}

                with patch.object(manager, "preflight_node", side_effect=fake_preflight), \
                     patch.object(manager, "_save_state_file", lambda: None), \
                     patch.object(manager._runtime, "acquire",
                                  side_effect=AssertionError("预检失败的节点不应进入租约")):
                    with self.assertRaises(proxy_pool_v3.ProxyPoolError):
                        manager.acquire("affinity", "worker", 1, 1, "sess", timeout=3)
                node_id = next(iter(manager._nodes))
                self.assertIsNotNone(manager._nodes[node_id].cooldown_until)
            finally:
                manager.shutdown(persist=False)

    def test_acquire_leases_preflight_ok_node(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = self._manager(tmp)
            try:
                with patch.object(manager, "preflight_node",
                                  return_value={"id": "x", "ok": True, "targets": []}), \
                     patch.object(manager, "_save_state_file", lambda: None), \
                     patch.object(manager._runtime, "acquire",
                                  return_value=("http://127.0.0.1:59999", None)):
                    lease = manager.acquire("affinity", "worker", 1, 1, "sess", timeout=5)
                    self.assertIsNotNone(lease)
                    self.assertEqual(lease.node_id, next(iter(manager._nodes)))
                    manager.release(lease)
            finally:
                manager.shutdown(persist=False)


if __name__ == "__main__":
    unittest.main()
