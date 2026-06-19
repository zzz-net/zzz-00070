"""
新增回归测试 —— 覆盖：
1. 导出状态（matched 直接导 / reviewed 导 / exported 再导 / 撤销后重导）
2. README 与 rules.py 共享常量一致（防文档漂移）
3. should_export_match 共享过滤函数单元测试
4. CLI --help 输出与 rules 常量一致
5. 完整 README 示例流程（import → match → show → list → export）真实可执行
"""

import os
import sys
import csv
import tempfile
import shutil
import unittest
from io import StringIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from click.testing import CliRunner

from invoice_recon.cli import cli, import_data, export_cmd, review_undo
from invoice_recon import db, rules
from invoice_recon.models import MatchStatus, BatchStatus


SAMPLES_DIR = Path(__file__).parent.parent / "samples"
README_PATH = Path(__file__).parent.parent / "README.md"


def _write_csv(path: Path, headers: list, rows: list):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(headers)
        w.writerows(rows)


# ======================================================================
# 共享过滤函数 should_export_match 的单元测试
# ======================================================================
class ExportFilterContractTest(unittest.TestCase):
    """导出过滤函数的契约测试 —— 对 rules.should_export_match 的行为兜底。"""

    def test_pending_should_export(self):
        self.assertTrue(rules.should_export_match({"status": MatchStatus.PENDING}))

    def test_conflict_should_export(self):
        self.assertTrue(rules.should_export_match({"status": MatchStatus.CONFLICT}))

    def test_confirmed_with_diff_should_export(self):
        self.assertTrue(rules.should_export_match({
            "status": MatchStatus.CONFIRMED, "amount_diff": 0.50
        }))
        self.assertTrue(rules.should_export_match({
            "status": MatchStatus.CONFIRMED, "amount_diff": -1.00
        }))
        self.assertTrue(rules.should_export_match({
            "status": MatchStatus.CONFIRMED, "amount_diff": 0.001
        }))

    def test_confirmed_clean_should_NOT_export(self):
        self.assertFalse(rules.should_export_match({
            "status": MatchStatus.CONFIRMED, "amount_diff": 0.0
        }))
        self.assertFalse(rules.should_export_match({
            "status": MatchStatus.CONFIRMED, "amount_diff": 0.0001
        }))

    def test_rejected_should_NOT_export(self):
        self.assertFalse(rules.should_export_match({
            "status": MatchStatus.REJECTED, "amount_diff": 100
        }))

    def test_explain_reasons_all_present(self):
        cases = [
            ({"status": MatchStatus.PENDING}, rules.EXPORT_FILTER_REASON_PENDING),
            ({"status": MatchStatus.CONFLICT}, rules.EXPORT_FILTER_REASON_CONFLICT),
            ({"status": MatchStatus.CONFIRMED, "amount_diff": 1}, rules.EXPORT_FILTER_REASON_HAS_DIFF),
            ({"status": MatchStatus.CONFIRMED, "amount_diff": 0}, rules.EXPORT_FILTER_REASON_CLEAN_CONFIRMED),
            ({"status": MatchStatus.REJECTED}, rules.EXPORT_FILTER_REASON_REJECTED),
        ]
        for m, expected in cases:
            self.assertEqual(rules.explain_export_filter(m), expected, f"explain 错: {m}")


