"""Thread-safe temp-mail mailbox pool (mail.zrf.me / cloudflare_temp_email).

池文件每行一个邮箱：`email----password----jwt`（password 可为空，即
`email----jwt`）。jwt 是 mail-api.zrf.me 签发的地址令牌，读取验证码邮件
时作为 Bearer 凭据使用；因此池文件与 Outlook 池一样按私密文件对待。

注册任务的运行时语义与 OutlookMailboxTaskRuntime 对齐：
- acquire() 从池文件弹出一个邮箱并立即持久化（池 = 当前可用余量）；
- 提交注册表单前失败可通过 release_before_submit() 归还；
- wait_for_code() 一次性消费租约（邮箱已被 Grok 注册占用，不可复用）。
"""
from __future__ import annotations

from dataclasses import dataclass
import re
import secrets
import threading
from typing import Optional, Union

from filelock import FileLock

_MAX_POOL_BYTES = 1_000_000
_HANDLE_PREFIX = "tempmail:"
_DEFAULT_POOL_PATH = "./output/mailboxes/tempmail-pool.txt"
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+$")


@dataclass
class TempMailEntry:
    email: str
    password: str
    jwt: str

    def to_line(self) -> str:
        return "----".join([self.email, self.password, self.jwt])


def parse_tempmail_entries(data: str) -> list[TempMailEntry]:
    entries = []
    normalized = str(data or "").replace("\r\n", "\n").replace("\r", "\n")
    for line in normalized.split("\n"):
        raw = line.strip()
        if not raw:
            continue
        # jwt 里可能出现短横线，只按分隔符切前两段，剩余全部视为 jwt。
        parts = raw.split("----", 2)
        if len(parts) == 2:
            email, password, jwt = parts[0], "", parts[1]
        elif len(parts) == 3:
            email, password, jwt = parts
        else:
            continue
        email = email.strip()
        password = password.strip()
        jwt = jwt.strip()
        if not _EMAIL_RE.match(email) or not jwt:
            continue
        entries.append(TempMailEntry(email=email, password=password, jwt=jwt))
    return entries


def inspect_tempmail_pool(data: str) -> dict:
    normalized = str(data or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    lines = [line.strip() for line in normalized.split("\n") if line.strip()]
    entries = parse_tempmail_entries(normalized)
    seen = set()
    duplicates = []
    for entry in entries:
        key = entry.email.lower()
        if key in seen and key not in duplicates:
            duplicates.append(key)
        seen.add(key)
    return {
        "count": len(entries),
        "invalid": max(0, len(lines) - len(entries)),
        "duplicates": duplicates,
        "accounts": [{"email": entry.email} for entry in entries],
    }


def _validate_pool_data(data: str):
    normalized = str(data or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        raise ValueError("临时邮箱池不能为空")
    if len(normalized.encode("utf-8")) > _MAX_POOL_BYTES:
        raise ValueError("临时邮箱池过大")
    summary = inspect_tempmail_pool(normalized)
    if summary["invalid"]:
        raise ValueError(
            "临时邮箱池中有 %s 条格式无效的记录；每行应为 "
            "email----password----jwt" % summary["invalid"]
        )
    if summary["duplicates"]:
        raise ValueError("临时邮箱池存在重复邮箱: " + ", ".join(summary["duplicates"][:3]))
    entries = parse_tempmail_entries(normalized)
    if not entries:
        raise ValueError("临时邮箱池没有有效邮箱")
    return normalized + "\n", entries


def _canonical_path(path: Union[str, "os.PathLike"]) -> "Path":  # noqa: F821
    from pathlib import Path

    raw = str(path or "").strip() or _DEFAULT_POOL_PATH
    return Path(raw).expanduser().resolve()


def _read_pool_text(path, missing_ok: bool = False):
    from pathlib import Path

    target = _canonical_path(path)
    try:
        with target.open("rb") as handle:
            raw = handle.read(_MAX_POOL_BYTES + 1)
    except FileNotFoundError as exc:
        if missing_ok:
            return target, ""
        raise ValueError("临时邮箱池文件不存在: %s" % target) from exc
    except OSError as exc:
        raise RuntimeError("读取临时邮箱池失败: %s" % exc) from exc
    if len(raw) > _MAX_POOL_BYTES:
        raise ValueError("临时邮箱池过大")
    try:
        return target, raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("临时邮箱池必须是 UTF-8 文本") from exc


def _write_private_file(path: "Path", data: str) -> None:  # noqa: F821
    import os
    import tempfile
    from pathlib import Path

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".%s." % path.name, dir=str(path.parent))
    temp_path = Path(temp_name)
    try:
        try:
            os.fchmod(fd, 0o600)
        except (AttributeError, OSError):
            pass
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temp_path), str(path))
        try:
            path.chmod(0o600)
        except OSError:
            pass
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass


