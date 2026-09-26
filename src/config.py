"""全局配置。

所有密钥类信息集中在此，可用环境变量或项目根 `.env` 覆盖（环境变量优先）。

🔴 凭据**不写死在代码里** —— 首次使用 `cp .env.example .env` 填真实值，
   `.env` 已在 .gitignore 中。

════════════════════════════════════════════════════════════════════
本文件记录的全部常量都是 2026-09-26 对 opc.fiime.cn 的**实测值**，
不是从文档抄的。每条后面都写了"怎么测出来的"。
════════════════════════════════════════════════════════════════════
"""

import os
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    """极简 `.env` 解析（stdlib，不引 python-dotenv）。

    只填「尚不存在于 os.environ」的键 —— 真实环境变量优先级更高，
    这样临时覆盖（`FO_XXX=1 python run.py`）依然生效。
    """
    if not path.is_file():
        return
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


_load_dotenv(Path(__file__).resolve().parents[1] / ".env")

# ── 目标站点 ──────────────────────────────────────────────────────
# 站点根。API 前缀实测是 `/api`（前端 `src` 里写死 `basePath:"/api"`）。
SITE_BASE = os.getenv("FO_SITE_BASE", "https://opc.fiime.cn").rstrip("/")
API_BASE = f"{SITE_BASE}/api"

# 模型服务（API Key 的真实用途）——`/api/model-service` 管理面，
# `/api/model-service/v1` 是 OpenAI 兼容面。
#
# 🔴 路径**必须带 `/api` 前缀**。踩过的坑（2026-09-26，本项目自己踩的）：
#    少了 `/api` 时 `https://opc.fiime.cn/model-service/v1/models` 返回
#    **HTTP 200 + 6707 字节的 SPA index.html**（前端路由兜底），
#    于是"状态码 200"看着完全正常，实际上根本没打到 API。
#    ⇒ 本站是 SPA，**HTTP 200 不能作为"端点存在"的判据**，
#      必须同时检查 `content-type` 是不是 JSON（见 `opc.verify_key`）。
MODEL_SERVICE_PATH = "/api/model-service"
MODEL_V1_PATH = "/api/model-service/v1"

# 报告里"怎么调"给的示例模型。
# 🔴 用**文本**模型，不要用第一个：服务端返回的 models 顺序是
#    `opc-txt-v1, opc-image-v1, opc-video-v1, ...`，把 image/video 模型塞进
#    chat/completions 的示例里会让读者照着抄就报错。
DEFAULT_MODEL = os.getenv("FO_DEFAULT_MODEL", "opc-txt-v1")
# 管理面里同一模型有 `type` 字段（text/image/video）—— 用于筛出文本模型。
MODEL_TYPE_TEXT = "text"

# 每账号最多能建几把 API Key。
# ⚠ 这**只是报告文案用的兜底值**（实测服务端 `maxApiKeysPerUser=5`）。
#   权威值以 `ServiceInfo.max_keys_per_user` 为准 —— 站点改了这里不会自动跟。
MAX_KEYS_PER_USER = int(os.getenv("FO_MAX_KEYS_PER_USER", "5"))


# ── 邮箱源（可插拔）──────────────────────────────────────────────
# 目前只实现 `worker`（CF Worker 临时邮箱）。保留开关是为了让将来加
# IMAP / 供应商账号池时**不用改 pipeline** —— 与参考项目同一个约定。
MAILBOX_KIND = os.getenv("FO_MAILBOX_KIND", "worker").strip().lower() or "worker"
MAILBOX_KINDS = ("worker",)

# CF Worker 临时邮箱。三项都**默认空**，缺失时由 `validate()` 在入口报错
# —— 不写死默认值，因为本仓库可能被公开（参考项目的血泪教训）。
WORKER_BASE = os.getenv("FO_WORKER_BASE", "").rstrip("/")
WORKER_ADMIN_TOKEN = os.getenv("FO_WORKER_ADMIN_TOKEN", "")
WORKER_DOMAIN = os.getenv("FO_WORKER_DOMAIN", "")

# 🔴 收信必须走 `/api/inbox?email=`，**不要**用 `/admin/all`。
#    实测（参考项目 2026-09-19，同一个 Worker）：`/admin/all` 是「最新 N 条」
#    全表窗口，而 D1 retention 只留 100 行，同机别的项目以 21.6 封/小时灌表
#    ⇒ 整表每 4.6 小时被冲刷一遍，我们的激活信一旦被挤出窗口就**永远读不到**。
#    `/api/inbox` 按收件人索引查，别人的邮件挤不掉我们的。
#    ⚠ 本项目的 Worker 实测 `/api/inbox` 正常工作（2026-09-26）。
MAIL_POLL_INTERVAL = float(os.getenv("FO_MAIL_POLL_INTERVAL", "2.0") or 2.0)
MAIL_POLL_TIMEOUT = float(os.getenv("FO_MAIL_POLL_TIMEOUT", "120") or 120)

