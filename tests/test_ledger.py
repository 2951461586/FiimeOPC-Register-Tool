"""`src.ledger` —— 台账合并与防静默缩水护栏。**不联网、不碰真实台账。**

🔴 这个模块的每条规则都对应一次真实事故，所以用例是"钉行为"不是"走过场"。
"""

import json

import pytest

from src import ledger


# ── 合并规则 ──────────────────────────────────────────────────────
class TestMergeRecords:
    def test_adds_new(self):
        merged, kept, added, up = ledger.merge_records([], [{"email": "a", "status": "success"}])
        assert len(merged) == 1 and kept == 0 and added == 1 and up == 0

    def test_keeps_history(self):
        """老记录里本次没跑到的**必须保留** —— 它们是历史，不是垃圾。"""
        old = [{"email": "a", "status": "success"}, {"email": "b", "status": "success"}]
        new = [{"email": "c", "status": "success"}]
        merged, kept, added, _ = ledger.merge_records(old, new)
        assert {r["email"] for r in merged} == {"a", "b", "c"}
        assert kept == 2 and added == 1

    def test_failure_does_not_overwrite_success(self):
        """🔴 服务端抖一下不该把好账号标成坏。"""
        old = [{"email": "a", "status": "success", "api_key": "opc_sk_1"}]
        new = [{"email": "a", "status": "failed", "error": "network"}]
        merged, _, _, _ = ledger.merge_records(old, new)
        assert merged[0]["status"] == "success"
        assert merged[0]["api_key"] == "opc_sk_1"

    def test_success_upgrades_failure(self):
        old = [{"email": "a", "status": "failed", "error": "boom"}]
        new = [{"email": "a", "status": "success", "api_key": "opc_sk_1"}]
        merged, _, _, up = ledger.merge_records(old, new)
        assert merged[0]["status"] == "success" and up == 1

    def test_same_rank_takes_union(self):
        """🔴 同级取**并集**，不是整条替换。

        参考项目实测：下游工具交回的是**增量字段**（只有 `verify`，没有
        `status`）→ rank 恒为 0，而旧记录 rank=2 ⇒ `rank(new) > rank(old)`
        **永远为假** ⇒ 字段一个都写不进去，而打印出来的一切都"正常"。
        这是最危险的一类失败：**数据静默缩水，指标全绿**。
        """
        old = [{"email": "a", "status": "success", "api_key": "k", "egress": "1.1.1.1"}]
        new = [{"email": "a", "status": "success", "stages": {"verify": "ok"}}]
        merged, _, _, up = ledger.merge_records(old, new)
        assert merged[0]["api_key"] == "k"          # 旧字段没丢
        assert merged[0]["egress"] == "1.1.1.1"     # 旧字段没丢
        assert merged[0]["stages"] == {"verify": "ok"}  # 新字段进来了
        assert up == 1

    def test_rank_without_status_is_zero(self):
        """没有 `status` 的增量记录 rank=0 —— 这正是要取并集的场景。"""
        assert ledger.rank({"verify": "ok"}) == 0
        assert ledger.rank({"status": "success"}) == 2
        assert ledger.rank({"status": "skipped"}) == 1
        assert ledger.rank({"status": "failed"}) == 0

    def test_records_without_email_kept_verbatim(self):
        """没有 email 的记录原样保留，不参与去重（防止把两条都吃掉）。"""
        old = [{"note": "手工备注"}]
        merged, kept, _, _ = ledger.merge_records(old, [{"note": "另一条"}])
        assert kept == 1 and len(merged) == 2

    def test_dedups_existing(self):
        """老台账里有同 email 的重复记录时，留第一条。"""
        old = [{"email": "a", "v": 1}, {"email": "a", "v": 2}]
        merged, kept, _, _ = ledger.merge_records(old, [])
        assert len(merged) == 1 and merged[0]["v"] == 1


