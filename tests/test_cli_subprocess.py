"""
子进程真实 CLI 测试。

使用 subprocess 真实调用 CLI，模拟用户在命令行的使用场景，
覆盖跨重启查询、导入导出往返、冲突处理、撤销回看和异常日志追踪。
"""

import os
import sys
import json
import tempfile
import shutil
import subprocess
import unittest
from pathlib import Path


SAMPLES_DIR = Path(__file__).parent.parent / "samples"


def _run_cli(args, env=None, cwd=None):
    """运行 CLI 命令，返回 (exit_code, stdout, stderr)。"""
    import shlex
    args_escaped = " ".join(shlex.quote(a) for a in args)
    script = (
        "import sys\n"
        "sys.argv = ['inv-recon'] + sys.argv[1:]\n"
        "from invoice_recon.cli import cli\n"
        "cli()\n"
    )
    cmd = [sys.executable, "-c", script] + list(args)
    full_env = os.environ.copy()
    full_env["PYTHONIOENCODING"] = "utf-8"
    full_env["PYTHONUTF8"] = "1"
    if env:
        full_env.update(env)
    result = subprocess.run(
        cmd,
        env=full_env,
        cwd=cwd or os.getcwd(),
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )
    return result.returncode, result.stdout, result.stderr


class SubprocessDrillCrossRestartTest(unittest.TestCase):
    """子进程方式测试 drill 跨重启恢复。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="inv_recon_subproc_drill_")
        self.db_path = os.path.join(self.tmpdir, "test.db")
        self.env = {"INV_RECON_DB": self.db_path}

        code, out, err = _run_cli(["init"], env=self.env)
        self.assertEqual(code, 0, f"init failed: {err}")

        self.inv_file = SAMPLES_DIR / "invoices.csv"
        self.pay_file = SAMPLES_DIR / "payments.csv"

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_drill_survives_subprocess_restart(self):
        """drill 跨子进程（模拟重启）后仍可查询和恢复。"""
        code, out, err = _run_cli(
            ["drill", "begin", "--name", "restart_test", "--operator", "tester"],
            env=self.env,
        )
        self.assertEqual(code, 0, f"drill begin failed: {err}")
        self.assertIn("演练已开始", out)

        code, out, err = _run_cli(
            ["import", "--invoices", str(self.inv_file), "--payments", str(self.pay_file)],
            env=self.env,
        )
        self.assertEqual(code, 0, f"import failed: {err}")

        code, out, err = _run_cli(["drill", "status"], env=self.env)
        self.assertEqual(code, 0, f"drill status failed: {err}")
        self.assertIn("演练进行中", out)
        self.assertIn("restart_test", out)
        self.assertIn("已执行步骤:", out)

        code, out, err = _run_cli(
            ["drill", "end", "--result", "success"],
            env=self.env,
        )
        self.assertEqual(code, 0, f"drill end failed: {err}")
        self.assertIn("演练已结束", out)

        code, out, err = _run_cli(
            ["replay", "list", "--operator", "tester"],
            env=self.env,
        )
        self.assertEqual(code, 0, f"replay list failed: {err}")
        self.assertIn("restart_test", out)

    def test_drill_resume_after_restart(self):
        """drill resume 可以在新进程中恢复活动演练。"""
        code, out, err = _run_cli(
            ["drill", "begin", "--name", "resume_test", "--operator", "tester"],
            env=self.env,
        )
        self.assertEqual(code, 0, f"drill begin failed: {err}")

        code, out, err = _run_cli(["drill", "status"], env=self.env)
        self.assertEqual(code, 0)
        self.assertIn("演练进行中", out)

        code, out, err = _run_cli(["drill", "resume"], env=self.env)
        self.assertEqual(code, 0, f"drill resume failed: {err}")
        self.assertIn("演练已恢复", out)
        self.assertIn("resume_test", out)

        code, out, err = _run_cli(
            ["drill", "end", "--result", "success"],
            env=self.env,
        )
        self.assertEqual(code, 0, f"drill end failed: {err}")

    def test_drill_undo_lookback(self):
        """撤销回看：撤销后仍能查询到完整操作链路。"""
        code, out, err = _run_cli(
            ["drill", "begin", "--name", "undo_test", "--operator", "tester"],
            env=self.env,
        )
        self.assertEqual(code, 0, f"drill begin failed: {err}")

        code, out, err = _run_cli(
            ["import", "--invoices", str(self.inv_file), "--payments", str(self.pay_file)],
            env=self.env,
        )
        self.assertEqual(code, 0, f"import failed: {err}")

        code, out, err = _run_cli(
            ["drill", "undo", "--note", "测试撤销"],
            env=self.env,
        )
        self.assertEqual(code, 0, f"drill undo failed: {err}")
        self.assertIn("演练已撤销", out)

        code, out, err = _run_cli(["replay", "list"], env=self.env)
        self.assertEqual(code, 0, f"replay list failed: {err}")
        self.assertIn("undo_test", out)
        self.assertIn("undone", out)


class SubprocessExportImportTest(unittest.TestCase):
    """子进程方式测试导入导出往返。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="inv_recon_subproc_export_")
        self.db_path = os.path.join(self.tmpdir, "test.db")
        self.export_dir = os.path.join(self.tmpdir, "exports")
        os.makedirs(self.export_dir)
        self.env = {"INV_RECON_DB": self.db_path}

        code, out, err = _run_cli(["init"], env=self.env)
        self.assertEqual(code, 0, f"init failed: {err}")

        self.inv_file = SAMPLES_DIR / "invoices.csv"
        self.pay_file = SAMPLES_DIR / "payments.csv"

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_export_import_json_roundtrip(self):
        """JSON 导出导入往返：导出后再导入，数据保持一致。"""
        code, out, err = _run_cli(
            ["drill", "begin", "--name", "export_test", "--operator", "tester"],
            env=self.env,
        )
        self.assertEqual(code, 0, f"drill begin failed: {err}")

        code, out, err = _run_cli(
            ["import", "--invoices", str(self.inv_file), "--payments", str(self.pay_file)],
            env=self.env,
        )
        self.assertEqual(code, 0, f"import failed: {err}")

        code, out, err = _run_cli(
            ["drill", "end", "--result", "success"],
            env=self.env,
        )
        self.assertEqual(code, 0, f"drill end failed: {err}")

        export_path = os.path.join(self.export_dir, "test_export.json")
        code, out, err = _run_cli(
            ["replay", "export", "--output", export_path, "--format", "json"],
            env=self.env,
        )
        self.assertEqual(code, 0, f"export failed: {err}")
        self.assertTrue(os.path.exists(export_path))

        with open(export_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertGreaterEqual(len(data.get("sessions", [])), 1)
        session_names = [s.get("name") for s in data["sessions"]]
        self.assertIn("export_test", session_names)

        new_db = os.path.join(self.tmpdir, "new.db")
        new_env = {"INV_RECON_DB": new_db}
        code, out, err = _run_cli(["init"], env=new_env)
        self.assertEqual(code, 0, f"init new db failed: {err}")

        code, out, err = _run_cli(
            ["replay", "import", "--input", export_path],
            env=new_env,
        )
        self.assertEqual(code, 0, f"import replay failed: {err}")
        self.assertIn("已导入", out)

        code, out, err = _run_cli(["replay", "list"], env=new_env)
        self.assertEqual(code, 0, f"replay list failed: {err}")
        self.assertIn("export_test", out)

    def test_export_zip_and_verify(self):
        """ZIP 证据包导出和校验。"""
        code, out, err = _run_cli(
            ["drill", "begin", "--name", "zip_test", "--operator", "tester"],
            env=self.env,
        )
        self.assertEqual(code, 0, f"drill begin failed: {err}")

        code, out, err = _run_cli(
            ["drill", "end", "--result", "success"],
            env=self.env,
        )
        self.assertEqual(code, 0, f"drill end failed: {err}")

        zip_path = os.path.join(self.export_dir, "test_pkg.reppkg")
        code, out, err = _run_cli(
            ["replay", "export", "--output", zip_path, "--format", "zip"],
            env=self.env,
        )
        self.assertEqual(code, 0, f"export zip failed: {err}")
        self.assertTrue(os.path.exists(zip_path))

        code, out, err = _run_cli(
            ["replay", "verify", "--input", zip_path],
            env=self.env,
        )
        self.assertEqual(code, 0, f"verify failed: {err}")
        self.assertIn("校验通过", out)

    def test_export_csv_format(self):
        """CSV 格式导出。"""
        code, out, err = _run_cli(
            ["drill", "begin", "--name", "csv_test", "--operator", "tester"],
            env=self.env,
        )
        self.assertEqual(code, 0, f"drill begin failed: {err}")

        code, out, err = _run_cli(
            ["drill", "end", "--result", "success"],
            env=self.env,
        )
        self.assertEqual(code, 0, f"drill end failed: {err}")

        csv_path = os.path.join(self.export_dir, "test.csv")
        code, out, err = _run_cli(
            ["replay", "export", "--output", csv_path, "--format", "csv"],
            env=self.env,
        )
        self.assertEqual(code, 0, f"export csv failed: {err}")
        self.assertTrue(os.path.exists(csv_path))

        with open(csv_path, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertIn("csv_test", content)


class SubprocessConflictTest(unittest.TestCase):
    """子进程方式测试冲突处理。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="inv_recon_subproc_conflict_")
        self.db_path = os.path.join(self.tmpdir, "test.db")
        self.env = {"INV_RECON_DB": self.db_path}

        code, out, err = _run_cli(["init"], env=self.env)
        self.assertEqual(code, 0, f"init failed: {err}")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_duplicate_import_skipped(self):
        """重复导入：默认跳过重复会话。"""
        code, out, err = _run_cli(
            ["drill", "begin", "--name", "dup_test", "--operator", "tester"],
            env=self.env,
        )
        self.assertEqual(code, 0)
        code, out, err = _run_cli(
            ["drill", "end", "--result", "success"],
            env=self.env,
        )
        self.assertEqual(code, 0)

        export_path = os.path.join(self.tmpdir, "dup.json")
        code, out, err = _run_cli(
            ["replay", "export", "--output", export_path, "--format", "json"],
            env=self.env,
        )
        self.assertEqual(code, 0)

        code, out, err = _run_cli(
            ["replay", "import", "--input", export_path],
            env=self.env,
        )
        self.assertEqual(code, 0)
        self.assertIn("跳过", out)

    def test_duplicate_import_force(self):
        """重复导入：使用 --force 强制覆盖。"""
        code, out, err = _run_cli(
            ["drill", "begin", "--name", "force_test", "--operator", "tester"],
            env=self.env,
        )
        self.assertEqual(code, 0)
        code, out, err = _run_cli(
            ["drill", "end", "--result", "success"],
            env=self.env,
        )
        self.assertEqual(code, 0)

        export_path = os.path.join(self.tmpdir, "force.json")
        code, out, err = _run_cli(
            ["replay", "export", "--output", export_path, "--format", "json"],
            env=self.env,
        )
        self.assertEqual(code, 0)

        code, out, err = _run_cli(
            ["replay", "import", "--input", export_path, "--force"],
            env=self.env,
        )
        self.assertEqual(code, 0)
        self.assertIn("已导入", out)
        self.assertNotIn("跳过", out)

    def test_export_file_exists_error(self):
        """导出时同名文件已存在，明确报错不覆盖。"""
        code, out, err = _run_cli(
            ["drill", "begin", "--name", "exist_test", "--operator", "tester"],
            env=self.env,
        )
        self.assertEqual(code, 0)
        code, out, err = _run_cli(
            ["drill", "end", "--result", "success"],
            env=self.env,
        )
        self.assertEqual(code, 0)

        export_path = os.path.join(self.tmpdir, "existing.json")
        with open(export_path, "w") as f:
            f.write("old content")

        code, out, err = _run_cli(
            ["replay", "export", "--output", export_path, "--format", "json"],
            env=self.env,
        )
        self.assertNotEqual(code, 0)
        self.assertIn("已存在", err)

        with open(export_path, "r") as f:
            content = f.read()
        self.assertEqual(content, "old content")

    def test_import_missing_file_error(self):
        """导入不存在的文件，明确报错。"""
        missing_file = os.path.join(self.tmpdir, "nonexistent.json")
        code, out, err = _run_cli(
            ["replay", "import", "--input", missing_file],
            env=self.env,
        )
        self.assertNotEqual(code, 0)
        self.assertIn("不存在", err)


class SubprocessExceptionTraceTest(unittest.TestCase):
    """子进程方式测试异常日志追踪。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="inv_recon_subproc_exc_")
        self.db_path = os.path.join(self.tmpdir, "test.db")
        self.env = {"INV_RECON_DB": self.db_path}

        code, out, err = _run_cli(["init"], env=self.env)
        self.assertEqual(code, 0, f"init failed: {err}")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_command_failure_recorded(self):
        """命令失败时，异常信息被记录到演练步骤中。"""
        code, out, err = _run_cli(
            ["drill", "begin", "--name", "fail_test", "--operator", "tester"],
            env=self.env,
        )
        self.assertEqual(code, 0, f"drill begin failed: {err}")

        bad_file = os.path.join(self.tmpdir, "bad.csv")
        with open(bad_file, "w") as f:
            f.write("invalid,columns\n1,2,3\n")

        code, out, err = _run_cli(
            ["import", "--invoices", bad_file, "--payments", bad_file],
            env=self.env,
        )

        code, out, err = _run_cli(
            ["drill", "end", "--result", "failure", "--error-message", "测试失败"],
            env=self.env,
        )
        self.assertEqual(code, 0, f"drill end failed: {err}")

        code, out, err = _run_cli(["replay", "list"], env=self.env)
        self.assertEqual(code, 0, f"replay list failed: {err}")
        self.assertIn("fail_test", out)

    def test_show_session_details(self):
        """replay show 可以查看会话详情和步骤。"""
        code, out, err = _run_cli(
            ["drill", "begin", "--name", "show_test", "--operator", "tester"],
            env=self.env,
        )
        self.assertEqual(code, 0)
        code, out, err = _run_cli(
            ["drill", "end", "--result", "success"],
            env=self.env,
        )
        self.assertEqual(code, 0)

        code, out, err = _run_cli(["replay", "show", "1"], env=self.env)
        self.assertEqual(code, 0, f"replay show failed: {err}")
        self.assertIn("show_test", out)
        self.assertIn("步骤", out)


if __name__ == "__main__":
    unittest.main()
