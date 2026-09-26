"""`src.report` 的**交付判据**渲染。**不联网、不落盘。**

这里钉的是两类东西：

1. `latency_summary` 的"四标签配四值"——参考项目实测过标签与值错位的缺陷；
2. **交付完整性**一节 —— `status == "success"` 只说明"key 建出来了"，
   **不代表它能调用**。本项目栽过一次（base_url 漏 `/api` 打到 SPA 兜底页），
   当时 3 个账号全是"建出成功、校验失败"，而报告里一切看着都正常。
   所以这一节必须有测试，尤其是**分母**。
"""

import pytest

from src import config
from src.pipeline import ERR_IP_QUOTA, ERR_QUOTA_GUARD, AccountRecord, error_kind_of
from src.report import fmt_ms, latency_summary, render_batch_report


# ── 依赖桩 ────────────────────────────────────────────────────────
class _QuotaStub:
    def status(self, scope=None):
        return self

    def describe(self):
        return "1/5（窗口 24h）"


def _render(results, capsys, written=None):
    render_batch_report(
        results, written or __import__("pathlib").Path("ledger/x.json"), 12.0,
        workers=4, config=config, quota=_QuotaStub(),
        error_kind_of=error_kind_of, err_ip_quota=ERR_IP_QUOTA,
        err_quota_guard=ERR_QUOTA_GUARD)
    return capsys.readouterr().out


def _ok(email, key="opc_sk_abc", verified: bool | None = True, **kw):
    return AccountRecord(email=email, api_key=key, status="success",
                         key_verified=verified, **kw)


# ── 耗时格式化 ────────────────────────────────────────────────────
class TestLatencySummary:
    def test_four_labels_four_values(self):
        """🔴 四个标签必须配**四个值、同一行**。

        参考项目实测到的缺陷：调用方把「均值」单独留在上一行的合计列，
        这一行仍写四个标签却只给三个值 ⇒ 读者按左对齐把"最慢"读成"最快"。
        """
        out = latency_summary([1, 2, 3, 4])
        assert out.count("/") == 3
        vals = [p.strip() for p in out.split("/")]
        assert len(vals) == 4
        assert vals[0] == "2.5"          # 均值
        assert vals[1] == "2.5"          # 中位
        assert vals[2] == "1.0"          # 最快
        assert vals[3] == "4.0"          # 最慢

    def test_empty_is_empty(self):
        assert latency_summary([]) == ""

    def test_fmt_ms_none_is_dash_not_zero(self):
        """🔴 缺段必须是 `-`，不能是 `0.0`。

        把半截耗时和完整耗时放进同一列比是误导 —— 0.0 看着像"这段不耗时"，
        实际是"这段根本没跑到"。
        """
        assert fmt_ms(None) == "-"
        assert fmt_ms("") == "-"
        assert fmt_ms(0) == "0.0"        # 真的 0 才是 0.0


