"""端到端编排 —— 注册 → 收信激活 → 登录 → 建 API Key → 校验。

阶段划分
--------
    Stage 1  建邮箱 + 注册        纯 HTTP，**消耗出口 IP 的每日注册配额**  ~3s
    Stage 2  收激活信 + 激活       纯 HTTP，不消耗注册配额                    ~6s
    Stage 3  登录                  纯 HTTP，不消耗注册配额                    ~2s
    Stage 4  建 API Key            纯 HTTP，不消耗注册配额                    ~1s
    Stage 5  校验 key              纯 HTTP（外部视角），非致命                 ~1s

🔴 **"哪些阶段消耗 IP 配额"是并发设计的唯一依据**（2026-09-26 实测）
------------------------------------------------------------------
实测：**用槽位出口注册 + 用直连出口（当日已触顶的那个 IP）登录 → 成功**。
⇒ 登录/建 Key **与注册出口无关**，只有「注册」这一步吃 IP 配额。

这直接决定了流水线的形状：

    ┌─ 生产者池（并发 = 出口数）─┐        ┌─ 消费者池（并发 = workers）─┐
    │ 建邮箱→注册→收信→激活       │ ────▶ │ 登录→建Key→校验              │
    │ （**持有槽位租约**）        │  队列  │ （**不持有租约**，直连即可）  │
    └───────────────────────────┘        └─────────────────────────────┘

为什么要拆：注册受"每出口 62s 一次"的硬闸门约束，而其余阶段不受。绑成一条
链会让登录也一起被限速（明明不用）。拆开后槽位在**激活完成的那一刻**就归还，
下一个注册可以立刻排进闸门 —— 槽位利用率 = 100%。

⚠ 无池模式（单 IP）下拆不拆区别不大（全局闸门本来就串行），但仍走同一套
  代码路径，避免"两种模式两套逻辑"。

⚠ **workers 不是总并发**，而是"同时在跑的登录会话数"。注册侧由生产者池的
  并发度和闸门共同决定。

⚠ 无池模式下不要盲目调大 `workers`：所有登录共用**同一个出口 IP**。
  站点目前对登录没有可见限流（实测连续登录无 429），但这是"实测没看到"，
  不是"服务端保证没有"。风控收紧时请回退到 `workers=1`。
"""

from __future__ import annotations

import json
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field

from . import config, opc, quota
from .mailbox import make_source
from .opc import OpcClient
from .proxypool import NoEligibleSlot, build_pool

# 生产者池的并发度上限。真正的速率由**每个出口各自的**闸门控制
# （`_ScopeRateLimiter`），并发度只影响"能同时铺开几个出口"。
# 取 6 覆盖实测的 4 个不同出口 + 余量。
REG_CONCURRENCY = 6

# 连续见到几个"服务端确认 IP 配额触顶"就认定整批该停（**仅无池模式**）。
# 取 2 而不是 1：单次可能是服务端抖动，等第二个确认再停，代价只有一次请求；
# 而误停的代价是整批账号被跳过。
QUOTA_STREAK_STOP = 2


# ── 错误分类（`AccountRecord.error_kind`）────────────────────────────
#
# 为什么要有这个字段：在这之前，"这个失败是不是出口被判死"靠**在 error 文本里
# 搜关键词**判断。文本匹配有两个毛病：
#   ① 我们自己拼的**守卫文案**里也引用了那些关键词，于是"一个请求都没发的
#      主动中止"会被读成"服务端确认的封禁"。
#   ② 改一句文案就可能悄悄改掉行为，而 diff 里看不出来。
#
# 结构化之后，"服务端到底返回了什么"由**打标点**决定，读点只认字段。
# 打标只在**真的拿到服务端响应**的地方做（`stage_register`），
# 读点统一走 `error_kind_of()`。
ERR_NONE = ""  # 没有错误
ERR_IP_QUOTA = "ip_quota"  # 服务端返回"该 IP 今日注册数量已达上限"
ERR_QUOTA_GUARD = "quota_guard"  # 本地守卫主动中止，**一个请求都没发**
ERR_REJECTED = "rejected"  # 服务端明确拒绝了这次注册（非配额）
ERR_NETWORK = "network"  # 网络 / 超时 / HTTP 层
ERR_MAIL = "mail"  # 激活邮件没到 / 读不出来
ERR_KEY = "key"  # 登录或建 Key 阶段


def _rec_field(rec, name: str) -> str:
    """从 `AccountRecord` **或**从 `latest.json` 读回来的 dict 里取字符串字段。

    两种形态都要支持：流水线里是对象，而 `run.py` 走台账那条路时是 dict。
    """
    v = rec.get(name, "") if isinstance(rec, dict) else getattr(rec, name, "")
    return v if isinstance(v, str) else ""


def error_kind_of(rec) -> str:
    """读一条记录的错误类别 —— **唯一读点**。接受 `AccountRecord` 或 dict。

    规则：**字段非空即权威**。字段为空时才退化成文本匹配 —— 那是给
    "手工构造 / 从旧台账读回来的记录"兜底的。

    🔴 `status == "skipped"` 永远返回"无配额证据"。这不是启发式，是逻辑：
      中止意味着**一个请求都没发**，没有请求就不可能有服务端响应。
    """
    kind = _rec_field(rec, "error_kind")
    if kind:
        return kind
    if _rec_field(rec, "status") == "skipped":
        return ERR_NONE
    err = _rec_field(rec, "error")
    if err and config.IP_QUOTA_MARK in err:
        return ERR_IP_QUOTA
    return ERR_NONE


class _QuotaAbort(RuntimeError):
    """本地配额守卫主动中止 —— **未发出任何注册请求**。

    与"注册失败"必须区分：失败是"试过了，不行"；中止是"没试，因为
    前面的证据已经足够"。混在一起会让人误以为服务端又拒了一批。
    """


class _IpQuotaBlocked(RuntimeError):
    """服务端确认：**这个出口今天完了**（`该 IP 今日注册数量已达上限`）。

    🔴 必须与"速率限流"分开：速率等 62s 就好，这个是等一整天。
      把它当速率去重试 = 对着一个已判死的出口每 62s 打一枪。
    """


class _ScopeRateLimiter:
    """**按出口分别**限速的最小间隔闸门。

    🔴 为什么必须按 scope 分（而不是参考项目那个全局闸门）：
      本站点的限制是"**每个 IP** 60 秒 1 次"，不是全局。用全局闸门时
      4 个出口也只能合计 62s 一次 —— 池子白建。

    🔴 为什么不能"持锁睡觉"：那样 4 个出口会互相阻塞（A 在等 62s 时，
      B 明明可以立刻发）。这里在锁内只**计算**该等多久，睡觉在锁外做。
    """

    def __init__(self, min_interval: float):
        self.min_interval = min_interval
        self._lock = threading.Lock()
        self._next: dict = {}

    def wait(self, scope: str = "") -> None:
        while True:
            with self._lock:
                now = time.time()
                nxt = self._next.get(scope, 0.0)
                if now >= nxt:
                    self._next[scope] = now + self.min_interval
                    return
                delay = nxt - now
            # 分段睡：长等待时让其它 scope 能插进来，也让 Ctrl-C 更跟手。
            time.sleep(min(delay, 1.0))

    def cooldown_remaining(self, scope: str = "") -> float:
        with self._lock:
            return max(0.0, self._next.get(scope, 0.0) - time.time())


