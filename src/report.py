"""报告渲染层：跑完一批之后印给人看的那一大块 stdout。

为什么单独一个模块
==================
`run.py` 一旦被 import 就会把整套 pipeline 拉进 `sys.path`，所以**测试链不该
import 它**。凡是需要被测试钉住的格式/判据，一律抽到这里来。

本模块**只依赖 stdlib**（`statistics` + 本项目的 `redact`），刻意
**不 import `src.pipeline` / `src.config` / `src.quota`**：

  * `error_kind_of` / `ERR_*` / `config` / `quota` 全部由调用方**注入** ——
    报告层因此是依赖图上的叶子，不会把 `requests` 之类的传递依赖带进测试链；
  * 测试里想造「出口被判死」「配额触顶」这类场景，塞个桩就行。

⚠ 报告里 **API Key 明文只在最后一节出现**（`调用方式`），其余地方一律走
  `mask_key()`。理由：这份 stdout 经常被贴进对话/issue，而 key 是**只显示一次**
  的凭据。台账（`ledger/`，整目录 gitignored）才是明文该待的地方。
"""

import statistics

LATENCY_LABELS = "均值 / 中位 / 最快 / 最慢"


def latency_summary(totals) -> str:
    """把一组耗时（秒）格式化成 `均值 / 中位 / 最快 / 最慢` 一行。

    🔴 四个标签必须配**四个值，而且印在同一行**。参考项目实测到的缺陷：
      调用方把「均值」单独留在上一行的合计列，这一行仍写四个标签却只给三个值
      ⇒ 读者按左对齐会把"最慢"读成"最快"。
    """
    xs: list = []
    for x in totals:
        # 🔴 非数字（缺段/半截记录）直接跳过，不把整份报告炸掉 ——
        #    这是展示层，不该因为一条坏数据让整批结果打不出来。
        try:
            xs.append(float(x))
        except (TypeError, ValueError):
            continue
    if not xs:
        return ""
    return (
        f"{statistics.mean(xs):.1f} / {statistics.median(xs):.1f} / {min(xs):.1f} / {max(xs):.1f}"
    )


def fmt_ms(v) -> str:
    """毫秒 → 一位小数的秒；**非数字给 `-`**。

    非数字（缺段：失败/跳过/还没走到那一段）必须区别于 `0.0` ——
    把半截耗时和完整耗时放在同一列里比是误导。
    """
    return f"{v / 1000:.1f}" if isinstance(v, (int, float)) else "-"


def _mask(key: str) -> str:
    from .redact import mask_key

    return mask_key(key)


