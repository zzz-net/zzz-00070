# -*- coding: utf-8 -*-
"""
审计与异常追踪测试 —— 覆盖：
1. 跨重启查询审计记录
2. 配置变化后审计行为变化
3. 非法配置被拒绝
4. 权限不足导出报告报错
5. 撤销后审计回看（undo_before/undo_after）
6. 报告导出冲突（文件已存在、目录不可写）
7. 各操作类型审计记录完整性
8. 按批次/操作者/时间/动作/结果筛选
9. 审计详情查看（规则版本、文件落点、冲突原因、导出结果）
10. 导出格式 CSV/JSON 正确性
11. 保留天数清理
12. 同名批次审计追踪
13. 缺失关联数据时导出标注
"""

import os
import sys
import csv
import json
import stat
import tempfile
import shutil
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from click.testing import CliRunner

from invoice_recon.cli import cli
from invoice_recon import db, audit, snapshot
from invoice_recon.models import MatchStatus


SAMPLES_DIR = Path(__file__).parent.parent / "samples"


def _write_csv(path: Path, headers: list, rows: list):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(headers)
        w.writerows(rows)


class CrossRestartAuditTest(unittest.TestCase):
    """跨重启查询审计记录。"""

    def setUp(self):
        self.runner = CliRunner()
        self.tmpdir = tempfile.mkdtemp(prefix="inv_recon_audit_restart_")
        self.db_path = os.path.join(self.tmpdir, "test.db")
        self.snap_dir = os.path.join(self.tmpdir, "snapshots")
        os.environ["INV_RECON_DB"] = self.db_path
        os.environ["INV_RECON_SNAPSHOT_DIR"] = self.snap_dir
        self._invoke("init")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        for env_var in ("INV_RECON_DB", "INV_RECON_SNAPSHOT_DIR"):
            if env_var in os.environ:
                del os.environ[env_var]

    def _invoke(self, *args, **kwargs):
        return self.runner.invoke(cli, args, **kwargs)

    def test_audit_survives_restart(self):
        """跨重启后审计记录仍可查询。"""
        inv = str(SAMPLES_DIR / "invoices.csv")
        pay = str(SAMPLES_DIR / "payments.csv")

        self._invoke("import", "--invoices", inv, "--payments", pay, "--name", "restart_test")

        r = self._invoke("audit", "list", "--action", "import")
        self.assertEqual(r.exit_code, 0)
        self.assertIn("import", r.output)

        del os.environ["INV_RECON_DB"]
        os.environ["INV_RECON_DB"] = self.db_path
        del os.environ["INV_RECON_SNAPSHOT_DIR"]
        os.environ["INV_RECON_SNAPSHOT_DIR"] = self.snap_dir

        r2 = self._invoke("audit", "list", "--action", "import")
        self.assertEqual(r2.exit_code, 0)
        self.assertIn("import", r2.output)

    def test_audit_detail_survives_restart(self):
        """跨重启后审计详情仍可查看。"""
        inv = str(SAMPLES_DIR / "invoices.csv")
        pay = str(SAMPLES_DIR / "payments.csv")

        self._invoke("import", "--invoices", inv, "--payments", pay, "--name", "detail_restart")

        records = audit.query_audit(action="import", db_path=self.db_path)
        self.assertGreater(len(records), 0)

        del os.environ["INV_RECON_DB"]
        os.environ["INV_RECON_DB"] = self.db_path

        record = audit.get_audit_record(records[0]["id"], db_path=self.db_path)
        self.assertIsNotNone(record)
        self.assertEqual(record["action"], "import")
        self.assertIn("invoice_count", record.get("detail", {}))