def load_tempmail_pool(path) -> dict:
    target, data = _read_pool_text(path, missing_ok=True)
    return {"path": str(target), "data": data, **inspect_tempmail_pool(data)}


def save_tempmail_pool(path, data: str) -> dict:
    normalized, entries = _validate_pool_data(data)
    target = _canonical_path(path)
    with FileLock(str(target) + ".lock", timeout=30):
        _write_private_file(target, normalized)
    return {
        "path": str(target),
        "count": len(entries),
        "accounts": [{"email": entry.email} for entry in entries],
    }


def get_tempmail_pool_capacity(path) -> int:
    target, data = _read_pool_text(path, missing_ok=True)
    if not data.strip():
        return 0
    _normalized, entries = _validate_pool_data(data)
    return len(entries)


def pop_tempmail_entry(path) -> Optional[TempMailEntry]:
    """从池文件弹出第一个可用邮箱并持久化剩余部分。"""
    target = _canonical_path(path)
    with FileLock(str(target) + ".lock", timeout=30):
        _target, data = _read_pool_text(target, missing_ok=True)
        entries = parse_tempmail_entries(data)
        if not entries:
            return None
        entry = entries.pop(0)
        remaining = "\n".join(item.to_line() for item in entries)
        _write_private_file(target, remaining + "\n" if remaining else "")
    return entry


def append_tempmail_entries(path, entries) -> list[TempMailEntry]:
    """追加邮箱到池文件；重复邮箱会被跳过并返回实际写入的条目。"""
    normalized_entries = []
    for item in entries:
        if isinstance(item, TempMailEntry):
            normalized_entries.append(item)
        else:
            normalized_entries.append(
                TempMailEntry(
                    email=str(item.get("email", "")).strip(),
                    password=str(item.get("password", "") or "").strip(),
                    jwt=str(item.get("jwt", "")).strip(),
                )
            )
    normalized_entries = [
        item for item in normalized_entries if _EMAIL_RE.match(item.email) and item.jwt
    ]
    if not normalized_entries:
        return []
    target = _canonical_path(path)
    written = []
    with FileLock(str(target) + ".lock", timeout=30):
        _target, data = _read_pool_text(target, missing_ok=True)
        existing = {item.email.lower() for item in parse_tempmail_entries(data)}
        lines = [line for line in data.splitlines() if line.strip()]
        for item in normalized_entries:
            key = item.email.lower()
            if key in existing:
                continue
            existing.add(key)
            lines.append(item.to_line())
            written.append(item)
        if written:
            _write_private_file(target, "\n".join(lines) + "\n")
    return written


def _probe_entries(entries, api_base, proxies=None, timeout=20) -> dict:
    """逐个邮箱探测 jwt 是否仍能读取邮件列表。"""
    import requests

    results = []
    healthy = 0
    for entry in entries:
        usable = False
        error = ""
        try:
            response = requests.get(
                api_base.rstrip("/") + "/api/mails",
                params={"limit": 1, "offset": 0},
                headers={"Authorization": "Bearer " + entry.jwt},
                proxies=proxies or None,
                timeout=timeout,
            )
            if response.status_code < 400:
                usable = True
            else:
                error = "HTTP %s" % response.status_code
        except Exception as exc:
            error = str(exc)
        if usable:
            healthy += 1
        results.append({"email": entry.email, "usable": usable, "error": error})
    return {
        "count": len(results),
        "healthy": healthy,
        "unhealthy": len(results) - healthy,
        "results": results,
    }


def probe_tempmail_pool_data(data: str, api_base: str, proxies=None) -> dict:
    _normalized, entries = _validate_pool_data(data)
    return _probe_entries(entries, api_base, proxies=proxies)


def probe_tempmail_pool(path, api_base: str, proxies=None) -> dict:
    _target, data = _read_pool_text(path, missing_ok=False)
    _normalized, entries = _validate_pool_data(data)
    return _probe_entries(entries, api_base, proxies=proxies)


