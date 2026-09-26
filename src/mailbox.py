"""邮箱源抽象层 —— 把「建邮箱 / 收信」从 CF Worker 解耦出来。

为什么要有这一层
----------------
参考项目（Intern-Register-Tool）用同一层抽象换来一个能力：**换邮箱源不用改
pipeline**。它当时的处境是"自有域名过不了域名门 / gmail 过得了门但读信要
app password"，于是把源做成可插拔，`stage_register` 一行都不用动。

本项目当前**只有 worker 一个实现**，但仍然保留这层 —— 因为站点那条
`blockGmailAliases: true` 的规则说明它**在乎邮箱域名**，将来很可能：
  · 自有域名被站点拉黑 → 要换成 IMAP / 供应商账号池；
  · 或者反过来，别的项目要用本站点的链路。
两种情况下，"改一个工厂 + 加一个类"比"改 pipeline"便宜得多。

`MailboxSource` 是**结构性契约**（`typing.Protocol`），不是基类 ——
`pipeline` 本来就是鸭子类型，这里只是把既有事实写成可检查的接口，
**不引入继承关系**。

    create_mailbox(domain=None, count=1) -> list[str]
    wait_for_mail(address, ...) -> Mail | None
    last_error: str        # "" = 邮件没到；非空 = 读不出来（两者修法不同）
    last_polls: int        # 打了几次收信
    last_http_errors: int  # 读失败了几次

🔴 **`issubclass()` 不能用**：`MailboxSource` 含数据成员（`last_error` 等），
   `issubclass` 会直接抛 `TypeError: Protocols with non-method members ...`。
   契约检查只能用 `isinstance`（实测 Python 3.13.14）。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from . import config
from .tempmail import Mail, TempMailClient

__all__ = ["MailboxSource", "WorkerMailbox", "make_source"]


@runtime_checkable
class MailboxSource(Protocol):
    """邮箱源契约。`pipeline.stage_register` 只依赖这五个成员。"""

    last_error: str
    last_polls: int
    last_http_errors: int

    def create_mailbox(self, domain: str = None, count: int = 1) -> list: ...

    def wait_for_mail(self, address: str, **kw) -> Mail | None: ...


class WorkerMailbox:
    """CF Worker 实现 —— 包住 `TempMailClient`，**纯转发、行为零变化**。

    为什么不直接让 `TempMailClient` 当源：它的方法签名里带了 Worker 特有的
    参数（`limit`），而契约只要 `(address, **kw)`。多一层薄封装让
    "pipeline 依赖的是契约、不是 Worker"这件事在代码里看得见。
    """

    def __init__(self, client: TempMailClient = None):
        self.client = client or TempMailClient()

    # 契约成员（转发到 client，保证读到的就是真实计数）
    @property
    def last_error(self) -> str:
        return self.client.last_error

    @property
    def last_polls(self) -> int:
        return self.client.last_polls

    @property
    def last_http_errors(self) -> int:
        return self.client.last_http_errors

    def create_mailbox(self, domain: str = None, count: int = 1) -> list:
        return self.client.create_mailbox(domain=domain, count=count)

    def wait_for_mail(self, address: str, **kw) -> Mail | None:
        return self.client.wait_for_mail(address, **kw)


def make_source(kind: str = None) -> MailboxSource:
    """按 `FO_MAILBOX_KIND` 选实现。

    🔴 默认 `worker` ⇒ 与引入这个开关之前**行为完全一致**。
      换源必须是**显式动作**。
    🔴 **刻意不做别名**（不认 `cf` / `temp` 之类）：别名会让
      `FO_MAILBOX_KIND=cf` 看起来像"换了个源"，其实只是换了个写法。
    """
    k = (kind or config.MAILBOX_KIND or "worker").strip().lower()
    if k == "worker":
        return WorkerMailbox()
    raise ValueError(
        f"未知的邮箱源 {k!r}，只认 {' / '.join(config.MAILBOX_KINDS)}"
        f"（加新源请同时改 config.MAILBOX_KINDS 与本工厂 —— "
        f"两处必须一起改，否则「校验认得的取值」和「工厂认得的取值」会漂移）"
    )
