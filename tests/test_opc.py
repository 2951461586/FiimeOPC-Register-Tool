"""`src.opc` 的纯函数与判据。**不联网。**

这些判据是整链最容易静默出错的地方，所以每一条都钉死。
"""

import pytest

from src import opc


# ── 429 分流（本项目最重要的判据）──────────────────────────────────
class TestClassify429:
    """两种 429 的处置完全相反，判错一次就是"对着死出口打一天枪"。"""

    def test_rate_limit(self):
        assert opc.classify_429("注册过于频繁，请一分钟后再试") == opc.LIMIT_RATE

    def test_ip_quota(self):
        assert opc.classify_429(
            "该 IP 今日注册数量已达上限，请明日再试或联系管理员"
        ) == opc.LIMIT_IP_QUOTA

    def test_unknown_falls_back_to_rate(self):
        """未知文案 → 当速率处理。

        🔴 这个方向是**刻意**的，代价不对称：
          误判成 ip_quota ⇒ 白白把干净出口冷却 6 小时；
          误判成 rate     ⇒ 只是多退避一次。
        """
        assert opc.classify_429("服务器开小差了") == opc.LIMIT_OTHER
        assert opc.classify_429("") == opc.LIMIT_OTHER
        assert opc.classify_429(None) == opc.LIMIT_OTHER

    def test_quota_wins_when_both_marks_present(self):
        """两个关键词都出现时，**配额优先** —— 它是更严重的那一类。

        ⚠ 顺序不能反：站点将来可能把文案改成"注册过于频繁…今日数量已达上限"，
          那样判成 rate 会让我们无限重试一个已死的出口。
        """
        both = "注册过于频繁，请一分钟后再试；该 IP 今日注册数量已达上限"
        assert opc.classify_429(both) == opc.LIMIT_IP_QUOTA

    def test_marks_are_not_confused(self):
        """两个 mark 常量本身必须是不同的字符串（防止改配置时手滑写重）。"""
        assert opc.config.RATE_LIMIT_MARK != opc.config.IP_QUOTA_MARK


# ── 密码生成 ──────────────────────────────────────────────────────
class TestGenPassword:
    def test_contains_letter(self):
        """🔴 站点要求密码**必须含字母**（实测纯数字被 400 拒）。"""
        for _ in range(50):
            p = opc.gen_password()
            assert any(c.isalpha() for c in p), p

    def test_has_all_three_classes(self):
        """大小写字母 / 数字 / 符号 三类齐全（站点前端校验"含两类以上"）。

        🔴 这一条曾经**必红**，而且暴露的是真 bug：老实现是
          `"Lz#" + random(letters+digits, 9)`，9 位里没数字的概率约 21.6%，
          但 docstring 宣称"保证三类齐全" —— 假保证。
          现在是先钉死必需字符再补位，50 次里每一次都成立。
        """
        for _ in range(200):
            p = opc.gen_password()
            assert any(c.islower() for c in p)
            assert any(c.isupper() for c in p)
            assert any(c.isdigit() for c in p)
            assert any(not c.isalnum() for c in p)

    def test_length(self):
        assert len(opc.gen_password(12)) == 12
        assert len(opc.gen_password()) == opc.config.PASSWORD_LEN

    def test_min_length_is_raised_not_ignored(self):
        """长度太小会被抬到 5（`Lz#` + 数字 + 小写字母），不抛异常。"""
        for n in (1, 2, 3, 4, 5):
            p = opc.gen_password(n)
            assert len(p) >= 5, (n, p)
            assert any(c.isdigit() for c in p), (n, p)
            assert any(c.islower() for c in p), (n, p)

    def test_keeps_recognizable_prefix(self):
        """前缀 `Lz#` 保留 —— 台账里靠它一眼认出是本工具生成的密码。"""
        for _ in range(20):
            assert opc.gen_password().startswith("Lz#")

    def test_unique_enough(self):
        """200 个样本里重复不应超过 1 对（随机性够用即可，不做统计检验）。"""
        s = {opc.gen_password() for _ in range(200)}
        assert len(s) >= 199


