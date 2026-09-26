"""CLI 入口。

用法：
  python run.py                          # 跑 1 个账号（用池子里的第一个出口）
  python run.py --count 8                # 跑 8 个（池化，每出口 62s 一个）
  python run.py --count 8 --workers 4    # 登录侧 4 路并发
  python run.py --slot 7903              # 指定出口槽位
  python run.py --direct                 # 不用池子，直连（单 IP，慢）
  python run.py probe                    # **只探测**站点状态，不改动任何东西
  python run.py --out keys.json          # 显式导出到指定文件（不落台账快照）

关于落盘（台账布局）：
  **默认不填 --out = 写进台账目录** `ledger/`：
      ledger/runs/<日期>/results-<时间戳>.json   ← 合并后的**全量**快照（= 读源）
      ledger/latest.json                         ← **本批结果**（含失败/跳过）
  显式 `--out X` 才是"只写 X、不落快照"的老行为（导出到别处用）。

  ⚠ 台账的读源是**最新的那份全量快照**，**不是** `latest.json`。后者只含本批
    那几十条，拿它当读源 ⇒ 合并基准只剩上一批 ⇒ 台账停止累积、每次跑批覆盖
    上一次（参考项目栽过两次）。详见 `src/ledger.py`。

关于出口（**本项目最关键的一个参数**）：
  站点按 **IP** 限每日注册数（实测），所以"用几个不同出口 IP"直接决定本批
  能注册几个。`--slot` 指定单个，默认走池子（`FO_PROXY_SLOTS_FILE` 里全部）。
  池子怎么起见 README「起槽位池」。
"""

import argparse
import sys
import time
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src import cli as _cli  # noqa: E402
from src import config, opc, proxypool, quota  # noqa: E402
from src import ledger as _ledger
from src import report as _report  # noqa: E402
from src.pipeline import (  # noqa: E402
    ERR_IP_QUOTA,
    ERR_QUOTA_GUARD,
    error_kind_of,
    run_batch,
    run_one,
)
from src.redact import redact_url  # noqa: E402


def _print_pool_summary() -> int:
    """起飞前打印"每个出口还剩多少额度"。返回**总可用额度**。"""
    slots = config.proxy_slots()
    if not slots:
        st = quota.status()
        print(f"本地配额：{st.describe()}  [state: {quota.state_path()}]", flush=True)
        if st.exhausted:
            if st.banned:
                # 🔴 服务端确认的判死**不是**本地保守估计 —— 把两者混成一句，
                #    读者会以为“本地窗口太悲观”而去调大上限、继续对着一个
                #    已死的出口打请求。
                print(
                    "  ⚠ 服务端**已确认**该出口当日不可用（收到过 IP 日配额的 "
                    "429）—— 这**不是**本地的保守估计。\n"
                    "    唯一解法是换出口 IP，或等次日服务端恢复。"
                    "调 FO_IP_QUOTA_MAX / --ignore-quota 只会继续撞墙。",
                    flush=True,
                )
            else:
                print(
                    "  ⚠ 窗口内计数已达上限。这是**保守估计** —— 服务端恢复时间未知，"
                    "本地窗口取的是偏保守值。\n"
                    "    若确信服务端已恢复，可加 --ignore-quota 或调大 FO_IP_QUOTA_MAX。",
                    flush=True,
                )
        return st.remaining

    print(
        f"🔀 槽位池：{len(slots)} 个槽位已配置（{config.IR_PROXY_SLOTS_FILE or 'FO_PROXY_SLOTS'}）",
        flush=True,
    )
    seen: dict = {}
    total_left = 0
    try:
        for i, url in enumerate(slots, 1):
            ip = config.slot_scope(url)
            st = quota.status(scope=ip)
            left = st.remaining
            # 同一个出口 IP 只算一次额度 —— 6 个槽位可能只有 4 个不同出口。
            first = ip not in seen
            seen[ip] = seen.get(ip, 0) + 1
            if st.banned:
                mark = "已满（服务端确认）"
            elif st.exhausted:
                mark = "已满"
            else:
                mark = f"余 {left}"
            dup = "" if first else "（与前面的槽位同出口，额度已计入）"
            print(
                f"     slot{i} {redact_url(url):<24} 出口 {ip:<16} "
                f"{st.used:>2}/{st.limit}  {mark}{dup}",
                flush=True,
            )
            if first:
                total_left += left
    except ValueError as ex:
        print(f"\n✗ 槽位出口 IP 未登记，拒绝开跑：\n  {ex}", file=sys.stderr)
        return -1

    n_egress = len(seen)
    print(
        f"   ── {len(slots)} 槽 / {n_egress} 个不同出口，"
        f"合计可用额度 {total_left} 个"
        f"（⚠ 并发上限是 {n_egress}，不是 {len(slots)}）",
        flush=True,
    )
    return total_left


