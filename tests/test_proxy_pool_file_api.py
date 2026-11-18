"""代理池文件 API 回归：每轮导入为替换语义（非合并）。"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    from fastapi.testclient import TestClient
except ImportError:
    TestClient = None

NODE_A = ("vless://uuid-a@test-a.example:443"
          "?path=%2Fabc%3D1.1.1.1%3A443%3Fauto%3D0&security=tls&type=ws"
          "&host=front.example&sni=front.example&fp=chrome#nodeA")
NODE_B = ("vless://uuid-b@test-b.example:443"
          "?path=%2Fabc%3D2.2.2.2%3A443%3Fauto%3D0&security=tls&type=ws"
          "&host=front.example&sni=front.example&fp=chrome#nodeB")
NODE_C = ("vless://uuid-c@test-c.example:443"
          "?path=%2Fabc%3D3.3.3.3%3A443%3Fauto%3D0&security=tls&type=ws"
          "&host=front.example&sni=front.example&fp=chrome#nodeC")


@unittest.skipIf(TestClient is None, "web dependencies not installed")
class ProxyPoolFileApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from web import server
        cls.server = server
        cls.client = TestClient(server.app)

    def setUp(self):
        with self.server._job_lock:
            self.server._job_state["running"] = False
            self.server._maintenance_state = None

    def _load_for_path(self, path):
        cfg = dict(self.server.engine.DEFAULT_CONFIG)
        cfg["proxy_pool_file"] = str(path)
        def load():
            self.server.engine.config.clear()
            self.server.engine.config.update(cfg)
            return self.server.engine.config
        return load

    def test_put_replaces_pool_entirely_each_round(self):
        """第二轮导入后，池文件只包含第二轮节点——第一轮节点被替换掉。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "proxy-pool.txt"
            loader = self._load_for_path(path)
            with patch.object(self.server.engine, "load_config", side_effect=loader):
                first = self.client.put("/api/proxies/pool", json={
                    "data": NODE_A + "\n" + NODE_B + "\n"})
                self.assertEqual(first.status_code, 200)
                self.assertEqual(first.json()["count"], 2)

                second = self.client.put("/api/proxies/pool", json={
                    "data": NODE_B + "\n" + NODE_C + "\n"})
                self.assertEqual(second.status_code, 200)
                self.assertEqual(second.json()["count"], 2)

                pool = self.client.get("/api/proxies/pool").json()
                self.assertEqual(pool["count"], 2)
                self.assertNotIn("uuid-a", pool["data"], "旧节点应被替换掉")
                self.assertIn("uuid-b", pool["data"])
                self.assertIn("uuid-c", pool["data"])

    def test_get_reports_empty_pool_for_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            loader = self._load_for_path(Path(tmp) / "missing.txt")
            with patch.object(self.server.engine, "load_config", side_effect=loader):
                pool = self.client.get("/api/proxies/pool").json()
                self.assertEqual(pool["count"], 0)
                self.assertEqual(pool["data"], "")

    def test_put_rejects_empty_and_unrecognizable_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            loader = self._load_for_path(Path(tmp) / "pool.txt")
            with patch.object(self.server.engine, "load_config", side_effect=loader):
                empty = self.client.put("/api/proxies/pool", json={"data": "  \n"})
                self.assertEqual(empty.status_code, 400)
                bad = self.client.put("/api/proxies/pool", json={"data": "hello world\n"})
                self.assertEqual(bad.status_code, 400)
                self.assertIn("代理池内容无效", bad.json()["detail"])


if __name__ == "__main__":
    unittest.main()
