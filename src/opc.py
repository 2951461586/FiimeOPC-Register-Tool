"""opc.fiime.cn 站点客户端 —— 注册 / 激活 / 登录 / 建 API Key（**纯 HTTP**）。

════════════════════════════════════════════════════════════════════
接口清单（2026-09-26 全部实测，不是从文档抄的）
════════════════════════════════════════════════════════════════════

  GET  /api/csrf-token                    -> {"token":"","headerName":"X-CSRF-Token"}
  GET  /api/settings/public               -> {registration:{closed,blockGmailAliases,captchaEnabled}, ...}
  GET  /api/auth/check-username?username= -> {"available":bool,"reason":""|"taken"|"format"}
  POST /api/auth/register                 -> 200 {user:{...}, needActivation:true, registrationTokenReward}
  POST /api/auth/activate   {"token":..}  -> 200 {user:{...}, registrationTokenReward}
  POST /api/auth/login {"account","password"} -> 200 {user:{...}} | 401 {code:"EMAIL_NOT_ACTIVATED",email}
  GET  /api/model-service                 -> {service:{enabled,apiBasePath,dailyTokenLimit,maxApiKeysPerUser,models:[...]}}
  POST /api/model-service/keys {"name":..}-> 200 {key:{id,name,masked,secret,status}, secret}
  GET  /api/model-service/v1/models       （Authorization: Bearer opc_sk_...）
  POST /api/model-service/v1/chat/completions

════════════════════════════════════════════════════════════════════
为什么整链**不需要浏览器**（与参考项目最大的架构差异）
════════════════════════════════════════════════════════════════════
参考项目（Intern-Register-Tool）必须起 Playwright，因为它的登录环节挂了
阿里云人机验证。本站点实测**三处都不需要**：

  1. **注册无验证码** —— `GET /api/settings/public` 实测返回
     `registration.captchaEnabled = false`。前端确实有 SVG 验证码逻辑
     （`/api/captcha/new` 返回 `{captchaId,svg,type}`，type ∈ judge/blank），
     但开关是关的，注册请求体里**不带** `captchaId/captchaAnswer` 也 200。
  2. **登录无验证码、无 2FA** —— 直接 `{"account","password"}` 就返回 user。
     响应里有 `needTotp` 字段（站点支持 2FA），但**新注册账号默认没开**。
  3. **密码是明文 POST** —— 前端不做 RSA/加密，实测请求体就是原始密码。

⇒ 所以依赖只有 `requests`，没有 playwright / cryptography。
   ⚠ 这不是"永久结论"：站点一开验证码，本链路立刻断（验证码是 SVG 图形题）。
     `config.py` 里留了检查点，`run.py` 会在开验证码时**拒绝开跑**并说明原因。

════════════════════════════════════════════════════════════════════
两层限流 —— 本项目最重要的结论
════════════════════════════════════════════════════════════════════
`POST /api/auth/register` 有两层**互相独立**的限制，都是 HTTP 429，
但**文案不同、处置完全不同**：

  ① 速率层：60 秒 1 次
     实测：连打 5 次 → 第 1 次 200，第 2~5 次 **0.2s 内**返回
           429 {"error":"注册过于频繁，请一分钟后再试"}
     ⇒ 退避重试即可（**同一出口**），不是失败。

  ② 配额层：同一 IP **每日**注册数上限
     实测：本机出口 128.241.231.18 注册若干次后返回
           429 {"error":"该 IP 今日注册数量已达上限，请明日再试或联系管理员"}
           而**同一时刻**换到 4 个槽位出口全部立刻 200。
     ⇒ 该出口当天判死，**只能换 IP**。退避重试毫无意义（要等明天）。

   🔴 把两者混为一谈的代价：把 ② 当 ① 去重试 = 对着一个已经判死的出口
      每 65 秒打一枪，一天 1300+ 次，每次都加深风控；把 ① 当 ② 去换 IP =
      白白烧掉一个干净出口的额度。所以 `classify_429()` 是**唯一判据点**，
      由文案分流，不允许调用方自己猜。

════════════════════════════════════════════════════════════════════
踩坑记录（都是实测撞出来的）
════════════════════════════════════════════════════════════════════
  · **CSRF token 是空串**：`/api/csrf-token` 返回 `{"token":""}`，且
    `POST /auth/register` 不带 `X-CSRF-Token` 也 200。但仍要**保持同一个
    Session（带 cookie）** —— 站点在响应里下发 `server_session_xxx` cookie，
    激活/登录环节依赖它。所以客户端全程用一个 `requests.Session`。
  · **蜜罐字段 `website` 必须传空串**，不能省略。实测传
    `"http://spam.com"` → 400 {"error":"注册失败，请刷新页面后重试"}。
  · **`accountType` 是枚举**：`personal|enterprise|community|alliance`。
    实测传 `nonsense` → 400。
  · **`agree` 必须为 true**，否则 400 且文案点名三个协议。
  · **密码必须含字母**：实测 `12345678` → 400 {"error":"密码必须包含字母"}。
  · **`blockGmailAliases: true`** —— gmail 的 `user+tag@gmail.com` 别名被拒。
    本项目用 CF Worker 自有域名，不受影响，但换邮箱源时要记得这条。
  · **API Key 前缀是 `opc_sk_`**，不是 `sk-`（参考项目的 `KEY_PREFIX` 不适用）。
  · **每账号最多 5 把 key**（`service.maxApiKeysPerUser = 5`）。
"""