# ── 交付完整性 ────────────────────────────────────────────────────
class TestDeliveryCompleteness:
    def test_all_verified_reports_clean(self, capsys):
        out = _render([_ok("a@x.com"), _ok("b@x.com", key="opc_sk_b")], capsys)
        assert "交付完整性（可调用性）：2/2 个 key 校验通过" in out
        assert "全部可调用，无遗留项" in out

    def test_broken_keys_are_surfaced_with_reason(self, capsys):
        """🔴 这是本项目最重要的一条：建出成功 ≠ 能用。"""
        r = _ok("a@x.com", verified=False)
        r.stages["verify"] = "HTTP 200 但 content-type=text/html（6707 字节）"
        out = _render([r, _ok("b@x.com", key="opc_sk_b")], capsys)
        assert "1/2 个 key 校验通过" in out
        assert "建出来了但校验不过" in out
        assert "6707 字节" in out, "要把说明原文带出来，否则无从定位"
        assert "漏了 /api" in out, "要给出最常见的根因"

    def test_unverified_is_not_counted_as_broken(self, capsys):
        """🔴 `key_verified=None` 不算失败，但**也不能说成"用户主动跳过"**。

        它可能是 `--no-verify`，也可能是台账里的陈旧结论 —— 报告必须如实说
        "没有结论"并指出去哪定论，而不是替读者下一个它不知道的判断。
        """
        out = _render([_ok("a@x.com", verified=None)], capsys)
        assert "0/1 个 key 校验通过" in out
        assert "没有校验结论" in out
        assert "check_keys_alive.py --apply" in out, "要给出定论手段"
        assert "建出来了但校验不过" not in out

    def test_denominator_is_total_success_not_verified(self, capsys):
        """🔴 分母必须是 `len(ok)`，不是"已校验的个数"。

        只报 `2/2 通过` 而实际有 5 个成功账号，读者会以为全绿 ——
        这正是"数据静默缩水、指标全绿"那一类失败。
        """
        recs = [_ok(f"{i}@x.com", key=f"opc_sk_{i}") for i in range(3)]
        recs += [_ok("bad@x.com", key="opc_sk_bad", verified=False)]
        recs += [_ok("skip@x.com", key="opc_sk_s", verified=None)]
        out = _render(recs, capsys)
        assert "3/5 个 key 校验通过" in out

    def test_base_url_comes_from_record_not_config(self, capsys):
        """🔴 用**记录里存的那个** base_url。

        台账里可能是历史批次用的地址；拿今天的配置去解释昨天的 key
        会得出错误结论（而且不报错）。
        """
        r = _ok("a@x.com")
        r.base_url = "https://legacy.example.com/v1"
        r.default_model = "legacy-model"
        out = _render([r], capsys)
        assert "base_url = https://legacy.example.com/v1" in out
        assert "model    = legacy-model" in out
        assert f"{config.SITE_BASE}{config.MODEL_V1_PATH}" not in out

    def test_base_url_falls_back_for_old_records(self, capsys):
        """老记录 `base_url` 为空 ⇒ 回落到配置默认值，而不是打印空白。"""
        out = _render([_ok("a@x.com")], capsys)
        assert f"base_url = {config.SITE_BASE}{config.MODEL_V1_PATH}" in out
        assert f"model    = {config.DEFAULT_MODEL}" in out

    def test_capacity_line_is_readable(self, capsys):
        """🔴 容量提示不能拼成 `每账号最多 ok(19 models, max_keys=5)`。

        早期那行直接把 `stages['service']` 塞进「每账号最多 {}」，而那个字段的值
        是 `ok(19 models, max_keys=5)` —— 拼出来是句病句，而且把"能调几个模型"
        和"能建几把 key"两件事混成一件。
        """
        r = _ok("a@x.com")
        r.models = ["opc-txt-v1", "opc-image-v1"]
        out = _render([r], capsys)
        assert "该 key 可调 2 个模型" in out
        assert f"每账号最多 {config.MAX_KEYS_PER_USER} 把 key" in out
        assert "每账号最多 ok(" not in out

    def test_key_plaintext_only_in_final_section(self, capsys):
        """🔴 明文 key 只能出现在「调用方式」与「成功账号」两节。

        这份 stdout 经常被贴进对话/issue，而 key 是**只显示一次**的凭据。
        """
        out = _render([_ok("a@x.com", key="opc_sk_SECRET123")], capsys)
        assert out.count("opc_sk_SECRET123") == 2


