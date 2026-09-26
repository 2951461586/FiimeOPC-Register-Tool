"""`src.redact` —— 日志 / 输出边界的脱敏。**不联网、不落盘。**

🔴 住宅代理有两种「凭据藏在哪」的形态：
   · `scheme://user:pass@host:port`          —— 凭据在 userinfo
   · `scheme://<token>.provider.tld:port`     —— **子域 token 本身就是凭据**
只抹 userinfo 会把后者的 token 漏进 stdout / 日志 / 状态文件。
"""

from src.redact import redact_proxy, redact_url


def test_redact_url_masks_userinfo():
    assert redact_url("http://user:pass@h.example.com:2000") == "http://***@h.example.com:2000"


def test_redact_url_leaves_ip_untouched():
    assert redact_url("http://127.0.0.1:7901") == "http://127.0.0.1:7901"


def test_redact_proxy_masks_token_subdomain():
    out = redact_proxy("https://tok123.hub.example.com:7161")
    assert "tok123" not in out
    assert out == "https://***.hub.example.com:7161"


def test_redact_proxy_masks_userinfo_form():
    assert redact_proxy("http://user:pass@us.example.com:2000") == "http://***@us.example.com:2000"


def test_redact_proxy_leaves_ip_port_untouched():
    assert redact_proxy("http://127.0.0.1:7901") == "http://127.0.0.1:7901"


def test_redact_proxy_two_label_host_untouched():
    """只有两段域且无 userinfo —— 不像「token 子域」，不动它。"""
    assert redact_proxy("http://example.com:2000") == "http://example.com:2000"
