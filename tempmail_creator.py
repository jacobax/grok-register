"""在 mail.zrf.me（cloudflare_temp_email 前端）上通过真实浏览器创建临时邮箱。

站点的新建地址接口受 Cloudflare Turnstile 保护：CDP/JS 合成点击会被指纹识别，
拿不到 token（aws2 zai-auto 项目实测结论）。可用路径是驱动**有头** Chromium
打开站点 UI，用 OS 级真实鼠标输入点击 Turnstile 复选框，随后在页面上下文里
直接 fetch `/api/new_address`（携带 cf_token），拿到 address/jwt。

实现移植自 aws2:/opt/zai-auto/scripts/mail_pool.py（已长期稳定运行），
浏览器驱动改为本项目统一的 DrissionPage。
"""
from __future__ import annotations

import json
import os
import platform
import random
import subprocess
import time
from typing import Optional

from DrissionPage import Chromium, ChromiumOptions

from browser_runtime import apply_browser_proxy_option

_DEFAULT_UI_BASE = "https://mail.zrf.me"
_DEFAULT_API_BASE = "https://mail-api.zrf.me"
_WINDOW_SIZE = "1280,900"

# ---------------------------------------------------------------- JS 片段（移植自 zai-auto，已验证）

# 点击“创建邮箱”按钮打开弹窗（弹窗内承载 Turnstile 组件）。
# 可见性用 getClientRects 判断：站点头部按钮可能是 fixed 定位，offsetParent 会是 null。
OPEN_MODAL_JS = r"""()=>{
 const els=[...document.querySelectorAll('button,a,[role=button]')];
 const t=els.filter(e=>((e.innerText||'').trim()==='创建邮箱') && e.getClientRects().length>0);
 if(!t.length) return false;
 t[t.length-1].click();
 return true;
}"""

# 新 profile 首次访问会弹出站点公告，先关闭它。
DISMISS_ANNOUNCEMENT_JS = r"""()=>{
 const dialogs=[...document.querySelectorAll('.el-dialog__wrapper,[class*=dialog],[class*=modal]')]
   .filter(d=>d.offsetWidth&&d.offsetHeight);
 for(const d of dialogs){
   const btn=[...d.querySelectorAll('button')].find(b=>/我知道了|知道了|确定|关闭|好的|OK|Confirm/i.test((b.innerText||'').trim()));
   if(btn){btn.click(); return 'dismissed';}
 }
 return '';
}"""

# 计算 Turnstile iframe 的屏幕坐标（含浏览器窗口 chrome 偏移修正）。
TS_RECT_JS = r"""()=>{
 const ifr=[...document.querySelectorAll('iframe')].find(f=>((f.src||'').includes('challenges.cloudflare.com')));
 if(!ifr) return null;
 const r=ifr.getBoundingClientRect();
 if(r.width<50||r.height<20) return null;
 const bx=window.screenX+(window.outerWidth-window.innerWidth)/2;
 const by=window.screenY+(window.outerHeight-window.innerHeight);
 return {x:bx+r.x+r.width/2,y:by+r.y+r.height/2,w:r.width,h:r.height,
         dx:window.outerWidth-window.innerWidth,dy:window.outerHeight-window.innerHeight,
         dpr:window.devicePixelRatio||1};
}"""

# 读取 Turnstile token（隐藏 input）。
TOKEN_JS = r"""()=>{
 const e=document.querySelector('input[id^=cf-chl-widget]')||document.querySelector('input[name=cf-turnstile-response]');
 return e?(e.value||''):'';
}"""

# 页面上下文内同步创建地址（同步 XHR 避免 Promise 等待差异；API 支持 CORS）。
CREATE_JS = r"""(args)=>{
 const xhr=new XMLHttpRequest();
 xhr.open('POST',args.apiBase+'/api/new_address',false);
 xhr.setRequestHeader('Content-Type','application/json');
 xhr.send(JSON.stringify({name:args.name,domain:args.domain,cf_token:args.token,enableRandomSubdomain:false}));
 let body=null;
 try{body=JSON.parse(xhr.responseText)}catch(e){body=String(xhr.responseText||'').slice(0,300)}
 return {status:xhr.status,body:body};
}"""


