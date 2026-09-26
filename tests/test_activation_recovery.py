"""激活自愈（有界重试 + 取最新邮件）+ 恢复工具的选取逻辑。**不联网。**

🔴 背景：站点偶发把激活邮件里的链接发坏（实测 2/10：既漏 `=`，token 还被截成
   62 位）。直接激活必然 400，白掉一个**已注册成功**的账号。
   自愈 = `POST /auth/resend-activation` 重发 + 用**最新**邮件重试（有界）。
"""

import importlib.util
from pathlib import Path

import pytest

from src import opc, pipeline

# 运维脚本在 tools/ops/ 下（不是包），用 importlib 按路径加载 —— 静态 import
# 解析不了 `sys.path` 注入，而脚本本身靠它把仓库根加进 sys.path。
_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "recover_activation", _ROOT / "tools" / "ops" / "recover_activation.py"
)
assert _spec is not None and _spec.loader is not None
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
_candidates = _mod._candidates
_newest_link = _mod._newest_link

GOOD = "https://opc.fiime.cn/activate?token=" + "a" * 64
BAD = "https://opc.fiime.cn/activate?token" + "b" * 62  # 漏 `=` 且只剩 62 位（站点缺陷）


@pytest.fixture(autouse=True)
def _fast_mail_poll(monkeypatch):
    """把收信轮询压到毫秒级 —— 否则"没邮件"的分支要真等 120s。"""
    monkeypatch.setattr(pipeline.config, "MAIL_POLL_TIMEOUT", 0.05)
    monkeypatch.setattr(pipeline.config, "MAIL_POLL_INTERVAL", 0.005)


class _Msg:
    def __init__(self, body: str, received_at: int = 2000):
        self.body = body
        self.received_at = received_at


class _FakeMail:
    """每次 `list_mails()` 返回同一批；`since_ts` 过滤保证已用过的不会重复命中。"""

    def __init__(self, mails=None):
        self._mails = list(mails or [])
        self.calls: list = []

    def list_mails(self, email):
        self.calls.append(email)
        return list(self._mails)


class _FakeClient:
    def __init__(self, activate_results, *, resend_ok=True):
        self._acts = list(activate_results)
        self._resend_ok = resend_ok
        self.activated: list = []
        self.resend_calls: list = []

    def activate(self, token):
        self.activated.append(token)
        return self._acts.pop(0)

    def resend_activation(self, email):
        self.resend_calls.append(email)
        return opc.ResendResult(
            ok=self._resend_ok,
            http_status=200 if self._resend_ok else 400,
            error="" if self._resend_ok else "boom",
        )


def _run(mail, client, link, since_ts=1):
    return pipeline.activate_with_resend(
        mail, client, "a@x.com", link, since_ts=since_ts, log=lambda *a: None
    )


# ── 自愈 ──────────────────────────────────────────────────────────
def test_good_link_activates_without_resend():
    client = _FakeClient([opc.ActivateResult(ok=True, http_status=200)])
    act, resent = _run(_FakeMail(), client, GOOD)
    assert act.ok is True
    assert resent is False
    assert client.resend_calls == []
    assert client.activated == ["a" * 64]


def test_bad_link_triggers_resend_and_retry():
    client = _FakeClient(
        [
            opc.ActivateResult(ok=False, http_status=400, error="激活链接无效或已使用"),
            opc.ActivateResult(ok=True, http_status=200),
        ]
    )
    mail = _FakeMail([_Msg(GOOD, received_at=2500)])
    act, resent = _run(mail, client, BAD, since_ts=1001)
    assert act.ok is True
    assert resent is True
    assert client.resend_calls == ["a@x.com"]
    assert client.activated == ["b" * 62, "a" * 64], "先试坏的，再用重发的新 token"
    assert mail.calls[0] == "a@x.com"


def test_uses_newest_mail_not_first():
    """迟到的旧邮件（token 已作废）在前、新邮件在后 —— 必须用**最新**那封。"""
    client = _FakeClient(
        [
            opc.ActivateResult(ok=False, http_status=400, error="激活链接无效或已使用"),
            opc.ActivateResult(ok=True, http_status=200),
        ]
    )
    old = _Msg("https://opc.fiime.cn/activate?token=" + "c" * 64, received_at=1500)
    new = _Msg(GOOD, received_at=2500)
    act, resent = _run(_FakeMail([old, new]), client, BAD, since_ts=1001)
    assert act.ok is True
    assert resent is True
    assert client.activated == ["b" * 62, "a" * 64], "用了旧邮件，不是最新的"


def test_network_failure_does_not_resend():
    """网络类失败（非 400）重发没用 —— 不能浪费一次重发。"""
    client = _FakeClient([opc.ActivateResult(ok=False, http_status=0, error="timeout")])
    act, resent = _run(_FakeMail(), client, GOOD)
    assert act.ok is False
    assert resent is False
    assert client.resend_calls == []


def test_no_link_goes_straight_to_resend():
    client = _FakeClient([opc.ActivateResult(ok=True, http_status=200)])
    act, resent = _run(_FakeMail([_Msg(GOOD, received_at=3000)]), client, None)
    assert act.ok is True
    assert resent is True
    assert client.resend_calls == ["a@x.com"]


def test_resend_rejected_returns_original_failure():
    client = _FakeClient(
        [opc.ActivateResult(ok=False, http_status=400, error="invalid")], resend_ok=False
    )
    act, resent = _run(_FakeMail([_Msg(GOOD)]), client, BAD)
    assert act.ok is False
    assert resent is False
    assert act.error == "invalid"
    assert len(client.resend_calls) == 1, "重发被拒就停，不再重试"


def test_bounded_retry_when_no_mail():
    """一次重发没等到邮件 → 再重发一次（有界），最终仍失败。"""
    client = _FakeClient([opc.ActivateResult(ok=False, http_status=400, error="invalid")])
    act, resent = _run(_FakeMail([]), client, BAD)
    assert act.ok is False
    assert resent is True
    assert len(client.resend_calls) == pipeline.RESEND_ATTEMPTS, "没做有界重试"
    assert "未收到可用新邮件" in act.error


def test_new_mail_without_token_keeps_retrying_then_fails():
    client = _FakeClient([opc.ActivateResult(ok=False, http_status=400, error="invalid")])
    mail = _FakeMail([_Msg("https://opc.fiime.cn/activate", received_at=2500)])
    act, resent = _run(mail, client, BAD)
    assert act.ok is False
    assert resent is True
    assert "未收到可用新邮件" in act.error


# ── 恢复工具：选取 + 取最新链接 ───────────────────────────────────
def test_candidates_selects_registered_but_not_activated():
    recs = [
        {"email": "a", "password": "p", "status": "failed", "stages": {"register": "ok"}},
        {
            "email": "b",
            "password": "p",
            "status": "success",
            "stages": {"register": "ok", "activate": "ok"},
        },
        {"email": "c", "password": "p", "status": "failed", "stages": {"register": "failed"}},
        {
            "email": "d",
            "password": "p",
            "status": "failed",
            "stages": {"register": "ok", "activate": "ok"},
        },
        {"email": "e", "status": "failed", "stages": {"register": "ok"}},  # 无密码
    ]
    assert {r["email"] for r in _candidates(recs)} == {"a"}


def test_newest_link_picks_latest_mail_with_link():
    old = _Msg(BAD, received_at=1000)
    new = _Msg(GOOD, received_at=2000)
    link, newest = _newest_link([old, new])
    assert newest == 2000
    assert link is not None and link.endswith("a" * 64)
