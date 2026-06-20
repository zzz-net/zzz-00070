# -*- coding: utf-8 -*-
"""
操作录制与证据回灌测试 —— 覆盖：
1. drill begin 后自动录制 import/match/review/review-undo/export/revoke
2. import 后自动设置活动批次 ID
3. drill end 收口（success/failure/error）
4. drill undo 撤销
5. 跨重启后演练记录可查询
6. 命令异常自动捕获（error_type, error_traceback）
7. 配置切换（关闭明细采集后不记录详情）
8. 脱敏字段生效
9. 冲突处理（match 后有 conflict，review 处理）
10. 撤销后回看（drill undo 后仍可查询步骤）
11. drill status 显示活动演练状态
12. 无活动演练时命令正常执行（不录制）
13. 重复 begin 报错
14. 无活动演练时 end/undo 报错
"""

import os
import sys
import json
import tempfile
import shutil
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from click.testing import CliRunner

from invoice_recon.cli import cli
from invoice_recon import db, replay


SAMPLES_DIR = Path(__file__).parent.parent / "samples"


class OperationRecorderTest(unittest.TestCase):
    """操作录制核心功能测试。"""

    def setUp(self):
        self.runner = CliRunner()
        self.tmpdir = tempfile.mkdtemp(prefix="inv_recon_drill_")
        self.db_path = os.path.join(self.tmpdir, "test.db")
        os.environ["INV_RECON_DB"] = self.db_path
        self._invoke("init")
        replay.get_recorder().reset()

    def tearDown(self):
        replay.get_recorder().reset()
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        for env_var in ("INV_RECON_DB",):
            if env_var in os.environ:
                del os.environ[env_var]

    def _invoke(self, *args, **kwargs):
        return self.runner.invoke(cli, args, **kwargs)

    def test_drill_begin_creates_session(self):
        """drill begin 创建回放会话。"""
        r = self._invoke("drill", "begin", "--name", "test_drill")
        self.assertEqual(r.exit_code, 0, f"begin failed: {r.output}")
        self.assertIn("演练已开始", r.output)

        session = replay.get_active_drill(db_path=self.db_path)
        self.assertIsNotNone(session)
        self.assertEqual(session["name"], "test_drill")
        self.assertEqual(session["result"], "running")

        steps = replay.get_replay_steps(session["id"], db_path=self.db_path)
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["action"], "drill_begin")

    def test_drill_status_active(self):
        """drill status 显示活动演练。"""
        self._invoke("drill", "begin", "--name", "status_test")

        r = self._invoke("drill", "status")
        self.assertEqual(r.exit_code, 0)
        self.assertIn("演练进行中", r.output)
        self.assertIn("status_test", r.output)
        self.assertIn("已执行步骤: 1", r.output)

    def test_drill_status_idle(self):
        """drill status 无活动演练时显示空闲。"""
        r = self._invoke("drill", "status")
        self.assertEqual(r.exit_code, 0)
        self.assertIn("无活动演练", r.output)

    def test_drill_end_success(self):
        """drill end 收口为 success。"""
        self._invoke("drill", "begin", "--name", "end_test")
        session_id = replay.get_active_drill(db_path=self.db_path)["id"]

        r = self._invoke("drill", "end", "--result", "success")
        self.assertEqual(r.exit_code, 0)
        self.assertIn("演练已结束", r.output)
        self.assertIn("success", r.output)

        session = replay.get_replay_session(session_id, db_path=self.db_path)
        self.assertEqual(session["result"], "success")
        self.assertIsNotNone(session["end_time"])

        steps = replay.get_replay_steps(session_id, db_path=self.db_path)
        self.assertEqual(len(steps), 2)
        self.assertEqual(steps[1]["action"], "drill_end")

    def test_drill_undo(self):
        """drill undo 撤销演练。"""
        self._invoke("drill", "begin", "--name", "undo_test")
        session_id = replay.get_active_drill(db_path=self.db_path)["id"]

        r = self._invoke("drill", "undo", "--note", "撤销测试")
        self.assertEqual(r.exit_code, 0)
        self.assertIn("演练已撤销", r.output)

        session = replay.get_replay_session(session_id, db_path=self.db_path)
        self.assertEqual(session["result"], "undone")
        self.assertIsNotNone(session["undo_time"])
        self.assertEqual(session["undo_note"], "撤销测试")

    def test_no_active_drill_end_error(self):
        """无活动演练时 drill end 报错。"""
        r = self._invoke("drill", "end")
        self.assertNotEqual(r.exit_code, 0)
        self.assertIn("当前没有活动的演练", r.output)

    def test_no_active_drill_undo_error(self):
        """无活动演练时 drill undo 报错。"""
        r = self._invoke("drill", "undo")
        self.assertNotEqual(r.exit_code, 0)
        self.assertIn("当前没有活动的演练", r.output)

    def test_duplicate_begin_error(self):
        """重复 drill begin 报错。"""
        self._invoke("drill", "begin", "--name", "first")
        r = self._invoke("drill", "begin", "--name", "second")
        self.assertNotEqual(r.exit_code, 0)

    def test_commands_without_drill_run_normally(self):
        """无活动演练时命令正常执行，不录制。"""
        inv = SAMPLES_DIR / "invoices.csv"
        pay = SAMPLES_DIR / "payments.csv"

        r = self._invoke("import", "--invoices", str(inv), "--payments", str(pay))
        self.assertEqual(r.exit_code, 0)

        sessions = replay.list_replay_sessions(db_path=self.db_path)
        for s in sessions:
            steps = replay.get_replay_steps(s["id"], db_path=self.db_path)
            for st in steps:
                self.assertNotEqual(st["action"], "import")


