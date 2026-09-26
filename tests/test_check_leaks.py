"""`tools/gates/check_leaks.py` 的离线测试。**不联网。**

闸门本身是"证明仓库干净"的工具，所以它自己更需要被证明 ——
一个**永远返回 0 条**的扫描器和"仓库真干净"在输出上完全一样。
参考项目里同类闸门就是这么静默失效的。

⚠ 本文件用 `importlib` 从路径加载闸门脚本（`tools/` 不是包）。
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GATE = ROOT / "tools" / "gates" / "check_leaks.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_leaks", GATE)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["check_leaks"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def gate():
    return _load()


# ── 清单来源 ──────────────────────────────────────────────────────
class TestTokenExtraction:
    def test_splits_comma_separated_mapping(self, gate):
        """🔴 必须**拆开**：整串 `7901=...` 去搜永远搜不到，
        因为源码里只会提到**其中一个** IP。

        ⚠ 用非保留段的值（`1.2.3.4` 是给测试用的假值，不进闸门脚本源码）；
          占位段 `203.0.113.x` 会被 `ALLOWED_VALUES` 放过，验证不了拆分。
        """
        env = {"FO_SLOT_EGRESS_IPS": "7901=1.2.3.4,7902=5.6.7.8"}
        assert gate._tokens_from_env(env) == {"1.2.3.4", "5.6.7.8"}

    def test_strips_scheme_and_port(self, gate):
        env = {"FO_WORKER_BASE": "https://abc.workers.dev:443/x"}
        assert gate._tokens_from_env(env) == {"abc.workers.dev"}

    def test_ignores_placeholders(self, gate):
        """占位值不该被当成"要拦的东西" —— 否则仓库里满屏假命中。"""
        env = {
            "FO_SLOT_EGRESS_IPS": "7901=203.0.113.11,7902=127.0.0.1",
            "FO_WORKER_DOMAIN": "example-mail.test",
        }
        assert gate._tokens_from_env(env) == set()

    def test_ignores_short_fragments(self, gate):
        """长度 < 6 的片段丢掉。

        ⚠ 取舍：这会**漏掉**极短的基础设施名（比如 4 字符的域名）。
          但短串带来的误报会把闸门变成噪音，而噪音闸门的下场是被 `|| true`
          绕过 —— 那比漏掉一个短名更糟。所以门槛留在 6。
        ⚠ `1.2.3.4` 是 **7** 字符，**不**触发这条门槛（它是要被拦的）。
        """
        assert gate._tokens_from_env({"FO_PROXY": "ab.cd"}) == set()
        assert gate._tokens_from_env({"FO_PROXY": "abc"}) == set()
        # 对照：7 字符的 IP 必须留下
        assert gate._tokens_from_env({"FO_PROXY": "1.2.3.4"}) == {"1.2.3.4"}

    def test_empty_env_gives_no_tokens(self, gate):
        assert gate._tokens_from_env({}) == set()

    def test_missing_var_tolerated(self, gate):
        assert gate._tokens_from_env({"FO_WORKER_BASE": ""}) == set()


class TestNoHardcodedIdentifiers:
    """🔴 闸门脚本**自己**不能含真实标识。

    把要保护的字符串硬编码进闸门，等于让闸门变成泄漏源 ——
    而它通常是最不会被 review 的那个文件。
    """

    def test_gate_source_has_no_ip_literals(self, gate):
        import re

        src = GATE.read_text(encoding="utf-8")
        # 匹配非保留段的公网 IPv4（排除 203.0.113.x / 198.51.100.x / 127.x / 0.0.0.0）
        ips = set(re.findall(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", src))
        bad = {
            ip for ip in ips if not ip.startswith(("203.0.113.", "198.51.100.", "127.", "0.0.0.0"))
        }
        assert not bad, f"闸门脚本里有真实 IP：{sorted(bad)} —— 应该从 .env 读"

    def test_gate_source_has_no_worker_subdomain(self, gate):
        src = GATE.read_text(encoding="utf-8")
        assert "workers.dev" not in src, "闸门不该硬编码 Worker 子域"


# ── 扫描行为 ──────────────────────────────────────────────────────
class TestScan:
    def test_finds_planted_token(self, gate, tmp_path, monkeypatch):
        monkeypatch.setattr(gate, "ROOT", tmp_path)
        monkeypatch.setattr(gate, "SCAN_DIRS", ("src",))
        (tmp_path / "src").mkdir()
        f = tmp_path / "src" / "leak.py"
        f.write_text('BASE = "https://secret-host.example"\n', encoding="utf-8")
        hits = gate.scan({"secret-host.example"})
        assert len(hits) == 1
        assert hits[0][0] == f and hits[0][1] == 1

    def test_skips_gitignored_dirs(self, gate, tmp_path, monkeypatch):
        """🔴 `.env` / `ledger/` / `.workbuddy-ai/` 里**本来就该有**真值。

        不排除它们，闸门会永远红 —— 然后被加 `|| true` 绕过，等于没有。
        """
        monkeypatch.setattr(gate, "ROOT", tmp_path)
        monkeypatch.setattr(gate, "SCAN_DIRS", ("src",))
        for d in ("ledger", ".workbuddy-ai", "__pycache__"):
            p = tmp_path / "src" / d
            p.mkdir(parents=True)
            (p / "x.py").write_text("secret-host.example\n", encoding="utf-8")
        assert gate.scan({"secret-host.example"}) == []

    def test_returns_empty_for_clean_tree(self, gate, tmp_path, monkeypatch):
        monkeypatch.setattr(gate, "ROOT", tmp_path)
        monkeypatch.setattr(gate, "SCAN_DIRS", ("src",))
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "ok.py").write_text("X = 1\n", encoding="utf-8")
        assert gate.scan({"secret-host.example"}) == []

    def test_reports_line_number_and_content(self, gate, tmp_path, monkeypatch):
        monkeypatch.setattr(gate, "ROOT", tmp_path)
        monkeypatch.setattr(gate, "SCAN_DIRS", ("src",))
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "a.py").write_text(
            "x = 1\ny = 2\nZ = 'secret-host.example'\n", encoding="utf-8"
        )
        hits = gate.scan({"secret-host.example"})
        assert hits[0][1] == 3
        assert "secret-host.example" in hits[0][3]

    def test_binary_file_does_not_crash(self, gate, tmp_path, monkeypatch):
        monkeypatch.setattr(gate, "ROOT", tmp_path)
        monkeypatch.setattr(gate, "SCAN_DIRS", ("src",))
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "b.py").write_bytes(b"\xff\xfe\x00secret-host.example")
        assert gate.scan({"secret-host.example"}) == [], "解码失败要跳过，不是崩"


# ── 自检（变异测试）───────────────────────────────────────────────
class TestSelftest:
    def test_selftest_passes(self, gate):
        """🔴 自检必须**真的能过**，否则闸门是坏的。

        它证明的是"扫描器抓得到种入的值" —— 一个永远返回 0 条的实现会在这里红。
        """
        assert gate.selftest() == 0

    def test_selftest_planted_value_is_reserved_range(self, gate):
        """种入的值必须用保留段，否则真跑到生产环境可能假命中。"""
        import inspect

        src = inspect.getsource(gate.selftest)
        assert "198.51.100." in src or "203.0.113." in src


# ── 退出码语义 ────────────────────────────────────────────────────
class TestExitCodes:
    def test_missing_env_is_error_not_clean(self, gate, tmp_path, monkeypatch):
        """🔴 读不到 `.env` 必须是 **2（闸门坏了）**，不是 0（干净）。

        否则"CI 上没配 .env"会被读成"仓库没有泄漏" —— 这是最危险的假绿。
        """
        monkeypatch.setattr(gate, "ENV_FILE", tmp_path / "nope.env")
        monkeypatch.setattr(sys, "argv", ["check_leaks.py"])
        assert gate.main() == 2

    def test_env_without_sensitive_vars_is_error(self, gate, tmp_path, monkeypatch):
        """`.env` 存在但一个敏感变量都没有 ⇒ 同样是闸门空转，不是干净。"""
        env = tmp_path / "e.env"
        env.write_text("FO_PASSWORD_LEN=12\n", encoding="utf-8")
        monkeypatch.setattr(gate, "ENV_FILE", env)
        monkeypatch.setattr(sys, "argv", ["check_leaks.py"])
        assert gate.main() == 2

    def test_hit_returns_1(self, gate, tmp_path, monkeypatch):
        env = tmp_path / "e.env"
        env.write_text("FO_WORKER_BASE=https://secret-host.example\n", encoding="utf-8")
        monkeypatch.setattr(gate, "ENV_FILE", env)
        monkeypatch.setattr(gate, "ROOT", tmp_path)
        monkeypatch.setattr(gate, "SCAN_DIRS", ("src",))
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "leak.py").write_text("X='secret-host.example'\n", encoding="utf-8")
        monkeypatch.setattr(sys, "argv", ["check_leaks.py", "--quiet"])
        assert gate.main() == 1

    def test_clean_returns_0(self, gate, tmp_path, monkeypatch):
        env = tmp_path / "e.env"
        env.write_text("FO_WORKER_BASE=https://secret-host.example\n", encoding="utf-8")
        monkeypatch.setattr(gate, "ENV_FILE", env)
        monkeypatch.setattr(gate, "ROOT", tmp_path)
        monkeypatch.setattr(gate, "SCAN_DIRS", ("src",))
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "ok.py").write_text("X = 1\n", encoding="utf-8")
        monkeypatch.setattr(sys, "argv", ["check_leaks.py", "--quiet"])
        assert gate.main() == 0

    def test_selftest_flag_short_circuits(self, gate, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["check_leaks.py", "--selftest"])
        assert gate.main() == 0


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