# 🔴 激活邮件的发件人域名。实测发件人是 `rockssr@163.com`（主题「激活你的
#    FiimeOPC 账号」）。用来过滤邮件，避免把 Worker 里其它项目的信读错。
#    ⚠ 但**不能**把它当硬条件：发信域名将来可能换。所以做成"优先匹配，
#    匹配不到就退回任意收件人匹配"，见 tempmail.wait_for_mail。
MAIL_SENDER_HINT = os.getenv("FO_MAIL_SENDER_HINT", "rockssr").strip()


# ── 站点请求 ──────────────────────────────────────────────────────
REQUEST_TIMEOUT = float(os.getenv("FO_REQUEST_TIMEOUT", "30") or 30)

USER_AGENT = os.getenv(
    "FO_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36",
)

# ── 注册约束（全部实测）──────────────────────────────────────────
#
# 🔴 站点有**两层**注册限制，必须分开处理 —— 这是本项目最重要的结论：
#
#   ① 速率层：`POST /auth/register` 60 秒 1 次。
#      实测：连打 5 次，第 1 次 200，第 2~5 次立刻（0.2s）返回
#            429 {"error":"注册过于频繁，请一分钟后再试"}
#      → 等 60s 就能继续，**换个 IP 也绕不过**（它按 IP 计）。
#
#   ② 配额层：同一 IP **每日**注册数有上限，触顶后当天不再放行。
#      实测：本机出口 128.241.231.18 在注册若干次后返回
#            429 {"error":"该 IP 今日注册数量已达上限，请明日再试或联系管理员"}
#            而**同一时刻**换到槽位出口（203.0.113.11 等 4 个 IP）
#            全部立刻 200 成功。
#      → 这是 **IP 维度**的硬墙，**只能靠换出口 IP 绕过**。
#        这一条实测就是本项目必须带代理池的原因（与参考项目同构）。
#
# 两种 429 的**文案不同、处置完全不同**，所以 pipeline 按文案分流：
#   含「过于频繁」   → 速率 → 退避重试（同一出口）
#   含「已达上限」   → 配额 → 该出口判死，换 IP
RATE_LIMIT_MARK = "过于频繁"
IP_QUOTA_MARK = "已达上限"

# 两次注册之间的最小间隔（秒）。站点要求 60s，这里取 62s 留余量。
# ⚠ 这是**每个出口**各自的间隔，不是全局 —— 池化模式下 6 个出口
#    可以各自 62s 一次，理论吞吐 6/62s ≈ 0.097 账号/秒。
REG_MIN_INTERVAL = float(os.getenv("FO_REG_MIN_INTERVAL", "62") or 62)

# 撞到速率限流后的退避重试次数与基数（秒）。
# 🔴 必须重试：429 是"慢点再来"，不是"注册失败"。当失败处理会让整批假失败。
#    实测 429 在 0.2s 内返回，服务端没有 Retry-After 头，只能自己退避。
REG_RATE_RETRY = int(os.getenv("FO_REG_RATE_RETRY", "2") or 2)
REG_RATE_BACKOFF = float(os.getenv("FO_REG_RATE_BACKOFF", "65") or 65)

# 用户名/密码规则（实测）。
#   用户名：前端做 `^[a-zA-Z0-9_]{3,20}$` 类校验；服务端对重复名返回
#           `{"available":false,"reason":"taken"}`，格式错返回 reason="format"。
#   密码：实测弱密码 `12345678` → 400 {"error":"密码必须包含字母"}。
#         所以生成器**必须**混字母。长度取 12。
USERNAME_PREFIX = os.getenv("FO_USERNAME_PREFIX", "lz")
PASSWORD_LEN = int(os.getenv("FO_PASSWORD_LEN", "12") or 12)

# 账号类型（实测枚举，来自 Register chunk 的 `be` 数组）。
# ⚠ 传枚举外的值会被服务端拒（实测 badtype → 400）。
ACCOUNT_TYPE = os.getenv("FO_ACCOUNT_TYPE", "personal").strip() or "personal"
ACCOUNT_TYPES = ("personal", "enterprise", "community", "alliance")

