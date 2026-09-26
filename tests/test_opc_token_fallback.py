"""激活链接里 token 的提取 —— 含站点漏写 `=` 的兜底。**不联网。**

🔴 实测（2026-09-26 一批 10 个账号）：**2 个**的激活邮件里链接是
   `https://opc.fiime.cn/activate?token<hex>`，少了 `=`。邮件原文确认如此
   （不是我们截断的）。这个兜底避免站点模板缺陷白掉已注册账号。
"""

from src import opc


def test_standard_token_parsed():
    tok = "a" * 64
    assert (
        opc.OpcClient.token_from_activation_url("https://opc.fiime.cn/activate?token=" + tok) == tok
    )


def test_missing_equals_is_recovered():
    tok = "b52a010465b0" * 2 + "deadbeef"  # 32 hex
    assert (
        opc.OpcClient.token_from_activation_url("https://opc.fiime.cn/activate?token" + tok) == tok
    )


def test_missing_equals_full_length():
    tok = "0123456789abcdef" * 4  # 64 hex
    assert (
        opc.OpcClient.token_from_activation_url("https://opc.fiime.cn/activate?token" + tok) == tok
    )


def test_no_token_returns_empty():
    assert opc.OpcClient.token_from_activation_url("https://opc.fiime.cn/activate") == ""
    assert opc.OpcClient.token_from_activation_url("https://opc.fiime.cn/activate?foo=bar") == ""


def test_short_hex_is_not_a_token():
    """< 32 位 hex 不认 —— 否则会把别的东西当 token。"""
    assert (
        opc.OpcClient.token_from_activation_url("https://opc.fiime.cn/activate?tokenabc123") == ""
    )
