"""`src.proxypool` 的状态文件键 —— 动态住宅下必须**每会话唯一**。

不这么做的后果：10 个住宅会话挤在同一个 `host:port` 上，按 `host:port` 当键
会让它们**共用**一条冷却记录 —— 一个会话触顶，整个住宅池一起被晾，
正好把“每个会话独立出口”的核心价值抹掉。

⚠ 本文件的代理/主机名一律用保留示例域，真实住宅网关是基础设施标识。
"""

from src import proxypool


def test_session_mode_keys_are_unique_per_session(monkeypatch):
    monkeypatch.setattr(proxypool.config, "SLOT_EGRESS_IPS", {})
    monkeypatch.setattr(proxypool.config, "PROXY_SCOPE_MODE", "session")
    a = proxypool._state_key("res.example.com:2000:u-region-US-sid-AAA111-t-1:pw")
    b = proxypool._state_key("res.example.com:2000:u-region-US-sid-BBB222-t-1:pw")
    assert a != b, "同一 host:port 的两个会话必须各有各的冷却记录"


def test_state_key_never_contains_credentials(monkeypatch):
    monkeypatch.setattr(proxypool.config, "SLOT_EGRESS_IPS", {})
    monkeypatch.setattr(proxypool.config, "PROXY_SCOPE_MODE", "session")
    key = proxypool._state_key("res.example.com:2000:u-region-US-sid-AAA111-t-1:SUPERSECRET")
    assert "SUPERSECRET" not in key, "状态文件经常被贴出来排查，绝不能带密码"


def test_egress_mode_key_stays_host_port(monkeypatch):
    """`egress` 模式保持原键格式 —— 不打扰 mihomo 的既有状态。"""
    monkeypatch.setattr(proxypool.config, "PROXY_SCOPE_MODE", "egress")
    monkeypatch.setattr(proxypool.config, "SLOT_EGRESS_IPS", {"7901": "203.0.113.7"})
    assert proxypool._state_key("http://127.0.0.1:7901") == "127.0.0.1:7901"


def test_pool_egress_uses_session_scope(monkeypatch, tmp_path):
    """建池时 `session` 模式必须把每个会话当成独立出口（distinct=2）。"""
    monkeypatch.setattr(proxypool.config, "SLOT_EGRESS_IPS", {})
    monkeypatch.setattr(proxypool.config, "PROXY_SCOPE_MODE", "session")
    slots = [
        "res.example.com:2000:u-region-US-sid-AAA111-t-1:pw",
        "res.example.com:2000:u-region-US-sid-BBB222-t-1:pw",
    ]
    pool = proxypool.ProxySlotPool(slots, log=lambda *a: None, state_file=tmp_path / "pool.json")
    assert pool.distinct_egress() == 2, "两个会话不能算成一个出口"