# ── 注册邀请码（**统一注入**）────────────────────────────────────
#
# 请求体字段名实测是 **`inviteCode`**（来自 Register chunk：
#   `xe("/auth/register", {username, email, password, agree:!0,
#      accountType, inviteCode: n.inviteCode || void 0, website})`）。
# 🔴 空值时前端发的是 `void 0` —— **整个键省略**，不是空串。本实现一致
#    （`opc.register()` 只在非空时才写 `inviteCode`）。
# 前端还有 `?invite=<code>` 的 URL 参数写法（`u.get("invite")`），本工具不用。
#
# 🔴🔴 **服务端不校验邀请码是否存在**（2026-09-26 实测，这是本节最重要的一条）：
#   用形状合法但必然不存在的码 `ZZZZZZZZ` 注册 → **HTTP 200，账号正常建出**，
#   没有任何报错。所以"码打错了"**不会**让注册失败 ——
#   它只会让推荐关系**静默不成立**（激活响应里 `user.invitedAt` 保持 0）。
#
#   ⇒ 结论：邀请码不可能靠"注册失败"发现问题，只能靠事后核对
#     `invitedAt`。所以本工具把它**记进台账**（`AccountRecord.invite_code`
#     / `invited_at`）并在报告里打出来 —— 否则整批注册用错了码，
#     从头到尾不会有任何异常。
#
# 🔴 另一个实测差异：**带邀请码时注册响应的 `registrationTokenReward` 是 0**，
#   奖励延后到**激活**时才发（有效码激活后 `registrationTokenReward=100000000`）。
#   不带码时注册就直接给 100000000。
#   ⇒ 所以"注册奖励为 0"**不是**错误信号，别拿它判断码是否有效。
#
# 码的形态：8 位大写字母数字（实测 `GET /api/invite` 返回形如 `R5AZGYNU`）。
# 这里只做**规范化**（去空白 + 转大写）和**长度/字符集**校验 ——
# 存在性无法在本地校验，别假装能。
INVITE_CODE = os.getenv("FO_INVITE_CODE", "").strip().upper()
# 前端 `maxLength={32}`；超过这个长度的值服务端行为未知，直接拒掉更安全。
INVITE_CODE_MAXLEN = 32
# 每次邀请的 token 奖励（实测 `GET /api/model-service/campaign` 的
# `tokensPerInvite`）。仅用于报告里说明"这个码值多少"，不参与逻辑。
INVITE_TOKENS_PER_INVITE = 100000000


def normalize_invite_code(raw: str) -> str:
    """规范化邀请码：去空白 + 转大写。空值返回空串（= 不使用邀请码）。

    🔴 **只做规范化 + 格式校验，不做存在性校验** —— 存在性服务端都不查
      （见上面那段），本地更不可能查。别在这里假装能验出"码是不是真的"。
    """
    code = (raw or "").strip().upper()
    if not code:
        return ""
    if len(code) > INVITE_CODE_MAXLEN:
        raise ValueError(
            f"邀请码过长（{len(code)} > {INVITE_CODE_MAXLEN}）：{code[:16]}…\n"
            f"  前端 maxLength=32，超过这个长度的值服务端行为未知。")
    if not code.isalnum():
        bad = sorted({c for c in code if not c.isalnum()})
        raise ValueError(
            f"邀请码含非字母数字字符 {bad}：{code!r}\n"
            f"  实测码的形态是 8 位大写字母数字（形如 R5AZGYNU）。\n"
            f"  ⚠ 服务端**不校验**邀请码，所以写错的码不会报错 ——"
            f"只会让推荐关系静默不成立。这里拦下来是为了别让它静默发生。")
    return code

# 站点是否开验证码 —— **实测关闭**（`registration.captchaEnabled=false`）。
# 保留这个开关是为了"哪天站点开了验证码"能立刻看出来：
# `probe` 子命令会打印它，pipeline 在开启时会直接拒绝开跑并提示。
# ⚠ 真开了验证码，纯协议链路就断了（验证码是 SVG 图形题，需要人/OCR）。
#   届时本工具的定位会退化成"半自动"，见 README「验证码假设」。


# ── 代理池（绕 IP 维度每日配额）──────────────────────────────────
# 🔴 不是可选优化 —— 见上面 ② 的实测。没有池子，单 IP 一天就只能注册几个。
#
# 格式（与参考项目一致）：一行一个 URL，或逗号分隔；`#` 开头为注释。
IR_PROXY_SLOTS = os.getenv("FO_PROXY_SLOTS", "").strip()
IR_PROXY_SLOTS_FILE = os.getenv("FO_PROXY_SLOTS_FILE", "").strip()

# 单条全局代理（不配池子时的退路，行为与参考项目 `IR_PROXY` 一致）。
PROXY = os.getenv("FO_PROXY", "").strip()