# ── 激活链接提取（**本站最关键的坑**）──────────────────────────────
class TestFindActivationUrl:
    """🔴 Worker 的 `extracted_json` 对本站激活邮件返回空数组，
    所以必须自己从 body 正则捞。这些用例钉的就是那个正则的行为。
    """

    REAL_BODY = """
    <html><body>
    <p>你好 lz123456，</p>
    <a href="https://opc.fiime.cn/activate?token=7d88622ae13c558c9534f949468311e2b811df5b2b57f88d239f4730d13930a2">
      点击激活
    </a>
    <p>或复制：https://opc.fiime.cn/activate?token=7d88622ae13c558c9534f949468311e2b811df5b2b57f88d239f4730d13930a2</p>
    <p><a href="https://opc.fiime.cn/privacy">隐私政策</a></p>
    <img src="https://cdn.example.com/logo.png">
    </body></html>
    """

    def test_finds_real_link(self):
        url = opc.find_activation_url(self.REAL_BODY)
        assert url is not None
        assert url.startswith("https://opc.fiime.cn/activate?token=")
        assert "7d88622a" in url

    def test_ignores_other_links(self):
        """邮件里还有隐私政策 / logo 等链接，不能抓错。"""
        url = opc.find_activation_url(self.REAL_BODY)
        assert "privacy" not in url
        assert "logo" not in url

    def test_strips_trailing_punctuation(self):
        """正文里 URL 后面常跟中文标点，必须剪掉，否则 token 会被污染。

        🔴 这条曾经**必红**：老正则用排除法 `[^\\s"'<>()]`，会把 `。谢谢`
          整串吞进匹配；而 `rstrip` 只剪**末尾**、末尾是「谢」⇒ 一个字都剪不掉
          ⇒ token = `abc123。谢谢` ⇒ 激活 400，且报错完全指不到这个原因。
          现在按 RFC 3986 字符集匹配，中文标点从源头就进不来。
        """
        body = "请点击 https://opc.fiime.cn/activate?token=abc123。谢谢"
        assert opc.find_activation_url(body) == \
            "https://opc.fiime.cn/activate?token=abc123"

    def test_strips_various_cjk_punctuation(self):
        """中文标点种类很多，逐个钉一遍（全角括号 / 逗号 / 顿号 / 破折号）。"""
        for tail in ("。", "，", "、", "；", "：", "！", "？", "）", "】",
                     "」", "』", "…", "—", "～"):
            body = f"链接：https://opc.fiime.cn/activate?token=abc123{tail}"
            assert opc.find_activation_url(body) == \
                "https://opc.fiime.cn/activate?token=abc123", tail

    def test_strips_ascii_trailing_punctuation(self):
        body = "see https://opc.fiime.cn/activate?token=abc123, then click"
        assert opc.find_activation_url(body) == \
            "https://opc.fiime.cn/activate?token=abc123"

    def test_link_inside_html_attr(self):
        """HTML 版邮件里 URL 被双引号包着 —— 引号必须截断匹配。"""
        body = '<a href="https://opc.fiime.cn/activate?token=abc123">go</a>'
        assert opc.find_activation_url(body) == \
            "https://opc.fiime.cn/activate?token=abc123"

    def test_link_inside_html_attr_with_punctuation(self):
        """双重陷阱：引号截断 + 属性后紧跟中文标点。"""
        body = '<a href="https://opc.fiime.cn/activate?token=abc123">点此</a>，谢谢'
        assert opc.find_activation_url(body) == \
            "https://opc.fiime.cn/activate?token=abc123"

    def test_rejects_foreign_host(self):
        """别的域名下的 /activate 不算 —— 邮件里可能有第三方的追踪链接。"""
        body = "https://evil.example.com/activate?token=abc"
        assert opc.find_activation_url(body) is None

    def test_returns_none_on_empty(self):
        assert opc.find_activation_url("") is None
        assert opc.find_activation_url(None) is None
        assert opc.find_activation_url("no links here") is None


class TestTokenFromUrl:
    def test_extracts_token(self):
        u = "https://opc.fiime.cn/activate?token=deadbeef"
        assert opc.OpcClient.token_from_activation_url(u) == "deadbeef"

    def test_empty_when_missing(self):
        assert opc.OpcClient.token_from_activation_url(
            "https://opc.fiime.cn/activate") == ""

    def test_extra_params_ok(self):
        u = "https://opc.fiime.cn/activate?token=abc&ref=x"
        assert opc.OpcClient.token_from_activation_url(u) == "abc"


# ── 结果类型 ──────────────────────────────────────────────────────
class TestResultDataclasses:
    def test_register_result_defaults(self):
        r = opc.RegisterResult()
        assert r.ok is False and r.limit_kind == "" and r.http_status == 0

    def test_apikey_defaults(self):
        k = opc.ApiKey()
        assert k.key == "" and k.key_id == ""


# ── 客户端构造（不联网的部分）────────────────────────────────────
class TestClientConstruction:
    def test_csrf_starts_empty(self):
        c = opc.OpcClient(proxy="http://127.0.0.1:1", log=lambda *a: None)
        assert c.csrf == ""

    def test_proxy_sets_trust_env_false(self):
        """🔴 挂了代理必须同时关 `trust_env`，否则环境变量会静默盖掉它。

        本机实测有 `HTTP_PROXY`（沙箱代理），不关的话请求**成功但没走指定代理** ——
        不报错、不告警，很容易误判成"代理已生效"。
        """
        c = opc.OpcClient(proxy="http://127.0.0.1:7901", log=lambda *a: None)
        assert c.session.trust_env is False
        assert "7901" in c.session.proxies.get("https", "")

    def test_no_proxy_does_not_touch_trust_env(self):
        """没配代理时**不动** `trust_env` —— 行为与加这个功能之前一致。"""
        c = opc.OpcClient(proxy=None, log=lambda *a: None)
        assert c.session.trust_env is True

    def test_api_base(self):
        c = opc.OpcClient(proxy=None, log=lambda *a: None)
        assert c.api_base().endswith("/api/model-service/v1")