# ── 防静默缩水护栏 ────────────────────────────────────────────────
class TestNoShrinkGuard:
    def test_raises_on_shrink(self, tmp_path):
        """🔴 少数据但指标全"正常"是最坏的失败 —— 宁可报错也不要静默丢账号。"""
        p = tmp_path / "r.json"
        p.write_text(json.dumps([{"email": "a"}, {"email": "b"}]), encoding="utf-8")
        with pytest.raises(ValueError, match="防静默缩水"):
            ledger.save(p, [{"email": "a"}])

    def test_explicit_empty_existing_allows_shrink(self, tmp_path):
        """`existing=[]` 是逃生口：显式声明"这次缩水是我要的"。"""
        p = tmp_path / "r.json"
        p.write_text(json.dumps([{"email": "a"}, {"email": "b"}]), encoding="utf-8")
        ledger.save(p, [{"email": "a"}], existing=[])
        assert len(ledger.load_existing(p)) == 1

    def test_growth_is_fine(self, tmp_path):
        p = tmp_path / "r.json"
        ledger.save(p, [{"email": "a"}, {"email": "b"}], existing=[])
        assert len(ledger.load_existing(p)) == 2

    def test_none_existing_auto_loads_and_guards(self, tmp_path):
        """🔴 `existing=None` 是"没传"，**不是**"跳过检查"。

        这个方向是刻意的：`save(p, recs)` 是最自然的调用形式，如果 `None`
        代表跳过，那护栏在最常见的路径上等于没装。所以 `None` 在 `save()`
        入口被转成"从磁盘自动读取当前台账"，护栏**生效**。

        ⚠ 早期 docstring 写反了（说 `None` 表示不检查），并因此带偏了本条用例。
        """
        p = tmp_path / "r.json"
        p.write_text(json.dumps([{"email": "a"}, {"email": "b"}]), encoding="utf-8")
        with pytest.raises(ValueError, match="防静默缩水"):
            ledger.save(p, [{"email": "a"}], existing=None)

    def test_none_existing_skips_when_no_file(self, tmp_path):
        """`None` + 文件不存在 ⇒ 自动读到 `[]` ⇒ 正常写入（不是报错）。"""
        p = tmp_path / "new.json"
        ledger.save(p, [{"email": "a"}], existing=None)
        assert len(ledger.load_existing(p)) == 1


