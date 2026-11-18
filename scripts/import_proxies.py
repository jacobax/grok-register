#!/usr/bin/env python3
"""读取已验证的 ip:port[#country] 列表，批量填入 vless 模板，导入 grok-register 面板。

用法（GitHub Actions 或本地）：
  python3 scripts/import_proxies.py \
    --list proxies_validated.txt \
    --template proxy-template.txt \
    --panel https://grok-ac.ein.us.ci \
    --token "$GROK_PANEL_TOKEN" \
    [--set-pool-mode] [--dry-run] [--limit 2000]

仅依赖标准库；--dry-run 只生成不导入。
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", required=True, help="ip:port[#country] 列表文件路径")
    parser.add_argument("--template", required=True, help="含 {ip}/{port} 占位符的 vless 模板文件")
    parser.add_argument("--panel", required=True, help="面板地址，如 https://grok-ac.ein.us.ci")
    parser.add_argument("--token", default="", help="面板访问令牌（未设置面板门禁时可留空）")
    parser.add_argument("--pool-path", default="", help="面板侧代理池文件路径（默认用面板当前配置）")
    parser.add_argument("--limit", type=int, default=2000, help="最多导入节点数")
    parser.add_argument("--set-pool-mode", action="store_true", help="同时把面板代理模式切到 pool")
    parser.add_argument("--dry-run", action="store_true", help="只生成节点行，不请求面板")
    return parser.parse_args()


def load_entries(path: Path, limit: int) -> list[tuple[str, str, str]]:
    entries: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    skipped = 0
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        country = ""
        if "#" in line:
            line, country = line.split("#", 1)
            country = country.strip()
        ip_port = line.strip()
        if ":" not in ip_port:
            skipped += 1
            continue
        ip, _, port = ip_port.rpartition(":")
        ip, port = ip.strip(), port.strip()
        if not ip or not port.isdigit():
            skipped += 1
            continue
        key = ip + ":" + port
        if key in seen:
            continue
        seen.add(key)
        entries.append((ip, port, country))
        if len(entries) >= limit:
            break
    if skipped:
        print(f"[!] 跳过 {skipped} 行无法解析的条目")
    return entries


def render(template: str, ip: str, port: str, country: str) -> str:
    if "{ip}" not in template or "{port}" not in template:
        raise SystemExit("模板缺少 {ip}/{port} 占位符")
    line = template.replace("{ip}", ip).replace("{port}", port)
    if "#" in line:
        base, _, fragment = line.rpartition("#")
        parts = [p for p in (fragment.strip(), country.strip(), ip) if p]
        line = base + "#" + "-".join(parts)
    return line


def api_request(panel: str, path: str, token: str, payload: dict | None, method: str = "POST") -> dict:
    url = panel.rstrip("/") + path
    headers = {
        "Content-Type": "application/json",
        # Cloudflare 对 python-urllib 等脚本 UA 会返回 error 1010（浏览器签名封禁）
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    }
    if token:
        headers["Authorization"] = "Bearer " + token
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8", "replace")
            return {"status": response.status, "body": body[:500]}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise SystemExit(f"[!] {method} {path} -> HTTP {exc.code}: {detail}")
    except urllib.error.URLError as exc:
        raise SystemExit(f"[!] {method} {path} 连接失败: {exc.reason}")


def main() -> int:
    args = parse_args()
    list_path = Path(args.list)
    template_path = Path(args.template)
    if not list_path.is_file():
        raise SystemExit(f"[!] 列表文件不存在: {list_path}")
    if not template_path.is_file():
        raise SystemExit(f"[!] 模板文件不存在: {template_path}")

    entries = load_entries(list_path, args.limit)
    if not entries:
        raise SystemExit("[!] 列表里没有可用的 ip:port 条目")
    template = template_path.read_text(encoding="utf-8").strip()

    lines: list[str] = []
    for ip, port, country in entries:
        lines.append(render(template, ip, port, country))

    # 若在 grok-register 仓库内运行，则用项目解析器预校验生成的每一行
    repo_root = Path(__file__).resolve().parents[1]
    if (repo_root / "proxy_protocols.py").is_file():
        sys.path.insert(0, str(repo_root))
        try:
            from proxy_protocols import parse_proxy_line

            for line in lines:
                parse_proxy_line(line)
            print(f"[*] 项目解析器校验通过: {len(lines)} 个节点")
        except Exception as exc:
            raise SystemExit(f"[!] 生成的节点未能通过解析校验: {exc}")

    data = "\n".join(lines) + "\n"
    print(f"[*] 生成节点: {len(lines)} 个（去重后）")
    if args.dry_run:
        print(data)
        return 0

    result = api_request(args.panel, "/api/proxies/pool", args.token,
                         {"data": data, "path": args.pool_path}, method="PUT")
    print(f"[*] 导入面板: HTTP {result['status']} {result['body'][:200]}")

    if args.set_pool_mode:
        result = api_request(args.panel, "/api/config", args.token,
                             {"proxy_mode": "pool", "proxy_pool_file": args.pool_path or "./proxy-pool.txt"},
                             method="PUT")
        print(f"[*] 代理模式切换: HTTP {result['status']}")

    result = api_request(args.panel, "/api/proxy-pool/reload", args.token, None, method="POST")
    print(f"[*] 节点池重载: HTTP {result['status']}")
    print("[+] 完成；在面板代理池页执行「测试节点」确认各出口可用性")
    return 0


if __name__ == "__main__":
    sys.exit(main())
