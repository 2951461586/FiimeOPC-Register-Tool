"""`src.config` 的配置项与**文档/代码交叉引用**的漂移门禁。**不联网。**

为什么值得单独一个测试文件
==========================
这个项目里最贵的失败不是"报错"，是**不报错的漂移**：

  * `.env.example` 少一个变量 ⇒ 新机器照抄模板跑，功能静默按默认值走；
  * 注释里引用一个**不存在的脚本** ⇒ 出事时按提示去跑，报"文件不存在"，
    而那时人正在排障，最容易被误导。

参考项目里的原话是「"重复"不等于"等价"，先证明再合并」——
同理，**"文档提到过"不等于"真的存在"**，所以要机器来证。
"""

import re
from pathlib import Path

# pytest 在 pyproject 的 dev 依赖里（真实解释器可解析）；这里加 ignore 只是因为
# pi-lens 的语言服务器用的是另一个不含 pytest 的解释器。
import pytest  # type: ignore[import-not-found]

from src import config, quota

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PY = ROOT / "src" / "config.py"
ENV_EXAMPLE = ROOT / ".env.example"
SRC_DIR = ROOT / "src"
TOOLS_DIR = ROOT / "tools"

# 🔴 `.env.example` 里**故意**保留、但本项目代码不读的变量。
#    每一条都要写清为什么留着 —— 这份白名单是"允许存在"的唯一入口，
#    加东西必须在这里说明理由，防止它变成一个随便塞的垃圾桶。
ENV_DOC_ONLY = {
    # mihomo 可执行文件路径。本仓库**不启动** mihomo（槽位池是"已经在跑的
    # 监听端口"，起停由使用者自己的命令负责），所以代码不读它。
    # 留在模板里是因为使用者的启动命令会引用它，写在一起便于照抄。
    "FO_MIHOMO_EXE",
}

# 形如 `os.getenv("FO_XXX"` / `os.getenv("FO_XXX", "1")`
_GETENV_RE = re.compile(r'os\.getenv\(\s*"(FO_[A-Z0-9_]+)"')
# `.env.example` 里的赋值行（含被注释掉的 `# FO_X=`）
_ENV_LINE_RE = re.compile(r"^\s*#?\s*(FO_[A-Z0-9_]+)\s*=", re.M)
# 源码/文档里提到的 `tools/...py` 路径
_TOOL_REF_RE = re.compile(r"tools/[A-Za-z0-9_./-]+\.py")


def _env_names_read_by_config() -> set:
    return set(_GETENV_RE.findall(CONFIG_PY.read_text(encoding="utf-8")))


def _env_names_documented() -> set:
    return set(_ENV_LINE_RE.findall(ENV_EXAMPLE.read_text(encoding="utf-8")))


