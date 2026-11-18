"""浏览器/代理运行时空闲回收看门狗（防内存泄漏）。

- sing-box 代理运行时：按 idle TTL 回收（proxy_protocol_runtime.cleanup_idle）
- 项目 chromium（DrissionPage 资料目录）：任务空闲期全部视为泄漏进程回收

安全规则：注册/重授权任务运行期间不动任何浏览器，只回收代理运行时。
"""
import os
import threading
import time

_INTERVAL_SEC = max(15, int(os.environ.get("GROK_REGISTER_JANITOR_INTERVAL_SEC") or 60))
_MARKERS = ("DrissionPage",)

_started = False
_start_lock = threading.Lock()


def _proc_is_project_browser(info) -> bool:
    name = str(info.get("name") or "").lower()
    if "chrome" not in name and "chromium" not in name and "msedge" not in name:
        return False
    raw = info.get("cmdline") or []
    if isinstance(raw, str):
        raw = [raw]
    cmdline = " ".join(str(x) for x in raw)
    return any(marker in cmdline for marker in _MARKERS)


def kill_project_chromium() -> int:
    """回收所有项目 chromium 进程（DrissionPage 资料目录的残留实例）。

    仅在任务空闲期调用；运行期由 stop_browser/cleanup_runtime_memory 负责清理。
    """
    import psutil

    victims = []
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            info = proc.info
            if _proc_is_project_browser(info):
                victims.append(proc)
        except Exception:
            continue
    killed = 0
    for proc in victims:
        try:
            proc.terminate()
            killed += 1
        except Exception:
            continue
    if victims:
        time.sleep(1.0)
        for proc in victims:
            try:
                if proc.is_running():
                    proc.kill()
            except Exception:
                continue
    return killed


def cleanup_proxy_runtimes() -> bool:
    """回收空闲的 sing-box 代理运行时。"""
    try:
        from proxy_pool_v3 import get_manager

        manager = get_manager()
        if manager is not None:
            manager._runtime.cleanup_idle()
            return True
    except Exception:
        pass
    return False


def janitor_once(busy_check=None, log=None) -> dict:
    """执行一次回收。busy_check() 为真时只回收代理运行时，不动浏览器。"""
    result = {"busy": False, "runtime_cleaned": False, "killed_chromium": 0}
    busy = False
    if busy_check is not None:
        try:
            busy = bool(busy_check())
        except Exception:
            busy = False
    result["busy"] = busy
    try:
        result["runtime_cleaned"] = cleanup_proxy_runtimes()
    except Exception:
        pass
    if busy:
        return result
    try:
        result["killed_chromium"] = kill_project_chromium()
        if result["killed_chromium"] and log:
            log("[janitor] 空闲回收 %d 个残留浏览器进程" % result["killed_chromium"])
    except Exception:
        pass
    return result


def start_janitor(busy_check=None, log=None, interval=None) -> bool:
    """启动空闲回收看门狗线程（进程内单例）。"""
    global _started
    with _start_lock:
        if _started:
            return False
        _started = True
    interval = max(15, int(interval or _INTERVAL_SEC))

    def _loop():
        while True:
            time.sleep(interval)
            try:
                janitor_once(busy_check=busy_check, log=log)
            except Exception:
                continue

    thread = threading.Thread(target=_loop, name="browser-janitor", daemon=True)
    thread.start()
    return True
