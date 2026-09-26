"""原子写盘 —— 先写同目录临时文件，再原子替换。

为什么单独一个模块
------------------
这个模式在本项目里有**三处**用到：`ledger` 的台账落盘、`proxypool` 的池子
状态落盘、`quota` 的计数重写。同一份"崩溃安全"逻辑复制三遍，代价不是行数，
是**修复只会改一处**：漏改的那一处，症状是"批量跑一半被 Ctrl-C 之后状态文件
变成语法不完整的 JSON"，而下一轮读它会**静默降级成「没有状态」**——

  * 池子状态丢了 ⇒ 退避序列从第一档重来（120s 后又去撞同一个被封的出口）；
  * 台账丢了 ⇒ 那批账号**永久失去访问凭据**。

两处都不报错。所以"怎么写才安全"必须有**唯一实现**。

🔴 临时文件名一律是 `p.name + ".tmp"`（**追加**），不用 `path.with_suffix()`。
   `with_suffix` 会**替换**最后一个后缀：`opc_quota.jsonl` 会写成
   `opc_quota.tmp` —— 抹掉了"它是什么文件"这个信息，且同目录同时存在
   `a.json` 与 `a.jsonl` 时两个临时名会**撞车**。

⚠ 本模块**只负责写**。要不要捕获 `OSError` 由调用方决定：
  * `ledger` —— 让它抛（台账写不成必须让人知道）；
  * `proxypool` / `quota` —— 各自捕获并降级成告警（状态文件是加速器）。

本模块只依赖标准库 —— 它是叶子，不进任何依赖环。
"""

import os
from pathlib import Path

TMP_SUFFIX = ".tmp"


def tmp_path_for(path) -> Path:
    """`path` 对应的临时文件路径（**追加**后缀，不替换）。"""
    p = Path(path)
    return p.with_name(p.name + TMP_SUFFIX)


def atomic_write_text(path, text: str, *, encoding: str = "utf-8") -> Path:
    """把 `text` 原子地写进 `path`，返回实际落盘路径。

    保证 `path` 要么是**旧内容**、要么是**新内容**，永远不会是半截 ——
    读者（下次运行的合并、或并发进程）看不到中间态。

    ⚠ 单次写入不是原子的：`tmp.write_text()` 自己写一半被杀，`tmp` 是半截，
    但 `path` **没被动过**。这正是要的效果。
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = tmp_path_for(p)
    tmp.write_text(text, encoding=encoding)
    os.replace(tmp, p)
    return p