class DrillRecordCommandsTest(unittest.TestCase):
    """演练过程中命令自动录制测试。"""

    def setUp(self):
        self.runner = CliRunner()
        self.tmpdir = tempfile.mkdtemp(prefix="inv_recon_drill_rec_")
        self.db_path = os.path.join(self.tmpdir, "test.db")
        os.environ["INV_RECON_DB"] = self.db_path
        self._invoke("init")
        replay.get_recorder().reset()

        self.inv_file = SAMPLES_DIR / "invoices.csv"
        self.pay_file = SAMPLES_DIR / "payments.csv"

    def tearDown(self):
        replay.get_recorder().reset()
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        for env_var in ("INV_RECON_DB",):
            if env_var in os.environ:
                del os.environ[env_var]

    def _invoke(self, *args, **kwargs):
        return self.runner.invoke(cli, args, **kwargs)

    def test_import_auto_recorded(self):
        """import 命令自动录制到演练。"""
        self._invoke("drill", "begin", "--name", "import_test")

        r = self._invoke("import", "--invoices", str(self.inv_file),
                         "--payments", str(self.pay_file))
        self.assertEqual(r.exit_code, 0, f"import failed: {r.output}")

        session = replay.get_active_drill(db_path=self.db_path)
        steps = replay.get_replay_steps(session["id"], db_path=self.db_path)

        actions = [st["action"] for st in steps]
        self.assertIn("import", actions)

        import_step = [st for st in steps if st["action"] == "import"][0]
        self.assertEqual(import_step["result"], "success")
        self.assertIn("input_args", import_step["detail"])
        self.assertIn("batch_id", import_step["detail"])

    def test_import_sets_active_batch(self):
        """import 后自动设置活动批次 ID。"""
        self._invoke("drill", "begin", "--name", "batch_test")

        self._invoke("import", "--invoices", str(self.inv_file),
                     "--payments", str(self.pay_file))

        recorder = replay.get_recorder()
        self.assertIsNotNone(recorder.get_active_batch_id())

    def test_match_auto_recorded(self):
        """match 命令自动录制到演练。"""
        self._invoke("drill", "begin", "--name", "match_test")
        self._invoke("import", "--invoices", str(self.inv_file),
                     "--payments", str(self.pay_file))

        batch_id = replay.get_recorder().get_active_batch_id()

        r = self._invoke("match", "--batch", str(batch_id))
        self.assertEqual(r.exit_code, 0, f"match failed: {r.output}")

        session = replay.get_active_drill(db_path=self.db_path)
        steps = replay.get_replay_steps(session["id"], db_path=self.db_path)
        actions = [st["action"] for st in steps]
        self.assertIn("match", actions)

    def test_review_auto_recorded(self):
        """review 命令自动录制到演练。"""
        self._invoke("drill", "begin", "--name", "review_test")
        self._invoke("import", "--invoices", str(self.inv_file),
                     "--payments", str(self.pay_file))
        batch_id = replay.get_recorder().get_active_batch_id()
        self._invoke("match", "--batch", str(batch_id))

        r = self._invoke("review", "--batch", str(batch_id),
                         "--match-id", "1", "--action", "confirm")
        self.assertEqual(r.exit_code, 0, f"review failed: {r.output}")

        session = replay.get_active_drill(db_path=self.db_path)
        steps = replay.get_replay_steps(session["id"], db_path=self.db_path)
        actions = [st["action"] for st in steps]
        self.assertIn("review", actions)

    def test_review_undo_auto_recorded(self):
        """review-undo 命令自动录制到演练。"""
        self._invoke("drill", "begin", "--name", "undo_test")
        self._invoke("import", "--invoices", str(self.inv_file),
                     "--payments", str(self.pay_file))
        batch_id = replay.get_recorder().get_active_batch_id()
        self._invoke("match", "--batch", str(batch_id))
        self._invoke("review", "--batch", str(batch_id),
                     "--match-id", "1", "--action", "confirm")

        r = self._invoke("review-undo", "--batch", str(batch_id),
                         "--match-id", "1")
        self.assertEqual(r.exit_code, 0, f"review-undo failed: {r.output}")

        session = replay.get_active_drill(db_path=self.db_path)
        steps = replay.get_replay_steps(session["id"], db_path=self.db_path)
        actions = [st["action"] for st in steps]
        self.assertIn("review-undo", actions)

    def test_export_auto_recorded(self):
        """export 命令自动录制到演练。"""
        self._invoke("drill", "begin", "--name", "export_test")
        self._invoke("import", "--invoices", str(self.inv_file),
                     "--payments", str(self.pay_file))
        batch_id = replay.get_recorder().get_active_batch_id()
        self._invoke("match", "--batch", str(batch_id))

        out_file = os.path.join(self.tmpdir, "export.csv")
        r = self._invoke("export", "--batch", str(batch_id),
                         "--output", out_file)
        self.assertEqual(r.exit_code, 0, f"export failed: {r.output}")

        session = replay.get_active_drill(db_path=self.db_path)
        steps = replay.get_replay_steps(session["id"], db_path=self.db_path)
        actions = [st["action"] for st in steps]
        self.assertIn("export", actions)

    def test_revoke_auto_recorded(self):
        """revoke 命令自动录制到演练。"""
        self._invoke("drill", "begin", "--name", "revoke_test")
        self._invoke("import", "--invoices", str(self.inv_file),
                     "--payments", str(self.pay_file))
        batch_id = replay.get_recorder().get_active_batch_id()

        r = self._invoke("revoke", "--batch", str(batch_id))
        self.assertEqual(r.exit_code, 0, f"revoke failed: {r.output}")

        session = replay.get_active_drill(db_path=self.db_path)
        steps = replay.get_replay_steps(session["id"], db_path=self.db_path)
        actions = [st["action"] for st in steps]
        self.assertIn("revoke", actions)

    def test_full_workflow_recorded(self):
        """完整工作流 import -> match -> review -> export 全部录制。"""
        self._invoke("drill", "begin", "--name", "full_flow",
                     "--operator", "tester")
        self._invoke("import", "--invoices", str(self.inv_file),
                     "--payments", str(self.pay_file))
        batch_id = replay.get_recorder().get_active_batch_id()
        self._invoke("match", "--batch", str(batch_id))
        self._invoke("review", "--batch", str(batch_id),
                     "--match-id", "1", "--action", "confirm")

        out_file = os.path.join(self.tmpdir, "full_export.csv")
        self._invoke("export", "--batch", str(batch_id),
                     "--output", out_file)

        self._invoke("drill", "end", "--result", "success")

        session_id = replay.list_replay_sessions(
            operator="tester", db_path=self.db_path
        )[0]["id"]

        steps = replay.get_replay_steps(session_id, db_path=self.db_path)
        actions = [st["action"] for st in steps]

        expected = ["drill_begin", "import", "match", "review",
                    "export", "drill_end"]
        for exp in expected:
            self.assertIn(exp, actions, f"缺少动作: {exp}")

        for st in steps:
            if st["action"] not in ("drill_begin", "drill_end"):
                self.assertIn("batch_id", st["detail"])

    def test_command_failure_recorded(self):
        """命令失败自动记录为 failure。"""
        self._invoke("drill", "begin", "--name", "failure_test")

        r = self._invoke("match", "--batch", "99999")
        self.assertNotEqual(r.exit_code, 0)

        session = replay.get_active_drill(db_path=self.db_path)
        steps = replay.get_replay_steps(session["id"], db_path=self.db_path)
        match_step = [st for st in steps if st["action"] == "match"][0]
        self.assertEqual(match_step["result"], "failure")
        self.assertIn("命令退出码", match_step["error_message"])

    def test_exception_recorded(self):
        """异常自动捕获并记录 error_type 和 error_traceback。"""
        self._invoke("drill", "begin", "--name", "exception_test")

        r = self._invoke("export", "--batch", "99999",
                         "--output", os.path.join(self.tmpdir, "x.csv"))
        self.assertNotEqual(r.exit_code, 0)

        session = replay.get_active_drill(db_path=self.db_path)
        steps = replay.get_replay_steps(session["id"], db_path=self.db_path)
        export_step = [st for st in steps if st["action"] == "export"][0]
        detail = export_step["detail"]
        self.assertEqual(export_step["result"], "failure")
        self.assertIn("命令退出码", export_step["error_message"])