# ── 配置项覆盖 ────────────────────────────────────────────────────
class TestEnvExampleCoverage:
    def test_every_env_var_is_documented(self):
        """🔴 `config.py` 读的每个 `FO_*` 都必须在 `.env.example` 里出现。

        漏一个的后果是**静默的**：新机器照抄模板，那个功能按代码默认值走，
        而没人知道有个开关存在。反方向（文档里有、代码没读）则更糟 ——
        用户以为调了有用。
        """
        read = _env_names_read_by_config()
        documented = _env_names_documented()
        missing = sorted(read - documented)
        assert not missing, (
            f"这些变量 config.py 会读，但 .env.example 里没有：{missing}\n"
            f"  → 补进 .env.example，并写清默认值与实测依据"
        )

    def test_no_phantom_env_vars_documented(self):
        """反方向：`.env.example` 里不该有代码从不读的 `FO_*`。

        ⚠ 例外见模块顶部的 `ENV_DOC_ONLY` —— 每条都要写清为什么留着。
        ⚠ 扫的是**整个 src/**，不是只扫 config.py：`FO_IP_QUOTA_MAX` 就住在
          `quota.py`，只扫 config 会把它误判成幽灵变量。
        """
        documented = _env_names_documented()
        read_anywhere = set()
        for f in list(SRC_DIR.rglob("*.py")) + [ROOT / "run.py"]:
            read_anywhere |= set(_GETENV_RE.findall(f.read_text(encoding="utf-8")))
        # tools/ 下的自检脚本也读环境变量（隔离状态文件用）
        for f in TOOLS_DIR.rglob("*.py"):
            read_anywhere |= set(_GETENV_RE.findall(f.read_text(encoding="utf-8")))

        phantom = sorted(documented - read_anywhere - ENV_DOC_ONLY)
        assert not phantom, (
            f"这些变量在 .env.example 里，但没有任何代码读它们：{phantom}\n"
            f"  → 要么是拼写错了，要么该删。留着会让用户以为调了有用。\n"
            f"  → 若确实只是给人看的（不进代码），加进 ENV_DOC_ONLY 并写明理由"
        )

    def test_env_doc_only_entries_are_actually_documented(self):
        """白名单自己也会腐烂 —— 被删掉的变量不该留在白名单里。"""
        stale = ENV_DOC_ONLY - _env_names_documented()
        assert not stale, f"ENV_DOC_ONLY 里有已不在模板中的变量：{sorted(stale)}"

    def test_defaults_in_example_match_code(self):
        """🔴 模板里的**具体取值**必须与代码默认值一致。

        最危险的一类：模板写 `FO_REG_MIN_INTERVAL=10`，代码默认 62。
        照抄模板的人会撞上服务端 60s 限流，而"默认值是安全的"这个假设
        在模板面前失效了。
        """
        text = ENV_EXAMPLE.read_text(encoding="utf-8")
        pairs = {
            "FO_REG_MIN_INTERVAL": config.REG_MIN_INTERVAL,
            "FO_PASSWORD_LEN": config.PASSWORD_LEN,
            "FO_REQUEST_TIMEOUT": config.REQUEST_TIMEOUT,
            "FO_MAIL_POLL_TIMEOUT": config.MAIL_POLL_TIMEOUT,
            "FO_REG_RATE_BACKOFF": config.REG_RATE_BACKOFF,
            # ⚠ 配额上限住在 quota.py，不在 config.py
            "FO_IP_QUOTA_MAX": quota.limit(),
        }
        for name, val in pairs.items():
            m = re.search(rf"^\s*{name}=([^\s#]*)", text, re.M)
            assert m, f"{name} 在 .env.example 里没有**生效的**赋值行（只有注释？）"
            shown = m.group(1).strip()
            # 数字按数值比，避免 "62" vs "62.0" 这种假红
            assert float(shown) == float(val), (
                f"{name}：模板写 {shown}，代码默认 {val} —— 模板必须等于默认值"
            )

    def test_string_defaults_in_example_match_code(self):
        """字符串类默认值也要一致（数值那条用 float 比，管不了它们）。

        实测踩到：模板里 UA 写 Chrome/131，代码默认 Chrome/152 ——
        `FO_USER_AGENT` 又是"照抄模板"的典型（模板里有赋值行，看着像必填）。
        """
        text = ENV_EXAMPLE.read_text(encoding="utf-8")
        for name, val in {"FO_USER_AGENT": config.USER_AGENT}.items():
            m = re.search(rf"^\s*{name}=(.+)$", text, re.M)
            assert m, f"{name} 在 .env.example 里没有赋值行"
            assert m.group(1).strip() == val, (
                f"{name}：模板写 {m.group(1).strip()!r}，代码默认 {val!r}"
            )


# ── 交叉引用完整性 ────────────────────────────────────────────────
class TestToolReferencesExist:
    """🔴 注释里引用的脚本必须真的存在。

    本项目实际踩过：`slot_scope()` 的报错文案让用户跑
    `tools/data/migrate_quota_scope.py --apply`，而那个文件从来不存在，
    而且**那个工具本来就不该存在**（配额按出口 IP 记，换端口不需要迁移）。
    人是在排障时看到这句话的 —— 这是最不该给错信息的位置。
    """

    def _refs(self):
        out = {}
        files = (
            list(SRC_DIR.rglob("*.py"))
            + [ROOT / "run.py"]
            + list(TOOLS_DIR.rglob("*.py"))
            + [ROOT / "README.md", ROOT / "docs" / "protocol.md"]
        )
        for f in files:
            if not f.is_file():
                continue
            for m in _TOOL_REF_RE.finditer(f.read_text(encoding="utf-8")):
                out.setdefault(m.group(0), set()).add(f.name)
        return out

    def test_all_referenced_tools_exist(self):
        refs = self._refs()
        assert refs, "一个 tools/ 引用都没扫到 —— 正则或路径假设错了"
        broken = {r: sorted(w) for r, w in refs.items() if not (ROOT / r).is_file()}
        assert not broken, "这些被引用的脚本不存在：\n" + "\n".join(
            f"  {r}  ← {', '.join(w)}" for r, w in sorted(broken.items())
        )

    def test_probe_scripts_are_referenced_somewhere(self):
        """反向：`tools/` 下的脚本不该是孤儿（没人知道它存在就等于没有）。"""
        all_text = "\n".join(
            f.read_text(encoding="utf-8")
            for f in [ROOT / "README.md", ROOT / "docs" / "protocol.md", *SRC_DIR.rglob("*.py")]
        )
        orphans = []
        for f in TOOLS_DIR.rglob("*.py"):
            rel = f.relative_to(ROOT).as_posix()
            if rel not in all_text and f.name not in all_text:
                orphans.append(rel)
        assert not orphans, f"这些脚本没有任何地方引用：{orphans}"


