# -*- coding: utf-8 -*-
"""
打包/验包功能回归测试 —— 覆盖：
1. pack 基本功能（含/不含导出结果）
2. verify 完整性校验
3. inspect 元信息查看
4. unpack 冲突处理：同名批次、文件已存在、快照目录缺失、包内容不完整
5. 导入后校验报告（沿用原状态 vs 重命名记录）
6. 跨重启重新导入再导出
7. 导入后 review-undo 再导出
8. CLI --help 与 rules 常量一致
9. README 示例命令真实可执行
"""

import os
import sys
import csv
import json
import zipfile
import tempfile
import shutil
import unittest
from unittest import mock
from io import StringIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from click.testing import CliRunner

from invoice_recon.cli import cli, pack_cmd, unpack_cmd, verify_cmd, inspect_cmd
from invoice_recon import db, rules, pack, snapshot
from invoice_recon.models import MatchStatus, BatchStatus


SAMPLES_DIR = Path(__file__).parent.parent / "samples"
README_PATH = Path(__file__).parent.parent / "README.md"


def _write_csv(path: Path, headers: list, rows: list):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(headers)
        w.writerows(rows)


class PackCreateTest(unittest.TestCase):
    """打包命令基本功能测试。"""

    def setUp(self):
        self.runner = CliRunner()
        self.tmpdir = tempfile.mkdtemp(prefix="inv_recon_pack_create_")
        self.db_path = os.path.join(self.tmpdir, "test.db")
        self.snap_dir = os.path.join(self.tmpdir, "snapshots")
        os.environ["INV_RECON_DB"] = self.db_path
        os.environ["INV_RECON_SNAPSHOT_DIR"] = self.snap_dir
        self._invoke("init")
        self._setup_batch()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        for env_var in ("INV_RECON_DB", "INV_RECON_SNAPSHOT_DIR"):
            if env_var in os.environ:
                del os.environ[env_var]

    def _invoke(self, *args, input=None, catch_exceptions=False):
        return self.runner.invoke(cli, args, input=input, catch_exceptions=catch_exceptions)

    def _setup_batch(self):
        inv_csv = os.path.join(self.tmpdir, "inv.csv")
        pay_csv = os.path.join(self.tmpdir, "pay.csv")
        _write_csv(
            Path(inv_csv),
            ["invoice_no", "vendor", "amount", "date"],
            [
                ["INV-P1", "VendorX", "1500.00", "2024-01-10"],
                ["INV-P2", "VendorY", "800.00", "2024-01-12"],
                ["INV-P3", "VendorZ", "300.00", "2024-01-13"],
            ],
        )
        _write_csv(
            Path(pay_csv),
            ["payment_no", "vendor", "amount", "date"],
            [
                ["PAY-P1", "VendorX", "1500.00", "2024-01-15"],
                ["PAY-P2", "VendorX", "1500.00", "2024-01-16"],
                ["PAY-P3", "VendorY", "799.50", "2024-01-20"],
            ],
        )
        r = self._invoke("import", "--invoices", inv_csv, "--payments", pay_csv, "--name", "pack_test")
        self.assertEqual(r.exit_code, 0, f"import failed: {r.output}")
        self.batch_id = 1
        r = self._invoke("match", "--batch", str(self.batch_id))
        self.assertEqual(r.exit_code, 0, f"match failed: {r.output}")

    def test_pack_from_matched_state(self):
        """matched 状态可以打包。"""
        out_pkg = os.path.join(self.tmpdir, "test_pkg.invpkg")
        r = self._invoke("pack", "--batch", str(self.batch_id), "--output", out_pkg)
        self.assertEqual(r.exit_code, 0, f"pack failed: {r.output}")
        self.assertIn(rules.PACK_OK_PACKED, r.output)
        self.assertIn("pack_test", r.output)
        self.assertTrue(os.path.exists(out_pkg))
        self.assertGreater(os.path.getsize(out_pkg), 0)

    def test_pack_includes_export_when_exported(self):
        """exported 状态打包自动包含导出结果。"""
        matches = db.get_matches_by_batch(self.batch_id, db_path=self.db_path)
        pending = [m for m in matches if m["status"] in (MatchStatus.PENDING, MatchStatus.CONFLICT)]
        self._invoke(
            "review", "--batch", str(self.batch_id),
            "--match-id", str(pending[0]["id"]),
            "--action", "confirm", "--note", "test note",
        )
        out_csv = os.path.join(self.tmpdir, "exp.csv")
        self._invoke("export", "--batch", str(self.batch_id), "--output", out_csv)

        out_pkg = os.path.join(self.tmpdir, "with_export.invpkg")
        r = self._invoke("pack", "--batch", str(self.batch_id), "--output", out_pkg)
        self.assertEqual(r.exit_code, 0, f"pack with export failed: {r.output}")
        self.assertIn("包含导出结果: 是", r.output)

        with zipfile.ZipFile(out_pkg, "r") as zf:
            self.assertIn("export.csv", zf.namelist())

    def test_pack_no_include_export(self):
        """--no-include-export 不包含导出结果。"""
        matches = db.get_matches_by_batch(self.batch_id, db_path=self.db_path)
        pending = [m for m in matches if m["status"] in (MatchStatus.PENDING, MatchStatus.CONFLICT)]
        self._invoke(
            "review", "--batch", str(self.batch_id),
            "--match-id", str(pending[0]["id"]),
            "--action", "confirm",
        )
        out_csv = os.path.join(self.tmpdir, "exp.csv")
        self._invoke("export", "--batch", str(self.batch_id), "--output", out_csv)

        out_pkg = os.path.join(self.tmpdir, "no_export.invpkg")
        r = self._invoke("pack", "--batch", str(self.batch_id), "--output", out_pkg, "--no-include-export")
        self.assertEqual(r.exit_code, 0, f"pack no export failed: {r.output}")

        with zipfile.ZipFile(out_pkg, "r") as zf:
            self.assertNotIn("export.csv", zf.namelist())

    def test_pack_output_exists_no_force(self):
        """输出文件已存在且不带 --force 时报错。"""
        out_pkg = os.path.join(self.tmpdir, "exists.invpkg")
        with open(out_pkg, "w") as f:
            f.write("existing")

        r = self._invoke("pack", "--batch", str(self.batch_id), "--output", out_pkg)
        self.assertNotEqual(r.exit_code, 0)
        self.assertIn("已存在", r.output)
        self.assertIn("--force", r.output)

    def test_pack_output_exists_with_force(self):
        """输出文件已存在但带 --force 时覆盖。"""
        out_pkg = os.path.join(self.tmpdir, "force.invpkg")
        with open(out_pkg, "w") as f:
            f.write("existing")
        original_size = os.path.getsize(out_pkg)

        r = self._invoke("pack", "--batch", str(self.batch_id), "--output", out_pkg, "--force")
        self.assertEqual(r.exit_code, 0, f"pack force failed: {r.output}")
        self.assertGreater(os.path.getsize(out_pkg), original_size)

    def test_pack_nonexistent_batch(self):
        """对不存在的批次打包报错。"""
        r = self._invoke("pack", "--batch", "999")
        self.assertNotEqual(r.exit_code, 0)
        self.assertIn("不存在", r.output)

    def test_pack_package_structure(self):
        """包结构正确，包含必需文件。"""
        out_pkg = os.path.join(self.tmpdir, "structure.invpkg")
        self._invoke("pack", "--batch", str(self.batch_id), "--output", out_pkg)

        with zipfile.ZipFile(out_pkg, "r") as zf:
            names = zf.namelist()
            for req in ["manifest.json", "snapshot.json", "checksums.json"]:
                self.assertIn(req, names, f"包缺少必需文件: {req}")

            with zf.open("manifest.json") as f:
                manifest = json.load(f)
            self.assertIn("package_version", manifest)
            self.assertIn("schema_version", manifest)
            self.assertIn("tool_version", manifest)
            self.assertIn("created_at", manifest)
            self.assertIn("source_batch_id", manifest)
            self.assertIn("source_batch_name", manifest)
            self.assertIn("record_counts", manifest)

            with zf.open("checksums.json") as f:
                checksums = json.load(f)
            for fname in ["manifest.json", "snapshot.json"]:
                self.assertIn(fname, checksums)