class QuotaGovernor:
    """配额决策的**唯一**去处。

    为什么要收拢：三处判据散在 `run_batch` 里时，看任何一处都推不出全局行为，
    尤其"运行中哨兵只在非池模式生效"这条，散着写只能靠注释解释。

    | 时机 | 无池模式 | 池模式 |
    |---|---|---|
    | 开跑前 | 用全局 scope 裁计划量 | **跳过**（全局计数是老出口的，拦了会误判） |
    | 拿租约前 | — | 按**该出口**的 scope 预筛 `accept` |
    | 拿到租约后 | — | 按该出口的 scope 再查一遍 |
    | 运行中 | 连续 N 个 IP 配额错误就置位"停" | **不置位**（那是出口维度，不是整批触顶） |
    """

    def __init__(self, *, pool=None, ignore: bool = False, log=print):
        self.pool = pool
        self.ignore = ignore
        self.log = log
        self._hit = threading.Event()
        self._lock = threading.Lock()
        self._streak = 0

    # ── 开跑前 ────────────────────────────────────────────────
    def allow(self, count: int) -> int:
        """返回**实际允许投递**的账号数（可能被裁小）。"""
        if self.ignore:
            return count
        if self.pool is not None:
            # 🔀 池模式下**跳过全局守卫**：本地计数是按出口 IP 记的，
            #    而这里的"全局"计数属于老的单出口。拿它拦池化批次会得到
            #    错误结论（参考项目实测：53/40 "超额"，但 6 个新出口一个都没用过）。
            #    改由每个 producer 按自己的 scope 单独检查。
            n = self.pool.distinct_egress()
            self.log(
                f"ℹ 池模式：跳过全局配额守卫（全局计数是**老出口**的，"
                f"与新出口无关）—— 改按出口分别计数，"
                f"每出口上限 {quota.limit()} 个。"
            )
            if n:
                self.log(f"   {n} 个不同出口 → 本轮理论上限 {n * quota.limit()} 个")
            return count
        try:
            st = quota.check_or_raise(planned=count, allow_partial=True)
        except quota.QuotaExceeded as ex:
            raise _QuotaAbort(str(ex)) from ex
        if st.used + count > st.limit:
            keep = st.remaining
            self.log(
                f"⚠ 本地配额保护：计划注册 {count} 个，但{st.describe()} "
                f"—— 本次只跑 {keep} 个。\n"
                f"  想全跑：调大 FO_IP_QUOTA_MAX，或加 --ignore-quota"
                f"（先确认服务端确实已恢复）。"
            )
            return keep
        return count

    # ── 拿租约 ────────────────────────────────────────────────
    def check_slot(self, index: int) -> bool:
        """`accept` 谓词：第 `index` 个出口**现在**还有额度吗（拿租约**之前**）。

        🔴 不加这一步的话，池子只会按"用得最少"均分租约，已满的槽位会白白
          吃掉一大半 —— 参考项目实测 50 个任务只成 16 个，而池子里明明还躺着
          44 个额度没用（全在没被分到的槽位上）。
        """
        if self.pool is None:
            return True
        try:
            scope = config.slot_scope(self.pool.url_of(index))
        except ValueError:
            # 端口没登记出口 IP：不敢用，否则配额会被静默记错地方。
            # 🔴 这一支**刻意不受 --ignore-quota 影响**：它拦的不是"额度用完了"
            #    而是"不知道该把额度记到谁头上"。放行等于让某个真实出口悄悄
            #    超过服务端上限（然后**真**被封），而 --ignore-quota 的语义只是
            #    "别信本地那个保守计数"，不是"允许账目错乱"。
            return False
        if self.ignore:
            return True
        return not quota.status(scope=scope).exhausted

    def claim_slot(self, scope: str, log=print) -> str:
        """拿到租约后**再查一遍**。返回跳过原因，`""` = 放行。

        🔴 为什么必须复查：`check_slot()` 是在**拿租约之前**判的，从拿到租约
          到真正发请求之间，可能有别的线程把最后一点额度用掉了。
        """
        if self.ignore:
            return ""
        st = quota.status(scope=scope)
        if not st.exhausted:
            return ""
        log(f"跳过（出口 {scope} 配额保护：{st.describe()}）")
        return f"quota guard: 出口 {scope} 本地计数已满（{st.describe()}），未发请求"

    # ── 运行中 ────────────────────────────────────────────────
    def note_result(self, ok: bool, rec) -> None:
        """注册结果反馈 —— 运行中哨兵的**唯一**喂食口。"""
        with self._lock:
            if self.pool is not None:
                # 🔀 池模式下"IP 配额触顶"是**出口维度**的信号，不是"整批触顶"。
                #    旧逻辑在这里会把整批后续任务标 skipped —— 那样等于
                #    **把还有余量的出口一起停掉**。自我保护改由槽位冷却承担。
                if ok:
                    self._streak = 0
                return
            if ok:
                self._streak = 0
            elif getattr(rec, "status", "") == "skipped":
                # 主动中止：没发请求，既不是配额证据也不是恢复证据。
                # ⚠ 必须显式排除 —— 它的 error 文本里也含关键词（是引用，
                #   不是服务端返回），不排除就会被当成新证据重复计数。
                pass
            elif error_kind_of(rec) == ERR_IP_QUOTA:
                self._streak += 1
                if self._streak >= QUOTA_STREAK_STOP:
                    self._hit.set()

    def hit(self) -> bool:
        """运行中已确认配额触顶。**只在无池模式下可能为真**。"""
        return self._hit.is_set()

    def streak(self) -> int:
        return self._streak


def settle_lease(pool, lease, ok: bool, rec) -> None:
    """按注册结果归还槽位租约。

    🔴 三种结果必须分开处理 —— 冷却时长差 6 倍（120s vs 20s）：
        出口被判死（IP 配额） → report_banned  长冷却 + 退避，配额不会几秒就恢复
        网络类失败            → report_failed  短冷却，偶发超时很正常
        正常 / 主动中止        → release        立刻还回去
    混成一种的后果：要么把健康出口按长冷却晾 2 分钟（吞吐塌），
    要么把已判死的出口当偶发故障 20s 后重投（继续撞墙）。

    🔴 判据走 `error_kind_of()`，**不能**改成在 `rec.error` 里搜关键词：
      我们自己拼的守卫文案里含关键词（是引用，不是服务端返回），文本匹配会把
      "一个请求都没发的主动中止"读成"服务端确认的封禁"，于是一个**干净出口**
      被白晾 120s。
    """
    if pool is None or lease is None:
        return
    kind = error_kind_of(rec)
    if kind == ERR_IP_QUOTA:
        pool.report_banned(lease, f"IP quota @ {rec.email}")
    elif ok:
        pool.release(lease)
    elif getattr(rec, "status", "") == "skipped":
        # 主动中止：一个请求都没发，出口是干净的，别浪费一次冷却。
        pool.release(lease)
    else:
        pool.report_failed(lease, (getattr(rec, "error", "") or "")[:80])


