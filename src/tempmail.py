"""CF Worker 临时邮箱客户端。

API（均需 `X-Admin-Token`；实测 `Authorization: Bearer` 也认，两个都带最稳）：
  POST /api/mailboxes            {"domain": "...", "count": N} -> {"ok":true,"emails":[...]}
  GET  /api/inbox?email=<addr>   按收件人列出邮件（走 to_address 索引）
  GET  /admin/all?limit=N        列出最新 N 封（全表，**不支持按收件人过滤**）
  GET  /admin/msg?id=&email=     单封邮件详情
  DELETE /admin/delete?id=&email=

🔴 **收信一律走 `/api/inbox?email=`，不要用 `/admin/all`**（实测依据，不是推测）：
   1. 读配额：`/admin/all?limit=50` 读 **51 行**，而 `/api/inbox?email=`
      （命中 1 封）只读 **2 行**、没命中读 **0 行**。背后是 D1 的读配额。
   2. **窗口截断（这才是要命的）**：`/admin/all` 返回「最新 N 条」，而 D1
      retention 只保留 100 行。同一个 Worker 被别的项目以 21.6 封/小时灌满时，
      整表每 4.6 小时被冲刷一遍 —— 我们的激活信一旦被挤出窗口就**永远读不到**。
      `/api/inbox` 按收件人索引查，**别人的邮件挤不掉我们的**。

🔴 本站点的激活邮件**不能用 Worker 的 `extracted_json`** ——
   实测（2026-09-26）发件人是 `rockssr@163.com`，Worker 的自动链接提取
   返回**空数组** `[]`。所以 `Mail.links` 拿不到东西，必须由
   `opc.find_activation_url()` 从 `body` 里正则捞。这是与参考项目最大的
   行为差异（那边 Worker 能提出链接，本项目不能）。
"""

import json
import os
import time
from dataclasses import dataclass

import requests

from . import config


@dataclass
class Mail:
    id: str = ""
    to_address: str = ""
    from_address: str = ""
    subject: str = ""
    body: str = ""
    extracted_json: str = "[]"
    received_at: int = 0

    @property
    def links(self) -> list:
        """解析 Worker 已提取的链接列表。

        ⚠ **本站点上这个列表基本是空的**（见模块 docstring）。留着是为了
          兼容 Worker 哪天把提取修好，以及让参考项目那套调用方式仍能工作。
          找激活链接请用 `opc.find_activation_url(mail.body)`。
        """
        try:
            items = json.loads(self.extracted_json or "[]")
        except (ValueError, TypeError):
            return []
        out = []
        for it in items:
            if isinstance(it, dict) and it.get("value"):
                out.append(str(it["value"]))
            elif isinstance(it, str) and it:
                out.append(it)
        return out

    def find_link(self, *keywords: str) -> str | None:
        """按关键词匹配 `links` 里的 URL（默认匹配激活类）。"""
        kws = [k.lower() for k in keywords] or ["activate", "activat", "verif", "confirm"]
        for url in self.links:
            low = url.lower()
            if any(k in low for k in kws):
                return url
        return None