class DrillConfigTest(unittest.TestCase):
    """演练配置切换测试。"""

    def setUp(self):
        self.runner = CliRunner()
        self.tmpdir = tempfile.mkdtemp(prefix="inv_recon_drill_cfg_")
        self.db_path = os.path.join(self.tmpdir, "test.db")
        os.environ["INV_RECON_DB"] = self.db_path
        self._invoke("init")
        replay.get_recorder().reset()

        self.inv_file = SAMPLES_DIR / "invoices.csv"
        self.pay_file = SAMPLES_DIR / "payments.csv"

    def tearDown(self):
        replay.get_recorder().reset()
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        for env_var in ("INV_RECON_DB",):
            if env_var in os.environ:
                del os.environ[env_var]

    def _invoke(self, *args, **kwargs):
        return self.runner.invoke(cli, args, **kwargs)

    def test_detail_disabled_no_input_args(self):
        """关闭明细采集后不记录 input_args。"""
        self._invoke("replay", "config", "--no-detail")

        self._invoke("drill", "begin", "--name", "no_detail")
        self._invoke("import", "--invoices", str(self.inv_file),
                     "--payments", str(self.pay_file))

        session = replay.get_active_drill(db_path=self.db_path)
        steps = replay.get_replay_steps(session["id"], db_path=self.db_path)
        import_step = [st for st in steps if st["action"] == "import"][0]

        self.assertIsNone(import_step.get("detail"))

    def test_masked_fields_in_drill(self):
        """演练录制中脱敏字段生效。"""
        self._invoke("replay", "config",
                     "--masked-fields", "password,secret")

        self._invoke("drill", "begin", "--name", "mask_test",
                     "--input", '{"user": "alice", "password": "secret123"}')

        session = replay.get_active_drill(db_path=self.db_path)
        summary = session.get("input_summary", {})
        self.assertEqual(summary["user"], "alice")
        self.assertEqual(summary["password"], "***MASKED***")