# ── 429 分类的端到端串接（用假 session，不联网）──────────────────
class TestRegisterHandles429:
    class _Resp:
        def __init__(self, status, body):
            self.status_code = status
            self._body = body
            self.ok = 200 <= status < 300
            self.headers = {}

        def json(self):
            return self._body

    def _client(self, resp):
        c = opc.OpcClient(proxy=None, log=lambda *a: None)
        c._post = lambda *a, **k: resp          # 打桩，不联网
        return c

    def test_rate_429_sets_limit_kind(self):
        c = self._client(self._Resp(429, {"error": "注册过于频繁，请一分钟后再试"}))
        r = c.register("u", "e@x.com", "Lz#abc123456")
        assert r.ok is False
        assert r.limit_kind == opc.LIMIT_RATE
        assert r.http_status == 429

    def test_ip_quota_429_sets_limit_kind(self):
        c = self._client(self._Resp(429, {"error": "该 IP 今日注册数量已达上限，请明日再试"}))
        r = c.register("u", "e@x.com", "Lz#abc123456")
        assert r.limit_kind == opc.LIMIT_IP_QUOTA

    def test_400_is_plain_error(self):
        c = self._client(self._Resp(400, {"error": "密码必须包含字母"}))
        r = c.register("u", "e@x.com", "12345678")
        assert r.ok is False
        assert r.limit_kind == ""            # 不是 429，不该有 limit_kind
        assert r.error == "密码必须包含字母"

    def test_200_parses_payload(self):
        c = self._client(self._Resp(200, {
            "user": {"id": "u_1", "opcId": "OPC-1", "username": "u"},
            "needActivation": True, "registrationTokenReward": 100}))
        r = c.register("u", "e@x.com", "Lz#abc123456")
        assert r.ok and r.need_activation and r.token_reward == 100
        assert r.user["opcId"] == "OPC-1"

    def test_honeypot_field_always_sent_empty(self):
        """🔴 蜜罐字段 `website` **必须传空串**，不能省略。

        实测传值会得到 400「注册失败，请刷新页面后重试」；而省略的后果取决于
        服务端实现（可能当成 None 触发同一分支）。所以固定传 ""。
        """
        seen = {}

        class Cap:
            status_code = 200
            ok = True
            headers = {}

            def json(self):
                return {}

        def spy(path, body=None, **kw):
            seen.update(body or {})
            return Cap()

        c = opc.OpcClient(proxy=None, log=lambda *a: None)
        c._post = spy
        c.register("u", "e@x.com", "Lz#abc123456")
        assert "website" in seen and seen["website"] == ""
        assert seen["agree"] is True
        assert seen["accountType"] in opc.config.ACCOUNT_TYPES