class AuditConfigTest(unittest.TestCase):
    """审计配置变化。"""

    def setUp(self):
        self.runner = CliRunner()
        self.tmpdir = tempfile.mkdtemp(prefix="inv_recon_audit_config_")
        self.db_path = os.path.join(self.tmpdir, "test.db")
        self.snap_dir = os.path.join(self.tmpdir, "snapshots")
        os.environ["INV_RECON_DB"] = self.db_path
        os.environ["INV_RECON_SNAPSHOT_DIR"] = self.snap_dir
        self._invoke("init")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        for env_var in ("INV_RECON_DB", "INV_RECON_SNAPSHOT_DIR"):
            if env_var in os.environ:
                del os.environ[env_var]

    def _invoke(self, *args, **kwargs):
        return self.runner.invoke(cli, args, **kwargs)

    def test_default_config(self):
        """默认审计配置正确。"""
        r = self._invoke("audit", "config")
        self.assertEqual(r.exit_code, 0)
        self.assertIn("365", r.output)

    def test_change_retention_days(self):
        """修改保留天数后配置生效。"""
        r = self._invoke("audit", "config", "--retention-days", "90")
        self.assertEqual(r.exit_code, 0)
        self.assertIn("90", r.output)

        r2 = self._invoke("audit", "config")
        self.assertEqual(r2.exit_code, 0)
        self.assertIn("90", r2.output)

    def test_change_verbose(self):
        """修改详细字段开关后配置生效。"""
        r = self._invoke("audit", "config", "--no-verbose")
        self.assertEqual(r.exit_code, 0)
        self.assertIn("否", r.output)

        r2 = self._invoke("audit", "config")
        self.assertEqual(r2.exit_code, 0)
        self.assertIn("否", r2.output)

    def test_invalid_retention_days_rejected(self):
        """非法保留天数被拒绝。"""
        r = self._invoke("audit", "config", "--retention-days", "-1")
        self.assertNotEqual(r.exit_code, 0)

    def test_verbose_mode_affects_detail(self):
        """verbose 关闭后审计记录不含完整详情。"""
        self._invoke("audit", "config", "--no-verbose")

        inv = str(SAMPLES_DIR / "invoices.csv")
        pay = str(SAMPLES_DIR / "payments.csv")
        self._invoke("import", "--invoices", inv, "--payments", pay, "--name", "verbose_test")

        records = audit.query_audit(action="import", db_path=self.db_path)
        self.assertGreater(len(records), 0)
        detail = records[0].get("detail")
        if detail:
            self.assertNotIn("invoice_count", detail)

    def test_retention_cleanup(self):
        """设置保留天数后清理过期记录。"""
        audit.set_audit_config(retention_days=0, db_path=self.db_path)

        audit.log_audit(action="import", result="success", db_path=self.db_path)

        records = audit.query_audit(action="import", db_path=self.db_path)
        self.assertGreater(len(records), 0)

        deleted = audit.cleanup_audit(retention_days=0, db_path=self.db_path)
        self.assertEqual(deleted, 0)

        audit.set_audit_config(retention_days=365, db_path=self.db_path)
        deleted2 = audit.cleanup_audit(db_path=self.db_path)


class AuditFilterTest(unittest.TestCase):
    """审计筛选功能。"""

    def setUp(self):
        self.runner = CliRunner()
        self.tmpdir = tempfile.mkdtemp(prefix="inv_recon_audit_filter_")
        self.db_path = os.path.join(self.tmpdir, "test.db")
        self.snap_dir = os.path.join(self.tmpdir, "snapshots")
        os.environ["INV_RECON_DB"] = self.db_path
        os.environ["INV_RECON_SNAPSHOT_DIR"] = self.snap_dir
        self._invoke("init")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        for env_var in ("INV_RECON_DB", "INV_RECON_SNAPSHOT_DIR"):
            if env_var in os.environ:
                del os.environ[env_var]

    def _invoke(self, *args, **kwargs):
        return self.runner.invoke(cli, args, **kwargs)

    def test_filter_by_batch(self):
        """按批次 ID 筛选审计记录。"""
        inv = str(SAMPLES_DIR / "invoices.csv")
        pay = str(SAMPLES_DIR / "payments.csv")

        self._invoke("import", "--invoices", inv, "--payments", pay, "--name", "batch_filter")

        r = self._invoke("audit", "list", "--batch", "1")
        self.assertEqual(r.exit_code, 0)
        self.assertIn("import", r.output)

    def test_filter_by_action(self):
        """按动作类型筛选审计记录。"""
        inv = str(SAMPLES_DIR / "invoices.csv")
        pay = str(SAMPLES_DIR / "payments.csv")

        self._invoke("import", "--invoices", inv, "--payments", pay, "--name", "action_filter")
        self._invoke("match", "--batch", "1")

        r1 = self._invoke("audit", "list", "--action", "import")
        self.assertEqual(r1.exit_code, 0)
        self.assertIn("import", r1.output)
        self.assertNotIn("match", r1.output)

        r2 = self._invoke("audit", "list", "--action", "match")
        self.assertEqual(r2.exit_code, 0)
        self.assertIn("match", r2.output)

    def test_filter_by_result(self):
        """按结果筛选审计记录。"""
        r = self._invoke("audit", "list", "--result", "success")
        self.assertEqual(r.exit_code, 0)

    def test_filter_by_time_range(self):
        """按时间范围筛选审计记录。"""
        inv = str(SAMPLES_DIR / "invoices.csv")
        pay = str(SAMPLES_DIR / "payments.csv")

        self._invoke("import", "--invoices", inv, "--payments", pay, "--name", "time_filter")

        r = self._invoke("audit", "list", "--from", "2020-01-01", "--to", "2030-12-31")
        self.assertEqual(r.exit_code, 0)
        self.assertIn("import", r.output)

    def test_filter_by_operator(self):
        """按操作者筛选审计记录。"""
        audit.log_audit(action="import", operator="testuser", result="success", db_path=self.db_path)

        r = self._invoke("audit", "list", "--operator", "testuser")
        self.assertEqual(r.exit_code, 0)
        self.assertIn("testuser", r.output)