import json
import random
import re
import string
import time
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlparse

import requests

from . import config
from .redact import mask_key

# ── 429 分类（**唯一判据点**）─────────────────────────────────────
LIMIT_RATE = "rate"  # 速率层：60s 1 次 → 同出口退避重试
LIMIT_IP_QUOTA = "ip_quota"  # 配额层：该 IP 今日已满 → 换出口
LIMIT_OTHER = "other"  # 其它 429（未知文案）→ 当速率处理，但记下来


def classify_429(text: str) -> str:
    """把 429 的响应文本分类成 `LIMIT_*`。

    🔴 这是**唯一**的 429 判据点。调用方（pipeline）只认返回值，
      不去 match 文案 —— 否则文案一改行为就悄悄变了，而 diff 里看不出来。

    ⚠ 未知文案归到 `LIMIT_OTHER`（当速率处理）而不是 `LIMIT_IP_QUOTA`：
      误判成"出口判死"会白白把干净出口冷却 6 小时；误判成"速率"只是多退避
      一次。两者代价不对称，所以往代价小的那边倒。
    """
    t = text or ""
    if config.IP_QUOTA_MARK in t:
        return LIMIT_IP_QUOTA
    if config.RATE_LIMIT_MARK in t:
        return LIMIT_RATE
    return LIMIT_OTHER


# ── 邮件正文里的 URL 提取 ─────────────────────────────────────────
# 🔴 用 **RFC 3986 的合法字符集**，不要写 `[^\s"'<>()]` 那种"排除法"。
#    排除法会一路吞到中文标点（`?token=abc。谢谢` 整串算一个匹配），
#    而 rstrip 只剪末尾 ⇒ 剪不掉 ⇒ token 被污染。显式字符集里没有中文，
#    从源头就不可能匹配进去。
_URL_RE = re.compile(
    r"https?://[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+",
    re.I,
)

# 🔴 站点**偶发**把激活链接发成 `.../activate?token<hex>`（漏了 `=`）——
#    实测一批 10 个账号里 2 个如此，且邮件原文的确就是 `t o k e n <hex>`
#    （不是我们截断的）。用它从整串里兜底捞 token。
_ACTIVATION_TOKEN_RE = re.compile(r"token=?([0-9a-f]{32,})", re.I)


def _invited_at_of(user: dict) -> int:
    """从服务端的 `user` 对象里取 `invitedAt`。

    🔴 这是"邀请码是否**真的**生效"的唯一判据。服务端**不校验**邀请码的
      存在性（实测无效码也返回 200），所以状态码给不出任何信息 ——
      只能看这个字段：0 = 未记录为受邀。

    ⚠ 用 `.get()` 而不是下标：字段缺失时返回 0（= 未受邀），
      这个方向的误判是**保守**的（把"未知"当"没成立"），
      反过来会把一次失败的推荐记成成功。
    """
    if not isinstance(user, dict):
        return 0
    try:
        return int(user.get("invitedAt") or 0)
    except (TypeError, ValueError):
        return 0


def _int_of(v, default: int = 0) -> int:
    """宽松取整：`None`/空/非数字一律回落 `default`。

    🔴 服务端字段是外部输入（也可能被中间层改写），一个坏值不该让整条链崩；
      而这些值只用于展示 / 兜底，坏值退化成默认值远好于抛异常。
    """
    if v is None or v == "":
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