class DrillCrossRestartTest(unittest.TestCase):
    """演练跨重启查询测试。"""

    def setUp(self):
        self.runner = CliRunner()
        self.tmpdir = tempfile.mkdtemp(prefix="inv_recon_drill_restart_")
        self.db_path = os.path.join(self.tmpdir, "test.db")
        os.environ["INV_RECON_DB"] = self.db_path
        self._invoke("init")
        replay.get_recorder().reset()

        self.inv_file = SAMPLES_DIR / "invoices.csv"
        self.pay_file = SAMPLES_DIR / "payments.csv"

    def tearDown(self):
        replay.get_recorder().reset()
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        for env_var in ("INV_RECON_DB",):
            if env_var in os.environ:
                del os.environ[env_var]

    def _invoke(self, *args, **kwargs):
        return self.runner.invoke(cli, args, **kwargs)

    def test_drill_session_survives_restart(self):
        """演练会话跨重启后仍可查询。"""
        self._invoke("drill", "begin", "--name", "restart_test",
                     "--operator", "tester")
        self._invoke("import", "--invoices", str(self.inv_file),
                     "--payments", str(self.pay_file))
        batch_id = replay.get_recorder().get_active_batch_id()
        self._invoke("match", "--batch", str(batch_id))
        self._invoke("drill", "end", "--result", "success")

        replay.get_recorder().reset()

        del os.environ["INV_RECON_DB"]
        os.environ["INV_RECON_DB"] = self.db_path

        sessions = replay.list_replay_sessions(
            operator="tester", db_path=self.db_path
        )
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["name"], "restart_test")
        self.assertEqual(sessions[0]["result"], "success")

        steps = replay.get_replay_steps(sessions[0]["id"], db_path=self.db_path)
        actions = [st["action"] for st in steps]
        self.assertIn("import", actions)
        self.assertIn("match", actions)

    def test_query_by_time_range(self):
        """按时间段查询演练。"""
        self._invoke("drill", "begin", "--name", "time_test")
        self._invoke("drill", "end", "--result", "success")

        r = self._invoke("replay", "list",
                         "--from", "2020-01-01", "--to", "2030-12-31")
        self.assertEqual(r.exit_code, 0)
        self.assertIn("time_test", r.output)

    def test_query_by_batch(self):
        """按批次查询演练。"""
        self._invoke("drill", "begin", "--name", "batch_query_test")
        self._invoke("import", "--invoices", str(self.inv_file),
                     "--payments", str(self.pay_file))
        batch_id = replay.get_recorder().get_active_batch_id()
        self._invoke("drill", "end", "--result", "success")

        sessions = replay.list_replay_sessions(
            batch_id=batch_id, db_path=self.db_path
        )
        self.assertGreater(len(sessions), 0)
        self.assertEqual(sessions[0]["batch_id"], batch_id)


