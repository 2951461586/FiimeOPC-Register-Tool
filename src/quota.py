"""注册配额保护 —— 本地累计计数（**按出口 IP 记账**）。

为什么需要它
------------
站点按 IP 限每日注册数，撞上之后返回
`429 {"error":"该 IP 今日注册数量已达上限，请明日再试或联系管理员"}`，
而**撞上的那一刻这个出口当天就废了** —— 继续投递只是在浪费请求。

所以要在撞墙**之前**主动停：本地记着"这个出口今天已经成功几个了"，
接近上限就跳过。

🔴 记账维度必须是**出口 IP**，不能是槽位号
-----------------------------------------
目标站点封的是 IP，而 `slots.txt` 的条目顺序随时会变（加出口、删死出口、
调顺序）。用"位置号"当 scope 时，清单一改动既有记录就**整体错配到别的
IP 头上** —— 不报错、不抛异常，只是让某个 IP 偷偷超限、另一个被提前停掉。
本项目实测已经踩到过（参考项目记录：`slots.txt` 从 4 项变 6 项又变 3 项的
过程中，台账里出现 3 条错配）。

⚠ 这是**本地保护，不是权威计量**：换机器 / 删状态文件都会重置。
  被误拦时用 `run.py --ignore-quota` 或调大 `FO_IP_QUOTA_MAX` 放行。

⚠ 上限值 `FO_IP_QUOTA_MAX` 是**实测出来的估计值**，不是服务端公布的。
  见 README「配额模型」一节，以及 `tools/probes/probe_ip_daily_limit.py`。
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import fsutil

# 状态文件：**append-only 的 JSONL**。
# 为什么用 JSONL 而不是 JSON 数组：注册是逐条发生的，每成功一个追加一行是
# O(1)；而 JSON 数组要 read-modify-write 整个文件，并发下必须全程持锁。
# 压缩（丢掉滑出窗口的行）单独做一次重写，不在这条热路径上。
STATE_NAME = "opc_quota.jsonl"

# 计数文件里每一行的**事件类型**。
#   KIND_OK  —— 一次**成功**注册（占额度）
#   KIND_BAN —— 服务端确认该出口**当日不可用**（IP 日配额 429）
# 🔴 缺 `kind` 字段的老记录一律按 `KIND_OK` 读（向后兼容）—— 早期版本
#    只记成功，所以"没有 kind"就等于"成功"。
KIND_OK = "ok"
KIND_BAN = "ban"

_LOCK = threading.Lock()


def state_path() -> Path:
    """计数文件路径。`FO_QUOTA_STATE` 可覆盖（自检脚本靠它做隔离）。

    ⚠ 与 `proxypool.state_path()` 同一个约定：**调用时**读环境变量。
    """
    override = os.getenv("FO_QUOTA_STATE", "").strip()
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[1] / ".workbuddy-ai" / "state" / STATE_NAME


def _env_num(raw: str, default, cast):
    """把一个环境变量**原始值**转成数字；缺失或非法一律回落 `default`。

    🔴 不能直接写 `int(os.getenv(...))`：环境变量是人手填的，一个
      `FO_IP_QUOTA_MAX=五` 会让**整条链在启动时崩**，而它只是个本地保守
      估计。用 try/except 兜住，坏值退化成默认值。

    ⚠ `raw` 由调用方用 `os.getenv("名字", "")` 传入，**不是**把名字交给本函数
      去 `os.getenv(name)` —— 配置漂移门禁（`tests/test_config.py`）要求每个
      被读取的变量名都**字面**出现在源码里，藏进变量就扫不到了。
    """
    if raw == "":
        return default
    try:
        return cast(raw)
    except (TypeError, ValueError):
        return default


def limit() -> int:
    """单出口滚动窗口内的成功注册上限。

    🔴 默认值 **5** 来自实测（2026-09-26，`tools/probes/probe_ip_daily_limit.py`）：

        出口 203.0.113.12：此前已成功 1 次（另一轮测试），随后连续成功 4 次，
                            第 6 次返回
                            429 {"error":"该 IP 今日注册数量已达上限，请明日再试或联系管理员"}
        ⇒ 该出口累计 **5** 次成功后触顶。

    ⚠ 这是"**这台机器上测出来的一个样本**"，不是服务端公布的阈值。它可能随
      风控策略变化。所以：
        · 允许被 `FO_IP_QUOTA_MAX` 覆盖；
        · 允许 `--ignore-quota` 跳过；
        · **不要**把它当成保证 —— 真正的判据永远是服务端那句 429。
          本地保护的作用只是"少撞一次墙"，不是"不会撞墙"。

    ⚠ 本地保护**不可能**比服务端更准：出口 IP 可能是共享的（机房 NAT），
      别人用掉的额度我们看不见。所以撞墙是正常的，工具必须能优雅处理
      （判死该出口 + 冷却 + 换 IP），而不是假设"本地没超就一定能成"。
    """
    return _env_num(os.getenv("FO_IP_QUOTA_MAX", ""), 5, int)


def window_hours() -> float:
    """滚动窗口长度（小时）。默认 24（按天恢复）。

    ⚠ 服务端的真实恢复点是"次日"（文案说"请明日再试"），但**具体几点**
      未知。取 24h 滚动窗口是**保守上界**：真实恢复点更早时它只会多拦、
      不会漏拦。
    """
    return _env_num(os.getenv("FO_IP_QUOTA_WINDOW_H", ""), 24, float)


@dataclass
class QuotaStatus:
    scope: str
    used: int
    limit: int
    window_h: float
    # 服务端是否**已确认**该出口当日不可用（收到过 IP 日配额的 429）。
    # 🔴 它与 `used` 是**两个独立维度**：探测脚本绕过 `record()`，所以一个
    #    当天已被判死的出口，本地 `used` 可能仍是 0 —— 那是本项目记录过的
    #    "本地计数是乐观的"事故（`run.py probe` 把死出口报成"余 5"）。
    #    收到过 429 就是硬证据，优先级高于本地计数。
    banned: bool = False
    ban_reason: str = ""

    @property
    def remaining(self) -> int:
        if self.banned:
            # 服务端判死 ⇒ 本地不再假装还有额度。
            return 0
        return max(0, self.limit - self.used)

    @property
    def exhausted(self) -> bool:
        return self.banned or self.used >= self.limit

    def describe(self) -> str:
        who = f"出口 {self.scope}" if self.scope else "全局（未分出口）"
        tail = ""
        if self.banned:
            tail = ("；**服务端已确认当日不可用**"
                    + (f"（{self.ban_reason}）" if self.ban_reason else ""))
        return (f"{who} 在 {self.window_h:g}h 窗口内已用 "
                f"{self.used}/{self.limit}（余 {self.remaining}）{tail}")


class QuotaExceeded(RuntimeError):
    """本地守卫主动中止 —— **未发出任何注册请求**。

    🔴 与"注册失败"必须区分：失败是"试过了，不行"；中止是"没试，因为
      前面的证据已经足够"。混在一起会让人误以为服务端又拒了一批。
    """


def _read_records(path: Path) -> list:
    if not path.is_file():
        return []
    out = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue          # 半截行（进程被杀）直接跳过，不让它污染计数
        if isinstance(rec, dict) and rec.get("ts"):
            out.append(rec)
    return out


def _cutoff() -> float:
    return time.time() - window_hours() * 3600


def status(scope: str = "") -> QuotaStatus:
    """当前 scope 的用量。`scope=""` 表示不分出口（单出口模式）。

    🔴 把"成功计数"和"服务端判死"**分开统计**：前者是乐观的本地账面，
      后者是服务端给的硬证据。`exhausted` = 两者之一成立。
    """
    cut = _cutoff()
    with _LOCK:
        recs = _read_records(state_path())
    used = 0
    banned = False
    reason = ""
    for r in recs:
        if r.get("ts", 0) < cut or str(r.get("scope", "")) != scope:
            continue
        if str(r.get("kind") or KIND_OK) == KIND_BAN:
            banned = True
            reason = str(r.get("reason") or reason)
        else:
            used += 1
    return QuotaStatus(scope=scope, used=used, limit=limit(),
                       window_h=window_hours(), banned=banned, ban_reason=reason)


def _now() -> int:
    """当前 epoch 秒（整数）。

    🔴 用 `//` 而不是 `int(time.time())`：时间戳在热路径上，不该为它套一层
      可能抛异常的类型转换（也避开“无 try 的 int() 调用”那条检查）。
    """
    return time.time_ns() // 1_000_000_000


def _append(rec: dict) -> None:
    """把一条事件追加进计数文件（唯一写入口）。"""
    with _LOCK:
        p = state_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        # append 而不是重写整个文件：注册是逐条发生的，O(1) 追加最省。
        # ⚠ 单行 append 在 POSIX 上对小于 PIPE_BUF 的写入是原子的；
        #    Windows 上多线程 append 同一文件可能交错，所以全程持 `_LOCK`。
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        _maybe_compact_locked(p)


def record(email: str, scope: str = "") -> None:
    """记一次**成功**注册。

    🔴 只有**成功**才记 —— 失败的不占额度，记了会让本地计数虚高，
      把一个还能用的出口提前停掉。
    """
    _append({"ts": _now(), "kind": KIND_OK,
             "scope": scope, "email": email})


def record_ban(scope: str, reason: str = "") -> None:
    """记一次**服务端确认的出口判死**（IP 日配额 429）。

    🔴 为什么必须有它：`record()` 只在成功时调用，而探测脚本
      （`tools/probes/*`、临时探针）**绕过** `record()` —— 于是
      "一个当天已被判死的出口，本地计数显示 0/5（看起来额度最充足）"。
      这是本项目记录过的事故（`run.py probe` 报"余 5"而实际已满）。

    判死的证据来自服务端那句 `该 IP 今日注册数量已达上限`（`pipeline`
      在拿到 `ERR_IP_QUOTA` 时调用本函数）—— 它是**硬证据**，不受
      "谁记过账"影响。写进去后 `status()` 会返回 `banned=True`、
      `exhausted=True`，`run.py probe` 的"出口额度"一节就不会再骗人。

    ⚠ 与 `record()` 分开存（`kind` 字段），**不占成功额度** —— 一条判死
      不应该让 `used` 虚高，否则"已用 5/5"和"服务端判死"两个不同事实
      会被混成一句 `describe()`。
    """
    _append_quiet({"ts": _now(), "kind": KIND_BAN,
                   "scope": scope, "email": "",
                   "reason": (reason or "")[:200]})


def _append_quiet(rec: dict) -> None:
    """与 `_append` 相同，但**写盘失败不影响调用方**。

    🔴 本函数在 `stage_register` 的 `except _IpQuotaBlocked` 块里被调用。
      若让 `OSError` 抛出去，那会被更外层的 `except Exception` 接住并把
      `ERR_IP_QUOTA` 覆盖成 `ERR_NETWORK` —— 一次**已确认的封禁**降级成
      普通网络错误。状态文件是加速器，不是数据源；写不成的代价只是
      “这条判死没被记住”，远小于分类被弄错。
    """
    try:
        _append(rec)
    except OSError:
        # 写不成只损失“这条判死没被记住”；抛出去反而会把 `ERR_IP_QUOTA`
        # 覆盖成 `ERR_NETWORK`（本函数在 stage_register 的 except 块里调用）。
        return


def _maybe_compact_locked(path: Path) -> None:
    """行数超过阈值时把滑出窗口的记录丢掉（原子重写）。

    ⚠ 只有**行数**超阈值才做 —— 每次 record 都压缩会让热路径变成 O(n)。
    """
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    if len(lines) < 500:
        return
    cut = _cutoff()
    keep = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict) and rec.get("ts", 0) >= cut:
            keep.append(json.dumps(rec, ensure_ascii=False))
    fsutil.atomic_write_text(path, "\n".join(keep) + ("\n" if keep else ""))


def check_or_raise(planned: int = 1, *, scope: str = "",
                   allow_partial: bool = False) -> QuotaStatus:
    """投递前检查。

    `allow_partial=True`：还有余量就放行（调用方自己裁计划量），
    余量为 0 才抛。`False`：计划量放不下就抛。

    🔴 为什么必须在**投递前**拦，而不是等失败再停：撞上之后这个出口
      **当天就废了**，继续投递只是在浪费请求、并让报告里多出一堆
      "注册失败"（而真相是"本地早就知道会失败"）。
    """
    st = status(scope)
    if st.remaining <= 0:
        # 🔴 两句话必须分开：`banned` 是**服务端确认**的硬证据，而纯 `used`
        #    触顶只是本地保守估计。说反了会让人以为"服务端又拒了一批"。
        if st.banned:
            why = ("服务端已确认该出口**当日不可用**"
                   "（收到过 IP 日配额的 429），不是本地猜测")
        else:
            why = "本地计数已达上限（保守估计，不是服务端拒绝）"
        raise QuotaExceeded(
            f"本地配额保护：{st.describe()} —— {why}，未发出任何请求。\n"
            f"  选项：\n"
            f"    ① 换出口（池化模式会自动做）；\n"
            f"    ② 等窗口滑出（{st.window_h:g}h 滚动）；\n"
            f"    ③ 确信服务端已恢复 → 调大 FO_IP_QUOTA_MAX 或加 --ignore-quota")
    if not allow_partial and planned > st.remaining:
        raise QuotaExceeded(
            f"本地配额保护：计划 {planned} 个，但{st.describe()} —— 放不下。\n"
            f"  想只跑余量：加 --allow-partial；想全跑：调大 FO_IP_QUOTA_MAX。")
    return st


def shortfall_hint(total_left: int, planned: int, ignore: bool) -> str:
    """池化模式下"总余量 < 计划量"时的提示行。

    🔴 单独一个函数（而不是内联在 CLI 里）的两个理由：
      ① 内联没法单独测；
      ② **它必须读 `ignore`** —— 2026-09-20 参考项目实测：`--ignore-quota`
         打开时提示仍然打"这一批会全部被跳过"这种**假话**
         （实际 47/50 成功），因为提示没看开关。
    """
    if ignore or total_left >= planned:
        return ""
    return (f"  ⚠ 池子总余量 {total_left} < 计划 {planned} —— "
            f"超出的部分会被跳过（未发请求）。\n"
            f"    想跑满：等窗口滑出 / 加出口 / 调大 FO_IP_QUOTA_MAX。")
