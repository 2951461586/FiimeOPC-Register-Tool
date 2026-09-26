"""`src.proxypool` 的状态文件键 —— 动态住宅下必须**每会话唯一**。

不这么做的后果：10 个住宅会话挤在同一个 `host:port` 上，按 `host:port` 当键
会让它们**共用**一条冷却记录 —— 一个会话触顶，整个住宅池一起被晾，
正好把“每个会话独立出口”的核心价值抹掉。

⚠ 本文件的代理/主机名一律用保留示例域，真实住宅网关是基础设施标识。
"""

import time as _time

# pytest 在 pyproject 的 dev 依赖里（真实解释器可解析）；这里加 ignore 只是因为
# pi-lens 的语言服务器用的是另一个不含 pytest 的解释器。
import pytest  # type: ignore[import-not-found]

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


def test_host_only_proxy_key_is_hashed(monkeypatch):
    """🔴 “子域即凭据”的代理，状态键绝不能包含那个子域。"""
    monkeypatch.setattr(proxypool.config, "SLOT_EGRESS_IPS", {})
    monkeypatch.setattr(proxypool.config, "PROXY_SCOPE_MODE", "session")
    k = proxypool._state_key("https://tokAAA.hub.example.com:7161")
    assert "tokAAA" not in k and "hub.example.com" not in k


def test_host_only_proxies_get_distinct_keys(monkeypatch):
    monkeypatch.setattr(proxypool.config, "SLOT_EGRESS_IPS", {})
    monkeypatch.setattr(proxypool.config, "PROXY_SCOPE_MODE", "session")
    a = proxypool._state_key("https://tokAAA.hub.example.com:7161")
    b = proxypool._state_key("https://tokBBB.hub.example.com:7161")
    assert a != b, "不同会话不能共用一条冷却记录"


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


# ── acquire 的快速失败（不白等 PROXY_SLOT_TIMEOUT）──────────────────
def test_acquire_fails_fast_when_all_free_slots_rejected(monkeypatch, tmp_path):
    """🔴 出口配额全满时必须**立刻**抛 `NoEligibleSlot`，不能白等 timeout。

    没有这一条，一个“无池可用”的批会被拖成 `ceil(count/并发) × 240s`
    （73 个账号 ≈ 52 分钟），而外层看到的就是“卡住”。
    """
    monkeypatch.setattr(proxypool.config, "SLOT_EGRESS_IPS", {})
    monkeypatch.setattr(proxypool.config, "PROXY_SCOPE_MODE", "session")
    slots = [
        "res.example.com:2000:u-region-US-sid-AAA111-t-1:pw",
        "res.example.com:2000:u-region-US-sid-BBB222-t-1:pw",
    ]
    pool = proxypool.ProxySlotPool(slots, log=lambda *a: None, state_file=tmp_path / "pool.json")
    t0 = _time.time()
    with pytest.raises(proxypool.NoEligibleSlot):
        pool.acquire(timeout=30.0, accept=lambda i: False)
    assert _time.time() - t0 < 5.0, "应当立刻失败，而不是等满 timeout"


def test_acquire_waits_when_slots_are_busy(monkeypatch, tmp_path):
    """所有槽位都在忙 ⇒ `TimeoutError`（等不到空闲），不是 `NoEligibleSlot`。"""
    monkeypatch.setattr(proxypool.config, "SLOT_EGRESS_IPS", {})
    monkeypatch.setattr(proxypool.config, "PROXY_SCOPE_MODE", "session")
    pool = proxypool.ProxySlotPool(
        ["res.example.com:2000:u-region-US-sid-AAA111-t-1:pw"],
        log=lambda *a: None,
        state_file=tmp_path / "pool.json",
    )
    lease = pool.acquire(timeout=1.0)
    try:
        with pytest.raises(TimeoutError):
            pool.acquire(timeout=0.3)
    finally:
        pool.release(lease)