def _check_headless() -> None:
    if os.environ.get("GROK_REGISTER_CHROMIUM_HEADLESS") == "1":
        raise RuntimeError(
            "临时邮箱创建必须有头浏览器（真实鼠标点击 Turnstile）；"
            "请取消 GROK_REGISTER_CHROMIUM_HEADLESS=1 后重试"
        )


def _discover_browser_path() -> str:
    """按项目约定与常见安装路径寻找 Chrome/Chromium/Edge 可执行文件。"""
    env_path = str(os.environ.get("CHROME_PATH") or "").strip()
    if env_path and os.path.isfile(env_path):
        return env_path
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    ]
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    if platform.system() == "Windows":
        try:
            import winreg

            for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                try:
                    with winreg.OpenKey(
                        root, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe"
                    ) as key:
                        value = str(winreg.QueryValueEx(key, None)[0] or "").strip()
                        if value and os.path.isfile(value):
                            return value
                except OSError:
                    continue
        except Exception:
            pass
    return ""


def _kill_profile_chrome(profile_dir) -> None:
    """结束仍在占用本 profile 的 Chrome 进程（上次异常退出残留会导致新实例连不上）。"""
    try:
        import psutil

        marker = str(profile_dir)
        for proc in psutil.process_iter(["name", "cmdline"]):
            try:
                info = proc.info
                name = str(info.get("name") or "").lower()
                if "chrome" not in name and "chromium" not in name and "msedge" not in name:
                    continue
                cmdline = " ".join(str(x) for x in (info.get("cmdline") or []))
                if marker in cmdline:
                    proc.terminate()
            except Exception:
                continue
        time.sleep(1.0)
    except Exception:
        pass


def _build_options(profile_dir, proxy) -> ChromiumOptions:
    options = ChromiumOptions()
    browser_path = _discover_browser_path()
    if browser_path:
        options.set_browser_path(browser_path)
    options.set_paths(user_data_path=str(profile_dir))
    # auto_port 必须放在 set_paths 之后：set_user_data_path 会把 _auto_port 重置为 False。
    options.auto_port()
    options.set_argument("--window-size=%s" % _WINDOW_SIZE)
    options.set_argument("--window-position=0,0")
    options.set_argument("--no-first-run")
    options.set_argument("--no-default-browser-check")
    options.set_argument("--disable-features=Translate,MediaRouter")
    if os.environ.get("GROK_REGISTER_CHROMIUM_NO_SANDBOX") == "1":
        options.set_argument("--no-sandbox")
    if os.environ.get("GROK_REGISTER_CHROMIUM_DISABLE_DEV_SHM_USAGE") == "1":
        options.set_argument("--disable-dev-shm-usage")
    if proxy:
        apply_browser_proxy_option(options, proxy)
    return options


# ---------------------------------------------------------------- 原生点击（OS 级真实输入）

def _screen_scale(dpr: float) -> float:
    """JS 屏幕坐标（DIP）→ OS 物理坐标换算系数。"""
    system = platform.system()
    if system == "Windows":
        # Chromium 的 screenX/screenY 在 Windows 上按 DIP 上报，SetCursorPos 用物理像素。
        return float(dpr or 1)
    return 1.0  # X11 无缩放；macOS 全局坐标即 point，无需换算


def _win_find_chrome_hwnd(pid=None):
    import ctypes

    user32 = ctypes.windll.user32
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        class_buf = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, class_buf, 64)
        if class_buf.value != "Chrome_WidgetWin_1":
            return True
        window_pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(window_pid))
        found.append((hwnd, window_pid.value))
        return True

    user32.EnumWindows(callback, None)
    if pid:
        for hwnd, window_pid in found:
            if window_pid == pid:
                return hwnd
    return found[0][0] if found else None


def _win_focus(hwnd) -> None:
    import ctypes

    user32 = ctypes.windll.user32
    try:
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        # 先按一下 ALT 绕过前台锁定，否则 SetForegroundWindow 可能被系统拒绝。
        user32.keybd_event(0x12, 0, 0, 0)
        user32.SetForegroundWindow(hwnd)
        user32.keybd_event(0x12, 0, 2, 0)
    except Exception:
        pass
    time.sleep(0.25)