# ── 读源与快照布局 ────────────────────────────────────────────────
class TestLayout:
    def test_latest_is_not_the_read_source(self, tmp_path, monkeypatch):
        """🔴 `latest.json` **不能**当读源。

        它只含本批那几条。拿它当读源 ⇒ 合并基准只剩上一批 ⇒ 台账停止累积、
        每次跑批覆盖上一次。参考项目栽过两次，第二次是 `--count 1` 把 53 条
        覆盖成 1 条。
        """
        monkeypatch.setattr(ledger, "LEDGER_DIR", tmp_path / "ledger")
        assert ledger.ledger_path().name != ledger.LAST_RUN_NAME

    def test_ledger_path_returns_nonexistent_placeholder_when_empty(self, tmp_path,
                                                                   monkeypatch):
        """空台账目录不该让工具起不来 —— 返回一个**永不存在的占位路径**，
        让 `load_existing()` 按契约返回 `[]`。"""
        monkeypatch.setattr(ledger, "LEDGER_DIR", tmp_path / "ledger")
        p = ledger.ledger_path()
        assert not p.exists()
        assert ledger.load_existing(p) == []

    def test_snapshot_path_shape(self):
        p = ledger.snapshot_path(when=0)
        assert ledger.SNAPSHOT_RE.match(p.name), p.name
        assert p.parent.name.count("-") == 2       # YYYY-MM-DD

    def test_snapshot_paths_ignores_irregular_names(self, tmp_path, monkeypatch):
        """🔴 用正则筛，不用 `glob("*.json")`。

        `runs/` 是个人工可写的目录，一个 `notes.json` 落进去，按"最新"
        就会被当成台账读源 —— 于是缩水护栏的分母、`--count` 的取样池
        全跟着变，而且**全程不报错**。
        """
        monkeypatch.setattr(ledger, "LEDGER_DIR", tmp_path / "ledger")
        d = tmp_path / "ledger" / "runs" / "2026-09-26"
        d.mkdir(parents=True)
        (d / "results-20260926-010101.json").write_text("[]", encoding="utf-8")
        (d / "notes.json").write_text("[]", encoding="utf-8")
        (d / "results.json.bak").write_text("[]", encoding="utf-8")
        got = ledger.snapshot_paths()
        assert len(got) == 1 and got[0].name == "results-20260926-010101.json"

    def test_snapshot_paths_sorted_ascending(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ledger, "LEDGER_DIR", tmp_path / "ledger")
        d = tmp_path / "ledger" / "runs" / "2026-09-26"
        d.mkdir(parents=True)
        for n in ("results-20260926-030000.json", "results-20260926-010000.json"):
            (d / n).write_text("[]", encoding="utf-8")
        names = [p.name for p in ledger.snapshot_paths()]
        assert names == sorted(names)

    def test_ledger_path_repicks_after_new_snapshot(self, tmp_path, monkeypatch):
        """🔴 每次调用都**重新扫目录**，不要冻进模块级常量。

        "先读台账 → 跑 → 再写回台账"的流程里，常量会一直指向**落盘之前**
        那份快照 ⇒ 写回去等于覆盖掉刚生成的新快照。
        """
        monkeypatch.setattr(ledger, "LEDGER_DIR", tmp_path / "ledger")
        d = tmp_path / "ledger" / "runs" / "2026-09-26"
        d.mkdir(parents=True)
        first = d / "results-20260926-010000.json"
        first.write_text("[]", encoding="utf-8")
        assert ledger.ledger_path() == first
        second = d / "results-20260926-020000.json"
        second.write_text("[]", encoding="utf-8")
        assert ledger.ledger_path() == second, "读源必须跟着新快照走"

    def test_save_snapshot_writes_both(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ledger, "LEDGER_DIR", tmp_path / "ledger")
        merged = [{"email": "a", "status": "success"}]
        batch = [{"email": "a", "status": "success"}]
        snap, last = ledger.save_snapshot(merged, batch, existing=[])
        assert snap.is_file() and last.is_file()
        assert last.name == ledger.LAST_RUN_NAME
        assert ledger.ledger_path() == snap

    def test_is_ledger_path(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ledger, "LEDGER_DIR", tmp_path / "ledger")
        assert ledger.is_ledger_path(tmp_path / "ledger" / "x.json")
        assert not ledger.is_ledger_path(tmp_path / "elsewhere" / "x.json")


# ── 加载容错 ──────────────────────────────────────────────────────
class TestLoadExisting:
    def test_missing_file_returns_empty(self, tmp_path):
        assert ledger.load_existing(tmp_path / "nope.json") == []

    def test_corrupt_json_returns_empty(self, tmp_path):
        """🔴 刻意不抛：一个坏掉的结果文件不该让整次运行失败。

        真正的护栏在 `_guard_no_shrink` 的条数检查上。
        """
        p = tmp_path / "bad.json"
        p.write_text("{not json", encoding="utf-8")
        assert ledger.load_existing(p) == []

    def test_non_list_returns_empty(self, tmp_path):
        p = tmp_path / "obj.json"
        p.write_text('{"a": 1}', encoding="utf-8")
        assert ledger.load_existing(p) == []

    def test_filters_non_dict_items(self, tmp_path):
        p = tmp_path / "mix.json"
        p.write_text('[{"email":"a"}, "junk", 3, null]', encoding="utf-8")
        assert ledger.load_existing(p) == [{"email": "a"}]


