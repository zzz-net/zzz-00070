# -*- coding: utf-8 -*-
"""
任务回放与证据包模块。

将一次复杂操作的输入、配置、关键步骤、冲突处理、撤销结果和异常日志
串成可重放记录，持久化到 SQLite，跨重启可查。

命令: replay start/list/show/export/import/undo

配置项:
  - detail_enabled: 是否开启明细采集（默认 True）
  - masked_fields: 脱敏字段列表（JSON 数组，默认 []）
  - retention_days: 回放记录保留天数（默认 365，0 表示永久保留）
"""

import csv
import json
import os
import re
import sys
import zipfile
import hashlib
import tempfile
import shutil
import traceback
import datetime
import sqlite3
from typing import Optional, List, Dict, Any, Tuple
from pathlib import Path

from . import db


REPLAY_SCHEMA_VERSION = 1
PACKAGE_SCHEMA_VERSION = 1
TOOL_VERSION = "1.0.0"

REPLAY_EXT = ".reppkg"

DEFAULT_DETAIL_ENABLED = True
DEFAULT_MASKED_FIELDS: List[str] = []
DEFAULT_RETENTION_DAYS = 365

VALID_SESSION_RESULTS = {"running", "success", "failure", "undone", "error"}
VALID_STEP_RESULTS = {"success", "failure", "blocked", "skipped", "error"}

REQUIRED_PACKAGE_FILES = ["manifest.json", "sessions.json", "steps.json", "checksums.json"]


REPLAY_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS replay_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_key TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    description TEXT,
    operator TEXT NOT NULL DEFAULT 'system',
    batch_id INTEGER,
    batch_name TEXT,
    result TEXT NOT NULL DEFAULT 'running',
    start_time TEXT NOT NULL DEFAULT (datetime('now')),
    end_time TEXT,
    input_summary TEXT,
    config_snapshot TEXT,
    error_message TEXT,
    undo_time TEXT,
    undo_note TEXT
);

CREATE TABLE IF NOT EXISTS replay_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL,
    step_index INTEGER NOT NULL,
    action TEXT NOT NULL,
    description TEXT,
    result TEXT NOT NULL DEFAULT 'success',
    timestamp TEXT NOT NULL DEFAULT (datetime('now')),
    detail TEXT,
    error_message TEXT,
    FOREIGN KEY (session_id) REFERENCES replay_sessions(id)
);