# ── 邀请码（**静默失败**的那条链）─────────────────────────────────
class TestInviteCodeInRequest:
    """🔴 邀请码这条链失败时**完全静默**，所以每一环都得单独钉住。

    实测（2026-09-26）：形状合法但必然不存在的码 `ZZZZZZZZ` → HTTP 200，
    账号正常建出，零报错。所以"码写错了"在状态码、成功率、退出码里
    **一个信号都没有** —— 唯一判据是响应里的 `user.invitedAt`。
    """

    class _Resp:
        status_code = 200
        ok = True
        headers = {}

        def __init__(self, body):
            self._body = body

        def json(self):
            return self._body

    def _client(self, resp_body=None):
        """返回 `(client, seen)`。`seen["body"]` 是**实际发出去的请求体**。"""
        seen: dict = {}

        def spy(path, body=None, **kw):
            seen["path"] = path
            seen["body"] = dict(body or {})
            return self._Resp(resp_body or {})

        c = opc.OpcClient(proxy=None, log=lambda *a: None)
        c._post = spy                      # 打桩，不联网
        return c, seen

    def test_code_is_normalised_before_sending(self):
        """小写 + 前后空格 → 发出去的是大写去空白的码。"""
        c, seen = self._client({"user": {"id": "u1"}})
        c.register("u", "e@x.com", "pw", invite_code="  zq9qevjj ")
        assert seen["body"]["inviteCode"] == "ZQ9QEVJJ"

    def test_key_is_omitted_when_no_code(self):
        """🔴 空值时**整个键省略**，不是空串。

        前端就是 `inviteCode: n.inviteCode || void 0`。发空串和省略在
        服务端可能走不同分支 —— 与前端保持一致是唯一有依据的做法。
        """
        c, seen = self._client({"user": {"id": "u1"}})
        c.register("u", "e@x.com", "pw")
        assert "inviteCode" not in seen["body"]

    def test_key_is_omitted_when_code_is_blank(self):
        c, seen = self._client({"user": {"id": "u1"}})
        c.register("u", "e@x.com", "pw", invite_code="   ")
        assert "inviteCode" not in seen["body"]

    def test_sent_code_recorded_on_result(self):
        """`invite_code_sent` 记的是**实际发出去的**，不是配置值。"""
        c, _ = self._client({"user": {"id": "u1"}})
        r = c.register("u", "e@x.com", "pw", invite_code="zq9qevjj")
        assert r.invite_code_sent == "ZQ9QEVJJ"

    def test_sent_code_empty_when_not_sent(self):
        c, _ = self._client({"user": {"id": "u1"}})
        r = c.register("u", "e@x.com", "pw")
        assert r.invite_code_sent == ""

    def test_malformed_code_raises_and_sends_nothing(self):
        """🔴 本地拦下畸形码，且**不发请求**（别烧掉一个出口的额度）。"""
        c, seen = self._client({"user": {"id": "u1"}})
        with pytest.raises(ValueError):
            c.register("u", "e@x.com", "pw", invite_code="BAD-CODE")
        assert "path" not in seen, "畸形码不该发出任何请求"

    # ── invitedAt：唯一的有效性判据 ────────────────────────────
    def test_invited_at_parsed_from_register_user(self):
        c, _ = self._client({"user": {"id": "u1", "invitedAt": 1788000000000}})
        r = c.register("u", "e@x.com", "pw", invite_code="ZQ9QEVJJ")
        assert r.invited_at == 1788000000000

    def test_invited_at_zero_when_absent(self):
        """字段缺失 ⇒ 0（= 未受邀）。这个方向的误判是**保守**的。"""
        c, _ = self._client({"user": {"id": "u1"}})
        r = c.register("u", "e@x.com", "pw", invite_code="ZQ9QEVJJ")
        assert r.invited_at == 0

    def test_invited_at_tolerates_garbage(self):
        """字段是脏值时返回 0，**不能**抛异常把整个注册判成失败。"""
        for bad in ("abc", None, {}, []):
            c, _ = self._client({"user": {"id": "u1", "invitedAt": bad}})
            r = c.register("u", "e@x.com", "pw", invite_code="ZQ9QEVJJ")
            assert r.ok is True and r.invited_at == 0, f"bad={bad!r}"

    def test_zero_reward_is_not_treated_as_failure(self):
        """🔴 带码时注册响应的 `registrationTokenReward` **就是 0**。

        实测：有效码 → 注册 0 / 激活 100000000；不带码 → 注册直接 100000000。
        ⇒ "注册时 reward=0" 是**奖励延后到激活**的正常现象，**不是**错误信号，
          更不能拿它判断码是否有效（早期就是这么误读的）。
        """
        c, _ = self._client({"user": {"id": "u1", "invitedAt": 1788000000000},
                             "needActivation": True,
                             "registrationTokenReward": 0})
        r = c.register("u", "e@x.com", "pw", invite_code="ZQ9QEVJJ")
        assert r.ok is True
        assert r.token_reward == 0          # 正常，不是错误
        assert r.invited_at == 1788000000000

    def test_activate_parses_invited_at(self):
        """激活响应也要取 `invitedAt` —— 它可能就是**延后写入**的那个点。"""
        c, _ = self._client({"user": {"id": "u1", "invitedAt": 1788000000000},
                             "registrationTokenReward": 100000000})
        r = c.activate("tok")
        assert r.ok and r.invited_at == 1788000000000
        assert r.token_reward == 100000000


