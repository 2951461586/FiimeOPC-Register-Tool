"""`src.quota` —— 按出口 IP 的本地配额计数。**不联网、不碰真实状态。**

🔴 这里钉的是本项目记录过的一次事故：**本地计数是乐观的**。
`record()` 只在注册成功时调用，而探测脚本绕过它 —— 于是一个当天已被
服务端判死的出口，本地显示 `0/5`（看起来额度最充足），`run.py probe`
也会照着这个数字报“余 5”。

修法：服务端确认的 IP 日配额 429 用 `record_ban()` 单记一笔**硬证据**，
`status()` 据此把 `exhausted`/`remaining` 直接置为“满 / 0”，不再被
乐观的成功计数盖过。

⚠ 本文件里的 IP 一律用 RFC 5737 保留段 —— 真实出口 IP 是基础设施标识，
   泄漏闸门（`tools/gates/check_leaks.py`）会拦。
"""

import json
import time

import pytest

from src import quota


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    """每个用例一份独立计数文件，且不读真实 `.env` 的配额参数。"""
    monkeypatch.setenv("FO_QUOTA_STATE", str(tmp_path / "q.jsonl"))
    monkeypatch.setenv("FO_IP_QUOTA_MAX", "5")
    monkeypatch.setenv("FO_IP_QUOTA_WINDOW_H", "24")


class TestStatusCounts:
    def test_counts_only_within_scope(self):
        quota.record("a", scope="203.0.113.11")
        quota.record("b", scope="203.0.113.11")
        quota.record("c", scope="203.0.113.12")
        assert quota.status("203.0.113.11").used == 2
        assert quota.status("203.0.113.12").used == 1

    def test_empty_scope_is_its_own_bucket(self):
        """无池/直连模式的 scope 是空串，不能和任何具名出口混算。"""
        quota.record("a", scope="")
        quota.record("b", scope="203.0.113.11")
        assert quota.status("").used == 1
        assert quota.status("203.0.113.11").used == 1

    def test_remaining_and_exhausted_at_limit(self):
        for i in range(5):
            quota.record(f"u{i}", scope="203.0.113.11")
        st = quota.status("203.0.113.11")
        assert st.used == 5
        assert st.remaining == 0
        assert st.exhausted


class TestBanEvidence:
    """🔴 服务端判死是硬证据，不能被乐观的本地计数盖过。"""

    def test_ban_exhausts_even_with_zero_used(self):
        scope = "203.0.113.11"
        assert quota.status(scope).used == 0
        assert not quota.status(scope).exhausted
        quota.record_ban(scope, "该 IP 今日注册数量已达上限")
        st = quota.status(scope)
        assert st.used == 0, "判死不该占成功额度（used 要如实反映成功数）"
        assert st.banned
        assert st.exhausted
        assert st.remaining == 0

    def test_ban_is_scoped(self):
        quota.record_ban("203.0.113.11", "quota")
        assert quota.status("203.0.113.11").exhausted
        assert not quota.status("203.0.113.12").exhausted

    def test_ban_reason_surfaces_in_describe(self):
        quota.record_ban("203.0.113.11", "该 IP 今日注册数量已达上限")
        d = quota.status("203.0.113.11").describe()
        assert "服务端已确认" in d
        assert "已达上限" in d

    def test_ban_outside_window_is_ignored(self, monkeypatch):
        quota.record_ban("203.0.113.11", "old")
        monkeypatch.setenv("FO_IP_QUOTA_WINDOW_H", "0")   # 窗口缩到 ~0h
        assert not quota.status("203.0.113.11").exhausted

    def test_old_records_without_kind_count_as_success(self):
        """🔴 向后兼容：早期版本只记 `{ts, scope, email}`，没有 `kind`。

        把这类老记录读成“未知”会把整份历史账清空；正确读法是**成功**。
        """
        p = quota.state_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"ts": int(time.time()), "scope": "203.0.113.11",
                                 "email": "a@example-mail.test"}) + "\n",
                     encoding="utf-8")
        st = quota.status("203.0.113.11")
        assert st.used == 1
        assert not st.banned


class TestGuard:
    def test_check_or_raise_blocks_when_banned(self):
        quota.record_ban("203.0.113.11", "该 IP 今日注册数量已达上限")
        with pytest.raises(quota.QuotaExceeded, match="服务端已确认"):
            quota.check_or_raise(planned=1, scope="203.0.113.11")

    def test_allow_partial_returns_status(self):
        quota.record("a", scope="203.0.113.11")
        st = quota.check_or_raise(planned=3, scope="203.0.113.11",
                                  allow_partial=True)
        assert st.remaining == 4

    def test_hint_reads_ignore_flag(self):
        """`--ignore-quota` 打开时**不能**再打“这一批会全部被跳过”的假话。"""
        assert quota.shortfall_hint(0, 3, True) == ""
        assert "总余量" in quota.shortfall_hint(0, 3, False)


class TestBadConfigFallsBack:
    """🔴 坏配置不该让整条链在启动时崩 —— 它只是个本地保守估计。"""

    def test_non_numeric_limit_falls_back(self, monkeypatch):
        monkeypatch.setenv("FO_IP_QUOTA_MAX", "五")
        assert quota.limit() == 5

    def test_non_numeric_window_falls_back(self, monkeypatch):
        monkeypatch.setenv("FO_IP_QUOTA_WINDOW_H", "一天")
        assert quota.window_hours() == 24


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