class TempMailTaskRuntime:
    """Per-task allocator over the persisted pool file.

    与 OutlookTaskRuntime 相同的租约语义：acquire 领取、submit 前可归还、
    wait_for_code 一次性消费。区别是池本身持久化在文件里，acquire 会立即
    从文件移除该邮箱，避免并发任务重复领取。
    """

    def __init__(self, path, log_callback=None, cancelled_exception=None) -> None:
        self._path = str(path or "").strip()
        if get_tempmail_pool_capacity(self._path) <= 0:
            raise ValueError("临时邮箱池没有有效邮箱: %s" % self._path)
        self._log_callback = log_callback
        self._cancelled_exception = cancelled_exception
        self._lock = threading.RLock()
        self._leases = {}
        self._closed = False

    @property
    def path(self) -> str:
        return self._path

    @property
    def remaining(self) -> int:
        try:
            return get_tempmail_pool_capacity(self._path)
        except Exception:
            return 0

    def _raise_if_cancelled(self, cancel_callback=None) -> None:
        if not cancel_callback or not cancel_callback():
            return
        if self._cancelled_exception is not None:
            raise self._cancelled_exception("用户停止注册")
        raise RuntimeError("任务已停止")

    def acquire(self, log_callback=None):
        entry = pop_tempmail_entry(self._path)
        if entry is None:
            raise RuntimeError("临时邮箱池已耗尽，请在邮箱池页面补充邮箱")
        with self._lock:
            if self._closed:
                append_tempmail_entries(self._path, [entry])
                raise RuntimeError("临时邮箱任务运行时已关闭")
            handle = _HANDLE_PREFIX + secrets.token_urlsafe(24)
            self._leases[handle] = entry
        logger = log_callback or self._log_callback
        if logger:
            logger("[*] 已从临时邮箱池领取: %s（剩余 %s 个）" % (entry.email, self.remaining))
        return entry.email, handle

    def release_before_submit(self, handle: str, email: str) -> bool:
        with self._lock:
            key = str(handle or "")
            entry = self._leases.get(key)
            if entry is None:
                return False
            if entry.email.lower() != str(email or "").lower():
                raise RuntimeError("临时邮箱会话与目标邮箱不匹配")
            del self._leases[key]
        append_tempmail_entries(self._path, [entry])
        return True

    def wait_for_code(
        self,
        handle: str,
        email: str,
        timeout: int = 180,
        poll_interval: int = 3,
        log_callback=None,
        cancel_callback=None,
        resend_callback=None,
    ) -> str:
        with self._lock:
            if self._closed:
                raise RuntimeError("临时邮箱任务运行时已关闭")
            key = str(handle or "")
            entry = self._leases.get(key)
            if entry is None:
                raise RuntimeError("临时邮箱会话不存在、已使用或已失效")
            if entry.email.lower() != str(email or "").lower():
                raise RuntimeError("临时邮箱会话与目标邮箱不匹配")
            # 一次性消费：开始轮询后租约即失效，邮箱已提交注册不可复用。
            del self._leases[key]
        self._raise_if_cancelled(cancel_callback)
        try:
            import mail_service

            code = mail_service.tempmail_poll_code(
                entry.jwt,
                email,
                timeout=int(timeout),
                poll_interval=int(poll_interval),
                log_callback=log_callback,
                cancel_callback=cancel_callback,
                resend_callback=resend_callback,
            )
        except Exception:
            self._raise_if_cancelled(cancel_callback)
            raise
        self._raise_if_cancelled(cancel_callback)
        return str(code)

    def status(self) -> dict:
        with self._lock:
            return {
                "remaining": self.remaining,
                "leased": len(self._leases),
                "closed": self._closed,
            }

    def close(self) -> None:
        with self._lock:
            self._closed = True
            leases = list(self._leases.values())
            self._leases.clear()
        if leases and self._log_callback:
            self._log_callback(
                "[!] 任务结束时仍有 %s 个临时邮箱未提交注册，已归还邮箱池" % len(leases)
            )
        try:
            append_tempmail_entries(self._path, leases)
        except Exception as exc:
            if self._log_callback:
                self._log_callback("[!] 未提交邮箱归还邮箱池失败: %s" % exc)


def create_tempmail_task_runtime(path, log_callback=None, cancelled_exception=None):
    return TempMailTaskRuntime(
        path, log_callback=log_callback, cancelled_exception=cancelled_exception
    )


def is_tempmail_handle(value: str) -> bool:
    return str(value or "").startswith(_HANDLE_PREFIX)