# ── validate() 的行为 ─────────────────────────────────────────────
class TestValidate:
    def test_missing_worker_token_is_reported(self, monkeypatch):
        monkeypatch.setattr(config, "WORKER_ADMIN_TOKEN", "")
        monkeypatch.setattr(config, "MAILBOX_KIND", "worker")
        assert "FO_WORKER_ADMIN_TOKEN" in config.validate()

    def test_need_worker_token_false_skips_token_check(self, monkeypatch):
        """`check_keys_alive.py` 这类只读工具不需要 Worker token。"""
        monkeypatch.setattr(config, "WORKER_ADMIN_TOKEN", "")
        monkeypatch.setattr(config, "MAILBOX_KIND", "worker")
        assert "FO_WORKER_ADMIN_TOKEN" not in config.validate(need_worker_token=False)

    def test_unknown_mailbox_kind_is_reported(self, monkeypatch):
        monkeypatch.setattr(config, "MAILBOX_KIND", "cf")
        missing = config.validate()
        assert any("FO_MAILBOX_KIND" in m for m in missing), "刻意不认别名 cf —— 必须报出来"

    def test_slots_without_egress_map_is_reported(self, monkeypatch):
        """🔴 配了槽位池却没有「端口 → 出口 IP」映射，必须在**启动时**报。

        否则 `slot_scope()` 会在**第一个任务**才抛错，那时已经跑了一半。
        """
        monkeypatch.setattr(config, "MAILBOX_KIND", "worker")
        monkeypatch.setattr(config, "WORKER_ADMIN_TOKEN", "x")
        monkeypatch.setattr(config, "IR_PROXY_SLOTS", "http://127.0.0.1:7901")
        monkeypatch.setattr(config, "IR_PROXY_SLOTS_FILE", "")
        monkeypatch.setattr(config, "SLOT_EGRESS_IPS", {})
        monkeypatch.setattr(config, "PROXY_SCOPE_MODE", "egress")
        missing = config.validate()
        assert any("FO_SLOT_EGRESS_IPS" in m for m in missing)

    def test_no_slots_no_egress_requirement(self, monkeypatch):
        """没配池子就不该要求映射表（直连模式是合法的）。"""
        monkeypatch.setattr(config, "MAILBOX_KIND", "worker")
        monkeypatch.setattr(config, "WORKER_ADMIN_TOKEN", "x")
        # 不能依赖本地 `.env`（CI / 新克隆都没有）—— 显式满足 Worker 必需项。
        monkeypatch.setattr(config, "WORKER_BASE", "https://worker.example")
        monkeypatch.setattr(config, "WORKER_DOMAIN", "example-mail.test")
        monkeypatch.setattr(config, "IR_PROXY_SLOTS", "")
        monkeypatch.setattr(config, "IR_PROXY_SLOTS_FILE", "")
        monkeypatch.setattr(config, "SLOT_EGRESS_IPS", {})
        assert config.validate() == []

    def test_session_mode_does_not_require_egress_map(self, monkeypatch):
        """🔴 动态住宅按**会话**记账，不需要端口→IP 映射表。"""
        monkeypatch.setattr(config, "MAILBOX_KIND", "worker")
        monkeypatch.setattr(config, "WORKER_ADMIN_TOKEN", "x")
        monkeypatch.setattr(config, "IR_PROXY_SLOTS", "res.example.com:2000:u:p")
        monkeypatch.setattr(config, "IR_PROXY_SLOTS_FILE", "")
        monkeypatch.setattr(config, "SLOT_EGRESS_IPS", {})
        monkeypatch.setattr(config, "PROXY_SCOPE_MODE", "session")
        assert not any("FO_SLOT_EGRESS_IPS" in m for m in config.validate())

    def test_unknown_scope_mode_is_reported(self, monkeypatch):
        monkeypatch.setattr(config, "MAILBOX_KIND", "worker")
        monkeypatch.setattr(config, "WORKER_ADMIN_TOKEN", "x")
        monkeypatch.setattr(config, "IR_PROXY_SLOTS", "")
        monkeypatch.setattr(config, "IR_PROXY_SLOTS_FILE", "")
        monkeypatch.setattr(config, "PROXY_SCOPE_MODE", "bogus")
        assert any("FO_PROXY_SCOPE_MODE" in m for m in config.validate())

    def test_unknown_proxy_scheme_is_reported(self, monkeypatch):
        monkeypatch.setattr(config, "MAILBOX_KIND", "worker")
        monkeypatch.setattr(config, "WORKER_ADMIN_TOKEN", "x")
        monkeypatch.setattr(config, "IR_PROXY_SLOTS", "")
        monkeypatch.setattr(config, "IR_PROXY_SLOTS_FILE", "")
        monkeypatch.setattr(config, "PROXY_SCHEME", "ftp")
        assert any("FO_PROXY_SCHEME" in m for m in config.validate())