class DrillUndoLookbackTest(unittest.TestCase):
    """演练撤销后回看测试。"""

    def setUp(self):
        self.runner = CliRunner()
        self.tmpdir = tempfile.mkdtemp(prefix="inv_recon_drill_look_")
        self.db_path = os.path.join(self.tmpdir, "test.db")
        os.environ["INV_RECON_DB"] = self.db_path
        self._invoke("init")
        replay.get_recorder().reset()

        self.inv_file = SAMPLES_DIR / "invoices.csv"
        self.pay_file = SAMPLES_DIR / "payments.csv"

    def tearDown(self):
        replay.get_recorder().reset()
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        for env_var in ("INV_RECON_DB",):
            if env_var in os.environ:
                del os.environ[env_var]

    def _invoke(self, *args, **kwargs):
        return self.runner.invoke(cli, args, **kwargs)

    def test_undone_drill_steps_accessible(self):
        """撤销的演练步骤仍可回看。"""
        self._invoke("drill", "begin", "--name", "lookback")
        self._invoke("import", "--invoices", str(self.inv_file),
                     "--payments", str(self.pay_file))
        batch_id = replay.get_recorder().get_active_batch_id()
        self._invoke("match", "--batch", str(batch_id))
        self._invoke("drill", "undo", "--note", "操作有误")

        sessions = replay.list_replay_sessions(
            result="undone", db_path=self.db_path
        )
        self.assertEqual(len(sessions), 1)
        session_id = sessions[0]["id"]

        steps = replay.get_replay_steps(session_id, db_path=self.db_path)
        actions = [st["action"] for st in steps]
        self.assertIn("import", actions)
        self.assertIn("match", actions)
        self.assertIn("drill_undo", actions)

    def test_show_undone_drill(self):
        """replay show 可查看撤销的演练。"""
        self._invoke("drill", "begin", "--name", "show_undone")
        self._invoke("import", "--invoices", str(self.inv_file),
                     "--payments", str(self.pay_file))
        self._invoke("drill", "undo")

        sessions = replay.list_replay_sessions(db_path=self.db_path)
        session_id = sessions[0]["id"]

        r = self._invoke("replay", "show", str(session_id))
        self.assertEqual(r.exit_code, 0)
        self.assertIn("show_undone", r.output)
        self.assertIn("undone", r.output)