# ── 结果类型 ──────────────────────────────────────────────────────
@dataclass
class RegisterResult:
    ok: bool = False
    need_activation: bool = False
    user: dict = field(default_factory=dict)
    token_reward: int = 0
    # 服务端原文（失败时是 `{"error": "..."}` 里的那句话）
    error: str = ""
    http_status: int = 0
    # 429 分类（取值见 LIMIT_*）；非 429 时为空串
    limit_kind: str = ""
    # **实际发出去的**邀请码（空 = 没带）。记它而不是只记配置值：
    # 请求体和配置可能不一致（比如调用方显式传了空串覆盖），
    # 台账里要留下"这次到底发了什么"。
    invite_code_sent: str = ""
    # 服务端回的 `user.invitedAt`（毫秒时间戳，0 = **未记录为受邀**）。
    # 🔴 这是"邀请码是否真的生效"的**唯一**判据 —— 服务端不校验码的存在性
    #    （实测无效码也返回 200），所以只能靠它事后核对。
    invited_at: int = 0


@dataclass
class ActivateResult:
    ok: bool = False
    user: dict = field(default_factory=dict)
    token_reward: int = 0
    error: str = ""
    http_status: int = 0
    # 同 `RegisterResult.invited_at`：0 = 未记录为受邀。
    invited_at: int = 0


@dataclass
class ResendResult:
    ok: bool = False
    message: str = ""
    error: str = ""
    http_status: int = 0


@dataclass
class LoginResult:
    ok: bool = False
    user: dict = field(default_factory=dict)
    error: str = ""
    http_status: int = 0
    # 站点返回的错误码，如 `EMAIL_NOT_ACTIVATED` / `ACCOUNT_NOT_FOUND`
    code: str = ""


@dataclass
class ApiKey:
    key: str = ""
    key_id: str = ""
    name: str = ""
    masked: str = ""
    status: str = ""


@dataclass
class ServiceInfo:
    enabled: bool = False
    api_base_path: str = "/api/model-service/v1"
    daily_token_limit: int = 0
    max_keys_per_user: int = 0
    # 服务端返回的是**对象列表**（`{"id","object","type","enabled",...}`），
    # 不是字符串列表 —— 早期调用方按字符串用会静默拿到 dict。
    models: list = field(default_factory=list)

    def model_ids(self, *, only_enabled: bool = True) -> list:
        """模型 id 列表。`models` 里混着 text/image/video 三类。"""
        out = []
        for m in self.models or []:
            if isinstance(m, str):
                out.append(m)
                continue
            if not isinstance(m, dict):
                continue
            if only_enabled and not m.get("enabled", True):
                continue
            mid = m.get("id")
            if mid:
                out.append(str(mid))
        return out

    def text_models(self) -> list:
        """只要 `type == "text"` 的 —— 这才是能进 `/v1/chat/completions` 的。"""
        out = []
        for m in self.models or []:
            if not isinstance(m, dict):
                continue
            if not m.get("enabled", True):
                continue
            if str(m.get("type") or "").lower() != config.MODEL_TYPE_TEXT:
                continue
            if m.get("id"):
                out.append(str(m["id"]))
        return out

    def default_model(self) -> str:
        """示例模型：优先服务端列表里的文本模型，回落到 `config.DEFAULT_MODEL`。"""
        tm = self.text_models()
        if config.DEFAULT_MODEL in tm:
            return config.DEFAULT_MODEL
        return tm[0] if tm else config.DEFAULT_MODEL

    def describe(self) -> str:
        return f"ok({len(self.model_ids())} models, max_keys={self.max_keys_per_user})"


