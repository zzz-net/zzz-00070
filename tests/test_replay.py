# -*- coding: utf-8 -*-
"""
任务回放与证据包测试 —— 覆盖：
1. 跨重启查询回放记录
2. 配置切换后回放行为变化
3. 非法配置被拒绝（明细采集、脱敏字段、保留天数）
4. 导入导出冲突（重复批次、版本不兼容、缺少文件）
5. 权限失败（目录不可写、文件已存在）
6. 撤销后回看（undo 前后状态）
7. 异常日志可追踪（error_traceback、error_type）
8. 按动作/结果/时间/操作者/批次筛选
9. 证据包格式（JSON/CSV/ZIP）正确性
10. 原子写入：失败不留下半截产物
11. 脱敏字段生效
12. 只读目录导入失败
"""

import os
import sys
import csv
import json
import stat
import tempfile
import shutil
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from click.testing import CliRunner

from invoice_recon.cli import cli
from invoice_recon import db, replay


SAMPLES_DIR = Path(__file__).parent.parent / "samples"


class CrossRestartReplayTest(unittest.TestCase):
    """跨重启查询回放记录。"""

    def setUp(self):
        self.runner = CliRunner()
        self.tmpdir = tempfile.mkdtemp(prefix="inv_recon_replay_restart_")
        self.db_path = os.path.join(self.tmpdir, "test.db")
        os.environ["INV_RECON_DB"] = self.db_path
        self._invoke("init")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        for env_var in ("INV_RECON_DB",):
            if env_var in os.environ:
                del os.environ[env_var]

    def _invoke(self, *args, **kwargs):
        return self.runner.invoke(cli, args, **kwargs)

    def test_replay_survives_restart(self):
        """跨重启后回放记录仍可查询。"""
        r = self._invoke("replay", "start", "--name", "restart_test",
                         "--description", "测试跨重启")
        self.assertEqual(r.exit_code, 0, f"start failed: {r.output}")

        r2 = self._invoke("replay", "list")
        self.assertEqual(r2.exit_code, 0)
        self.assertIn("restart_test", r2.output)

        del os.environ["INV_RECON_DB"]
        os.environ["INV_RECON_DB"] = self.db_path

        r3 = self._invoke("replay", "list")
        self.assertEqual(r3.exit_code, 0)
        self.assertIn("restart_test", r3.output)

    def test_replay_detail_survives_restart(self):
        """跨重启后回放详情仍可查看。"""
        self._invoke("replay", "start", "--name", "detail_restart",
                     "--operator", "tester")

        sessions = replay.list_replay_sessions(db_path=self.db_path)
        self.assertGreater(len(sessions), 0)
        sid = sessions[0]["id"]

        replay.add_replay_step(
            session_id=sid,
            action="test_action",
            description="测试步骤",
            result="success",
            detail={"key": "value"},
            db_path=self.db_path,
        )
        replay.finish_replay_session(sid, result="success", db_path=self.db_path)

        del os.environ["INV_RECON_DB"]
        os.environ["INV_RECON_DB"] = self.db_path

        session = replay.get_replay_session(sid, db_path=self.db_path)
        self.assertIsNotNone(session)
        self.assertEqual(session["name"], "detail_restart")

        steps = replay.get_replay_steps(sid, db_path=self.db_path)
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["action"], "test_action")
        self.assertEqual(steps[0]["detail"]["key"], "value")