# ── 账号记录 ──────────────────────────────────────────────────────
@dataclass
class AccountRecord:
    email: str = ""
    username: str = ""
    password: str = ""
    user_id: str = ""
    opc_id: str = ""
    api_key: str = ""
    key_id: str = ""
    key_masked: str = ""
    token_reward: int = 0
    # 注册时**实际发出**的邀请码（空 = 一个都没发）。记它是因为"这批到底有没有
    # 带码"必须能事后核查 —— 服务端对**不存在的码不报错**（实测：乱填 8 位 →
    # HTTP 200、账号照建），所以码错了不会以失败形式暴露，只能靠台账这一列看出来。
    invite_code: str = ""
    # 服务端在 `user.invitedAt` 回的时间戳（毫秒；**0 = 未记录为受邀**）。
    # 🔴 这是**唯一**能证明推荐关系真的成立的证据。状态码不是，`tokenReward`
    #    也不是 —— 带码时奖励**延后到激活**才发（注册阶段恒为 0），
    #    "注册时 reward=0" 是正常现象，不是错误信号。
    invited_at: int = 0
    status: str = "init"
    error: str = ""
    # 错误的**结构化类别**（取值见 ERR_* 常量）。空 = 没有错误。
    # 读它请用 `error_kind_of()`，不要直接读字段 —— 那里有老记录的兜底。
    error_kind: str = ""
    stages: dict = field(default_factory=dict)
    timings: dict = field(default_factory=dict)
    created_at: str = ""
    # 这个账号注册时用的出口槽位（`slot3(http://127.0.0.1:7903)@1.2.3.4`）。
    # 空 = 没启用槽位池（走的全局 FO_PROXY 或直连）。
    # 🔴 记它是为了事后能回答"被判死的到底是哪个出口" —— 光看错误文案
    #    不知道是哪个 IP，必须能把它和出口对应起来。
    proxy_slot: str = ""
    # 出口 IP（配额记账的维度）。与 `proxy_slot` 分开存：
    # 槽位是"用哪个端口"，出口 IP 是"站点真正看到的来源" —— 多个槽位可能同一个出口。
    egress: str = ""
    # 调用这个 key 的 base_url，形如 `https://opc.fiime.cn/api/model-service/v1`。
    # 🔴 **必须落进台账**：下游拿到 key 却不知道往哪打，等于没交付。
    #    而且这个值曾经出过错（漏 `/api` → 打到 SPA 兜底页 → 200 + HTML），
    #    把它写进记录里，事后才能一眼看出"当时用的地址对不对"。
    base_url: str = ""
    # key 校验是否**真的通过**（结构化布尔，不是靠 match stages 里那串文案）。
    # 三态：True=通过 / False=不通过 / None=没校验（`--no-verify`）。
    key_verified: bool = None
    # 服务端上报的可用模型 id（19 个，含 text/image/video 三类）。
    # 落进台账是为了回答"这个 key 到底能调什么"，不用再登录一次去查。
    models: list = field(default_factory=list)
    # 报告里"怎么调"示例用的模型 —— 必须是 text 类，否则照抄会 400。
    default_model: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=2)


# 重发上限。站点偶发漏发（实测同一批还夹着 Worker 连接重置），一次不够；
# 但**每次重发都会作废上一个 token**，且每轮要占着收信超时，不能无限重试。
RESEND_ATTEMPTS = 2


def _newest_activation_link(mail, email: str, since_ts: int, *, timeout: float, log=print) -> tuple:
    """轮询直到 `since_ts` **之后**出现带激活链接的邮件；返回**最新**那封的 link。

    返回 `(link | None, newest_received_at)`。

    🔴 用 `list_mails()` 取**最新**一封，而不是 `wait_for_mail()`（它返回第一封
      匹配的）—— 每次重发都会作废上一个 token，拿到迟到旧邮件里的 token 就会
      400。这是本函数存在的全部理由。
    """
    deadline = time.time() + timeout
    interval = config.MAIL_POLL_INTERVAL
    while True:
        try:
            mails = mail.list_mails(email)
        except Exception as ex:
            log(f"收信失败：{type(ex).__name__}: {ex}")
            mails = []
        best_link, best_ts = None, since_ts
        for m in mails:
            ts = m.received_at or 0
            if ts <= since_ts:
                continue
            lk = opc.find_activation_url(m.body or "")
            if lk and opc.OpcClient.token_from_activation_url(lk) and ts >= best_ts:
                best_link, best_ts = lk, ts
        if best_link is not None or time.time() >= deadline:
            return best_link, best_ts
        time.sleep(interval)


def activate_with_resend(
    mail,
    client: OpcClient,
    email: str,
    link: str | None,
    *,
    since_ts: int = 0,
    log=print,
) -> tuple:
    """激活账号；链接坏了 / 激活被拒就**重发激活邮件 + 用最新一封重试**。

    返回 `(opc.ActivateResult, resent: bool)`。

    🔴 为什么需要：站点偶发把激活邮件里的链接发坏（实测 2/10：既漏 `=`
      —— `.../activate?token<hex>`，token 还被截成 62 位）。直接激活必然
      400，白掉一个**已经注册成功**的账号。`POST /api/auth/resend-activation`
      会发一封格式正确的新邮件（首次主题「激活你的…」，重发「请激活你的…」，
      正文是 `?token=<64hex>`）。

    🔴 **重发会作废上一个 token** —— 两个后果：
      ① `since_ts` 必须指到"旧邮件之后"，否则会再次拿到旧链接
         （实测：拿旧 token 激活 → `400 无效或已使用`）；
      ② 取链接必须取**最新**那一封（`_newest_activation_link`），不能拿
         `wait_for_mail` 的第一封匹配 —— 迟到的旧邮件可能先被看到。

    🔀 有界重试：一次重发没等到邮件就再重发一次（`RESEND_ATTEMPTS`）——
      站点偶发漏发（实测同一批还夹着 Worker 连接重置），单次不够；但每轮要占
      收信超时、且有作废旧 token 的代价，所以必须**有界**。

    `link` 传 `None` 表示当前没有可用链接，直接走重发。
    """

    def _try(one_link: str | None) -> opc.ActivateResult:
        tok = opc.OpcClient.token_from_activation_url(one_link) if one_link else ""
        if not tok:
            return opc.ActivateResult(error="激活链接里没有 token", http_status=400)
        return client.activate(tok)

    act = _try(link) if link else None
    # 只有"链接 / token 被拒"（400）才值得重发；网络类失败重发没用。
    if act is not None and (act.ok or act.http_status != 400):
        return act, False
    if act is not None:
        log(f"↻ 激活被拒（{act.error[:40]}）→ 重发激活邮件后重试")
    else:
        log("↻ 无可用激活链接 → 重发激活邮件")

    resent = False
    got_new_link = False
    fail = act
    last_ts = since_ts
    for attempt in range(1, RESEND_ATTEMPTS + 1):
        try:
            rs = client.resend_activation(email)
        except Exception as ex:
            log(f"重发失败：{type(ex).__name__}: {ex}")
            if fail is None:
                fail = opc.ActivateResult(error=f"resend: {ex}")
            return fail, resent
        if not rs.ok:
            log(f"重发被拒：{rs.error or rs.http_status}")
            if fail is None:
                fail = opc.ActivateResult(error=f"resend: {rs.error}", http_status=rs.http_status)
            return fail, resent
        resent = True
        link2, ts = _newest_activation_link(
            mail, email, last_ts, timeout=config.MAIL_POLL_TIMEOUT, log=log
        )
        if link2 is None:
            log(f"第 {attempt} 次重发后没等到新邮件")
            continue
        got_new_link = True
        last_ts = ts
        fail = _try(link2)
        if fail.ok:
            return fail, True
        log(f"第 {attempt} 次重发后的 token 仍被拒（{fail.error[:40]}）")
    if fail is None or not got_new_link:
        # 一次都没拿到"新的可用链接" —— 明说这一点（比原 400 更贴近真相）。
        return opc.ActivateResult(
            error=f"resend {RESEND_ATTEMPTS} 次仍未收到可用新邮件", http_status=400
        ), resent
    return fail, resent