class TempMailClient:
    def __init__(self, base: str = None, token: str = None, timeout: float = None):
        self.base = (base or config.WORKER_BASE).rstrip("/")
        self.token = token or config.WORKER_ADMIN_TOKEN
        self.timeout = timeout or config.REQUEST_TIMEOUT
        self.session = requests.Session()
        # 🔴 邮箱 Worker **默认不走代理**。
        #    理由：收信轮询是整链瓶颈，绕道代理只会更慢；而 Cloudflare
        #    Worker 不关心我们的出口 IP，走代理零收益。
        #    代理只该挂在**被封的那一侧**（站点 API）。
        #    确实要让邮箱也走代理时：`FO_PROXY_MAIL=1`。
        if os.getenv("FO_PROXY_MAIL", "").strip().lower() in ("1", "true", "yes"):
            config.apply_proxy(self.session)
        self.session.headers.update(
            {
                "X-Admin-Token": self.token,
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
            }
        )
        # 上一次 `wait_for_mail` 失败的原因（"" = 没有异常，纯粹是邮件没到）。
        # 用来把"邮件没到"和"邮箱服务读不出来"分开 —— 两者的修法完全不同。
        self.last_error = ""
        # 上一次 `wait_for_mail` 打了几次收信接口（含 5xx 重试）。
        self.last_polls = 0
        self.last_http_errors = 0

    # ── 邮箱管理 ──────────────────────────────────────────────
    def create_mailbox(self, domain: str = None, count: int = 1) -> list:
        r = self.session.post(
            f"{self.base}/api/mailboxes",
            json={"domain": domain or config.WORKER_DOMAIN, "count": count},
            timeout=self.timeout,
        )
        r.raise_for_status()
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"create_mailbox failed: {data}")
        return data.get("emails", [])

    # ── 邮件读取 ──────────────────────────────────────────────
    def list_mails(self, limit: int = None, email: str = None) -> list:
        """列邮件。

        `email` 给定 → 走 `/api/inbox?email=`（索引查，读 0~1 行）。
        不给 → 退回 `/admin/all?limit=N`（全表，读 N+1 行）——
        **只该在排查时用**，生产路径必须带 email。
        """
        if email:
            r = self.session.get(
                f"{self.base}/api/inbox", params={"email": email}, timeout=self.timeout
            )
        else:
            r = self.session.get(
                f"{self.base}/admin/all", params={"limit": limit or 50}, timeout=self.timeout
            )
        r.raise_for_status()
        raw = r.json().get("messages", []) or []
        return [
            Mail(
                id=str(m.get("id", "")),
                to_address=str(m.get("to_address", "")),
                from_address=str(m.get("from_address", "")),
                subject=str(m.get("subject", "")),
                body=str(m.get("body") or m.get("body_text") or ""),
                extracted_json=str(m.get("extracted_json") or "[]"),
                received_at=int(m.get("received_at") or 0),
            )
            for m in raw
        ]

    def wait_for_mail(
        self,
        address: str,
        *,
        sender_hint: str = None,
        timeout: float = None,
        interval: float = None,
        since_ts: int = 0,
    ) -> Mail | None:
        """轮询等待目标地址的邮件。

        🔴 **5xx 必须重试，绝不能判死**（参考项目实测：同一个 Worker 的
           `/admin/all` 会间歇性抛 Cloudflare `Error 1101`，25 次只成 1 次）。
          语义上 5xx = "服务端现在读不出来"，不是"这封邮件不存在"。
          轮询本来就是在等，多等几次的代价远小于丢掉一个已注册账号。
          4xx（401 凭据错 / 404）才是**我们**的问题，必须立刻失败。

        `sender_hint`：优先返回发件人匹配的邮件。**匹配不到就退回任意邮件** ——
        实测发件人是 `rockssr@163.com`，但发信域名将来可能换，把它当硬条件
        会让整链在换域名当天全灭。所以它是"排序偏好"，不是过滤器。
        """
        timeout = timeout or config.MAIL_POLL_TIMEOUT
        interval = interval or config.MAIL_POLL_INTERVAL
        hint = (sender_hint if sender_hint is not None else config.MAIL_SENDER_HINT).lower()
        deadline = time.time() + timeout
        target = address.lower()
        self.last_error = ""
        self.last_polls = 0
        self.last_http_errors = 0
        errs = 0
        last_code = ""
        fallback: Mail | None = None

        while time.time() < deadline:
            self.last_polls += 1
            try:
                mails = self.list_mails(email=address)
            except requests.HTTPError as ex:
                code = ex.response.status_code if ex.response is not None else 0
                if code and code < 500:
                    raise
                errs += 1
                self.last_http_errors = errs
                last_code = str(code or "?")
                time.sleep(min(interval * (1 + errs // 10), 2.0))
                continue
            for m in mails:
                if m.to_address.lower() != target:
                    continue
                if since_ts and m.received_at and m.received_at < since_ts:
                    continue
                if hint and hint in m.from_address.lower():
                    return m
                # 记下第一封"发件人对不上"的邮件当兜底，但继续等匹配的。
                if fallback is None:
                    fallback = m
            if fallback is not None:
                # 已经等了至少一轮、且有一封信躺着 —— 再给 1 轮机会等匹配的，
                # 然后就用兜底那封。避免"发信域名换了"导致整链空等 120s。
                time.sleep(interval)
                break
            time.sleep(interval)

        if errs and fallback is None:
            self.last_error = (
                f"邮箱 Worker 持续 5xx（{errs} 次，最近 HTTP "
                f"{last_code}）—— 不是邮件没到，是读不出来"
            )
        return fallback