class ReplayConfigTest(unittest.TestCase):
    """回放配置变化。"""

    def setUp(self):
        self.runner = CliRunner()
        self.tmpdir = tempfile.mkdtemp(prefix="inv_recon_replay_config_")
        self.db_path = os.path.join(self.tmpdir, "test.db")
        os.environ["INV_RECON_DB"] = self.db_path
        self._invoke("init")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        for env_var in ("INV_RECON_DB",):
            if env_var in os.environ:
                del os.environ[env_var]

    def _invoke(self, *args, **kwargs):
        return self.runner.invoke(cli, args, **kwargs)

    def test_default_config(self):
        """默认回放配置正确。"""
        r = self._invoke("replay", "config")
        self.assertEqual(r.exit_code, 0)
        self.assertIn("365", r.output)
        self.assertIn("是", r.output)

    def test_change_retention_days(self):
        """修改保留天数后配置生效。"""
        r = self._invoke("replay", "config", "--retention-days", "90")
        self.assertEqual(r.exit_code, 0)
        self.assertIn("90", r.output)

        r2 = self._invoke("replay", "config")
        self.assertEqual(r2.exit_code, 0)
        self.assertIn("90", r2.output)

    def test_change_detail_enabled(self):
        """修改明细采集开关后配置生效。"""
        r = self._invoke("replay", "config", "--no-detail")
        self.assertEqual(r.exit_code, 0)
        self.assertIn("否", r.output)

        r2 = self._invoke("replay", "config")
        self.assertEqual(r2.exit_code, 0)
        self.assertIn("否", r2.output)

    def test_change_masked_fields(self):
        """修改脱敏字段后配置生效。"""
        r = self._invoke("replay", "config",
                         "--masked-fields", "password,secret_key")
        self.assertEqual(r.exit_code, 0)
        self.assertIn("password", r.output)
        self.assertIn("secret_key", r.output)

    def test_invalid_retention_days_rejected(self):
        """非法保留天数被拒绝。"""
        r = self._invoke("replay", "config", "--retention-days", "-1")
        self.assertNotEqual(r.exit_code, 0)

    def test_detail_mode_affects_detail(self):
        """detail 关闭后回放记录不含完整详情。"""
        self._invoke("replay", "config", "--no-detail")

        r = self._invoke("replay", "start", "--name", "no_detail_test",
                         "--input", '{"password": "secret123", "name": "test"}')
        self.assertEqual(r.exit_code, 0)

        sessions = replay.list_replay_sessions(db_path=self.db_path)
        self.assertGreater(len(sessions), 0)
        session = sessions[0]
        self.assertIsNone(session.get("input_summary"))

    def test_config_validation_errors_clearly(self):
        """非法配置被拦住并写清原因。"""
        try:
            replay.set_replay_config(
                retention_days=-5,
                detail_enabled="not_bool",
                db_path=self.db_path,
            )
            self.fail("应该抛出 ValueError")
        except ValueError as e:
            self.assertIn("retention_days", str(e))
            self.assertIn("detail_enabled", str(e))