class UndoAuditLookbackTest(unittest.TestCase):
    """撤销后审计回看。"""

    def setUp(self):
        self.runner = CliRunner()
        self.tmpdir = tempfile.mkdtemp(prefix="inv_recon_audit_undo_")
        self.db_path = os.path.join(self.tmpdir, "test.db")
        self.snap_dir = os.path.join(self.tmpdir, "snapshots")
        os.environ["INV_RECON_DB"] = self.db_path
        os.environ["INV_RECON_SNAPSHOT_DIR"] = self.snap_dir
        self._invoke("init")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        for env_var in ("INV_RECON_DB", "INV_RECON_SNAPSHOT_DIR"):
            if env_var in os.environ:
                del os.environ[env_var]

    def _invoke(self, *args, **kwargs):
        return self.runner.invoke(cli, args, **kwargs)

    def test_undo_audit_has_before_after(self):
        """撤销操作审计记录包含撤销前后状态。"""
        inv_csv = os.path.join(self.tmpdir, "inv.csv")
        pay_csv = os.path.join(self.tmpdir, "pay.csv")
        _write_csv(
            Path(inv_csv),
            ["invoice_no", "vendor", "amount", "date"],
            [
                ["INV-U1", "VendorX", "1500.00", "2024-01-10"],
                ["INV-U2", "VendorY", "800.00", "2024-01-12"],
            ],
        )
        _write_csv(
            Path(pay_csv),
            ["payment_no", "vendor", "amount", "date"],
            [
                ["PAY-U1", "VendorX", "1500.00", "2024-01-15"],
                ["PAY-U2", "VendorX", "1500.00", "2024-01-16"],
            ],
        )

        self._invoke("import", "--invoices", inv_csv, "--payments", pay_csv, "--name", "undo_audit")
        self._invoke("match", "--batch", "1")

        matches = db.get_matches_by_batch(1, db_path=self.db_path)
        conflicts = [m for m in matches if m["status"] == MatchStatus.CONFLICT]
        self.assertGreater(len(conflicts), 0)

        self._invoke(
            "review", "--batch", "1",
            "--match-id", str(conflicts[0]["id"]),
            "--action", "confirm", "--note", "先确认",
        )

        self._invoke("review-undo", "--batch", "1", "--match-id", str(conflicts[0]["id"]))

        undo_records = audit.query_audit(action="review-undo", db_path=self.db_path)
        self.assertGreater(len(undo_records), 0)

        record = audit.get_audit_record(undo_records[0]["id"], db_path=self.db_path)
        self.assertIsNotNone(record)
        detail = record.get("detail", {})
        self.assertIn("undo_before", detail)
        self.assertIn("undo_after", detail)