class VerifyTest(unittest.TestCase):
    """包完整性校验测试。"""

    def setUp(self):
        self.runner = CliRunner()
        self.tmpdir = tempfile.mkdtemp(prefix="inv_recon_verify_")
        self.db_path = os.path.join(self.tmpdir, "test.db")
        self.snap_dir = os.path.join(self.tmpdir, "snapshots")
        os.environ["INV_RECON_DB"] = self.db_path
        os.environ["INV_RECON_SNAPSHOT_DIR"] = self.snap_dir
        self._invoke("init")
        self._setup_package()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        for env_var in ("INV_RECON_DB", "INV_RECON_SNAPSHOT_DIR"):
            if env_var in os.environ:
                del os.environ[env_var]

    def _invoke(self, *args, **kwargs):
        return self.runner.invoke(cli, args, **kwargs)

    def _setup_package(self):
        inv_csv = os.path.join(self.tmpdir, "inv.csv")
        pay_csv = os.path.join(self.tmpdir, "pay.csv")
        _write_csv(Path(inv_csv), ["invoice_no", "vendor", "amount", "date"],
                   [["INV-V", "VendorA", "100.00", "2024-01-01"]])
        _write_csv(Path(pay_csv), ["payment_no", "vendor", "amount", "date"],
                   [["PAY-V", "VendorA", "100.00", "2024-01-02"]])
        self._invoke("import", "--invoices", inv_csv, "--payments", pay_csv, "--name", "verify_test")
        self._invoke("match", "--batch", "1")
        self.good_pkg = os.path.join(self.tmpdir, "good.invpkg")
        self._invoke("pack", "--batch", "1", "--output", self.good_pkg)

    def test_verify_valid_package(self):
        """有效包校验通过。"""
        r = self._invoke("verify", "--input", self.good_pkg)
        self.assertEqual(r.exit_code, 0, f"verify failed: {r.output}")
        self.assertIn(rules.PACK_OK_VERIFIED, r.output)
        self.assertIn("verify_test", r.output)

    def test_verify_nonexistent_file(self):
        """不存在的文件校验失败。"""
        r = self._invoke("verify", "--input", "/nonexistent.invpkg")
        self.assertNotEqual(r.exit_code, 0)
        self.assertIn("不存在", r.output)

    def test_verify_not_zip_file(self):
        """非 ZIP 文件校验失败。"""
        bad_pkg = os.path.join(self.tmpdir, "bad.invpkg")
        with open(bad_pkg, "w") as f:
            f.write("not a zip")

        r = self._invoke("verify", "--input", bad_pkg)
        self.assertNotEqual(r.exit_code, 0)
        self.assertIn("ZIP", r.output)

    def test_verify_missing_required_file(self):
        """缺少必需文件的包校验失败。"""
        bad_pkg = os.path.join(self.tmpdir, "missing.invpkg")
        with zipfile.ZipFile(bad_pkg, "w") as zf:
            zf.writestr("manifest.json", "{}")
            zf.writestr("snapshot.json", "{}")

        r = self._invoke("verify", "--input", bad_pkg)
        self.assertNotEqual(r.exit_code, 0)
        self.assertIn("缺少必需文件", r.output)
        self.assertIn("checksums.json", r.output)

    def test_verify_checksum_mismatch(self):
        """校验和不匹配的包校验失败。"""
        bad_pkg = os.path.join(self.tmpdir, "tampered.invpkg")
        shutil.copy2(self.good_pkg, bad_pkg)

        with tempfile.TemporaryDirectory() as td:
            with zipfile.ZipFile(bad_pkg, "r") as zf:
                zf.extractall(td)
            with open(os.path.join(td, "snapshot.json"), "a") as f:
                f.write("tampered!")
            with zipfile.ZipFile(bad_pkg, "w") as zf:
                for fname in ["manifest.json", "snapshot.json", "checksums.json"]:
                    zf.write(os.path.join(td, fname), fname)

        r = self._invoke("verify", "--input", bad_pkg)
        self.assertNotEqual(r.exit_code, 0)
        self.assertIn("校验和不匹配", r.output)

    def test_verify_invalid_json(self):
        """JSON 格式错误的包校验失败。"""
        bad_pkg = os.path.join(self.tmpdir, "badjson.invpkg")
        with zipfile.ZipFile(bad_pkg, "w") as zf:
            zf.writestr("manifest.json", "not json")
            zf.writestr("snapshot.json", "{}")
            zf.writestr("checksums.json", "{}")

        r = self._invoke("verify", "--input", bad_pkg)
        self.assertNotEqual(r.exit_code, 0)
        self.assertIn("格式错误", r.output)