class ReplayFilterTest(unittest.TestCase):
    """回放筛选功能。"""

    def setUp(self):
        self.runner = CliRunner()
        self.tmpdir = tempfile.mkdtemp(prefix="inv_recon_replay_filter_")
        self.db_path = os.path.join(self.tmpdir, "test.db")
        os.environ["INV_RECON_DB"] = self.db_path
        self._invoke("init")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        for env_var in ("INV_RECON_DB",):
            if env_var in os.environ:
                del os.environ[env_var]

    def _invoke(self, *args, **kwargs):
        return self.runner.invoke(cli, args, **kwargs)

    def test_filter_by_batch(self):
        """按批次 ID 筛选回放会话。"""
        replay.start_replay_session(
            name="batch_filter", batch_id=1, db_path=self.db_path
        )
        replay.start_replay_session(
            name="no_batch", batch_id=None, db_path=self.db_path
        )

        r = self._invoke("replay", "list", "--batch", "1")
        self.assertEqual(r.exit_code, 0)
        self.assertIn("batch_filter", r.output)
        self.assertNotIn("no_batch", r.output)

    def test_filter_by_operator(self):
        """按操作者筛选回放会话。"""
        replay.start_replay_session(
            name="op1", operator="alice", db_path=self.db_path
        )
        replay.start_replay_session(
            name="op2", operator="bob", db_path=self.db_path
        )

        r = self._invoke("replay", "list", "--operator", "alice")
        self.assertEqual(r.exit_code, 0)
        self.assertIn("op1", r.output)
        self.assertNotIn("op2", r.output)

    def test_filter_by_result(self):
        """按结果筛选回放会话。"""
        s1 = replay.start_replay_session(name="success_sess", db_path=self.db_path)
        replay.finish_replay_session(s1["id"], "success", db_path=self.db_path)

        s2 = replay.start_replay_session(name="failure_sess", db_path=self.db_path)
        replay.finish_replay_session(s2["id"], "failure", db_path=self.db_path)

        r = self._invoke("replay", "list", "--result", "success")
        self.assertEqual(r.exit_code, 0)
        self.assertIn("success_sess", r.output)
        self.assertNotIn("failure_sess", r.output)

    def test_filter_by_action(self):
        """按包含的步骤动作筛选回放会话。"""
        s1 = replay.start_replay_session(name="action_test1", db_path=self.db_path)
        replay.add_replay_step(s1["id"], "import", "导入步骤", db_path=self.db_path)
        replay.finish_replay_session(s1["id"], "success", db_path=self.db_path)

        s2 = replay.start_replay_session(name="action_test2", db_path=self.db_path)
        replay.add_replay_step(s2["id"], "export", "导出步骤", db_path=self.db_path)
        replay.finish_replay_session(s2["id"], "success", db_path=self.db_path)

        r = self._invoke("replay", "list", "--action", "import")
        self.assertEqual(r.exit_code, 0)
        self.assertIn("action_test1", r.output)
        self.assertNotIn("action_test2", r.output)

    def test_filter_by_time_range(self):
        """按时间范围筛选回放会话。"""
        s = replay.start_replay_session(name="time_test", db_path=self.db_path)
        replay.finish_replay_session(s["id"], "success", db_path=self.db_path)

        r = self._invoke("replay", "list",
                         "--from", "2020-01-01", "--to", "2030-12-31")
        self.assertEqual(r.exit_code, 0)
        self.assertIn("time_test", r.output)

    def test_filter_by_batch_name(self):
        """按批次名筛选回放会话。"""
        replay.start_replay_session(
            name="bn_test1", batch_name="alpha_batch", db_path=self.db_path
        )
        replay.start_replay_session(
            name="bn_test2", batch_name="beta_batch", db_path=self.db_path
        )

        sessions = replay.list_replay_sessions(
            batch_name="alpha", db_path=self.db_path
        )
        self.assertEqual(len(sessions), 1)
        self.assertIn("alpha", sessions[0]["batch_name"])