# ── slot_scope / proxy_slots ──────────────────────────────────────
class TestSlotScope:
    def test_maps_port_to_egress_ip(self, monkeypatch):
        monkeypatch.setattr(config, "SLOT_EGRESS_IPS", {"7901": "203.0.113.7"})
        assert config.slot_scope("http://127.0.0.1:7901") == "203.0.113.7"

    def test_raises_loudly_on_unknown_port(self, monkeypatch):
        """🔴 必须**大声报错**，不能退回位置号。

        退回位置号的后果是**静默错配**：清单一改，既有配额记录就整体算到
        别的 IP 头上，于是某个 IP 偷偷超限、另一个被提前停掉，全程不报错。
        """
        monkeypatch.setattr(config, "SLOT_EGRESS_IPS", {"7901": "203.0.113.7"})
        monkeypatch.setattr(config, "PROXY_SCOPE_MODE", "egress")
        with pytest.raises(ValueError) as ei:
            config.slot_scope("http://127.0.0.1:7999")
        msg = str(ei.value)
        assert "7999" in msg
        assert "probe_slots.py" in msg, "报错要给出下一步怎么做"
        assert "FO_SLOT_EGRESS_IPS" in msg, "要给出确切的变量名"

    def test_error_does_not_suggest_nonexistent_migration_tool(self, monkeypatch):
        """回归钉子：报错文案**不能**再引用 `migrate_quota_scope.py`。

        那个工具从来不存在，而且**不该存在** —— 配额按出口 IP 记，
        端口换 IP 时旧记录跟着旧 IP 走，不需要迁移。
        """
        monkeypatch.setattr(config, "SLOT_EGRESS_IPS", {})
        monkeypatch.setattr(config, "PROXY_SCOPE_MODE", "egress")
        with pytest.raises(ValueError) as ei:
            config.slot_scope("http://127.0.0.1:7999")
        assert "migrate_quota" not in str(ei.value)


class TestNormalizeProxy:
    """代理串规范化 —— 住宅代理写的是 `host:port:user:pass`。"""

    def test_four_part_becomes_url(self, monkeypatch):
        monkeypatch.setattr(config, "PROXY_SCHEME", "http")
        raw = "res.example.com:2000:u-region-US-sid-AAA111-t-1:pw"
        assert config.normalize_proxy(raw) == (
            "http://u-region-US-sid-AAA111-t-1:pw@res.example.com:2000"
        )

    def test_scheme_url_passthrough(self):
        u = "http://127.0.0.1:7901"
        assert config.normalize_proxy(u) == u

    def test_two_part_gets_scheme(self, monkeypatch):
        monkeypatch.setattr(config, "PROXY_SCHEME", "http")
        assert config.normalize_proxy("1.2.3.4:8080") == "http://1.2.3.4:8080"

    def test_scheme_comes_from_config(self, monkeypatch):
        """scheme-less 的协议由 `FO_PROXY_SCHEME` 决定（住宅 SOCKS5）。"""
        monkeypatch.setattr(config, "PROXY_SCHEME", "socks5")
        assert config.normalize_proxy("res.example.com:2000:u:p") == (
            "socks5://u:p@res.example.com:2000"
        )

    def test_explicit_scheme_wins(self, monkeypatch):
        """已经带 `://` 的串不再套 `FO_PROXY_SCHEME`。"""
        monkeypatch.setattr(config, "PROXY_SCHEME", "socks5")
        assert config.normalize_proxy("http://127.0.0.1:7901") == "http://127.0.0.1:7901"

    def test_empty_stays_empty(self):
        assert config.normalize_proxy("") == ""

    def test_garbage_raises(self):
        with pytest.raises(ValueError):
            config.normalize_proxy("a:b:c")


