# -*- coding: utf-8 -*-
"""
导入预检回归测试 —— 覆盖：
1. import dry-run 正常输出（含文件系统预检段、可复制命令）
2. unpack dry-run 正常输出（含文件系统预检段、冲突检测段、可复制命令）
3. 不可写目录预检报错
4. 不可写数据库预检报错
5. 改配置后预检结果变化
6. 跨重启再次预检结果一致
7. 同名冲突提示
8. 预检后继续导入、导出、撤销都能走通
9. 预检输出与真实写入的标签区分（"仅预览，未写入" vs "已写入"）
10. PlanResult 新字段完整性
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
from invoice_recon import db, rules, plan, snapshot, pack
from invoice_recon.models import MatchStatus, BatchStatus


SAMPLES_DIR = Path(__file__).parent.parent / "samples"


def _write_csv(path: Path, headers: list, rows: list):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(headers)
        w.writerows(rows)


class ImportDryRunOutputTest(unittest.TestCase):
    """import --dry-run 输出完整性测试。"""

    def setUp(self):
        self.runner = CliRunner()
        self.tmpdir = tempfile.mkdtemp(prefix="inv_recon_plan_import_")
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

    def test_dry_run_shows_fs_check_section(self):
        """dry-run 输出包含文件系统预检段。"""
        inv = str(SAMPLES_DIR / "invoices.csv")
        pay = str(SAMPLES_DIR / "payments.csv")

        r = self._invoke("import", "--invoices", inv, "--payments", pay, "--dry-run")
        self.assertEqual(r.exit_code, 0, f"dry-run failed: {r.output}")
        self.assertIn(rules.PLAN_SECTION_FS_CHECK, r.output)
        self.assertIn(rules.PLAN_FS_CHECK_OK, r.output)
        self.assertIn(rules.PLAN_FS_DB_PATH, r.output)

    def test_dry_run_shows_preview_label(self):
        """dry-run 输出包含"仅预览，未写入"标签。"""
        inv = str(SAMPLES_DIR / "invoices.csv")
        pay = str(SAMPLES_DIR / "payments.csv")

        r = self._invoke("import", "--invoices", inv, "--payments", pay, "--dry-run")
        self.assertEqual(r.exit_code, 0, f"dry-run failed: {r.output}")
        self.assertIn(rules.PLAN_MODE_LABEL, r.output)
        self.assertNotIn(rules.PLAN_REAL_MODE_LABEL, r.output)

    def test_dry_run_shows_copyable_command(self):
        """dry-run 输出包含可复制命令提示。"""
        inv = str(SAMPLES_DIR / "invoices.csv")
        pay = str(SAMPLES_DIR / "payments.csv")

        r = self._invoke("import", "--invoices", inv, "--payments", pay, "--dry-run")
        self.assertEqual(r.exit_code, 0, f"dry-run failed: {r.output}")
        self.assertIn(rules.PLAN_PREVIEW_COMMAND_INTRO, r.output)
        self.assertIn("inv-recon import", r.output)

    def test_dry_run_shows_files_to_create(self):
        """dry-run 输出包含将新建的文件和目录清单。"""
        inv = str(SAMPLES_DIR / "invoices.csv")
        pay = str(SAMPLES_DIR / "payments.csv")

        r = self._invoke("import", "--invoices", inv, "--payments", pay, "--dry-run")
        self.assertEqual(r.exit_code, 0, f"dry-run failed: {r.output}")
        self.assertIn(rules.PLAN_FS_FILES_TO_CREATE, r.output)

    def test_dry_run_same_name_conflict_hint(self):
        """同名批次时 dry-run 提示自动重命名。"""
        inv = str(SAMPLES_DIR / "invoices.csv")
        pay = str(SAMPLES_DIR / "payments.csv")

        self._invoke("import", "--invoices", inv, "--payments", pay, "--name", "dup_test")

        r = self._invoke("import", "--invoices", inv, "--payments", pay, "--name", "dup_test", "--dry-run")
        self.assertEqual(r.exit_code, 0, f"dry-run failed: {r.output}")
        self.assertIn("自动重命名", r.output)
        self.assertIn("dup_test_2", r.output)


class UnpackDryRunOutputTest(unittest.TestCase):
    """unpack --dry-run 输出完整性测试。"""

    def setUp(self):
        self.runner = CliRunner()
        self.tmpdir = tempfile.mkdtemp(prefix="inv_recon_plan_unpack_")
        self.src_db = os.path.join(self.tmpdir, "src.db")
        self.dst_db = os.path.join(self.tmpdir, "dst.db")
        self.snap_dir = os.path.join(self.tmpdir, "snapshots")
        os.environ["INV_RECON_SNAPSHOT_DIR"] = self.snap_dir
        self._setup_package()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        for env_var in ("INV_RECON_DB", "INV_RECON_SNAPSHOT_DIR"):
            if env_var in os.environ:
                del os.environ[env_var]

    def _invoke_src(self, *args, **kwargs):
        os.environ["INV_RECON_DB"] = self.src_db
        return self.runner.invoke(cli, args, **kwargs)

    def _invoke_dst(self, *args, **kwargs):
        os.environ["INV_RECON_DB"] = self.dst_db
        return self.runner.invoke(cli, args, **kwargs)

    def _setup_package(self):
        os.environ["INV_RECON_DB"] = self.src_db
        self._invoke_src("init")

        inv_csv = os.path.join(self.tmpdir, "inv.csv")
        pay_csv = os.path.join(self.tmpdir, "pay.csv")
        _write_csv(
            Path(inv_csv),
            ["invoice_no", "vendor", "amount", "date"],
            [["INV-P1", "VendorX", "1500.00", "2024-01-10"]],
        )
        _write_csv(
            Path(pay_csv),
            ["payment_no", "vendor", "amount", "date"],
            [["PAY-P1", "VendorX", "1500.00", "2024-01-15"]],
        )
        self._invoke_src("import", "--invoices", inv_csv, "--payments", pay_csv, "--name", "plan_test")
        self._invoke_src("match", "--batch", "1")
        self.pkg = os.path.join(self.tmpdir, "plan_test.invpkg")
        self._invoke_src("pack", "--batch", "1", "--output", self.pkg)

    def test_unpack_dry_run_shows_fs_check(self):
        """unpack dry-run 输出包含文件系统预检段。"""
        self._invoke_dst("init")
        r = self._invoke_dst("unpack", "--input", self.pkg, "--dry-run")
        self.assertEqual(r.exit_code, 0, f"unpack dry-run failed: {r.output}")
        self.assertIn(rules.PLAN_SECTION_FS_CHECK, r.output)
        self.assertIn(rules.PLAN_FS_CHECK_OK, r.output)
        self.assertIn(rules.PLAN_FS_DB_PATH, r.output)
        self.assertIn(rules.PLAN_FS_SNAPSHOT_DIR, r.output)
        self.assertIn(rules.PLAN_FS_TMP_DIR, r.output)

    def test_unpack_dry_run_shows_conflict_section(self):
        """unpack dry-run 输出包含冲突检测段。"""
        self._invoke_dst("init")
        r = self._invoke_dst("unpack", "--input", self.pkg, "--dry-run")
        self.assertEqual(r.exit_code, 0, f"unpack dry-run failed: {r.output}")
        self.assertIn(rules.PLAN_SECTION_CONFLICTS, r.output)
        self.assertIn(rules.PLAN_CONFLICT_NONE, r.output)

    def test_unpack_dry_run_shows_files_to_create(self):
        """unpack dry-run 输出包含将新建的文件清单。"""
        self._invoke_dst("init")
        r = self._invoke_dst("unpack", "--input", self.pkg, "--dry-run")
        self.assertEqual(r.exit_code, 0, f"unpack dry-run failed: {r.output}")
        self.assertIn(rules.PLAN_FS_FILES_TO_CREATE, r.output)
        self.assertIn(".snap.json", r.output)

    def test_unpack_dry_run_shows_dirs_to_create(self):
        """快照目录不存在时，unpack dry-run 提示将新建目录。"""
        self._invoke_dst("init")
        custom_snap = os.path.join(self.tmpdir, "new_snap_dir")
        os.environ["INV_RECON_SNAPSHOT_DIR"] = custom_snap
        try:
            r = self._invoke_dst("unpack", "--input", self.pkg, "--dry-run")
            self.assertEqual(r.exit_code, 0, f"unpack dry-run failed: {r.output}")
            self.assertIn(rules.PLAN_FS_DIRS_TO_CREATE, r.output)
        finally:
            os.environ["INV_RECON_SNAPSHOT_DIR"] = self.snap_dir

    def test_unpack_dry_run_shows_copyable_command(self):
        """unpack dry-run 输出包含可复制命令。"""
        self._invoke_dst("init")
        r = self._invoke_dst("unpack", "--input", self.pkg, "--dry-run")
        self.assertEqual(r.exit_code, 0, f"unpack dry-run failed: {r.output}")
        self.assertIn(rules.PLAN_PREVIEW_COMMAND_INTRO, r.output)
        self.assertIn("inv-recon unpack", r.output)


class UnwritableDirPreCheckTest(unittest.TestCase):
    """不可写目录预检报错测试。"""

    def setUp(self):
        self.runner = CliRunner()
        self.tmpdir = tempfile.mkdtemp(prefix="inv_recon_unwritable_")
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
    def test_import_unwritable_db_dir_fails_precheck(self):
        """数据库所在目录不可写时预检失败。"""
        readonly_dir = os.path.join(self.tmpdir, "readonly")
        os.makedirs(readonly_dir, exist_ok=True)
        readonly_db = os.path.join(readonly_dir, "test.db")
        os.environ["INV_RECON_DB"] = readonly_db

        os.chmod(readonly_dir, stat.S_IREAD | stat.S_IEXEC)
        self._readonly_dirs = [readonly_dir]

        inv = str(SAMPLES_DIR / "invoices.csv")
        pay = str(SAMPLES_DIR / "payments.csv")

        r = self._invoke("import", "--invoices", inv, "--payments", pay, "--dry-run")
        self.assertNotEqual(r.exit_code, 0, f"应该预检失败: {r.output}")
        self.assertIn(rules.PLAN_FS_CHECK_FAILED, r.output)

    @unittest.skipIf(os.name == "nt", "Windows 不可写目录检测需要特殊权限设置")
    def test_unpack_unwritable_snapshot_dir_fails_precheck(self):
        """快照目录不可写时 unpack 预检失败。"""
        inv = str(SAMPLES_DIR / "invoices.csv")
        pay = str(SAMPLES_DIR / "payments.csv")

        self._invoke("import", "--invoices", inv, "--payments", pay, "--name", "readonly_test")
        self._invoke("match", "--batch", "1")

        pkg = os.path.join(self.tmpdir, "readonly.invpkg")
        self._invoke("pack", "--batch", "1", "--output", pkg)

        readonly_snap = os.path.join(self.tmpdir, "readonly_snap")
        os.makedirs(readonly_snap, exist_ok=True)
        os.chmod(readonly_snap, stat.S_IREAD | stat.S_IEXEC)
        self._readonly_dirs = [readonly_snap]
        os.environ["INV_RECON_SNAPSHOT_DIR"] = readonly_snap

        r = self._invoke("unpack", "--input", pkg, "--dry-run")
        self.assertNotEqual(r.exit_code, 0, f"应该预检失败: {r.output}")
        self.assertIn(rules.PLAN_FS_CHECK_FAILED, r.output)


class ConfigChangePreCheckTest(unittest.TestCase):
    """改配置后预检结果变化测试。"""

    def setUp(self):
        self.runner = CliRunner()
        self.tmpdir = tempfile.mkdtemp(prefix="inv_recon_config_change_")
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

    def test_rule_version_changes_after_config(self):
        """修改配置后预检中规则版本发生变化。"""
        inv = str(SAMPLES_DIR / "invoices.csv")
        pay = str(SAMPLES_DIR / "payments.csv")

        r1 = self._invoke("import", "--invoices", inv, "--payments", pay, "--dry-run")
        self.assertEqual(r1.exit_code, 0, f"dry-run 1 failed: {r1.output}")
        self.assertIn("v1", r1.output)

        self._invoke("config", "--tolerance", "5.00")

        r2 = self._invoke("import", "--invoices", inv, "--payments", pay, "--dry-run")
        self.assertEqual(r2.exit_code, 0, f"dry-run 2 failed: {r2.output}")
        self.assertIn("v2", r2.output)

    def test_batch_name_changes_with_rule_version(self):
        """修改配置后默认批次名跟随规则版本变化。"""
        inv = str(SAMPLES_DIR / "invoices.csv")
        pay = str(SAMPLES_DIR / "payments.csv")

        r1 = self._invoke("import", "--invoices", inv, "--payments", pay, "--dry-run")
        self.assertEqual(r1.exit_code, 0)
        self.assertIn("batch_v1", r1.output)

        self._invoke("config", "--tolerance", "5.00")

        r2 = self._invoke("import", "--invoices", inv, "--payments", pay, "--dry-run")
        self.assertEqual(r2.exit_code, 0)
        self.assertIn("batch_v2", r2.output)


class CrossRestartPreCheckTest(unittest.TestCase):
    """跨重启再次预检结果一致性测试。"""

    def setUp(self):
        self.runner = CliRunner()
        self.tmpdir = tempfile.mkdtemp(prefix="inv_recon_cross_precheck_")
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

    def test_precheck_consistent_across_restarts(self):
        """跨重启后同一组数据的预检结果应一致。"""
        inv = str(SAMPLES_DIR / "invoices.csv")
        pay = str(SAMPLES_DIR / "payments.csv")

        r1 = self._invoke("import", "--invoices", inv, "--payments", pay, "--dry-run")
        self.assertEqual(r1.exit_code, 0)

        self._invoke("import", "--invoices", inv, "--payments", pay, "--name", "first")
        self._invoke("match", "--batch", "1")

        del os.environ["INV_RECON_DB"]
        os.environ["INV_RECON_DB"] = self.db_path
        del os.environ["INV_RECON_SNAPSHOT_DIR"]
        os.environ["INV_RECON_SNAPSHOT_DIR"] = self.snap_dir

        r2 = self._invoke("import", "--invoices", inv, "--payments", pay, "--name", "first", "--dry-run")
        self.assertEqual(r2.exit_code, 0)
        self.assertIn("自动重命名", r2.output)
        self.assertIn("first_2", r2.output)

    def test_unpack_precheck_consistent_across_restarts(self):
        """跨重启后同一包的 unpack 预检结果应一致。"""
        inv = str(SAMPLES_DIR / "invoices.csv")
        pay = str(SAMPLES_DIR / "payments.csv")

        self._invoke("import", "--invoices", inv, "--payments", pay, "--name", "cross_test")
        self._invoke("match", "--batch", "1")
        pkg = os.path.join(self.tmpdir, "cross.invpkg")
        self._invoke("pack", "--batch", "1", "--output", pkg)

        r1 = self._invoke("unpack", "--input", pkg, "--dry-run")
        self.assertEqual(r1.exit_code, 0)

        del os.environ["INV_RECON_DB"]
        os.environ["INV_RECON_DB"] = self.db_path
        del os.environ["INV_RECON_SNAPSHOT_DIR"]
        os.environ["INV_RECON_SNAPSHOT_DIR"] = self.snap_dir

        r2 = self._invoke("unpack", "--input", pkg, "--dry-run")
        self.assertEqual(r2.exit_code, 0)

        for keyword in [rules.PLAN_SECTION_FS_CHECK, rules.PLAN_FS_CHECK_OK,
                        rules.PLAN_SECTION_CONFLICTS]:
            self.assertIn(keyword, r2.output, f"跨重启预检缺关键词: {keyword}")


class SameNameConflictPreCheckTest(unittest.TestCase):
    """同名冲突提示测试。"""

    def setUp(self):
        self.runner = CliRunner()
        self.tmpdir = tempfile.mkdtemp(prefix="inv_recon_name_conflict_")
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

    def test_import_same_name_shows_rename_hint(self):
        """同名批次导入预检显示重命名提示。"""
        inv = str(SAMPLES_DIR / "invoices.csv")
        pay = str(SAMPLES_DIR / "payments.csv")

        self._invoke("import", "--invoices", inv, "--payments", pay, "--name", "conflict_test")

        r = self._invoke("import", "--invoices", inv, "--payments", pay, "--name", "conflict_test", "--dry-run")
        self.assertEqual(r.exit_code, 0)
        self.assertIn("同名批次已存在", r.output)
        self.assertIn("conflict_test_2", r.output)

    def test_import_same_name_three_times(self):
        """三次同名批次预检依次递增后缀。"""
        inv = str(SAMPLES_DIR / "invoices.csv")
        pay = str(SAMPLES_DIR / "payments.csv")

        self._invoke("import", "--invoices", inv, "--payments", pay, "--name", "tri_test")
        self._invoke("import", "--invoices", inv, "--payments", pay, "--name", "tri_test")

        r = self._invoke("import", "--invoices", inv, "--payments", pay, "--name", "tri_test", "--dry-run")
        self.assertEqual(r.exit_code, 0)
        self.assertIn("tri_test_3", r.output)

    def test_unpack_same_name_shows_rename_hint(self):
        """同名批次解包预检显示重命名提示。"""
        inv = str(SAMPLES_DIR / "invoices.csv")
        pay = str(SAMPLES_DIR / "payments.csv")

        self._invoke("import", "--invoices", inv, "--payments", pay, "--name", "dup_unpack")
        self._invoke("match", "--batch", "1")
        pkg = os.path.join(self.tmpdir, "dup_unpack.invpkg")
        self._invoke("pack", "--batch", "1", "--output", pkg)

        inv2_csv = os.path.join(self.tmpdir, "inv2.csv")
        pay2_csv = os.path.join(self.tmpdir, "pay2.csv")
        _write_csv(
            Path(inv2_csv),
            ["invoice_no", "vendor", "amount", "date"],
            [["INV-EX2", "VendorA", "100.00", "2024-02-01"]],
        )
        _write_csv(
            Path(pay2_csv),
            ["payment_no", "vendor", "amount", "date"],
            [["PAY-EX2", "VendorA", "100.00", "2024-02-02"]],
        )
        self._invoke("import", "--invoices", inv2_csv, "--payments", pay2_csv, "--name", "dup_unpack")

        r = self._invoke("unpack", "--input", pkg, "--dry-run")
        self.assertEqual(r.exit_code, 0, f"unpack dry-run failed: {r.output}")
        self.assertIn("自动重命名", r.output)


class PreCheckThenFullFlowTest(unittest.TestCase):
    """预检后继续导入、匹配、复核、导出、撤销全流程测试。"""

    def setUp(self):
        self.runner = CliRunner()
        self.tmpdir = tempfile.mkdtemp(prefix="inv_recon_precheck_flow_")
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

    def test_precheck_then_import_match_review_export(self):
        """预检 → 真实导入 → 匹配 → 复核 → 导出 全流程走通。"""
        inv = str(SAMPLES_DIR / "invoices.csv")
        pay = str(SAMPLES_DIR / "payments.csv")

        r = self._invoke("import", "--invoices", inv, "--payments", pay, "--dry-run")
        self.assertEqual(r.exit_code, 0)
        self.assertIn(rules.PLAN_MODE_LABEL, r.output)
        self.assertNotIn(rules.PLAN_REAL_MODE_LABEL, r.output)

        r = self._invoke("import", "--invoices", inv, "--payments", pay, "--name", "flow_test")
        self.assertEqual(r.exit_code, 0, f"import failed: {r.output}")
        self.assertIn(rules.PLAN_REAL_IMPORT_PASSED, r.output)
        self.assertIn(rules.PLAN_REAL_MODE_LABEL, r.output)

        r = self._invoke("match", "--batch", "1")
        self.assertEqual(r.exit_code, 0, f"match failed: {r.output}")

        matches = db.get_matches_by_batch(1, db_path=self.db_path)
        pending = [m for m in matches if m["status"] in (MatchStatus.PENDING, MatchStatus.CONFLICT)]
        if pending:
            self._invoke(
                "review", "--batch", "1",
                "--match-id", str(pending[0]["id"]),
                "--action", "confirm", "--note", "预检后复核",
            )

        out = os.path.join(self.tmpdir, "precheck_flow_export.csv")
        r = self._invoke("export", "--batch", "1", "--output", out)
        if r.exit_code == 0:
            self.assertTrue(os.path.exists(out))
        else:
            self.assertIn("无待处理记录", r.output)

    def test_precheck_then_import_then_undo(self):
        """预检 → 真实导入 → 匹配 → 复核 → 撤销复核 全流程走通。"""
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

        r = self._invoke("import", "--invoices", inv_csv, "--payments", pay_csv, "--dry-run", "--name", "undo_flow")
        self.assertEqual(r.exit_code, 0)

        r = self._invoke("import", "--invoices", inv_csv, "--payments", pay_csv, "--name", "undo_flow")
        self.assertEqual(r.exit_code, 0, f"import failed: {r.output}")

        self._invoke("match", "--batch", "1")

        matches = db.get_matches_by_batch(1, db_path=self.db_path)
        conflicts = [m for m in matches if m["status"] == MatchStatus.CONFLICT]
        self.assertGreater(len(conflicts), 0)

        self._invoke(
            "review", "--batch", "1",
            "--match-id", str(conflicts[0]["id"]),
            "--action", "confirm", "--note", "先确认",
        )

        r = self._invoke("review-undo", "--batch", "1", "--match-id", str(conflicts[0]["id"]))
        self.assertEqual(r.exit_code, 0, f"review-undo failed: {r.output}")

    def test_precheck_then_unpack_then_export(self):
        """预检 → 真实解包 → 导出 全流程走通。"""
        inv = str(SAMPLES_DIR / "invoices.csv")
        pay = str(SAMPLES_DIR / "payments.csv")

        self._invoke("import", "--invoices", inv, "--payments", pay, "--name", "unpack_flow")
        self._invoke("match", "--batch", "1")

        pkg = os.path.join(self.tmpdir, "unpack_flow.invpkg")
        self._invoke("pack", "--batch", "1", "--output", pkg)

        other_db = os.path.join(self.tmpdir, "other.db")
        os.environ["INV_RECON_DB"] = other_db
        try:
            self._invoke("init")

            r = self._invoke("unpack", "--input", pkg, "--dry-run")
            self.assertEqual(r.exit_code, 0, f"unpack dry-run failed: {r.output}")
            self.assertIn(rules.PLAN_MODE_LABEL, r.output)

            r = self._invoke("unpack", "--input", pkg)
            self.assertEqual(r.exit_code, 0, f"unpack failed: {r.output}")
            self.assertIn(rules.PLAN_REAL_IMPORT_PASSED, r.output)
            self.assertIn(rules.PLAN_REAL_MODE_LABEL, r.output)

            out = os.path.join(self.tmpdir, "unpack_flow_export.csv")
            r = self._invoke("export", "--batch", "1", "--output", out)
            if r.exit_code == 0:
                self.assertTrue(os.path.exists(out))
            else:
                self.assertIn("无待处理记录", r.output)
        finally:
            os.environ["INV_RECON_DB"] = self.db_path


class PlanResultFieldsTest(unittest.TestCase):
    """PlanResult 新字段完整性测试。"""

    def test_plan_import_populates_new_fields(self):
        """plan_import 返回的 PlanResult 包含新字段。"""
        db_path = os.path.join(tempfile.mkdtemp(prefix="inv_recon_plan_fields_"), "test.db")
        try:
            db.init_db(db_path)

            inv_csv = os.path.join(tempfile.gettempdir(), "plan_inv.csv")
            pay_csv = os.path.join(tempfile.gettempdir(), "plan_pay.csv")
            _write_csv(
                Path(inv_csv),
                ["invoice_no", "vendor", "amount", "date"],
                [["INV-F1", "VendorA", "100.00", "2024-01-01"]],
            )
            _write_csv(
                Path(pay_csv),
                ["payment_no", "vendor", "amount", "date"],
                [["PAY-F1", "VendorA", "100.00", "2024-01-02"]],
            )

            result = plan.plan_import(inv_csv, pay_csv, db_path=db_path)
            self.assertTrue(result.success)
            self.assertIsNotNone(result.db_path_resolved)
            self.assertIsInstance(result.files_to_create, list)
            self.assertIsInstance(result.dirs_to_create, list)
            self.assertIsInstance(result.writable_errors, list)
            self.assertTrue(result.writable_ok)
            self.assertEqual(len(result.writable_errors), 0)

            d = result.to_dict()
            self.assertIn("db_path_resolved", d)
            self.assertIn("snapshot_dir", d)
            self.assertIn("files_to_create", d)
            self.assertIn("dirs_to_create", d)
            self.assertIn("writable_ok", d)
            self.assertIn("writable_errors", d)
            self.assertIn("conflict_details", d)
        finally:
            import shutil
            shutil.rmtree(os.path.dirname(db_path), ignore_errors=True)
            for f in (inv_csv, pay_csv):
                if os.path.exists(f):
                    os.unlink(f)

    def test_plan_unpack_populates_new_fields(self):
        """plan_unpack 返回的 PlanResult 包含新字段。"""
        src_db = os.path.join(tempfile.mkdtemp(prefix="inv_recon_unpack_fields_"), "src.db")
        dst_db = os.path.join(os.path.dirname(src_db), "dst.db")
        try:
            db.init_db(src_db)

            inv_csv = os.path.join(tempfile.gettempdir(), "uf_inv.csv")
            pay_csv = os.path.join(tempfile.gettempdir(), "uf_pay.csv")
            _write_csv(
                Path(inv_csv),
                ["invoice_no", "vendor", "amount", "date"],
                [["INV-F2", "VendorB", "200.00", "2024-01-01"]],
            )
            _write_csv(
                Path(pay_csv),
                ["payment_no", "vendor", "amount", "date"],
                [["PAY-F2", "VendorB", "200.00", "2024-01-02"]],
            )

            from click.testing import CliRunner
            runner = CliRunner()

            os.environ["INV_RECON_DB"] = src_db
            os.environ["INV_RECON_SNAPSHOT_DIR"] = os.path.join(os.path.dirname(src_db), "snapshots")
            runner.invoke(cli, ["init"])
            runner.invoke(cli, ["import", "--invoices", inv_csv, "--payments", pay_csv, "--name", "field_test"])
            runner.invoke(cli, ["match", "--batch", "1"])
            pkg = os.path.join(os.path.dirname(src_db), "field_test.invpkg")
            runner.invoke(cli, ["pack", "--batch", "1", "--output", pkg])

            db.init_db(dst_db)
            result = plan.plan_unpack(pkg, db_path=dst_db)
            self.assertTrue(result.success)
            self.assertIsNotNone(result.db_path_resolved)
            self.assertIsNotNone(result.snapshot_dir)
            self.assertIsNotNone(result.unpack_tmp_dir)
            self.assertIsInstance(result.files_to_create, list)
            self.assertGreater(len(result.files_to_create), 0)
            self.assertIsInstance(result.conflict_details, list)
            self.assertTrue(result.writable_ok)
        finally:
            import shutil
            shutil.rmtree(os.path.dirname(src_db), ignore_errors=True)
            for f in (inv_csv, pay_csv):
                if os.path.exists(f):
                    os.unlink(f)
            for env_var in ("INV_RECON_DB", "INV_RECON_SNAPSHOT_DIR"):
                if env_var in os.environ:
                    del os.environ[env_var]


class PreviewVsRealLabelTest(unittest.TestCase):
    """预检输出与真实写入的标签区分测试。"""

    def setUp(self):
        self.runner = CliRunner()
        self.tmpdir = tempfile.mkdtemp(prefix="inv_recon_label_")
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

    def test_import_dry_run_has_preview_label_not_real_label(self):
        """import dry-run 只有"仅预览"标签，没有"已写入"标签。"""
        inv = str(SAMPLES_DIR / "invoices.csv")
        pay = str(SAMPLES_DIR / "payments.csv")

        r = self._invoke("import", "--invoices", inv, "--payments", pay, "--dry-run")
        self.assertEqual(r.exit_code, 0)
        self.assertIn(rules.PLAN_MODE_LABEL, r.output)
        self.assertNotIn(rules.PLAN_REAL_MODE_LABEL, r.output)
        self.assertNotIn(rules.PLAN_REAL_IMPORT_PASSED, r.output)

    def test_import_real_has_real_label_not_preview_label(self):
        """import 真实执行只有"已写入"标签，没有"仅预览"标签。"""
        inv = str(SAMPLES_DIR / "invoices.csv")
        pay = str(SAMPLES_DIR / "payments.csv")

        r = self._invoke("import", "--invoices", inv, "--payments", pay, "--name", "label_test")
        self.assertEqual(r.exit_code, 0, f"import failed: {r.output}")
        self.assertIn(rules.PLAN_REAL_MODE_LABEL, r.output)
        self.assertIn(rules.PLAN_REAL_IMPORT_PASSED, r.output)
        self.assertNotIn(rules.PLAN_MODE_LABEL, r.output)

    def test_unpack_dry_run_has_preview_label(self):
        """unpack dry-run 只有"仅预览"标签。"""
        inv = str(SAMPLES_DIR / "invoices.csv")
        pay = str(SAMPLES_DIR / "payments.csv")

        self._invoke("import", "--invoices", inv, "--payments", pay, "--name", "ulabel")
        self._invoke("match", "--batch", "1")
        pkg = os.path.join(self.tmpdir, "ulabel.invpkg")
        self._invoke("pack", "--batch", "1", "--output", pkg)

        r = self._invoke("unpack", "--input", pkg, "--dry-run")
        self.assertEqual(r.exit_code, 0)
        self.assertIn(rules.PLAN_MODE_LABEL, r.output)
        self.assertNotIn(rules.PLAN_REAL_MODE_LABEL, r.output)

    def test_unpack_real_has_real_label(self):
        """unpack 真实执行有"已写入"标签。"""
        inv = str(SAMPLES_DIR / "invoices.csv")
        pay = str(SAMPLES_DIR / "payments.csv")

        self._invoke("import", "--invoices", inv, "--payments", pay, "--name", "rlabel")
        self._invoke("match", "--batch", "1")
        pkg = os.path.join(self.tmpdir, "rlabel.invpkg")
        self._invoke("pack", "--batch", "1", "--output", pkg)

        other_db = os.path.join(self.tmpdir, "other.db")
        os.environ["INV_RECON_DB"] = other_db
        try:
            self._invoke("init")
            r = self._invoke("unpack", "--input", pkg)
            self.assertEqual(r.exit_code, 0, f"unpack failed: {r.output}")
            self.assertIn(rules.PLAN_REAL_MODE_LABEL, r.output)
            self.assertIn(rules.PLAN_REAL_IMPORT_PASSED, r.output)
        finally:
            os.environ["INV_RECON_DB"] = self.db_path


if __name__ == "__main__":
    unittest.main()