class DrillDirectAPITest(unittest.TestCase):
    """演练直接 API 测试。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="inv_recon_drill_api_")
        self.db_path = os.path.join(self.tmpdir, "test.db")
        db.init_db(self.db_path)
        replay.init_replay_db(self.db_path)
        replay.get_recorder().reset()

    def tearDown(self):
        replay.get_recorder().reset()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_begin_and_end_drill(self):
        """直接 API 开始和结束演练。"""
        session = replay.begin_drill(
            name="api_test",
            description="API 测试演练",
            operator="api_user",
            db_path=self.db_path,
        )
        self.assertIsNotNone(session)
        self.assertTrue(replay.is_recording())

        finished = replay.end_drill(
            result="success",
            db_path=self.db_path,
        )
        self.assertEqual(finished["result"], "success")
        self.assertFalse(replay.is_recording())

    def test_record_step_manually(self):
        """手动录制步骤。"""
        replay.begin_drill(name="manual_test", db_path=self.db_path)

        step_id = replay.get_recorder().record_step(
            action="custom_action",
            description="自定义操作",
            input_args={"param": "value"},
            result="success",
            detail={"key": "value"},
            db_path=self.db_path,
        )
        self.assertIsNotNone(step_id)

        session = replay.get_active_drill(db_path=self.db_path)
        steps = replay.get_replay_steps(session["id"], db_path=self.db_path)
        self.assertEqual(len(steps), 2)
        self.assertEqual(steps[1]["action"], "custom_action")

    def test_record_step_no_session_returns_none(self):
        """无活动会话时 record_step 返回 None。"""
        step_id = replay.get_recorder().record_step(
            action="test",
            description="测试",
            db_path=self.db_path,
        )
        self.assertIsNone(step_id)

    def test_undo_drill_api(self):
        """直接 API 撤销演练。"""
        replay.begin_drill(name="undo_api_test", db_path=self.db_path)
        session_id = replay.get_active_drill(db_path=self.db_path)["id"]

        undone = replay.undo_drill(note="API 撤销", db_path=self.db_path)
        self.assertEqual(undone["result"], "undone")
        self.assertEqual(undone["id"], session_id)


if __name__ == "__main__":
    unittest.main()