CREATE TABLE IF NOT EXISTS replay_config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_replay_session_key ON replay_sessions(session_key);
CREATE INDEX IF NOT EXISTS idx_replay_session_result ON replay_sessions(result);
CREATE INDEX IF NOT EXISTS idx_replay_session_operator ON replay_sessions(operator);
CREATE INDEX IF NOT EXISTS idx_replay_session_batch ON replay_sessions(batch_id);
CREATE INDEX IF NOT EXISTS idx_replay_session_start ON replay_sessions(start_time);
CREATE INDEX IF NOT EXISTS idx_replay_step_session ON replay_steps(session_id);
CREATE INDEX IF NOT EXISTS idx_replay_step_action ON replay_steps(action);
CREATE INDEX IF NOT EXISTS idx_replay_step_result ON replay_steps(result);
"""


def _sanitize_filename(name: str) -> str:
    safe = []
    for c in name:
        if c.isalnum() or c in ("-", "_", "."):
            safe.append(c)
        else:
            safe.append("_")
    return "".join(safe) or "replay"


def _sha256_file(filepath: Path) -> str:
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _generate_session_key() -> str:
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    import random
    rand = random.randint(1000, 9999)
    return f"rep_{ts}_{rand}"


def init_replay_db(db_path: Optional[str] = None) -> None:
    conn = db.connect(db_path)
    try:
        with conn:
            conn.executescript(REPLAY_SCHEMA_V1)
            _migrate_replay_schema(conn)
            _ensure_replay_config_defaults(conn)
    finally:
        conn.close()


def _migrate_replay_schema(conn: sqlite3.Connection) -> None:
    cols_session = {r["name"] for r in conn.execute("PRAGMA table_info(replay_sessions)").fetchall()}
    if "undo_time" not in cols_session:
        conn.execute("ALTER TABLE replay_sessions ADD COLUMN undo_time TEXT")
    if "undo_note" not in cols_session:
        conn.execute("ALTER TABLE replay_sessions ADD COLUMN undo_note TEXT")


def _ensure_replay_config_defaults(conn: sqlite3.Connection) -> None:
    existing = {r["key"] for r in conn.execute("SELECT key FROM replay_config").fetchall()}
    if "detail_enabled" not in existing:
        conn.execute(
            "INSERT INTO replay_config (key, value) VALUES (?, ?)",
            ("detail_enabled", "1" if DEFAULT_DETAIL_ENABLED else "0"),
        )
    if "masked_fields" not in existing:
        conn.execute(
            "INSERT INTO replay_config (key, value) VALUES (?, ?)",
            ("masked_fields", json.dumps(DEFAULT_MASKED_FIELDS, ensure_ascii=False)),
        )
    if "retention_days" not in existing:
        conn.execute(
            "INSERT INTO replay_config (key, value) VALUES (?, ?)",
            ("retention_days", str(DEFAULT_RETENTION_DAYS)),
        )
    if "active_session_id" not in existing:
        conn.execute(
            "INSERT INTO replay_config (key, value) VALUES (?, ?)",
            ("active_session_id", ""),
        )
    if "active_batch_id" not in existing:
        conn.execute(
            "INSERT INTO replay_config (key, value) VALUES (?, ?)",
            ("active_batch_id", ""),
        )


def _get_active_session_id(db_path: Optional[str] = None) -> Optional[int]:
    init_replay_db(db_path)
    conn = db.connect(db_path)
    try:
        row = conn.execute(
            "SELECT value FROM replay_config WHERE key = ?",
            ("active_session_id",),
        ).fetchone()
        if row and row["value"]:
            try:
                return int(row["value"])
            except (ValueError, TypeError):
                return None
        return None
    finally:
        conn.close()


def _set_active_session_id(session_id: Optional[int], db_path: Optional[str] = None) -> None:
    init_replay_db(db_path)
    conn = db.connect(db_path)
    try:
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO replay_config (key, value) VALUES (?, ?)",
                ("active_session_id", str(session_id) if session_id is not None else ""),
            )
    finally:
        conn.close()


def _get_active_batch_id(db_path: Optional[str] = None) -> Optional[int]:
    init_replay_db(db_path)
    conn = db.connect(db_path)
    try:
        row = conn.execute(
            "SELECT value FROM replay_config WHERE key = ?",
            ("active_batch_id",),
        ).fetchone()
        if row and row["value"]:
            try:
                return int(row["value"])
            except (ValueError, TypeError):
                return None
        return None
    finally:
        conn.close()


def _set_active_batch_id(batch_id: Optional[int], db_path: Optional[str] = None) -> None:
    init_replay_db(db_path)
    conn = db.connect(db_path)
    try:
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO replay_config (key, value) VALUES (?, ?)",
                ("active_batch_id", str(batch_id) if batch_id is not None else ""),
            )
    finally:
        conn.close()


def get_replay_config(db_path: Optional[str] = None) -> Dict[str, Any]:
    init_replay_db(db_path)
    conn = db.connect(db_path)
    try:
        rows = conn.execute("SELECT key, value FROM replay_config").fetchall()
        config = {}
        for r in rows:
            config[r["key"]] = r["value"]

        detail_enabled = config.get("detail_enabled", "1") == "1"
        try:
            masked_fields = json.loads(config.get("masked_fields", "[]"))
            if not isinstance(masked_fields, list):
                masked_fields = []
        except (json.JSONDecodeError, TypeError):
            masked_fields = []

        try:
            retention_days = int(config.get("retention_days", str(DEFAULT_RETENTION_DAYS)))
        except (ValueError, TypeError):
            retention_days = DEFAULT_RETENTION_DAYS

        return {
            "detail_enabled": detail_enabled,
            "masked_fields": masked_fields,
            "retention_days": retention_days,
        }
    finally:
        conn.close()


def set_replay_config(
    detail_enabled: Optional[bool] = None,
    masked_fields: Optional[List[str]] = None,
    retention_days: Optional[int] = None,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    init_replay_db(db_path)
    errors = []

    if detail_enabled is not None:
        if not isinstance(detail_enabled, bool):
            errors.append("detail_enabled 必须为布尔值")

    if masked_fields is not None:
        if not isinstance(masked_fields, list):
            errors.append("masked_fields 必须为列表")
        else:
            for f in masked_fields:
                if not isinstance(f, str) or not f:
                    errors.append("masked_fields 中的每个元素必须为非空字符串")
                elif len(f) > 100:
                    errors.append(f"脱敏字段名过长: {f[:20]}...")

    if retention_days is not None:
        if not isinstance(retention_days, int) or retention_days < 0:
            errors.append("retention_days 必须为非负整数")

    if errors:
        raise ValueError("; ".join(errors))

    old_config = get_replay_config(db_path)

    conn = db.connect(db_path)
    try:
        with conn:
            if detail_enabled is not None:
                conn.execute(
                    "INSERT OR REPLACE INTO replay_config (key, value) VALUES (?, ?)",
                    ("detail_enabled", "1" if detail_enabled else "0"),
                )
            if masked_fields is not None:
                conn.execute(
                    "INSERT OR REPLACE INTO replay_config (key, value) VALUES (?, ?)",
                    ("masked_fields", json.dumps(masked_fields, ensure_ascii=False)),
                )
            if retention_days is not None:
                conn.execute(
                    "INSERT OR REPLACE INTO replay_config (key, value) VALUES (?, ?)",
                    ("retention_days", str(retention_days)),
                )
    finally:
        conn.close()

    new_config = get_replay_config(db_path)

    changed = {}
    if detail_enabled is not None and old_config.get("detail_enabled") != detail_enabled:
        changed["detail_enabled"] = {
            "old": old_config.get("detail_enabled"),
            "new": detail_enabled,
        }
    if masked_fields is not None and old_config.get("masked_fields") != masked_fields:
        changed["masked_fields"] = {
            "old": old_config.get("masked_fields"),
            "new": masked_fields,
        }
    if retention_days is not None and old_config.get("retention_days") != retention_days:
        changed["retention_days"] = {
            "old": old_config.get("retention_days"),
            "new": retention_days,
        }

    if changed:
        _record_config_change(changed, db_path)

    _auto_cleanup(db_path)
    return new_config


def _record_config_change(changed: Dict, db_path: Optional[str]) -> None:
    try:
        session = start_replay_session(
            name="replay_config_change",
            description="回放配置变更",
            db_path=db_path,
        )
        add_replay_step(
            session_id=session["id"],
            action="config_change",
            description="修改回放配置",
            result="success",
            detail={"changes": changed},
            db_path=db_path,
        )
        finish_replay_session(session["id"], result="success", db_path=db_path)
    except Exception:
        pass


def start_replay_session(
    name: str,
    description: Optional[str] = None,
    operator: str = "system",
    batch_id: Optional[int] = None,
    batch_name: Optional[str] = None,
    input_summary: Optional[Dict] = None,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    init_replay_db(db_path)

    if not name or not isinstance(name, str):
        raise ValueError("会话名称不能为空")

    session_key = _generate_session_key()
    config = get_replay_config(db_path)

    config_snapshot = None
    if config["detail_enabled"]:
        config_snapshot = json.dumps({
            "detail_enabled": config["detail_enabled"],
            "masked_fields": config["masked_fields"],
            "retention_days": config["retention_days"],
        }, ensure_ascii=False)

    input_summary_str = None
    if input_summary and config["detail_enabled"]:
        masked = _mask_dict(input_summary, config["masked_fields"])
        input_summary_str = json.dumps(masked, ensure_ascii=False, default=str)

    conn = db.connect(db_path)
    try:
        with conn:
            cur = conn.execute(
                "INSERT INTO replay_sessions "
                "(session_key, name, description, operator, batch_id, batch_name, "
                "result, input_summary, config_snapshot) "
                "VALUES (?, ?, ?, ?, ?, ?, 'running', ?, ?)",
                (session_key, name, description, operator, batch_id, batch_name,
                 input_summary_str, config_snapshot),
            )
            session_id = cur.lastrowid
    finally:
        conn.close()

    return get_replay_session(session_id, db_path=db_path)


def _mask_dict(data: Dict, masked_fields: List[str]) -> Dict:
    if not masked_fields:
        return data

    result = {}
    for k, v in data.items():
        if k in masked_fields:
            result[k] = "***MASKED***"
        elif isinstance(v, dict):
            result[k] = _mask_dict(v, masked_fields)
        elif isinstance(v, list):
            result[k] = [
                _mask_dict(item, masked_fields) if isinstance(item, dict) else item
                for item in v
            ]
        else:
            result[k] = v
    return result


def add_replay_step(
    session_id: int,
    action: str,
    description: Optional[str] = None,
    result: str = "success",
    detail: Optional[Dict] = None,
    error_message: Optional[str] = None,
    exception: Optional[BaseException] = None,
    db_path: Optional[str] = None,
) -> int:
    init_replay_db(db_path)

    if result not in VALID_STEP_RESULTS:
        raise ValueError(
            f"非法步骤结果类型: {result}，合法值: {', '.join(sorted(VALID_STEP_RESULTS))}"
        )

    session = get_replay_session(session_id, db_path=db_path)
    if session is None:
        raise ValueError(f"回放会话不存在: {session_id}")

    if session["result"] == "undone":
        raise ValueError("已撤销的会话不能添加步骤")

    config = get_replay_config(db_path)
    detail_dict = dict(detail) if detail else {}

    if exception is not None:
        detail_dict["error_type"] = type(exception).__name__
        detail_dict["error_traceback"] = traceback.format_exc()
        if error_message is None:
            error_message = str(exception)

    detail_str = None
    if detail_dict and config["detail_enabled"]:
        masked = _mask_dict(detail_dict, config["masked_fields"])
        detail_str = json.dumps(masked, ensure_ascii=False, default=str)

    conn = db.connect(db_path)
    try:
        with conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(step_index), 0) AS max_idx "
                "FROM replay_steps WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            step_index = row["max_idx"] + 1 if row else 1

            cur = conn.execute(
                "INSERT INTO replay_steps "
                "(session_id, step_index, action, description, result, detail, error_message) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (session_id, step_index, action, description, result,
                 detail_str, error_message),
            )
            step_id = cur.lastrowid
    finally:
        conn.close()

    return step_id


def finish_replay_session(
    session_id: int,
    result: str = "success",
    error_message: Optional[str] = None,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    init_replay_db(db_path)

    if result not in VALID_SESSION_RESULTS or result == "running":
        raise ValueError(
            f"非法会话结果类型: {result}，合法值: success, failure, error"
        )

    session = get_replay_session(session_id, db_path=db_path)
    if session is None:
        raise ValueError(f"回放会话不存在: {session_id}")

    if session["result"] == "undone":
        raise ValueError("已撤销的会话不能修改结果")

    conn = db.connect(db_path)
    try:
        with conn:
            conn.execute(
                "UPDATE replay_sessions SET result = ?, end_time = datetime('now'), "
                "error_message = ? WHERE id = ?",
                (result, error_message, session_id),
            )
    finally:
        conn.close()

    _auto_cleanup(db_path)
    return get_replay_session(session_id, db_path=db_path)


def undo_replay_session(
    session_id: int,
    note: Optional[str] = None,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    init_replay_db(db_path)

    session = get_replay_session(session_id, db_path=db_path)
    if session is None:
        raise ValueError(f"回放会话不存在: {session_id}")

    if session["result"] == "undone":
        raise ValueError("该会话已被撤销")

    conn = db.connect(db_path)
    try:
        with conn:
            conn.execute(
                "UPDATE replay_sessions SET result = 'undone', "
                "undo_time = datetime('now'), undo_note = ? WHERE id = ?",
                (note, session_id),
            )
    finally:
        conn.close()

    return get_replay_session(session_id, db_path=db_path)


def get_replay_session(
    session_id: int,
    db_path: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    init_replay_db(db_path)
    conn = db.connect(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM replay_sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if row is None:
            return None
        return _session_row_to_dict(row)
    finally:
        conn.close()


def get_replay_session_by_key(
    session_key: str,
    db_path: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    init_replay_db(db_path)
    conn = db.connect(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM replay_sessions WHERE session_key = ?", (session_key,)
        ).fetchone()
        if row is None:
            return None
        return _session_row_to_dict(row)
    finally:
        conn.close()


def list_replay_sessions(
    batch_id: Optional[int] = None,
    batch_name: Optional[str] = None,
    operator: Optional[str] = None,
    result: Optional[str] = None,
    action: Optional[str] = None,
    time_start: Optional[str] = None,
    time_end: Optional[str] = None,
    limit: int = 50,
    db_path: Optional[str] = None,
) -> List[Dict[str, Any]]:
    init_replay_db(db_path)
    conn = db.connect(db_path)
    try:
        conditions = []
        params: list = []

        if batch_id is not None:
            conditions.append("s.batch_id = ?")
            params.append(batch_id)
        if batch_name is not None:
            conditions.append("s.batch_name LIKE ?")
            params.append(f"%{batch_name}%")
        if operator is not None:
            conditions.append("s.operator = ?")
            params.append(operator)
        if result is not None:
            conditions.append("s.result = ?")
            params.append(result)
        if time_start is not None:
            conditions.append("s.start_time >= ?")
            params.append(time_start)
        if time_end is not None:
            conditions.append("s.start_time <= ?")
            params.append(time_end)

        where = ""
        if conditions:
            where = "WHERE " + " AND ".join(conditions)

        if action:
            where = (where + " AND " if where else "WHERE ") + (
                "EXISTS (SELECT 1 FROM replay_steps st "
                "WHERE st.session_id = s.id AND st.action = ?)"
            )
            params.append(action)

        params.append(limit)
        rows = conn.execute(
            f"SELECT s.* FROM replay_sessions s {where} "
            f"ORDER BY s.id DESC LIMIT ?",
            params,
        ).fetchall()
        return [_session_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def get_replay_steps(
    session_id: int,
    db_path: Optional[str] = None,
) -> List[Dict[str, Any]]:
    init_replay_db(db_path)
    conn = db.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM replay_steps WHERE session_id = ? ORDER BY step_index ASC",
            (session_id,),
        ).fetchall()
        return [_step_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def _session_row_to_dict(row) -> Dict[str, Any]:
    d = dict(row)
    for field in ("input_summary", "config_snapshot"):
        if d.get(field) and isinstance(d[field], str):
            try:
                d[field] = json.loads(d[field])
            except (json.JSONDecodeError, TypeError):
                pass
    return d


def _step_row_to_dict(row) -> Dict[str, Any]:
    d = dict(row)
    if d.get("detail") and isinstance(d["detail"], str):
        try:
            d["detail"] = json.loads(d["detail"])
        except (json.JSONDecodeError, TypeError):
            pass
    return d


def _auto_cleanup(db_path: Optional[str] = None) -> None:
    try:
        config = get_replay_config(db_path)
        retention = config.get("retention_days", DEFAULT_RETENTION_DAYS)
        if retention <= 0:
            return
        conn = db.connect(db_path)
        try:
            with conn:
                conn.execute(
                    "DELETE FROM replay_steps WHERE session_id IN ("
                    "SELECT id FROM replay_sessions WHERE start_time < datetime('now', ?)"
                    ")",
                    (f"-{retention} days",),
                )
                conn.execute(
                    "DELETE FROM replay_sessions WHERE start_time < datetime('now', ?)",
                    (f"-{retention} days",),
                )
        finally:
            conn.close()
    except Exception:
        pass


def cleanup_replay(
    retention_days: Optional[int] = None,
    db_path: Optional[str] = None,
) -> int:
    init_replay_db(db_path)
    if retention_days is None:
        config = get_replay_config(db_path)
        retention_days = config.get("retention_days", DEFAULT_RETENTION_DAYS)
    if retention_days <= 0:
        return 0

    conn = db.connect(db_path)
    try:
        with conn:
            cur = conn.execute(
                "DELETE FROM replay_sessions WHERE start_time < datetime('now', ?)",
                (f"-{retention_days} days",),
            )
            return cur.rowcount
    finally:
        conn.close()


# ============================================================================
# 证据包导入导出
# ============================================================================

def export_replay_package(
    output_path: str,
    fmt: str = "json",
    session_ids: Optional[List[int]] = None,
    batch_id: Optional[int] = None,
    batch_name: Optional[str] = None,
    operator: Optional[str] = None,
    result: Optional[str] = None,
    time_start: Optional[str] = None,
    time_end: Optional[str] = None,
    db_path: Optional[str] = None,
) -> str:
    if fmt not in ("json", "csv", "zip"):
        raise ValueError(f"不支持的导出格式: {fmt}，仅支持 json/csv/zip")

    abs_path = os.path.abspath(output_path)
    dir_path = os.path.dirname(abs_path)

    if not dir_path:
        dir_path = "."

    if not os.path.exists(dir_path):
        raise FileNotFoundError(
            f"目标目录不存在: {dir_path}，请先创建目录再导出"
        )

    if not os.path.isdir(dir_path):
        raise NotADirectoryError(
            f"目标路径不是目录: {dir_path}"
        )

    if not os.access(dir_path, os.W_OK):
        raise PermissionError(
            f"目标目录不可写: {dir_path}，请检查目录权限后重试"
        )

    if os.path.exists(abs_path):
        if os.path.isdir(abs_path):
            raise IsADirectoryError(
                f"目标路径是一个目录: {abs_path}，请指定文件路径"
            )
        raise FileExistsError(
            f"目标文件已存在: {abs_path}，不会覆盖。请更换文件名或删除现有文件后重试"
        )

    if session_ids is not None:
        sessions = []
        for sid in session_ids:
            s = get_replay_session(sid, db_path=db_path)
            if s:
                sessions.append(s)
    else:
        sessions = list_replay_sessions(
            batch_id=batch_id,
            batch_name=batch_name,
            operator=operator,
            result=result,
            time_start=time_start,
            time_end=time_end,
            limit=100000,
            db_path=db_path,
        )

    all_steps = []
    for s in sessions:
        steps = get_replay_steps(s["id"], db_path=db_path)
        all_steps.extend(steps)

    if fmt == "json":
        _export_json_atomic(abs_path, sessions, all_steps)
    elif fmt == "csv":
        _export_csv_atomic(abs_path, sessions, all_steps)
    else:
        _export_zip_atomic(abs_path, sessions, all_steps)

    return abs_path


def _export_json_atomic(abs_path: str, sessions: List[Dict], steps: List[Dict]) -> None:
    dir_path = os.path.dirname(abs_path) or "."
    fd, tmp_path = tempfile.mkstemp(dir=dir_path, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            export_data = {
                "schema_version": PACKAGE_SCHEMA_VERSION,
                "tool_version": TOOL_VERSION,
                "export_time": datetime.datetime.now().isoformat(),
                "sessions": sessions,
                "steps": steps,
            }
            json.dump(export_data, f, ensure_ascii=False, indent=2, default=str)
        os.replace(tmp_path, abs_path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _export_csv_atomic(abs_path: str, sessions: List[Dict], steps: List[Dict]) -> None:
    dir_path = os.path.dirname(abs_path) or "."
    fd, tmp_path = tempfile.mkstemp(dir=dir_path, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                "type", "session_id", "session_key", "session_name",
                "step_index", "action", "description", "result",
                "timestamp", "operator", "batch_id", "batch_name",
                "detail", "error_message",
            ])
            for s in sessions:
                writer.writerow([
                    "session",
                    s.get("id", ""),
                    s.get("session_key", ""),
                    s.get("name", ""),
                    "",
                    "",
                    s.get("description", ""),
                    s.get("result", ""),
                    s.get("start_time", ""),
                    s.get("operator", ""),
                    s.get("batch_id", ""),
                    s.get("batch_name", ""),
                    _detail_to_str(s.get("input_summary")),
                    s.get("error_message", ""),
                ])
            for st in steps:
                writer.writerow([
                    "step",
                    st.get("session_id", ""),
                    "",
                    "",
                    st.get("step_index", ""),
                    st.get("action", ""),
                    st.get("description", ""),
                    st.get("result", ""),
                    st.get("timestamp", ""),
                    "",
                    "",
                    "",
                    _detail_to_str(st.get("detail")),
                    st.get("error_message", ""),
                ])
        os.replace(tmp_path, abs_path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _detail_to_str(detail) -> str:
    if detail is None:
        return ""
    if isinstance(detail, (dict, list)):
        return json.dumps(detail, ensure_ascii=False, default=str)
    return str(detail)


def _export_zip_atomic(abs_path: str, sessions: List[Dict], steps: List[Dict]) -> None:
    dir_path = os.path.dirname(abs_path) or "."
    fd, tmp_path = tempfile.mkstemp(dir=dir_path, suffix=".tmp")
    os.close(fd)

    try:
        with tempfile.TemporaryDirectory(prefix="replay_pack_") as tmpdir:
            tmp = Path(tmpdir)

            manifest = {
                "package_version": "1.0",
                "schema_version": PACKAGE_SCHEMA_VERSION,
                "tool_version": TOOL_VERSION,
                "created_at": datetime.datetime.now().isoformat(),
                "session_count": len(sessions),
                "step_count": len(steps),
            }
            with open(tmp / "manifest.json", "w", encoding="utf-8") as f:
                json.dump(manifest, f, ensure_ascii=False, indent=2)

            with open(tmp / "sessions.json", "w", encoding="utf-8") as f:
                json.dump(sessions, f, ensure_ascii=False, indent=2, default=str)

            with open(tmp / "steps.json", "w", encoding="utf-8") as f:
                json.dump(steps, f, ensure_ascii=False, indent=2, default=str)

            checksums = {}
            for fname in ["manifest.json", "sessions.json", "steps.json"]:
                checksums[fname] = _sha256_file(tmp / fname)
            with open(tmp / "checksums.json", "w", encoding="utf-8") as f:
                json.dump(checksums, f, ensure_ascii=False, indent=2)

            with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for fname in ["manifest.json", "sessions.json", "steps.json", "checksums.json"]:
                    zf.write(tmp / fname, fname)

        os.replace(tmp_path, abs_path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def import_replay_package(
    package_path: str,
    force: bool = False,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    init_replay_db(db_path)

    pkg = Path(package_path)
    if not pkg.exists():
        raise FileNotFoundError(f"证据包文件不存在: {package_path}")

    abs_db = os.path.abspath(db_path or db.get_db_path())
    db_dir = os.path.dirname(abs_db) or "."
    if not os.access(db_dir, os.W_OK):
        raise PermissionError(
            f"数据库目录不可写: {db_dir}，无法导入回放记录"
        )

    verify_result = verify_replay_package(package_path)
    if not verify_result["valid"]:
        error_msg = ";\n".join(verify_result["errors"])
        raise ValueError(f"证据包校验失败: {error_msg}")

    warnings = verify_result.get("warnings", [])
    version_warn = [w for w in warnings if "schema 版本" in w]
    if version_warn and not force:
        raise ValueError(
            f"证据包 schema 版本不兼容: {version_warn[0]}。"
            f"如需强制导入请使用 --force。"
        )

    with tempfile.TemporaryDirectory(prefix="replay_import_") as tmpdir:
        tmp = Path(tmpdir)

        if package_path.endswith(".zip") or package_path.endswith(REPLAY_EXT):
            with zipfile.ZipFile(pkg, "r") as zf:
                zf.extractall(tmp)
            sessions_file = tmp / "sessions.json"
            steps_file = tmp / "steps.json"
        else:
            sessions_file = pkg
            steps_file = None

        if not sessions_file.exists():
            raise ValueError("证据包中缺少会话数据文件")

        with open(sessions_file, "r", encoding="utf-8") as f:
            data = json.load(f)

        if isinstance(data, dict) and "sessions" in data:
            sessions = data.get("sessions", [])
            steps = data.get("steps", [])
        elif isinstance(data, list):
            sessions = data
            steps = []
        else:
            raise ValueError("证据包格式不正确")

        if steps_file and steps_file.exists() and not steps:
            with open(steps_file, "r", encoding="utf-8") as f:
                steps_data = json.load(f)
            if isinstance(steps_data, list):
                steps = steps_data
            elif isinstance(steps_data, dict) and "steps" in steps_data:
                steps = steps_data.get("steps", [])

        existing_keys = set()
        conn = db.connect(db_path)
        try:
            rows = conn.execute(
                "SELECT session_key FROM replay_sessions"
            ).fetchall()
            existing_keys = {r["session_key"] for r in rows}
        finally:
            conn.close()

        imported_sessions = []
        skipped_sessions = []
        imported_steps = 0

        conn = db.connect(db_path)
        try:
            with conn:
                for s in sessions:
                    session_key = s.get("session_key")
                    if not session_key:
                        session_key = _generate_session_key()

                    if session_key in existing_keys and not force:
                        skipped_sessions.append(session_key)
                        continue

                    if session_key in existing_keys and force:
                        conn.execute(
                            "DELETE FROM replay_steps WHERE session_id IN ("
                            "SELECT id FROM replay_sessions WHERE session_key = ?"
                            ")",
                            (session_key,),
                        )
                        conn.execute(
                            "DELETE FROM replay_sessions WHERE session_key = ?",
                            (session_key,),
                        )

                    cur = conn.execute(
                        "INSERT INTO replay_sessions "
                        "(session_key, name, description, operator, batch_id, "
                        "batch_name, result, start_time, end_time, "
                        "input_summary, config_snapshot, error_message, "
                        "undo_time, undo_note) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            session_key,
                            s.get("name", "imported"),
                            s.get("description"),
                            s.get("operator", "imported"),
                            s.get("batch_id"),
                            s.get("batch_name"),
                            s.get("result", "success"),
                            s.get("start_time"),
                            s.get("end_time"),
                            _json_or_none(s.get("input_summary")),
                            _json_or_none(s.get("config_snapshot")),
                            s.get("error_message"),
                            s.get("undo_time"),
                            s.get("undo_note"),
                        ),
                    )
                    new_session_id = cur.lastrowid
                    imported_sessions.append({
                        "new_id": new_session_id,
                        "session_key": session_key,
                        "original_id": s.get("id"),
                    })

                    session_steps = [st for st in steps if st.get("session_id") == s.get("id")]
                    step_id_map = {}
                    for st in session_steps:
                        cur_st = conn.execute(
                            "INSERT INTO replay_steps "
                            "(session_id, step_index, action, description, "
                            "result, timestamp, detail, error_message) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                            (
                                new_session_id,
                                st.get("step_index", 0),
                                st.get("action", "unknown"),
                                st.get("description"),
                                st.get("result", "success"),
                                st.get("timestamp"),
                                _json_or_none(st.get("detail")),
                                st.get("error_message"),
                            ),
                        )
                        step_id_map[st.get("id")] = cur_st.lastrowid
                        imported_steps += 1

                    existing_keys.add(session_key)
        finally:
            conn.close()

    return {
        "success": True,
        "imported_session_count": len(imported_sessions),
        "skipped_session_count": len(skipped_sessions),
        "imported_step_count": imported_steps,
        "imported_sessions": imported_sessions,
        "skipped_sessions": skipped_sessions,
        "warnings": warnings,
    }


def _json_or_none(val) -> Optional[str]:
    if val is None:
        return None
    if isinstance(val, str):
        return val
    return json.dumps(val, ensure_ascii=False, default=str)


def verify_replay_package(package_path: str) -> Dict[str, Any]:
    pkg = Path(package_path)
    if not pkg.exists():
        return {
            "valid": False,
            "errors": [f"证据包文件不存在: {package_path}"],
            "warnings": [],
            "manifest": None,
        }

    errors: List[str] = []
    warnings: List[str] = []
    manifest = None

    is_zip = package_path.endswith(".zip") or package_path.endswith(REPLAY_EXT)

    try:
        if is_zip:
            try:
                with zipfile.ZipFile(pkg, "r") as zf:
                    namelist = zf.namelist()
                    for req in REQUIRED_PACKAGE_FILES:
                        if req not in namelist:
                            errors.append(f"缺少必需文件: {req}")

                    if errors:
                        return {
                            "valid": False,
                            "errors": errors,
                            "warnings": warnings,
                            "manifest": None,
                        }

                    with tempfile.TemporaryDirectory(prefix="replay_verify_") as tmpdir:
                        tmp = Path(tmpdir)
                        zf.extractall(tmp)
                        return _verify_extracted_files(tmp, errors, warnings)
            except zipfile.BadZipFile:
                errors.append("文件不是有效的 ZIP 包")
                return {
                    "valid": False,
                    "errors": errors,
                    "warnings": warnings,
                    "manifest": None,
                }
        else:
            try:
                with open(pkg, "r", encoding="utf-8") as f:
                    data = json.load(f)

                if isinstance(data, dict):
                    if "sessions" not in data:
                        errors.append("JSON 文件中缺少 sessions 字段")
                    schema_ver = data.get("schema_version")
                    if schema_ver and schema_ver != PACKAGE_SCHEMA_VERSION:
                        warnings.append(
                            f"证据包 schema 版本 {schema_ver} "
                            f"与当前版本 {PACKAGE_SCHEMA_VERSION} 可能不兼容"
                        )
                elif not isinstance(data, list):
                    errors.append("JSON 文件格式不正确，需要对象或数组")
            except json.JSONDecodeError as e:
                errors.append(f"JSON 格式错误: {e}")
    except Exception as e:
        errors.append(f"校验异常: {e}")

    return {
        "valid": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "manifest": manifest,
    }


def _verify_extracted_files(tmp: Path, errors: List[str], warnings: List[str]) -> Dict[str, Any]:
    try:
        with open(tmp / "checksums.json", encoding="utf-8") as f:
            checksums = json.load(f)
    except json.JSONDecodeError as e:
        errors.append(f"checksums.json 格式错误: {e}")
        return {
            "valid": False,
            "errors": errors,
            "warnings": warnings,
            "manifest": None,
        }

    for fname, expected_hash in checksums.items():
        fpath = tmp / fname
        if not fpath.exists():
            errors.append(f"校验和文件缺失: {fname}")
            continue
        actual_hash = _sha256_file(fpath)
        if actual_hash != expected_hash:
            errors.append(f"文件 {fname} 校验和不匹配")

    try:
        with open(tmp / "manifest.json", encoding="utf-8") as f:
            manifest = json.load(f)
    except json.JSONDecodeError as e:
        errors.append(f"manifest.json 格式错误: {e}")
        return {
            "valid": False,
            "errors": errors,
            "warnings": warnings,
            "manifest": None,
        }

    if manifest.get("schema_version") != PACKAGE_SCHEMA_VERSION:
        warnings.append(
            f"证据包 schema 版本 {manifest.get('schema_version')} "
            f"与当前版本 {PACKAGE_SCHEMA_VERSION} 可能不兼容"
        )

    try:
        with open(tmp / "sessions.json", encoding="utf-8") as f:
            sessions = json.load(f)
        if not isinstance(sessions, list):
            errors.append("sessions.json 格式错误，应为数组")
    except json.JSONDecodeError as e:
        errors.append(f"sessions.json 格式错误: {e}")

    try:
        with open(tmp / "steps.json", encoding="utf-8") as f:
            steps = json.load(f)
        if not isinstance(steps, list):
            errors.append("steps.json 格式错误，应为数组")
    except json.JSONDecodeError as e:
        errors.append(f"steps.json 格式错误: {e}")

    return {
        "valid": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "manifest": manifest if not errors else None,
    }


# ============================================================================
# 操作录制管理器（OperationRecorder）
# ============================================================================
#
# 设计思路：
#   1. 用户通过 `drill begin` 开始一次演练，自动创建回放会话
#   2. 后续的 import/match/review/review-undo/export/revoke 等命令
#      被 @record_operation 装饰器装饰，自动将步骤追加到当前活动会话
#   3. 每一步自动记录：输入摘要、配置快照、批次号、结果、异常、操作者
#   4. 通过 `drill end` 收口（success/failure），或 `drill undo` 撤销
#   5. 命令异常时自动记录 error_type 和 error_traceback
#
# ============================================================================


class OperationRecorder:
    """
    操作录制管理器（单例模式）。

    自动追踪用户的操作序列，将每个 CLI 命令的执行过程
    挂到同一条回放轨迹中，形成完整的可重放证据链。

    活动状态持久化到 SQLite，跨进程、跨重启可恢复。
    """

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._active_session_id = None
            cls._instance._active_batch_id = None
            cls._instance._operator = "cli_user"
            cls._instance._loaded_from_db = False
            cls._instance._db_path_cache = None
        return cls._instance

    def reset(self):
        """重置录制管理器（主要用于测试）。"""
        self._active_session_id = None
        self._active_batch_id = None
        self._operator = "cli_user"
        self._loaded_from_db = False
        self._db_path_cache = None

    def _resolve_db_path(self, db_path: Optional[str] = None) -> str:
        """解析实际的数据库路径。"""
        return db_path if db_path is not None else db.get_db_path()

    def _ensure_loaded(self, db_path: Optional[str] = None) -> None:
        """确保活动状态已从数据库加载（懒加载）。

        总是从数据库加载最新状态，保证跨调用一致性。
        如果数据库中没有活动会话，则使用内存状态作为 fallback。
        """
        effective_path = self._resolve_db_path(db_path)

        if self._loaded_from_db and self._db_path_cache == effective_path:
            return

        session_id = _get_active_session_id(effective_path)
        batch_id = _get_active_batch_id(effective_path)

        if session_id is not None:
            session = get_replay_session(session_id, db_path=effective_path)
            if session and session["result"] == "running":
                self._active_session_id = session_id
                self._active_batch_id = batch_id
                self._operator = session.get("operator", "cli_user")
            else:
                self._active_session_id = None
                self._active_batch_id = None
                self._operator = "cli_user"
                _set_active_session_id(None, effective_path)
                _set_active_batch_id(None, effective_path)

        self._loaded_from_db = True
        self._db_path_cache = effective_path

    def _persist_active_state(self, db_path: Optional[str] = None) -> None:
        """将活动状态持久化到数据库。"""
        effective_path = self._resolve_db_path(db_path)
        _set_active_session_id(self._active_session_id, effective_path)
        _set_active_batch_id(self._active_batch_id, effective_path)
        self._db_path_cache = effective_path
        self._loaded_from_db = True

    # ------------------------------------------------------------------
    # 演练生命周期管理
    # ------------------------------------------------------------------

    def begin_drill(
        self,
        name: str,
        description: Optional[str] = None,
        operator: str = "cli_user",
        batch_id: Optional[int] = None,
        batch_name: Optional[str] = None,
        input_summary: Optional[Dict] = None,
        db_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        开始一次演练。

        创建一个新的回放会话，并将其设为当前活动会话。
        后续所有被 @record_operation 装饰的命令都会自动
        追加步骤到这个会话中。

        活动状态持久化到 SQLite，跨进程可恢复。

        Args:
            name: 演练名称
            description: 演练描述
            operator: 操作者
            batch_id: 关联的批次 ID
            batch_name: 关联的批次名称
            input_summary: 输入摘要
            db_path: 数据库路径

        Returns:
            新创建的回放会话字典
        """
        init_replay_db(db_path)
        self._ensure_loaded(db_path)

        if self._active_session_id is not None:
            raise RuntimeError(
                f"已有活动的演练（会话 ID: {self._active_session_id}），"
                f"请先调用 end_drill() 或 undo_drill() 结束当前演练"
            )

        session = start_replay_session(
            name=name,
            description=description,
            operator=operator,
            batch_id=batch_id,
            batch_name=batch_name,
            input_summary=input_summary,
            db_path=db_path,
        )

        self._active_session_id = session["id"]
        self._active_batch_id = batch_id
        self._operator = operator
        self._persist_active_state(db_path)

        add_replay_step(
            session_id=session["id"],
            action="drill_begin",
            description="演练开始",
            result="success",
            detail={
                "session_key": session["session_key"],
                "operator": operator,
            },
            db_path=db_path,
        )

        return session

    def resume_drill(
        self,
        session_id: Optional[int] = None,
        db_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        恢复一个已有的演练会话为活动状态。

        如果不指定 session_id，则尝试恢复数据库中记录的活动会话。
        只有 running 状态的会话可以被恢复。

        Args:
            session_id: 要恢复的会话 ID（可选）
            db_path: 数据库路径

        Returns:
            恢复后的回放会话字典
        """
        init_replay_db(db_path)

        if session_id is not None:
            session = get_replay_session(session_id, db_path=db_path)
            if session is None:
                raise ValueError(f"回放会话不存在: {session_id}")
            if session["result"] != "running":
                raise ValueError(
                    f"仅 running 状态的会话可恢复，当前状态: {session['result']}"
                )
            self._active_session_id = session_id
            self._active_batch_id = session.get("batch_id")
            self._operator = session.get("operator", "cli_user")
            self._persist_active_state(db_path)
            return session
        else:
            self._ensure_loaded(db_path)
            if self._active_session_id is None:
                raise RuntimeError("没有可恢复的活动演练")
            return get_replay_session(self._active_session_id, db_path=db_path)

    def end_drill(
        self,
        result: str = "success",
        error_message: Optional[str] = None,
        db_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        结束当前演练，收口成指定结果。

        Args:
            result: 结果（success/failure/error）
            error_message: 错误信息
            db_path: 数据库路径

        Returns:
            更新后的回放会话字典
        """
        self._ensure_loaded(db_path)

        if self._active_session_id is None:
            raise RuntimeError("当前没有活动的演练，请先调用 begin_drill()")

        session_id = self._active_session_id

        add_replay_step(
            session_id=session_id,
            action="drill_end",
            description="演练结束",
            result=result,
            detail={"final_result": result},
            error_message=error_message,
            db_path=db_path,
        )

        session = finish_replay_session(
            session_id=session_id,
            result=result,
            error_message=error_message,
            db_path=db_path,
        )

        self._active_session_id = None
        self._active_batch_id = None
        self._persist_active_state(db_path)

        return session

    def undo_drill(
        self,
        note: Optional[str] = None,
        db_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        撤销当前演练，标记为 undone。

        Args:
            note: 撤销备注
            db_path: 数据库路径

        Returns:
            更新后的回放会话字典
        """
        self._ensure_loaded(db_path)

        if self._active_session_id is None:
            raise RuntimeError("当前没有活动的演练，请先调用 begin_drill()")

        session_id = self._active_session_id

        add_replay_step(
            session_id=session_id,
            action="drill_undo",
            description="演练撤销",
            result="success",
            detail={"undo_note": note},
            db_path=db_path,
        )

        session = undo_replay_session(
            session_id=session_id,
            note=note,
            db_path=db_path,
        )

        self._active_session_id = None
        self._active_batch_id = None
        self._persist_active_state(db_path)

        return session

    # ------------------------------------------------------------------
    # 活动会话查询
    # ------------------------------------------------------------------

    def get_active_session_id(self, db_path: Optional[str] = None) -> Optional[int]:
        """获取当前活动的演练会话 ID。"""
        self._ensure_loaded(db_path)
        return self._active_session_id

    def get_active_session(
        self,
        db_path: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """获取当前活动的演练会话详情。"""
        self._ensure_loaded(db_path)
        if self._active_session_id is None:
            return None
        return get_replay_session(self._active_session_id, db_path=db_path)

    def is_recording(self, db_path: Optional[str] = None) -> bool:
        """是否正在录制中。"""
        self._ensure_loaded(db_path)
        return self._active_session_id is not None

    def set_active_batch_id(self, batch_id: Optional[int], db_path: Optional[str] = None) -> None:
        """设置当前活动的批次 ID（在 import 后自动关联）。

        同时更新数据库中的活动批次 ID 和会话表中的 batch_id。
        """
        self._ensure_loaded(db_path)
        self._active_batch_id = batch_id

        if self._active_session_id is not None and batch_id is not None:
            try:
                conn = db.connect(db_path)
                try:
                    with conn:
                        conn.execute(
                            "UPDATE replay_sessions SET batch_id = ? WHERE id = ?",
                            (batch_id, self._active_session_id),
                        )
                finally:
                    conn.close()
            except Exception:
                pass

        self._persist_active_state(db_path)

    def get_active_batch_id(self, db_path: Optional[str] = None) -> Optional[int]:
        """获取当前活动的批次 ID。"""
        self._ensure_loaded(db_path)
        return self._active_batch_id

    # ------------------------------------------------------------------
    # 操作步骤录制
    # ------------------------------------------------------------------

    def record_step(
        self,
        action: str,
        description: Optional[str] = None,
        input_args: Optional[Dict] = None,
        result: str = "success",
        detail: Optional[Dict] = None,
        error_message: Optional[str] = None,
        exception: Optional[BaseException] = None,
        batch_id: Optional[int] = None,
        db_path: Optional[str] = None,
    ) -> Optional[int]:
        """
        录制一个操作步骤到当前活动会话。

        如果没有活动会话，静默返回 None（不影响正常命令执行）。

        Args:
            action: 动作名称（如 import, match, review 等）
            description: 步骤描述
            input_args: 输入参数（会自动脱敏）
            result: 步骤结果
            detail: 详情字典
            error_message: 错误信息
            exception: 异常对象
            batch_id: 批次 ID（优先使用，否则使用活动批次 ID）
            db_path: 数据库路径

        Returns:
            步骤 ID，如果没有活动会话则返回 None
        """
        self._ensure_loaded(db_path)

        if self._active_session_id is None:
            return None

        config = get_replay_config(db_path)
        effective_batch_id = batch_id or self._active_batch_id

        detail_dict = dict(detail) if detail else {}

        if input_args and config["detail_enabled"]:
            detail_dict["input_args"] = _mask_dict(
                input_args, config["masked_fields"]
            )

        if effective_batch_id is not None:
            detail_dict["batch_id"] = effective_batch_id

        detail_dict["operator"] = self._operator

        step_id = add_replay_step(
            session_id=self._active_session_id,
            action=action,
            description=description,
            result=result,
            detail=detail_dict,
            error_message=error_message,
            exception=exception,
            db_path=db_path,
        )

        return step_id


# 全局单例实例
_recorder = OperationRecorder()


# ---------------------------------------------------------------------------
# 便捷函数：获取全局录制器
# ---------------------------------------------------------------------------

def get_recorder() -> OperationRecorder:
    """获取全局操作录制器单例。"""
    return _recorder


# ---------------------------------------------------------------------------
# 装饰器：@record_operation
# ---------------------------------------------------------------------------

def record_operation(
    action: Optional[str] = None,
    description: Optional[str] = None,
    auto_detect_batch: bool = True,
):
    """
    装饰器：自动录制 CLI 命令的执行过程。

    将被装饰的函数调用自动记录为当前活动演练的一个步骤。
    自动捕获：
    - 输入参数（脱敏后）
    - 执行结果
    - 异常（类型、消息、堆栈）
    - 批次 ID（从 kwargs 的 --batch 参数获取）

    Args:
        action: 动作名称（默认使用函数名）
        description: 步骤描述
        auto_detect_batch: 是否自动从 kwargs 中检测 --batch 参数

    Usage:
        @cli.command()
        @click.option("--batch", type=int)
        @record_operation(action="match", description="执行匹配")
        def match_cmd(batch):
            ...
    """

    def decorator(func):
        action_name = action or func.__name__
        step_description = description or f"执行命令: {action_name}"

        import functools

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            recorder = get_recorder()

            if not recorder.is_recording():
                return func(*args, **kwargs)

            input_args = _extract_input_args(func, args, kwargs)
            batch_id = kwargs.get("batch") if auto_detect_batch else None

            try:
                result = func(*args, **kwargs)

                recorder.record_step(
                    action=action_name,
                    description=step_description,
                    input_args=input_args,
                    result="success",
                    batch_id=batch_id,
                )

                return result

            except SystemExit as e:
                if e.code == 0 or e.code is None:
                    recorder.record_step(
                        action=action_name,
                        description=step_description,
                        input_args=input_args,
                        result="success",
                        batch_id=batch_id,
                    )
                else:
                    recorder.record_step(
                        action=action_name,
                        description=step_description,
                        input_args=input_args,
                        result="failure",
                        error_message=f"命令退出码: {e.code}",
                        batch_id=batch_id,
                    )
                raise

            except Exception as e:
                recorder.record_step(
                    action=action_name,
                    description=step_description,
                    input_args=input_args,
                    result="error",
                    error_message=str(e),
                    exception=e,
                    batch_id=batch_id,
                )
                raise

        return wrapper

    return decorator


def _extract_input_args(func, args, kwargs) -> Dict[str, Any]:
    """
    从函数调用中提取输入参数，转换为字典。

    处理 click 命令的参数，排除不可序列化的对象。
    """
    import inspect

    try:
        sig = inspect.signature(func)
        params = list(sig.parameters.keys())

        result = {}

        for i, arg in enumerate(args):
            if i < len(params):
                key = params[i]
                result[key] = _make_serializable(arg)

        for key, value in kwargs.items():
            result[key] = _make_serializable(value)

        return result
    except Exception:
        return {}


def _make_serializable(value: Any) -> Any:
    """将值转换为 JSON 可序列化的形式。"""
    if isinstance(value, (str, int, float, bool, type(None))):
        return value
    if isinstance(value, list):
        return [_make_serializable(v) for v in value]
    if isinstance(value, dict):
        return {k: _make_serializable(v) for k, v in value.items()}
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "__class__") and hasattr(value, "__name__"):
        return f"<{value.__class__.__name__}>"
    return str(value)


# ---------------------------------------------------------------------------
# drill 模块的便捷 API（供 CLI 直接调用）
# ---------------------------------------------------------------------------

def begin_drill(
    name: str,
    description: Optional[str] = None,
    operator: str = "cli_user",
    batch_id: Optional[int] = None,
    batch_name: Optional[str] = None,
    input_summary: Optional[Dict] = None,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """开始一次演练。"""
    return get_recorder().begin_drill(
        name=name,
        description=description,
        operator=operator,
        batch_id=batch_id,
        batch_name=batch_name,
        input_summary=input_summary,
        db_path=db_path,
    )


def resume_drill(
    session_id: Optional[int] = None,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """恢复一个演练为活动状态。

    如果不指定 session_id，则尝试恢复数据库中记录的活动会话。
    """
    return get_recorder().resume_drill(
        session_id=session_id,
        db_path=db_path,
    )


def end_drill(
    result: str = "success",
    error_message: Optional[str] = None,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """结束当前演练。"""
    return get_recorder().end_drill(
        result=result,
        error_message=error_message,
        db_path=db_path,
    )


def undo_drill(
    note: Optional[str] = None,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """撤销当前演练。"""
    return get_recorder().undo_drill(
        note=note,
        db_path=db_path,
    )


def get_active_drill(db_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """获取当前活动的演练会话。"""
    return get_recorder().get_active_session(db_path=db_path)


def is_recording(db_path: Optional[str] = None) -> bool:
    """是否正在录制演练。"""
    return get_recorder().is_recording(db_path=db_path)


def set_active_batch_for_drill(batch_id: Optional[int], db_path: Optional[str] = None) -> None:
    """为当前演练设置活动批次 ID，并更新会话表中的 batch_id。"""
    recorder = get_recorder()
    recorder.set_active_batch_id(batch_id, db_path=db_path)