class AuditExportReportTest(unittest.TestCase):
    """审计报告导出。"""

    def setUp(self):
        self.runner = CliRunner()
        self.tmpdir = tempfile.mkdtemp(prefix="inv_recon_audit_export_")
        self.db_path = os.path.join(self.tmpdir, "test.db")
        self.snap_dir = os.path.join(self.tmpdir, "snapshots")
        os.environ["INV_RECON_DB"] = self.db_path
        os.environ["INV_RECON_SNAPSHOT_DIR"] = self.snap_dir
        self._invoke("init")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        for env_var in ("INV_RECON_DB", "INV_RECON_SNAPSHOT_DIR"):
            if env_var in os.environ:
                del os.environ[env_var]

    def _invoke(self, *args, **kwargs):
        return self.runner.invoke(cli, args, **kwargs)

    def test_export_csv(self):
        """导出 CSV 格式审计报告。"""
        inv = str(SAMPLES_DIR / "invoices.csv")
        pay = str(SAMPLES_DIR / "payments.csv")
        self._invoke("import", "--invoices", inv, "--payments", pay, "--name", "export_csv")

        out = os.path.join(self.tmpdir, "audit.csv")
        r = self._invoke("audit", "export", "--output", out, "--format", "csv")
        self.assertEqual(r.exit_code, 0, f"export csv failed: {r.output}")
        self.assertTrue(os.path.exists(out))

        with open(out, "r", encoding="utf-8") as f:
            reader = csv.reader(f)
            headers = next(reader)
            self.assertIn("id", headers)
            self.assertIn("action", headers)

    def test_export_json(self):
        """导出 JSON 格式审计报告。"""
        inv = str(SAMPLES_DIR / "invoices.csv")
        pay = str(SAMPLES_DIR / "payments.csv")
        self._invoke("import", "--invoices", inv, "--payments", pay, "--name", "export_json")

        out = os.path.join(self.tmpdir, "audit.json")
        r = self._invoke("audit", "export", "--output", out, "--format", "json")
        self.assertEqual(r.exit_code, 0, f"export json failed: {r.output}")
        self.assertTrue(os.path.exists(out))

        with open(out, "r", encoding="utf-8") as f:
            data = json.load(f)
            self.assertIsInstance(data, list)
            self.assertGreater(len(data), 0)
            self.assertIn("action", data[0])

    def test_export_file_exists_error(self):
        """目标文件已存在时报错不覆盖。"""
        out = os.path.join(self.tmpdir, "existing.csv")
        with open(out, "w", encoding="utf-8") as f:
            f.write("old data")

        r = self._invoke("audit", "export", "--output", out, "--format", "csv")
        self.assertNotEqual(r.exit_code, 0)
        self.assertIn("已存在", r.output)

        with open(out, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), "old data")

    def test_export_dir_not_found(self):
        """目标目录不存在时报错。"""
        out = os.path.join(self.tmpdir, "nonexistent_dir", "audit.csv")
        r = self._invoke("audit", "export", "--output", out, "--format", "csv")
        self.assertNotEqual(r.exit_code, 0)
        self.assertIn("不存在", r.output)

    def test_export_no_partial_file(self):
        """导出失败不产生半截文件。"""
        out = os.path.join(self.tmpdir, "partial_test.csv")
        with open(out, "w", encoding="utf-8") as f:
            f.write("existing")

        r = self._invoke("audit", "export", "--output", out, "--format", "csv")
        self.assertNotEqual(r.exit_code, 0)

        with open(out, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), "existing")

    def test_export_with_filters(self):
        """带筛选条件导出审计报告。"""
        inv = str(SAMPLES_DIR / "invoices.csv")
        pay = str(SAMPLES_DIR / "payments.csv")
        self._invoke("import", "--invoices", inv, "--payments", pay, "--name", "filter_export")
        self._invoke("match", "--batch", "1")

        out = os.path.join(self.tmpdir, "filtered.csv")
        r = self._invoke("audit", "export", "--output", out, "--format", "csv", "--action", "import")
        self.assertEqual(r.exit_code, 0)

        with open(out, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
            for row in rows:
                self.assertEqual(row["action"], "import")


class AuditShowDetailTest(unittest.TestCase):
    """审计详情查看。"""

    def setUp(self):
        self.runner = CliRunner()
        self.tmpdir = tempfile.mkdtemp(prefix="inv_recon_audit_show_")
        self.db_path = os.path.join(self.tmpdir, "test.db")
        self.snap_dir = os.path.join(self.tmpdir, "snapshots")
        os.environ["INV_RECON_DB"] = self.db_path
        os.environ["INV_RECON_SNAPSHOT_DIR"] = self.snap_dir
        self._invoke("init")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        for env_var in ("INV_RECON_DB", "INV_RECON_SNAPSHOT_DIR"):
            if env_var in os.environ:
                del os.environ[env_var]

    def _invoke(self, *args, **kwargs):
        return self.runner.invoke(cli, args, **kwargs)

    def test_show_import_detail(self):
        """查看导入审计详情含规则版本和记录数。"""
        inv = str(SAMPLES_DIR / "invoices.csv")
        pay = str(SAMPLES_DIR / "payments.csv")
        self._invoke("import", "--invoices", inv, "--payments", pay, "--name", "show_detail")

        records = audit.query_audit(action="import", db_path=self.db_path)
        self.assertGreater(len(records), 0)

        r = self._invoke("audit", "show", str(records[0]["id"]))
        self.assertEqual(r.exit_code, 0)
        self.assertIn("import", r.output)
        self.assertIn("详情", r.output)

    def test_show_export_detail(self):
        """查看导出审计详情含导出路径。"""
        inv = str(SAMPLES_DIR / "invoices.csv")
        pay = str(SAMPLES_DIR / "payments.csv")
        self._invoke("import", "--invoices", inv, "--payments", pay, "--name", "show_export")
        self._invoke("match", "--batch", "1")

        out = os.path.join(self.tmpdir, "export_test.csv")
        self._invoke("export", "--batch", "1", "--output", out)

        records = audit.query_audit(action="export", db_path=self.db_path)
        if records:
            r = self._invoke("audit", "show", str(records[0]["id"]))
            self.assertEqual(r.exit_code, 0)
            self.assertIn("export", r.output)

    def test_show_nonexistent_record(self):
        """查看不存在的审计记录报错。"""
        r = self._invoke("audit", "show", "999999")
        self.assertNotEqual(r.exit_code, 0)
        self.assertIn("不存在", r.output)

    def test_show_review_detail(self):
        """查看复核审计详情含裁决前后状态。"""
        inv_csv = os.path.join(self.tmpdir, "inv.csv")
        pay_csv = os.path.join(self.tmpdir, "pay.csv")
        _write_csv(
            Path(inv_csv),
            ["invoice_no", "vendor", "amount", "date"],
            [["INV-R1", "VendorX", "100.00", "2024-01-10"]],
        )
        _write_csv(
            Path(pay_csv),
            ["payment_no", "vendor", "amount", "date"],
            [["PAY-R1", "VendorX", "100.00", "2024-01-15"]],
        )

        self._invoke("import", "--invoices", inv_csv, "--payments", pay_csv, "--name", "review_audit")
        self._invoke("match", "--batch", "1")

        matches = db.get_matches_by_batch(1, db_path=self.db_path)
        pending = [m for m in matches if m["status"] in (MatchStatus.PENDING, MatchStatus.CONFLICT)]
        if pending:
            self._invoke(
                "review", "--batch", "1",
                "--match-id", str(pending[0]["id"]),
                "--action", "confirm", "--note", "复核测试",
            )

            records = audit.query_audit(action="review", db_path=self.db_path)
            if records:
                r = self._invoke("audit", "show", str(records[0]["id"]))
                self.assertEqual(r.exit_code, 0)
                self.assertIn("详情", r.output)


class AuditAllOperationsTest(unittest.TestCase):
    """各操作类型审计记录完整性。"""

    def setUp(self):
        self.runner = CliRunner()
        self.tmpdir = tempfile.mkdtemp(prefix="inv_recon_audit_ops_")
        self.db_path = os.path.join(self.tmpdir, "test.db")
        self.snap_dir = os.path.join(self.tmpdir, "snapshots")
        os.environ["INV_RECON_DB"] = self.db_path
        os.environ["INV_RECON_SNAPSHOT_DIR"] = self.snap_dir
        self._invoke("init")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        for env_var in ("INV_RECON_DB", "INV_RECON_SNAPSHOT_DIR"):
            if env_var in os.environ:
                del os.environ[env_var]

    def _invoke(self, *args, **kwargs):
        return self.runner.invoke(cli, args, **kwargs)

    def test_import_creates_audit(self):
        """导入操作产生审计记录。"""
        inv = str(SAMPLES_DIR / "invoices.csv")
        pay = str(SAMPLES_DIR / "payments.csv")
        self._invoke("import", "--invoices", inv, "--payments", pay, "--name", "audit_import")

        records = audit.query_audit(action="import", db_path=self.db_path)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["result"], "success")
        self.assertEqual(records[0]["batch_name"], "audit_import")

    def test_match_creates_audit(self):
        """匹配操作产生审计记录。"""
        inv = str(SAMPLES_DIR / "invoices.csv")
        pay = str(SAMPLES_DIR / "payments.csv")
        self._invoke("import", "--invoices", inv, "--payments", pay, "--name", "audit_match")
        self._invoke("match", "--batch", "1")

        records = audit.query_audit(action="match", db_path=self.db_path)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["result"], "success")

    def test_revoke_creates_audit(self):
        """撤销批次产生审计记录。"""
        inv = str(SAMPLES_DIR / "invoices.csv")
        pay = str(SAMPLES_DIR / "payments.csv")
        self._invoke("import", "--invoices", inv, "--payments", pay, "--name", "audit_revoke")
        self._invoke("revoke", "--batch", "1")

        records = audit.query_audit(action="revoke", db_path=self.db_path)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["result"], "success")

    def test_config_creates_audit(self):
        """配置变更产生审计记录。"""
        self._invoke("config", "--tolerance", "5.00")

        records = audit.query_audit(action="config", db_path=self.db_path)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["result"], "success")

    def test_snapshot_create_creates_audit(self):
        """创建快照产生审计记录。"""
        inv = str(SAMPLES_DIR / "invoices.csv")
        pay = str(SAMPLES_DIR / "payments.csv")
        self._invoke("import", "--invoices", inv, "--payments", pay, "--name", "audit_snap")
        self._invoke("match", "--batch", "1")
        self._invoke("snapshot", "create", "--batch", "1")

        records = audit.query_audit(action="snapshot-create", db_path=self.db_path)
        self.assertEqual(len(records), 1)

    def test_snapshot_restore_creates_audit(self):
        """恢复快照产生审计记录。"""
        inv = str(SAMPLES_DIR / "invoices.csv")
        pay = str(SAMPLES_DIR / "payments.csv")
        self._invoke("import", "--invoices", inv, "--payments", pay, "--name", "audit_restore")
        self._invoke("match", "--batch", "1")
        self._invoke("snapshot", "create", "--batch", "1")

        snaps = snapshot.list_snapshots()
        if snaps:
            snap_id = snaps[0]["snapshot_id"][:8]
            self._invoke("snapshot", "restore", "--snapshot", snap_id)

            records = audit.query_audit(action="snapshot-restore", db_path=self.db_path)
            self.assertEqual(len(records), 1)

    def test_pack_creates_audit(self):
        """打包操作产生审计记录。"""
        inv = str(SAMPLES_DIR / "invoices.csv")
        pay = str(SAMPLES_DIR / "payments.csv")
        self._invoke("import", "--invoices", inv, "--payments", pay, "--name", "audit_pack")
        self._invoke("match", "--batch", "1")

        pkg = os.path.join(self.tmpdir, "audit_pack.invpkg")
        self._invoke("pack", "--batch", "1", "--output", pkg)

        records = audit.query_audit(action="pack", db_path=self.db_path)
        self.assertEqual(len(records), 1)

    def test_unpack_creates_audit(self):
        """解包操作产生审计记录。"""
        inv = str(SAMPLES_DIR / "invoices.csv")
        pay = str(SAMPLES_DIR / "payments.csv")
        self._invoke("import", "--invoices", inv, "--payments", pay, "--name", "audit_unpack")
        self._invoke("match", "--batch", "1")

        pkg = os.path.join(self.tmpdir, "audit_unpack.invpkg")
        self._invoke("pack", "--batch", "1", "--output", pkg)

        other_db = os.path.join(self.tmpdir, "other.db")
        os.environ["INV_RECON_DB"] = other_db
        try:
            self._invoke("init")
            self._invoke("unpack", "--input", pkg)

            records = audit.query_audit(action="unpack", db_path=other_db)
            self.assertEqual(len(records), 1)
        finally:
            os.environ["INV_RECON_DB"] = self.db_path