# ────────────────────────────────────────────────────────────────
# Stage 1+2：建邮箱 → 注册 → 收信激活（纯 HTTP，**消耗 IP 配额**）
# ────────────────────────────────────────────────────────────────
def stage_register(
    mail,
    client: OpcClient,
    rec: AccountRecord,
    *,
    gate: _ScopeRateLimiter = None,
    quota_scope: str = "",
    should_stop=None,
    mail_domain: str = None,
    invite_code: str = None,
    log=print,
) -> bool:
    """注册 + 激活。

    `gate`：**按出口**的限速闸门（`_ScopeRateLimiter`）。在真正调用注册接口前
    触发，把速率钉在服务端的安全区间内（见 `config.REG_MIN_INTERVAL`）。

    `invite_code`：邀请码。`None` = 用 `config.INVITE_CODE`（**统一注入**的
    默认路径）；传 `""` = 明确不带码；传非空字符串 = 本批临时换码。

    `should_stop`：可选无参 callable，返回 True 表示**已确认配额触顶**，
    此时**不发注册请求**、直接把记录标成 skipped。

    🔴 为什么 `should_stop` 必须在 `gate.wait()` **之后**检查：`gate` 是
      **同一出口的串行点**（同一 scope 的 producer 在这里排队），放在它后面
      才保证"看到前一个的失败结果"。放在函数开头只能挡住**尚未启动**的任务 ——
      而 `ThreadPoolExecutor` 会把 `min(count, reg_conc)` 个任务**同时**启动，
      它们会在任何失败发生之前一起通过检查。

    🔬 子阶段计时（`rec.timings["register_detail"]`）：不拆开就不知道多出来的
      时间在哪 —— 是闸门在等、还是收信在等、还是接口本身慢。

    🔴 邀请码这条链**失败时是静默的**，这是本函数里最需要留意的一点：
      服务端**不校验码是否存在**（实测：乱填 8 位 `ZZZZZZZZ` → HTTP 200，
      账号照建、无任何报错）。所以"整批用了一个拼错的码"在成功率、退出码、
      错误分类里**一个信号都没有**。唯一判据是响应里的 `user.invitedAt`
      —— 它保持 0 就说明推荐关系没成立。本函数把它落进 `rec.invited_at`，
      并在 `rec.stages["invite"]` 上打标，供报告层单独报出来。
    """
    t0 = time.time()
    sub = {}

    # `None` 与 `""` 语义不同，别合并：`None` = "没指定，走全局配置"（统一注入），
    # `""` = "本批明确不带码"。合并了就再也关不掉全局码。
    code = config.INVITE_CODE if invite_code is None else invite_code

    def mark(k: str, t_from: float):
        sub[k] = round((time.time() - t_from) * 1000)

    # ── 1. 建邮箱 ─────────────────────────────────────────────
    try:
        t = time.time()
        emails = mail.create_mailbox(domain=mail_domain, count=1)
        if not emails:
            raise RuntimeError("邮箱服务没有返回地址")
        rec.email = emails[0]
        mark("mailbox", t)
        log(f"mailbox: {rec.email}")
    except Exception as ex:
        rec.status = "failed"
        rec.error = f"mailbox: {ex}"
        rec.error_kind = ERR_NETWORK
        rec.timings["register"] = round((time.time() - t0) * 1000)
        return False

    # ── 2. 用户名 + 密码 ──────────────────────────────────────
    t = time.time()
    try:
        rec.username = client.gen_available_username()
        rec.password = opc.gen_password()
    except Exception as ex:
        rec.status = "failed"
        rec.error = f"username: {ex}"
        rec.error_kind = ERR_NETWORK
        rec.timings["register"] = round((time.time() - t0) * 1000)
        return False
    mark("username", t)

    # ── 3. 闸门（**按出口**串行点）────────────────────────────
    t = time.time()
    if gate is not None:
        gate.wait(quota_scope)
    mark("gate_wait", t)

    # 过了闸门（同一出口的全局串行点）再看一眼配额信号 —— 放这里才看得到
    # 前序请求的结果。
    if should_stop is not None and should_stop():
        rec.status = "skipped"
        rec.error = "quota guard: 前序账号已确认 IP 配额触顶，未发注册请求"
        rec.error_kind = ERR_QUOTA_GUARD
        rec.timings["register"] = round((time.time() - t0) * 1000)
        rec.timings["register_detail"] = sub
        log("跳过（配额保护）")
        return False

    # ── 4. 注册（含 429 分流）─────────────────────────────────
    t = time.time()
    try:
        reg = _register_with_retry(client, rec, gate, quota_scope, log, code)
    except _IpQuotaBlocked as ex:
        rec.status = "failed"
        rec.error = f"register: {ex}"
        rec.error_kind = ERR_IP_QUOTA
        # 🔴 服务端**确认**这个出口当日不可用 —— 落一笔硬证据进本地配额账。
        #    没有这一步，本地计数就是乐观的：探测脚本绕过 `quota.record()`，
        #    一个已判死的出口会在下一轮 `probe` 里被报成"余 5"。
        #    写进去之后 `status()` 返回 `banned=True`，probe 不再骗人。
        #    ⚠ 只记账，**不**在这里拦请求 —— 拦是 `check_or_raise` 的事。
        quota.record_ban(quota_scope, str(ex))
        rec.timings["register"] = round((time.time() - t0) * 1000)
        rec.timings["register_detail"] = sub
        log(f"✗ 出口被判死：{ex}")
        return False
    except Exception as ex:
        rec.status = "failed"
        rec.error = f"register: {ex}"
        if not rec.error_kind:
            rec.error_kind = ERR_NETWORK
        rec.timings["register"] = round((time.time() - t0) * 1000)
        rec.timings["register_detail"] = sub
        return False
    mark("register_call", t)

    if not reg.ok:
        # ⚠ 刻意**不**在这里写 `rec.invite_code = reg.invite_code_sent`：
        #   `invite_code_sent` 是在发 POST **之前**就填好的，所以网络类失败
        #   （请求根本没出去）也会说"发过码"。台账里写一个我们并不确定发过的值，
        #   比留空更坏 —— 留空 = "不知道"，是诚实的三态。
        rec.status = "failed"
        rec.error = f"register: {reg.error}"
        rec.error_kind = ERR_REJECTED
        rec.timings["register"] = round((time.time() - t0) * 1000)
        rec.timings["register_detail"] = sub
        return False

    rec.user_id = str(reg.user.get("id") or "")
    rec.opc_id = str(reg.user.get("opcId") or "")
    rec.token_reward = reg.token_reward
    rec.stages["register"] = "ok"
    # 只有**成功**才计入本地配额 —— 失败的不占额度，记了会虚高。
    quota.record(rec.email, scope=quota_scope)
    log(f"registered: {rec.opc_id} user={rec.username}")

    # ── 邀请码：先登记"发了什么码 / 注册阶段回没回 invitedAt" ──────
    # 🔴 这里**不下"废码"结论**。`invitedAt` 是注册时就写还是延后到激活才写，
    #    实测证据**指向注册时**（样本：值比激活早约 10s，见 docs/protocol.md），
    #    但**不是秒级确证**（带码时 `tokenReward` 就明确是延后的）。
    #    在注册阶段看到 0 就报"码无效"，万一它是延后写入的，就会把一个
    #    "还没写"的字段误报成废码 —— 那是**假警报**，比不报更坏：
    #    它会让人去改一个根本没问题的码。
    #    所以：非零立刻敢判 ok，是 0 就先记着，等激活之后再说。
    rec.invite_code = reg.invite_code_sent
    rec.invited_at = reg.invited_at
    if rec.invite_code and rec.invited_at:
        rec.stages["invite"] = "ok"
        log(f"邀请码 {rec.invite_code} 已在注册阶段生效（invitedAt={rec.invited_at}）")

    # ── 5. 收激活信 ───────────────────────────────────────────
    t = time.time()
    try:
        m = mail.wait_for_mail(rec.email)
        mark("mail_wait", t)
        sub["mail_polls"] = getattr(mail, "last_polls", 0)
        sub["mail_5xx"] = getattr(mail, "last_http_errors", 0)
        if not m:
            why = getattr(mail, "last_error", "") or "激活邮件在超时内未到达"
            rec.status = "failed"
            rec.error = f"mail: {why}"
            rec.error_kind = ERR_MAIL
            rec.timings["register"] = round((time.time() - t0) * 1000)
            rec.timings["register_detail"] = sub
            return False
        sub["mail_subject"] = m.subject[:60]

        # 🔴 必须自己从 body 里捞链接 —— Worker 的 `extracted_json` 对本站的
        #    激活邮件返回空数组（发件人是 163 邮箱，不是它熟悉的那几家）。
        link = opc.find_activation_url(m.body)
        if not link:
            rec.status = "failed"
            rec.error = (
                f"mail: 正文里找不到激活链接（主题 {m.subject[:40]!r}，正文 {len(m.body)} 字节）"
            )
            rec.error_kind = ERR_MAIL
            rec.timings["register"] = round((time.time() - t0) * 1000)
            rec.timings["register_detail"] = sub
            return False
        # ── 6. 激活（链接坏了会**重发 + 用最新邮件重试一次**）────
        t = time.time()
        sub["activation_token"] = opc.OpcClient.token_from_activation_url(link)[:8] + "…"
        act, resent = activate_with_resend(
            mail,
            client,
            rec.email,
            link,
            since_ts=(getattr(m, "received_at", 0) or 0) + 1,
            log=log,
        )
        mark("activate_call", t)
        if resent:
            sub["activate_resent"] = True
        if not act.ok:
            rec.status = "failed"
            rec.error = f"activate: {act.error}"
            rec.error_kind = ERR_REJECTED
            rec.timings["register"] = round((time.time() - t0) * 1000)
            rec.timings["register_detail"] = sub
            return False
        rec.stages["activate"] = "ok"
        if act.token_reward:
            rec.token_reward = act.token_reward
        log("activated")

        # ── 邀请码：注册+激活都走完了，现在才敢下结论 ─────────────
        # 用 `or` 而不是直接赋值：激活响应若不带这个字段（`_invited_at_of`
        # 会给 0），不能把注册阶段**已经拿到的**非零值抹成 0。
        rec.invited_at = act.invited_at or rec.invited_at
        if rec.invite_code:
            if rec.invited_at:
                rec.stages["invite"] = "ok"
            else:
                # 这是本链**唯一**能暴露"码不对"的地方 —— 服务端不会报错，
                # 账号也建得好好的，只有这个字段是 0。
                rec.stages["invite"] = "⚠ 已发码但服务端未记录受邀（码可能不存在/已失效/自邀）"
                log(f"⚠ 邀请码 {rec.invite_code} 已发出，但 invitedAt=0 —— 推荐关系**未成立**")
    except Exception as ex:
        rec.status = "failed"
        rec.error = f"activate: {ex}"
        if not rec.error_kind:
            rec.error_kind = ERR_NETWORK
        rec.timings["register"] = round((time.time() - t0) * 1000)
        rec.timings["register_detail"] = sub
        return False

    rec.timings["register"] = round((time.time() - t0) * 1000)
    rec.timings["register_detail"] = sub
    return True


