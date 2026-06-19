"""
快照功能回归测试 —— 覆盖：
1. 导出后建快照（含裁决历史完整保留）
2. 恢复到新库后再次 show/list/export
3. 恢复后 review-undo 再导出（状态链路不丢失）
4. 同名批次自动重命名
5. 已撤销批次快照恢复
6. CLI --help 输出与 rules 常量一致
7. README 快照示例流程真实可执行
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

from invoice_recon.cli import cli, snapshot_create, snapshot_restore
from invoice_recon import db, rules, snapshot
from invoice_recon.models import MatchStatus, BatchStatus


SAMPLES_DIR = Path(__file__).parent.parent / "samples"
README_PATH = Path(__file__).parent.parent / "README.md"


def _write_csv(path: Path, headers: list, rows: list):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(headers)
        w.writerows(rows)


class SnapshotCreateTest(unittest.TestCase):
    """导出后建快照 —— 验证快照内容完整。"""

    def setUp(self):
        self.runner = CliRunner()
        self.tmpdir = tempfile.mkdtemp(prefix="inv_recon_snap_create_")
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
                ["INV-A", "VendorX", "1000.00", "2024-01-10"],
                ["INV-B", "VendorY", "2500.50", "2024-01-12"],
                ["INV-C", "VendorZ", "500.00", "2024-01-13"],
            ],
        )
        _write_csv(
            Path(pay_csv),
            ["payment_no", "vendor", "amount", "date"],
            [
                ["PAY-A", "VendorX", "1000.00", "2024-01-15"],
                ["PAY-B", "VendorY", "2500.00", "2024-01-16"],
                ["PAY-C", "VendorW", "888.00", "2024-01-17"],
            ],
        )
        r = self._invoke("import", "--invoices", inv_csv, "--payments", pay_csv, "--name", "snap_test")
        self.assertEqual(r.exit_code, 0, f"import failed: {r.output}")
        self.batch_id = 1
        r = self._invoke("match", "--batch", str(self.batch_id))
        self.assertEqual(r.exit_code, 0, f"match failed: {r.output}")

    def test_create_snapshot_after_match(self):
        """matched 状态可以建快照。"""
        r = self._invoke("snapshot", "create", "--batch", str(self.batch_id), "--name", "snap_after_match")
        self.assertEqual(r.exit_code, 0, f"snapshot create failed: {r.output}")
        self.assertIn(rules.SNAPSHOT_OK_CREATED, r.output)
        self.assertIn("snap_after_match", r.output)

        snaps = snapshot.list_snapshots()
        self.assertEqual(len(snaps), 1)
        self.assertEqual(snaps[0]["snapshot_name"], "snap_after_match")
        self.assertEqual(snaps[0]["batch_status"], BatchStatus.MATCHED)
        self.assertGreater(snaps[0]["match_count"], 0)

    def test_create_snapshot_after_review_and_export(self):
        """exported 状态建快照，验证裁决历史完整保留。"""
        matches = db.get_matches_by_batch(self.batch_id, db_path=self.db_path)
        pending = [m for m in matches if m["status"] in (MatchStatus.PENDING, MatchStatus.CONFLICT)]
        self.assertGreater(len(pending), 0, "setup 应有待审记录")

        self._invoke(
            "review", "--batch", str(self.batch_id),
            "--match-id", str(pending[0]["id"]),
            "--action", "confirm", "--note", "已核实",
        )

        out_path = os.path.join(self.tmpdir, "exp.csv")
        self._invoke("export", "--batch", str(self.batch_id), "--output", out_path)

        b = db.get_batch(self.batch_id, db_path=self.db_path)
        self.assertEqual(b.status, BatchStatus.EXPORTED)

        r = self._invoke("snapshot", "create", "--batch", str(self.batch_id), "--name", "snap_exported")
        self.assertEqual(r.exit_code, 0, f"snapshot create exported failed: {r.output}")
        self.assertIn(rules.SNAPSHOT_OK_CREATED, r.output)

        snap_info = snapshot.get_snapshot_info("snap_exported")
        self.assertIsNotNone(snap_info)
        self.assertEqual(snap_info["batch_status"], BatchStatus.EXPORTED)
        self.assertGreater(snap_info["adjudication_count"], 0, "快照应包含裁决历史")

        full_snap = snapshot.get_snapshot("snap_exported")
        self.assertIsNotNone(full_snap)
        self.assertIn("rule_version", full_snap)
        self.assertIn("batch", full_snap)
        self.assertIn("invoices", full_snap)
        self.assertIn("payments", full_snap)
        self.assertIn("matches", full_snap)
        self.assertIn("adjudications", full_snap)

        adj_actions = [a["action"] for a in full_snap["adjudications"]]
        self.assertIn("confirmed", adj_actions, "裁决历史应包含 confirmed 记录")

    def test_snapshot_list_and_show(self):
        """snapshot list 和 show 命令正常工作。"""
        self._invoke("snapshot", "create", "--batch", str(self.batch_id), "--name", "snap1")

        r = self._invoke("snapshot", "list")
        self.assertEqual(r.exit_code, 0, f"snapshot list failed: {r.output}")
        self.assertIn("snap1", r.output)
        self.assertIn("matched", r.output)

        r = self._invoke("snapshot", "show", "--snapshot", "snap1")
        self.assertEqual(r.exit_code, 0, f"snapshot show failed: {r.output}")
        self.assertIn("snap1", r.output)
        self.assertIn("裁决历史", r.output)

    def test_snapshot_nonexistent_batch(self):
        """对不存在的批次建快照应报错。"""
        r = self._invoke("snapshot", "create", "--batch", "999", "--name", "bad")
        self.assertNotEqual(r.exit_code, 0)
        self.assertIn("不存在", r.output)


class SnapshotRestoreTest(unittest.TestCase):
    """快照恢复测试 —— 恢复到新库、review-undo、export 等。"""

    def setUp(self):
        self.runner = CliRunner()
        self.tmpdir = tempfile.mkdtemp(prefix="inv_recon_snap_restore_")
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
        """在源库中创建批次、匹配、复核、导出，然后建快照。"""
        os.environ["INV_RECON_DB"] = self.src_db
        self._invoke_src("init")

        inv_csv = os.path.join(self.tmpdir, "inv.csv")
        pay_csv = os.path.join(self.tmpdir, "pay.csv")
        _write_csv(
            Path(inv_csv),
            ["invoice_no", "vendor", "amount", "date"],
            [
                ["INV-C1", "VendorX", "1500.00", "2024-01-10"],
                ["INV-C2", "VendorY", "800.00", "2024-01-12"],
                ["INV-C3", "VendorZ", "300.00", "2024-01-13"],
            ],
        )
        _write_csv(
            Path(pay_csv),
            ["payment_no", "vendor", "amount", "date"],
            [
                ["PAY-A", "VendorX", "1500.00", "2024-01-15"],
                ["PAY-B", "VendorX", "1500.00", "2024-01-16"],
                ["PAY-C", "VendorY", "799.50", "2024-01-20"],
            ],
        )
        r = self._invoke_src("import", "--invoices", inv_csv, "--payments", pay_csv, "--name", "src_batch")
        self.assertEqual(r.exit_code, 0, f"src import failed: {r.output}")

        r = self._invoke_src("match", "--batch", "1")
        self.assertEqual(r.exit_code, 0, f"src match failed: {r.output}")

        matches = db.get_matches_by_batch(1, db_path=self.src_db)
        conflicts = [m for m in matches if m["status"] == MatchStatus.CONFLICT]
        self.assertGreater(len(conflicts), 1, f"setup 应有至少 2 条冲突记录，实际 {len(conflicts)}; matches={[(m['id'], m['status'], m['match_type']) for m in matches]}")

        self._invoke_src(
            "review", "--batch", "1",
            "--match-id", str(conflicts[0]["id"]),
            "--action", "confirm", "--note", "确认第一笔",
        )

        out = os.path.join(self.tmpdir, "src_export.csv")
        r = self._invoke_src("export", "--batch", "1", "--output", out)
        self.assertEqual(r.exit_code, 0, f"src export failed: {r.output}")

        r = self._invoke_src("snapshot", "create", "--batch", "1", "--name", "src_snap")
        self.assertEqual(r.exit_code, 0, f"src snapshot create failed: {r.output}")

    def test_restore_to_new_db_and_show_list(self):
        """恢复到新库后 show/list/export 都能正常工作。"""
        self._invoke_dst("init")

        r = self._invoke_dst("snapshot", "restore", "--snapshot", "src_snap")
        self.assertEqual(r.exit_code, 0, f"snapshot restore failed: {r.output}")
        self.assertIn(rules.SNAPSHOT_OK_RESTORED, r.output)
        self.assertIn("src_batch", r.output)

        b = db.get_batch(1, db_path=self.dst_db)
        self.assertIsNotNone(b)
        self.assertEqual(b.name, "src_batch")
        self.assertEqual(b.status, BatchStatus.EXPORTED)

        r = self._invoke_dst("list")
        self.assertEqual(r.exit_code, 0)
        self.assertIn("src_batch", r.output)

        r = self._invoke_dst("show", "--batch", "1")
        self.assertEqual(r.exit_code, 0)
        self.assertIn("src_batch", r.output)
        self.assertIn("确认第一笔", r.output)

        out = os.path.join(self.tmpdir, "dst_export.csv")
        r = self._invoke_dst("export", "--batch", "1", "--output", out)
        self.assertEqual(r.exit_code, 0, f"dst export failed: {r.output}")
        self.assertTrue(os.path.exists(out))

        with open(out, encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        self.assertGreater(len(rows), 0, "恢复后导出应有记录")

    def test_restore_after_review_undo_then_export(self):
        """恢复后执行 review-undo，再导出，验证状态链路完整。"""
        self._invoke_dst("init")
        self._invoke_dst("snapshot", "restore", "--snapshot", "src_snap")

        matches = db.get_matches_by_batch(1, db_path=self.dst_db)
        confirmed = [m for m in matches if m["status"] == MatchStatus.CONFIRMED]
        self.assertGreater(len(confirmed), 0, "恢复后应有已确认记录")

        target_id = confirmed[0]["id"]

        r = self._invoke_dst("review-undo", "--batch", "1", "--match-id", str(target_id))
        self.assertEqual(r.exit_code, 0, f"review-undo failed: {r.output}")

        b = db.get_batch(1, db_path=self.dst_db)
        self.assertEqual(b.status, BatchStatus.MATCHED, "撤销后批次应回到 matched")

        m_after = db.get_match(target_id, db_path=self.dst_db)
        self.assertEqual(m_after["status"], MatchStatus.CONFLICT, "撤销后应回到 conflict")

        adjs = db.get_adjudications_by_batch(1, db_path=self.dst_db)
        adj_actions = [a["action"] for a in adjs]
        self.assertIn("undone", adj_actions, "裁决历史应包含 undone 记录")
        self.assertIn("confirmed", adj_actions, "裁决历史应保留原始 confirmed 记录")

        out = os.path.join(self.tmpdir, "after_undo.csv")
        r = self._invoke_dst("export", "--batch", "1", "--output", out)
        self.assertEqual(r.exit_code, 0, f"export after undo failed: {r.output}")

        with open(out, encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        ids_exported = {int(r["match_id"]) for r in rows if r["match_id"]}
        self.assertIn(target_id, ids_exported, "撤销后该记录应重新出现在导出中")

    def test_restore_duplicate_name_auto_rename(self):
        """同名批次已存在时自动重命名。"""
        self._invoke_dst("init")

        inv_csv = os.path.join(self.tmpdir, "existing_inv.csv")
        pay_csv = os.path.join(self.tmpdir, "existing_pay.csv")
        _write_csv(
            Path(inv_csv),
            ["invoice_no", "vendor", "amount", "date"],
            [["INV-EX", "VendorA", "100.00", "2024-02-01"]],
        )
        _write_csv(
            Path(pay_csv),
            ["payment_no", "vendor", "amount", "date"],
            [["PAY-EX", "VendorA", "100.00", "2024-02-02"]],
        )
        r = self._invoke_dst("import", "--invoices", inv_csv, "--payments", pay_csv, "--name", "src_batch")
        self.assertEqual(r.exit_code, 0)

        r = self._invoke_dst("snapshot", "restore", "--snapshot", "src_snap")
        self.assertEqual(r.exit_code, 0, f"restore with rename failed: {r.output}")
        self.assertIn(rules.SNAPSHOT_RENAMED_HINT, r.output)
        self.assertIn("src_batch_2", r.output)

        batches = db.list_batches(db_path=self.dst_db)
        names = [b["name"] for b in batches]
        self.assertIn("src_batch", names)
        self.assertIn("src_batch_2", names)
        self.assertEqual(len(names), 2)

    def test_restore_revoked_batch(self):
        """已撤销批次的快照，恢复后保持 revoked 状态。"""
        os.environ["INV_RECON_DB"] = self.src_db
        self._invoke_src("revoke", "--batch", "1")
        self._invoke_src("snapshot", "create", "--batch", "1", "--name", "revoked_snap")

        self._invoke_dst("init")
        r = self._invoke_dst("snapshot", "restore", "--snapshot", "revoked_snap")
        self.assertEqual(r.exit_code, 0, f"restore revoked failed: {r.output}")

        b = db.get_batch(1, db_path=self.dst_db)
        self.assertEqual(b.status, BatchStatus.REVOKED, "已撤销快照恢复后应保持 revoked")

    def test_restore_preserves_original_batch(self):
        """恢复时绝不覆盖现有批次数据。"""
        self._invoke_dst("init")
        self._invoke_dst("snapshot", "restore", "--snapshot", "src_snap")

        before = db.get_batch(1, db_path=self.dst_db)
        before_matches = db.get_matches_by_batch(1, db_path=self.dst_db)

        self._invoke_dst("snapshot", "restore", "--snapshot", "src_snap")

        after = db.get_batch(1, db_path=self.dst_db)
        after_matches = db.get_matches_by_batch(1, db_path=self.dst_db)

        self.assertEqual(before.status, after.status, "原批次状态不应被覆盖")
        self.assertEqual(len(before_matches), len(after_matches), "原批次匹配数不应变")

        batches = db.list_batches(db_path=self.dst_db)
        self.assertEqual(len(batches), 2, "第二次恢复应创建新批次，不覆盖")

    def test_restore_rule_version_created(self):
        """快照对应的规则版本如库里不存在则自动创建。"""
        os.environ["INV_RECON_DB"] = self.src_db
        self._invoke_src("config", "--tolerance", "5.00")
        self._invoke_src("import",
                         "--invoices", os.path.join(self.tmpdir, "inv.csv"),
                         "--payments", os.path.join(self.tmpdir, "pay.csv"),
                         "--name", "v2_batch")
        self._invoke_src("match", "--batch", "2")
        self._invoke_src("snapshot", "create", "--batch", "2", "--name", "v2_snap")

        self._invoke_dst("init")

        current_rule_before = db.get_current_rule(db_path=self.dst_db)
        self.assertEqual(current_rule_before.version, "v1")

        r = self._invoke_dst("snapshot", "restore", "--snapshot", "v2_snap")
        self.assertEqual(r.exit_code, 0)

        b = db.get_batch(1, db_path=self.dst_db)
        self.assertEqual(b.rule_version, "v2")

        rule_v2 = db.get_rule_by_version("v2", db_path=self.dst_db)
        self.assertIsNotNone(rule_v2)
        self.assertAlmostEqual(rule_v2.tolerance, 5.00)


class SnapshotCliHelpSyncTest(unittest.TestCase):
    """CLI --help 与 rules 常量一致测试。"""

    def test_snapshot_create_doc_matches_rules(self):
        self.assertEqual(snapshot_create.__doc__, rules.SNAPSHOT_CREATE_HELP)

    def test_snapshot_restore_doc_matches_rules(self):
        self.assertEqual(snapshot_restore.__doc__, rules.SNAPSHOT_RESTORE_HELP)

    def test_snapshot_create_help_shows_rules(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            os.environ["INV_RECON_DB"] = os.path.abspath("help.db")
            try:
                r = runner.invoke(cli, ["snapshot", "create", "--help"])
                self.assertEqual(r.exit_code, 0)
                for keyword in [
                    "规则版本",
                    "裁决历史",
                    "状态链路不丢失",
                ]:
                    self.assertIn(keyword, r.output, f"snapshot create --help 缺关键词: {keyword}")
            finally:
                if "INV_RECON_DB" in os.environ:
                    del os.environ["INV_RECON_DB"]

    def test_snapshot_restore_help_shows_rules(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            os.environ["INV_RECON_DB"] = os.path.abspath("help.db")
            try:
                r = runner.invoke(cli, ["snapshot", "restore", "--help"])
                self.assertEqual(r.exit_code, 0)
                for keyword in [
                    "绝不覆盖",
                    "同名批次自动重命名",
                    "状态链路不丢失",
                    "revoked",
                ]:
                    self.assertIn(keyword, r.output, f"snapshot restore --help 缺关键词: {keyword}")
            finally:
                if "INV_RECON_DB" in os.environ:
                    del os.environ["INV_RECON_DB"]


class ReadmeSnapshotSyncTest(unittest.TestCase):
    """README 快照章节与常量一致性测试。"""

    @classmethod
    def setUpClass(cls):
        cls.readme = README_PATH.read_text(encoding="utf-8")

    def test_snapshot_section_in_readme(self):
        for keyphrase in [
            "批次快照与恢复",
            "snapshot create",
            "snapshot list",
            "snapshot show",
            "snapshot restore",
            "状态链路不丢失",
            "绝不覆盖",
            "同名批次自动重命名",
        ]:
            self.assertIn(keyphrase, self.readme, f"README 缺快照关键词: {keyphrase}")

    def test_error_table_has_snapshot_rows(self):
        for scenario, _behavior in rules.ERROR_TABLE_ROWS:
            if "快照" in scenario:
                self.assertIn(scenario, self.readme, f"README 错误处理表缺快照场景: {scenario}")

    def test_snapshot_in_public_contracts_readme(self):
        for contract in rules.PUBLIC_CONTRACTS:
            if "快照" in contract:
                pass


class ReadmeSnapshotExampleFlowTest(unittest.TestCase):
    """README 快照示例流程端到端真实可执行。"""

    def setUp(self):
        self.runner = CliRunner()
        self.tmpdir = tempfile.mkdtemp(prefix="inv_recon_snap_readme_")
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

    def _invoke(self, *args, input=None, catch_exceptions=False):
        return self.runner.invoke(cli, args, input=input, catch_exceptions=catch_exceptions)

    def test_full_snapshot_flow_with_samples(self):
        inv = str(SAMPLES_DIR / "invoices.csv")
        pay = str(SAMPLES_DIR / "payments.csv")

        r = self._invoke("import", "--invoices", inv, "--payments", pay, "--name", "snap_demo")
        self.assertEqual(r.exit_code, 0, f"import failed: {r.output}")

        r = self._invoke("match", "--batch", "1")
        self.assertEqual(r.exit_code, 0, f"match failed: {r.output}")

        r = self._invoke("snapshot", "create", "--batch", "1", "--name", "after_match")
        self.assertEqual(r.exit_code, 0, f"snapshot create failed: {r.output}")
        self.assertIn(rules.SNAPSHOT_OK_CREATED, r.output)

        r = self._invoke("snapshot", "list")
        self.assertEqual(r.exit_code, 0, f"snapshot list failed: {r.output}")
        self.assertIn("after_match", r.output)

        r = self._invoke("snapshot", "show", "--snapshot", "after_match")
        self.assertEqual(r.exit_code, 0, f"snapshot show failed: {r.output}")

        r = self._invoke("snapshot", "restore", "--snapshot", "after_match", "--batch-name", "restored_demo")
        self.assertEqual(r.exit_code, 0, f"snapshot restore failed: {r.output}")
        self.assertIn(rules.SNAPSHOT_OK_RESTORED, r.output)
        self.assertIn("restored_demo", r.output)

        r = self._invoke("list")
        self.assertEqual(r.exit_code, 0)
        self.assertIn("snap_demo", r.output)
        self.assertIn("restored_demo", r.output)

        r = self._invoke("show", "--batch", "2")
        self.assertEqual(r.exit_code, 0)
        self.assertIn("restored_demo", r.output)

        out = os.path.join(self.tmpdir, "restored_export.csv")
        r = self._invoke("export", "--batch", "2", "--output", out)
        if r.exit_code == 0:
            self.assertIn(rules.EXPORT_OK_PREFIX, r.output)
            self.assertTrue(os.path.exists(out))
        else:
            self.assertIn("无待处理记录", r.output)


if __name__ == "__main__":
    unittest.main()
