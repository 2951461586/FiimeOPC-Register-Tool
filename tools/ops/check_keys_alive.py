#!/usr/bin/env python
"""复验台账里所有 API Key 是否还活着。**不消耗注册配额。**

为什么必须有这个工具
====================
`run.py` 的"交付完整性"只回答**跑批那一刻** key 能不能用。而 key 会**事后**失效
（站点封号、重置、key 被停用），台账里的 `key_verified: true` 是历史快照，
不是当下的结论。

而"复验"这件事**不能**塞回 `run.py`：`run.py` 的任何一次调用都可能消耗
IP 维度的每日注册配额（实测 5 个/IP/天），而查 key 是**纯读**操作。
把两者分开，才能在额度耗尽时依然能回答"我手里这批 key 还能用吗"。

用法
====
  python tools/ops/check_keys_alive.py                 # 复验读源（最新全量快照）
  python tools/ops/check_keys_alive.py --all           # 复验**全部**历史快照，按 key 去重
  python tools/ops/check_keys_alive.py --ledger X.json # 指定台账文件
  python tools/ops/check_keys_alive.py --json out.json # 额外落一份机器可读结果
  python tools/ops/check_keys_alive.py --workers 8     # 并发（默认 8）
  python tools/ops/check_keys_alive.py --apply         # 复验并把结论**写回台账**

🔴 `--apply` 的作用（默认不写）：台账里会留下**旧版本代码**写的
   `stages.verify` 文案，而报告把 `key_verified=None` 渲染成“未校验
   （`--no-verify`）” —— 会把“校验没得出结论”说成“用户主动跳过”。
   复验一次并回写，这些陈旧/歧义记录就变成了当下的确定结论。

退出码：全活 = 0；有死 key = `cli.EXIT_KEY_UNVERIFIED`（与 `run.py` 同码，
"交付不完整"在仓库里只该有一个含义）。
"""

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src import cli as _cli  # noqa: E402
from src import ledger as _ledger
from src import opc  # noqa: E402


def _collect(args) -> list:
    """收集待复验的记录。按 `api_key` 去重 —— 同一把 key 复验两次没意义。"""
    if args.ledger:
        files = [Path(args.ledger)]
    elif args.all:
        files = _ledger.snapshot_paths()
        if not files:
            files = [_ledger.ledger_path()]
    else:
        files = [_ledger.ledger_path()]

    seen, out = set(), []
    for f in files:
        for r in _ledger.load_existing(f):
            k = (r.get("api_key") or "").strip()
            if not k or k in seen:
                continue
            seen.add(k)
            out.append(r)
    return out


def _write_back(args, verdicts: dict) -> int | None:
    """把复验结论写回台账。返回非 None = 该返回的退出码。

    🔴 写回目标是**台账读源**（`--ledger` 给了就用它），不是 `recs` ——
      `recs` 是按 `api_key` 去重后的子集（`--all` 时还会跨多份快照），
      拿它直接落盘会**把台账缩小**（正是防静默缩水护栏要拦的事）。
      所以先读回**全量**，再用 `apply_verdicts` 就地变换，条数不变。
    """
    target = Path(args.ledger) if args.ledger else _ledger.ledger_path()
    existing = _ledger.load_existing(target)
    if not existing:
        print(f"\n⚠ --apply：台账读源是空的（{target}），没有可回写的记录。")
        return None
    updated, changed = _ledger.apply_verdicts(existing, verdicts)
    if not changed:
        print("\nℹ --apply：没有记录的 api_key 命中本次复验结果，未写盘。")
        return None
    try:
        _ledger.save(target, updated)
    except ValueError as ex:
        print(f"✗ {ex}", file=sys.stderr)
        return _cli.EXIT_NO_SHRINK
    print(f"\n台账已回写：{target}（{changed} 条复验结论已更新，条数不变）")
    return None


def main() -> int:
    ap = argparse.ArgumentParser(
        description="复验台账里的 API Key（只读，不消耗注册配额）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--ledger", help="指定台账文件（默认：台账读源 = 最新全量快照）")
    ap.add_argument("--all", action="store_true", help="复验**全部**历史快照（按 api_key 去重）")
    ap.add_argument("--json", dest="json_out", help="额外落一份机器可读结果到该路径")
    ap.add_argument("--workers", type=int, default=8, help="并发数（默认 8）")
    ap.add_argument("--attempts", type=int, default=2, help="每个 key 的尝试次数")
    ap.add_argument(
        "--apply",
        action="store_true",
        help="把复验结论**写回台账**（默认只读；只改 key_verified / stages.verify，不动其它字段）",
    )
    args = ap.parse_args()

    # ⚠ 刻意**不**调 `config.validate()` 拦人：复验只需要 `SITE_BASE`，
    #   不需要邮箱源 / Worker 凭据。缺 Worker token 时挡在这里，会让
    #   "我手里这批 key 还能用吗"这个最该随时能答的问题答不上来。
    recs = _collect(args)
    if not recs:
        print("台账里没有带 api_key 的记录 —— 没什么可复验的。")
        return _cli.EXIT_OK

    print(f"复验 {len(recs)} 把 key（并发 {args.workers}，每个尝试 {args.attempts} 次）…\n")

    def one(r: dict) -> tuple:
        key = r["api_key"]
        # 🔴 用**记录里存的** base_url。空 = 回落到配置默认值 —— 传 `""`
        #    而不是 `None`：`verify_key` 内部就是 `base or 默认值`，空串等价，
        #    但省得它的 `base: str` 形参被传 `None`（类型上说不通）。
        base = str(r.get("base_url") or "")
        ok, why = opc.OpcClient.verify_key(key, base=base, attempts=args.attempts, delay=2.0)
        return r, bool(ok), why

    alive, dead = [], []
    verdicts: dict = {}
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        for f in as_completed([ex.submit(one, r) for r in recs]):
            r, ok, why = f.result()
            verdicts[r["api_key"]] = (bool(ok), why)
            (alive if ok else dead).append((r, why))

    for r, why in sorted(alive, key=lambda t: t[0].get("email") or ""):
        print(f"  ✓ {(r.get('email') or '(无邮箱)'):44s} {why}")
    for r, why in sorted(dead, key=lambda t: t[0].get("email") or ""):
        print(f"  ✗ {(r.get('email') or '(无邮箱)'):44s} {why}")
        print(
            f"      key: {opc.mask_key(r['api_key'])}  "
            f"base: {r.get('base_url') or '(未记录，用配置默认)'}"
        )

    # 🔴 必须带**分母** —— 只报「5 把存活」看不出还有几把没查。
    print(f"\n存活 {len(alive)}/{len(recs)}")
    if dead:
        print("  ⚠ 死 key 不一定是「站点挂了」：先看说明里的字节数 ——")
        print("    6707B 左右 = 打到了 SPA 兜底页，多半是 base_url 漏了 /api。")

    if args.json_out:
        payload = {
            "checked": len(recs),
            "alive": len(alive),
            "dead": len(dead),
            "results": [
                {"email": r.get("email"), "ok": True, "why": w, "base_url": r.get("base_url") or ""}
                for r, w in alive
            ]
            + [
                {
                    "email": r.get("email"),
                    "ok": False,
                    "why": w,
                    "base_url": r.get("base_url") or "",
                    "key_masked": opc.mask_key(r["api_key"]),
                }
                for r, w in dead
            ],
        }
        Path(args.json_out).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"  （机器可读结果 → {args.json_out}）")

    if args.apply:
        rc = _write_back(args, verdicts)
        if rc is not None:
            return rc

    return _cli.EXIT_KEY_UNVERIFIED if dead else _cli.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