# ── 邀请码（推荐关系）─────────────────────────────────────────────
class TestInviteSection:
    """🔴 这一节是整份报告里**唯一**能发现"整批用错码"的地方。

    服务端不校验码是否存在（实测无效码 → HTTP 200、账号照建），所以
    成功率、退出码、错误分类**全是绿的** —— 只有 `invitedAt` 会诚实地保持 0。
    这一节一旦被删或分母算错，一个拼错的码可以连着跑几批都没人发现。
    """

    def test_silent_when_no_code_was_sent(self, capsys):
        """没发码就整节不出现 —— 别用空节刷屏。"""
        out = _render([_ok("a@x.com")], capsys)
        assert "推荐关系" not in out

    def test_reports_confirmed_referral(self, capsys):
        r = _ok("a@x.com", invite_code="ZQ9QEVJJ",
                invited_at=1788000000000, stages={"invite": "ok"})
        out = _render([r], capsys)
        assert "邀请码（推荐关系）：1/1 个已确认受邀" in out
        assert "ZQ9QEVJJ" in out

    def test_flags_code_sent_but_not_recorded(self, capsys):
        """🔴 发码了但 `invitedAt` 为 0 ⇒ 必须显式报出来并解释为什么不报错。"""
        r = _ok("a@x.com", invite_code="ZQ9QEVJJ", invited_at=0,
                stages={"invite": "⚠ 已发码但服务端未记录受邀"})
        out = _render([r], capsys)
        assert "0/1 个已确认受邀" in out
        assert "已发码但服务端未记录受邀" in out
        assert "不校验码是否存在" in out, "要解释为什么注册会「成功」"

    def test_denominator_excludes_incomplete_accounts(self, capsys):
        """🔴 分母只算**走到了结论**的账号。

        半路失败的账号（比如收信超时）压根没走到能判断的那一步 ——
        把它们算进分母，会把"没走到"误报成"码废了"，那是**假警报**，
        比不报更坏：它会让人去改一个根本没问题的码。
        """
        done = _ok("a@x.com", invite_code="ZQ9QEVJJ",
                   invited_at=1788000000000, stages={"invite": "ok"})
        stuck = AccountRecord(email="b@x.com", status="failed",
                              invite_code="ZQ9QEVJJ", error="mail: 超时")
        out = _render([done, stuck], capsys)
        assert "1/1 个已确认受邀" in out, "分母被没走完的账号污染了"
        assert "2 个账号发出了码" in out
        assert "未走完注册+激活" in out

    def test_reports_when_nothing_reached_a_conclusion(self, capsys):
        """全都半路失败 ⇒ 说清"无法判断"，不要报 0/0 这种读不出意思的东西。"""
        stuck = AccountRecord(email="a@x.com", status="failed",
                              invite_code="ZQ9QEVJJ", error="mail: 超时")
        out = _render([stuck], capsys)
        assert "无法判断" in out
        assert "见下面「失败」一节" in out

    def test_lists_multiple_codes_when_mixed(self, capsys):
        """一批里混了两个码（临时换码跑了一半）⇒ 两个都要列出来。"""
        a = _ok("a@x.com", invite_code="ZQ9QEVJJ",
                invited_at=1, stages={"invite": "ok"})
        b = _ok("b@x.com", invite_code="R5AZGYNU",
                invited_at=1, stages={"invite": "ok"})
        out = _render([a, b], capsys)
        assert "ZQ9QEVJJ" in out and "R5AZGYNU" in out

    def test_does_not_leak_plaintext_key(self, capsys):
        """这一节也不能漏出 key 明文（它不在「调用方式」/「成功账号」两节里）。"""
        r = _ok("a@x.com", key="opc_sk_SECRET123", invite_code="ZQ9QEVJJ",
                invited_at=1, stages={"invite": "ok"})
        out = _render([r], capsys)
        assert out.count("opc_sk_SECRET123") == 2


# ── 失败归因 ──────────────────────────────────────────────────────
class TestFailureAttribution:
    def test_ip_quota_advice_says_rate_limit_tuning_is_useless(self, capsys):
        r = AccountRecord(email="a@x.com", status="failed",
                          error="register: 该 IP 今日注册数量已达上限",
                          error_kind=ERR_IP_QUOTA, egress="1.2.3.4")
        out = _render([r], capsys)
        assert "出口被判死" in out
        assert "1.2.3.4" in out
        assert "无效" in out, "必须明说调速率闸门没用"
        assert "换出口 IP" in out

    def test_guard_hits_are_not_reported_as_server_rejection(self, capsys):
        """🔴 本地守卫主动中止**不是**服务端拒绝 —— 两者混在一起会误导。

        判据走 `error_kind_of()`（结构化字段）。若按文本匹配，我们自己拼的
        守卫文案里也含"已达上限"，会把"一个请求都没发的主动中止"算成封禁。
        """
        r = AccountRecord(email="b@x.com", status="failed",
                          error="本地守卫：该 IP 已达上限，未发请求",
                          error_kind=ERR_QUOTA_GUARD)
        out = _render([r], capsys)
        assert "本地守卫主动中止" in out
        assert "出口被判死" not in out

    def test_guard_text_alone_would_be_misread(self, capsys):
        """🔴 变异验证：把 `error_kind` 抹掉、只留文案，**确实会**被误判。

        这条用例的存在意义是证明上面那条不是空跑 —— 如果文本匹配也能得出
        正确结论，那 `error_kind_of()` 这个结构化字段就是多余的。
        """
        r = AccountRecord(email="b@x.com", status="failed",
                          error="本地守卫：该 IP 已达上限，未发请求",
                          error_kind="")
        # 抹掉结构化字段后，`error_kind_of` 的文案兜底会把它读成 ip_quota。
        assert error_kind_of(r) == ERR_IP_QUOTA
        out = _render([r], capsys)
        assert "出口被判死" in out, "这正是我们要避免的误判 —— 所以字段不能省"

    def test_error_kind_of_tolerates_old_records(self):
        """老台账没有 `error_kind` 字段 ⇒ 靠文案兜底，不能炸。"""
        r = AccountRecord(email="a@x.com", status="failed",
                          error="register: 该 IP 今日注册数量已达上限")
        assert error_kind_of(r) == ERR_IP_QUOTA
        assert error_kind_of(r) != ERR_QUOTA_GUARD


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