def _cmd_probe() -> int:
    """`run.py probe` —— 只读探测，**不改动任何东西**。

    🔴 为什么这个子命令值得单独存在：本链有两个"一开跑就废掉一个出口"的
      前置条件（验证码开关、每日配额余量）。起飞前 3 秒探一次，比跑完
      8 个账号全失败再回头查便宜得多。
    """
    c = opc.OpcClient()
    print(f"站点：{config.SITE_BASE}")
    try:
        c.fetch_csrf()
        reg = c.registration_state()
        pub = c.public_settings()
    except Exception as ex:
        print(f"✗ 站点不可达：{type(ex).__name__}: {ex}", file=sys.stderr)
        return 1

    print("\n── 注册开关 ──")
    print(f"  closed（关闭注册）      : {reg.get('closed')}")
    print(f"  captchaEnabled（验证码）: {reg.get('captchaEnabled')}")
    print(f"  blockGmailAliases       : {reg.get('blockGmailAliases')}")
    print(f"  notice                  : {reg.get('notice')!r}")

    rc = 0
    if reg.get("closed"):
        print("\n  ✗ 站点已**关闭注册**，本链无法继续。")
        rc = 2
    if reg.get("captchaEnabled"):
        # 🔴 这是**致命**的：验证码是 SVG 图形题（type ∈ judge/blank），
        #    纯协议链路解不了。不要"试一下再说" —— 那会白烧一个出口的额度。
        print("\n  ✗ 站点已**开启注册验证码** —— 纯协议链路无法通过。")
        print("    验证码是 SVG 图形题（`/api/captcha/new` 返回 {captchaId,svg,type}）。")
        print(
            "    需要改走浏览器方案（参考项目 Intern-Register-Tool 的 src/browser/ 是现成的骨架）。"
        )
        rc = 2

    svc = pub.get("modules", {}).get("modelService", {})
    print("\n── 模型服务模块 ──")
    print(f"  enabled: {svc.get('enabled')}（{svc.get('label')}）")
    if not svc.get("enabled"):
        print("  ⚠ 模型服务模块关闭 → 建 Key 会失败。")
        rc = rc or 3

    print("\n── 出口额度 ──")
    _print_pool_summary()

    print("\n── 槽位连通性 ──")
    slots = config.proxy_slots()
    if slots:
        alive = proxypool.check_slots_alive(slots)
        for i, ok in alive.items():
            print(f"  slot{i + 1} {redact_url(slots[i]):<24} {'✓ 监听中' if ok else '✗ 连不上'}")
        if not any(alive.values()):
            print("  ✗ 一个槽位都连不上 —— 池子没起来。跑 README「起槽位池」那三条命令。")
            rc = rc or 4
    else:
        print("  （未配置槽位池 → 会走直连，单 IP 一天只能注册几个）")
    return rc