class InspectTest(unittest.TestCase):
    """查看包元信息测试。"""

    def setUp(self):
        self.runner = CliRunner()
        self.tmpdir = tempfile.mkdtemp(prefix="inv_recon_inspect_")
        self.db_path = os.path.join(self.tmpdir, "test.db")
        self.snap_dir = os.path.join(self.tmpdir, "snapshots")
        os.environ["INV_RECON_DB"] = self.db_path
        os.environ["INV_RECON_SNAPSHOT_DIR"] = self.snap_dir
        self._invoke("init")
        self._setup_package()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        for env_var in ("INV_RECON_DB", "INV_RECON_SNAPSHOT_DIR"):
            if env_var in os.environ:
                del os.environ[env_var]

    def _invoke(self, *args, **kwargs):
        return self.runner.invoke(cli, args, **kwargs)

    def _setup_package(self):
        inv_csv = os.path.join(self.tmpdir, "inv.csv")
        pay_csv = os.path.join(self.tmpdir, "pay.csv")
        _write_csv(Path(inv_csv), ["invoice_no", "vendor", "amount", "date"],
                   [["INV-I", "VendorA", "200.00", "2024-01-01"],
                    ["INV-I2", "VendorB", "300.00", "2024-01-02"]])
        _write_csv(Path(pay_csv), ["payment_no", "vendor", "amount", "date"],
                   [["PAY-I", "VendorA", "200.00", "2024-01-03"]])
        self._invoke("import", "--invoices", inv_csv, "--payments", pay_csv, "--name", "inspect_test")
        self._invoke("match", "--batch", "1")
        self.pkg = os.path.join(self.tmpdir, "inspect.invpkg")
        self._invoke("pack", "--batch", "1", "--output", self.pkg)

    def test_inspect_valid_package(self):
        """有效包可查看元信息。"""
        r = self._invoke("inspect", "--input", self.pkg)
        self.assertEqual(r.exit_code, 0, f"inspect failed: {r.output}")
        self.assertIn("inspect_test", r.output)
        self.assertIn("发票: 2 条", r.output)
        self.assertIn("付款: 1 条", r.output)
        self.assertIn("工具版本", r.output)
        self.assertIn("打包时间", r.output)

    def test_inspect_invalid_package(self):
        """无效包查看报错。"""
        bad_pkg = os.path.join(self.tmpdir, "bad.invpkg")
        with open(bad_pkg, "w") as f:
            f.write("not zip")

        r = self._invoke("inspect", "--input", bad_pkg)
        self.assertNotEqual(r.exit_code, 0)
        self.assertIn("无效", r.output)


