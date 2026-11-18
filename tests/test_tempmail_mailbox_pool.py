"""临时邮箱池（tempmail）回归测试：池文件、运行时、provider 分发、配置校验与 Web API。"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import mail_service
import tempmail_mailbox_pool
from app_config import ConfigError, DEFAULT_CONFIG, validate_config_structure

try:
    from fastapi.testclient import TestClient
except ImportError:
    TestClient = None


def make_row(email="user1@zrfme.com", password="pw1", jwt="jwt-token-1"):
    return "----".join([email, password, jwt])


class TempMailPoolParseTests(unittest.TestCase):
    def test_parse_three_field_lines(self):
        entries = tempmail_mailbox_pool.parse_tempmail_entries(
            make_row() + "\n" + make_row("user2@zrf.me", "", "jwt-two") + "\n"
        )
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0].email, "user1@zrfme.com")
        self.assertEqual(entries[0].password, "pw1")
        self.assertEqual(entries[0].jwt, "jwt-token-1")
        self.assertEqual(entries[1].password, "")
        self.assertEqual(entries[1].jwt, "jwt-two")

    def test_parse_tolerates_dashes_inside_jwt(self):
        jwt = "eyJhbGciOiJIUzI1NiJ9----eyJzdWIiOiIxIn0.sig"
        data = "user3@zrf.me----pw----" + jwt
        entries = tempmail_mailbox_pool.parse_tempmail_entries(data)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].jwt, jwt)

    def test_parse_rejects_invalid_lines(self):
        entries = tempmail_mailbox_pool.parse_tempmail_entries(
            "not-an-email----pw----jwt\n\nno-separator-line\n"
        )
        self.assertEqual(entries, [])

    def test_inspect_counts_invalid_and_duplicates(self):
        data = make_row() + "\n" + make_row() + "\ngarbage-line\n"
        summary = tempmail_mailbox_pool.inspect_tempmail_pool(data)
        self.assertEqual(summary["count"], 2)
        self.assertEqual(summary["invalid"], 1)
        self.assertEqual(summary["duplicates"], ["user1@zrfme.com"])


class TempMailPoolFileTests(unittest.TestCase):
    def test_save_load_pop_append_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pool.txt"
            summary = tempmail_mailbox_pool.save_tempmail_pool(
                path, make_row() + "\n" + make_row("user2@zrf.me", "pw2", "jwt-two") + "\n"
            )
            self.assertEqual(summary["count"], 2)
            self.assertEqual(tempmail_mailbox_pool.get_tempmail_pool_capacity(path), 2)

            entry = tempmail_mailbox_pool.pop_tempmail_entry(path)
            self.assertEqual(entry.email, "user1@zrfme.com")
            self.assertEqual(tempmail_mailbox_pool.get_tempmail_pool_capacity(path), 1)

            written = tempmail_mailbox_pool.append_tempmail_entries(path, [entry])
            self.assertEqual(len(written), 1)
            self.assertEqual(tempmail_mailbox_pool.get_tempmail_pool_capacity(path), 2)
            loaded = tempmail_mailbox_pool.load_tempmail_pool(path)
            self.assertEqual(loaded["count"], 2)

    def test_append_skips_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pool.txt"
            tempmail_mailbox_pool.save_tempmail_pool(path, make_row() + "\n")
            written = tempmail_mailbox_pool.append_tempmail_entries(
                path, [tempmail_mailbox_pool.TempMailEntry(
                    "user1@zrfme.com", "pw1", "jwt-token-1")]
            )
            self.assertEqual(written, [])
            self.assertEqual(tempmail_mailbox_pool.get_tempmail_pool_capacity(path), 1)

    def test_pop_on_empty_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(
                tempmail_mailbox_pool.pop_tempmail_entry(Path(tmp) / "missing.txt")
            )


class TempMailRuntimeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "pool.txt"
        tempmail_mailbox_pool.save_tempmail_pool(
            self.path,
            make_row() + "\n" + make_row("user2@zrf.me", "pw2", "jwt-two") + "\n",
        )

    def test_acquire_release_before_submit_restores_pool(self):
        runtime = tempmail_mailbox_pool.create_tempmail_task_runtime(self.path)
        email, handle = runtime.acquire()
        self.assertEqual(email, "user1@zrfme.com")
        self.assertTrue(tempmail_mailbox_pool.is_tempmail_handle(handle))
        # acquire 立即从池文件移除
        self.assertEqual(runtime.remaining, 1)
        self.assertTrue(runtime.release_before_submit(handle, email))
        self.assertEqual(runtime.remaining, 2)

    def test_acquire_pops_in_order_and_consumes_on_wait(self):
        runtime = tempmail_mailbox_pool.create_tempmail_task_runtime(self.path)
        first_email, first_handle = runtime.acquire()
        second_email, _second = runtime.acquire()
        self.assertEqual(first_email, "user1@zrfme.com")
        self.assertEqual(second_email, "user2@zrf.me")
        with patch.object(mail_service, "tempmail_poll_code", return_value="123-ABC") as poll:
            code = runtime.wait_for_code(first_handle, first_email, timeout=60)
        self.assertEqual(code, "123-ABC")
        self.assertEqual(poll.call_args.args[0], "jwt-token-1")
        # 一次性消费：句柄失效
        with self.assertRaisesRegex(RuntimeError, "临时邮箱会话"):
            runtime.wait_for_code(first_handle, first_email, timeout=1)

    def test_release_rejects_mismatched_email(self):
        runtime = tempmail_mailbox_pool.create_tempmail_task_runtime(self.path)
        email, handle = runtime.acquire()
        with self.assertRaisesRegex(RuntimeError, "不匹配"):
            runtime.release_before_submit(handle, "someone-else@zrf.me")

    def test_close_returns_unsubmitted_leases(self):
        runtime = tempmail_mailbox_pool.create_tempmail_task_runtime(self.path)
        runtime.acquire()
        self.assertEqual(runtime.remaining, 1)
        runtime.close()
        self.assertEqual(runtime.remaining, 2)

    def test_runtime_requires_nonempty_pool(self):
        with self.assertRaisesRegex(ValueError, "没有有效邮箱"):
            tempmail_mailbox_pool.create_tempmail_task_runtime(
                Path(self._tmp.name) / "missing.txt"
            )

    def test_parallel_workers_share_pool_file(self):
        first = tempmail_mailbox_pool.create_tempmail_task_runtime(self.path)
        second = tempmail_mailbox_pool.create_tempmail_task_runtime(self.path)
        e1, h1 = first.acquire()
        e2, h2 = second.acquire()
        self.assertNotEqual(e1, e2)
        self.assertEqual(first.remaining, 0)


class TempMailProviderDispatchTests(unittest.TestCase):
    def setUp(self):
        self.prev_mail_config = mail_service.config
        self.prev_runtime = getattr(mail_service, "tempmail_runtime", None)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "pool.txt"
        tempmail_mailbox_pool.save_tempmail_pool(self.path, make_row() + "\n")
        mail_service.config = {
            "email_provider": "tempmail",
            "tempmail_api_base": "https://mail-api.example.test",
            "tempmail_pool_file": str(self.path),
            "tempmail_proxy": "",
        }

    def tearDown(self):
        mail_service.config = self.prev_mail_config
        if self.prev_runtime is not None:
            mail_service.tempmail_runtime = self.prev_runtime
        elif hasattr(mail_service, "tempmail_runtime"):
            delattr(mail_service, "tempmail_runtime")

    def _install_runtime(self):
        runtime = tempmail_mailbox_pool.create_tempmail_task_runtime(self.path)
        mail_service.tempmail_runtime = runtime
        self.addCleanup(runtime.close)
        return runtime

    def test_get_email_and_token_acquires_from_runtime(self):
        runtime = self._install_runtime()
        email, handle = mail_service.get_email_and_token()
        self.assertEqual(email, "user1@zrfme.com")
        self.assertTrue(handle.startswith("tempmail:"))

    def test_release_email_lease_restores_pool(self):
        runtime = self._install_runtime()
        email, handle = mail_service.get_email_and_token()
        self.assertTrue(mail_service.release_email_lease(handle, email))
        self.assertEqual(runtime.remaining, 1)

    def test_get_oai_code_routes_to_runtime(self):
        runtime = self._install_runtime()
        email, handle = mail_service.get_email_and_token()
        with patch.object(mail_service, "tempmail_poll_code", return_value="654-XYZ") as poll:
            code = mail_service.get_oai_code(handle, email, timeout=30)
        self.assertEqual(code, "654-XYZ")
        self.assertEqual(poll.call_args.args[0], "jwt-token-1")

    def test_tempmail_poll_code_reuses_cloudflare_loop_with_overrides(self):
        fake_response = Mock()
        with patch.object(mail_service, "cloudflare_get_oai_code", return_value="999") as outer, \
                patch.object(mail_service, "http_get", return_value=fake_response, create=True):
            code = mail_service.tempmail_poll_code("jwt-x", "u@zrf.me", timeout=10)
        self.assertEqual(code, "999")
        kwargs = outer.call_args.kwargs
        self.assertEqual(kwargs["api_base"], "https://mail-api.example.test")
        self.assertIs(kwargs["fetch_messages"], mail_service.tempmail_get_messages)
        self.assertIs(kwargs["fetch_detail"], mail_service.tempmail_get_message_detail)

    def test_cloudflare_get_oai_code_defaults_unchanged(self):
        # 不传 api_base 时保持原行为（读 cloudflare_api_base）
        mail_service.config["cloudflare_api_base"] = "https://cf.example.test"
        with patch.object(mail_service, "cloudflare_get_messages") as get_messages, \
                patch.object(mail_service, "sleep_with_cancel", create=True), \
                patch.object(mail_service, "raise_if_cancelled", create=True):
            get_messages.side_effect = RuntimeError("stop")
            with self.assertRaises(Exception):
                mail_service.cloudflare_get_oai_code("tok", "u@x.test", timeout=1)
        self.assertEqual(get_messages.call_args.args[0], "https://cf.example.test")

    def test_missing_runtime_raises_clear_error(self):
        if hasattr(mail_service, "tempmail_runtime"):
            delattr(mail_service, "tempmail_runtime")
        with self.assertRaisesRegex(RuntimeError, "临时邮箱池运行时未初始化"):
            mail_service.get_email_and_token()


class TempMailCreatorGuardTests(unittest.TestCase):
    def test_headless_mode_rejects_creation(self):
        import tempmail_creator
        with patch.dict("os.environ", {"GROK_REGISTER_CHROMIUM_HEADLESS": "1"}):
            with self.assertRaisesRegex(RuntimeError, "有头浏览器"):
                tempmail_creator.create_tempmail_mailboxes(1, domains=["zrf.me"])

    def test_screen_scale_windows_multiplies_dpr(self):
        import platform
        import tempmail_creator
        if platform.system() != "Windows":
            self.skipTest("Windows-only scaling rule")
        self.assertEqual(tempmail_creator._screen_scale(1.5), 1.5)
        self.assertEqual(tempmail_creator._screen_scale(0), 1.0)

    def test_create_requires_domains(self):
        import tempmail_creator
        with self.assertRaisesRegex(ValueError, "tempmail_domains"):
            tempmail_creator.create_tempmail_mailboxes(1, domains=[])


class TempMailConfigTests(unittest.TestCase):
    def test_validate_accepts_tempmail_defaults(self):
        cfg = validate_config_structure(dict(DEFAULT_CONFIG))
        self.assertEqual(cfg["tempmail_pool_file"], "./output/mailboxes/tempmail-pool.txt")

    def test_tempmail_run_requires_pool_with_entries(self):
        cfg = dict(DEFAULT_CONFIG)
        cfg["email_provider"] = "tempmail"
        cfg["tempmail_pool_file"] = "./output/mailboxes/definitely-missing-pool.txt"
        with self.assertRaises(ConfigError):
            validate_run_requirements_safe(cfg)

    def test_tempmail_run_passes_with_valid_pool(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pool.txt"
            tempmail_mailbox_pool.save_tempmail_pool(path, make_row() + "\n")
            cfg = dict(DEFAULT_CONFIG)
            cfg["email_provider"] = "tempmail"
            cfg["tempmail_pool_file"] = str(path)
            validate_run_requirements_safe(cfg)


def validate_run_requirements_safe(cfg):
    from app_config import validate_run_requirements
    return validate_run_requirements(cfg)


@unittest.skipIf(TestClient is None, "web dependencies not installed")
class TempMailWebApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from web import server
        cls.server = server
        cls.client = TestClient(server.app)

    def setUp(self):
        with self.server._job_lock:
            self.server._job_state["running"] = False
            self.server._maintenance_state = None
        with self.server._job_lock:
            self.server._tempmail_fill_state.update({
                "running": False, "target": 0, "created": 0, "error": None,
            })

    def _load_for_path(self, path):
        cfg = dict(self.server.engine.DEFAULT_CONFIG)
        cfg["tempmail_pool_file"] = str(path)
        def load():
            self.server.engine.config.clear()
            self.server.engine.config.update(cfg)
            return self.server.engine.config
        return load

    def test_pool_api_roundtrip(self):
        row = make_row("web1@zrfme.com", "pw", "web-jwt") + "\n"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tempmail.txt"
            loader = self._load_for_path(path)
            with patch.object(self.server.engine, "load_config", side_effect=loader):
                put = self.client.put("/api/mailboxes/tempmail", json={"data": row})
                self.assertEqual(put.status_code, 200)
                self.assertEqual(put.headers.get("cache-control"), "no-store")
                get = self.client.get("/api/mailboxes/tempmail")
                self.assertEqual(get.status_code, 200)
                body = get.json()
                self.assertEqual(body["count"], 1)
                self.assertIn("web-jwt", body["data"])
                self.assertNotIn("web-jwt", repr(body["accounts"]))
                self.assertEqual(body["fill"]["running"], False)

    def test_fill_validates_count(self):
        response = self.client.post("/api/mailboxes/tempmail/fill", json={"count": 0})
        self.assertEqual(response.status_code, 400)
        response = self.client.post("/api/mailboxes/tempmail/fill", json={"count": 101})
        self.assertEqual(response.status_code, 400)

    def test_fill_start_sets_maintenance_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pool.txt"
            loader = self._load_for_path(path)
            with patch.object(self.server.engine, "load_config", side_effect=loader), \
                    patch("tempmail_creator.create_tempmail_mailboxes", side_effect=RuntimeError("boom")) as create:
                response = self.client.post("/api/mailboxes/tempmail/fill", json={"count": 1})
                self.assertEqual(response.status_code, 200)
                # 等 worker 结束
                import time as _time
                deadline = _time.time() + 5
                while _time.time() < deadline:
                    state = self.client.get("/api/status").json()
                    if not state.get("maintenance"):
                        break
                    _time.sleep(0.05)
                fill = self.client.get("/api/mailboxes/tempmail").json()["fill"]
                self.assertFalse(fill["running"])
                self.assertIsNotNone(fill["error"])
                self.assertEqual(create.call_count, 1)
                self.assertEqual(create.call_args.args[0], 1)


if __name__ == "__main__":
    unittest.main()
