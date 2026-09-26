"""pytest 全局配置。

🔴 `pythonpath = ["."]` 在 pyproject 里已经设了，这里不再 sys.path.insert。

⚠ 这里**没有** `conftest.py` 级别的真实网络访问 —— 全部用例都必须是
  离线可跑的。真实站点交互放在 `tools/probes/`，不放进测试链：
  CI 上跑真实注册会**烧掉一个出口当天的额度**，而额度上限只有 5 个/天。
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
