"""`src.pipeline.stage_register` —— 服务端确认 IP 配额触顶时**必须记账**。

这是“本地配额是乐观的”那次的回归钉子：`quota.record()` 只在注册成功时
调用，而探测脚本绕过它 ⇒ 一个当天已被判死的出口，本地显示 `0/5`。
唯一能纠正它的是把**服务端确认的判死**落成 `quota.record_ban()`。

本模块只模拟到注册那一步 —— 一拿到 `ip_quota` 就返回，**不发任何真实请求**。
"""

from typing import cast

import pytest

from src import opc, pipeline, quota


class _FakeMail:
    """邮箱源桩：只需要建出一个地址，收信永远不会被调到。"""

    last_error = ""
    last_polls = 0
    last_http_errors = 0

    def create_mailbox(self, domain=None, count=1):
        return ["probe@example-mail.test"]

    def wait_for_mail(self, address, **kw):
        raise AssertionError("注册都没成功，不该去收信")


class _QuotaBlockedClient:
    """站点客户端桩：注册永远返回 IP 日配额 429。

    `cast` 是必要的：`stage_register` 的形参标注是 `OpcClient`，而这里刻意
    只实现它真正调用的两个方法（鸭子类型）。离线测试不该为了类型标注去构造
    一个带 `requests.Session` 的真客户端。
    """

    def gen_available_username(self):
        return "lz123456"

    def register(self, username, email, password, *, account_type=None, invite_code=""):
        return opc.RegisterResult(
            ok=False,
            http_status=429,
            limit_kind=opc.LIMIT_IP_QUOTA,
            error="该 IP 今日注册数量已达上限，请明日再试或联系管理员",
        )


def _client() -> opc.OpcClient:
    return cast(opc.OpcClient, _QuotaBlockedClient())


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("FO_QUOTA_STATE", str(tmp_path / "q.jsonl"))


def test_ip_quota_records_a_ban_on_the_scope():
    rec = pipeline.AccountRecord()
    ok = pipeline.stage_register(
        _FakeMail(), _client(), rec, quota_scope="203.0.113.11", invite_code="", log=lambda *a: None
    )

    assert ok is False
    assert rec.error_kind == pipeline.ERR_IP_QUOTA
    st = quota.status("203.0.113.11")
    assert st.banned, "服务端确认的判死没有落成本地硬证据"
    assert st.exhausted
    assert st.used == 0, "判死不占成功额度"


def test_ban_is_written_even_when_state_dir_is_new(tmp_path, monkeypatch):
    """状态目录不存在时 `_append` 会自己建目录 —— 判死不能因为没目录就丢。"""
    nested = tmp_path / "deep" / "nested" / "q.jsonl"
    monkeypatch.setenv("FO_QUOTA_STATE", str(nested))
    rec = pipeline.AccountRecord()
    pipeline.stage_register(
        _FakeMail(), _client(), rec, quota_scope="203.0.113.12", invite_code="", log=lambda *a: None
    )
    assert nested.is_file()
    assert quota.status("203.0.113.12").banned