class UndoReplayLookbackTest(unittest.TestCase):
    """撤销后回放回看。"""

    def setUp(self):
        self.runner = CliRunner()
        self.tmpdir = tempfile.mkdtemp(prefix="inv_recon_replay_undo_")
        self.db_path = os.path.join(self.tmpdir, "test.db")
        os.environ["INV_RECON_DB"] = self.db_path
        self._invoke("init")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        for env_var in ("INV_RECON_DB",):
            if env_var in os.environ:
                del os.environ[env_var]

    def _invoke(self, *args, **kwargs):
        return self.runner.invoke(cli, args, **kwargs)

    def test_undo_replay_session(self):
        """撤销回放会话。"""
        s = replay.start_replay_session(
            name="undo_test", description="测试撤销", db_path=self.db_path
        )
        replay.add_replay_step(
            s["id"], "step1", "第一步", db_path=self.db_path
        )

        r = self._invoke("replay", "undo", str(s["id"]), "--note", "撤销测试")
        self.assertEqual(r.exit_code, 0)
        self.assertIn("已撤销", r.output)

        session = replay.get_replay_session(s["id"], db_path=self.db_path)
        self.assertEqual(session["result"], "undone")
        self.assertIsNotNone(session["undo_time"])
        self.assertEqual(session["undo_note"], "撤销测试")

    def test_undo_session_steps_still_accessible(self):
        """撤销后步骤仍可回看。"""
        s = replay.start_replay_session(
            name="lookback_test", db_path=self.db_path
        )
        replay.add_replay_step(
            s["id"], "test_action", "测试步骤",
            detail={"key": "value"}, db_path=self.db_path
        )
        replay.undo_replay_session(s["id"], db_path=self.db_path)

        steps = replay.get_replay_steps(s["id"], db_path=self.db_path)
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["action"], "test_action")
        self.assertEqual(steps[0]["detail"]["key"], "value")

    def test_undone_session_cannot_add_steps(self):
        """已撤销的会话不能添加步骤。"""
        s = replay.start_replay_session(
            name="locked_test", db_path=self.db_path
        )
        replay.undo_replay_session(s["id"], db_path=self.db_path)

        with self.assertRaises(ValueError):
            replay.add_replay_step(
                s["id"], "new_step", "不应添加", db_path=self.db_path
            )

    def test_double_undo_rejected(self):
        """重复撤销被拒绝。"""
        s = replay.start_replay_session(
            name="double_undo", db_path=self.db_path
        )
        replay.undo_replay_session(s["id"], db_path=self.db_path)

        r = self._invoke("replay", "undo", str(s["id"]))
        self.assertNotEqual(r.exit_code, 0)

    def test_undo_visible_in_list(self):
        """撤销后仍可在列表中查询到。"""
        s = replay.start_replay_session(
            name="visible_undo", db_path=self.db_path
        )
        replay.undo_replay_session(s["id"], db_path=self.db_path)

        sessions = replay.list_replay_sessions(
            result="undone", db_path=self.db_path
        )
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["name"], "visible_undo")