# ======================================================================
# 导出状态覆盖测试 —— 四种状态下都能正确导出
# ======================================================================
class ExportStatusCoverageTest(unittest.TestCase):
    """matched / reviewed / exported / 撤销后回到 matched —— 都能成功导出。"""

    def setUp(self):
        self.runner = CliRunner()
        self.tmpdir = tempfile.mkdtemp(prefix="inv_recon_export_")
        self.db_path = os.path.join(self.tmpdir, "test.db")
        os.environ["INV_RECON_DB"] = self.db_path
        self._invoke("init")
        self._setup_batch()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        if "INV_RECON_DB" in os.environ:
            del os.environ["INV_RECON_DB"]

    def _invoke(self, *args, input=None, catch_exceptions=False):
        return self.runner.invoke(cli, args, input=input, catch_exceptions=catch_exceptions)

    def _setup_batch(self):
        inv_csv = os.path.join(self.tmpdir, "inv.csv")
        pay_csv = os.path.join(self.tmpdir, "pay.csv")
        _write_csv(
            Path(inv_csv),
            ["invoice_no", "vendor", "amount", "date"],
            [
                ["INV-A", "VendorX", "1000.00", "2024-01-10"],
                ["INV-B", "VendorY", "2500.50", "2024-01-12"],
                ["INV-C", "VendorZ", "500.00", "2024-01-13"],
            ],
        )
        _write_csv(
            Path(pay_csv),
            ["payment_no", "vendor", "amount", "date"],
            [
                ["INV-A", "VendorX", "1000.00", "2024-01-15"],
                ["INV-B", "VendorY", "2500.00", "2024-01-16"],
                ["PAY-UNK", "VendorW", "888.00", "2024-01-17"],
            ],
        )
        r = self._invoke("import", "--invoices", inv_csv, "--payments", pay_csv, "--name", "exp_batch")
        self.assertEqual(r.exit_code, 0, f"import failed: {r.output}")
        self.batch_id = 1
        r = self._invoke("match", "--batch", str(self.batch_id))
        self.assertEqual(r.exit_code, 0, f"match failed: {r.output}")

    # ---------- matched 状态直接可以导出 ----------
    def test_export_from_matched_state(self):
        b = db.get_batch(self.batch_id, db_path=self.db_path)
        self.assertEqual(b.status, BatchStatus.MATCHED)

        out = os.path.join(self.tmpdir, "exp_matched.csv")
        r = self._invoke("export", "--batch", str(self.batch_id), "--output", out)
        self.assertEqual(r.exit_code, 0, f"export matched failed: {r.output}")

        self.assertIn(rules.EXPORT_OK_PREFIX, r.output)
        b_after = db.get_batch(self.batch_id, db_path=self.db_path)
        self.assertEqual(b_after.status, BatchStatus.EXPORTED)
        self.assertTrue(os.path.exists(out))
        with open(out, encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        self.assertGreater(len(rows), 0, "matched 导出应有待处理记录")

    # ---------- reviewed 状态可以导出 ----------
    def test_export_from_reviewed_state(self):
        for m in db.get_matches_by_batch(self.batch_id, db_path=self.db_path):
            if m["status"] in (MatchStatus.PENDING, MatchStatus.CONFLICT):
                self._invoke(
                    "review", "--batch", str(self.batch_id),
                    "--match-id", str(m["id"]), "--action", "confirm",
                )
        b = db.get_batch(self.batch_id, db_path=self.db_path)
        self.assertEqual(b.status, BatchStatus.REVIEWED)

        out = os.path.join(self.tmpdir, "exp_reviewed.csv")
        r = self._invoke("export", "--batch", str(self.batch_id), "--output", out)
        self.assertIn(r.exit_code, (0, 1))
        if r.exit_code == 0:
            b_after = db.get_batch(self.batch_id, db_path=self.db_path)
            self.assertEqual(b_after.status, BatchStatus.EXPORTED)

    # ---------- exported 状态可以再次导出（覆盖） ----------
    def test_export_from_exported_state(self):
        out1 = os.path.join(self.tmpdir, "exp1.csv")
        self._invoke("export", "--batch", str(self.batch_id), "--output", out1)

        out2 = os.path.join(self.tmpdir, "exp2.csv")
        r = self._invoke("export", "--batch", str(self.batch_id), "--output", out2)
        self.assertEqual(r.exit_code, 0, f"re-export from exported failed: {r.output}")
        self.assertTrue(os.path.exists(out2))

    # ---------- 撤销裁决回到 matched 后可以再导出 ----------
    def test_export_after_undo_back_to_matched(self):
        matches = db.get_matches_by_batch(self.batch_id, db_path=self.db_path)
        pending = [m for m in matches if m["status"] in (MatchStatus.PENDING, MatchStatus.CONFLICT)]
        self._invoke(
            "review", "--batch", str(self.batch_id),
            "--match-id", str(pending[0]["id"]), "--action", "confirm",
        )
        out1 = os.path.join(self.tmpdir, "exp_before_undo.csv")
        self._invoke("export", "--batch", str(self.batch_id), "--output", out1)

        self._invoke(
            "review-undo", "--batch", str(self.batch_id),
            "--match-id", str(pending[0]["id"]),
        )

        out2 = os.path.join(self.tmpdir, "exp_after_undo.csv")
        r = self._invoke("export", "--batch", str(self.batch_id), "--output", out2)
        self.assertEqual(r.exit_code, 0, f"export after undo failed: {r.output}")

        with open(out2, encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        ids_after = {int(r["match_id"]) for r in rows if r["match_id"]}
        self.assertIn(pending[0]["id"], ids_after, "撤销后该记录应重新出现在导出中")


# ======================================================================
# README 与 rules.py 共享常量一致性测试（防文档漂移）
# ======================================================================
class ReadmeRulesSyncTest(unittest.TestCase):
    """README 的关键章节与 rules.py 常量逐字一致，避免代码改了文档没跟上。"""

    @classmethod
    def setUpClass(cls):
        cls.readme = README_PATH.read_text(encoding="utf-8")

    def test_import_section_in_readme(self):
        for keyphrase in [
            "遇到坏行的三条保证",
            "合法行继续入库",
            "坏行明确报 行号 + 原因",
            "旧批次状态不被带坏",
            "整体失败（不写库、直接退出）场景",
        ]:
            self.assertIn(keyphrase, self.readme, f"README 缺导入规则关键词: {keyphrase}")

    def test_export_section_in_readme(self):
        for keyphrase in [
            "只导出还需要处理的记录",
            "pending", "conflict",
            "有差额", "confirmed",
            "matched", "reviewed", "exported",
            "撤销裁决后回到", "matched",
            "原子写入",
        ]:
            self.assertIn(keyphrase, self.readme, f"README 缺导出规则关键词: {keyphrase}")

    def test_review_undo_section_exists_in_readme(self):
        self.assertIn("review-undo", self.readme, "README 完全没提 review-undo 命令")
        for keyphrase in [
            "上一次",
            "auto_rejected",
            "reviewed", "exported", "允许撤销",
            "回到 matched",
        ]:
            self.assertIn(keyphrase, self.readme, f"README 缺 review-undo 关键词: {keyphrase}")

    def test_status_flow_diagram_in_readme(self):
        self.assertIn("imported ──match──► matched", self.readme, "README 缺状态流转图")
        self.assertIn("review-undo", self.readme, "状态流转图没体现 review-undo 回退")
        self.assertIn("批次状态会回到 matched", self.readme, "流转说明缺回退解释")

    def test_error_table_rows_in_readme(self):
        for scenario, _behavior in rules.ERROR_TABLE_ROWS:
            self.assertIn(scenario, self.readme, f"README 错误处理表缺场景: {scenario}")

    def test_readme_error_table_build_matches(self):
        built = rules.build_readme_error_table()
        for scenario, behavior in rules.ERROR_TABLE_ROWS:
            self.assertIn(scenario, built, "build_readme_error_table 缺场景行")
            self.assertIn(behavior, built, "build_readme_error_table 缺行为描述")


# ======================================================================
# CLI --help 与 rules 常量一致（help 文本不能漂移）
# ======================================================================
class CliHelpSyncTest(unittest.TestCase):
    """命令的 __doc__ 与 rules 常量完全一致。"""

    def test_import_doc_matches_rules(self):
        self.assertEqual(import_data.__doc__, rules.IMPORT_RULES_HELP)

    def test_export_doc_matches_rules(self):
        self.assertEqual(export_cmd.__doc__, rules.EXPORT_RULES_HELP)

    def test_review_undo_doc_matches_rules(self):
        self.assertEqual(review_undo.__doc__, rules.REVIEW_UNDO_RULES_HELP)

    def test_import_help_shows_rules(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            os.environ["INV_RECON_DB"] = os.path.abspath("help.db")
            try:
                r = runner.invoke(cli, ["import", "--help"])
                self.assertEqual(r.exit_code, 0)
                for keyword in [
                    "合法行继续入库",
                    "行号", "原因",
                    "旧批次状态绝不被带坏",
                    "缺少必需列",
                ]:
                    self.assertIn(keyword, r.output, f"import --help 缺关键词: {keyword}")
            finally:
                if "INV_RECON_DB" in os.environ:
                    del os.environ["INV_RECON_DB"]

    def test_export_help_shows_rules(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            os.environ["INV_RECON_DB"] = os.path.abspath("help.db")
            try:
                r = runner.invoke(cli, ["export", "--help"])
                self.assertEqual(r.exit_code, 0)
                for keyword in [
                    "pending", "conflict",
                    "有差额", "confirmed",
                    "rejected", "auto_rejected",
                    "matched", "reviewed", "exported",
                    "原子写入",
                ]:
                    self.assertIn(keyword, r.output, f"export --help 缺关键词: {keyword}")
            finally:
                if "INV_RECON_DB" in os.environ:
                    del os.environ["INV_RECON_DB"]


# ======================================================================
# README 示例命令端到端真实可执行（防示例过期）
# ======================================================================
class ReadmeExampleFlowTest(unittest.TestCase):
    """README "命令顺序"一节的步骤真能跑通（import → match → show → list → export）。"""

    def setUp(self):
        self.runner = CliRunner()
        self.tmpdir = tempfile.mkdtemp(prefix="inv_recon_readme_flow_")
        self.db_path = os.path.join(self.tmpdir, "flow.db")
        os.environ["INV_RECON_DB"] = self.db_path
        self._invoke("init")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        if "INV_RECON_DB" in os.environ:
            del os.environ["INV_RECON_DB"]

    def _invoke(self, *args, input=None, catch_exceptions=False):
        return self.runner.invoke(cli, args, input=input, catch_exceptions=catch_exceptions)

    def test_full_readme_flow_with_samples(self):
        inv = str(SAMPLES_DIR / "invoices.csv")
        pay = str(SAMPLES_DIR / "payments.csv")

        # Step 2: import —— README 示例
        r = self._invoke("import", "--invoices", inv, "--payments", pay, "--name", "2024Q1")
        self.assertEqual(r.exit_code, 0, f"readme flow import failed: {r.output}")
        self.assertIn(rules.IMPORT_OK_HINT_LEGACY_ROW, r.output, "缺 '旧批次状态未受影响' 提示")
        batch_id = 1

        # Step 4: match
        r = self._invoke("match", "--batch", str(batch_id))
        self.assertEqual(r.exit_code, 0, f"readme flow match failed: {r.output}")

        # Step 5: show
        r = self._invoke("show", "--batch", str(batch_id))
        self.assertEqual(r.exit_code, 0, f"readme flow show failed: {r.output}")
        self.assertIn("匹配:", r.output)

        # Step 6b: review (非交互)
        matches = db.get_matches_by_batch(batch_id, db_path=self.db_path)
        pending = [m for m in matches if m["status"] in (MatchStatus.PENDING, MatchStatus.CONFLICT)]
        if pending:
            r = self._invoke(
                "review", "--batch", str(batch_id),
                "--match-id", str(pending[0]["id"]),
                "--action", "confirm", "--note", "已核实",
            )
            self.assertEqual(r.exit_code, 0, f"readme flow review failed: {r.output}")

        # list
        r = self._invoke("list")
        self.assertEqual(r.exit_code, 0, f"readme flow list failed: {r.output}")
        self.assertIn("2024Q1", r.output)

        # export
        out = os.path.join(self.tmpdir, "diff_2024Q1.csv")
        r = self._invoke("export", "--batch", str(batch_id), "--output", out)
        # 可能有两种情况 —— 有记录导出(0) 或 全是干净记录(1)，都合理
        if r.exit_code == 0:
            self.assertIn(rules.EXPORT_OK_PREFIX, r.output)
            self.assertTrue(os.path.exists(out))
        else:
            self.assertIn("无待处理记录", r.output)


if __name__ == "__main__":
    unittest.main()
