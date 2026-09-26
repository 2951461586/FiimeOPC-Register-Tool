"""取激活链接：**优先 Worker 的 `extracted_json`，回退正文正则**。

🔴 2026-09-26 起共享 Worker 为本站激活邮件给出 `extracted_json`
   （D1 `rules` id=21：sender `^rockssr@163\\.com$`），所以
   `opc.activation_link_from_mail()` **优先**读它；正文正则只做兜底
   （存量邮件 / 规则被误删 / 站点换发信域名）。

单独成文件：让这条新行为的钉子不去重扫 `test_opc.py` 整个文件。
"""

import json

# pytest 在 pyproject 的 dev 依赖里（真实解释器可解析）；这里加 ignore 只是因为
# pi-lens 的语言服务器用的是另一个不含 pytest 的解释器。
import pytest  # type: ignore[import-not-found]

from src import opc
from src.tempmail import Mail

LINK = "https://opc.fiime.cn/activate?token=7d88622ae13c558c9534f949468311e2b811df5b2b57f88d239f4730d13930a2"


class TestActivationLinkFromMail:
    def _mail(self, extracted_json="[]", body=""):
        return Mail(extracted_json=extracted_json, body=body)

    def test_prefers_extracted_json_over_body(self):
        """结构化字段优先 —— 即使正文里另有一条（更旧/更坏的）链接。"""
        other = "https://opc.fiime.cn/activate?token=deadbeef"
        m = self._mail(
            extracted_json=json.dumps([{"value": LINK, "type": "link"}]),
            body=f"正文里也有一条 {other}",
        )
        assert opc.activation_link_from_mail(m) == LINK

    def test_falls_back_to_body_when_extracted_empty(self):
        m = self._mail(extracted_json="[]", body=f"点击 {LINK} 激活")
        assert opc.activation_link_from_mail(m) == LINK

    def test_ignores_extracted_foreign_host(self):
        """extracted_json 里若混进别家的链接，不算激活链接；改用正文兜底。"""
        m = self._mail(
            extracted_json=json.dumps(
                [{"value": "https://evil.example.com/activate?token=x", "type": "link"}]
            ),
            body=f"点击 {LINK} 激活",
        )
        assert opc.activation_link_from_mail(m) == LINK

    def test_malformed_token_from_extracted_still_returned(self):
        """站点偶发漏 `=`（`?token<hex>`）—— extracted_json 里也是这种形状，仍要取到。"""
        link = "https://opc.fiime.cn/activate?token2f5d02a5be3e81b05087c9074fb8ad41"
        m = self._mail(extracted_json=json.dumps([{"value": link, "type": "link"}]))
        assert opc.activation_link_from_mail(m) == link

    def test_none_when_neither(self):
        m = self._mail(extracted_json="[]", body="欢迎加入，没有链接")
        assert opc.activation_link_from_mail(m) is None

    def test_accepts_duck_typed_mail(self):
        """不要求真是 `Mail` —— 只要带 `find_link()` / `body` 即可（便于测试替身）。"""
        link = LINK

        class Fake:
            body = f"see {link}"

            def find_link(self):
                return link

        assert opc.activation_link_from_mail(Fake()) == link

    def test_finder_exception_falls_back(self):
        """`find_link()` 抛异常不能拖垮整链 —— 回退正文。"""

        class BadFinder:
            body = f"see {LINK}"

            def find_link(self):
                raise RuntimeError("boom")

        assert opc.activation_link_from_mail(BadFinder()) == LINK


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