# ── 邀请码必须活过重试 ────────────────────────────────────────────
class TestInviteCodeSurvivesRetry:
    """🔴 邀请码必须**每一次重试都带上** —— 这是最容易静默降级的一处。

    若码只在首次尝试里传，那么"被速率限流过一次"的账号会**不带码注册成功**：
    注册成功、激活成功、退出码 0，只有 `invitedAt=0` 孤零零地摆在那里，
    而且它会被读成"这个码不灵"，而不是"我们的代码把码丢了"。
    """

    def test_code_passed_on_every_attempt(self, monkeypatch):
        from src import pipeline

        monkeypatch.setattr(pipeline.config, "REG_RATE_RETRY", 2)
        calls: list = []

        class _Client:
            def register(self, u, e, p, invite_code=""):
                calls.append(invite_code)
                if len(calls) == 1:
                    return opc.RegisterResult(limit_kind=opc.LIMIT_RATE,
                                              error="注册过于频繁")
                return opc.RegisterResult(ok=True, user={"id": "u1"})

        class _Gate:
            def wait(self, scope):
                pass

        rec = pipeline.AccountRecord(username="u", email="e@x.com", password="pw")
        res = pipeline._register_with_retry(
            _Client(), rec, _Gate(), "203.0.113.7", lambda *a: None, "ZQ9QEVJJ")

        assert res.ok is True
        assert calls == ["ZQ9QEVJJ", "ZQ9QEVJJ"], f"重试时丢了邀请码：{calls}"

    def test_ip_quota_aborts_without_losing_code(self, monkeypatch):
        """出口被判死时抛异常中止 —— 此时也不该把码"用掉"或改掉。"""
        from src import pipeline

        seen: list = []

        class _Client:
            def register(self, u, e, p, invite_code=""):
                seen.append(invite_code)
                return opc.RegisterResult(limit_kind=opc.LIMIT_IP_QUOTA,
                                          error="该 IP 今日注册数量已达上限")

        rec = pipeline.AccountRecord(username="u", email="e@x.com", password="pw")
        with pytest.raises(pipeline._IpQuotaBlocked):
            pipeline._register_with_retry(
                _Client(), rec, None, "203.0.113.7", lambda *a: None, "ZQ9QEVJJ")
        assert seen == ["ZQ9QEVJJ"]


# ── 登录结果 ──────────────────────────────────────────────────────
class TestLogin:
    class _Resp:
        def __init__(self, status, body):
            self.status_code = status
            self._body = body
            self.ok = 200 <= status < 300
            self.headers = {}

        def json(self):
            return self._body

    def _client(self, resp):
        c = opc.OpcClient(proxy=None, log=lambda *a: None)
        c._post = lambda *a, **k: resp
        return c

    def test_success(self):
        c = self._client(self._Resp(200, {"user": {"id": "u1", "username": "x"}}))
        r = c.login("e@x.com", "p")
        assert r.ok and r.user["id"] == "u1"

    def test_email_not_activated_carries_code(self):
        c = self._client(self._Resp(401, {
            "error": "邮箱未激活", "code": "EMAIL_NOT_ACTIVATED",
            "email": "e@x.com"}))
        r = c.login("e@x.com", "p")
        assert not r.ok and r.code == "EMAIL_NOT_ACTIVATED"

    def test_need_totp_is_reported_not_faked(self):
        """开了 2FA 的账号本工具**不支持** —— 必须如实报错，不能假装成功。

        理由：TOTP secret 在注册流程里拿不到，假装成功只会让台账里多一条
        永远用不了的 key。
        """
        c = self._client(self._Resp(200, {"needTotp": True, "challenge": "c1"}))
        r = c.login("e@x.com", "p")
        assert not r.ok and r.code == "NEED_TOTP"

    def test_missing_user_field_is_failure(self):
        c = self._client(self._Resp(200, {}))
        r = c.login("e@x.com", "p")
        assert not r.ok