def main() -> int:
    ap = argparse.ArgumentParser(description="FiimeOPC 注册 + 邮箱激活 + API Key 提取")
    ap.add_argument("--count", type=int, default=1, help="注册账号数量")
    ap.add_argument(
        "--workers",
        type=int,
        default=4,
        help="**登录侧**并发数（注册侧由每出口闸门控制，调大它没用）",
    )
    _cli.add_key_name_arg(ap)
    ap.add_argument("--slot", default=None, help="指定单个出口槽位（如 7903）。不传 = 用整个池子")
    ap.add_argument(
        "--direct", action="store_true", help="不用池子，直连（单 IP；一天只能注册几个，仅调试用）"
    )
    ap.add_argument("--mail-domain", default=None, help="临时邮箱域名（默认取 FO_WORKER_DOMAIN）")
    ap.add_argument(
        "--invite-code",
        default=None,
        help="注册邀请码。**默认取 FO_INVITE_CODE**（统一注入，"
        "一般不用传这个开关）；传空串 `--invite-code ''` 可"
        "临时关掉。⚠ 服务端不校验码是否存在，码错了不会报错，"
        "只会让推荐关系静默不成立。",
    )
    ap.add_argument(
        "--out",
        default=None,
        help="结果输出文件。**不填（默认）= 写进台账目录**："
        "ledger/runs/<日期>/results-<时间戳>.json（合并后的全量快照，"
        "也是台账读源）+ ledger/latest.json（本批结果）。"
        "显式给路径则只写那一个文件、不落快照。",
    )
    ap.add_argument(
        "--overwrite",
        action="store_true",
        help="只写本次结果、不合并历史（默认按 email 合并，防止一次小规模探测覆盖掉整个账号台账）",
    )
    ap.add_argument("--quiet", action="store_true", help="只输出汇总")
    ap.add_argument("--no-verify", action="store_true", help="跳过末尾的 key 可用性校验")
    ap.add_argument(
        "--probe-key",
        action="store_true",
        help="额外用 key 真发一次 /v1/chat/completions（**会消耗 Token**）",
    )
    _cli.add_ignore_quota_arg(ap)

    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("probe", help="只探测站点状态与出口额度，不改动任何东西")

    args = ap.parse_args()

    if args.cmd == "probe":
        return _cmd_probe()

    # 启动校验：缺凭据就立刻失败，别等跑了一半才发现全是 401。
    missing = config.validate()
    if missing:
        print(f"✗ 缺少必需配置：{'、'.join(missing)}", file=sys.stderr)
        print(
            "  修法：cp .env.example .env 并填入真实值（.env 已在 .gitignore 中，不会进仓库）",
            file=sys.stderr,
        )
        return 1

    # 起飞前探一次注册开关 —— 验证码一开，整链白跑。
    try:
        _c = opc.OpcClient()
        _c.fetch_csrf()
        reg = _c.registration_state()
    except Exception as ex:
        print(f"✗ 站点不可达：{type(ex).__name__}: {ex}", file=sys.stderr)
        return 1
    if reg.get("closed"):
        print("✗ 站点已关闭注册，拒绝开跑。", file=sys.stderr)
        return 1
    if reg.get("captchaEnabled"):
        print(
            "✗ 站点已开启注册验证码 —— 纯协议链路无法通过。\n"
            "  验证码是 SVG 图形题，需改走浏览器方案。",
            file=sys.stderr,
        )
        return 1

    # ── 邀请码：起飞前**规范化 + 印出来** ────────────────────────
    # 🔴 两件事都必须在这里做，不能留给下游：
    #   1) 规范化。不合法（超长/含非字母数字）就当场退出 —— 否则
    #      `opc.register()` 会在**重试循环里**抛 ValueError，被宽泛的
    #      `except Exception` 吞掉，表现成"N 个账号网络失败"，而真实原因
    #      是一个打错的命令行参数。
    #   2) 打印。服务端**不校验码是否存在**（乱填 8 位 → HTTP 200、账号照建），
    #      所以一个拼错的码在成功率、退出码、错误分类里**一个信号都没有** ——
    #      唯一的痕迹就是这一行，和报告末尾的「推荐关系」一节。
    try:
        eff_invite = config.normalize_invite_code(
            config.INVITE_CODE if args.invite_code is None else args.invite_code
        )
    except ValueError as ex:
        print(f"✗ 邀请码不合法：{ex}", file=sys.stderr)
        return 1
    if eff_invite:
        src = "--invite-code" if args.invite_code is not None else "FO_INVITE_CODE"
        print(
            f"🎟  邀请码：{eff_invite}（来源 {src}）\n"
            f"    ⚠ 服务端**不校验码是否存在** —— 码错了照样注册成功。"
            f"判据只有报告末尾的「推荐关系」一节。",
            flush=True,
        )
    else:
        print("🎟  邀请码：未设置 —— 这批账号不会挂在任何邀请人名下。", flush=True)

    if args.slot:
        proxy = f"http://127.0.0.1:{args.slot}"
        slots = [proxy]
    elif args.direct:
        proxy = None
        slots = []
    else:
        try:
            slots = config.proxy_slots()
        except (ValueError, OSError) as ex:
            print(f"✗ 槽位清单读不到：{ex}", file=sys.stderr)
            return 1
        proxy = slots[0] if slots else None

    total_left = _print_pool_summary()
    if total_left < 0:
        return 1
    if slots and not args.ignore_quota:
        hint = quota.shortfall_hint(total_left, args.count, args.ignore_quota)
        if hint:
            print(hint, flush=True)

    t0 = time.time()

    # ── 单账号 + 单出口：走顺序路径（日志最清楚）────────────────
    if args.count == 1:
        if not args.quiet:
            print(f"\n▶ 单账号模式（出口 {proxy or '直连'}）", flush=True)
        try:
            rec = run_one(
                key_name=args.key_name,
                mail_domain=args.mail_domain,
                proxy=proxy or "",
                verbose=not args.quiet,
                verify=not args.no_verify,
                probe=args.probe_key,
                # 🔴 传**已规范化**的 `eff_invite`（str）而不是原始
                #    `args.invite_code`（可能是 None）：语义完全一样
                #    （`None` 在 `stage_register` 里也是回落到
                #    `config.INVITE_CODE`，而那已经过同一套规范化），
                #    但省掉一个 Optional，调用链的类型一致。
                invite_code=eff_invite,
            )
        except proxypool.AllSlotsDead as ex:
            print(f"\n✗ {ex}", file=sys.stderr)
            return _cli.EXIT_ALL_SLOTS_DEAD
        results = [rec]
    else:
        # ── 批量：两段式流水线（池子在 run_batch 内部建）──────────
        # ⚠ `--slot` / `--direct` 与池模式互斥：显式指定单出口时，临时把
        #   池子配置关掉，让 run_batch 走 `lease=None` 那条路。
        import os

        saved_file = config.IR_PROXY_SLOTS_FILE
        saved_inline = config.IR_PROXY_SLOTS
        if args.slot or args.direct:
            os.environ.pop("FO_PROXY_SLOTS_FILE", None)
            os.environ.pop("FO_PROXY_SLOTS", None)
            config.IR_PROXY_SLOTS_FILE = ""
            config.IR_PROXY_SLOTS = ""
            if args.slot:
                # 单槽位模式：把池子退化成"只有这一个槽位"，这样仍然有
                # 租约/冷却/记账，只是没有并发。
                os.environ["FO_PROXY_SLOTS"] = f"http://127.0.0.1:{args.slot}"
                config.IR_PROXY_SLOTS = f"http://127.0.0.1:{args.slot}"
        try:
            results = run_batch(
                count=args.count,
                workers=args.workers,
                key_name=args.key_name,
                mail_domain=args.mail_domain,
                verbose=not args.quiet,
                ignore_quota=args.ignore_quota,
                verify=not args.no_verify,
                probe=args.probe_key,
                invite_code=eff_invite,
            )
        except proxypool.AllSlotsDead as ex:
            # 🔴 刻意给一个干净的报错而不是 traceback：这个失败模式的误读
            #    代价极高 —— 槽位进程没起来时，每条记录都会以"代理连接错误"
            #    收场，而"注册全失败"在本项目里最容易被读成"IP 又被封了"。
            print(f"\n✗ {ex}", file=sys.stderr)
            return _cli.EXIT_ALL_SLOTS_DEAD
        except quota.QuotaExceeded as ex:
            print(f"\n✗ {ex}", file=sys.stderr)
            return _cli.EXIT_QUOTA_EXCEEDED
        finally:
            config.IR_PROXY_SLOTS_FILE = saved_file
            config.IR_PROXY_SLOTS = saved_inline

    wall = time.time() - t0

    # ── 落盘 ──────────────────────────────────────────────────
    #   * `--out` 不填（默认）→ 走**台账目录**：读 `ledger.ledger_path()`
    #     （= 最新那份全量快照），写全量快照 + 把**本批**刷进 latest.json。
    #   * `--out X` 显式给路径 → 老行为，只写 X、不落快照。
    out = Path(args.out) if args.out else None
    new_records = [asdict(r) for r in results]
    if args.overwrite:
        merged = new_records
        print(f"\n（--overwrite：只写本次 {len(merged)} 条，不合并历史）")
    else:
        # 🔴 读源用 `out or ledger_path()`：`--out` 不填时**必须**读台账读源。
        #    若这里退化成读"本次要写的那个新文件"，合并就等于没做 ——
        #    每次运行都从零开始，正是 `--count 1` 把 53 条覆盖成 1 条那条路。
        existing = _ledger.load_existing(out or _ledger.ledger_path())
        merged, kept, added, upgraded = _ledger.merge_records(existing, new_records)
        if kept:
            print(_cli.merge_summary_line(kept, added, upgraded, len(merged)))
    try:
        if out is None:
            snap, last = _ledger.save_snapshot(
                merged, new_records, existing=[] if args.overwrite else None
            )
            written = snap
            print(
                f"\n台账已落盘：\n"
                f"  快照（读源） {snap.relative_to(_ledger.ROOT)}\n"
                f"  本批结果     {last.relative_to(_ledger.ROOT)}"
                f"（{len(new_records)} 条）"
            )
        else:
            written = _ledger.save(out, merged, existing=[] if args.overwrite else None)
    except ValueError as ex:
        # 防静默缩水护栏。少数据但指标全"正常"是最坏的失败，
        # 宁可报错退出也不要静默丢掉账号。
        print(f"✗ {ex}", file=sys.stderr)
        return _cli.EXIT_NO_SHRINK

    # 收尾报告整块在 `src/report.py`（测试链不该 import `run.py` —— 它会把
    # 整套 pipeline 拉进 sys.path）。依赖全部**注入**，报告层只依赖 stdlib。
    _report.render_batch_report(
        results,
        written,
        wall,
        workers=args.workers,
        config=config,
        quota=quota,
        error_kind_of=error_kind_of,
        err_ip_quota=ERR_IP_QUOTA,
        err_quota_guard=ERR_QUOTA_GUARD,
    )

    # ── 退出码：交付完整性 ────────────────────────────────────
    # 🔴 `status == "success"` 只说明"key 建出来了"，**不代表它能调用**。
    #    本项目栽过一次：base_url 漏 `/api` 打到 SPA 兜底页，3 个账号
    #    全部"建出成功、校验失败"，而当时的退出码是 0 —— 任何脚本化调用
    #    都会把这一批读成"成功"。
    #    所以只要有一个 key 明确校验不过，就非零退出。
    #    `--no-verify`（`key_verified is None`）**不算失败** —— 那是用户
    #    显式选择不校验，不是我们校验失败。
    broken = [r for r in results if r.key_verified is not None and not r.key_verified]
    if broken:
        print(
            f"\n✗ {len(broken)} 个 key 建出来了但**校验不过**，"
            f"这批交付不完整（见上面的「交付完整性」一节）",
            file=sys.stderr,
        )
        return _cli.EXIT_KEY_UNVERIFIED
    return _cli.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