# ── 客户端 ────────────────────────────────────────────────────────
class OpcClient:
    """站点客户端。**一个实例 = 一个出口 IP = 一个 Session**。

    🔴 并发场景下**不要复用实例**：Session 带着 cookie，而 cookie 是
      "某个账号的会话"。跨账号复用会让激活/登录串号 —— 而且是静默的
      （服务端只看到"带着 A 的 cookie 去激活 B"，报出来的错跟 cookie 无关）。

    `proxy` 传具体值时只作用于这个实例（槽位池并发必须这样用 ——
    改全局 `config.PROXY` 在多个 producer 之间会互相踩）。
    """

    def __init__(self, *, proxy: str | None = None, timeout: float | None = None, log=print):
        self.timeout = timeout or config.REQUEST_TIMEOUT
        self.log = log
        self.proxy = proxy
        self.session = requests.Session()
        config.apply_proxy(self.session, proxy)
        self.session.headers.update(
            {
                "Accept": "application/json",
                "Accept-Language": "zh-CN",
                "User-Agent": config.USER_AGENT,
                "Origin": config.SITE_BASE,
                "Referer": f"{config.SITE_BASE}/register",
            }
        )
        self.csrf = ""

    # ── 底层请求 ──────────────────────────────────────────────
    def _url(self, path: str) -> str:
        return path if path.startswith("http") else config.API_BASE + path

    def _csrf_headers(self, write: bool) -> dict:
        # 🔴 实测 token 是空串、且不校验，但仍按站点前端的契约带上 ——
        #    哪天站点把 CSRF 打开，这里不用改。空值不发头（避免 `X-CSRF-Token: `）。
        return {"X-CSRF-Token": self.csrf} if (write and self.csrf) else {}

    def _get(self, path: str, params: dict = None, *, timeout: float = None) -> requests.Response:
        return self.session.get(
            self._url(path),
            params=params,
            headers=self._csrf_headers(False),
            timeout=timeout or self.timeout,
        )

    def _post(self, path: str, body: dict = None, *, timeout: float = None) -> requests.Response:
        r = self.session.post(
            self._url(path),
            json=body,
            headers=self._csrf_headers(True),
            timeout=timeout or self.timeout,
        )
        # 站点在响应头里回传轮换后的 token（`x-csrf-token`），收下。
        tok = r.headers.get("x-csrf-token")
        if tok:
            self.csrf = tok
        return r

    @staticmethod
    def _err_of(r: requests.Response) -> str:
        """从响应体里取服务端的错误文案。

        🔴 站点失败时返回 `{"error": "..."}`（实测），不是参考项目那种
          `{success, msgCode, msg}` 结构。取不到就退回 HTTP 状态码 ——
          绝不能返回空串，否则日志里只会看到 "register failed:" 后面什么都没有。
        """
        try:
            j = r.json()
        except ValueError:
            return f"HTTP {r.status_code}"
        if isinstance(j, dict):
            for k in ("error", "message", "msg"):
                v = j.get(k)
                if isinstance(v, str) and v:
                    return v
        return f"HTTP {r.status_code}"

    # ── 站点状态 ──────────────────────────────────────────────
    def fetch_csrf(self) -> str:
        r = self._get("/csrf-token")
        if r.ok:
            self.csrf = str((r.json() or {}).get("token") or "")
        return self.csrf

    def public_settings(self) -> dict:
        """`GET /api/settings/public`。返回整个 dict（调用方自己取需要的段）。"""
        r = self._get("/settings/public")
        r.raise_for_status()
        return r.json() or {}

    def registration_state(self) -> dict:
        """注册相关的开关：`{closed, notice, blockGmailAliases, captchaEnabled}`。"""
        return self.public_settings().get("registration") or {}

    # ── 用户名 ────────────────────────────────────────────────
    def check_username(self, username: str) -> dict:
        """`{"available":bool,"reason":""|"taken"|"format"}`。网络异常时抛。"""
        r = self._get("/auth/check-username", params={"username": username})
        r.raise_for_status()
        return r.json() or {}

    def gen_available_username(self, attempts: int = 8) -> str:
        """生成一个**服务端确认可用**的用户名。

        🔴 必须真的查一次，不能只靠本地随机：
          用户名撞车时服务端返回的是 `{"available":false,"reason":"taken"}`，
          而注册接口对**已占用**用户名的报错文案与"格式错"是同一句
          （前端也是靠 `check-username` 提前拦的），事后分不清。
        """
        last = ""
        for _ in range(attempts):
            last = config.USERNAME_PREFIX + "".join(random.choices(string.digits, k=6))
            try:
                if self.check_username(last).get("available"):
                    return last
            except requests.RequestException:
                # 查重接口偶发失败不该让整个账号作废 —— 直接用这个候选名
                # 去试注册，注册本身会给出最终结论。
                return last
        return last

    # ── 注册 ──────────────────────────────────────────────────
    def register(
        self,
        username: str,
        email: str,
        password: str,
        *,
        account_type: str = None,
        invite_code: str = "",
    ) -> RegisterResult:
        """`POST /api/auth/register`。

        🔴 调用方**必须**处理 `limit_kind`：
          · `LIMIT_RATE`     → 等 60s 重试（同一出口）
          · `LIMIT_IP_QUOTA` → 这个出口今天完了，换 IP
          把它当普通失败会让整批假失败；无脑重试 IP 配额则会加深风控。

        `invite_code`：邀请码。**先经 `config.normalize_invite_code()` 规范化**
        （去空白 + 转大写），非空才写进请求体 —— 空值时**整个键省略**，
        与前端 `inviteCode: n.inviteCode || void 0` 的行为一致。

        🔴 **服务端不校验邀请码是否存在**（2026-09-26 实测：形状合法但不存在
          的码照样 HTTP 200，账号正常建出）。所以"码写错了"**不会**报错，
          只会让推荐关系静默不成立 —— 判据是响应里的 `user.invitedAt`，
          不是状态码。`RegisterResult.invited_at` 就是取这个字段。
        """
        code = config.normalize_invite_code(invite_code)
        at = account_type or config.ACCOUNT_TYPE
        body = {
            "username": username,
            "email": email,
            "password": password,
            "agree": True,
            "accountType": at,
            # 🔴 蜜罐字段：**必须传空串**，省略或传值都会 400（实测）。
            "website": "",
        }
        if code:
            body["inviteCode"] = code
        r = self._post("/auth/register", body)
        res = RegisterResult(http_status=r.status_code, invite_code_sent=code)
        if r.status_code == 429:
            res.error = self._err_of(r)
            res.limit_kind = classify_429(res.error)
            return res
        if not r.ok:
            res.error = self._err_of(r)
            return res
        j = r.json() or {}
        res.ok = True
        res.need_activation = bool(j.get("needActivation"))
        res.user = j.get("user") or {}
        res.token_reward = _int_of(j.get("registrationTokenReward"))
        res.invited_at = _invited_at_of(res.user)
        return res

    # ── 激活 ──────────────────────────────────────────────────
    @staticmethod
    def token_from_activation_url(url: str) -> str:
        """从 `https://opc.fiime.cn/activate?token=<hex>` 里取 token。

        🔴 只认 query 里的 `token`，**不要**拿整条 URL 去 POST ——
          实测激活接口收的是 `{"token": "<hex>"}`，不是完整链接。

        ⚠ 站点偶尔漏掉 `=`（实测 2/10，邮件原文就是 `?token<hex>`）。此时
          `parse_qs` 解析不出 token —— 退一步用正则捞 `token` 后的 hex，
          避免因站点模板缺陷白掉一个**已经注册成功**的账号。
        """
        tok = (parse_qs(urlparse(url).query).get("token") or [""])[0]
        if tok:
            return tok
        m = _ACTIVATION_TOKEN_RE.search(url)
        return m.group(1) if m else ""

    def activate(self, token: str) -> ActivateResult:
        r = self._post("/auth/activate", {"token": token})
        res = ActivateResult(http_status=r.status_code)
        if not r.ok:
            res.error = self._err_of(r)
            return res
        j = r.json() or {}
        res.ok = True
        res.user = j.get("user") or {}
        res.token_reward = _int_of(j.get("registrationTokenReward"))
        res.invited_at = _invited_at_of(res.user)
        return res

    def activate_from_url(self, url: str) -> ActivateResult:
        token = self.token_from_activation_url(url)
        if not token:
            return ActivateResult(error=f"激活链接里没有 token：{url[:120]}")
        return self.activate(token)

    def resend_activation(self, email: str) -> ResendResult:
        """`POST /api/auth/resend-activation` —— 重发激活邮件。

        实测响应：`200 {"message": "若该邮箱存在未激活账号，激活邮件已发送"}`
        （无论邮箱是否存在都回同一句，防用户枚举）。

        🔴 **每次重发都会作废上一个激活 token** —— 重发后必须用**最新**那封
          邮件里的链接。实测拿旧 token 去激活会
          `400 {"error":"激活链接无效或已使用"}`。
        """
        r = self._post("/auth/resend-activation", {"email": email})
        res = ResendResult(http_status=r.status_code)
        if r.ok:
            res.ok = True
            try:
                res.message = str((r.json() or {}).get("message") or "")
            except ValueError:
                res.message = ""
        else:
            res.error = self._err_of(r)
        return res

    # ── 登录 ──────────────────────────────────────────────────
    def login(self, account: str, password: str) -> LoginResult:
        """`POST /api/auth/login`。新注册账号无 2FA、无验证码。

        ⚠ 站点返回的 `needTotp` 为真时说明这个账号开了两步验证 ——
          本项目**不支持**（那需要 TOTP secret，注册流程拿不到）。
          如实报错，不要假装成功。
        """
        r = self._post("/auth/login", {"account": account, "password": password})
        res = LoginResult(http_status=r.status_code)
        try:
            j = r.json() or {}
        except ValueError:
            j = {}
        if not r.ok:
            res.error = self._err_of(r)
            res.code = str(j.get("code") or "")
            return res
        if j.get("needTotp"):
            res.error = "账号已开启两步验证（needTotp），本工具不支持"
            res.code = "NEED_TOTP"
            return res
        user = j.get("user")
        if not user:
            res.error = "登录响应里没有 user 字段"
            return res
        res.ok = True
        res.user = user
        return res

    # ── 模型服务 ──────────────────────────────────────────────
    def model_service(self) -> dict:
        """`GET /api/model-service` 的**完整**响应（需要登录 cookie）。

        🔴 这个端点一次返回**全部**账号侧信息，顶层键实测：

            service          服务开关 / apiBasePath / dailyTokenLimit /
                             maxApiKeysPerUser / models
            wallet           {balance, totalEarned, totalSpent}
            usage            {date, usedToday, dailyLimit, remainingToday}
            keys             🔴 **该账号的全部 API Key，含明文 `secret`**
            invite / giftPool / giftLeaderboard / benefits /
            partnership / ranking

        🔴 **`keys[].secret` 是明文。** 站点 UI 写着「这串密钥只显示一次」，
          但那是**前端**的说法 —— 后端每次 GET 都把它原样吐回来（实测）。
          两个后果：
            · 好消息：key 丢了可以**捞回来**（`list_keys()`），不必删掉重建；
            · ⚠ 安全面：任何拿到会话 cookie 的人都能读走全部 key。
          所以「建完立刻落台账」仍然要做，但理由从"丢了就没了"变成
          "少一次往返、少一个出错窗口"。
        """
        r = self._get("/model-service")
        r.raise_for_status()
        return r.json() or {}

    def service_info(self) -> ServiceInfo:
        j = self.model_service()
        s = j.get("service") or {}
        return ServiceInfo(
            enabled=bool(s.get("enabled")),
            api_base_path=str(s.get("apiBasePath") or config.MODEL_V1_PATH),
            daily_token_limit=_int_of(s.get("dailyTokenLimit")),
            max_keys_per_user=_int_of(s.get("maxApiKeysPerUser")),
            models=s.get("models") or [],
        )

    def usage(self) -> dict:
        """`{date, usedToday, dailyLimit, remainingToday}` —— 这个账号今天还剩多少 token。"""
        return self.model_service().get("usage") or {}

    def wallet(self) -> dict:
        """`{balance, totalEarned, totalSpent}` —— token 余额。"""
        return self.model_service().get("wallet") or {}

    def list_keys(self, *, strict: bool = True) -> list:
        """列出账号下的全部 API Key。**每条的 `secret` 是明文**（见 `model_service`）。

        🔴 **不是** `GET /api/model-service/keys` —— 那个路径**不存在**
          （实测 404 + text/html；服务端只在同一路径上注册了 POST）。
          早期实现打的正是它，并且 `if not r.ok: return []` 把 404 静默变成
          "这个账号没有 key" —— 而调用方无从分辨"真的没有"和"端点打错了"。
          这是本项目最忌讳的那类失败：**报错被翻译成了正常结果**。

        🔴 所以默认 `strict=True`：拿不到就抛，不返回空列表。
          真要"拿不到当没有"（例如探测脚本），显式传 `strict=False`。
        """
        r = self._get("/model-service")
        if not r.ok:
            if strict:
                raise RuntimeError(
                    f"list_keys 失败：HTTP {r.status_code}（"
                    f"{(r.headers.get('content-type') or '?')}）"
                    f" —— 注意 `/model-service/keys` 这个路径不存在，"
                    f"key 列表在 `GET /api/model-service` 的 `keys` 字段里"
                )
            return []
        return list((r.json() or {}).get("keys") or [])

    def find_key(self, *, name: str = "", key_id: str = "") -> dict:
        """按名字或 id 找一把 key。找不到返回 `{}`（**不抛**，这是查询不是取数）。"""
        for k in self.list_keys():
            if key_id and str(k.get("id") or "") == key_id:
                return k
            if name and str(k.get("name") or "") == name:
                return k
        return {}

    def create_key(self, name: str = "default") -> ApiKey:
        """`POST /api/model-service/keys` → 返回带 `secret` 的 key。

        🔴 建完**立刻**落台账。⚠ 但理由不是"丢了就没了" —— 实测
          `GET /api/model-service` 的 `keys[].secret` 会**原样返回明文**，
          丢了其实能捞回来（见 `model_service` / `list_keys`）。
          立刻落盘的理由是：少一次往返、少一个"跑到一半崩了"的窗口。
        """
        r = self._post("/model-service/keys", {"name": name})
        if not r.ok:
            raise RuntimeError(f"create_key failed: {self._err_of(r)}")
        j = r.json() or {}
        k = j.get("key") or {}
        secret = str(j.get("secret") or k.get("secret") or "")
        if not secret:
            raise RuntimeError(f"create_key 响应里没有 secret：{json.dumps(j)[:200]}")
        return ApiKey(
            key=secret,
            key_id=str(k.get("id") or ""),
            name=str(k.get("name") or name),
            masked=str(k.get("masked") or mask_key(secret)),
            status=str(k.get("status") or "active"),
        )

    def delete_key(self, key_id: str) -> bool:
        r = self.session.delete(
            self._url(f"/model-service/keys/{key_id}"),
            headers=self._csrf_headers(True),
            timeout=self.timeout,
        )
        return r.ok

    # ── API Key 校验（OpenAI 兼容面）───────────────────────────
    def api_base(self) -> str:
        """推理面的 base URL（`https://opc.fiime.cn/api/model-service/v1`）。

        🔴 必须**带 `/api`** —— 少了它站点会返回 SPA 兜底页（HTTP 200 + HTML），
          看着像"端点存在但没生效"，实际是地址错了。见 `verify_key`。
        """
        return config.SITE_BASE + config.MODEL_V1_PATH

    @staticmethod
    def verify_key(
        api_key: str,
        *,
        base: str = None,
        timeout: float = None,
        attempts: int = 1,
        delay: float = 3.0,
    ) -> tuple:
        """用 key 打一次 `GET /v1/models`，返回 `(ok, 说明)`。

        🔴 **不走 Session、不带 cookie、不带代理** —— 这是刻意与账号侧
          隔离的"外部视角"校验：如果 key 只在带 cookie 时能用，那它就
          不是一个真的 API Key（第三方调用方没有我们的 cookie）。
          用独立 `requests.get` 保证这一点。

        🔴 **判据是 content-type + 能否解析出 `data` 列表，不是状态码**
          （2026-09-26 本项目踩到，代价是一次批量跑完 3 个账号的校验全"失败"）：

          本站是 SPA。请求一个**不存在的路径**（例如漏了 `/api` 前缀）时，
          前端路由会兜底返回 **HTTP 200 + 6707 字节的 index.html**。
          于是"状态码 200"看着完全正常，实际上根本没打到 API。

          实测对照：
            GET https://opc.fiime.cn/model-service/v1/models      → 200, HTML, 6707B  ✗
            GET https://opc.fiime.cn/api/model-service/v1/models  → 200, JSON, 1880B  ✓

          ⇒ 所以这里把"2xx 但 content-type 不是 JSON"单独报出来，
            并且**在说明里带上字节数** —— 那个数字是识别"被 SPA 兜底"的指纹。

        ⚠ 401 是"key 无效"（确定失败，不重试）；2xx + 非 JSON 是"地址错了或
          边缘层没同步"（重试有意义，但更像地址错 —— 先看说明里的字节数）。
        """
        url = (base or (config.SITE_BASE + config.MODEL_V1_PATH)) + "/models"
        last = ""
        for i in range(max(1, attempts)):
            try:
                r = requests.get(
                    url,
                    headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
                    timeout=timeout or config.REQUEST_TIMEOUT,
                )
            except requests.RequestException as ex:
                last = f"network: {type(ex).__name__}"
            else:
                ctype = (r.headers.get("content-type") or "").lower()
                if r.status_code == 401:
                    # 明确无效 —— 重试无意义，立刻返回。
                    return False, "401（key 无效或已停用）"
                if not r.ok:
                    last = f"HTTP {r.status_code}"
                elif "json" not in ctype:
                    # 🔴 SPA 兜底页 / 边缘层页面 —— 这**不是**一个成功的校验。
                    last = (
                        f"HTTP {r.status_code} 但 content-type={ctype or '?'}"
                        f"（{len(r.text)} 字节）—— 多半打到了 SPA 兜底页，"
                        f"检查 base_url 是否漏了 /api"
                    )
                else:
                    try:
                        data = (r.json() or {}).get("data") or []
                    except ValueError:
                        last = f"content-type 是 JSON 但解析失败（{len(r.text)} 字节）"
                    else:
                        return True, f"ok({len(data)} models)"
            if i < attempts - 1:
                time.sleep(delay)
        return False, last or "unknown"

    @classmethod
    def wait_until_active(
        cls, api_key: str, *, attempts: int = 3, delay: float = 2.0, **kw
    ) -> tuple:
        """新建 key 的校验封装。

        ⚠ 早期这里写着"新建 key 有 ~10s 网关传播延迟"，**那个结论是错的** ——
          真正原因是 base_url 漏了 `/api`，打到了 SPA 兜底页。修正之后实测
          key **建出来即可用**，没有可测的传播延迟。
          重试保留着只是因为边缘层偶发抖动，`attempts` 因此可以取小。
        """
        return cls.verify_key(api_key, attempts=attempts, delay=delay, **kw)

    @staticmethod
    def probe_key(
        api_key: str, *, model: str = "opc-txt-v1", base: str = None, timeout: float = None
    ) -> tuple:
        """用 key 真发一次 `POST /v1/chat/completions`，返回 `(ok, 说明)`。

        ⚠ 这会**真的消耗 Token 余额**。默认不跑，只有 `--probe-key` 才跑。
        """
        url = (base or (config.SITE_BASE + config.MODEL_V1_PATH)) + "/chat/completions"
        try:
            r = requests.post(
                url,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": "ping"}],
                    "max_tokens": 8,
                },
                timeout=timeout or config.REQUEST_TIMEOUT,
            )
        except requests.RequestException as ex:
            return False, f"network: {type(ex).__name__}"
        if not r.ok:
            return False, f"HTTP {r.status_code}: {r.text[:120]}"
        return True, "ok"


