"""FiimeOPC 注册 + API Key 提取工具（纯协议链路）。

链路：建邮箱 → 注册 → 收激活信 → 激活 → 登录 → 建 API Key → 校验 → 落台账。

⚠ 包名就叫 `src`（与参考项目一致）。全库用 `from src.xxx import`，
  改名是纯 churn。本项目**不要求** `pip install -e .` —— 测试靠 pytest 的
  `pythonpath`，运行靠 `run.py` 顶部的 `sys.path.insert`。
"""

__version__ = "0.1.0"