def _register_with_retry(
    client: OpcClient,
    rec: AccountRecord,
    gate: _ScopeRateLimiter,
    scope: str,
    log,
    invite_code: str = "",
) -> opc.RegisterResult:
    """调注册接口，按 429 的**类别**分流处理。

    🔴 两种 429 的处置完全相反（这是本项目最重要的分流）：
        LIMIT_RATE     → 等 62s 重试（同一出口）—— 它只是"慢点再来"
        LIMIT_IP_QUOTA → 抛 `_IpQuotaBlocked`（**换 IP**）—— 它要等一整天
      把它当速率去重试 = 对着已判死的出口每 62s 打一枪，一天 1300+ 次。

    🔴 `invite_code` 必须**每一次重试都带上**（所以它在这个循环的**外层**，
      而不是某一次调用的参数）。速率限流后重试是很常见的一条路径，若码只在
      首次尝试里传，那么"被限流过一次的账号"就会**静默地不带码注册成功** ——
      注册成功、激活成功、退出码 0，只有 `invitedAt=0` 孤零零地摆在那里。
      这正是本项目最忌讳的那类失败：**静默降级**。
    """
    last = None
    for attempt in range(config.REG_RATE_RETRY + 1):
        last = client.register(rec.username, rec.email, rec.password, invite_code=invite_code)
        if last.ok:
            return last
        if last.limit_kind == opc.LIMIT_IP_QUOTA:
            raise _IpQuotaBlocked(last.error or "该 IP 今日注册数量已达上限")
        if last.limit_kind in (opc.LIMIT_RATE, opc.LIMIT_OTHER) and attempt < config.REG_RATE_RETRY:
            # 退避重试。**必须先"占住"这个出口的闸门名额**，否则重试会
            # 立刻再撞一次（服务端的窗口是从**上一次请求**算的）。
            if gate is not None:
                gate.wait(scope)
            log(f"↻ 速率限流，退避后重试（{attempt + 1}/{config.REG_RATE_RETRY}）")
            continue
        return last
    return last


