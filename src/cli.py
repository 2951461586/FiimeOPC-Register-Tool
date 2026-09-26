"""CLI 入口共用的骨架。

为什么单独一个模块
==================
本项目有**五个** CLI 入口（`run.py` / `tools/probes/probe_slots.py` /
`tools/probes/probe_ip_daily_limit.py` / `tools/ops/check_keys_alive.py` /
`tools/ops/recover_keys.py`），它们对"槽位池""台账落盘"的接线是同一套。
接线重复的代价不是"多打几行字"，而是**漂移**：改一边忘一边，两个入口对同一个
开关给出不同行为 —— 而这类漂移**不会报错**。

⚠ 本模块**只放真正等价的部分**。看似重复但语义不同的（如 `--out` 的落盘分支
  判据），刻意**不**抽进来 —— 参考项目审计里的原话：
  **"重复"不等于"等价"，先证明再合并**。
"""

import argparse

# ── 退出码总表（全项目只有这一处定义）────────────────────────────
# 🔴 集中定义不是为了好看：本项目**五个**入口/分支会返回非零码，散着写迟早
#    撞车（两个完全不同的故障给出同一个数字，脚本化调用就分不清了）。
#
#   0 = 成功
#   2 = 本地配额触顶（`quota.QuotaExceeded`）
#   3 = 防静默缩水护栏触发（`ledger.EXIT_COVERAGE_GAP` 同码，见那边的注释）
#   4 = 槽位池全死（`proxypool.AllSlotsDead`）
#   5 = key 建出来了但**校验不过** ⇒ 交付不完整
EXIT_OK = 0
EXIT_QUOTA_EXCEEDED = 2
EXIT_NO_SHRINK = 3
EXIT_ALL_SLOTS_DEAD = 4
EXIT_KEY_UNVERIFIED = 5

# 🔴 help 文本提成常量，**不是**为了少打几个字 —— 是为了能被断言。
#    测试会跑各入口的 `--help`，断言这两句逐字出现，这样"某个入口又把 help
#    抄成自己的一份"会当场变红。
SLOT_HELP = ("槽位端口，如 7901（默认取 FO_PROXY_SLOTS_FILE 的第一个）。"
             "不传且没配池子 = 直连")
IGNORE_QUOTA_HELP = ("跳过本地配额保护（仅当**确信**服务端配额已恢复时用；"
                     "本地计数是保守估计，不是权威值）")


def add_ignore_quota_arg(ap: argparse.ArgumentParser) -> None:
    """加 `--ignore-quota`。

    🔴 提成函数是为了让**三个入口**的开关名与 help 文本一致 —— 参考项目
      实测过：某个入口的 `--ignore-quota` 只覆盖了两个检查点，第三个漏了，
      表现是"整批 0 个成功，报错却说池子满了"。
    """
    ap.add_argument("--ignore-quota", action="store_true", help=IGNORE_QUOTA_HELP)


def add_key_name_arg(ap: argparse.ArgumentParser, default: str = "default") -> None:
    """加 `--key-name`。"""
    ap.add_argument("--key-name", default=default,
                    help=f"API Key 名称（默认 {default}；每账号最多 5 把）")


def merge_summary_line(kept: int, added: int, upgraded: int, total: int) -> str:
    """「结果合并：原有 N 条 + 本次新增 M 条（K 条已更新）= T 条」。

    🔴 返回的字符串**带前导 `\\n`** —— 它就是 `print()` 出去的那一行，
      保持逐字节相同是抽取的前提，所以换行跟着文本一起走。

    ⚠ 只返回**文本**，不打印、不带 `if kept:` 判断 —— 调用方在那一点上不同，
      把差异留在函数之外，抽取才是零行为变化。
    """
    extra = f"（{upgraded} 条已更新：升级或补全字段）" if upgraded else ""
    return f"\n结果合并：原有 {kept} 条 + 本次新增 {added} 条{extra} = {total} 条"