# ── 凭据生成 ──────────────────────────────────────────────────────
_PW_PREFIX = "Lz#"


def gen_password(length: int = None) -> str:
    """生成符合站点规则的密码。**保证**大小写字母 / 数字 / 符号三类齐全。

    🔴 必须**含字母**：实测纯数字 `12345678` 被服务端拒
      （400 {"error":"密码必须包含字母"}）。

    🔴 三类齐全必须是**构造出来的**，不能靠"随机抽到就算" ——
      早期实现是 `"Lz#" + random(letters+digits, 9)`，9 位里一个数字都没有的
      概率约 **21.6%**（(52/62)^9），而 docstring 却宣称"保证三类齐全"。
      测试跑 50 次必然撞上，暴露了这个假保证。现在改成先钉死必需的字符、
      再补位、最后打散，长度与组成都是确定的。

    ⚠ 最小长度 5（`Lz#` + 至少一个数字 + 至少一个小写字母）；传更小的值会被
      抬到 5 并**不报错** —— 密码长度不是本站的校验点，静默兜底比抛异常合适。
    """
    n = _int_of(length or config.PASSWORD_LEN, config.PASSWORD_LEN)
    n = max(n, len(_PW_PREFIX) + 2)
    body_len = n - len(_PW_PREFIX)
    # 先放"必须有"的，再用普通字符补满，最后 shuffle 打散位置特征。
    required = [random.choice(string.digits), random.choice(string.ascii_lowercase)]
    pool = string.ascii_letters + string.digits
    body = required + [random.choice(pool) for _ in range(body_len - len(required))]
    random.shuffle(body)
    return _PW_PREFIX + "".join(body)


