"""`tools/ops/recover_keys.py` 的离线测试。**不联网。**

重点钉住 `--journal` 的判据：增量日志里“只走完注册+激活、key 还没建”的记录
`status` 仍是 `"init"`（`stage_login_key` 结尾才置 `success`），它必须被识别为
“可登录捞 key” —— 否则 batch73 那种中断批即便留下了日志也救不回来。

⚠ 本文件用 `importlib` 从路径加载脚本（`tools/` 不是包）。
"""

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "ops" / "recover_keys.py"


def _load():
    spec = importlib.util.spec_from_file_location("recover_keys", TOOL)
    if spec is None or spec.loader is None:  # pragma: no cover - 仅在打包异常时
        raise RuntimeError(f"无法从路径加载 {TOOL}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["recover_keys"] = mod
    spec.loader.exec_module(mod)
    return mod


def _rec(**over):
    base = {
        "email": "oai-x@example-mail.test",
        "password": "Pw#123456",
        "status": "init",
        "stages": {"register": "ok", "activate": "ok"},
    }
    base.update(over)
    return base


class TestNeedsRecovery:
    def test_accepts_registered_activated_journal_row(self):
        """🔴 日志里的“注册+激活已 ok、status 仍 init”行必须可回收。"""
        mod = _load()
        assert mod._needs_recovery(_rec(), refresh=False)

    def test_rejects_init_without_activation(self):
        mod = _load()
        assert not mod._needs_recovery(_rec(stages={"register": "ok"}), refresh=False)

    def test_rejects_failed(self):
        mod = _load()
        assert not mod._needs_recovery(_rec(status="failed"), refresh=False)

    def test_rejects_missing_password(self):
        mod = _load()
        assert not mod._needs_recovery(_rec(password=""), refresh=False)

    def test_success_with_key_is_not_recovered_unless_refresh(self):
        mod = _load()
        rec = _rec(status="success", api_key="opc_sk_x")
        assert not mod._needs_recovery(rec, refresh=False)
        assert mod._needs_recovery(rec, refresh=True)

    def test_success_without_key_needs_recovery(self):
        mod = _load()
        assert mod._needs_recovery(_rec(status="success"), refresh=False)