class TestSessionScope:
    """🔴 动态住宅：同一 host:port 下每个 `sid-` 是**独立出口**。

    按端口记账会把 10 个会话合成一个桶 —— 10 路并发被本地闸门串成 1 路，
    且一个会话触顶会误判成全部触顶。所以 `session` 模式的 scope 取会话标识。
    """

    def test_same_host_port_sessions_get_distinct_scopes(self, monkeypatch):
        monkeypatch.setattr(config, "SLOT_EGRESS_IPS", {})
        monkeypatch.setattr(config, "PROXY_SCOPE_MODE", "session")
        a = config.slot_scope("res.example.com:2000:u-region-US-sid-AAA111-t-1:pw")
        b = config.slot_scope("res.example.com:2000:u-region-US-sid-BBB222-t-1:pw")
        assert a != b, "同 host:port 的两个会话必须分到两个桶"
        assert "sid-AAA111" in a and "sid-BBB222" in b

    def test_scope_never_contains_password(self, monkeypatch):
        monkeypatch.setattr(config, "SLOT_EGRESS_IPS", {})
        monkeypatch.setattr(config, "PROXY_SCOPE_MODE", "session")
        scope = config.slot_scope("res.example.com:2000:u-region-US-sid-AAA111-t-1:SUPERSECRET")
        assert "SUPERSECRET" not in scope, "scope 会落盘，绝不能带密码"

    def test_without_sid_uses_stable_hash(self, monkeypatch):
        monkeypatch.setattr(config, "SLOT_EGRESS_IPS", {})
        monkeypatch.setattr(config, "PROXY_SCOPE_MODE", "session")
        u = "res.example.com:2000:plainuser:pw"
        s1, s2 = config.slot_scope(u), config.slot_scope(u)
        assert s1 == s2, "scope 必须稳定（否则每次记到新桶）"
        assert "pw" not in s1

    def test_host_only_proxy_hashes_the_token(self, monkeypatch):
        """🔴 无 userinfo 时子域 token **就是凭据** —— scope 必须哈希掉它。"""
        monkeypatch.setattr(config, "SLOT_EGRESS_IPS", {})
        monkeypatch.setattr(config, "PROXY_SCOPE_MODE", "session")
        a = "https://tokAAA.hub.example.com:7161"
        b = "https://tokBBB.hub.example.com:7161"
        sa, sb = config.slot_scope(a), config.slot_scope(b)
        assert sa != sb, "不同 token 必须分到不同桶"
        assert "tokAAA" not in sa and "hub.example.com" not in sa, "token host 不能进 scope"

    def test_egress_mode_still_refuses_unknown_port(self, monkeypatch):
        monkeypatch.setattr(config, "SLOT_EGRESS_IPS", {})
        monkeypatch.setattr(config, "PROXY_SCOPE_MODE", "egress")
        with pytest.raises(ValueError):
            config.slot_scope("res.example.com:2000:u-region-US-sid-AAA111-t-1:pw")

    def test_mapped_port_wins_even_in_session_mode(self, monkeypatch):
        monkeypatch.setattr(config, "SLOT_EGRESS_IPS", {"7901": "203.0.113.7"})
        monkeypatch.setattr(config, "PROXY_SCOPE_MODE", "session")
        assert config.slot_scope("http://127.0.0.1:7901") == "203.0.113.7"