def render_batch_report(
    results, written, wall, *, workers, config, quota, error_kind_of, err_ip_quota, err_quota_guard
) -> None:
    """渲染一批结果的收尾报告（直接 `print` 到 stdout）。

    `results`  —— `AccountRecord` 列表（成功/跳过/失败都在里面）
    `written`  —— 结果落盘路径
    `wall`     —— 批次墙钟秒数（只在没有 `keys_done`/`batch_total` 时兜底）
    `workers`  —— 登录侧并发数
    `config`   —— 有 `SITE_BASE` / `MODEL_V1_PATH` 的对象
    `quota`    —— 有 `status()` 的对象（`src.quota` 或桩）
    `error_kind_of` / `err_ip_quota` / `err_quota_guard` —— 由 pipeline 注入

    ⚠ 判「出口被判死」走 `error_kind_of()`（**结构化字段**），不搜 `error` 文本
      —— 我们自己拼的守卫文案里也含"已达上限"这几个字，文本匹配会把
      "一个请求都没发的主动中止"算成"服务端确认的封禁"。
    """
    ok = [r for r in results if r.status == "success"]
    skipped = [r for r in results if r.status == "skipped"]
    bad = [r for r in results if r.status not in ("success", "skipped")]

    print(f"\n{'=' * 78}")
    print(f"DONE: {len(ok)}/{len(results)} succeeded -> {written.resolve()}")
    if skipped:
        print(f"      {len(skipped)} 个被配额保护跳过（未发请求，非失败）")

    # ── 耗时明细 ──────────────────────────────────────────────
    # 🔴 「合计」= 注册 + 登录 + 建Key，是**单账号端到端**耗时，必须自己算。
    #    别去读 `timings['total']` —— 批量模式下那个键不存在，退化成
    #    `batch_total`（批次墙钟）会让这一列**每行都是同一个数**，
    #    看起来像"所有账号耗时一样"，其实是显示 bug。
    print("\n耗时明细（秒）：")
    print(f"  {'email':44s} {'出口':>20s} {'注册':>7s} {'登录':>7s} {'建Key':>7s} {'合计':>7s}")
    totals: list = []
    for r in results:
        tm = r.timings or {}
        vals = [tm.get(k) for k in ("register", "login", "key")]
        nums = [v for v in vals if isinstance(v, (int, float))]
        if len(nums) == len(vals):
            total_ms = sum(nums)
            totals.append(total_ms / 1000)
            total_s = f"{total_ms / 1000:.1f}"
        else:
            total_s = "-"
        # 🔴 不要静默截断 scope 的**尾部**：`res-<hash>` 是会话桶名，切尾会把它
        #    显示成**另一个桶**。超长才加省略号。
        eg = r.egress or "-"
        egress = eg if len(eg) <= 20 else eg[:19] + "…"
        print(
            f"  {(r.email or '(未建邮箱)'):44s} {egress:>20s} "
            f"{fmt_ms(tm.get('register')):>7s} "
            f"{fmt_ms(tm.get('login')):>7s} "
            f"{fmt_ms(tm.get('key')):>7s} {total_s:>7s}"
        )
    if totals:
        hdr = f"── 统计（{len(totals)} 个完整样本）"
        print(f"  {hdr:44s} {'':>20s}")
        # 🔴 四个标签与四个取值**必须在同一个 print 里**（见 `latency_summary`）。
        print(
            f"  {'   ' + LATENCY_LABELS:44s} {'':>20s} "
            f"{'':>7s} {'':>7s} {'':>7s} {latency_summary(totals)}"
        )

    # ── 注册内部阶段（取**最慢**的账号）────────────────────────
    # 🔴 看最慢的那个，不是第一个 —— 批量吞吐由关键路径（最慢账号）决定，
    #    离群值才是要找的东西。只打印第一个会把离群值藏起来。
    slow_reg = None
    for r in results:
        sub = (r.timings or {}).get("register_detail")
        if not sub:
            continue
        if slow_reg is None or (r.timings or {}).get("register", 0) > (slow_reg.timings or {}).get(
            "register", 0
        ):
            slow_reg = r
    if slow_reg is not None:
        print(
            f"\n注册内部阶段（最慢账号 {slow_reg.email or '(未建邮箱)'}，"
            f"注册 {fmt_ms(slow_reg.timings.get('register'))}s）："
        )
        d = slow_reg.timings["register_detail"]
        # 🔴 这些键是**计数/文本**，不是毫秒 —— 混进去会打印成 "0.00s"，
        #    看着像个耗时。
        NON_MS = {"mail_polls", "mail_5xx", "mail_subject", "activation_token"}
        for k, v in d.items():
            if k.endswith("_ms") or k in NON_MS:
                continue
            print(f"  {k:16s} {v / 1000:6.2f}s")
        polls = d.get("mail_polls")
        if polls:
            e5 = d.get("mail_5xx", 0)
            extra = f"，其中 5xx 重试 {e5} 次" if e5 else ""
            print(f"  {'收信轮询':14s} {polls:6d} 次 /api/inbox{extra}")
        if d.get("mail_subject"):
            print(f"  {'邮件主题':14s} {d['mail_subject']}")

    # ── 注册耗时分布 ──────────────────────────────────────────
    regs = sorted(r.timings.get("register", 0) / 1000 for r in results if r.timings.get("register"))
    if len(regs) >= 2:
        print(f"\n注册耗时分布（{len(regs)} 个账号）：")
        print(
            f"  最快 {regs[0]:.1f}s · 中位 {regs[len(regs) // 2]:.1f}s "
            f"· 最慢 {regs[-1]:.1f}s · 极差 {regs[-1] - regs[0]:.1f}s"
        )
        if regs[-1] > regs[0] * 1.5:
            print("  ⚠ 极差超过最快值的 1.5 倍 —— 总时长多半由这个离群值决定，不是均值")

    # ── 吞吐 ──────────────────────────────────────────────────
    if results:
        tm0 = results[0].timings or {}
        kd, bt = tm0.get("keys_done"), tm0.get("batch_total")
        n_egress = len({r.egress for r in results if r.egress}) or 1
        print(
            f"\n吞吐（{len(results)} 个账号，登录并发 workers={workers}，"
            f"用到的出口 {n_egress} 个）："
        )
        if kd and bt:
            print(
                f"  关键路径（全部 key 建出）: {kd / 1000:6.1f}s "
                f"= {kd / 1000 / len(results):5.1f}s / 账号"
            )
            print(
                f"  含末尾统一校验          : {bt / 1000:6.1f}s "
                f"= {bt / 1000 / len(results):5.1f}s / 账号"
            )
        else:
            print(f"  总耗时 {wall:6.1f}s = {wall / len(results):5.1f}s / 账号")
        # 🔴 这里的"理论上限"由**注册闸门**决定，不是登录并发：
        #    每出口每 REG_MIN_INTERVAL 秒只能注册 1 个。
        print(
            f"  ⚠ 本链的硬瓶颈是**注册闸门**：每出口 {config.REG_MIN_INTERVAL:g}s 1 个。"
            f"\n     {n_egress} 个出口 → 理论上限 "
            f"{n_egress * 3600 / config.REG_MIN_INTERVAL:.0f} 个/小时。"
            f"调大 workers 不会突破它。"
        )

    # ── 结果清单 ──────────────────────────────────────────────
    if ok:
        print("\n成功账号（key 明文只在下面这一节出现）：")
        for r in ok:
            print(f"  {r.email:44s} {r.api_key}")

    # ── 交付完整性：key 是不是**真的能用** ─────────────────────
    # 🔴 这是整条链的最终判据。`status == "success"` 只说明"key 建出来了"，
    #    不代表它能调用 —— 本项目栽过一次：base_url 漏 `/api` 打到 SPA 兜底页，
    #    3 个账号全是"建出成功、校验失败"，而清单里一切看着都正常。
    #    所以这一节**必须单独报**，而且必须带分母。
    if ok:
        # 🔴 `key_verified` 是**三态**，`None` 必须与 `False` 分开：
        #    True=通过 / False=不通过 / None=没有结论。
        #    用真值判断而不是 `is True`/`is False`（对 `bool` 字面量做 `is`
        #    比较会被判成「与字面量做身份比较」）。
        verified = [r for r in ok if r.key_verified]
        broken = [r for r in ok if r.key_verified is not None and not r.key_verified]
        untested = [r for r in ok if r.key_verified is None]
        print(f"\n交付完整性（可调用性）：{len(verified)}/{len(ok)} 个 key 校验通过")
        if broken:
            print(f"  ✗ {len(broken)} 个**建出来了但校验不过** —— 这些不算交付：")
            for r in broken:
                print(f"      {r.email:40s} {(r.stages or {}).get('verify', '')[:70]}")
            print(
                "    先看说明里的字节数：6707B 左右 = 打到了 SPA 兜底页，"
                "多半是 base_url 漏了 /api。"
            )
        if untested:
            # 🔴 `key_verified is None` **不等于**“用户主动 --no-verify”。
            #    它可能是：① 用户显式 `--no-verify`；② 旧版本代码写进台账、
            #    当前代码无法复现的陈旧结论。早期文案直接把 None 说成
            #    “`--no-verify`”，等于把“没得出结论”报成“用户选择不校验” ——
            #    会让人放心地不去查。所以这里如实说“没有结论”，并给出定论手段。
            print(f"  ℹ {len(untested)} 个**没有校验结论**（`key_verified` 为空）：")
            print("     可能是 `--no-verify` 主动跳过，也可能是台账里的陈旧结论。")
            print("     定论方法：python tools/ops/check_keys_alive.py --apply（复验并回写）")
        if verified and not broken and not untested:
            print("  ✓ 全部可调用，无遗留项。")

    # ── 邀请码（推荐关系）──────────────────────────────────────
    # 🔴 这一节必须**单独存在、单独判据**，因为它是整份报告里**唯一**能发现
    #    "整批用错码"的地方：服务端**不校验码是否存在**（实测：乱填 8 位
    #    `ZZZZZZZZ` → HTTP 200、账号照建、零报错），所以成功率、退出码、
    #    错误分类**全是绿的**。只有 `invited_at` 会诚实地保持 0。
    #    不单独报 ⇒ 一个拼错的码可以连着跑几批都没人发现。
    sent = [r for r in results if getattr(r, "invite_code", "")]
    if sent:
        # 分母只算**走到了结论**的记录：`stages["invite"]` 是注册+激活都成功
        # 之后才写的。半路失败的账号压根没走到能判断的那一步 —— 把它们算进
        # 分母，会把"没走到"误报成"码废了"，等于发假警报。
        judged = [r for r in sent if (r.stages or {}).get("invite")]
        confirmed = [r for r in judged if getattr(r, "invited_at", 0)]
        missed = [r for r in judged if not getattr(r, "invited_at", 0)]
        codes = sorted({r.invite_code for r in sent})

        print(f"\n邀请码（推荐关系）：{len(confirmed)}/{len(judged)} 个已确认受邀")
        print(
            f"  本次使用：{'、'.join(codes)}"
            f"（{len(sent)} 个账号发出了码"
            f"，{len(sent) - len(judged)} 个未走完注册+激活、无法判断）"
        )
        if not judged:
            print("  ℹ 没有一个账号走到能判断的那一步 —— 见下面「失败」一节。")
        elif missed:
            print(
                f"  ⚠ {len(missed)} 个**已发码但服务端未记录受邀**"
                f"（`invitedAt` = 0）—— 推荐关系未成立："
            )
            for r in missed:
                print(f"      {(r.email or '(未建邮箱)'):40s}")
            print("    服务端**不校验码是否存在**，所以这类问题**不会**以注册失败的形式暴露。")
            print("    常见原因：码拼错 / 码已失效 / 用自己的码给自己注册。")
        else:
            print("  ✓ 发出的码全部生效。")

    if bad:
        print(f"\n失败 {len(bad)} 个：")
        for r in bad:
            print(f"  {(r.email or '(未建邮箱)'):44s} {(r.error or '')[:90]}")

        # 🔴 出口被判死是批量失败最常见的原因，且**退避重试无用**。
        #    判据走 `error_kind_of()`（结构化字段），不搜 error 文本 ——
        #    我们自己拼的守卫文案里也含那几个字，文本匹配会把主动中止算进来。
        quota_hits = [r for r in bad if error_kind_of(r) == err_ip_quota]
        if quota_hits:
            eg = sorted({r.egress for r in quota_hits if r.egress})
            print(f"\n  ⚠ 其中 {len(quota_hits)} 个是**出口被判死**（该 IP 今日注册数量已达上限）")
            if eg:
                print(f"    涉及出口：{', '.join(eg)}")
            print("    这是 **IP 维度**的每日配额，不是瞬时速率 ——")
            print("      · 调 FO_REG_MIN_INTERVAL（速率闸门）**无效**；")
            print("      · 唯一解法是**换出口 IP**（加槽位 / 换订阅 / 换节点）；")
            print("      · 换邮箱域名、换用户名、换 UA 都无效（已实测）。")
        guard_hits = [r for r in bad if error_kind_of(r) == err_quota_guard]
        if guard_hits:
            print(
                f"\n  ℹ 另有 {len(guard_hits)} 个是**本地守卫主动中止**"
                f"（一个请求都没发）—— 这不是服务端拒绝，"
            )
            print(
                "    说明本地计数比服务端更保守。要放行：调大 FO_IP_QUOTA_MAX 或加 --ignore-quota。"
            )

    # ── 调用方式 ──────────────────────────────────────────────
    if ok:
        # 🔴 用**记录里存的那个** base_url，不要现算 —— 台账里可能是历史批次
        #    用的地址，拿今天的配置去解释昨天的 key 会得出错误结论。
        #    老记录（`base_url` 为空）才回落到配置默认值。
        base = ok[0].base_url or f"{config.SITE_BASE}{config.MODEL_V1_PATH}"
        model = ok[0].default_model or config.DEFAULT_MODEL
        print("\n调用方式（OpenAI 兼容）：")
        print(f"  base_url = {base}")
        print(f"  model    = {model}")
        print(f"  api_key  = {ok[0].api_key}")
        # ⚠ 早期这行是 `每账号最多 {stages['service']}`，而那个字段的值是
        #   `ok(19 models, max_keys=5)` —— 拼出来读成"每账号最多 ok(19 models…)"。
        #   分开说清楚：能调几个模型 / 每个账号能建几把 key。
        n_models = len(ok[0].models or [])
        tail = "（含 text/image/video 三类）" if n_models else ""
        print(
            f"  ⚠ 该 key 可调 {n_models} 个模型{tail}；"
            f"每账号最多 {config.MAX_KEYS_PER_USER} 把 key。"
        )
        print("    key 明文只在台账与本行出现，别把这段贴出去。")

    # ── 收尾再报一次配额 ──────────────────────────────────────
    if ok:
        scopes = sorted({r.egress for r in ok if r.egress})
        if scopes:
            print("\n各出口消耗（本次）：")
            for s in scopes:
                st = quota.status(scope=s)
                # 🔴 `describe()` 自带“出口 <scope>”前缀，别再拼一次 ——
                #    否则会成 “res-xxx 出口 res-xxx 在 24h 窗口内已用 …”。
                print(f"  {st.describe()}")
        else:
            print(f"\n本地配额：{quota.status().describe()}")
    print(
        f"\n本地配额合计：{quota.status().describe()}"
        f"（本次成功 {len(ok)} 个，跳过 {len(skipped)} 个）"
    )
    print("=" * 78)