# ── key 校验的重试语义 ────────────────────────────────────────────
class TestVerifyKeySemantics:
    """🔴 判据是 **content-type 是 JSON + 能解析出 data 列表**，不是状态码。

    本站是 SPA。请求**不存在的路径**（例如 base_url 漏了 `/api`）时，
    前端路由会兜底返回 **HTTP 200 + 6707 字节的 index.html**。
    所以"2xx 就算成功"是错的判据 —— 它会把一个根本没打到 API 的请求记成成功。

    实测对照（2026-09-26，本项目为此付过一次批量校验全红的代价）：
      GET /model-service/v1/models      → 200, HTML, 6707B  ✗
      GET /api/model-service/v1/models  → 200, JSON, 1880B  ✓

    ⚠ 早期这里写着"新建 key 有 ~10s 传播延迟"，**那个结论是错的** ——
      真正原因就是漏了 `/api`。重试保留只是防边缘层抖动。
    """

    class _R:
        """最小假响应。

        🔴 **必须带 `headers`** —— 早期版本的假响应漏了这个字段，
           `verify_key` 里 `r.headers.get(...)` 会抛 `AttributeError`，
           monkeypatch 的 `fake_get` 又把它吞成"网络异常"，用例假绿。
        """
        def __init__(self, status, text, *, ctype="", body=None):
            self.status_code = status
            self.ok = 200 <= status < 300
            self.text = text
            self.headers = {"content-type": ctype} if ctype else {}
            self._body = body

        def json(self):
            if self._body is None:
                raise ValueError("not json")
            return self._body

    def test_401_is_terminal(self, monkeypatch):
        import requests as _rq

        calls = []

        def fake_get(url, **kw):
            calls.append(url)
            return self._R(401, '{"error":"API Key 无效"}',
                           ctype="application/json",
                           body={"error": {"code": "invalid_api_key"}})

        monkeypatch.setattr(_rq, "get", fake_get)
        ok, why = opc.OpcClient.verify_key("k", attempts=3, delay=0)
        assert ok is False and "401" in why
        assert len(calls) == 1, "401 是确定失败，不该重试"

    def test_spa_fallback_2xx_is_not_success(self, monkeypatch):
        """🔴 单次调用：2xx + text/html（SPA 兜底页）必须判失败，且说明里带字节数。

        那个字节数是识别"被 SPA 兜底"的指纹 —— 6707 就是本站 index.html 的大小。
        """
        import requests as _rq

        html = "<html>" + "x" * 6700 + "</html>"          # ≈6707 字节

        def fake_get(url, **kw):
            return self._R(200, html, ctype="text/html; charset=utf-8")

        monkeypatch.setattr(_rq, "get", fake_get)
        ok, why = opc.OpcClient.verify_key("k", attempts=1, delay=0)
        assert ok is False
        assert "content-type" in why
        assert str(len(html)) in why, "说明里必须带字节数（SPA 兜底页指纹）"
        assert "/api" in why, "提示里要指出多半是漏了 /api"

    def test_non_json_2xx_retries_then_succeeds(self, monkeypatch):
        """前两次 SPA 兜底页 → 第三次真 JSON：必须重试到成功。"""
        import requests as _rq

        state = {"n": 0}

        def fake_get(url, **kw):
            state["n"] += 1
            if state["n"] < 3:
                return self._R(200, "<html>edge not synced</html>",
                               ctype="text/html")
            return self._R(200, '{"data":[{"id":"m1"}]}',
                           ctype="application/json; charset=utf-8",
                           body={"data": [{"id": "m1"}]})

        monkeypatch.setattr(_rq, "get", fake_get)
        ok, why = opc.OpcClient.verify_key("k", attempts=4, delay=0)
        assert ok is True
        assert "1 models" in why
        assert state["n"] == 3, "前两次非 JSON 必须重试"

    def test_gives_up_after_attempts(self, monkeypatch):
        """一直兜底页 → 用尽 attempts 后判失败。"""
        import requests as _rq

        def fake_get(url, **kw):
            return self._R(200, "<html>still not synced</html>",
                           ctype="text/html")

        monkeypatch.setattr(_rq, "get", fake_get)
        ok, why = opc.OpcClient.verify_key("k", attempts=2, delay=0)
        assert ok is False
        assert "content-type" in why

    def test_json_ctype_but_unparsable(self, monkeypatch):
        """边缘层偶发：content-type 说 JSON、body 却解析不了 —— 也要判失败。"""
        import requests as _rq

        def fake_get(url, **kw):
            return self._R(200, "{oops", ctype="application/json")  # body=None

        monkeypatch.setattr(_rq, "get", fake_get)
        ok, why = opc.OpcClient.verify_key("k", attempts=1, delay=0)
        assert ok is False
        assert "解析失败" in why

    def test_url_uses_api_prefix(self, monkeypatch):
        """🔴 回归钉子：请求 URL 必须带 `/api` 前缀（漏了就是 6707B 兜底页）。"""
        import requests as _rq

        seen = []

        def fake_get(url, **kw):
            seen.append(url)
            return self._R(200, '{"data":[]}', ctype="application/json",
                           body={"data": []})

        monkeypatch.setattr(_rq, "get", fake_get)
        opc.OpcClient.verify_key("k", attempts=1, delay=0)
        assert seen and "/api/model-service/v1/models" in seen[0]

    def test_explicit_base_overrides(self, monkeypatch):
        """显式传 base 时以 base 为准（台账里记录的自定义网关）。"""
        import requests as _rq

        seen = []

        def fake_get(url, **kw):
            seen.append(url)
            return self._R(200, '{"data":[]}', ctype="application/json",
                           body={"data": []})

        monkeypatch.setattr(_rq, "get", fake_get)
        opc.OpcClient.verify_key("k", base="https://gw.example.com/v1",
                                 attempts=1, delay=0)
        assert seen[0] == "https://gw.example.com/v1/models"