# 槽位被判"这个出口的每日配额已满"后冷却多久（秒）。
# ⚠ 每日配额是**当天**恢复的，所以冷却不该短于"跑到明天"。
#   但设 24h 会让"偶发一次 429 抖动"的出口整天闲置。
#   取 6h：真满的基本退出本轮，抖动的还能回来（与参考项目的取舍一致）。
PROXY_COOLDOWN = float(os.getenv("FO_PROXY_COOLDOWN", "120") or 120)
PROXY_COOLDOWN_MAX = float(os.getenv("FO_PROXY_COOLDOWN_MAX", "21600") or 21600)
PROXY_SLOT_TIMEOUT = float(os.getenv("FO_PROXY_SLOT_TIMEOUT", "240") or 240)
# 起飞前是否做槽位端口连通检查（默认开）。
# 🔴 默认开的理由：槽位实例是**独立前台进程**，很容易"配置还在、进程没了"。
#    那种状态下每条记录都以"代理连接错误"收场，而"注册全失败"最容易被
#    误读成"IP 又被封了" —— 结论完全错。
PROXY_PREFLIGHT = os.getenv("FO_PROXY_PREFLIGHT", "1").strip().lower() \
    not in ("0", "false", "no", "off")

# 🔴 槽位端口 → 出口 IP 的映射。**不进仓库**（出口 IP 是基础设施标识）。
#    实测（2026-09-26，本机 mihomo 6 槽）：
#        7901 → 203.0.113.11
#        7902 → 203.0.113.12
#        7903 → 203.0.113.13
#        7904 → 203.0.113.14
#        7905 → 203.0.113.13   ← 与 7903 同出口
#        7906 → 203.0.113.11  ← 与 7901 同出口
#    ⇒ **6 个槽位只有 4 个不同出口 IP**。并发上限是 4，不是 6。
#    配额按**出口 IP** 记（不是槽位号）—— 槽位清单顺序会变，位置号会错配。
IR_SLOT_EGRESS_IPS = os.getenv("FO_SLOT_EGRESS_IPS", "").strip()


def _parse_slot_egress(raw: str) -> dict:
    """`7901=1.2.3.4,7902=5.6.7.8` → `{端口: IP}`。容错，格式不对的项跳过。"""
    out: dict = {}
    for item in raw.replace("\n", ",").split(","):
        item = item.strip()
        if not item or item.startswith("#") or "=" not in item:
            continue
        port, _, ip = item.partition("=")
        port, ip = port.strip(), ip.strip()
        if port and ip:
            out[port] = ip
    return out


SLOT_EGRESS_IPS: dict = _parse_slot_egress(IR_SLOT_EGRESS_IPS)


def proxy_slots() -> list[str]:
    """读槽位清单。没配置就返回空列表（调用方据此退回单代理/直连）。

    文件里允许一行一个，也允许逗号分隔；`#` 开头视为注释。
    """
    raw = ""
    if IR_PROXY_SLOTS_FILE:
        p = Path(IR_PROXY_SLOTS_FILE)
        if not p.is_file():
            raise ValueError(f"FO_PROXY_SLOTS_FILE 指向的文件不存在：{p}")
        raw = p.read_text(encoding="utf-8")
    elif IR_PROXY_SLOTS:
        raw = IR_PROXY_SLOTS
    out: list[str] = []
    for line in raw.replace("\n", ",").split(","):
        item = line.strip()
        if item and not item.startswith("#"):
            out.append(item)
    return out


def slot_scope(url: str) -> str:
    """槽位 URL → **配额记账的 scope**（= 该槽位的出口 IP）。

    🔴 必须按出口 IP 记，不能按槽位号（`slot1`/`slot2`）：
      目标站点封的是 IP，而 `slots.txt` 的顺序随时会变。用位置号当 scope 时，
      清单一改动既有记录就**整体错配到别的 IP 头上** —— 不报错、不抛异常，
      只是让某个 IP 偷偷超限、另一个被提前停掉。

    🔴 端口不在映射表里时**大声报错，不退回位置号**：
      静默错配比"跑不起来、逼你去补映射"危险得多。

    ⚠ 这里**不需要**"迁移配额记录"这类工具：记录是按**出口 IP** 记的，
      端口换成别的 IP 时，旧 IP 的记录跟着旧 IP 走（既不丢也不串），
      新 IP 从 0 开始。只有"按槽位号记账"才需要迁移 —— 那正是本函数
      拒绝做的事。（早期报错文案里引用过一个 `migrate_quota_scope.py`，
      那个工具**本来就不该存在**，已删除引用。）
    """
    port = url.rsplit(":", 1)[-1].strip()
    ip = SLOT_EGRESS_IPS.get(port)
    if not ip:
        raise ValueError(
            f"槽位 {url} 的出口 IP 未知（端口 {port} 不在 SLOT_EGRESS_IPS 里）。\n"
            f"  1) 先量出真实出口 IP： python tools/probes/probe_slots.py\n"
            f"  2) 写进 .env（**不要写进源码**）：\n"
            f"       FO_SLOT_EGRESS_IPS={port}=<那个槽位的出口 IP>\n"
            f"     多个槽位用逗号分隔：7901=1.1.1.1,7902=2.2.2.2")
    return ip


