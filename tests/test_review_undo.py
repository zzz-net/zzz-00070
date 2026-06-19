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

from invoice_recon.cli import cli
from invoice_recon import db
from invoice_recon.models import MatchStatus, BatchStatus


SAMPLES_DIR = Path(__file__).parent.parent / "samples"


def _write_csv(path: Path, headers: list, rows: list):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(headers)
        w.writerows(rows)


class ReviewUndoRegressionTest(unittest.TestCase):
    def setUp(self):
        self.runner = CliRunner()
        self.tmpdir = tempfile.mkdtemp(prefix="inv_recon_test_")
        self.db_path = os.path.join(self.tmpdir, "test.db")
        os.environ["INV_RECON_DB"] = self.db_path
        self._invoke("init")
        self._setup_conflict_batch()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        if "INV_RECON_DB" in os.environ:
            del os.environ["INV_RECON_DB"]

    def _invoke(self, *args, input=None, catch_exceptions=False):
        return self.runner.invoke(cli, args, input=input, catch_exceptions=catch_exceptions)

    def _setup_conflict_batch(self):
        inv_csv = os.path.join(self.tmpdir, "inv.csv")
        pay_csv = os.path.join(self.tmpdir, "pay.csv")
        _write_csv(
            Path(inv_csv),
            ["invoice_no", "vendor", "amount", "date"],
            [
                ["INV-C1", "VendorX", "1500.00", "2024-01-10"],
                ["INV-C2", "VendorY", "800.00", "2024-01-12"],
            ],
        )
        _write_csv(
            Path(pay_csv),
            ["payment_no", "vendor", "amount", "date"],
            [
                ["PAY-A", "VendorX", "1500.00", "2024-01-15"],
                ["PAY-B", "VendorX", "1500.00", "2024-01-16"],
                ["PAY-C2", "VendorY", "799.50", "2024-01-20"],
            ],
        )
        r = self._invoke("import", "--invoices", inv_csv, "--payments", pay_csv, "--name", "test_conflict")
        self.assertEqual(r.exit_code, 0, f"import failed: {r.output}")
        self.batch_id = 1
        r = self._invoke("match", "--batch", str(self.batch_id))
        self.assertEqual(r.exit_code, 0, f"match failed: {r.output}")
        conflicts = self._find_conflicts()
        self.assertGreaterEqual(len(conflicts), 2, f"setup: need >= 2 conflicts, got {len(conflicts)}; matches={self._get_matches()}")

    def _get_matches(self):
        return db.get_matches_by_batch(self.batch_id, db_path=self.db_path)

    def _find_by_status(self, status):
        return [m for m in self._get_matches() if m["status"] == status]

    def _find_conflicts(self):
        return self._find_by_status(MatchStatus.CONFLICT)

    # ------------------------------------------------------------------
    # Test 1: 撤销 confirm 后，关联 auto_rejected 冲突记录恢复可复核
    # ------------------------------------------------------------------
    def test_undo_confirm_restores_sibling_conflicts(self):
        conflicts = self._find_conflicts()
        self.assertGreaterEqual(len(conflicts), 2, f"need at least 2 conflicts, got {len(conflicts)}")
        c1, c2 = conflicts[0], conflicts[1]

        r = self._invoke(
            "review", "--batch", str(self.batch_id),
            "--match-id", str(c1["id"]),
            "--action", "confirm",
            "--note", "选了第一笔",
        )
        self.assertEqual(r.exit_code, 0, f"review confirm failed: {r.output}")

        matches_after = self._get_matches()
        m1 = next(m for m in matches_after if m["id"] == c1["id"])
        m2 = next(m for m in matches_after if m["id"] == c2["id"])
        self.assertEqual(m1["status"], MatchStatus.CONFIRMED)
        self.assertEqual(m1["review_note"], "选了第一笔")
        self.assertEqual(m2["status"], MatchStatus.REJECTED)
        self.assertEqual(m2["adjudication"], "auto_rejected")

        r = self._invoke("review-undo", "--batch", str(self.batch_id), "--match-id", str(c1["id"]))
        self.assertEqual(r.exit_code, 0, f"review-undo failed: {r.output}")
        self.assertIn("关联冲突记录已恢复可复核", r.output)

        matches_undo = self._get_matches()
        u1 = next(m for m in matches_undo if m["id"] == c1["id"])
        u2 = next(m for m in matches_undo if m["id"] == c2["id"])
        self.assertEqual(u1["status"], MatchStatus.CONFLICT, f"c1 should be conflict, got {u1['status']}")
        self.assertEqual(u1["review_note"], None, "撤销后备注应恢复为撤销前的状态")
        self.assertEqual(u2["status"], MatchStatus.CONFLICT, f"c2 should be conflict (restored), got {u2['status']}")
        self.assertIsNone(u2["adjudication"], "sibling adjudication should be cleared")

    # ------------------------------------------------------------------
    # Test 2: 撤销后 show / list 输出反映正确状态
    # ------------------------------------------------------------------
    def test_show_and_list_after_undo(self):
        conflicts = self._find_conflicts()
        c1 = conflicts[0]

        self._invoke("review", "--batch", str(self.batch_id),
                     "--match-id", str(c1["id"]), "--action", "confirm", "--note", "abc")
        self._invoke("review-undo", "--batch", str(self.batch_id), "--match-id", str(c1["id"]))

        r = self._invoke("show", "--batch", str(self.batch_id))
        self.assertEqual(r.exit_code, 0, f"show failed: {r.output}")
        self.assertIn("conflict", r.output.lower())
        self.assertNotIn("选了第一笔", r.output)

        r = self._invoke("list")
        self.assertEqual(r.exit_code, 0, f"list failed: {r.output}")
        self.assertIn("test_conflict", r.output)

        b = db.get_batch(self.batch_id, db_path=self.db_path)
        self.assertEqual(b.status, BatchStatus.MATCHED)

    # ------------------------------------------------------------------
    # Test 3: 导出只含待处理记录，撤销后重新导出结果正确
    # ------------------------------------------------------------------
    def test_export_only_pending_and_diff_after_undo(self):
        conflicts = self._find_conflicts()
        c1, c2 = conflicts[0], conflicts[1]
        pending_all = self._find_by_status(MatchStatus.PENDING)
        self.assertGreaterEqual(len(pending_all), 1)

        self._invoke(
            "review", "--batch", str(self.batch_id),
            "--match-id", str(c1["id"]), "--action", "confirm", "--note", "note1",
        )
        for pm in pending_all:
            self._invoke(
                "review", "--batch", str(self.batch_id),
                "--match-id", str(pm["id"]), "--action", "confirm",
            )

        out1 = os.path.join(self.tmpdir, "exp1.csv")
        r = self._invoke("export", "--batch", str(self.batch_id), "--output", out1)
        self.assertEqual(r.exit_code, 0, f"export1 failed: {r.output}")
        with open(out1, encoding="utf-8") as f:
            rows1 = list(csv.DictReader(f))
        ids_exported_1 = {int(r["match_id"]) for r in rows1 if r["match_id"]}
        self.assertNotIn(c1["id"], ids_exported_1, "零差额已确认不应导出")
        self.assertNotIn(c2["id"], ids_exported_1, "已拒绝 (auto_rejected) 不应导出")

        self._invoke("review-undo", "--batch", str(self.batch_id), "--match-id", str(c1["id"]))

        out2 = os.path.join(self.tmpdir, "exp2.csv")
        r = self._invoke("export", "--batch", str(self.batch_id), "--output", out2)
        self.assertEqual(r.exit_code, 0, f"export2 failed: {r.output}")
        with open(out2, encoding="utf-8") as f:
            rows2 = list(csv.DictReader(f))
        ids_exported_2 = {int(r["match_id"]) for r in rows2 if r["match_id"]}
        self.assertIn(c1["id"], ids_exported_2, "撤销后应重新出现在导出中")
        self.assertIn(c2["id"], ids_exported_2, "关联冲突撤销后应重新出现在导出中")

    # ------------------------------------------------------------------
    # Test 4: 模拟重启后（关闭所有连接再重新打开）数据持久正确，可再次复核与导出
    # ------------------------------------------------------------------
    def test_restart_persistence_and_re_review(self):
        conflicts = self._find_conflicts()
        c1, c2 = conflicts[0], conflicts[1]

        self._invoke(
            "review", "--batch", str(self.batch_id),
            "--match-id", str(c1["id"]), "--action", "confirm", "--note", "noteA",
        )
        self._invoke("review-undo", "--batch", str(self.batch_id), "--match-id", str(c1["id"]))

        # 关闭所有连接（模拟进程重启）
        import sqlite3
        sqlite3.connect(self.db_path).close()

        # 重新加载：重新获取 db 状态
        matches_after_restart = db.get_matches_by_batch(self.batch_id, db_path=self.db_path)
        u1 = next(m for m in matches_after_restart if m["id"] == c1["id"])
        u2 = next(m for m in matches_after_restart if m["id"] == c2["id"])
        self.assertEqual(u1["status"], MatchStatus.CONFLICT)
        self.assertEqual(u2["status"], MatchStatus.CONFLICT)
        b = db.get_batch(self.batch_id, db_path=self.db_path)
        self.assertEqual(b.status, BatchStatus.MATCHED)

        # 重启后可以选择另一条匹配（c2）
        r = self._invoke(
            "review", "--batch", str(self.batch_id),
            "--match-id", str(c2["id"]), "--action", "confirm", "--note", "改选第二笔",
        )
        self.assertEqual(r.exit_code, 0, f"re-review c2 failed: {r.output}")

        matches_final = db.get_matches_by_batch(self.batch_id, db_path=self.db_path)
        f1 = next(m for m in matches_final if m["id"] == c1["id"])
        f2 = next(m for m in matches_final if m["id"] == c2["id"])
        self.assertEqual(f2["status"], MatchStatus.CONFIRMED)
        self.assertEqual(f2["review_note"], "改选第二笔")
        self.assertEqual(f1["status"], MatchStatus.REJECTED)
        self.assertEqual(f1["adjudication"], "auto_rejected")

        out = os.path.join(self.tmpdir, "exp_restart.csv")
        r = self._invoke("export", "--batch", str(self.batch_id), "--output", out)
        self.assertEqual(r.exit_code, 0, f"final export failed: {r.output}")
        with open(out, encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        ids_final = {int(r["match_id"]) for r in rows if r["match_id"]}
        self.assertNotIn(c2["id"], ids_final, "零差额已确认 c2 不应导出")
        self.assertNotIn(c1["id"], ids_final, "已拒绝 c1 不应导出")

    # ------------------------------------------------------------------
    # Test 5: 撤销 reject 的场景（非冲突记录）
    # ------------------------------------------------------------------
    def test_undo_reject_non_conflict(self):
        pending = self._find_by_status(MatchStatus.PENDING)
        self.assertGreaterEqual(len(pending), 1)
        p = pending[0]

        self._invoke(
            "review", "--batch", str(self.batch_id),
            "--match-id", str(p["id"]), "--action", "reject", "--note", "拒了",
        )
        m_after = db.get_match(p["id"], db_path=self.db_path)
        self.assertEqual(m_after["status"], MatchStatus.REJECTED)
        self.assertEqual(m_after["review_note"], "拒了")

        r = self._invoke("review-undo", "--batch", str(self.batch_id), "--match-id", str(p["id"]))
        self.assertEqual(r.exit_code, 0, f"undo reject failed: {r.output}")

        m_undo = db.get_match(p["id"], db_path=self.db_path)
        self.assertEqual(m_undo["status"], MatchStatus.PENDING)
        self.assertIsNone(m_undo["review_note"])


class ImportBadRowsTest(unittest.TestCase):
    """独立测试类：导入坏行的错误报告和旧批次保护"""

    def setUp(self):
        self.runner = CliRunner()
        self.tmpdir = tempfile.mkdtemp(prefix="inv_recon_import_")
        self.db_path = os.path.join(self.tmpdir, "test2.db")
        os.environ["INV_RECON_DB"] = self.db_path
        self._invoke("init")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        if "INV_RECON_DB" in os.environ:
            del os.environ["INV_RECON_DB"]

    def _invoke(self, *args, input=None, catch_exceptions=False):
        return self.runner.invoke(cli, args, input=input, catch_exceptions=catch_exceptions)

    def test_bad_rows_reported_with_line_numbers_and_legacy_batch_intact(self):
        inv_good = os.path.join(self.tmpdir, "inv_good.csv")
        pay_good = os.path.join(self.tmpdir, "pay_good.csv")
        _write_csv(
            Path(inv_good),
            ["invoice_no", "vendor", "amount", "date"],
            [["INV-LEG1", "VendorZ", "300.00", "2024-02-01"]],
        )
        _write_csv(
            Path(pay_good),
            ["payment_no", "vendor", "amount", "date"],
            [["INV-LEG1", "VendorZ", "300.50", "2024-02-02"]],
        )
        r = self._invoke("import", "--invoices", inv_good, "--payments", pay_good, "--name", "legacy_batch")
        self.assertEqual(r.exit_code, 0, f"import legacy failed: {r.output}")
        r = self._invoke("match", "--batch", "1")
        self.assertEqual(r.exit_code, 0, f"match legacy failed: {r.output}")
        self._invoke("review", "--batch", "1", "--match-id", "1", "--action", "confirm", "--note", "差额核实过")
        exp_out = os.path.join(self.tmpdir, "legacy.csv")
        r = self._invoke("export", "--batch", "1", "--output", exp_out)
        self.assertEqual(r.exit_code, 0, f"export legacy failed: {r.output}")

        legacy_before = db.get_batch(1, db_path=self.db_path)
        self.assertEqual(legacy_before.status, BatchStatus.EXPORTED)

        inv_bad = os.path.join(self.tmpdir, "inv_bad.csv")
        pay_ok = os.path.join(self.tmpdir, "pay_ok.csv")
        _write_csv(
            Path(inv_bad),
            ["invoice_no", "vendor", "amount", "date"],
            [
                ["INV-NEW1", "VendorA", "500.00", "2024-03-01"],
                ["", "VendorA", "100.00", "2024-03-02"],
                ["INV-NEW1", "VendorB", "999.00", "2024-03-03"],
                ["INV-NEW2", "VendorC", "not_a_number", "2024-03-04"],
            ],
        )
        _write_csv(
            Path(pay_ok),
            ["payment_no", "vendor", "amount", "date"],
            [["PAY-NEW1", "VendorA", "500.00", "2024-03-05"]],
        )
        r = self._invoke("import", "--invoices", inv_bad, "--payments", pay_ok, "--name", "batch_with_bad_rows")
        self.assertEqual(r.exit_code, 0, f"import bad rows failed: {r.output}")

        self.assertIn("第 3 行", r.output)
        self.assertIn("第 4 行", r.output)
        self.assertIn("第 5 行", r.output)
        self.assertIn("合法数据已正常入库", r.output)
        self.assertIn("旧批次状态未受影响", r.output)

        new_batch = db.get_batch(2, db_path=self.db_path)
        self.assertIsNotNone(new_batch)
        invs = db.get_invoices_by_batch(2, db_path=self.db_path)
        self.assertEqual(len(invs), 1, f"should only keep 1 valid invoice, got {len(invs)}")
        self.assertEqual(invs[0].invoice_no, "INV-NEW1")

        legacy_after = db.get_batch(1, db_path=self.db_path)
        self.assertEqual(legacy_after.status, BatchStatus.EXPORTED, "旧批次状态不应被新批次导入带坏")


if __name__ == "__main__":
    unittest.main()
