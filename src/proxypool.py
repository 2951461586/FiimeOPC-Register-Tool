"""槽位代理池 —— 给每个注册 worker 一个**独立的出口 IP**。

为什么需要它（本项目**必需**，不是可选优化）
--------------------------------------------
站点按 **IP** 限每日注册数。2026-09-26 实测：

    本机出口 128.241.231.18  → 429 {"error":"该 IP 今日注册数量已达上限，请明日再试或联系管理员"}
    槽位出口 203.0.113.11  → 200 ✓
    槽位出口 203.0.113.12   → 200 ✓
    槽位出口 203.0.113.13   → 200 ✓
    槽位出口 203.0.113.14    → 200 ✓

**同一时刻**、同一台机器，只换了出口 IP —— 4/4 全部成功。
⇒ 每日配额确实是 IP 维度的，换 IP 即解开；单出口方案一天只能注册几个。

设计参考
--------
`asz798838958/aBaiFreeGPT` 的 mihomo 分配器（经参考项目 Intern-Register-Tool
验证过）。核心是**租约（lease）**：

    worker ──acquire()──> 槽位 N（= 本地端口 790N = 某个固定出口 IP）
           ──用完/失败──> release() 或 report_banned()

四条规则（都照搬参考实现，理由见各自注释）：
  1. **租约粘性**：一个 worker 从注册到激活走同一个出口 IP。
     中途换 IP 会让服务端看到"半个会话换了来源"，比慢更糟。
  2. **被封的槽位进冷却，不是永久拉黑**。同一 worker 不再回到它失败过的槽位。
  3. **均衡分配**：优先挑"用得最少"的槽位，而不是轮询 ——
     否则少数出口被反复使用，把 IP 配额更快撞穿。
  4. **同出口 IP 互斥**：同一时刻**一个出口 IP 只允许一个租约在外**。

🔴 规则 4 的理由（**分配单位与约束单位必须一致**）
------------------------------------------------
本项目的约束是 IP 维度，所以：

    约束的单位 = 出口 IP
    分配的单位 = 槽位

两者**不相等**。本项目实测（2026-09-26，本机 mihomo 6 槽）：

    7901 → 203.0.113.11      7904 → 203.0.113.14
    7902 → 203.0.113.12       7905 → 203.0.113.13  ← 与 7903 同出口
    7903 → 203.0.113.13       7906 → 203.0.113.11 ← 与 7901 同出口

**6 个槽位只有 4 个不同出口 IP。** 按槽位分配会让两个同 IP 的槽位被同时租
出去，服务端看到同一 IP 上 2 路并发，撞穿速度翻倍。

⚠ 代价要说清楚：**有效并发度从"槽位数"降到"不同出口 IP 数"**（6 → 4）。
  这是对的 —— 多出来的 2 个槽位本来就不提供额外配额。
  `describe()` 会把这个数字打印出来，避免有人以为"配了 6 个就能跑 6 路"。

⚠ 拿不到出口 IP 映射时不启用规则 4（退回按槽位分配），并在 `describe()`
  里显式告警 —— "不知道出口 IP"和"出口 IP 互不相同"是两件完全不同的事。

状态持久化
----------
**冷却到期时刻 + 封禁次数**落到 `.workbuddy-ai/state/proxypool.json`
（`FO_PROXY_STATE` 可覆盖），建池时读回。

🔴 为什么必须持久化：冷却按 2 的幂退避（120s → 6h），而 IP 每日配额是
  **按天**恢复的。进程一退、退避就重置回 120s —— 每重跑一次批量，就在
  同一个已满的出口上重新撞一遍。持久化把"一天 N 次"压到"一天几次"。
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from . import config, fsutil
from .redact import redact_url

__all__ = ["SlotLease", "NoEligibleSlot", "AllSlotsDead", "ProxySlotPool",
           "build_pool", "check_slots_alive", "state_path"]


def state_path() -> Path:
    """池子状态文件。`FO_PROXY_STATE` 可覆盖（自检脚本靠它做隔离）。

    ⚠ 与 `quota.state_path()` 同一个约定：**调用时**读环境变量，
      不受导入顺序影响。
    """
    override = os.getenv("FO_PROXY_STATE", "").strip()
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[1] / ".workbuddy-ai" / "state" / "proxypool.json"


def _state_key(url: str) -> str:
    """状态文件里的键 = `host:port`（**不含 userinfo**）。

    🔴 不能拿整个 URL 当键：代理串可能是 `http://user:pass@host:port`，
      写进状态文件等于把账密落盘。而状态文件经常被贴出来排查。
    """
    try:
        p = urlparse(url if "://" in url else f"http://{url}")
        return f"{p.hostname or '?'}:{p.port or 0}"
    except ValueError:
        return url


@dataclass
class SlotLease:
    """一个槽位的租约。`url` 是给 `requests` 用的代理串。"""
    slot: int
    url: str
    egress: str = ""       # 出口 IP（拿不到时为空串）

    def __str__(self) -> str:
        # 🔴 走 `redact_url` —— `url` 可能带账密，原样打印会写进日志。
        ip = f"@{self.egress}" if self.egress else ""
        return f"slot{self.slot + 1}({redact_url(self.url)}){ip}"


class NoEligibleSlot(RuntimeError):
    """有空闲槽位，但它们的出口配额全满了（或全在配额冷却中）。

    🔴 与 `TimeoutError` **必须分开**：配额要几小时才滑出窗口，等下去毫无
      意义 → 直接记"跳过"；而"等不到空闲槽位"是本地资源竞争 → 记"失败"。
      两者混成一种，报告里就分不清"没额度了"和"池子太小"。
    """


class AllSlotsDead(RuntimeError):
    """起飞前的端口预检：**一个槽位都连不上**。

    🔴 这时**直接拒绝开跑**，不静默退回单代理 —— 槽位进程没起来时，
      每条记录都会以"代理连接错误"收场，而"注册全失败"在本项目里最容易被
      误读成"IP 又被封了"，结论完全错。
    """


def _host_port(url: str) -> tuple:
    p = urlparse(url if "://" in url else f"http://{url}")
    return p.hostname or "", int(p.port or 0)


def check_slots_alive(slots: list, *, timeout: float = 0.5) -> dict:
    """TCP 连通预检。返回 `{slot_index: bool}`。

    ⚠ 只做 **TCP connect**，不发请求 —— 毫秒级。它的用途是"进程还在不在"，
      不是"代理通不通"（后者要真发请求，见 `tools/probes/probe_slots.py`）。
      🔴 拿 TCP 连通性判断"代理能不能用"是错的：本机有 Clash TUN 时，
        连一个美国 IP 也会显示 0.02s "连通"（其实连到了本地虚拟网卡）。
    """
    out = {}
    for i, url in enumerate(slots):
        host, port = _host_port(url)
        ok = False
        if host and port:
            try:
                with socket.create_connection((host, port), timeout=timeout):
                    ok = True
            except OSError:
                ok = False
        out[i] = ok
    return out


class ProxySlotPool:
    """槽位池。线程安全。"""

    def __init__(self, slots: list, *, cooldown: float = None,
                 cooldown_max: float = None, egress_ips: dict = None,
                 log=print, state_file=None):
        if not slots:
            raise ValueError("槽位清单为空")
        self.slots = list(slots)
        self.cooldown = cooldown if cooldown is not None else config.PROXY_COOLDOWN
        self.cooldown_max = (cooldown_max if cooldown_max is not None
                             else config.PROXY_COOLDOWN_MAX)
        self.log = log
        self.state_file = Path(state_file) if state_file else state_path()

        # 端口 → 出口 IP。拿不到就留空（退回按槽位分配，见模块 docstring）。
        mapping = egress_ips if egress_ips is not None else config.SLOT_EGRESS_IPS
        self.egress = {}
        for i, url in enumerate(self.slots):
            port = str(_host_port(url)[1])
            self.egress[i] = mapping.get(port, "")
        self._egress_known = any(self.egress.values())

        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._busy: dict = {}            # slot -> True（在外租约）
        self._use: dict = {}             # slot -> 累计租用次数（均衡用，规则 3）
        self._cooldown_until: dict = {}  # slot -> epoch
        self._bans: dict = {}            # slot -> 累计封禁次数（退避用）
        self._load_state()

    # ── 只读视图 ──────────────────────────────────────────────
    @property
    def size(self) -> int:
        return len(self.slots)

    def url_of(self, slot: int) -> str:
        return self.slots[slot]

    def egress_of(self, slot: int) -> str:
        return self.egress.get(slot, "")

    def distinct_egress(self):
        """不同出口 IP 的数量（拿不到映射时返回 None）。"""
        if not self._egress_known:
            return None
        return len({v for v in self.egress.values() if v})

    def _warn(self, msg: str) -> None:
        try:
            self.log(msg)
        except Exception:
            pass

    # ── 状态持久化 ────────────────────────────────────────────
    def _load_state(self) -> None:
        if not self.state_file.is_file():
            return
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return
        if not isinstance(data, dict):
            return
        now = time.time()
        by_key = data.get("slots") or {}
        for i, url in enumerate(self.slots):
            rec = by_key.get(_state_key(url))
            if not isinstance(rec, dict):
                continue
            until = float(rec.get("cooldown_until") or 0)
            if until > now:
                self._cooldown_until[i] = until
            n = int(rec.get("bans") or 0)
            if n:
                self._bans[i] = n
        if self._cooldown_until:
            self._warn(f"[pool] 读回 {len(self._cooldown_until)} 个仍在冷却的槽位"
                       f"（状态文件 {self.state_file}）")

    def _save_state_locked(self) -> None:
        """写状态文件。**失败只告警**，不让整批任务挂掉。

        理由：状态文件是**加速器**（记住退避进度），不是数据源。
        它写不成的代价只是"下次重跑退避从 120s 重来"，远小于让整批失败。
        """
        payload = {"saved_at": int(time.time()), "slots": {}}
        for i, url in enumerate(self.slots):
            payload["slots"][_state_key(url)] = {
                "cooldown_until": int(self._cooldown_until.get(i, 0)),
                "bans": int(self._bans.get(i, 0)),
            }
        try:
            fsutil.atomic_write_text(
                self.state_file, json.dumps(payload, ensure_ascii=False, indent=2))
        except OSError as ex:
            self._warn(f"[pool] ⚠ 状态文件写失败（不影响本批）：{ex}")

    # ── 选择（必须在锁内调用）──────────────────────────────────
    def _pick_locked(self, exclude: set, accept=None):
        """挑一个可用槽位。返回 slot 下标，或 `None`。

        过滤顺序（规则 2 + 4 + 3）：
          1. 排除 `exclude`（本 worker 失败过的槽位）
          2. 排除冷却中的、正在被占用的
          3. 排除**出口 IP 已被占用**的（规则 4）
          4. 过 `accept`（pipeline 用它筛"该出口配额已满"）
          5. 在剩下的里挑**累计租用次数最少**的（规则 3）
        """
        now = time.time()
        busy_ips = set()
        if self._egress_known:
            for s in self._busy:
                ip = self.egress.get(s, "")
                if ip:
                    busy_ips.add(ip)

        best, best_use = None, None
        for i in range(self.size):
            if i in exclude or self._busy.get(i):
                continue
            if self._cooldown_until.get(i, 0) > now:
                continue
            ip = self.egress.get(i, "")
            if ip and ip in busy_ips:
                continue                      # 规则 4
            if accept is not None and not accept(i):
                continue
            use = self._use.get(i, 0)
            if best is None or use < best_use:
                best, best_use = i, use
        return best

    def acquire(self, *, timeout: float = None, accept=None,
                exclude: set = None) -> SlotLease:
        """拿一个槽位租约。

        `accept(slot) -> bool`：额外过滤（pipeline 用它筛掉"该出口配额已满"）。
        `exclude`：本 worker 已经失败过的槽位（规则 2）。

        超时抛 `NoEligibleSlot`（有空位但配额全满）或 `TimeoutError`
        （等不到空闲槽位）—— 两者的处置完全不同，见各自 docstring。
        """
        timeout = timeout if timeout is not None else config.PROXY_SLOT_TIMEOUT
        exclude = set(exclude or ())
        deadline = time.time() + timeout

        with self._cond:
            while True:
                slot = self._pick_locked(exclude, accept)
                if slot is not None:
                    self._busy[slot] = True
                    self._use[slot] = self._use.get(slot, 0) + 1
                    return SlotLease(slot=slot, url=self.slots[slot],
                                     egress=self.egress.get(slot, ""))
                now = time.time()
                if now >= deadline:
                    raise self._no_slot_reason_locked(exclude, accept, now)
                # 等到"最早一个冷却到期"或 0.5s，谁先到就谁 —— 避免空转。
                self._cond.wait(timeout=max(0.05, min(0.5, deadline - now)))

    def _no_slot_reason_locked(self, exclude: set, accept, now: float) -> Exception:
        """超时后判断"到底为什么没槽位"，返回该抛的异常。

        🔴 这个区分是**语义**的，不是话术：
          · 有空位但被 `accept` 否掉（出口配额满）→ `NoEligibleSlot` → 记"跳过"
          · 所有冷却中的槽位都是**配额封禁**导致的 → `NoEligibleSlot` → 记"跳过"
          · 其余（真在忙 / 偶发失败冷却）→ `TimeoutError` → 记"失败"
        """
        free_any = self._pick_locked(exclude, None)
        if accept is not None and free_any is not None:
            return NoEligibleSlot(
                f"{self.size} 个槽位里没有一个合格（有空闲但出口配额均已满）")
        cooling = [i for i in range(self.size)
                   if i not in exclude and self._cooldown_until.get(i, 0) > now]
        if cooling and all(self._bans.get(i, 0) > 0 for i in cooling):
            return NoEligibleSlot(
                f"{len(cooling)} 个槽位全在**配额冷却**中"
                f"（最近一次到期还需 {int(min(self._cooldown_until[i] for i in cooling) - now)}s）")
        return TimeoutError(f"等待空闲槽位超时（{self.size} 个槽位，"
                            f"在外 {len(self._busy)}，冷却 {len(cooling)}）")

    def _release_locked(self, lease: SlotLease) -> None:
        self._busy.pop(lease.slot, None)
        self._cond.notify_all()

    def release(self, lease: SlotLease) -> None:
        """正常归还（立刻可复用）。"""
        if lease is None:
            return
        with self._cond:
            self._release_locked(lease)

    def report_banned(self, lease: SlotLease, reason: str = "") -> None:
        """这个出口被目标站点判死（本项目 = IP 每日配额已满）。

        冷却按 **2 的幂退避**，封顶 `cooldown_max`：
            120 → 240 → 480 → 960 → 1920 → 3840 → 7680 → 15360 → 21600（封顶）

        🔴 为什么必须退避：IP 配额是**按天**恢复的，而基础冷却只有 120s ——
          差 720 倍。不递增的话，一个已满的出口每 2 分钟被重新租出去打一枪，
          一天约 720 次，每次都在加深风控。
        """
        if lease is None:
            return
        with self._cond:
            n = self._bans.get(lease.slot, 0) + 1
            self._bans[lease.slot] = n
            cool = min(self.cooldown * (2 ** (n - 1)), self.cooldown_max)
            self._cooldown_until[lease.slot] = time.time() + cool
            self._warn(f"[pool] {lease} 判死（{reason or 'quota'}）"
                       f" → 冷却 {int(cool)}s（第 {n} 次）")
            self._release_locked(lease)
            self._save_state_locked()

    def report_failed(self, lease: SlotLease, reason: str = "",
                      cooldown: float = 20.0) -> None:
        """网络类偶发失败 —— **短冷却**（默认 20s），不递增退避。

        🔴 与 `report_banned` 分开的理由：把"偶发超时"当"被封"处理，
          会把健康出口按 120s+ 晾着（吞吐塌）；反过来把"被封"当偶发，
          20s 后重投只会继续撞墙。
        """
        if lease is None:
            return
        with self._cond:
            self._cooldown_until[lease.slot] = time.time() + cooldown
            self._warn(f"[pool] {lease} 偶发失败（{reason[:60]}）"
                       f" → 冷却 {int(cooldown)}s")
            self._release_locked(lease)

    # ── 展示 ──────────────────────────────────────────────────
    def stats(self) -> dict:
        with self._lock:
            now = time.time()
            return {
                "size": self.size,
                "busy": len(self._busy),
                "cooling": sum(1 for t in self._cooldown_until.values() if t > now),
                "bans": dict(self._bans),
                "distinct_egress": self.distinct_egress(),
            }

    def describe(self) -> str:
        s = self.stats()
        n = s["distinct_egress"]
        if n is None:
            tail = ("⚠ 未登记出口 IP 映射 → **未启用同 IP 互斥**，"
                    "同出口可能被并发租用（跑 tools/probes/probe_slots.py 量出来）")
        elif n < self.size:
            tail = (f"⚠ {self.size} 个槽位只有 **{n}** 个不同出口 IP "
                    f"→ 并发上限是 {n}，不是 {self.size}"
                    f"（多出来的槽位不提供额外配额）")
        else:
            tail = f"{n} 个不同出口 IP"
        return (f"{self.size} 槽位 / 在外 {s['busy']} / 冷却 {s['cooling']}"
                f" / {tail}")


def build_pool(*, log=None, cooldown: float = None,
               preflight: bool = None) -> ProxySlotPool | None:
    """按配置建池。**没配槽位就返回 `None`**（调用方据此退回单代理/直连）。

    🔴 预检失败（一个槽位都连不上）时抛 `AllSlotsDead` —— 不静默退回，
      理由见该异常的 docstring。
    """
    log = log or print
    try:
        slots = config.proxy_slots()
    except (ValueError, OSError) as ex:
        log(f"[pool] ⚠ 槽位清单读不到，退回单代理模式：{ex}")
        return None
    if not slots:
        return None

    do_pre = config.PROXY_PREFLIGHT if preflight is None else preflight
    if do_pre:
        alive = check_slots_alive(slots)
        dead = [i for i, ok in alive.items() if not ok]
        if len(dead) == len(slots):
            raise AllSlotsDead(
                f"槽位端口全部连不上（{len(slots)}/{len(slots)}）。\n"
                f"  槽位实例是**独立前台进程**，很容易「配置还留着、进程已经没了」。\n"
                f"  起实例： \"$FO_MIHOMO_EXE\" -d .workbuddy-ai/proxypool "
                f"-f .workbuddy-ai/proxypool/config.yaml\n"
                f"  ⚠ 不静默退回单代理：那样每条记录都会以「代理连接错误」收场，"
                f"而「注册全失败」最容易被误读成「IP 又被封了」。")
        if dead:
            log(f"[pool] ⚠ {len(dead)}/{len(slots)} 个槽位端口连不上，"
                f"已剔除：{[f'slot{i + 1}' for i in dead]}")
            slots = [u for i, u in enumerate(slots) if alive.get(i)]

    return ProxySlotPool(slots, cooldown=cooldown, log=log)