def _win_click(x: float, y: float, browser_pid=None) -> None:
    import ctypes

    user32 = ctypes.windll.user32
    hwnd = _win_find_chrome_hwnd(browser_pid)
    if hwnd:
        _win_focus(hwnd)
    user32.SetCursorPos(int(x), int(y))
    time.sleep(0.2)
    user32.SetCursorPos(int(x) + 2, int(y) + 1)
    time.sleep(0.15)
    user32.mouse_event(0x0002, 0, 0, 0, 0)  # LEFTDOWN
    time.sleep(0.06)
    user32.mouse_event(0x0004, 0, 0, 0, 0)  # LEFTUP


def _linux_click(x: float, y: float, log_callback=None) -> None:
    # 不用 --sync：Xvfb 21.x 的核心指针查询不反映 XTEST 合成移动（页面事件流
    # 实测已精确到位），--sync 会因位置永不匹配而无限轮询挂起（aws1 实测）。
    env = dict(os.environ)
    env.setdefault("DISPLAY", ":0")
    try:
        subprocess.run(
            ["xdotool", "mousemove", str(int(x)), str(int(y))],
            env=env, timeout=10, check=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        time.sleep(0.2)
        subprocess.run(
            ["xdotool", "mousemove", str(int(x) + 2), str(int(y) + 1)],
            env=env, timeout=10, check=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        time.sleep(0.15)
        subprocess.run(
            ["xdotool", "click", "1"], env=env, timeout=10, check=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        raise RuntimeError("Linux 创建临时邮箱需要 xdotool（apt install xdotool）")
    except subprocess.CalledProcessError as exc:
        raise RuntimeError("xdotool 点击失败: %s" % exc) from exc


def _mac_click(x: float, y: float) -> None:
    try:
        subprocess.run(
            ["osascript", "-e", 'tell application "Google Chrome" to activate'],
            timeout=10, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass
    time.sleep(0.25)
    try:
        core_graphics = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics"
        )
        point = type("CGPoint", (ctypes.Structure,), {})()
        point.x = ctypes.c_double(x)
        point.y = ctypes.c_double(y)
        # kCGEventLeftMouseDown=1, Up=2; kCGHIDEventTap=0
        move = core_graphics.CGEventCreateMouseEvent(None, 5, point, 0)  # 5=MouseMoved
        core_graphics.CGEventPost(0, move)
        time.sleep(0.1)
        for event_type in (1, 2):
            event = core_graphics.CGEventCreateMouseEvent(None, event_type, point, 0)
            core_graphics.CGEventPost(0, event)
            time.sleep(0.05)
    except Exception as exc:
        raise RuntimeError(
            "macOS 原生点击失败（需要在系统设置中授予终端辅助功能权限）: %s" % exc
        ) from exc


def native_click(x: float, y: float, dpr: float = 1.0, browser_pid=None, log_callback=None) -> None:
    scale = _screen_scale(dpr)
    px, py = x * scale, y * scale
    system = platform.system()
    if system == "Windows":
        _win_click(px, py, browser_pid=browser_pid)
    elif system == "Linux":
        _linux_click(px, py, log_callback=log_callback)
    elif system == "Darwin":
        _mac_click(px, py)
    else:
        raise RuntimeError("当前平台(%s)不支持创建临时邮箱所需的原生点击" % system)


# ---------------------------------------------------------------- AX 树定位 Turnstile（closed shadow DOM 兜底）

def _ax_turnstile_rect(tab):
    try:
        tab.run_cdp("DOM.enable")
        tab.run_cdp("Accessibility.enable")
        ax = tab.run_cdp("Accessibility.getFullAXTree")
        for node in ax.get("nodes", []):
            if (node.get("role") or {}).get("value") != "Iframe":
                continue
            name = (node.get("name") or {}).get("value") or ""
            if "cloudflare" not in name.lower():
                continue
            bid = node.get("backendDOMNodeId")
            if not bid:
                continue
            box = tab.run_cdp("DOM.getBoxModel", backendNodeId=bid)
            quad = (box.get("model") or {}).get("content") or []
            if len(quad) < 8:
                continue
            xs, ys = quad[0::2], quad[1::2]
            w, h = max(xs) - min(xs), max(ys) - min(ys)
            if w < 50 or h < 20:
                continue
            cx, cy = min(xs) + 24, (max(ys) + min(ys)) / 2
            geo = tab.run_js(
                "(()=>({dx:(window.outerWidth-window.innerWidth)/2,"
                "dy:window.outerHeight-window.innerHeight,"
                "sx:window.screenX,sy:window.screenY,"
                "dpr:window.devicePixelRatio||1}))()",
                as_expr=True,
            ) or {}
            return {
                "x": geo.get("sx", 0) + geo.get("dx", 0) + cx,
                "y": geo.get("sy", 0) + geo.get("dy", 0) + cy,
                "w": w, "h": h, "dpr": geo.get("dpr", 1),
            }
    except Exception:
        return None
    return None


def _locate_turnstile(tab):
    rect = tab.run_js("(" + TS_RECT_JS + ")()", as_expr=True)
    if not rect:
        rect = _ax_turnstile_rect(tab)
    return rect


def _wait_js(tab, expr: str, timeout: float, interval: float = 0.4):
    end = time.time() + timeout
    while time.time() < end:
        try:
            value = tab.run_js("(" + expr + ")()", as_expr=True)
            if value:
                return value
        except Exception:
            pass
        time.sleep(interval)
    return None


# ---------------------------------------------------------------- 创建流程

def create_one(tab, name: str, domain: str, api_base: str, browser_pid=None,
               cancel_callback=None, cancelled_exception=None, log_callback=None,
               ui_base: str = "") -> dict:
    def _check_cancelled():
        if cancel_callback and cancel_callback():
            if cancelled_exception is not None:
                raise cancelled_exception("用户停止注册")
            raise RuntimeError("任务已停止")

    ui_base = (str(ui_base or "").strip() or _DEFAULT_UI_BASE).rstrip("/")
    tab.get(ui_base + "/")
    time.sleep(1.5)
    if not _wait_js(tab, r"()=>document.readyState!=='loading'", 25):
        raise RuntimeError("mail 站点加载超时")
    time.sleep(1.2)
    # 新 profile 首次访问会弹公告弹窗，挡住主界面，先关闭。
    try:
        dismissed = tab.run_js("(" + DISMISS_ANNOUNCEMENT_JS + ")()", as_expr=True)
        if dismissed:
            time.sleep(1.0)
    except Exception:
        pass
    _check_cancelled()

    if not _wait_js(tab, OPEN_MODAL_JS, 15):
        raise RuntimeError("未找到“创建邮箱”按钮（站点 UI 可能已变化）")
    time.sleep(2.5)
    _check_cancelled()

    token = None
    last_rect = None
    for click_try in range(3):
        _check_cancelled()
        rect = _locate_turnstile(tab)
        if rect:
            last_rect = rect
            if log_callback:
                log_callback(
                    "[Debug] Turnstile 定位: %.0fx%.0f click=(%.0f,%.0f) dpr=%.2f"
                    % (rect["w"], rect["h"], rect["x"], rect["y"], rect.get("dpr", 1))
                )
            native_click(
                rect["x"], rect["y"], dpr=rect.get("dpr", 1),
                browser_pid=browser_pid, log_callback=log_callback,
            )
            token = _wait_js(tab, TOKEN_JS, 15, 0.8)
            if token:
                break
            if log_callback:
                log_callback("[Debug] Turnstile 点击第 %s 次未取得 token，重试" % (click_try + 1))
        else:
            if log_callback:
                log_callback("[Debug] 未定位到 Turnstile iframe（第 %s 次）" % (click_try + 1))
        time.sleep(1.5)

    if not token:
        raise RuntimeError(
            "Turnstile 未通过（%s）；请确认浏览器窗口未被遮挡、屏幕未锁屏"
            % ("已定位" if last_rect else "未定位到 iframe")
        )
    _check_cancelled()

    result = tab.run_js(
        "(" + CREATE_JS + ")(" + json.dumps({
            "apiBase": api_base, "name": name, "domain": domain, "token": token,
        }) + ")",
        as_expr=True,
    )
    if not result or result.get("status") != 200:
        raise RuntimeError("创建邮箱接口失败: %s" % json.dumps(result, ensure_ascii=False)[:300])
    body = result.get("body")
    if not isinstance(body, dict) or not body.get("jwt"):
        raise RuntimeError("创建邮箱返回缺少 jwt: %s" % json.dumps(body, ensure_ascii=False)[:300])
    return {
        "email": str(body.get("address") or ("%s@%s" % (name, domain))).strip(),
        "name": name,
        "domain": domain,
        "password": str(body.get("password") or ""),
        "jwt": str(body.get("jwt")).strip(),
        "address_id": body.get("address_id"),
        "created_at": int(time.time()),
    }


def create_tempmail_mailboxes(
    count: int,
    *,
    api_base: str = _DEFAULT_API_BASE,
    domains=None,
    proxy: str = "",
    ui_base: str = "",
    profile_dir: Optional[str] = None,
    log_callback=None,
    cancel_callback=None,
    cancelled_exception=None,
    retries: int = 3,
    name_prefix: str = "",
    name_length: int = 10,
    gap_sec=(3, 6),
) -> list:
    """批量创建邮箱，返回 [{email, name, domain, password, jwt, address_id, created_at}]。

    失败的邮箱会按 retries 重试换域名再试；全部失败时抛出异常并保留已完成部分。
    """
    _check_headless()
    count = max(1, int(count))
    if not domains:
        raise ValueError("tempmail_domains 未配置，无法创建邮箱")
    domains = [str(item).strip().lstrip("@") for item in domains if str(item).strip()]
    if not domains:
        raise ValueError("tempmail_domains 未配置，无法创建邮箱")

    import secrets as _secrets
    import string as _string

    from pathlib import Path

    if profile_dir:
        profile = Path(profile_dir)
    else:
        profile = Path("./output/mailboxes/tempmail-profile")
    profile.expanduser().mkdir(parents=True, exist_ok=True)

    def log(message):
        if log_callback:
            log_callback(message)

    browser = None
    created = []
    try:
        _kill_profile_chrome(profile)
        browser = Chromium(_build_options(profile, proxy))
        tabs = browser.get_tabs()
        tab = tabs[-1] if tabs else browser.new_tab()
        browser_pid = getattr(browser, "process_id", None)
        if browser_pid is None:
            process = getattr(browser, "process", None)
            browser_pid = getattr(process, "pid", None)
        random.shuffle(domains)
        log("[*] 浏览器已启动，开始创建 %s 个临时邮箱（代理: %s）" % (count, proxy or "直连"))
        for index in range(1, count + 1):
            last_error = None
            for attempt in range(max(1, int(retries))):
                if cancel_callback and cancel_callback():
                    if cancelled_exception is not None:
                        raise cancelled_exception("用户停止注册")
                    raise RuntimeError("任务已停止")
                chars = _string.ascii_lowercase + _string.digits
                name = (name_prefix or "") + "".join(
                    _secrets.choice(chars) for _ in range(name_length)
                )
                domain = domains[(index + attempt) % len(domains)]
                try:
                    record = create_one(
                        tab, name, domain, api_base,
                        browser_pid=browser_pid,
                        cancel_callback=cancel_callback,
                        cancelled_exception=cancelled_exception,
                        log_callback=log_callback,
                        ui_base=ui_base,
                    )
                    created.append(record)
                    log("[+] 临时邮箱创建成功: %s（%s/%s）" % (record["email"], index, count))
                    last_error = None
                    break
                except Exception as exc:
                    last_error = exc
                    log("[!] 邮箱 %s 创建失败(第%s次): %s" % (index, attempt + 1, str(exc)[:200]))
                    time.sleep(2)
            if last_error is not None:
                raise RuntimeError("临时邮箱 %s 创建失败（已重试 %s 次）: %s" % (index, retries, last_error))
            if index < count:
                time.sleep(random.uniform(*gap_sec))
    finally:
        if browser is not None:
            try:
                browser.quit()  # 保留 user-data-dir（Turnstile 通过率依赖历史 cookie）
            except Exception:
                pass
    return created


def fetch_site_domains(api_base: str = _DEFAULT_API_BASE, proxies=None, timeout: int = 15) -> list:
    """读取站点公开设置里的可用域名列表。"""
    import requests

    response = requests.get(
        api_base.rstrip("/") + "/open_api/settings",
        proxies=proxies or None,
        timeout=timeout,
    )
    response.raise_for_status()
    data = response.json()
    domains = data.get("domains") or data.get("defaultDomains") or []
    return [str(item).strip().lstrip("@") for item in domains if str(item).strip()]