# ────────────────────────────────────────────────────────────────
# Stage 3+4+5：登录 → 建 Key → 校验（**不消耗注册配额**）
# ────────────────────────────────────────────────────────────────
def stage_login_key(
    rec: AccountRecord,
    *,
    client: OpcClient = None,
    key_name: str = "default",
    verify: bool = True,
    probe: bool = False,
    log=print,
) -> bool:
    """登录 → 建 API Key →（可选）校验。

    `client`：不传就自建一个**直连**客户端。实测登录/建 Key 与注册出口无关，
    所以这里**不需要槽位租约** —— 这正是流水线能拆成两段的原因。

    `verify`：批量场景建议 True，但重试次数取小。

    ⚠ 早期这里写着"新建 key 有网关传播延迟（实测约 10s）"，**那个结论是错的**。
      真正原因是 base_url 漏了 `/api`，请求打到了 SPA 兜底页（200 + 6707B HTML）。
      修正地址后实测 key **建出来即可用**，没有可测的传播延迟。
      重试保留只是因为边缘层偶发抖动，`attempts` 因此可以取小。
    """
    t0 = time.time()
    own = client is None
    c = client or OpcClient(proxy=None, log=log)
    try:
        if own:
            c.fetch_csrf()

        # 🔴 base_url 先落记录 —— 即便后面建 key 失败，也能从台账看出
        #    当时打算往哪打（这是排查"漏 /api"那类事故的第一手证据）。
        rec.base_url = c.api_base()

        # ── 登录 ──────────────────────────────────────────────
        t = time.time()
        lg = c.login(rec.email, rec.password)
        rec.timings["login"] = round((time.time() - t) * 1000)
        if not lg.ok:
            rec.status = "failed"
            rec.error = f"login: {lg.error}" + (f"（{lg.code}）" if lg.code else "")
            rec.error_kind = ERR_KEY
            return False
        rec.user_id = str(lg.user.get("id") or rec.user_id)
        rec.stages["login"] = "ok"
        log("logged in")

        # ── 服务信息（非致命，失败不挡建 key）──────────────────
        try:
            si = c.service_info()
            rec.stages["service"] = si.describe()
            rec.models = si.model_ids()
            rec.default_model = si.default_model()
            # 🔴 base_url 用**服务端上报的** `apiBasePath`，不是本地常量 ——
            #    站点改路径时这里会跟着变，而不是继续往旧地址打。
            #    实测服务端报的就是 `/api/model-service/v1`，与本地常量一致。
            if si.api_base_path:
                rec.base_url = config.SITE_BASE + si.api_base_path
        except Exception as ex:
            rec.stages["service"] = f"probe failed: {str(ex)[:80]}"

        # ── 建 Key ────────────────────────────────────────────
        t = time.time()
        k = c.create_key(key_name)
        rec.timings["key"] = round((time.time() - t) * 1000)
        rec.api_key = k.key
        rec.key_id = k.key_id
        rec.key_masked = k.masked
        rec.stages["key"] = "ok"
        # 🔴 日志里**只打掩码** —— 明文 key 只在台账里出现（台账整目录 gitignored）。
        log(f"API KEY = {k.masked}")
    except Exception as ex:
        rec.status = "failed"
        rec.error = f"key: {ex}"
        rec.error_kind = ERR_KEY
        return False

    # ── 校验（非致命）────────────────────────────────────────
    if verify:
        try:
            ok, why = OpcClient.wait_until_active(
                rec.api_key, base=rec.base_url, attempts=3, delay=3.0
            )
            rec.stages["verify"] = why
            rec.key_verified = bool(ok)
            log(f"key verify: {why}")
        except Exception as ex:
            rec.stages["verify"] = f"failed: {str(ex)[:100]}"
            rec.key_verified = False
    if probe:
        try:
            ok, why = OpcClient.probe_key(rec.api_key)
            rec.stages["probe"] = why
            log(f"key probe: {why}")
        except Exception as ex:
            rec.stages["probe"] = f"failed: {str(ex)[:100]}"

    rec.timings["login_key"] = round((time.time() - t0) * 1000)
    rec.status = "success"
    return True


def verify_keys(records: list, *, verbose: bool = True, log=print) -> None:
    """流水线末尾统一校验已建出的 API Key。

    为什么放最后而不是塞在关键路径里：批量场景下逐账号就地校验会让整个
    流水线的关键路径被最慢的校验拖住。挪到末尾并发校验，**非致命** ——
    失败只写进 `stages` 和 `key_verified`。

    ⚠ 早期这里写着"新建 key 有 ~10s 传播延迟"，**那个结论是错的**（真正原因
      是 base_url 漏了 `/api`，见 `OpcClient.verify_key` 的 docstring）。
      放末尾的真正理由只剩"并发 + 不挡关键路径"。

    🔴 用**记录自己的 `base_url`** 去校验，不要用模块级配置 —— 台账里存的
      可能是历史批次用的地址，拿今天的配置去校验昨天的 key 会得出错误结论。
      `base_url` 为空的记录（老台账）才回落到配置默认值。
    """
    targets = [r for r in records if r is not None and r.api_key and r.status == "success"]
    if not targets:
        return

    def one(rec: AccountRecord) -> tuple:
        try:
            ok, why = OpcClient.wait_until_active(
                rec.api_key, base=rec.base_url or None, attempts=3, delay=3.0
            )
            return rec, bool(ok), why
        except Exception as ex:
            return rec, False, f"failed: {str(ex)[:100]}"

    with ThreadPoolExecutor(max_workers=min(len(targets), 8)) as ex:
        for f in as_completed([ex.submit(one, r) for r in targets]):
            rec, ok, verdict = f.result()
            rec.stages["verify"] = verdict
            rec.key_verified = ok
            if verbose:
                log(f"key verify {rec.email}: {verdict}")