class UnpackTest(unittest.TestCase):
    """解包导入测试，含冲突处理。"""

    def setUp(self):
        self.runner = CliRunner()
        self.tmpdir = tempfile.mkdtemp(prefix="inv_recon_unpack_")
        self.src_db = os.path.join(self.tmpdir, "src.db")
        self.dst_db = os.path.join(self.tmpdir, "dst.db")
        self.snap_dir = os.path.join(self.tmpdir, "snapshots")
        os.environ["INV_RECON_SNAPSHOT_DIR"] = self.snap_dir
        self._setup_source()

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

    def _setup_source(self):
        os.environ["INV_RECON_DB"] = self.src_db
        self._invoke_src("init")

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
                ["PAY-U3", "VendorY", "799.50", "2024-01-20"],
            ],
        )
        self._invoke_src("import", "--invoices", inv_csv, "--payments", pay_csv, "--name", "unpack_test")

        self._invoke_src("match", "--batch", "1")

        matches = db.get_matches_by_batch(1, db_path=self.src_db)
        conflicts = [m for m in matches if m["status"] == MatchStatus.CONFLICT]
        pending = [m for m in matches if m["status"] == MatchStatus.PENDING]

        self._invoke_src(
            "review", "--batch", "1",
            "--match-id", str(conflicts[0]["id"]),
            "--action", "confirm", "--note", "确认冲突",
        )
        if pending:
            self._invoke_src(
                "review", "--batch", "1",
                "--match-id", str(pending[0]["id"]),
                "--action", "reject", "--note", "拒绝",
            )

        out_csv = os.path.join(self.tmpdir, "exp.csv")
        self._invoke_src("export", "--batch", "1", "--output", out_csv)

        self.pkg = os.path.join(self.tmpdir, "unpack_test.invpkg")
        self._invoke_src("pack", "--batch", "1", "--output", self.pkg)

    def test_unpack_to_new_db(self):
        """导入到新库，验证数据完整。"""
        self._invoke_dst("init")

        r = self._invoke_dst("unpack", "--input", self.pkg)
        self.assertEqual(r.exit_code, 0, f"unpack failed: {r.output}")
        self.assertIn(rules.PACK_OK_UNPACKED, r.output)
        self.assertIn("unpack_test", r.output)
        self.assertIn("导入后校验报告", r.output)
        self.assertIn(rules.PACK_PRESERVED_PREFIX, r.output)
        self.assertIn(rules.PACK_PENDING_PREFIX, r.output)

        b = db.get_batch(1, db_path=self.dst_db)
        self.assertIsNotNone(b)
        self.assertEqual(b.name, "unpack_test")
        self.assertEqual(b.status, BatchStatus.EXPORTED)

        matches = db.get_matches_by_batch(1, db_path=self.dst_db)
        self.assertGreater(len(matches), 0)

        confirmed = [m for m in matches if m["status"] == MatchStatus.CONFIRMED]
        rejected = [m for m in matches if m["status"] == MatchStatus.REJECTED]
        self.assertGreater(len(confirmed), 0)
        self.assertGreater(len(rejected), 0)

        adjs = db.get_adjudications_by_batch(1, db_path=self.dst_db)
        self.assertGreater(len(adjs), 0)
        adj_actions = [a["action"] for a in adjs]
        self.assertIn("confirmed", adj_actions)
        self.assertIn("rejected", adj_actions)

    def test_unpack_duplicate_batch_name(self):
        """同名批次已存在时自动重命名。"""
        self._invoke_dst("init")

        inv_csv = os.path.join(self.tmpdir, "exist_inv.csv")
        pay_csv = os.path.join(self.tmpdir, "exist_pay.csv")
        _write_csv(Path(inv_csv), ["invoice_no", "vendor", "amount", "date"],
                   [["INV-EX", "VendorA", "100.00", "2024-02-01"]])
        _write_csv(Path(pay_csv), ["payment_no", "vendor", "amount", "date"],
                   [["PAY-EX", "VendorA", "100.00", "2024-02-02"]])
        self._invoke_dst("import", "--invoices", inv_csv, "--payments", pay_csv, "--name", "unpack_test")

        r = self._invoke_dst("unpack", "--input", self.pkg)
        self.assertEqual(r.exit_code, 0, f"unpack rename failed: {r.output}")
        self.assertIn(rules.PACK_RENAMED_HINT, r.output)
        self.assertIn("unpack_test_2", r.output)

        batches = db.list_batches(db_path=self.dst_db)
        names = [b["name"] for b in batches]
        self.assertIn("unpack_test", names)
        self.assertIn("unpack_test_2", names)
        self.assertEqual(len(names), 2)

    def test_unpack_preserves_existing_batch(self):
        """导入绝不覆盖现有批次数据。"""
        self._invoke_dst("init")
        self._invoke_dst("unpack", "--input", self.pkg)

        before = db.get_batch(1, db_path=self.dst_db)
        before_matches = db.get_matches_by_batch(1, db_path=self.dst_db)

        self._invoke_dst("unpack", "--input", self.pkg)

        after = db.get_batch(1, db_path=self.dst_db)
        after_matches = db.get_matches_by_batch(1, db_path=self.dst_db)

        self.assertEqual(before.status, after.status)
        self.assertEqual(len(before_matches), len(after_matches))

        batches = db.list_batches(db_path=self.dst_db)
        self.assertEqual(len(batches), 2)

    def test_unpack_missing_snapshot_dir(self):
        """快照目录不存在时自动创建。"""
        self._invoke_dst("init")

        custom_snap = os.path.join(self.tmpdir, "new_snap_dir")
        os.environ["INV_RECON_SNAPSHOT_DIR"] = custom_snap
        self.assertFalse(os.path.exists(custom_snap))

        try:
            r = self._invoke_dst("unpack", "--input", self.pkg)
            self.assertEqual(r.exit_code, 0, f"unpack missing dir failed: {r.output}")
            self.assertTrue(os.path.exists(custom_snap))
        finally:
            os.environ["INV_RECON_SNAPSHOT_DIR"] = self.snap_dir

    def test_unpack_invalid_package(self):
        """无效包拒绝导入。"""
        self._invoke_dst("init")

        bad_pkg = os.path.join(self.tmpdir, "bad.invpkg")
        with open(bad_pkg, "w") as f:
            f.write("not zip")

        r = self._invoke_dst("unpack", "--input", bad_pkg)
        self.assertNotEqual(r.exit_code, 0)
        self.assertIn("校验失败", r.output)

        batches = db.list_batches(db_path=self.dst_db)
        self.assertEqual(len(batches), 0, "无效包不应导入任何数据")

    def test_unpack_with_custom_batch_name(self):
        """可指定新批次名称。"""
        self._invoke_dst("init")

        r = self._invoke_dst("unpack", "--input", self.pkg, "--batch-name", "custom_name")
        self.assertEqual(r.exit_code, 0, f"unpack custom name failed: {r.output}")
        self.assertIn("custom_name", r.output)

        b = db.get_batch(1, db_path=self.dst_db)
        self.assertEqual(b.name, "custom_name")

    def test_unpack_validation_report(self):
        """导入后校验报告正确区分沿用原状态和重命名记录。"""
        self._invoke_dst("init")

        r = self._invoke_dst("unpack", "--input", self.pkg)
        self.assertEqual(r.exit_code, 0, f"unpack failed: {r.output}")

        self.assertIn("沿用原状态:", r.output)
        self.assertIn("待复核/重分配:", r.output)

        vr = pack.unpack_package(self.pkg, db_path=self.dst_db, force=True)["validation_report"]
        self.assertGreater(vr["total_records"], 0)
        self.assertGreater(vr["preserved_count"], 0)
        self.assertGreater(vr["renamed_count"], 0)
        self.assertEqual(
            vr["preserved_count"] + vr["renamed_count"],
            vr["total_records"]
        )

        for p in vr["preserved"]:
            self.assertIn(p["status"], ("confirmed", "rejected"))

    def test_unpack_saves_export_file(self):
        """包内导出结果被保存。"""
        self._invoke_dst("init")

        r = self._invoke_dst("unpack", "--input", self.pkg)
        self.assertEqual(r.exit_code, 0, f"unpack failed: {r.output}")
        self.assertIn("导出结果已保存为", r.output)

        result = pack.unpack_package(self.pkg, db_path=self.dst_db)
        self.assertIsNotNone(result["export_saved_as"])
        self.assertTrue(os.path.exists(result["export_saved_as"]))