def proxies(raw: str = None) -> dict | None:
    """代理串 → requests 的 `proxies` 字典；空则 None。

    支持 `host:port:user:pass`、`scheme://user:pass@host:port`、`http://host:port`。
    `raw` 传 None 时用全局 `FO_PROXY`；**传具体值时不碰全局状态** ——
    槽位池并发场景必须这样用，否则多线程改全局会互相踩。
    """
    raw = (PROXY if raw is None else raw).strip()
    if not raw:
        return None
    if "://" not in raw:
        parts = raw.split(":")
        if len(parts) == 4:
            host, port, user, pwd = parts
            raw = f"http://{user}:{pwd}@{host}:{port}"
        elif len(parts) == 2:
            raw = f"http://{raw}"
        else:
            raise ValueError(
                f"代理串格式无法识别：{raw!r}"
                f"（期望 host:port:user:pass 或 scheme://user:pass@host:port）")
    return {"http": raw, "https": raw}


def apply_proxy(session, proxy: str = None) -> None:
    """把代理挂到 `requests.Session`（未配置则什么都不做）。

    🔴 **必须同时关掉 `trust_env`** —— 否则环境变量会**静默盖掉**
    `session.proxies`。机制在 `requests/sessions.py:merge_environment_settings`：
    环境代理先 `setdefault` 进字典，而 `session.proxies` 已存在的键不覆盖。

    本机实测有 `HTTP_PROXY=http://127.0.0.1:64435`（WorkBuddy 沙箱代理），
    于是 `session.proxies = {...}` 会被忽略，请求**成功但没走指定代理** ——
    不报错、不告警，很容易误判成"代理已生效"。

    ⚠ 未配置代理时**不动 `trust_env`**：环境代理仍是默认出口，
      与加这个功能之前的行为一致。
    """
    px = proxies(proxy)
    if not px:
        return
    session.proxies = px
    session.trust_env = False


# ── 启动校验 ──────────────────────────────────────────────────────
def validate(*, need_worker_token: bool = True) -> list[str]:
    """返回缺失/有问题的必需配置项（空列表 = 就绪）。

    刻意**不在 import 时抛错** —— 那样连 `--help` 和离线分析都跑不起来。
    由入口显式调用，报错时直接给出修法。
    """
    missing = []
    if MAILBOX_KIND == "worker":
        if need_worker_token and not WORKER_ADMIN_TOKEN:
            missing.append("FO_WORKER_ADMIN_TOKEN")
        if not WORKER_BASE:
            missing.append("FO_WORKER_BASE")
        if not WORKER_DOMAIN:
            missing.append("FO_WORKER_DOMAIN")
    else:
        missing.append(
            f"FO_MAILBOX_KIND（未知取值 {MAILBOX_KIND!r}，"
            f"只认 {' / '.join(MAILBOX_KINDS)}）")

    # 配了槽位池却没给「端口 → 出口 IP」映射：`slot_scope()` 会在**第一个任务**
    # 才抛错，那时已经跑了一半。提前到启动阶段报。
    try:
        has_slots = bool(proxy_slots())
    except (ValueError, OSError):
        missing.append(f"FO_PROXY_SLOTS_FILE（指向的文件读不到：{IR_PROXY_SLOTS_FILE}）")
        has_slots = False
    if has_slots and not SLOT_EGRESS_IPS:
        missing.append(
            "FO_SLOT_EGRESS_IPS（已配槽位池但缺「端口=出口IP」映射；"
            "先跑 python tools/probes/probe_slots.py 量出来）")

    # 邀请码：只校验**格式**，不校验存在性（服务端自己都不查）。
    # 🔴 放在这里拦，是因为"码写错"是**不会报错**的失败 ——
    #    注册照常 200，只有推荐关系静默不成立。能在启动阶段拦掉的，
    #    就不要留到跑完一批之后靠人翻台账。
    try:
        normalize_invite_code(INVITE_CODE)
    except ValueError as ex:
        missing.append(f"FO_INVITE_CODE（{ex}）")
    return missing
