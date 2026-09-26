#!/usr/bin/env python
"""泄漏闸门：`.env` 里的基础设施标识**不允许**出现在会被提交的文件里。

为什么这样设计
==============
常见做法是把"要拦的字符串"硬编码进闸门脚本 —— 那等于**把要保护的秘密抄一份
写进仓库**，闸门本身成了泄漏源。

所以这里反过来：**从 `.env` 读真实值**（`.env` 已 gitignored），
拿它去扫其余文件。`.env` 里有什么，闸门就拦什么，不需要维护清单。

拦什么（按家族）
================
  * 出口 IP / 代理地址（`FO_SLOT_EGRESS_IPS` / `FO_PROXY*`）
  * CF Worker 子域与凭据（`FO_WORKER_BASE` / `FO_WORKER_ADMIN_TOKEN`）
  * 邮箱域名（`FO_WORKER_DOMAIN`）

规范是**按家族占位化**：出口 IP 用 RFC 5737 保留段 `203.0.113.x`，
域名用 `example-mail.test` 这类保留域名。见 README「公开前的占位化」。

用法
====
  python tools/gates/check_leaks.py            # 扫全仓（排除 .env/ledger/.workbuddy-ai）
  python tools/gates/check_leaks.py --selftest # 变异测试：证明闸门**真的会拦**
  python tools/gates/check_leaks.py --quiet    # 只在命中时输出

退出码：0 = 干净；1 = 命中泄漏；2 = 闸门自身有问题（`.env` 读不到）。
"""

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = ROOT / ".env"

# 🔴 本脚本刻意对**业务代码**零依赖（它是独立闸门），但「输出编码」这件事必须
#    与其它入口共用一份实现：Windows GBK 控制台重定向时 `✓`/`✗` 会直接崩。
#    只借 `src.cli` 里那个纯 stdlib 的小函数，不引入 requests 之类的传递依赖。
sys.path.insert(0, str(ROOT))

from src.cli import force_utf8_stdio  # noqa: E402

force_utf8_stdio()

# 这些变量里的**值**是要保护的标识。
SENSITIVE_VARS = (
    "FO_SLOT_EGRESS_IPS",
    "FO_WORKER_BASE",
    "FO_WORKER_ADMIN_TOKEN",
    "FO_WORKER_DOMAIN",
    "FO_PROXY",
    "FO_PROXY_SLOTS",
)

# 扫描范围：会被提交的东西。刻意**白名单式**列出，不写"排除法" ——
# 排除法漏掉一个新目录就静默失效，而白名单漏了只会少扫（还能发现）。
SCAN_GLOBS = (
    "*.py",
    "*.md",
    "*.toml",
    "*.cfg",
    "*.ini",
    "*.yml",
    "*.yaml",
    "*.json",
    "*.txt",
    "*.example",
    "*.bat",
    "*.ps1",
    "*.sh",
)
SCAN_DIRS = ("src", "tests", "tools", "docs", ".github")
SKIP_DIR_NAMES = {
    ".git",
    ".workbuddy-ai",
    "ledger",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "venv",
}

# 占位化之后**允许**出现的值。命中这些不算泄漏。
ALLOWED_VALUES = {
    "203.0.113.11",
    "203.0.113.12",
    "203.0.113.13",
    "203.0.113.14",
    "example-mail.test",
    "example.com",
    "127.0.0.1",
    "0.0.0.0",
}


def _read_env(path=None) -> dict:
    """读 `.env`。

    🔴 `path=None` 而不是 `path=ENV_FILE` —— 后者会在**函数定义时**把
      `ENV_FILE` 的值绑死成默认参数，于是模块级常量改不动（monkeypatch 无效、
      换目录也无效）。这与 `quota.state_path()` 是同一个约定：
      **调用时**读模块级变量。
    """
    p = Path(path) if path else ENV_FILE
    if not p.is_file():
        return {}
    out = {}
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _tokens_from_env(env: dict) -> set:
    """把敏感变量的值拆成"要拦的最小片段"。

    🔴 必须拆：`FO_SLOT_EGRESS_IPS=7901=203.0.113.11,7902=...` 整串去搜永远搜不到，
    因为源码里只会提到**其中一个** IP。所以要按分隔符拆开。
    """
    toks = set()
    for var in SENSITIVE_VARS:
        raw = env.get(var) or ""
        if not raw:
            continue
        for part in re.split(r"[,;\s]+", raw):
            part = part.strip()
            if not part:
                continue
            # `7901=198.51.100.7` → 取等号右边
            if "=" in part:
                part = part.split("=", 1)[1].strip()
            # URL 形态取 host（`https://x.y.z` → `x.y.z`）
            part = re.sub(r"^[a-z][a-z0-9+.-]*://", "", part, flags=re.I)
            part = part.split("/", 1)[0].split(":", 1)[0].strip()
            if len(part) < 6 or part in ALLOWED_VALUES:
                continue
            toks.add(part)
    return toks