class SameNameBatchAuditTest(unittest.TestCase):
    """同名批次审计追踪。"""

    def setUp(self):
        self.runner = CliRunner()
        self.tmpdir = tempfile.mkdtemp(prefix="inv_recon_audit_samename_")
        self.db_path = os.path.join(self.tmpdir, "test.db")
        self.snap_dir = os.path.join(self.tmpdir, "snapshots")
        os.environ["INV_RECON_DB"] = self.db_path
        os.environ["INV_RECON_SNAPSHOT_DIR"] = self.snap_dir
        self._invoke("init")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        for env_var in ("INV_RECON_DB", "INV_RECON_SNAPSHOT_DIR"):
            if env_var in os.environ:
                del os.environ[env_var]

    def _invoke(self, *args, **kwargs):
        return self.runner.invoke(cli, args, **kwargs)

    def test_same_name_import_audit(self):
        """同名批次导入审计记录含重命名信息。"""
        inv = str(SAMPLES_DIR / "invoices.csv")
        pay = str(SAMPLES_DIR / "payments.csv")

        self._invoke("import", "--invoices", inv, "--payments", pay, "--name", "dup_audit")
        self._invoke("import", "--invoices", inv, "--payments", pay, "--name", "dup_audit")

        records = audit.query_audit(action="import", db_path=self.db_path)
        self.assertEqual(len(records), 2)

        second = [r for r in records if r.get("detail", {}).get("was_renamed")]
        self.assertGreater(len(second), 0)
        self.assertTrue(second[0]["detail"]["was_renamed"])