# ────────────────────────────────────────────────────────────────
# 单账号（顺序执行，便于调试）
# ────────────────────────────────────────────────────────────────
def run_one(
    *,
    key_name: str = "default",
    mail_domain: str = None,
    proxy: str = None,
    verbose: bool = True,
    verify: bool = True,
    probe: bool = False,
    invite_code: str = None,
) -> AccountRecord:
    """完整跑通一个账号。`proxy` 不传时用 `FO_PROXY`（没有就直连）。

    `invite_code`：`None` = 用 `config.INVITE_CODE`（统一注入）；`""` = 不带码。

    🔴 **与 `run_batch` 有两处刻意的行为差异**，不要"顺手统一"：

      1. **不走本地配额守卫**（`quota.check_or_raise`）。单账号模式的用途是
         调试/探测真实服务端行为，本地计数是**保守估计**，拿它挡住请求会让
         "站点到底还能不能注册"这个最想知道的问题永远答不上来。
         ⇒ 单账号模式会真的发出注册请求，撞上服务端 429 也照实报出来。
         ⚠ 代价：`--count 1` 循环跑**会**触发服务端 60s 速率限制。

      2. **不接按出口的速率闸门**（`_ScopeRateLimiter`）。同样是为了"立刻看到
         真实响应"，不排队。

      `run_batch` 是批量交付路径，那两样都必须有 —— 批量场景下浪费一个出口
      当天 1/5 的额度是真实损失，单账号场景下多等 62s 只是慢。
    """
    rec = AccountRecord(created_at=time.strftime("%Y-%m-%d %H:%M:%S"))

    def log(msg: str):
        if verbose:
            print(f"    {msg}", flush=True)

    t0 = time.time()
    mail = make_source()
    client = OpcClient(proxy=proxy, log=log)
    client.fetch_csrf()
    scope = ""
    if proxy:
        try:
            scope = config.slot_scope(proxy)
        except ValueError:
            scope = ""
    rec.egress = scope
    rec.proxy_slot = proxy or "(direct)"

    if not stage_register(
        mail,
        client,
        rec,
        quota_scope=scope,
        mail_domain=mail_domain,
        invite_code=invite_code,
        log=log,
    ):
        rec.timings["total"] = round((time.time() - t0) * 1000)
        return rec

    # 登录/建 Key 用**独立的直连客户端** —— 实测与注册出口无关，
    # 这样单账号模式也不占着槽位（与批量模式的行为一致）。
    stage_login_key(rec, key_name=key_name, verify=verify, probe=probe, log=log)
    rec.timings["total"] = round((time.time() - t0) * 1000)
    return rec