class ReplayExportImportTest(unittest.TestCase):
    """回放证据包导入导出。"""

    def setUp(self):
        self.runner = CliRunner()
        self.tmpdir = tempfile.mkdtemp(prefix="inv_recon_replay_export_")
        self.db_path = os.path.join(self.tmpdir, "test.db")
        os.environ["INV_RECON_DB"] = self.db_path
        self._invoke("init")

        self._create_test_sessions()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        for env_var in ("INV_RECON_DB",):
            if env_var in os.environ:
                del os.environ[env_var]

    def _invoke(self, *args, **kwargs):
        return self.runner.invoke(cli, args, **kwargs)

    def _create_test_sessions(self):
        s1 = replay.start_replay_session(
            name="export_test_1", operator="alice",
            batch_id=1, batch_name="batch_alpha",
            input_summary={"param": "value1"},
            db_path=self.db_path,
        )
        replay.add_replay_step(
            s1["id"], "import", "导入数据",
            detail={"records": 100}, db_path=self.db_path
        )
        replay.add_replay_step(
            s1["id"], "match", "执行匹配",
            result="success", detail={"matches": 50}, db_path=self.db_path
        )
        replay.finish_replay_session(
            s1["id"], result="success", db_path=self.db_path
        )

        s2 = replay.start_replay_session(
            name="export_test_2", operator="bob",
            db_path=self.db_path,
        )
        replay.add_replay_step(
            s2["id"], "export", "导出结果",
            result="failure", error_message="导出失败",
            db_path=self.db_path,
        )
        replay.finish_replay_session(
            s2["id"], result="failure", db_path=self.db_path
        )

    def test_export_json(self):
        """导出 JSON 格式证据包。"""
        out = os.path.join(self.tmpdir, "replay.json")
        r = self._invoke("replay", "export", "--output", out, "--format", "json")
        self.assertEqual(r.exit_code, 0, f"export failed: {r.output}")
        self.assertTrue(os.path.exists(out))

        with open(out, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertIn("sessions", data)
        self.assertIn("steps", data)
        self.assertGreater(len(data["sessions"]), 0)
        self.assertGreater(len(data["steps"]), 0)

    def test_export_csv(self):
        """导出 CSV 格式证据包。"""
        out = os.path.join(self.tmpdir, "replay.csv")
        r = self._invoke("replay", "export", "--output", out, "--format", "csv")
        self.assertEqual(r.exit_code, 0, f"export failed: {r.output}")
        self.assertTrue(os.path.exists(out))

        with open(out, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        self.assertGreater(len(rows), 0)
        self.assertIn("type", rows[0])

    def test_export_zip(self):
        """导出 ZIP 压缩包格式证据包。"""
        out = os.path.join(self.tmpdir, "replay.reppkg")
        r = self._invoke("replay", "export", "--output", out, "--format", "zip")
        self.assertEqual(r.exit_code, 0, f"export failed: {r.output}")
        self.assertTrue(os.path.exists(out))

        with zipfile.ZipFile(out, "r") as zf:
            namelist = zf.namelist()
            self.assertIn("manifest.json", namelist)
            self.assertIn("sessions.json", namelist)
            self.assertIn("steps.json", namelist)
            self.assertIn("checksums.json", namelist)

    def test_export_file_exists_error(self):
        """目标文件已存在时报错不覆盖。"""
        out = os.path.join(self.tmpdir, "existing.json")
        with open(out, "w", encoding="utf-8") as f:
            f.write("old data")

        r = self._invoke("replay", "export", "--output", out, "--format", "json")
        self.assertNotEqual(r.exit_code, 0)
        self.assertIn("已存在", r.output)

        with open(out, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), "old data")

    def test_export_dir_not_found(self):
        """目标目录不存在时报错。"""
        out = os.path.join(self.tmpdir, "nonexistent", "replay.json")
        r = self._invoke("replay", "export", "--output", out, "--format", "json")
        self.assertNotEqual(r.exit_code, 0)
        self.assertIn("不存在", r.output)

    def test_export_no_partial_file(self):
        """导出失败不产生半截文件。"""
        out = os.path.join(self.tmpdir, "partial_test.json")
        with open(out, "w", encoding="utf-8") as f:
            f.write("ORIGINAL_CONTENT")

        r = self._invoke("replay", "export", "--output", out, "--format", "json")
        self.assertNotEqual(r.exit_code, 0)

        with open(out, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), "ORIGINAL_CONTENT")

    def test_export_with_filter(self):
        """带筛选条件导出。"""
        out = os.path.join(self.tmpdir, "filtered.json")
        r = self._invoke("replay", "export", "--output", out,
                         "--format", "json", "--operator", "alice")
        self.assertEqual(r.exit_code, 0)

        with open(out, "r", encoding="utf-8") as f:
            data = json.load(f)
        for s in data["sessions"]:
            self.assertEqual(s["operator"], "alice")

    def test_import_json_package(self):
        """导入 JSON 证据包。"""
        out = os.path.join(self.tmpdir, "export.json")
        replay.export_replay_package(out, fmt="json", db_path=self.db_path)

        other_db = os.path.join(self.tmpdir, "other.db")
        os.environ["INV_RECON_DB"] = other_db
        try:
            self._invoke("init")
            r = self._invoke("replay", "import", "--input", out)
            self.assertEqual(r.exit_code, 0, f"import failed: {r.output}")
            self.assertIn("已导入", r.output)

            sessions = replay.list_replay_sessions(db_path=other_db)
            self.assertGreater(len(sessions), 0)
        finally:
            os.environ["INV_RECON_DB"] = self.db_path

    def test_import_zip_package(self):
        """导入 ZIP 证据包。"""
        out = os.path.join(self.tmpdir, "export.reppkg")
        replay.export_replay_package(out, fmt="zip", db_path=self.db_path)

        other_db = os.path.join(self.tmpdir, "other2.db")
        os.environ["INV_RECON_DB"] = other_db
        try:
            self._invoke("init")
            r = self._invoke("replay", "import", "--input", out)
            self.assertEqual(r.exit_code, 0, f"import failed: {r.output}")

            sessions = replay.list_replay_sessions(db_path=other_db)
            self.assertGreater(len(sessions), 0)

            steps = []
            for s in sessions:
                steps.extend(replay.get_replay_steps(s["id"], db_path=other_db))
            self.assertGreater(len(steps), 0)
        finally:
            os.environ["INV_RECON_DB"] = self.db_path

    def test_import_duplicate_skipped(self):
        """重复会话默认跳过。"""
        out = os.path.join(self.tmpdir, "dup_export.json")
        replay.export_replay_package(out, fmt="json", db_path=self.db_path)

        r = self._invoke("replay", "import", "--input", out)
        self.assertEqual(r.exit_code, 0)
        self.assertIn("跳过", r.output)

    def test_import_duplicate_force(self):
        """重复会话使用 --force 覆盖。"""
        out = os.path.join(self.tmpdir, "force_export.json")
        replay.export_replay_package(out, fmt="json", db_path=self.db_path)

        before = replay.list_replay_sessions(db_path=self.db_path)
        before_count = len(before)

        r = self._invoke("replay", "import", "--input", out, "--force")
        self.assertEqual(r.exit_code, 0)

        after = replay.list_replay_sessions(db_path=self.db_path)
        self.assertEqual(len(after), before_count)

    def test_import_missing_file_error(self):
        """导入不存在的文件报错。"""
        r = self._invoke("replay", "import",
                         "--input", os.path.join(self.tmpdir, "nonexistent.json"))
        self.assertNotEqual(r.exit_code, 0)

    def test_import_invalid_json_error(self):
        """导入无效 JSON 报错。"""
        bad_file = os.path.join(self.tmpdir, "bad.json")
        with open(bad_file, "w", encoding="utf-8") as f:
            f.write("not valid json {")

        r = self._invoke("replay", "import", "--input", bad_file)
        self.assertNotEqual(r.exit_code, 0)

    def test_import_version_incompatible(self):
        """版本不兼容时报错，--force 可强制。"""
        out = os.path.join(self.tmpdir, "version_test.json")
        with open(out, "w", encoding="utf-8") as f:
            json.dump({
                "schema_version": 999,
                "sessions": [],
                "steps": [],
            }, f)

        r = self._invoke("replay", "import", "--input", out)
        self.assertNotEqual(r.exit_code, 0)
        self.assertIn("版本不兼容", r.output)

        r2 = self._invoke("replay", "import", "--input", out, "--force")
        self.assertEqual(r2.exit_code, 0)

    def test_verify_package(self):
        """校验证据包。"""
        out = os.path.join(self.tmpdir, "verify_test.reppkg")
        replay.export_replay_package(out, fmt="zip", db_path=self.db_path)

        r = self._invoke("replay", "verify", "--input", out)
        self.assertEqual(r.exit_code, 0)
        self.assertIn("校验通过", r.output)

    def test_verify_bad_zip(self):
        """校验无效 ZIP 报错。"""
        bad_file = os.path.join(self.tmpdir, "bad.zip")
        with open(bad_file, "w", encoding="utf-8") as f:
            f.write("not a zip file")

        r = self._invoke("replay", "verify", "--input", bad_file)
        self.assertNotEqual(r.exit_code, 0)