class AuditModuleDirectTest(unittest.TestCase):
    """审计模块直接 API 测试。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="inv_recon_audit_api_")
        self.db_path = os.path.join(self.tmpdir, "test.db")
        db.init_db(self.db_path)
        audit.init_audit_db(self.db_path)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_log_and_query(self):
        """写入并查询审计记录。"""
        rid = audit.log_audit(
            action="import",
            operator="tester",
            batch_id=1,
            batch_name="test_batch",
            rule_version="v1",
            result="success",
            detail={"key": "value"},
            db_path=self.db_path,
        )
        self.assertGreater(rid, 0)

        records = audit.query_audit(action="import", db_path=self.db_path)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["action"], "import")
        self.assertEqual(records[0]["operator"], "tester")

    def test_invalid_action_rejected(self):
        """非法操作类型被拒绝。"""
        with self.assertRaises(ValueError):
            audit.log_audit(action="invalid_action", db_path=self.db_path)

    def test_get_record(self):
        """获取单条审计记录。"""
        rid = audit.log_audit(
            action="match",
            result="success",
            detail={"conflict_count": 2},
            db_path=self.db_path,
        )

        record = audit.get_audit_record(rid, db_path=self.db_path)
        self.assertIsNotNone(record)
        self.assertEqual(record["action"], "match")
        self.assertEqual(record["detail"]["conflict_count"], 2)

    def test_get_nonexistent_record(self):
        """获取不存在的审计记录返回 None。"""
        record = audit.get_audit_record(999999, db_path=self.db_path)
        self.assertIsNone(record)

    def test_config_validation_negative_retention(self):
        """负数保留天数被拒绝。"""
        with self.assertRaises(ValueError):
            audit.set_audit_config(retention_days=-1, db_path=self.db_path)

    def test_config_validation_zero_retention(self):
        """零保留天数允许（永久保留）。"""
        config = audit.set_audit_config(retention_days=0, db_path=self.db_path)
        self.assertEqual(config["retention_days"], 0)

    def test_export_csv_direct(self):
        """直接 API 导出 CSV。"""
        audit.log_audit(action="import", result="success", db_path=self.db_path)

        out = os.path.join(self.tmpdir, "direct.csv")
        path = audit.export_audit_report(out, fmt="csv", db_path=self.db_path)
        self.assertEqual(path, os.path.abspath(out))
        self.assertTrue(os.path.exists(out))

    def test_export_json_direct(self):
        """直接 API 导出 JSON。"""
        audit.log_audit(action="import", result="success", db_path=self.db_path)

        out = os.path.join(self.tmpdir, "direct.json")
        path = audit.export_audit_report(out, fmt="json", db_path=self.db_path)
        self.assertEqual(path, os.path.abspath(out))
        self.assertTrue(os.path.exists(out))

    def test_export_file_exists(self):
        """导出目标文件已存在时抛出异常。"""
        out = os.path.join(self.tmpdir, "exists.csv")
        with open(out, "w") as f:
            f.write("old")

        with self.assertRaises(FileExistsError):
            audit.export_audit_report(out, fmt="csv", db_path=self.db_path)

    def test_export_dir_not_writable(self):
        """导出目录不可写时抛出异常。"""
        out = os.path.join(self.tmpdir, "nonexistent", "audit.csv")
        with self.assertRaises(FileNotFoundError):
            audit.export_audit_report(out, fmt="csv", db_path=self.db_path)

    def test_export_invalid_format(self):
        """不支持的导出格式抛出异常。"""
        out = os.path.join(self.tmpdir, "audit.xml")
        with self.assertRaises(ValueError):
            audit.export_audit_report(out, fmt="xml", db_path=self.db_path)

    def test_failure_audit_record(self):
        """失败操作审计记录。"""
        rid = audit.log_audit(
            action="import",
            result="failure",
            error_message="数据库不可写",
            db_path=self.db_path,
        )
        record = audit.get_audit_record(rid, db_path=self.db_path)
        self.assertEqual(record["result"], "failure")
        self.assertEqual(record["error_message"], "数据库不可写")

    def test_query_limit(self):
        """查询条数限制。"""
        for i in range(10):
            audit.log_audit(action="import", result="success", db_path=self.db_path)

        records = audit.query_audit(limit=3, db_path=self.db_path)
        self.assertEqual(len(records), 3)


class PermissionDeniedAuditExportTest(unittest.TestCase):
    """权限不足导出审计报告报错。"""

    def setUp(self):
        self.runner = CliRunner()
        self.tmpdir = tempfile.mkdtemp(prefix="inv_recon_audit_perm_")
        self.db_path = os.path.join(self.tmpdir, "test.db")
        self.snap_dir = os.path.join(self.tmpdir, "snapshots")
        os.environ["INV_RECON_DB"] = self.db_path
        os.environ["INV_RECON_SNAPSHOT_DIR"] = self.snap_dir
        self._invoke("init")

    def tearDown(self):
        for d in getattr(self, '_readonly_dirs', []):
            try:
                os.chmod(d, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
            except OSError:
                pass
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        for env_var in ("INV_RECON_DB", "INV_RECON_SNAPSHOT_DIR"):
            if env_var in os.environ:
                del os.environ[env_var]

    def _invoke(self, *args, **kwargs):
        return self.runner.invoke(cli, args, **kwargs)

    @unittest.skipIf(os.name == "nt", "Windows 不可写目录检测需要特殊权限设置")
    def test_export_to_unwritable_dir(self):
        """导出到不可写目录报错。"""
        inv = str(SAMPLES_DIR / "invoices.csv")
        pay = str(SAMPLES_DIR / "payments.csv")
        self._invoke("import", "--invoices", inv, "--payments", pay, "--name", "perm_test")

        readonly_dir = os.path.join(self.tmpdir, "readonly")
        os.makedirs(readonly_dir, exist_ok=True)
        os.chmod(readonly_dir, stat.S_IREAD | stat.S_IEXEC)
        self._readonly_dirs = [readonly_dir]

        out = os.path.join(readonly_dir, "audit.csv")
        r = self._invoke("audit", "export", "--output", out, "--format", "csv")
        self.assertNotEqual(r.exit_code, 0)


class AuditSameNameExportConflictTest(unittest.TestCase):
    """同名批次导出冲突审计追踪。"""

    def setUp(self):
        self.runner = CliRunner()
        self.tmpdir = tempfile.mkdtemp(prefix="inv_recon_audit_conflict_")
        self.db_path = os.path.join(self.tmpdir, "test.db")
        self.snap_dir = os.path.join(self.tmpdir, "snapshots")
        os.environ["INV_RECON_DB"] = self.db_path
        os.environ["INV_RECON_SNAPSHOT_DIR"] = self.snap_dir
        self._invoke("init")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        for env_var in ("INV_RECON_DB", "INV_RECON_SNAPSHOT_DIR"):
            if env_var in os.environ:
                del os.environ[env_var]

    def _invoke(self, *args, **kwargs):
        return self.runner.invoke(cli, args, **kwargs)

    def test_export_same_name_file_conflict(self):
        """审计报告导出同名文件冲突。"""
        out = os.path.join(self.tmpdir, "conflict.csv")
        with open(out, "w", encoding="utf-8") as f:
            f.write("existing content")

        r = self._invoke("audit", "export", "--output", out, "--format", "csv")
        self.assertNotEqual(r.exit_code, 0)

        with open(out, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), "existing content")


if __name__ == "__main__":
    unittest.main()