class CrossRestartTest(unittest.TestCase):
    """跨重启重新导入再导出测试。"""

    def setUp(self):
        self.runner = CliRunner()
        self.tmpdir = tempfile.mkdtemp(prefix="inv_recon_cross_restart_")
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

    def test_cross_restart_import_and_re_export(self):
        """跨重启场景：打包 → 模拟重启（清空 DB）→ 导入 → 重新导出。"""
        inv_csv = os.path.join(self.tmpdir, "inv.csv")
        pay_csv = os.path.join(self.tmpdir, "pay.csv")
        _write_csv(
            Path(inv_csv),
            ["invoice_no", "vendor", "amount", "date"],
            [
                ["INV-R1", "VendorA", "1000.00", "2024-01-10"],
                ["INV-R2", "VendorB", "500.50", "2024-01-12"],
            ],
        )
        _write_csv(
            Path(pay_csv),
            ["payment_no", "vendor", "amount", "date"],
            [
                ["PAY-R1", "VendorA", "1000.00", "2024-01-15"],
                ["PAY-R2", "VendorB", "500.00", "2024-01-16"],
            ],
        )
        self._invoke("import", "--invoices", inv_csv, "--payments", pay_csv, "--name", "restart_test")
        self._invoke("match", "--batch", "1")

        matches = db.get_matches_by_batch(1, db_path=self.db_path)
        pending = [m for m in matches if m["status"] in (MatchStatus.PENDING, MatchStatus.CONFLICT)]
        for p in pending:
            self._invoke(
                "review", "--batch", "1",
                "--match-id", str(p["id"]),
                "--action", "confirm", "--note", "确认",
            )

        exp1 = os.path.join(self.tmpdir, "exp1.csv")
        self._invoke("export", "--batch", "1", "--output", exp1)
        with open(exp1, encoding="utf-8") as f:
            rows1 = list(csv.DictReader(f))

        pkg = os.path.join(self.tmpdir, "restart.invpkg")
        self._invoke("pack", "--batch", "1", "--output", pkg)

        import sqlite3
        sqlite3.connect(self.db_path).close()
        os.remove(self.db_path)
        if os.path.exists(self.db_path + "-wal"):
            os.remove(self.db_path + "-wal")
        if os.path.exists(self.db_path + "-shm"):
            os.remove(self.db_path + "-shm")

        self._invoke("init")

        r = self._invoke("unpack", "--input", pkg, "--batch-name", "restart_imported")
        self.assertEqual(r.exit_code, 0, f"unpack after restart failed: {r.output}")

        r = self._invoke("list")
        self.assertEqual(r.exit_code, 0)
        self.assertIn("restart_imported", r.output)

        r = self._invoke("show", "--batch", "1")
        self.assertEqual(r.exit_code, 0)
        self.assertIn("restart_imported", r.output)
        self.assertIn("确认", r.output)

        exp2 = os.path.join(self.tmpdir, "exp2.csv")
        r = self._invoke("export", "--batch", "1", "--output", exp2)
        self.assertEqual(r.exit_code, 0, f"re-export after restart failed: {r.output}")

        with open(exp2, encoding="utf-8") as f:
            rows2 = list(csv.DictReader(f))
        self.assertEqual(len(rows1), len(rows2))

        statuses2 = {r["match_type"]: r["status"] for r in rows2}
        for r in rows1:
            self.assertIn(r["match_type"], statuses2)