class TestProxySlots:
    def test_empty_when_unset(self, monkeypatch):
        monkeypatch.setattr(config, "IR_PROXY_SLOTS", "")
        monkeypatch.setattr(config, "IR_PROXY_SLOTS_FILE", "")
        assert config.proxy_slots() == []

    def test_parses_commas_and_newlines_and_comments(self, monkeypatch):
        monkeypatch.setattr(config, "IR_PROXY_SLOTS_FILE", "")
        monkeypatch.setattr(
            config,
            "IR_PROXY_SLOTS",
            "http://127.0.0.1:7901, http://127.0.0.1:7902\n# 注释行\nhttp://127.0.0.1:7903",
        )
        assert config.proxy_slots() == [
            "http://127.0.0.1:7901",
            "http://127.0.0.1:7902",
            "http://127.0.0.1:7903",
        ]

    def test_normalizes_residential_form(self, monkeypatch):
        """住宅代理写 `host:port:user:pass` —— 清单读出来必须是完整 URL。"""
        monkeypatch.setattr(config, "PROXY_SCHEME", "http")
        monkeypatch.setattr(config, "IR_PROXY_SLOTS_FILE", "")
        monkeypatch.setattr(
            config,
            "IR_PROXY_SLOTS",
            "res.example.com:2000:u-region-US-sid-AAA111-t-1:pw",
        )
        assert config.proxy_slots() == ["http://u-region-US-sid-AAA111-t-1:pw@res.example.com:2000"]

    def test_missing_file_raises_not_silently_empty(self, monkeypatch, tmp_path):
        """🔴 指向的文件不存在 ⇒ 抛错，**不能**静默返回空。

        静默返回空的后果：池化模式悄悄退化成直连，所有账号挤一个出口 IP，
        表现是"批量全灭" —— 而真正的原因（路径写错）完全看不出来。
        """
        monkeypatch.setattr(config, "IR_PROXY_SLOTS_FILE", str(tmp_path / "nope.txt"))
        monkeypatch.setattr(config, "IR_PROXY_SLOTS", "")
        with pytest.raises(ValueError, match="不存在"):
            config.proxy_slots()


# ── 邀请码 ────────────────────────────────────────────────────────
class TestNormalizeInviteCode:
    """🔴 本地**只做规范化 + 格式校验**，不做存在性校验。

    存在性服务端自己都不查（实测：形状合法但不存在的 `ZZZZZZZZ` → HTTP 200、
    账号照建、零报错），本地更不可能查。下面这些用例钉的是"别假装能验出来"。
    """

    def test_uppercases_and_strips(self):
        assert config.normalize_invite_code("  zq9qevjj ") == "ZQ9QEVJJ"

    def test_empty_stays_empty(self):
        assert config.normalize_invite_code("") == ""
        assert config.normalize_invite_code("   ") == ""
        assert config.normalize_invite_code(None) == ""

    def test_accepts_realistic_code(self):
        # 形态取自实测 `GET /api/invite` 的返回（8 位大写字母数字）
        assert config.normalize_invite_code("R5AZGYNU") == "R5AZGYNU"

    def test_rejects_overlong(self):
        with pytest.raises(ValueError, match="过长"):
            config.normalize_invite_code("A" * (config.INVITE_CODE_MAXLEN + 1))

    def test_accepts_exactly_maxlen(self):
        """边界：正好等于 `maxLength` 的值**必须**放过（前端就是这么卡的）。"""
        code = "A" * config.INVITE_CODE_MAXLEN
        assert config.normalize_invite_code(code) == code

    def test_rejects_non_alnum(self):
        with pytest.raises(ValueError):
            config.normalize_invite_code("ZQ9Q-EVJJ")

    def test_error_explains_the_silent_failure(self):
        """🔴 报错文案必须点明"服务端不会替你发现"。

        不写这一句，读者会以为"本地不拦也没关系，服务端总会报错" ——
        而服务端**恰恰不报错**，这正是本地校验唯一的价值所在。
        """
        with pytest.raises(ValueError) as ei:
            config.normalize_invite_code("ZQ9Q-EVJJ")
        assert "不校验" in str(ei.value)


