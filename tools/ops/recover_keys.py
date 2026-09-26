#!/usr/bin/env python
"""从**线上账号**把 API Key 捞回台账。**不消耗注册配额。**

为什么需要它
============
`run_batch` 把本批记录攒在**内存**里，跑完才落盘。中途崩了（Ctrl-C、
网络断、进程被杀），已经建出来的 key 就只存在于服务端的 `GET /api/model-service`
里 —— 台账上那些账号是"注册成功但没有 key"。

正常思路是"key 只显示一次，丢了就删掉重建"。但**本站不是这样**：
实测 `GET /api/model-service` 的 `keys[].secret` 每次都会原样返回明文
（站点 UI 那句「只显示一次」是**前端**的说法，后端没这个限制）。
⇒ 崩掉的那批可以**零成本捞回来**，不用重新注册（注册要烧 IP 日配额，5 个/天）。

🔴 这也意味着一个安全事实：**任何拿到会话 cookie 的人都能读走全部 key。**
   所以这个工具的输出（含明文）只该进 `ledger/`（整目录 gitignored），
   不要把结果贴到任何地方。

用法
====
  python tools/ops/recover_keys.py                    # 干跑：只报告缺哪些，不写盘
  python tools/ops/recover_keys.py --apply            # 真写回台账
  python tools/ops/recover_keys.py --apply --refresh  # 连已有 key 的也重新拉一遍
  python tools/ops/recover_keys.py --email a@b.com --apply   # 只处理一个账号
  python tools/ops/recover_keys.py --journal latest   # 从**未收尾的跑批日志**读账号

🔴 `--journal`：`run_batch` 每完成一个账号就追加一行 JSONL（`ledger/runs/.../
   journal-*.jsonl`）。正常收尾后会被删；**进程中途被杀**时它留在盘上，
   里面是那批已注册账号（含密码）的**唯一副本** —— 没有它，那批 key 不可恢复
   （batch73 就是这样丢的）。`--journal latest` = 取最新一份。

🔴 **默认干跑**。写台账是不可逆的（护栏只能防"变少"，防不了"写错内容"），
   所以必须先看一遍要写什么。`--apply` 是显式动作。
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src import cli as _cli  # noqa: E402
from src import ledger as _ledger  # noqa: E402
from src import opc


def _load_read_source(path=None) -> tuple:
    """返回 `(读源路径, 记录列表)`。"""
    p = Path(path) if path else _ledger.ledger_path()
    return p, _ledger.load_existing(p)


def _resolve_journal(spec: str) -> Path | None:
    """把 `--journal` 的取值解析成一个存在的日志路径；`latest` = 最新一份。"""
    if spec in ("", "latest", "-"):
        js = _ledger.journal_paths()
        return js[-1] if js else None
    p = Path(spec)
    return p if p.is_file() else None


def _has_activated_account(rec: dict) -> bool:
    """这条记录指的账号是否**已建好、可登录**。

    台账里的最终记录用 `status == "success"`；而**增量日志**里“只走完注册+激活、
    key 还没建”的那一行 `status` 仍是 `"init"`（`stage_login_key` 结尾才置 success）。
    那种记录**同样能登录捞 key** —— 这正是 batch73 丢批后要用到的入口。
    判据取结构化的 `stages["register"]/["activate"]`，**不搜 error 文本**。
    """
    if rec.get("status") == "success":
        return True
    stages = rec.get("stages") or {}
    return (
        rec.get("status") == "init"
        and stages.get("register") == "ok"
        and stages.get("activate") == "ok"
    )


def _needs_recovery(rec: dict, *, refresh: bool) -> bool:
    """这条记录要不要捞？

    ⚠ 账号必须**已建好**（台账 success，或日志里注册+激活 ok）—— 注册都没成功
      （或没走到激活）的账号根本登不进去，去登录只会白跑并可能触发风控。
      已建好但 `api_key` 为空 = 典型的需要捞。
    """
    if not _has_activated_account(rec):
        return False
    if not (rec.get("email") and rec.get("password")):
        return False
    return refresh or not (rec.get("api_key") or "").strip()


def main() -> int:
    ap = argparse.ArgumentParser(
        description="从线上账号把 API Key 捞回台账（不消耗注册配额）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "--apply", action="store_true", help="真写回台账（**默认干跑**，只报告要写什么）"
    )
    ap.add_argument(
        "--refresh",
        action="store_true",
        help="连**已有 key** 的记录也重新拉一遍（核对台账是否与线上一致）",
    )
    ap.add_argument("--email", help="只处理这一个账号")
    ap.add_argument(
        "--key-name",
        default="default",
        help="账号上有 key 但名字不匹配时，按这个名单选（默认 default）",
    )
    ap.add_argument("--ledger", help="指定台账读源（默认：最新全量快照）")
    ap.add_argument(
        "--journal",
        nargs="?",
        const="latest",
        help="从未收尾的跑批日志（JSONL）读取记录；`latest` = 最新一份。"
        "与 --ledger 读源合并后再处理 —— 用于捞回中途被杀那批的 key。",
    )
    args = ap.parse_args()

    src, records = _load_read_source(args.ledger)
    if args.journal:
        jp = _resolve_journal(args.journal)
        if jp is None:
            print(f"✗ 找不到跑批日志：{args.journal}", file=sys.stderr)
            return 1
        jrecs = _ledger.journal_load(jp)
        records, _kept, added, _up = _ledger.merge_records(records, jrecs)
        print(f"跑批日志：{jp}（{len(jrecs)} 条，其中 {added} 条不在台账）")
    if not records:
        print(f"台账读源是空的（{src}）—— 没什么可捞的。")
        return _cli.EXIT_OK

    todo = [r for r in records if _needs_recovery(r, refresh=args.refresh)]
    if args.email:
        todo = [r for r in todo if r.get("email") == args.email]

    print(f"台账读源：{src}")
    print(f"共 {len(records)} 条记录，其中 {len(todo)} 条需要捞 key")
    if not todo:
        print("  ✓ 没有缺口。")
        return _cli.EXIT_OK

    recovered, still_missing = [], []
    for rec in todo:
        email = rec.get("email") or "(无邮箱)"
        c = opc.OpcClient(proxy=None, log=lambda *a: None)
        try:
            c.fetch_csrf()
            lg = c.login(rec["email"], rec["password"])
            if not lg.ok:
                print(f"  ✗ {email:44s} 登录失败：{lg.error}{f'（{lg.code}）' if lg.code else ''}")
                still_missing.append((rec, f"login: {lg.error}"))
                continue
            keys = c.list_keys()
        except Exception as ex:
            print(f"  ✗ {email:44s} {type(ex).__name__}: {str(ex)[:80]}")
            still_missing.append((rec, f"{type(ex).__name__}"))
            continue

        if not keys:
            print(f"  ○ {email:44s} 线上确实一把 key 都没有（已登录确认）")
            still_missing.append((rec, "no keys online"))
            continue

        # 优先同名 key；没有就取第一把（并**明确报出来**，不静默换名）。
        pick = c.find_key(name=args.key_name) or keys[0]
        secret = str(pick.get("secret") or "")
        if not secret:
            print(f"  ✗ {email:44s} 线上 key 没带 secret 字段（形状变了？）")
            still_missing.append((rec, "no secret in list"))
            continue

        if pick.get("name") != args.key_name:
            print(
                f"  ⚠ {email:44s} 没有名为 {args.key_name!r} 的 key，"
                f"取到的是 {pick.get('name')!r}（**已如实记下名字**）"
            )

        rec["api_key"] = secret
        rec["key_id"] = str(pick.get("id") or "")
        rec["key_masked"] = str(pick.get("masked") or opc.mask_key(secret))
        stages = dict(rec.get("stages") or {})
        stages["recovered"] = (
            f"from live account (name={pick.get('name')!r}, createdAt={pick.get('createdAt')})"
        )
        rec["stages"] = stages
        recovered.append(rec)
        print(f"  ✓ {email:44s} 捞到 {rec['key_masked']}（{pick.get('name')!r}）")

    print(f"\n捞回 {len(recovered)}/{len(todo)}")
    if still_missing:
        print(f"  ℹ 仍有 {len(still_missing)} 条没捞到：")
        for r, why in still_missing:
            print(f"      {(r.get('email') or '(无邮箱)'):44s} {why}")

    if not recovered:
        return _cli.EXIT_OK

    if not args.apply:
        print("\n🔴 干跑结束 —— **没有写盘**。确认上面的清单无误后加 --apply 再跑一次。")
        print("   （写台账不可逆：护栏只能防'变少'，防不了'写错内容'。）")
        return _cli.EXIT_OK

    # 🔴 合并而不是覆盖：捞回来的记录只改了 key 相关字段，
    #    `merge_records` 的 rank+并集规则会保住其余字段（egress / timings / …）。
    merged, kept, added, upgraded = _ledger.merge_records(records, recovered)
    try:
        snap, last = _ledger.save_snapshot(merged, recovered)
    except ValueError as ex:
        print(f"✗ {ex}", file=sys.stderr)
        return _cli.EXIT_NO_SHRINK

    print(
        f"\n台账已写回：\n"
        f"  快照（读源） {snap.relative_to(_ledger.ROOT)}\n"
        f"  本批结果     {last.relative_to(_ledger.ROOT)}"
    )
    print(
        f"  合并：原有 {kept} + 捞回 {added}"
        f"{f'（{upgraded} 条字段已更新）' if upgraded else ''} = {len(merged)} 条"
    )
    print("\n⚠ 这份台账含明文 key。它整目录 gitignored —— 别把内容贴出去。")
    return _cli.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