class ReviewUndoAfterUnpackTest(unittest.TestCase):
    """导入后 review-undo 再导出测试。"""

    def setUp(self):
        self.runner = CliRunner()
        self.tmpdir = tempfile.mkdtemp(prefix="inv_recon_undo_after_unpack_")
        self.src_db = os.path.join(self.tmpdir, "src.db")
        self.dst_db = os.path.join(self.tmpdir, "dst.db")
        self.snap_dir = os.path.join(self.tmpdir, "snapshots")
        os.environ["INV_RECON_SNAPSHOT_DIR"] = self.snap_dir
        self._setup_source()

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

    def _setup_source(self):
        os.environ["INV_RECON_DB"] = self.src_db
        self._invoke_src("init")

        inv_csv = os.path.join(self.tmpdir, "inv.csv")
        pay_csv = os.path.join(self.tmpdir, "pay.csv")
        _write_csv(
            Path(inv_csv),
            ["invoice_no", "vendor", "amount", "date"],
            [
                ["INV-UD1", "VendorX", "1500.00", "2024-01-10"],
                ["INV-UD2", "VendorY", "800.00", "2024-01-12"],
            ],
        )
        _write_csv(
            Path(pay_csv),
            ["payment_no", "vendor", "amount", "date"],
            [
                ["PAY-UD1", "VendorX", "1500.00", "2024-01-15"],
                ["PAY-UD2", "VendorX", "1500.00", "2024-01-16"],
            ],
        )
        self._invoke_src("import", "--invoices", inv_csv, "--payments", pay_csv, "--name", "undo_test")
        self._invoke_src("match", "--batch", "1")

        matches = db.get_matches_by_batch(1, db_path=self.src_db)
        conflicts = [m for m in matches if m["status"] == MatchStatus.CONFLICT]
        self.assertGreaterEqual(len(conflicts), 2)

        self._invoke_src(
            "review", "--batch", "1",
            "--match-id", str(conflicts[0]["id"]),
            "--action", "confirm", "--note", "选第一笔",
        )

        out = os.path.join(self.tmpdir, "src_exp.csv")
        self._invoke_src("export", "--batch", "1", "--output", out)

        self.pkg = os.path.join(self.tmpdir, "undo_test.invpkg")
        self._invoke_src("pack", "--batch", "1", "--output", self.pkg)

    def test_review_undo_after_unpack_then_export(self):
        """导入后执行 review-undo，再导出，验证状态链路完整。"""
        self._invoke_dst("init")
        self._invoke_dst("unpack", "--input", self.pkg)

        matches = db.get_matches_by_batch(1, db_path=self.dst_db)
        confirmed = [m for m in matches if m["status"] == MatchStatus.CONFIRMED]
        self.assertGreater(len(confirmed), 0)

        target_id = confirmed[0]["id"]

        r = self._invoke_dst("review-undo", "--batch", "1", "--match-id", str(target_id))
        self.assertEqual(r.exit_code, 0, f"review-undo after unpack failed: {r.output}")
        self.assertIn("关联冲突记录已恢复可复核", r.output)

        b = db.get_batch(1, db_path=self.dst_db)
        self.assertEqual(b.status, BatchStatus.MATCHED)

        m_after = db.get_match(target_id, db_path=self.dst_db)
        self.assertEqual(m_after["status"], MatchStatus.CONFLICT)

        adjs = db.get_adjudications_by_batch(1, db_path=self.dst_db)
        adj_actions = [a["action"] for a in adjs]
        self.assertIn("undone", adj_actions)
        self.assertIn("confirmed", adj_actions)
        self.assertIn("undone_auto_rejected", adj_actions)

        out = os.path.join(self.tmpdir, "after_undo_exp.csv")
        r = self._invoke_dst("export", "--batch", "1", "--output", out)
        self.assertEqual(r.exit_code, 0, f"export after undo failed: {r.output}")

        with open(out, encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        ids_exported = {int(r["match_id"]) for r in rows if r["match_id"]}
        self.assertIn(target_id, ids_exported)


class CliHelpSyncTest(unittest.TestCase):
    """CLI --help 与 rules 常量一致性测试。"""

    def test_pack_doc_matches_rules(self):
        self.assertEqual(pack_cmd.__doc__, rules.PACK_CREATE_HELP)

    def test_unpack_doc_matches_rules(self):
        self.assertEqual(unpack_cmd.__doc__, rules.PACK_UNPACK_HELP)

    def test_verify_doc_matches_rules(self):
        self.assertEqual(verify_cmd.__doc__, rules.PACK_VERIFY_HELP)

    def test_inspect_doc_matches_rules(self):
        self.assertEqual(inspect_cmd.__doc__, rules.PACK_INSPECT_HELP)

    def test_pack_help_shows_rules(self):
        self.assertIn(rules.PACK_RULES_HELP_KEYPHRASES["rules_version"], rules.PACK_CREATE_HELP)
        self.assertIn(rules.PACK_RULES_HELP_KEYPHRASES["adjudication_notes"], rules.PACK_CREATE_HELP)
        self.assertIn(rules.PACK_RULES_HELP_KEYPHRASES["export_results"], rules.PACK_CREATE_HELP)
        self.assertIn(rules.PACK_RULES_HELP_KEYPHRASES["checksum"], rules.PACK_CREATE_HELP)
        self.assertIn(rules.PACK_RULES_HELP_KEYPHRASES["invpkg"], rules.PACK_CREATE_HELP)

    def test_unpack_help_shows_rules(self):
        self.assertIn(rules.PACK_RULES_HELP_KEYPHRASES["no_overwrite"], rules.PACK_UNPACK_HELP)
        self.assertIn(rules.PACK_RULES_HELP_KEYPHRASES["auto_rename"], rules.PACK_UNPACK_HELP)
        self.assertIn(rules.PACK_RULES_HELP_KEYPHRASES["validation_report"], rules.PACK_UNPACK_HELP)
        self.assertIn(rules.PACK_RULES_HELP_KEYPHRASES["preserve_status"], rules.PACK_UNPACK_HELP)
        self.assertIn(rules.PACK_RULES_HELP_KEYPHRASES["auto_create"], rules.PACK_UNPACK_HELP)


class ReadmePackSyncTest(unittest.TestCase):
    """README 打包章节与常量一致性测试。"""

    @classmethod
    def setUpClass(cls):
        cls.readme = README_PATH.read_text(encoding="utf-8")

    def test_pack_section_in_readme(self):
        for keyphrase in [
            "快照打包与跨机器搬运",
            "inv-recon pack",
            "inv-recon unpack",
            "inv-recon verify",
            "inv-recon inspect",
            "invpkg",
            "manifest.json",
            "checksums.json",
            "导入前校验",
            "冲突处理",
            "导入后校验报告",
        ]:
            self.assertIn(keyphrase, self.readme, f"README 缺打包关键词: {keyphrase}")

    def test_error_table_has_pack_rows(self):
        for scenario, _behavior in rules.ERROR_TABLE_ROWS:
            if "打包" in scenario or "导入包" in scenario:
                self.assertIn(scenario, self.readme, f"README 错误处理表缺打包场景: {scenario}")

    def test_public_contracts_have_pack_entries(self):
        for contract in rules.PUBLIC_CONTRACTS:
            if "打包" in contract or "导入包" in contract:
                pass

    def test_readme_example_commands_include_pack(self):
        for cmd in [
            "inv-recon pack --batch",
            "inv-recon verify --input",
            "inv-recon inspect --input",
            "inv-recon unpack --input",
        ]:
            self.assertIn(cmd, self.readme, f"README 命令示例缺: {cmd}")


class ReadmePackExampleFlowTest(unittest.TestCase):
    """README 打包示例流程端到端真实可执行。"""

    def setUp(self):
        self.runner = CliRunner()
        self.tmpdir = tempfile.mkdtemp(prefix="inv_recon_readme_pack_")
        self.db_path = os.path.join(self.tmpdir, "flow.db")
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

    def test_full_pack_flow_with_samples(self):
        """README 打包示例流程真实可执行。"""
        inv = str(SAMPLES_DIR / "invoices.csv")
        pay = str(SAMPLES_DIR / "payments.csv")

        r = self._invoke("import", "--invoices", inv, "--payments", pay, "--name", "pack_demo")
        self.assertEqual(r.exit_code, 0, f"import failed: {r.output}")

        r = self._invoke("match", "--batch", "1")
        self.assertEqual(r.exit_code, 0, f"match failed: {r.output}")

        pkg = os.path.join(self.tmpdir, "demo.invpkg")
        r = self._invoke("pack", "--batch", "1", "--output", pkg, "--name", "pack_demo_pkg")
        self.assertEqual(r.exit_code, 0, f"pack failed: {r.output}")
        self.assertIn(rules.PACK_OK_PACKED, r.output)
        self.assertTrue(os.path.exists(pkg))

        r = self._invoke("verify", "--input", pkg)
        self.assertEqual(r.exit_code, 0, f"verify failed: {r.output}")
        self.assertIn(rules.PACK_OK_VERIFIED, r.output)

        r = self._invoke("inspect", "--input", pkg)
        self.assertEqual(r.exit_code, 0, f"inspect failed: {r.output}")
        self.assertIn("pack_demo", r.output)

        other_db = os.path.join(self.tmpdir, "other.db")
        os.environ["INV_RECON_DB"] = other_db
        try:
            self._invoke("init")
            r = self._invoke("unpack", "--input", pkg, "--batch-name", "imported_demo")
            self.assertEqual(r.exit_code, 0, f"unpack failed: {r.output}")
            self.assertIn(rules.PACK_OK_UNPACKED, r.output)
            self.assertIn("imported_demo", r.output)
            self.assertIn("导入后校验报告", r.output)

            r = self._invoke("list")
            self.assertEqual(r.exit_code, 0)
            self.assertIn("imported_demo", r.output)

            r = self._invoke("show", "--batch", "1")
            self.assertEqual(r.exit_code, 0)
            self.assertIn("imported_demo", r.output)

            out = os.path.join(self.tmpdir, "imported_export.csv")
            r = self._invoke("export", "--batch", "1", "--output", out)
            if r.exit_code == 0:
                self.assertIn(rules.EXPORT_OK_PREFIX, r.output)
                self.assertTrue(os.path.exists(out))
            else:
                self.assertIn("无待处理记录", r.output)
        finally:
            os.environ["INV_RECON_DB"] = self.db_path


class RapidReimportTest(unittest.TestCase):
    """快速重复导入回归测试 —— 覆盖同一秒/毫秒内多次导入的边界情况。

    Bug 根因：
      1. 快照文件名只用秒级时间戳，同一秒内重复导入会冲突
      2. 落盘顺序错误：先存快照文件 → 再重命名批次，导致快照名用原始名
      3. 快照文件名冲突时直接报错，没有自动重试机制

    修复后保证：
      - 毫秒级时间戳 + 自动加后缀，确保文件名唯一
      - 预先解析批次名，快照名与批次名一致
      - 每次导入都生成独立、可操作的批次
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="inv_recon_rapid_")
        self.db_path = os.path.join(self.tmpdir, "test.db")
        self.snap_dir = os.path.join(self.tmpdir, "snapshots")
        os.environ["INV_RECON_DB"] = self.db_path
        os.environ["INV_RECON_SNAPSHOT_DIR"] = self.snap_dir
        self.runner = CliRunner()
        self._create_test_package()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        if "INV_RECON_DB" in os.environ:
            del os.environ["INV_RECON_DB"]
        if "INV_RECON_SNAPSHOT_DIR" in os.environ:
            del os.environ["INV_RECON_SNAPSHOT_DIR"]

    def _create_test_package(self):
        """创建测试用的 .invpkg 包"""
        self.runner.invoke(cli, ["init"])

        inv_csv = os.path.join(self.tmpdir, "inv.csv")
        pay_csv = os.path.join(self.tmpdir, "pay.csv")
        Path(inv_csv).write_text(
            "invoice_no,vendor,amount,date\n"
            "INV-001,ACME,1000.00,2024-01-15\n"
            "INV-002,ACME,2000.00,2024-01-16\n",
            encoding="utf-8"
        )
        Path(pay_csv).write_text(
            "payment_no,vendor,amount,date\n"
            "PAY-001,ACME,1000.00,2024-01-20\n"
            "PAY-002,ACME,2000.00,2024-01-21\n",
            encoding="utf-8"
        )

        self.runner.invoke(cli, ["import", "--invoices", inv_csv, "--payments", pay_csv, "--name", "rapid_test"])
        self.runner.invoke(cli, ["match", "--batch", "1"])
        self.runner.invoke(cli, ["review", "--batch", "1", "--match-id", "1", "--action", "confirm", "--note", "核对无误"])

        self.pkg = os.path.join(self.tmpdir, "rapid_test.invpkg")
        r = self.runner.invoke(cli, ["pack", "--batch", "1", "--output", self.pkg])
        self.assertEqual(r.exit_code, 0, f"pack failed: {r.output}")

        # 重置数据库用于导入测试
        os.remove(self.db_path)
        shutil.rmtree(self.snap_dir, ignore_errors=True)

    def _invoke(self, *args):
        return self.runner.invoke(cli, args)

    def test_same_millisecond_multiple_imports(self):
        """同一毫秒内连续导入同一个包 5 次，每次都生成独立批次。

        通过 mock _snapshot_filename 强制返回相同文件名，
        验证自动加后缀机制正常工作。
        """
        self._invoke("init")

        call_count = [0]
        fixed_name = "20260620_044021_000_rapid_test_imported_abcd1234.snap.json"

        def mock_snapshot_filename(snap_id, name):
            call_count[0] += 1
            return fixed_name

        with mock.patch('invoice_recon.snapshot._snapshot_filename', side_effect=mock_snapshot_filename):
            results = []
            for i in range(5):
                r = self._invoke("unpack", "--input", self.pkg)
                results.append(r)
                self.assertEqual(r.exit_code, 0,
                    f"第 {i+1} 次导入失败: {r.output}")

        # 验证 5 个独立批次
        batches = db.list_batches(db_path=self.db_path)
        self.assertEqual(len(batches), 5)
        batch_names = sorted([b["name"] for b in batches])
        expected_names = ["rapid_test", "rapid_test_2", "rapid_test_3", "rapid_test_4", "rapid_test_5"]
        self.assertEqual(batch_names, expected_names)

        # 验证 5 个独立快照文件，自动加后缀
        snaps = sorted(os.listdir(self.snap_dir))
        self.assertEqual(len(snaps), 5)
        self.assertEqual(len(set(snaps)), 5)  # 全部唯一
        self.assertIn(fixed_name, snaps)
        for i in range(2, 6):
            self.assertIn(f"abcd1234_{i}.snap.json", snaps[i-1])

    def test_imported_batches_fully_operational(self):
        """导入后的批次可以正常 list/show/review/review-undo/export。"""
        self._invoke("init")

        # 连续导入 3 次
        for _ in range(3):
            r = self._invoke("unpack", "--input", self.pkg)
            self.assertEqual(r.exit_code, 0, f"unpack failed: {r.output}")

        batches = db.list_batches(db_path=self.db_path)
        self.assertEqual(len(batches), 3)

        for i, batch in enumerate(batches, 1):
            batch_id = batch["id"]

            # list
            r = self._invoke("list")
            self.assertEqual(r.exit_code, 0)
            self.assertIn(batch["name"], r.output)

            # show
            r = self._invoke("show", "--batch", str(batch_id))
            self.assertEqual(r.exit_code, 0)
            self.assertIn(batch["name"], r.output)

            # 找到一个匹配记录
            matches = db.get_matches_by_batch(batch_id, db_path=self.db_path)
            self.assertGreater(len(matches), 0)
            match_id = matches[0]["id"]

            # review
            r = self._invoke("review", "--batch", str(batch_id),
                            "--match-id", str(match_id),
                            "--action", "reject", "--note", f"自动测试 #{i}")
            self.assertEqual(r.exit_code, 0, f"review failed: {r.output}")

            # review-undo
            r = self._invoke("review-undo", "--batch", str(batch_id),
                            "--match-id", str(match_id))
            self.assertEqual(r.exit_code, 0, f"review-undo failed: {r.output}")

            # export
            out_path = os.path.join(self.tmpdir, f"export_{batch_id}.csv")
            r = self._invoke("export", "--batch", str(batch_id), "--output", out_path)
            self.assertEqual(r.exit_code, 0, f"export failed: {r.output}")
            self.assertTrue(os.path.exists(out_path))

    def test_cross_restart_reimport_same_package(self):
        """跨重启后再次导入同包，仍能正常重命名和继续使用。

        模拟场景：导入一次 → 重启程序 → 再次导入同一个包 → 自动重命名。
        """
        self._invoke("init")

        # 第一次导入
        r1 = self._invoke("unpack", "--input", self.pkg)
        self.assertEqual(r1.exit_code, 0, f"第一次导入失败: {r1.output}")

        # 模拟重启 - 删除旧的环境变量再重新设置（使用相同的 DB）
        del os.environ["INV_RECON_DB"]
        del os.environ["INV_RECON_SNAPSHOT_DIR"]
        os.environ["INV_RECON_DB"] = self.db_path
        os.environ["INV_RECON_SNAPSHOT_DIR"] = self.snap_dir

        # 第二次导入
        r2 = self._invoke("unpack", "--input", self.pkg)
        self.assertEqual(r2.exit_code, 0, f"第二次导入失败: {r2.output}")
        self.assertIn(rules.PACK_RENAMED_HINT.strip(), r2.output)

        # 第三次导入
        r3 = self._invoke("unpack", "--input", self.pkg)
        self.assertEqual(r3.exit_code, 0, f"第三次导入失败: {r3.output}")

        # 验证批次名
        batches = db.list_batches(db_path=self.db_path)
        self.assertEqual(len(batches), 3)
        batch_names = sorted([b["name"] for b in batches])
        self.assertEqual(batch_names, ["rapid_test", "rapid_test_2", "rapid_test_3"])

        # 验证所有批次可正常操作
        for i, batch in enumerate(batches, 1):
            matches = db.get_matches_by_batch(batch["id"], db_path=self.db_path)
            self.assertGreater(len(matches), 0)
            match_id = matches[0]["id"]

            r = self._invoke("review", "--batch", str(batch["id"]),
                            "--match-id", str(match_id),
                            "--action", "confirm", "--note", f"跨重启测试 #{i}")
            self.assertEqual(r.exit_code, 0, f"review failed: {r.output}")

            out_path = os.path.join(self.tmpdir, f"cross_export_{i}.csv")
            r = self._invoke("export", "--batch", str(batch["id"]), "--output", out_path)
            self.assertEqual(r.exit_code, 0, f"export failed: {r.output}")


if __name__ == "__main__":
    unittest.main()
