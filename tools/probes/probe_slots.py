"""量出每个槽位的**真实出口 IP**，并写出 `FO_SLOT_EGRESS_IPS` 那一行。

🔴 为什么必须跑这个
------------------
配额按**出口 IP** 记账，而"槽位数 ≠ 出口 IP 数"。实测（2026-09-26）：

    7901 → 203.0.113.11      7904 → 203.0.113.14
    7902 → 203.0.113.12       7905 → 203.0.113.13  ← 与 7903 同出口
    7903 → 203.0.113.13       7906 → 203.0.113.11 ← 与 7901 同出口

**6 个槽位只有 4 个不同出口 IP** —— 同机房的多台机器常常共用同一个 NAT 出口。
⇒ **真正的并发上限是 4，不是 6**。配 6 个槽位就往 workers=6 冲，只会让多出来
  的槽位在同一个出口上排队，反而更快撞穿那个 IP 的配额。

🔴 三条判据（都是踩出来的）
--------------------------
  1. **不能用 TCP 连通性判断代理是否可用**。本机跑着 Clash TUN 时，连一个
     美国 IP 也会显示 0.02s "连通"（其实连到了本地虚拟网卡）。
     必须真的发一个请求拿到出口 IP 才算数。
  2. **必须用真实目标域名试**。有代理商能通 google/baidu，却精确屏蔽了目标站。
     所以默认同时测 `api.ipify.org`（量 IP）与站点自身（验可达）。
  3. **清掉环境代理**。本机有 `HTTP_PROXY`（WorkBuddy 沙箱代理 / Clash），
     `requests` 默认会用它 —— 那样量出来的是**环境代理的出口**，不是槽位的。
     本脚本对每个请求显式 `proxies=` 且 `trust_env=False`。

用法：
    python tools/probes/probe_slots.py
    python tools/probes/probe_slots.py --slots http://127.0.0.1:7901,http://127.0.0.1:7902
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import requests  # noqa: E402

from src import cli, config, quota  # noqa: E402
from src.redact import redact_url  # noqa: E402

# 🔴 Windows GBK 控制台**重定向输出**时，下面这些 `✓`/`⚠` 会抛
#    UnicodeEncodeError。统一切到 UTF-8 —— 实现见 src/cli.py，与其它入口共用一份。
cli.force_utf8_stdio()

IP_ECHO = "https://api.ipify.org"
TIMEOUT = 25


def egress_of(url: str) -> str:
    """经 `url` 请求一次 IP 回显，返回出口 IP（失败返回 `""`）。"""
    px = {"http": url, "https": url}
    try:
        r = requests.get(IP_ECHO, proxies=px, timeout=TIMEOUT)
        if r.ok:
            return r.text.strip()
    except requests.RequestException:
        pass
    return ""


def site_reachable(url: str) -> str:
    """经 `url` 请求站点自身，返回简况。

    🔴 单独一步，因为"能出网"≠"能到目标站" —— 有代理商精确屏蔽了目标域名。
    """
    px = {"http": url, "https": url}
    try:
        r = requests.get(
            f"{config.SITE_BASE}/api/settings/public",
            proxies=px,
            timeout=TIMEOUT,
            headers={"Accept": "application/json"},
        )
        return f"HTTP {r.status_code}"
    except requests.RequestException as ex:
        return f"{type(ex).__name__}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--slots",
        default=None,
        help="逗号分隔的槽位 URL（默认取 FO_PROXY_SLOTS_FILE / FO_PROXY_SLOTS）",
    )
    args = ap.parse_args()

    if args.slots:
        slots = [config.normalize_proxy(s) for s in args.slots.split(",") if s.strip()]
    else:
        try:
            slots = config.proxy_slots()
        except (ValueError, OSError) as ex:
            print(f"✗ 槽位清单读不到：{ex}", file=sys.stderr)
            return 1
    if not slots:
        print("✗ 没有槽位。先配 FO_PROXY_SLOTS_FILE 或传 --slots。", file=sys.stderr)
        return 1

    print(f"探测 {len(slots)} 个槽位（每个都真发请求拿出口 IP）\n")
    rows = []
    for i, url in enumerate(slots, 1):
        port = url.rsplit(":", 1)[-1]
        ip = egress_of(url)
        site = site_reachable(url) if ip else "-"
        rows.append((port, url, ip, site))
        mark = "✓" if ip else "✗"
        print(
            f"  {mark} slot{i} {redact_url(url):<26} 出口 {ip or '(取不到)':<16} 站点 {site}",
            flush=True,
        )

    good = [(p, u, ip, s) for p, u, ip, s in rows if ip]
    if not good:
        print("\n✗ 一个槽位都取不到出口 IP —— 池子没起来，或节点全挂了。")
        return 1

    distinct = sorted({ip for _, _, ip, _ in good})
    print("\n── 汇总 ──")
    print(f"  槽位 {len(good)} 个，**不同出口 IP {len(distinct)} 个**")
    if len(distinct) < len(good):
        print(f"  ⚠ 有槽位共用出口 —— **并发上限是 {len(distinct)}，不是 {len(good)}**")
        print("    多出来的槽位不提供额外配额，只会让同一个 IP 更快撞穿。")
    print(f"\n  每个出口每日上限 {quota.limit()} 个（本地保护值，见 src/quota.py）")
    print(f"  ⇒ 本池理论日产能 ≈ {len(distinct) * quota.limit()} 个")

    unreachable = [(p, s) for p, _, ip, s in good if not s.startswith("HTTP 200")]
    if unreachable:
        print(f"\n  ⚠ 有 {len(unreachable)} 个出口**取到了 IP 但站点不可达**：")
        for p, s in unreachable:
            print(f"      {p}: {s}")
        print("    （代理商精确屏蔽目标域名时会这样 —— 换节点，别调代码）")

    line = ",".join(f"{p}={ip}" for p, _, ip, _ in good)
    print("\n── 写进 .env（**不要写进源码**，出口 IP 是基础设施标识）──\n")
    print(f"FO_SLOT_EGRESS_IPS={line}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