class ReplayPermissionTest(unittest.TestCase):
    """权限相关测试。"""

    def setUp(self):
        self.runner = CliRunner()
        self.tmpdir = tempfile.mkdtemp(prefix="inv_recon_replay_perm_")
        self.db_path = os.path.join(self.tmpdir, "test.db")
        os.environ["INV_RECON_DB"] = self.db_path
        self._invoke("init")

    def tearDown(self):
        for d in getattr(self, '_readonly_dirs', []):
            try:
                os.chmod(d, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
            except OSError:
                pass
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        for env_var in ("INV_RECON_DB",):
            if env_var in os.environ:
                del os.environ[env_var]

    def _invoke(self, *args, **kwargs):
        return self.runner.invoke(cli, args, **kwargs)

    @unittest.skipIf(os.name == "nt", "Windows 不可写目录检测需要特殊权限设置")
    def test_export_to_unwritable_dir(self):
        """导出到不可写目录报错。"""
        readonly_dir = os.path.join(self.tmpdir, "readonly")
        os.makedirs(readonly_dir, exist_ok=True)
        os.chmod(readonly_dir, stat.S_IREAD | stat.S_IEXEC)
        self._readonly_dirs = [readonly_dir]

        out = os.path.join(readonly_dir, "replay.json")
        r = self._invoke("replay", "export", "--output", out, "--format", "json")
        self.assertNotEqual(r.exit_code, 0)


class ReplayExceptionTraceTest(unittest.TestCase):
    """异常日志追踪测试。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="inv_recon_replay_exc_")
        self.db_path = os.path.join(self.tmpdir, "test.db")
        db.init_db(self.db_path)
        replay.init_replay_db(self.db_path)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_exception_in_replay_step(self):
        """异常对象被记录到回放步骤中。"""
        s = replay.start_replay_session(
            name="exc_test", db_path=self.db_path
        )

        try:
            raise ValueError("测试异常信息")
        except ValueError as e:
            step_id = replay.add_replay_step(
                session_id=s["id"],
                action="test_action",
                result="error",
                exception=e,
                db_path=self.db_path,
            )

        steps = replay.get_replay_steps(s["id"], db_path=self.db_path)
        self.assertEqual(len(steps), 1)
        step = steps[0]
        self.assertEqual(step["result"], "error")
        self.assertEqual(step["error_message"], "测试异常信息")
        detail = step.get("detail", {})
        self.assertEqual(detail.get("error_type"), "ValueError")
        self.assertIn("error_traceback", detail)
        self.assertIn("测试异常信息", detail["error_traceback"])

    def test_error_session_finish(self):
        """错误的会话结果可记录。"""
        s = replay.start_replay_session(
            name="error_sess", db_path=self.db_path
        )
        finished = replay.finish_replay_session(
            s["id"], result="error",
            error_message="整体失败",
            db_path=self.db_path,
        )
        self.assertEqual(finished["result"], "error")
        self.assertEqual(finished["error_message"], "整体失败")


class ReplayMaskedFieldsTest(unittest.TestCase):
    """脱敏字段测试。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="inv_recon_replay_mask_")
        self.db_path = os.path.join(self.tmpdir, "test.db")
        db.init_db(self.db_path)
        replay.init_replay_db(self.db_path)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_masked_fields_hide_sensitive_data(self):
        """脱敏字段会隐藏敏感数据。"""
        replay.set_replay_config(
            masked_fields=["password", "secret_key"],
            db_path=self.db_path,
        )

        s = replay.start_replay_session(
            name="mask_test",
            input_summary={
                "username": "alice",
                "password": "super_secret",
                "nested": {"secret_key": "abc123", "visible": "yes"},
            },
            db_path=self.db_path,
        )

        session = replay.get_replay_session(s["id"], db_path=self.db_path)
        summary = session.get("input_summary", {})
        self.assertEqual(summary["username"], "alice")
        self.assertEqual(summary["password"], "***MASKED***")
        self.assertEqual(summary["nested"]["secret_key"], "***MASKED***")
        self.assertEqual(summary["nested"]["visible"], "yes")

    def test_masked_fields_in_steps(self):
        """步骤详情中的脱敏字段也会被隐藏。"""
        replay.set_replay_config(
            masked_fields=["token"],
            db_path=self.db_path,
        )

        s = replay.start_replay_session(
            name="mask_step_test", db_path=self.db_path
        )
        replay.add_replay_step(
            s["id"], "login", "登录步骤",
            detail={"user": "bob", "token": "secret_token_123"},
            db_path=self.db_path,
        )

        steps = replay.get_replay_steps(s["id"], db_path=self.db_path)
        detail = steps[0]["detail"]
        self.assertEqual(detail["user"], "bob")
        self.assertEqual(detail["token"], "***MASKED***")


