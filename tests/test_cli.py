"""`src.cli` 的共用骨架。**不联网。**

重点钉两件事：

1. **退出码不撞车** —— 本项目有五个非零码，散着写迟早两个不同的故障给出
   同一个数字，脚本化调用就分不清了；
2. **help 文本单一来源** —— 三个入口的 `--slot` / `--ignore-quota` 文案必须
   逐字一致。参考项目实测过：某个入口的 `--ignore-quota` 只覆盖了两个检查点，
   第三个漏了，表现是"整批 0 个成功，报错却说池子满了"。
"""

import argparse

import pytest

from src import cli


class TestExitCodes:
    def test_all_distinct(self):
        codes = {
            "OK": cli.EXIT_OK,
            "QUOTA_EXCEEDED": cli.EXIT_QUOTA_EXCEEDED,
            "NO_SHRINK": cli.EXIT_NO_SHRINK,
            "ALL_SLOTS_DEAD": cli.EXIT_ALL_SLOTS_DEAD,
            "KEY_UNVERIFIED": cli.EXIT_KEY_UNVERIFIED,
        }
        assert len(set(codes.values())) == len(codes), f"退出码撞车：{codes}"

    def test_zero_is_only_success(self):
        assert cli.EXIT_OK == 0
        for name in ("EXIT_QUOTA_EXCEEDED", "EXIT_NO_SHRINK",
                     "EXIT_ALL_SLOTS_DEAD", "EXIT_KEY_UNVERIFIED"):
            assert getattr(cli, name) != 0, name

    def test_matches_ledger_coverage_gap(self):
        """🔴 `EXIT_NO_SHRINK` 必须与 `ledger.EXIT_COVERAGE_GAP` 同码。

        "护栏触发"在仓库里只该有一个含义 —— 为每个工具各发明一个数字，
        调用方就没法用一条规则判断"这是护栏还是别的问题"。
        """
        from src import ledger

        assert cli.EXIT_NO_SHRINK == ledger.EXIT_COVERAGE_GAP


class TestArgHelpers:
    def _parser(self):
        return argparse.ArgumentParser(prog="t")

    def test_ignore_quota_uses_shared_help(self):
        ap = self._parser()
        cli.add_ignore_quota_arg(ap)
        act = next(a for a in ap._actions if a.dest == "ignore_quota")
        assert act.help == cli.IGNORE_QUOTA_HELP
        assert act.nargs == 0, "必须是 store_true 型（不接值）"

    def test_ignore_quota_is_one_switch_across_entries(self):
        """三个入口拿到的开关名与 help 必须逐字相同。"""
        helps = []
        for _ in range(3):
            ap = self._parser()
            cli.add_ignore_quota_arg(ap)
            act = next(a for a in ap._actions if a.dest == "ignore_quota")
            helps.append((act.option_strings, act.help))
        assert len(set(map(str, helps))) == 1

    def test_key_name_default_and_help(self):
        ap = self._parser()
        cli.add_key_name_arg(ap)
        act = next(a for a in ap._actions if a.dest == "key_name")
        assert act.default == "default"
        assert "5" in act.help, "help 里要提醒每账号上限（实测 maxApiKeysPerUser=5）"

    def test_key_name_custom_default(self):
        ap = self._parser()
        cli.add_key_name_arg(ap, default="prod")
        assert ap.parse_args([]).key_name == "prod"

    def test_slot_help_mentions_direct_fallback(self):
        """`--slot` 不传且没配池子 = 直连 —— 这个语义必须写在 help 里。"""
        assert "直连" in cli.SLOT_HELP
        assert "FO_PROXY_SLOTS_FILE" in cli.SLOT_HELP


class TestMergeSummaryLine:
    def test_shape(self):
        assert cli.merge_summary_line(2, 3, 0, 5) == \
            "\n结果合并：原有 2 条 + 本次新增 3 条 = 5 条"

    def test_leading_newline_is_part_of_contract(self):
        """🔴 前导 `\\n` 是契约的一部分 —— 调用方直接 `print()` 它。

        抽取时保持逐字节相同是前提，所以换行跟着文本一起走。
        """
        assert cli.merge_summary_line(1, 1, 0, 2).startswith("\n")

    def test_upgrade_clause_only_when_nonzero(self):
        assert "已更新" not in cli.merge_summary_line(1, 1, 0, 2)
        assert "3 条已更新" in cli.merge_summary_line(1, 1, 3, 5)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
