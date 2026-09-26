#!/usr/bin/env python
"""把「注册成功但激活失败」的账号捞回来（激活 + 建 key）。**不消耗注册配额。**

为什么需要
==========
站点偶发把激活邮件里的链接发坏（实测 2026-09-26 一批 10 个里 2 个）：
链接是 `.../activate?token<62hex>` —— **既漏了 `=`，token 还被截成 62 位**
（正常是 `=` + 64hex）。`stage_register` 现在会自愈
（`activate_with_resend`），本工具用来**事后**处理已经落进台账的失败记录。

对每条「register=ok 但没激活」的记录：
  ① 先登录试试（可能上一次已经激活、只是没建 key）；
  ② 需要激活就走 `activate_with_resend()`：优先用最新一封**格式正确**的邮件，
     必要时 `POST /auth/resend-activation` 重发
     （⚠ 重发会**作废上一个 token**，必须取最新那封 —— 见 docs/protocol.md）；
  ③ 登录 → 建 key → 校验 → 回写台账（rank+并集，只补字段）。

🔴 **默认干跑**：写台账不可逆，先看一遍要写什么，确认无误再加 `--apply`。
   激活 / 登录 / 建 key 都**不消耗注册配额**（那是注册阶段的专属成本，5 个/IP/天）。

用法
====
  python tools/ops/recover_activation.py                 # 干跑：只报告要写什么
  python tools/ops/recover_activation.py --apply         # 真写回台账
  python tools/ops/recover_activation.py --email a@b.com --apply   # 只处理一个账号
  python tools/ops/recover_activation.py --ledger X.json
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src import cli as _cli  # noqa: E402
from src import config, mailbox, opc  # noqa: E402
from src import ledger as _ledger
from src.pipeline import activate_with_resend  # noqa: E402


def _candidates(records: list) -> list:
    """还没激活、但**注册成功**的记录（只有这些才捞得回来）。"""
    out = []
    for r in records:
        if r.get("status") == "success":
            continue
        st = r.get("stages") or {}
        if st.get("register") != "ok":
            continue  # 注册都没成，没有账号可激活
        if st.get("activate") == "ok":
            continue
        if not (r.get("email") and r.get("password")):
            continue
        out.append(r)
    return out


def _newest_link(mails: list) -> tuple:
    """返回 `(link_or_None, newest_received_at)`。

    取**最新一封带激活链接**的邮件 —— 重发会作废旧 token，必须用最新的。
    """
    link, newest = None, 0
    for m in sorted(mails, key=lambda x: x.received_at or 0):
        newest = max(newest, m.received_at or 0)
        lk = opc.find_activation_url(m.body or "")
        if lk:
            link = lk
    return link, newest


def _recover_one(mail, r: dict, key_name: str) -> tuple:
    """捞一个账号。返回 `(updated_dict_or_None, 失败原因)`。"""
    email, pwd = r["email"], r["password"]
    c = opc.OpcClient(log=lambda *a: None)
    c.fetch_csrf()
    # ① 也许已经激活了，只是没建 key
    lg = c.login(email, pwd)
    if not lg.ok and lg.code == "EMAIL_NOT_ACTIVATED":
        link, newest = _newest_link(mail.list_mails(email))
        print(f"  激活（现有链接：{'有' if link else '无'}）")
        act, resent = activate_with_resend(
            mail, c, email, link, since_ts=newest + 1, log=lambda m: print("  " + m)
        )
        print(
            f"  activate: ok={act.ok} http={act.http_status} err={act.error}"
            f"{'（重发过）' if resent else ''}"
        )
        if act.ok:
            lg = c.login(email, pwd)
    if not lg.ok:
        return None, f"login: {lg.error}（{lg.code}）"

    # ② 建 key + 校验
    si = c.service_info()
    k = c.create_key(key_name)
    base = config.SITE_BASE + (si.api_base_path or config.MODEL_V1_PATH)
    ok, why = opc.OpcClient.wait_until_active(k.key, base=base, attempts=3, delay=3.0)
    print(f"  key={k.masked}  verify={ok} {why}")

    new = dict(r)
    new.update(
        {
            "status": "success",
            "error": "",
            "error_kind": "",
            "api_key": k.key,
            "key_id": k.key_id,
            "key_masked": k.masked,
            "base_url": base,
            "key_verified": bool(ok),
            "models": si.model_ids(),
            "default_model": si.default_model(),
            "user_id": str((lg.user or {}).get("id") or new.get("user_id", "")),
        }
    )
    st = dict(new.get("stages") or {})
    st.update(
        {
            "activate": "ok",
            "login": "ok",
            "key": "ok",
            "verify": why,
            "recovered": "tools/ops/recover_activation.py",
        }
    )
    new["stages"] = st
    return new, ""


def main() -> int:
    ap = argparse.ArgumentParser(
        description="把「注册成功但激活失败」的账号捞回来（不消耗注册配额）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--apply", action="store_true", help="真写回台账（**默认干跑**）")
    ap.add_argument("--email", help="只处理这一个账号")
    ap.add_argument("--ledger", help="指定台账读源（默认：最新全量快照）")
    ap.add_argument("--key-name", default="default", help="建的 key 名字（默认 default）")
    args = ap.parse_args()

    src = Path(args.ledger) if args.ledger else _ledger.ledger_path()
    records = _ledger.load_existing(src)
    if not records:
        print(f"台账读源是空的（{src}）—— 没什么可捞的。")
        return _cli.EXIT_OK
    todo = _candidates(records)
    if args.email:
        todo = [r for r in todo if r.get("email") == args.email]

    print(f"台账读源：{src}")
    print(f"共 {len(records)} 条记录，其中 {len(todo)} 条「注册成功但未激活」需要捞")
    if not todo:
        print("  ✓ 没有需要捞的。")
        return _cli.EXIT_OK

    # ⚠ 刻意**不**调 `config.validate()`：捞回只需要站点 + 邮箱，缺 Worker token 时
    #    挡在这里会让"能不能捞回来"这个最该随时能答的问题答不上来。
    mail = mailbox.make_source()
    recovered, still = [], []
    for r in todo:
        print(f"\n{r['email']}")
        try:
            new, why = _recover_one(mail, r, args.key_name)
        except Exception as ex:
            new, why = None, f"{type(ex).__name__}: {ex}"
        if new is None:
            print(f"  ✗ {why}")
            still.append((r, why))
        else:
            recovered.append(new)

    print(f"\n捞回 {len(recovered)}/{len(todo)}")
    for r, why in still:
        print(f"  ✗ {(r.get('email') or '(无邮箱)'):44s} {why}")
    if not recovered:
        return _cli.EXIT_OK
    if not args.apply:
        print("\n🔴 干跑结束 —— **没有写盘**。确认上面的清单无误后加 --apply 再跑一次。")
        return _cli.EXIT_OK

    # 🔴 合并而不是覆盖：捞回的记录只改了 key/激活相关字段，merge_records 的
    #    rank+并集规则会保住其余字段（egress / timings / …）。
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
    print("\n⚠ 这份台账含明文 key，整目录 gitignored —— 别把内容贴出去。")
    return _cli.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