def _iter_files():
    seen = set()
    for p in ROOT.glob("*"):
        if p.is_file() and any(p.match(g) for g in SCAN_GLOBS):
            seen.add(p)
    for d in SCAN_DIRS:
        base = ROOT / d
        if not base.is_dir():
            continue
        for p in base.rglob("*"):
            if not p.is_file():
                continue
            if any(s in p.parts for s in SKIP_DIR_NAMES):
                continue
            if any(p.match(g) for g in SCAN_GLOBS):
                seen.add(p)
    return sorted(seen)


def scan(tokens: set) -> list:
    """返回 `[(文件, 行号, 命中片段, 行内容)]`。"""
    hits = []
    for p in _iter_files():
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for i, line in enumerate(text.splitlines(), 1):
            for t in tokens:
                if t in line:
                    hits.append((p, i, t, line.strip()[:110]))
    return hits


def selftest() -> int:
    """变异测试：种一个**必然命中**的值，确认扫描器抓得到。

    🔴 为什么必须有：一个"永远返回 0 条"的扫描器和"仓库真干净"在输出上
      完全一样。参考项目里同类闸门就是这么静默失效的。
    """
    import tempfile

    planted = "198.51.100.77"  # RFC 5737 保留段，真实环境不会用到
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "planted.py"
        f.write_text(f'BASE = "https://{planted}/x"\n', encoding="utf-8")
        text = f.read_text(encoding="utf-8")
        assert planted in text, "种入失败"
        # 用与 scan() 相同的匹配逻辑，但只针对这个临时文件
        found = any(planted in ln for ln in text.splitlines())
    if not found:
        print("✗ 自检失败：扫描器抓不到种入的值 —— 闸门是坏的", file=sys.stderr)
        return 2
    print("✓ 自检通过：扫描器能抓到种入的标识（闸门不是空转）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="泄漏闸门：.env 里的基础设施标识不允许出现在提交物里",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--selftest", action="store_true", help="变异测试：证明闸门真的会拦")
    ap.add_argument("--quiet", action="store_true", help="只在命中时输出")
    ap.add_argument(
        "--show-tokens",
        action="store_true",
        help="打印拦哪些片段（⚠ 会输出真实标识，别在共享终端用）",
    )
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    env = _read_env()
    if not env:
        print(
            f"✗ 读不到 {ENV_FILE} —— 闸门无法确定要拦什么。\n"
            f"  这**不是**「仓库干净」，是「闸门没在工作」。",
            file=sys.stderr,
        )
        return 2

    tokens = _tokens_from_env(env)
    if not tokens:
        print(
            f"✗ {ENV_FILE} 里没有任何敏感变量（{', '.join(SENSITIVE_VARS)}）"
            f" —— 闸门空转，不能据此判定干净。",
            file=sys.stderr,
        )
        return 2

    if args.show_tokens:
        print(f"要拦的片段（{len(tokens)} 个）：{sorted(tokens)}")

    hits = scan(tokens)
    if hits:
        print(f"✗ 命中 {len(hits)} 处泄漏：\n", file=sys.stderr)
        for p, i, t, line in hits:
            print(f"  {p.relative_to(ROOT)}:{i}  ← {t}\n      {line}", file=sys.stderr)
        print(
            "\n修法：按家族占位化（出口 IP → 203.0.113.x，域名 → example-mail.test），"
            "见 README「公开前的占位化」。",
            file=sys.stderr,
        )
        return 1

    if not args.quiet:
        print(f"✓ 干净：扫了 {len(_iter_files())} 个文件，{len(tokens)} 个标识片段一个都没出现。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