# ── ServiceInfo（服务端 `service` 块的解析）────────────────────────
class TestServiceInfo:
    """🔴 `models` 是**对象列表**，不是字符串列表。

    实测服务端返回（2026-09-26，`GET /api/model-service`）：

        {"service": {"enabled": true,
                     "apiBasePath": "/api/model-service/v1",
                     "dailyTokenLimit": 500000000,
                     "maxApiKeysPerUser": 5,
                     "models": [{"id": "opc-txt-v1", "type": "text",
                                 "enabled": true, "rateMultiplier": 2},
                                {"id": "opc-image-v1", "type": "image", ...}]}}

    早期 `describe()` 里写的是 `len(si.models)` —— 那是**元素个数**，
    恰好也对，但如果哪天有人写成 `",".join(si.models)` 就会静默得到
    dict 的 repr。所以这里把"取 id"这件事变成有测试的方法。
    """

    REAL = {
        "enabled": True,
        "apiBasePath": "/api/model-service/v1",
        "dailyTokenLimit": 500000000,
        "maxApiKeysPerUser": 5,
        "models": [
            {"id": "opc-txt-v1", "object": "model", "type": "text",
             "enabled": True, "debug": False, "rateMultiplier": 2},
            {"id": "opc-image-v1", "object": "model", "type": "image",
             "enabled": True},
            {"id": "opc-video-v1", "object": "model", "type": "video",
             "enabled": True},
            {"id": "opc-txt-v2", "object": "model", "type": "text",
             "enabled": True, "rateMultiplier": 2},
            {"id": "sensenova-6-8-flash-lite", "object": "model",
             "type": "text", "enabled": True},
            {"id": "opc-txt-v0", "object": "model", "type": "text",
             "enabled": False},                      # 停用的不该出现
        ],
    }

    def _si(self, **over):
        d = dict(self.REAL)
        d.update(over)
        return opc.ServiceInfo(
            enabled=bool(d.get("enabled")),
            api_base_path=str(d.get("apiBasePath") or ""),
            daily_token_limit=int(d.get("dailyTokenLimit") or 0),
            max_keys_per_user=int(d.get("maxApiKeysPerUser") or 0),
            models=d.get("models") or [],
        )

    def test_model_ids_only_enabled(self):
        assert self._si().model_ids() == [
            "opc-txt-v1", "opc-image-v1", "opc-video-v1",
            "opc-txt-v2", "sensenova-6-8-flash-lite"]

    def test_model_ids_can_include_disabled(self):
        assert "opc-txt-v0" in self._si().model_ids(only_enabled=False)

    def test_text_models_excludes_image_and_video(self):
        """🔴 image/video 模型不能进 `/v1/chat/completions` —— 筛出来。"""
        tm = self._si().text_models()
        assert "opc-image-v1" not in tm and "opc-video-v1" not in tm
        assert tm == ["opc-txt-v1", "opc-txt-v2", "sensenova-6-8-flash-lite"]

    def test_default_model_prefers_config(self):
        assert self._si().default_model() == opc.config.DEFAULT_MODEL

    def test_default_model_falls_back_to_first_text(self):
        """配置里的默认模型不在服务端列表里 ⇒ 取第一个**文本**模型。"""
        si = self._si(models=[{"id": "other-txt", "type": "text",
                               "enabled": True}])
        assert si.default_model() == "other-txt"

    def test_default_model_never_returns_image(self):
        """一个文本模型都没有时，回落到配置值，**不要**返回 image 模型。"""
        si = self._si(models=[{"id": "opc-image-v1", "type": "image",
                               "enabled": True}])
        assert si.default_model() == opc.config.DEFAULT_MODEL

    def test_describe_shape(self):
        """报告里逐字依赖这个形状（`ok(19 models, max_keys=5)`）。"""
        assert self._si().describe() == "ok(5 models, max_keys=5)"

    def test_tolerates_plain_string_models(self):
        """服务端哪天改成返回字符串列表，也不能炸。"""
        si = self._si(models=["a", "b"])
        assert si.model_ids() == ["a", "b"]

    def test_tolerates_junk_entries(self):
        si = self._si(models=[None, 3, {"no_id": 1}, {"id": "ok",
                                                      "type": "text",
                                                      "enabled": True}])
        assert si.model_ids() == ["ok"]

    def test_empty_models(self):
        si = self._si(models=[])
        assert si.model_ids() == [] and si.text_models() == []
        assert si.describe() == "ok(0 models, max_keys=5)"


# ── 交付完整性字段 ────────────────────────────────────────────────
class TestAccountRecordDeliveryFields:
    """`base_url` / `key_verified` / `models` 是"交付"的判据，必须存在且默认安全。"""

    def test_defaults(self):
        from src.pipeline import AccountRecord

        r = AccountRecord()
        assert r.base_url == ""
        assert r.models == [] and r.default_model == ""
        # 🔴 默认必须是 None（"没校验"），**不是** False。
        #    若默认 False，`--no-verify` 的批量会被退出码判成"交付不完整"。
        assert r.key_verified is None

    def test_serialises_new_fields(self):
        from src.pipeline import AccountRecord

        r = AccountRecord(base_url="https://x/api/model-service/v1",
                          key_verified=True, models=["m"], default_model="m")
        import json as _json

        d = _json.loads(r.to_json())
        assert d["base_url"] == "https://x/api/model-service/v1"
        assert d["key_verified"] is True


