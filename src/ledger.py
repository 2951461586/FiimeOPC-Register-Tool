"""账号台账（`ledger/` 目录）的读写与合并。

台账长什么样
------------
    ledger/runs/2026-09-26/results-20260926-014530.json   ← **读源**（全量台账）
    ledger/runs/2026-09-26/results-20260926-012011.json   ← 上一份快照（回滚点）
    ledger/latest.json                                    ← **本批结果**（含失败/跳过）

两个文件职责**不同**，混用是本模块最大的坑：

  * `runs/` 里的快照 = **按 email 合并后的累计全量**；读源是**最新那一份**；
  * `latest.json`     = **最近一次落盘写进去的记录**，只给人看"这次写了什么"。

🔴 为什么 `latest.json` 不能当读源
---------------------------------
它只含本批那几条。拿它当读源 ⇒ 下一次运行合并的基准只有上一批 ⇒
**台账停止累积、每次跑批覆盖上一次**。参考项目栽过两次，第二次是
`run.py --count 1`（探测服务端是否解封）把 53 条台账**覆盖成 1 条**。

所以"合并而不是覆盖"这条规则必须**只有一处实现**，被所有会写台账的工具复用。

🔴 为什么台账里**必须存明文凭据**
--------------------------------
`api_key` 是**只在创建时返回一次**的（站点前端文案：「出于安全原因，
这串密钥只显示一次。」）。台账不存明文就等于这批账号白注册。
所以 `ledger/` **整目录被 .gitignore 排除**，且报告层只打 `mask_key()` 形式。
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import asdict, is_dataclass
from pathlib import Path

from . import fsutil

# 台账目录：仓库根下的 `ledger/`（整目录被 .gitignore 忽略 —— 里面是明文凭据）。
# ⚠ 用 `Path(__file__).resolve().parents[1]` 而不是 `Path.cwd()`：cwd 是调用者的，
#   从别的目录跑 `python tools/ops/check_keys_alive.py` 会把台账写到别处去。
ROOT = Path(__file__).resolve().parents[1]
LEDGER_DIR = ROOT / "ledger"
RUNS_DIRNAME = "runs"
SNAPSHOT_STEM = "results"
LAST_RUN_NAME = "latest.json"

# 快照文件名的**严格**形状：`results-YYYYMMDD-HHMMSS.json`。
# 🔴 用正则筛，不用 `glob("*.json")`：`runs/` 是个人工可写的目录，一个
#    `notes.json` / `results.json.bak` 落进去，按"最新"就会被当成台账读源 ——
#    于是缩水护栏的分母、`--count` 的取样池全跟着变，而且**全程不报错**。
SNAPSHOT_RE = re.compile(rf"^{SNAPSHOT_STEM}-(\d{{8}})-(\d{{6}})\.json$")

# 增量落盘日志（**append-only JSONL**）的严格形状：
#     journal-YYYYMMDD-HHMMSS-<pid>.jsonl
# 🔴 带 pid：两个进程同时跑批时各自一份，不会互相追加/截断。
# 🔴 扩展名是 `.jsonl`（**不是** `.json`）：它永远不该被 `SNAPSHOT_RE` 匹配，
#    也永远不该成为台账读源 —— 读它是 `recover_keys.py --journal` 的事。
JOURNAL_STEM = "journal"
JOURNAL_SUFFIX = ".jsonl"
JOURNAL_RE = re.compile(rf"^{JOURNAL_STEM}-(\d{{8}})-(\d{{6}})-(\d+)\.jsonl$")

# 🔴 本站点的 API Key 前缀是 **`opc_sk_`**，不是参考项目的 `sk-`。
#    抽成常量：覆盖统计与校验工具必须认同一套前缀，否则两边口径不一致
#    会互相掩盖（一边当 key、另一边当噪音）。
KEY_PREFIX = "opc_sk_"


def snapshot_path(when=None) -> Path:
    """新快照的落点：`ledger/runs/<YYYY-MM-DD>/results-<YYYYMMDD-HHMMSS>.json`。

    `when` 传 epoch 秒（不传 = 现在）。存在的唯一理由是让测试能钉住路径格式。
    """
    t = time.localtime() if when is None else time.localtime(when)
    return (
        LEDGER_DIR
        / RUNS_DIRNAME
        / time.strftime("%Y-%m-%d", t)
        / f"{SNAPSHOT_STEM}-{time.strftime('%Y%m%d-%H%M%S', t)}.json"
    )


def snapshot_paths() -> list:
    """`runs/` 下所有**命名合规**的快照，按时间**升序**。

    排序键取**文件名里的时间戳**，不取 mtime —— mtime 会被 `cp -p` / 解压 /
    同步工具改掉，而文件名是落盘那一刻写死的。`YYYYMMDD-HHMMSS` 定长零填充，
    所以字典序就是时间序。
    """
    runs = LEDGER_DIR / RUNS_DIRNAME
    if not runs.is_dir():
        return []
    found = [p for p in runs.glob(f"*/{SNAPSHOT_STEM}-*.json") if SNAPSHOT_RE.match(p.name)]
    return sorted(found, key=lambda p: (p.parent.name, p.name))


def ledger_path() -> Path:
    """台账的**唯一读源**：`runs/` 里**最新的那份全量快照**。

    🔴 每次调用都**重新扫目录**，不要冻进模块级常量：
      "先读台账 → 跑 → 再写回台账"的流程里，常量会一直指向**落盘之前**
      那份快照 ⇒ 写回去等于覆盖掉刚生成的新快照。

    没有任何快照时返回一个**永不存在的占位路径**：让 `load_existing()`
    按契约返回 `[]`，而不是抛异常 —— 一个空的台账目录不该让工具起不来。
    """
    snaps = snapshot_paths()
    if snaps:
        return snaps[-1]
    return LEDGER_DIR / RUNS_DIRNAME / "__none__" / f"{SNAPSHOT_STEM}-none.json"


def last_run_path() -> Path:
    """**本批结果**的落点：`ledger/latest.json`。⚠ 不能当读源。"""
    return LEDGER_DIR / LAST_RUN_NAME


def is_ledger_path(path) -> bool:
    """这个路径是不是落在**项目台账目录**里。

    判据是"在 `LEDGER_DIR` 底下"，**不是**"等于读源" —— 读源每次落盘都换名字，
    拿它做相等比较必然漏。
    """
    try:
        return Path(path).resolve().is_relative_to(LEDGER_DIR.resolve())
    except (OSError, ValueError):
        return False


# 记录"优劣"排序：成功 > 跳过 > 失败。合并时不让失败盖掉成功。
_RANK = {"success": 2, "skipped": 1}
_EMPTY = (None, "", {}, [])


def rank(rec: dict) -> int:
    return _RANK.get(str(rec.get("status") or ""), 0)


def is_noop_skip(rec) -> bool:
    """没有账号的“跳过”记录（`status=="skipped"` 且 `email` 为空）。

    这类记录是**纯噪音**：出口配额全满时，生产者在一个请求都没发的情况下
    把后续账号标成 `skipped`，**连邮箱都没建** —— 落盘后既不能恢复、也不
    指向任何出口，只会让读源膨胀（实测一次 `--count 73` 灌进 53 条匿名
    `skipped`，读源 36 → 109；batch73 复跑时踩到）。

    🔴 判据必须是**两个条件的与**，不能只看 `email`：
      邮箱创建失败（`status=="failed"`、`email` 也为空）是**有意义**的失败，
      必须保留；只有“skipped + 无 email”才代表“根本没开始”，才丢。
    """
    if isinstance(rec, dict):
        status, email = rec.get("status"), rec.get("email")
    else:
        status, email = getattr(rec, "status", ""), getattr(rec, "email", "")
    return str(status or "") == "skipped" and not str(email or "").strip()


def is_persistable(rec) -> bool:
    """该记录是否值得落进台账（`False` = 匿名跳过，丢掉）。"""
    return not is_noop_skip(rec)


def load_existing(path) -> list:
    """读现有台账。文件缺失 / 损坏 / 不是 list 都返回 `[]`，不抛异常。

    刻意不抛：一个坏掉的结果文件不该让整次运行失败。真正的护栏在
    `_guard_no_shrink` 的条数检查上。
    """
    p = Path(path)
    if not p.is_file():
        return []
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return []
    return [r for r in d if isinstance(r, dict)] if isinstance(d, list) else []


def merge_records(existing: list, new: list) -> tuple:
    """按 `email` 合并，返回 `(merged, 原有条数, 新增, 覆盖数)`。

    规则：
      - 按 `email` 去重；没有 `email` 的记录原样保留（不参与去重）
      - 同一 email：**不让失败盖掉成功**（服务端抖一下不该把好账号标成坏）
      - 同一 email 且 rank 相同时：**取并集**（`{**旧, **新}`）——
        新值胜出，但旧记录里独有的字段一个都不丢
      - 老记录里本次没跑到的，**保留** —— 它们是历史，不是垃圾

    🔴 为什么同级要取并集而不是"整条替换"或"比字段数"：
    "下游工具交回增量字段"（只有 `verify`，没有 `status`）时 `rank` 恒为 0，
    而旧记录 rank=2 —— `rank(new) > rank(old)` **永远为假**，字段一个都写不进去，
    而打印出来的一切都"正常"。这正是最危险的一类失败：**数据静默缩水，指标全绿**。
    并集没有这个漏洞 —— 它不靠猜，数学上保证字段只增不减。
    """
    idx: dict = {}
    merged: list = []
    for rec in existing:
        email = rec.get("email")
        if email and email in idx:
            continue  # 同 email 的旧重复记录，留第一条
        if email:
            idx[email] = len(merged)
        merged.append(rec)

    kept = len(merged)
    added = upgraded = 0
    for rec in new:
        email = rec.get("email")
        if not email or email not in idx:
            if email:
                idx[email] = len(merged)
            merged.append(rec)
            added += 1
            continue
        pos = idx[email]
        cur = merged[pos]
        r_new, r_old = rank(rec), rank(cur)
        if r_new > r_old:
            merged[pos] = rec  # 升级：整体替换
            upgraded += 1
        elif r_new == r_old:
            union = {**cur, **rec}  # 同级：并集，新值胜出
            if union != cur:
                merged[pos] = union
                upgraded += 1
    return merged, kept, added, upgraded


def apply_verdicts(records: list, verdicts: dict, *, stamp: str | None = None) -> tuple:
    """把**复验结论**写回记录，返回 `(新列表, 改动条数)`。

    `verdicts`: `{api_key: (ok, why)}` —— 由 `tools/ops/check_keys_alive.py` 收集。
    只动三处：`key_verified` / `stages["verify"]` / `stages["verify_refreshed_at"]`，
    其余字段原样保留（`recover_keys` 的教训：写回只该补字段，不该整条替换）。

    🔴 为什么需要它：台账里会留下**旧版本代码**写的 `stages.verify` 文案
      （例如“2xx 但 body 非 JSON”），而当前代码对同一情形给出的是 `False`。
      这些陈旧结论无法从代码推断，只能靠**重新复验**覆盖 —— 而结论必须能
      落回台账，否则每次看都得重跑一次复验，且报告会把 `key_verified=None`
      误读成“用户 `--no-verify`”。

    🔴 返回**新列表**（`dict` 浅拷贝后改），不改调用方传进来的对象：
      原列表可能同时是别的合并基准，就地改会让“另一个读者”看到中间态。

    ⚠ 命中不了的记录（`api_key` 为空 / 不在 `verdicts` 里）**原样保留**，
      所以调用方可以把它当作全量台账的变换，直接 `save()` 回去。
    """
    stamp = stamp or time.strftime("%Y-%m-%d %H:%M:%S")
    out: list = []
    changed = 0
    for rec in records:
        key = str(rec.get("api_key") or "")
        v = verdicts.get(key) if key else None
        if v is None:
            out.append(rec)
            continue
        ok, why = v
        new = dict(rec)
        stages = dict(new.get("stages") or {})
        stages["verify"] = why
        # 🔴 带上戳：读者要能分辨“这是当时的结论”还是“事后复验过的”。
        stages["verify_refreshed_at"] = stamp
        new["stages"] = stages
        new["key_verified"] = bool(ok)
        out.append(new)
        changed += 1
    return out, changed


def _guard_no_shrink(records: list, existing) -> None:
    """合并后条数**少于**原有就抛 `ValueError`。

    少数据但所有指标都"正常"是最坏的一类失败 —— 宁可报错也不要静默丢账号。

    🔴 **`existing=None` 不是"跳过检查"，是"没传"** —— 它只可能从
      `_guard_no_shrink` 被直接调用时出现。`save()` / `save_snapshot()` 都会在
      入口把 `None` 转成"从磁盘自动读取当前台账"：

        save(p, recs)                → existing 自动 = load_existing(p)，**护栏生效**
        save(p, recs, existing=[])   → 跳过护栏（唯一逃生口）

      这个方向是刻意的：护栏的作用就是防"默认调用悄悄缩水"，如果
      `None` 代表跳过，那**最自然的写法 `save(p, recs)` 反而没有护栏**，
      等于白装。

    ⚠ 早期 docstring 写的是"`existing=None` 表示不检查"，与实现相反，
      并因此带偏了一条测试用例（2026-09-26 修正）。
    """
    if existing is None:
        return
    if len(records) < len(existing):
        raise ValueError(
            f"拒绝写盘：合并后 {len(records)} 条 < 原有 {len(existing)} 条"
            f"（防静默缩水）。确认要覆盖请显式调用 save(..., existing=[])"
        )


def _atomic_write(path, records: list) -> None:
    """写 JSON 台账：先落同目录临时文件再原子替换。

    🔴 直接 `write_text` 有真实的半截风险：台账写到一半被 Ctrl-C，
       留下的就是**语法不完整的 JSON** ⇒ 下次 `load_existing()` 返回 `[]`
       ⇒ 合并退化成"只有本次" ⇒ **静默丢台账**。
    """
    fsutil.atomic_write_text(path, json.dumps(records, ensure_ascii=False, indent=2))


def save(path, records: list, *, existing: list | None = None) -> Path:
    """写台账到**显式路径**，带防静默缩水护栏。返回实际落盘路径。

    走台账目录请用 `save_snapshot()` —— 这个函数只写你给的那个文件，
    **不落快照**。它存在是为了 `--out keys.json` 这类"我就想导一份到别处"。
    """
    p = Path(path)
    if existing is None:
        existing = load_existing(p)
    _guard_no_shrink(records, existing)
    _atomic_write(p, records)
    return p


def save_snapshot(
    merged: list, batch: list | None = None, *, existing: list | None = None, when=None
) -> tuple:
    """正式落盘：**全量快照** + **本批结果**。返回 `(快照路径, 本批路径)`。

    三个动作，顺序不能换：
      1. 护栏 —— `merged` 条数 < `existing` 就抛 `ValueError`（**先于任何写入**）；
      2. 写快照 `ledger/runs/<日期>/results-<时间戳>.json`，内容是 `merged`；
      3. 原子刷新 `ledger/latest.json`，内容是 `batch`（**本批**）。

    🔴 快照必须是**全量**。只存本批的话，下一次 `ledger_path()` 读到的最新快照
       就只有本批那几条 ⇒ 合并等于没做 ⇒ 台账被切碎。

    🔴 先写快照、再刷本批。万一第 3 步挂了，读源仍是**上一份完整台账**，
       而新快照已经躺在 `runs/` 里可以人工捞回；反过来会变成
       "读源已更新、快照没留下"，这次落盘就没有回滚点了。
    """
    if existing is None:
        existing = load_existing(ledger_path())
    _guard_no_shrink(merged, existing)
    snap = snapshot_path(when)
    _atomic_write(snap, merged)
    last = last_run_path()
    _atomic_write(last, merged if batch is None else batch)
    return snap, last


# ── 覆盖率护栏 ────────────────────────────────────────────────────
# 覆盖不足时的退出码。与 `run.py` 的"防静默缩水"护栏同码 —— "护栏触发"
# 在仓库里只该有一个含义，别为每个工具各发明一个数字。
EXIT_COVERAGE_GAP = 3


def _coverage_of(ledger_side, known_side) -> tuple:
    """`(台账侧去重计数, 清单侧去重计数, 台账有而清单没有的集合)`。

    两侧的**空值都丢掉**：`None` / `""` 不是"一个缺失的条目"，
    把它们算进分母会让覆盖率看着更好看。
    """
    a = {v for v in ledger_side if v}
    b = {k for k in known_side if k}
    return len(a), len(b), a - b


def key_coverage(records, known_keys) -> tuple:
    """台账里的 API Key 相对一份**外部清单**的覆盖情况。

    🔴 为什么要有这个函数：校验工具（`check_keys_alive.py`）的默认输入是
      **某次导出的快照**，而它只报"存活 N/N" —— N 是**快照自己的**分母，
      不告诉你快照覆盖了多少台账。

      参考项目实测踩到：快照停在 3 天前、只有 53 把，台账里已有 407 把，
      于是跑出"53/53 全绿"这种**看着没问题、实则完全没覆盖本批**的结论。

      ⚠ 返回的 `missing` 只覆盖**台账有、清单没有**这一个方向。
    """
    return _coverage_of(
        [
            r.get("api_key")
            for r in records
            if isinstance(r, dict) and str(r.get("api_key") or "").startswith(KEY_PREFIX)
        ],
        known_keys,
    )


def account_coverage(records, known_emails) -> tuple:
    """台账里的**账号**（`email`）相对一份外部清单的覆盖情况。

    与 `key_coverage` 是同一道护栏的另一个字段：
    `key_coverage` 管"我核验了多少把 key"，`account_coverage` 管
    "我能从哪个池子里取账号"。
    """
    return _coverage_of([r.get("email") for r in records if isinstance(r, dict)], known_emails)


def _repo_rel(path) -> str:
    """路径归一化成**相对仓库根**的 posix 串；不在仓库内则原样返回。

    🔴 artifact 里不能存绝对路径：绝对路径带盘符和用户名，一旦这份 artifact
      被拷进仓库就会撞泄漏闸门。
    """
    if not path:
        return ""
    try:
        return Path(path).resolve().relative_to(ROOT).as_posix()
    except (ValueError, OSError):
        return str(path)


def coverage_block(ledger_n, known_n, missing, *, ledger=None, snapshot=None) -> dict:
    """把覆盖三元组整理成 artifact 里的 `coverage` 块。

    🔴 为什么它必须进 artifact 而不能只打印：护栏只走 stdout 的话，
      **事后单独看 artifact** 就是"53/53 全绿"，看不出它只覆盖了台账的
      53/605。stdout 不随 artifact 走，而 artifact 才是被反复引用的那份。
    """
    return {
        "ledger_n": ledger_n,
        "known_n": known_n,
        "uncovered": len(missing),
        "covers_ledger": not missing,
        "ledger": _repo_rel(ledger),
        "snapshot": _repo_rel(snapshot),
    }


def coverage_exit_code(missing, *, allow_partial: bool = False) -> int:
    """覆盖不足且未显式放行 → `EXIT_COVERAGE_GAP`，否则 0。

    🔴 为什么退出码必须非 0：护栏原来只 `print`，`main()` 照样 `return 0` ——
      任何脚本化调用都会读到"成功"。宁可让调用方炸，也不要让一份只覆盖
      53/605 的报告被当成全量结论。
    """
    if allow_partial or not missing:
        return 0
    return EXIT_COVERAGE_GAP


# ── 增量落盘日志（journal）────────────────────────────────────────
# 为什么需要它
# ------------
# `run_batch` 把本批记录攒在**内存**里，跑完才 `save_snapshot`。中途崩了
# （Ctrl-C / 网络断 / 任务管理器结束进程），已经注册+建好的账号就**只存在于
# 内存** —— 台账没有，日志里也没有密码 ⇒ `recover_keys.py` 也无从登录、
# 无法把 key 捞回来。这正是 batch73 的事故（14 把 key 未落盘且不可恢复）。
#
# 所以每完成一个账号就往**追加式 JSONL** 里写一行。它是**checkpoint，
# 不是读源**：
#   * 正常收尾后由 `run.py` 删除（`journal_finish`）；
#   * 若进程被杀，日志留在 `runs/` 下，`recover_keys.py --journal latest`
#     可把里面的账号登录、把 key 捞回台账。
#
# 🔴 为什么是 append-only：追加对「单行 < PIPE_BUF」是原子且**没有读改写窗口**。
#    若每次重写整份 JSON，写到一半被杀就会留下一份**语法不完整的文件** ——
#    从"可恢复"退化成"彻底不可读"。

# 🔴 名字必须与 `journal_begin/append/finish` 里用的完全一致（都是 `active_journal`）。
#    曾写成 `_active_journal`，于是单账号模式（从不 `journal_begin`）调到
#    `journal_finish` 直接 NameError —— 批量模式因为 begin 先把它建出来了才没暴露。
_journal_lock = threading.Lock()
active_journal: Path | None = None


def journal_path(when=None, pid=None) -> Path:
    """本次跑批日志的落点：`runs/<日期>/journal-<时间戳>-<pid>.jsonl`。"""
    t = time.localtime() if when is None else time.localtime(when)
    pid = os.getpid() if pid is None else pid
    return (
        LEDGER_DIR
        / RUNS_DIRNAME
        / time.strftime("%Y-%m-%d", t)
        / f"{JOURNAL_STEM}-{time.strftime('%Y%m%d-%H%M%S', t)}-{pid}{JOURNAL_SUFFIX}"
    )


def _rec_to_dict(rec) -> dict:
    """把 `AccountRecord`（dataclass）或 dict 统一成 dict。"""
    if isinstance(rec, dict):
        return rec
    if is_dataclass(rec) and not isinstance(rec, type):
        return asdict(rec)
    raise TypeError(f"journal_append 只接受 dict / dataclass，收到 {type(rec).__name__}")


def journal_begin(*, when=None, pid=None) -> Path:
    """开一份新的跑批日志（截断同名旧文件），并把它设为**活动日志**。返回路径。"""
    global active_journal
    p = journal_path(when, pid)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("", encoding="utf-8")
    with _journal_lock:
        active_journal = p
    return p


def journal_append(rec) -> None:
    """把一个账号的当前状态**追加**一行到活动日志（线程安全）。

    `rec` 为 `None` / 没有活动日志时静默返回 —— journal 是加速器，
    绝不能因为日志写不进去而让跑批失败（与 `quota._append_quiet` 同一原则）。
    """
    p = active_journal
    if p is None or rec is None:
        return
    if not is_persistable(rec):
        # 匿名跳过零信息量 —— 日志是给“崩掉的批”留恢复口子的，
        # 没必要为一堆没开始过的账号占行。
        return
    try:
        line = json.dumps(_rec_to_dict(rec), ensure_ascii=False)
    except (TypeError, ValueError):
        return
    try:
        # fsync：崩在"已打印成功、还没收尾"之间时，也要保证这行真的落了盘。
        with _journal_lock, p.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())
    except OSError:
        return


def journal_finish(*, remove: bool = True) -> Path | None:
    """收尾：清除活动日志指针，可选删除文件。返回被收尾的路径。

    🔴 **只在快照写成功之后**调用（见 `run.py`）—— 否则会删掉还没落进台账的
      唯一副本，把"可恢复"的崩溃重新变回"彻底丢失"。
    """
    global active_journal
    with _journal_lock:
        p = active_journal
        active_journal = None
        if p is not None and remove:
            try:
                p.unlink()
            except OSError:
                pass
    return p


def journal_paths() -> list:
    """`runs/` 下所有命名合规的跑批日志（升序）。用于发现**没收尾的批**。"""
    runs = LEDGER_DIR / RUNS_DIRNAME
    if not runs.is_dir():
        return []
    found = [
        p for p in runs.glob(f"*/{JOURNAL_STEM}-*{JOURNAL_SUFFIX}") if JOURNAL_RE.match(p.name)
    ]
    return sorted(found, key=lambda p: (p.parent.name, p.name))


def journal_load(path) -> list:
    """读跑批日志 → 按 `email` 归并（rank + 字段并集，后写者胜出）。

    归并规则直接复用 `merge_records`：
      * 同一账号的“注册完成”行与“key 建出”行**同级**，取并集 ⇒ key 不丢；
      * 若后写的是一行失败（rank 低），它**不会**盖掉前面的成功行。
    半截行（进程被杀）逐行跳过，不让它污染整个日志。
    """
    p = Path(path)
    if not p.is_file():
        return []
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return []
    acc: list = []
    for raw in text.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            rec = json.loads(raw)
        except ValueError:
            continue
        if isinstance(rec, dict):
            acc, _, _, _ = merge_records(acc, [rec])
    return acc