# ────────────────────────────────────────────────────────────────
# 批量：两段式流水线
# ────────────────────────────────────────────────────────────────
def run_batch(
    *,
    count: int,
    workers: int = 4,
    key_name: str = "default",
    mail_domain: str = None,
    verbose: bool = True,
    reg_concurrency: int = None,
    ignore_quota: bool = False,
    verify: bool = True,
    probe: bool = False,
    invite_code: str = None,
) -> list:
    """批量注册。注册阶段与登录阶段流水线并行。

    Args:
        count: 账号数量
        invite_code: 邀请码。`None` = 用 `config.INVITE_CODE`（**统一注入**，
                 这是默认路径）；`""` = 本批不带码；非空 = 临时换码。
                 ⚠ 服务端不校验码是否存在，码错了不会报错 —— 判据是每个账号
                 的 `invited_at`，见 `stage_register` 的说明。
        workers: **登录侧**并发数。它不是总并发 —— 注册侧由生产者池的
                 并发度与**每出口闸门**共同决定。无池模式下所有登录共用
                 同一个出口 IP，风控收紧时请回退到 1。
        reg_concurrency: 生产者池并发度，默认 6（覆盖实测的 4 个不同出口 + 余量）。
        ignore_quota: 跳过本地配额保护。仅当**确信**服务端配额已恢复、
                 而本地计数还没滑出窗口时才用。

    🔀 槽位代理池：配了 `FO_PROXY_SLOTS`（或 `FO_PROXY_SLOTS_FILE`）就自动启用，
    每个生产者独占一个出口 IP。这是**唯一**能绕开 IP 维度每日配额的办法。

    Returns:
        与提交顺序一致的 `AccountRecord` 列表。⚠ 配额保护可能把 count 裁小、
        或把部分记录标成 `status="skipped"` —— **返回长度未必等于传入的 count**，
        调用方应以返回列表为准，不要假设下标齐全。
    """
    if workers < 1:
        raise ValueError("workers must be >= 1")

    # ── 池（必须在配额守卫之前建 —— 守卫要看"是不是池模式"）──────
    pool = build_pool(log=lambda m: print(f"[pool] {m}", flush=True))
    if pool is not None:
        print(
            f"🔀 槽位代理池已启用：{pool.describe()}\n"
            f"   每个注册生产者独占一个出口 IP"
            f"（IP 配额触顶只判死那个出口，不中断整批）",
            flush=True,
        )

    if ignore_quota:
        print(
            "⚠ --ignore-quota 已开启：本地配额守卫**三个检查点**全跳过"
            "（开跑前裁剪 / 拿租约前预筛 / 拿到租约后复查）。\n"
            "  本地计数只是保守估计，跳过它意味着**完全依赖服务端**："
            "真触顶会直接吃「该 IP 今日注册数量已达上限」。\n"
            "  那是 IP 维度的日配额，**不会自己恢复** —— 请自行确认窗口已滑出。",
            flush=True,
        )

    gov = QuotaGovernor(pool=pool, ignore=ignore_quota, log=lambda m: print(m, flush=True))
    count = gov.allow(count)
    if count <= 0:
        print("⚠ 本轮计划量为 0（本地配额已满），未发出任何请求。", flush=True)
        return []

    reg_conc = reg_concurrency or min(count, REG_CONCURRENCY)
    results: list = [None] * count
    q: queue.Queue = queue.Queue()
    print_lock = threading.Lock()

    def make_log(idx: int):
        prefix = f"[{idx + 1}/{count}]"

        def log(msg: str):
            if verbose:
                with print_lock:
                    print(f"{prefix} {msg}", flush=True)

        return log

    # ── 消费者：登录 + 建 Key（**不持有槽位租约**）──────────────
    def consumer_worker(wid: int):
        while True:
            item = q.get()
            try:
                if item is None:
                    return
                idx, rec = item
                if rec.status != "init":
                    continue
                with print_lock:
                    print(f"[{idx + 1}/{count}] login+key: {rec.email}", flush=True)
                # 🔴 **每个账号一个独立 client（= 一个 Session）**，绝不跨账号复用。
                #    Session 带着 cookie，而 cookie 是"某个账号的会话"；站点下发的是
                #    会话级命名的 `server_session_xxx`，复用会让旧会话 cookie 与新账号
                #    的并存 —— 服务端挑哪个不确定，表现成"带着 A 的 cookie 去建 B 的
                #    key"，而且**报错与 cookie 无关**，极难定位。
                #    成本≈0：csrf token 实测是空串，多一个 client 只是个新对象。
                client = OpcClient(proxy=None)
                try:
                    client.fetch_csrf()
                except Exception as ex:
                    # csrf 不参与登录（token 为空、服务端不校验），预热失败只告警。
                    with print_lock:
                        print(
                            f"[worker {wid + 1}] ⚠ csrf 预热失败（登录不依赖它）：{ex}",
                            flush=True,
                        )
                stage_login_key(
                    rec,
                    client=client,
                    key_name=key_name,
                    verify=False,
                    probe=False,
                    log=make_log(idx),
                )
            except Exception as ex:
                rec.status = "failed"
                rec.error = f"consumer: {str(ex)[:120]}"
                rec.error_kind = ERR_KEY
            finally:
                q.task_done()

    consumers = [
        threading.Thread(target=consumer_worker, args=(i,), daemon=True) for i in range(workers)
    ]
    for t in consumers:
        t.start()

    # ── 生产者：注册 + 激活（**持有槽位租约**）──────────────────
    # 🔴 闸门必须**按出口**分开 —— 站点限制是"每个 IP 60 秒 1 次"，
    #    用全局闸门会让 4 个出口合计也只能 62s 一次，池子白建。
    reg_gate = _ScopeRateLimiter(config.REG_MIN_INTERVAL)

    def producer(idx: int):
        rec = AccountRecord(created_at=time.strftime("%Y-%m-%d %H:%M:%S"))
        results[idx] = rec
        log = make_log(idx)

        # 廉价预筛：挡住**尚未启动**的任务。真正的最后一道闸在 stage_register
        # 内部、限速闸门之后（理由见那里的 docstring）。
        if gov.hit():
            rec.status = "skipped"
            rec.error = "quota guard: 前序账号已确认 IP 配额触顶，跳过投递（未发请求）"
            rec.error_kind = ERR_QUOTA_GUARD
            log("跳过（配额保护）")
            q.put((idx, rec))
            return

        ok = False
        lease = None
        scope = ""
        try:
            if pool is not None:
                # 独占一个出口 IP。全池都在冷却时阻塞等待。
                # 🔴 `accept=gov.check_slot` 把"出口配额已满"的槽位**提前排除
                #    在候选之外**（判据在 `QuotaGovernor.check_slot`）。
                try:
                    lease = pool.acquire(timeout=config.PROXY_SLOT_TIMEOUT, accept=gov.check_slot)
                except NoEligibleSlot as ex:
                    # 有空闲槽位，但它们的出口配额全满了。配额要几小时才滑出
                    # 窗口 ⇒ 等下去毫无意义，直接记"跳过"。
                    rec.status = "skipped"
                    rec.error = f"quota guard: 所有出口配额均已满（{ex}），未发请求"
                    rec.error_kind = ERR_QUOTA_GUARD
                    log(f"跳过（所有出口配额已满：{ex}）")
                    return
                except TimeoutError as ex:
                    # 等不到槽位 ≠ 这个账号注册失败，但也绝不能假装成功。
                    rec.status = "failed"
                    rec.error = f"register: {ex}"
                    rec.error_kind = ERR_NETWORK
                    log(f"⏳ 等不到空闲槽位（{pool.describe()}）")
                    return
                # 🔴 scope 取**出口 IP**，不是槽位位置号 —— 位置号会随
                #    `slots.txt` 增删条目整体错位，把配额记到别的 IP 头上。
                scope = config.slot_scope(lease.url)
                rec.egress = scope
                rec.proxy_slot = str(lease)
                log(f"出口 {lease}")
                why = gov.claim_slot(scope, log)
                if why:
                    rec.status = "skipped"
                    rec.error = why
                    rec.error_kind = ERR_QUOTA_GUARD
                    return

            mail = make_source()
            # 🔴 proxy 必须传**具体值**给这个 client，不能改全局 `FO_PROXY` ——
            #    多个 producer 同时改全局会互相踩（见 config.apply_proxy）。
            client = OpcClient(proxy=(lease.url if lease else None), log=log)
            client.fetch_csrf()
            ok = stage_register(
                mail,
                client,
                rec,
                gate=reg_gate,
                quota_scope=scope,
                should_stop=gov.hit,
                mail_domain=mail_domain,
                invite_code=invite_code,
                log=log,
            )
        except Exception as ex:
            rec.status = "failed"
            rec.error = f"register: {ex}"
            if not rec.error_kind:
                rec.error_kind = ERR_NETWORK
        finally:
            settle_lease(pool, lease, ok, rec)
            gov.note_result(ok, rec)
            q.put((idx, rec))

    t_start = time.time()
    with ThreadPoolExecutor(max_workers=reg_conc) as ex:
        list(ex.map(producer, range(count)))

    if gov.hit():
        skipped = sum(1 for r in results if r is not None and r.status == "skipped")
        if skipped:
            print(
                f"⚠ 运行中确认 IP 配额触顶（连续 {QUOTA_STREAK_STOP} 个）"
                f"→ 跳过后续 {skipped} 个（未发请求）。\n"
                f"  {quota.status().describe()}",
                flush=True,
            )

    # 投递结束哨兵
    for _ in range(workers):
        q.put(None)
    for t in consumers:
        t.join()

    t_keys_done = time.time()
    # 关键路径已结束，此时第一个 key 早已过传播期 → 校验几乎瞬时
    if verify:
        verify_keys(results, verbose=verbose, log=make_log(0))
    if probe:
        for r in results:
            if r is not None and r.api_key and r.status == "success":
                okp, whyp = OpcClient.probe_key(r.api_key)
                r.stages["probe"] = whyp
                if verbose:
                    print(f"[probe] {r.email}: {whyp}", flush=True)

    # ── 池结算 ────────────────────────────────────────────────
    if pool is not None and verbose:
        print(f"🔀 槽位池结算：{pool.describe()}", flush=True)
        bans = pool.stats()["bans"]
        if bans and len(bans) >= pool.size:
            # 每个出口都被判死过 —— 这是**换订阅/换节点**的信号，
            # 不是"再加并发"的信号。加并发只会更快撞穿剩下的出口。
            print(
                f"   ⚠ {pool.size} 个槽位**每个**都被判死过至少一次"
                f"（累计 {sum(bans.values())} 次）—— 出口池整体在目标站点"
                f"那边都不干净了。该换订阅或换节点，加并发无效。",
                flush=True,
            )
        elif bans:
            clean = pool.size - len(bans)
            print(
                f"   仍有 {clean}/{pool.size} 个出口从未被判死 —— "
                f"把并发提到 {clean} 以内是有意义的。",
                flush=True,
            )

    elapsed = round(time.time() - t_start, 1)
    for r in results:
        if r is not None:
            r.timings.setdefault("batch_total", round(elapsed * 1000))
            r.timings["keys_done"] = round((t_keys_done - t_start) * 1000)
    return [r for r in results if r is not None]