# ── Key 列表 / 恢复（**端点踩错过的那个坑**）─────────────────────
class TestListKeys:
    """🔴 `GET /api/model-service/keys` **不存在**（实测 404 + text/html）。

    服务端只在那个路径上注册了 POST。key 列表在 `GET /api/model-service`
    的 `keys` 字段里 —— 而且 `keys[].secret` 是**明文**
    （站点 UI 说「只显示一次」，那是前端的说法）。

    早期实现打的是 404 那个路径，并且 `if not r.ok: return []` 把 404
    静默变成"这个账号没有 key"。这是本项目最忌讳的失败：**报错被翻译成了正常结果**。
    """

    REAL = {
        "service": {"enabled": True, "apiBasePath": "/api/model-service/v1",
                    "dailyTokenLimit": 500000000, "maxApiKeysPerUser": 5,
                    "models": []},
        "wallet": {"balance": 100000000, "totalEarned": 100000000,
                   "totalSpent": 0},
        "usage": {"date": "2026-09-26", "usedToday": 0,
                  "dailyLimit": 500000000, "remainingToday": 500000000},
        "keys": [
            {"id": "msk_aaa", "name": "default", "masked": "opc_sk_5aa2••••2d3a",
             "secret": "opc_sk_5aa233312cea7d8c08a864dc54dda7212398209607292d3a",
             "status": "active", "createdAt": 1790357521795, "lastUsedAt": 0},
            {"id": "msk_bbb", "name": "prod", "masked": "opc_sk_79ca••••f18e",
             "secret": "opc_sk_79cabc422d417711a6ef2891f8834694e9e4105472cff18e",
             "status": "active", "createdAt": 1790358000000, "lastUsedAt": 0},
        ],
    }

    class _Resp:
        def __init__(self, status, body, ctype="application/json"):
            self.status_code = status
            self.ok = 200 <= status < 300
            self.headers = {"content-type": ctype}
            self._body = body

        def json(self):
            return self._body

        def raise_for_status(self):
            if not self.ok:
                raise RuntimeError(f"HTTP {self.status_code}")

    def _client(self, resp, seen=None):
        c = opc.OpcClient(proxy=None, log=lambda *a: None)

        def fake_get(path, **kw):
            if seen is not None:
                seen.append(path)
            return resp

        c._get = fake_get
        return c

    def test_reads_from_model_service_not_the_404_path(self, monkeypatch):
        """🔴 回归钉子：必须打 `/model-service`，**不是** `/model-service/keys`。"""
        seen = []
        c = self._client(self._Resp(200, self.REAL), seen=seen)
        c.list_keys()
        assert seen == ["/model-service"], seen
        assert not any("keys" in p for p in seen), "那个路径是 404"

    def test_returns_secrets(self):
        """🔴 `secret` 是**明文**且可重复读取 —— 这正是"能捞回来"的依据。"""
        ks = self._client(self._Resp(200, self.REAL)).list_keys()
        assert len(ks) == 2
        assert ks[0]["secret"].startswith("opc_sk_")
        assert ks[0]["id"] == "msk_aaa"

    def test_404_raises_by_default(self):
        """🔴 端点挂了**必须抛**，不能静默返回空列表。

        "真的没有 key" 和 "端点打错了" 对调用方的含义完全不同：
        前者是正常状态，后者是我们的 bug。把后者翻译成前者，
        会让"恢复失败"看起来像"本来就没 key"。
        """
        c = self._client(self._Resp(404, None, ctype="text/html"))
        with pytest.raises(RuntimeError) as ei:
            c.list_keys()
        msg = str(ei.value)
        assert "404" in msg
        assert "/model-service/keys" in msg, "报错要点明那个路径不存在"

    def test_non_strict_returns_empty(self):
        """显式 `strict=False` 才允许"拿不到当没有"。"""
        c = self._client(self._Resp(404, None, ctype="text/html"))
        assert c.list_keys(strict=False) == []

    def test_missing_keys_field_is_empty_not_error(self):
        """真·没有 key：200 + 没有 `keys` 字段 ⇒ `[]`（这是正常状态，不抛）。"""
        c = self._client(self._Resp(200, {"service": {}}))
        assert c.list_keys() == []

    def test_find_key_by_name(self):
        c = self._client(self._Resp(200, self.REAL))
        assert c.find_key(name="prod")["id"] == "msk_bbb"

    def test_find_key_by_id(self):
        c = self._client(self._Resp(200, self.REAL))
        assert c.find_key(key_id="msk_aaa")["name"] == "default"

    def test_find_key_missing_returns_empty_dict(self):
        """查询语义：找不到返回 `{}`，**不抛**（这是查询不是取数）。"""
        c = self._client(self._Resp(200, self.REAL))
        assert c.find_key(name="nope") == {}

    def test_find_key_id_wins_over_name(self):
        c = self._client(self._Resp(200, self.REAL))
        assert c.find_key(name="prod", key_id="msk_aaa")["name"] == "default"

    def test_usage_and_wallet(self):
        c = self._client(self._Resp(200, self.REAL))
        assert c.usage()["remainingToday"] == 500000000
        assert c.wallet()["balance"] == 100000000

    def test_usage_missing_returns_empty_dict(self):
        c = self._client(self._Resp(200, {}))
        assert c.usage() == {} and c.wallet() == {}


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