# ── 覆盖率护栏 ────────────────────────────────────────────────────
class TestCoverage:
    def test_key_coverage_direction(self):
        recs = [{"api_key": "opc_sk_a"}, {"api_key": "opc_sk_b"}]
        n_led, n_known, missing = ledger.key_coverage(recs, {"opc_sk_a"})
        assert n_led == 2 and n_known == 1 and missing == {"opc_sk_b"}

    def test_non_prefix_keys_excluded(self):
        """🔴 前缀常量必须与站点一致（`opc_sk_`，不是 `sk-`）。"""
        recs = [{"api_key": "sk-wrong"}, {"api_key": "opc_sk_right"}]
        n_led, _, _ = ledger.key_coverage(recs, set())
        assert n_led == 1

    def test_empty_values_dropped_from_denominator(self):
        """空值不算"一个缺失的条目" —— 算进去会让覆盖率虚高。"""
        recs = [{"api_key": ""}, {"api_key": None}, {"api_key": "opc_sk_a"}]
        n_led, _, _ = ledger.key_coverage(recs, set())
        assert n_led == 1

    def test_exit_code_nonzero_on_gap(self):
        """🔴 退出码必须非 0 —— 护栏原来只 print，`main()` 照样 return 0，
        任何脚本化调用都会读到"成功"。"""
        assert ledger.coverage_exit_code({"x"}) == ledger.EXIT_COVERAGE_GAP
        assert ledger.coverage_exit_code(set()) == 0
        assert ledger.coverage_exit_code({"x"}, allow_partial=True) == 0

    def test_coverage_block_has_denominator(self):
        """🔴 artifact 里必须带"分母"，否则事后单独看就是"53/53 全绿"。"""
        b = ledger.coverage_block(605, 53, {"x"})
        assert b["ledger_n"] == 605 and b["known_n"] == 53
        assert b["covers_ledger"] is False


# ── 复验结论回写 ────────────────────────────────────────────────
class TestApplyVerdicts:
    """🔴 陈旧/歧义的 `verify` 结论只能靠**重新复验**覆盖。

    台账里会留下旧版本代码写的 `stages.verify` 文案，而报告会把
    `key_verified=None` 误读成“用户 `--no-verify`”。复验一次并回写，
    这些记录就变成了当下的确定结论 —— 本类是那个写回变换的钉子。
    """

    def test_updates_only_verify_fields(self):
        rec = {"email": "a@example-mail.test", "api_key": "opc_sk_x",
               "status": "success", "key_verified": None,
               "egress": "203.0.113.11",
               "stages": {"register": "ok", "verify": "陈旧文案"}}
        out, changed = ledger.apply_verdicts(
            [rec], {"opc_sk_x": (True, "ok(3 models)")},
            stamp="2026-09-27 10:00:00")
        assert changed == 1
        r = out[0]
        assert r["key_verified"]
        assert r["stages"]["verify"] == "ok(3 models)"
        assert r["stages"]["verify_refreshed_at"] == "2026-09-27 10:00:00"
        assert r["stages"]["register"] == "ok"      # 旧字段没丢
        assert r["egress"] == "203.0.113.11"          # 其它字段没动

    def test_does_not_mutate_input_records(self):
        """返回**新列表**：原列表可能同时是别的合并基准。"""
        rec = {"email": "a", "api_key": "opc_sk_x", "key_verified": None}
        out, _ = ledger.apply_verdicts([rec], {"opc_sk_x": (False, "401")})
        assert rec["key_verified"] is None
        assert out[0] is not rec

    def test_unmatched_records_kept_verbatim(self):
        rec = {"email": "a", "api_key": "opc_sk_other"}
        out, changed = ledger.apply_verdicts([rec], {"opc_sk_x": (True, "ok")})
        assert changed == 0
        assert out == [rec]

    def test_missing_api_key_untouched(self):
        """`api_key=""` 不该误命中 `verdicts` 里的空键。"""
        rec = {"email": "a", "api_key": ""}
        out, changed = ledger.apply_verdicts([rec], {"": (True, "ok")})
        assert changed == 0

    def test_false_verdict_overwrites_stale_success(self):
        """复验的目的就是**覆盖**旧结论 —— 不能因为旧值是 true 就手下留情。"""
        rec = {"api_key": "opc_sk_x", "key_verified": True,
               "stages": {"verify": "ok(old)"}}
        out, _ = ledger.apply_verdicts(
            [rec], {"opc_sk_x": (False, "401（key 无效）")})
        assert not out[0]["key_verified"]
        assert out[0]["stages"]["verify"] == "401（key 无效）"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