def find_activation_url(text: str, *, host: str = None) -> str | None:
    """从邮件正文里找出激活链接。

    🔴 **这一步不能用 Worker 的 `extracted_json`** —— 实测它对本站的激活邮件
      返回**空数组** `[]`（发件人是 `rockssr@163.com`，不是 Worker 熟悉的那几家）。
      必须自己从 body 里正则捞。

    判据：URL 的 path 含 `activate`，且 host 匹配站点域名（防止把邮件里
    别的链接——比如退订、logo、社交账号——当激活链接）。
    实测邮件正文里同一个链接出现 **4 次**（纯文本版 + HTML 版 + 按钮 href），
    去重后只有一条。

    🔴 **按 RFC 3986 字符集匹配，不要用"排除空白和引号"那种写法**
      （2026-09-26 踩到）：正文里 URL 后面直接跟中文标点时，
      `[^\\s"'<>()]` 会把 `。谢谢` 一并吞进匹配结果，而 `rstrip` 只剪**末尾**、
      末尾是「谢」所以**一个字都剪不掉** —— token 被污染成
      `abc123。谢谢`，激活必然 400，且报错完全看不出是这个原因。
      改成显式的 URL 合法字符集后，中文标点天然不参与匹配，剪不剪都干净。
    """
    if not text:
        return None
    want_host = (host or urlparse(config.SITE_BASE).hostname or "").lower()
    for m in _URL_RE.finditer(text):
        url = m.group(0).rstrip(".,;:!?、。，；：！？）】」』\"'")
        try:
            p = urlparse(url)
        except ValueError:
            continue
        if want_host and want_host not in (p.hostname or "").lower():
            continue
        if "activate" not in p.path.lower():
            continue
        return url
    return None