class ReplayModuleDirectTest(unittest.TestCase):
    """回放模块直接 API 测试。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="inv_recon_replay_api_")
        self.db_path = os.path.join(self.tmpdir, "test.db")
        db.init_db(self.db_path)
        replay.init_replay_db(self.db_path)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_start_and_get_session(self):
        """开始并获取回放会话。"""
        s = replay.start_replay_session(
            name="api_test",
            description="API 测试",
            operator="tester",
            batch_id=1,
            batch_name="test_batch",
            input_summary={"key": "value"},
            db_path=self.db_path,
        )
        self.assertGreater(s["id"], 0)
        self.assertEqual(s["name"], "api_test")
        self.assertEqual(s["result"], "running")

    def test_add_and_list_steps(self):
        """添加并列出步骤。"""
        s = replay.start_replay_session(
            name="step_test", db_path=self.db_path
        )

        id1 = replay.add_replay_step(
            s["id"], "step1", "第一步", db_path=self.db_path
        )
        id2 = replay.add_replay_step(
            s["id"], "step2", "第二步",
            detail={"data": "test"}, db_path=self.db_path
        )

        self.assertGreater(id1, 0)
        self.assertGreater(id2, 0)

        steps = replay.get_replay_steps(s["id"], db_path=self.db_path)
        self.assertEqual(len(steps), 2)
        self.assertEqual(steps[0]["step_index"], 1)
        self.assertEqual(steps[1]["step_index"], 2)

    def test_invalid_step_result_rejected(self):
        """非法步骤结果被拒绝。"""
        s = replay.start_replay_session(
            name="invalid_result", db_path=self.db_path
        )
        with self.assertRaises(ValueError):
            replay.add_replay_step(
                s["id"], "test", "测试",
                result="invalid_result", db_path=self.db_path
            )

    def test_nonexistent_session_rejected(self):
        """不存在的会话操作被拒绝。"""
        with self.assertRaises(ValueError):
            replay.add_replay_step(
                99999, "test", "测试", db_path=self.db_path
            )

    def test_get_session_by_key(self):
        """通过 session_key 获取会话。"""
        s = replay.start_replay_session(
            name="key_test", db_path=self.db_path
        )
        key = s["session_key"]

        found = replay.get_replay_session_by_key(key, db_path=self.db_path)
        self.assertIsNotNone(found)
        self.assertEqual(found["id"], s["id"])

    def test_cleanup_retention(self):
        """保留天数清理。"""
        deleted = replay.cleanup_replay(
            retention_days=0, db_path=self.db_path
        )
        self.assertEqual(deleted, 0)

    def test_config_zero_retention(self):
        """零保留天数允许（永久保留）。"""
        config = replay.set_replay_config(
            retention_days=0, db_path=self.db_path
        )
        self.assertEqual(config["retention_days"], 0)

    def test_config_invalid_masked_fields(self):
        """非法脱敏字段被拒绝。"""
        with self.assertRaises(ValueError):
            replay.set_replay_config(
                masked_fields=["", "valid"], db_path=self.db_path
            )

    def test_session_count_limit(self):
        """查询条数限制。"""
        for i in range(10):
            replay.start_replay_session(
                name=f"limit_{i}", db_path=self.db_path
            )

        sessions = replay.list_replay_sessions(
            limit=3, db_path=self.db_path
        )
        self.assertEqual(len(sessions), 3)


if __name__ == "__main__":
    unittest.main()