class TestInviteCodeWiring:
    """配置层只负责"从环境变量读 + 规范成大写"。"""

    def _quiet(self, monkeypatch):
        """把 `validate()` 里与邀请码无关的必需项都满足掉。"""
        monkeypatch.setattr(config, "MAILBOX_KIND", "worker")
        monkeypatch.setattr(config, "WORKER_ADMIN_TOKEN", "x")
        # 🔴 Worker 地址/域名也要满足：不能依赖开发者本地的 `.env`
        #    （CI / 新克隆都没有 `.env`，缺了这两项 `validate()` 会报出来）。
        monkeypatch.setattr(config, "WORKER_BASE", "https://worker.example")
        monkeypatch.setattr(config, "WORKER_DOMAIN", "example-mail.test")
        monkeypatch.setattr(config, "IR_PROXY_SLOTS", "")
        monkeypatch.setattr(config, "IR_PROXY_SLOTS_FILE", "")
        monkeypatch.setattr(config, "SLOT_EGRESS_IPS", {})

    def test_module_constant_is_normalised_at_read_time(self):
        """🔴 模块级读取行必须带 `.strip().upper()` —— 这是**源码级**断言。

        为什么不写行为断言（`INVITE_CODE == INVITE_CODE.upper()`）：
        CI 环境里 `FO_INVITE_CODE` 是空的，那种断言**平凡通过**，钉不住东西。
        而漏掉 `.upper()` 的后果是静默的：小写码发出去，服务端不校验、
        不报错，推荐关系悄悄不成立。
        """
        src = CONFIG_PY.read_text(encoding="utf-8")
        m = re.search(r'^INVITE_CODE\s*=\s*os\.getenv\("FO_INVITE_CODE".*$', src, re.M)
        assert m, "找不到 INVITE_CODE 的模块级读取行（改名了？）"
        line = m.group(0)
        assert ".strip()" in line and ".upper()" in line, f"INVITE_CODE 读取行缺少规范化：{line}"

    def test_validate_reports_malformed_code(self, monkeypatch):
        """🔴 码不合法必须在 `validate()` 里报出来 —— 它是在**起飞前**跑的。

        拖到注册时才炸的后果：`opc.register()` 抛的 ValueError 会被
        `stage_register` 的宽泛 `except Exception` 吞掉，表现成
        "N 个账号网络失败"，而真实原因（一个打错的参数）完全看不出来。
        """
        self._quiet(monkeypatch)
        monkeypatch.setattr(config, "INVITE_CODE", "BAD-CODE")
        assert any("FO_INVITE_CODE" in m for m in config.validate())

    def test_validate_silent_on_empty_code(self, monkeypatch):
        """空码是**合法**的（= 不使用邀请码），不该被报成缺配置。"""
        self._quiet(monkeypatch)
        monkeypatch.setattr(config, "INVITE_CODE", "")
        assert config.validate() == []

    def test_validate_silent_on_good_code(self, monkeypatch):
        self._quiet(monkeypatch)
        monkeypatch.setattr(config, "INVITE_CODE", "ZQ9QEVJJ")
        assert config.validate() == []


# ── 常量自洽 ──────────────────────────────────────────────────────
class TestConstants:
    def test_model_paths_have_api_prefix(self):
        """🔴 回归钉子：模型路径**必须**带 `/api`。

        漏了它站点返回 200 + 6707B 的 SPA 兜底页，看着完全正常。
        """
        assert config.MODEL_SERVICE_PATH.startswith("/api/")
        assert config.MODEL_V1_PATH.startswith("/api/")
        assert config.MODEL_V1_PATH.startswith(config.MODEL_SERVICE_PATH)

    def test_default_model_is_a_text_model_name(self):
        assert config.DEFAULT_MODEL.startswith("opc-")
        assert "txt" in config.DEFAULT_MODEL, (
            "默认示例模型必须是 text 类，否则 chat/completions 的示例会报错"
        )

    def test_account_types_cover_default(self):
        assert config.ACCOUNT_TYPE in config.ACCOUNT_TYPES

    def test_ip_quota_max_matches_measured_value(self):
        """实测值 = 5（`tools/probes/probe_ip_daily_limit.py`）。

        ⚠ 站点改了这里不会自动跟 —— 这条钉的是"有人手滑改小了"，
          不是"站点永远是这个数"。上限值住在 `quota.py`（`FO_IP_QUOTA_MAX`），
          不在 `config.py` —— 别在 config 里找它。
        """
        assert quota.limit() == 5

    def test_rate_interval_exceeds_server_window(self):
        """🔴 本地闸门必须**大于**服务端的 60s 窗口。

        调小到 60 以下 ⇒ 每次注册都撞 429 ⇒ 白等一个窗口，吞吐反而更低。
        """
        assert config.REG_MIN_INTERVAL > 60

    def test_rate_backoff_exceeds_window(self):
        """退避时长也要盖过 60s 窗口，否则重试等于再撞一次。"""
        assert config.REG_RATE_BACKOFF > 60


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
