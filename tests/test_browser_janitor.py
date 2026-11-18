# -*- coding: utf-8 -*-
"""看门狗测试：进程识别、忙闲语义、运行时回收。"""
import psutil
import unittest
from unittest.mock import patch

import browser_janitor


def _proc(pid, name, cmdline):
    return {"pid": pid, "name": name, "cmdline": cmdline}


class FakePsutilProc:
    def __init__(self, info, alive=True):
        self.info = dict(info)
        self._alive = alive
        self.terminated = False
        self.killed = False

    def terminate(self):
        self.terminated = True

    def is_running(self):
        return self._alive

    def kill(self):
        self.killed = True


class KillProjectChromiumTests(unittest.TestCase):
    def test_kills_only_project_browser_processes(self):
        victims = [
            FakePsutilProc(_proc(1, "chromium", "/tmp/DrissionPage/autoPortData/a --headless")),
            FakePsutilProc(_proc(2, "chromium", "/tmp/DrissionPage/autoPortData/b")),
        ]
        spared = [
            FakePsutilProc(_proc(3, "chromium", "/usr/bin/some-other-browser")),
            FakePsutilProc(_proc(4, "python", "python -m uvicorn web.server:app")),
            FakePsutilProc(_proc(5, "sing-box", "sing-box run -c /tmp/x.json")),
        ]

        def fake_iter(fields):
            return list(victims) + list(spared)

        with patch.object(psutil, "process_iter", side_effect=fake_iter):
            killed = browser_janitor.kill_project_chromium()

        self.assertEqual(killed, 2)
        for proc in victims:
            self.assertTrue(proc.terminated)
        for proc in spared:
            self.assertFalse(proc.terminated)

    def test_kill_escalates_to_kill_for_survivors(self):
        survivor = FakePsutilProc(_proc(1, "chromium", "/tmp/DrissionPage/a"), alive=True)
        with patch.object(psutil, "process_iter",
                          side_effect=lambda fields: [survivor]):
            killed = browser_janitor.kill_project_chromium()
        self.assertEqual(killed, 1)
        self.assertTrue(survivor.killed)


class JanitorOnceTests(unittest.TestCase):
    def test_busy_skips_browser_kill_but_cleans_runtimes(self):
        cleaned = {"n": 0}

        class FakeRuntime:
            def cleanup_idle(self):
                cleaned["n"] += 1

        class FakeManager:
            _runtime = FakeRuntime()

        def busy():
            return True

        with patch.object(browser_janitor, "kill_project_chromium",
                          side_effect=AssertionError("busy 时不得杀浏览器")) as kill_mock, \
             patch.object(browser_janitor, "cleanup_proxy_runtimes", return_value=True):
            result = browser_janitor.janitor_once(busy_check=busy, log=None)

        self.assertTrue(result["busy"])
        self.assertEqual(result["killed_chromium"], 0)
        kill_mock.assert_not_called()

    def test_idle_kills_leaked_browsers(self):
        with patch.object(browser_janitor, "kill_project_chromium", return_value=3) as kill_mock, \
             patch.object(browser_janitor, "cleanup_proxy_runtimes", return_value=True):
            result = browser_janitor.janitor_once(busy_check=lambda: False, log=None)
        self.assertEqual(result["killed_chromium"], 3)
        kill_mock.assert_called_once()

    def test_start_janitor_is_single_instance(self):
        # 已启动时拒绝重复启动
        with patch.object(browser_janitor, "_started", True), \
             patch.object(browser_janitor.threading, "Thread") as thread_cls:
            self.assertFalse(browser_janitor.start_janitor(busy_check=None, interval=15))
            thread_cls.assert_not_called()
        # 未启动时只启动一次
        with patch.object(browser_janitor, "_started", False), \
             patch.object(browser_janitor.threading, "Thread") as thread_cls:
            self.assertTrue(browser_janitor.start_janitor(busy_check=None, interval=15))
            self.assertEqual(thread_cls.call_count, 1)


if __name__ == "__main__":
    unittest.main()
