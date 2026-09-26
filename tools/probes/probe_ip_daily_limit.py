"""测定「单 IP 每日注册上限」—— 在同一个槽位出口上按 65s 间隔连续注册，
直到出现 `该 IP 今日注册数量已达上限`，报告成功次数。

🔴 为什么要测这个数：它是本地配额保护（src/quota.py）的**唯一依据**。
   不知道上限就只能瞎猜，猜小了浪费额度、猜大了撞墙。
   实测的撞墙代价很高（撞上之后**当天**这个 IP 就废了），所以只用一个槽位测。

用法：
    python tools/probes/probe_ip_daily_limit.py [--slot 7902] [--max 12]
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src import cli, config, mailbox, opc, quota  # noqa: E402

# 🔴 见 src/cli.py：Windows GBK 控制台重定向输出时，下面的 `✓`/`⚠` 会崩。
cli.force_utf8_stdio()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slot", default="7902", help="槽位端口（默认 7902，避开其它测试）")
    ap.add_argument("--max", type=int, default=12, help="最多尝试几次")
    ap.add_argument("--gap", type=float, default=None,
                    help="两次之间间隔秒数（默认取 config.REG_MIN_INTERVAL）")
    args = ap.parse_args()

    proxy = f"http://127.0.0.1:{args.slot}"
    gap = args.gap if args.gap is not None else config.REG_MIN_INTERVAL
    # 🔴 账要记到**出口 IP** 上（配额维度），不是槽位号。端口没登记时退化成
    #    空 scope，并明确告警 —— 不静默假设。
    try:
        scope = config.slot_scope(proxy)
    except ValueError as ex:
        scope = ""
        print(f"⚠ 该槽位出口 IP 未登记，本次消耗不会计入出口配额：{ex}", flush=True)
    print(f"槽位 {proxy}（出口 {scope or '未登记'}），"
          f"间隔 {gap}s，最多 {args.max} 次", flush=True)

    ok = 0
    for i in range(1, args.max + 1):
        mail = mailbox.make_source()
        email = mail.create_mailbox()[0]
        c = opc.OpcClient(proxy=proxy)
        c.fetch_csrf()
        u = c.gen_available_username()
        r = c.register(u, email, opc.gen_password())
        stamp = time.strftime("%H:%M:%S")
        if r.ok:
            ok += 1
            # 🔴 探测脚本以前**绕过** quota.record()，于是它烧掉的服务端额度在
            #    本地账上不存在 —— 一个当天已被判死的出口会显示 0/5。
            #    这里如实记一笔，让下一轮 `probe` / `run.py` 看到真相。
            quota.record(email, scope=scope)
            print(f"[{i:2}] {stamp} ✓ 成功  {u}  (累计 {ok})", flush=True)
        else:
            print(f"[{i:2}] {stamp} ✗ HTTP {r.http_status} kind={r.limit_kind!r} "
                  f"err={r.error!r}", flush=True)
            if r.limit_kind == opc.LIMIT_IP_QUOTA:
                # 服务端**确认**判死 —— 落成硬证据，别只留在本次输出里。
                quota.record_ban(scope, r.error)
                print(f"\n⇒ 该出口在**成功 {ok} 次后**触顶", flush=True)
                return 0
        if i < args.max:
            time.sleep(gap)
    print(f"\n⇒ 跑满 {args.max} 次未触顶，成功 {ok} 次", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
