# -*- coding: utf-8 -*-
"""
审计与异常追踪模块。

所有操作（导入、匹配、复核、撤销、导出、快照恢复、包导入、配置变更）
都留有可查询的操作记录，持久化到 SQLite，跨重启可查。

配置项：
  - retention_days: 审计记录保留天数（默认 365，设为 0 表示永久保留）
  - verbose: 是否记录详细字段（如文件落点、冲突原因、撤销前后状态等）
"""

import csv
import json
import os
import sys
import tempfile
import sqlite3
import traceback
from typing import Optional, List, Dict

from . import db


AUDIT_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL DEFAULT (datetime('now')),
    action TEXT NOT NULL,
    operator TEXT NOT NULL DEFAULT 'system',
    batch_id INTEGER,
    batch_name TEXT,
    match_id INTEGER,
    rule_version TEXT,
    result TEXT NOT NULL DEFAULT 'success',
    detail TEXT,
    error_message TEXT
);

CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action);
CREATE INDEX IF NOT EXISTS idx_audit_batch_id ON audit_log(batch_id);
CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON audit_log(timestamp);
CREATE INDEX IF NOT EXISTS idx_audit_operator ON audit_log(operator);
CREATE INDEX IF NOT EXISTS idx_audit_result ON audit_log(result);

CREATE TABLE IF NOT EXISTS audit_config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

VALID_ACTIONS = {
    "import", "match", "review", "review-undo",
    "revoke", "export", "snapshot-create", "snapshot-restore",
    "pack", "unpack", "config",
}

VALID_RESULTS = {"success", "failure", "blocked", "error"}

DEFAULT_RETENTION_DAYS = 365
DEFAULT_VERBOSE = True

VERBOSE_MINIMAL_FIELDS = (
    "conflict_reason", "undo_before", "undo_after",
    "export_path", "blocked_reason", "error_type",
)


def init_audit_db(db_path: Optional[str] = None) -> None:
    conn = db.connect(db_path)
    try:
        with conn:
            conn.executescript(AUDIT_SCHEMA_V1)
            _migrate_audit_schema(conn)
            _ensure_config_defaults(conn)
    finally:
        conn.close()


def _migrate_audit_schema(conn: sqlite3.Connection) -> None:
    """迁移审计表结构，确保向后兼容。"""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(audit_log)").fetchall()}

    if "batch_name" not in cols:
        conn.execute("ALTER TABLE audit_log ADD COLUMN batch_name TEXT")

    if "error_message" not in cols:
        conn.execute("ALTER TABLE audit_log ADD COLUMN error_message TEXT")

    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_audit_batch_name ON audit_log(batch_name)"
    )

    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_audit_rule_version ON audit_log(rule_version)"
    )


def _ensure_config_defaults(conn: sqlite3.Connection) -> None:
    existing = {r["key"] for r in conn.execute("SELECT key FROM audit_config").fetchall()}
    if "retention_days" not in existing:
        conn.execute(
            "INSERT INTO audit_config (key, value) VALUES (?, ?)",
            ("retention_days", str(DEFAULT_RETENTION_DAYS)),
        )
    if "verbose" not in existing:
        conn.execute(
            "INSERT INTO audit_config (key, value) VALUES (?, ?)",
            ("verbose", "1" if DEFAULT_VERBOSE else "0"),
        )


def get_audit_config(db_path: Optional[str] = None) -> Dict:
    init_audit_db(db_path)
    conn = db.connect(db_path)
    try:
        rows = conn.execute("SELECT key, value FROM audit_config").fetchall()
        config = {}
        for r in rows:
            config[r["key"]] = r["value"]
        ret = int(config.get("retention_days", str(DEFAULT_RETENTION_DAYS)))
        verbose = config.get("verbose", "1") == "1"
        return {"retention_days": ret, "verbose": verbose}
    finally:
        conn.close()


def set_audit_config(
    retention_days: Optional[int] = None,
    verbose: Optional[bool] = None,
    db_path: Optional[str] = None,
) -> Dict:
    init_audit_db(db_path)
    errors = []

    if retention_days is not None:
        if not isinstance(retention_days, int) or retention_days < 0:
            errors.append("retention_days 必须为非负整数")

    if verbose is not None:
        if not isinstance(verbose, bool):
            errors.append("verbose 必须为布尔值")

    if errors:
        raise ValueError("; ".join(errors))

    old_config = get_audit_config(db_path)

    conn = db.connect(db_path)
    try:
        with conn:
            if retention_days is not None:
                conn.execute(
                    "INSERT OR REPLACE INTO audit_config (key, value) VALUES (?, ?)",
                    ("retention_days", str(retention_days)),
                )
            if verbose is not None:
                conn.execute(
                    "INSERT OR REPLACE INTO audit_config (key, value) VALUES (?, ?)",
                    ("verbose", "1" if verbose else "0"),
                )
    finally:
        conn.close()

    new_config = get_audit_config(db_path)

    changed = {}
    if retention_days is not None and old_config.get("retention_days") != retention_days:
        changed["retention_days"] = {
            "old": old_config.get("retention_days"),
            "new": retention_days,
        }
    if verbose is not None and old_config.get("verbose") != verbose:
        changed["verbose"] = {
            "old": old_config.get("verbose"),
            "new": verbose,
        }

    if changed:
        log_audit(
            action="config",
            result="success",
            detail={
                "config_type": "audit_config",
                "changes": changed,
            },
            db_path=db_path,
        )

    return new_config


def log_audit(
    action: str,
    operator: str = "system",
    batch_id: Optional[int] = None,
    batch_name: Optional[str] = None,
    match_id: Optional[int] = None,
    rule_version: Optional[str] = None,
    result: str = "success",
    detail: Optional[Dict] = None,
    error_message: Optional[str] = None,
    exception: Optional[BaseException] = None,
    db_path: Optional[str] = None,
) -> int:
    if action not in VALID_ACTIONS:
        raise ValueError(f"非法操作类型: {action}，合法值: {', '.join(sorted(VALID_ACTIONS))}")

    if result not in VALID_RESULTS:
        raise ValueError(f"非法结果类型: {result}，合法值: {', '.join(sorted(VALID_RESULTS))}")

    init_audit_db(db_path)

    config = get_audit_config(db_path)
    detail_dict = dict(detail) if detail else {}

    if exception is not None:
        detail_dict["error_type"] = type(exception).__name__
        detail_dict["error_traceback"] = traceback.format_exc()
        if error_message is None:
            error_message = str(exception)

    detail_str = None
    if detail_dict:
        if config["verbose"]:
            detail_str = json.dumps(detail_dict, ensure_ascii=False, default=str)
        else:
            minimal = {}
            for k in VERBOSE_MINIMAL_FIELDS:
                if k in detail_dict:
                    minimal[k] = detail_dict[k]
            if minimal:
                detail_str = json.dumps(minimal, ensure_ascii=False, default=str)

    conn = db.connect(db_path)
    try:
        with conn:
            cur = conn.execute(
                "INSERT INTO audit_log "
                "(action, operator, batch_id, batch_name, match_id, "
                "rule_version, result, detail, error_message) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (action, operator, batch_id, batch_name, match_id,
                 rule_version, result, detail_str, error_message),
            )
            record_id = cur.lastrowid
    finally:
        conn.close()

    _auto_cleanup(db_path)
    return record_id


def query_audit(
    batch_id: Optional[int] = None,
    batch_name: Optional[str] = None,
    operator: Optional[str] = None,
    action: Optional[str] = None,
    result: Optional[str] = None,
    time_start: Optional[str] = None,
    time_end: Optional[str] = None,
    limit: int = 100,
    db_path: Optional[str] = None,
) -> List[Dict]:
    init_audit_db(db_path)
    conn = db.connect(db_path)
    try:
        conditions = []
        params: list = []

        if batch_id is not None:
            conditions.append("batch_id = ?")
            params.append(batch_id)
        if batch_name is not None:
            conditions.append("batch_name LIKE ?")
            params.append(f"%{batch_name}%")
        if operator is not None:
            conditions.append("operator = ?")
            params.append(operator)
        if action is not None:
            conditions.append("action = ?")
            params.append(action)
        if result is not None:
            conditions.append("result = ?")
            params.append(result)
        if time_start is not None:
            conditions.append("timestamp >= ?")
            params.append(time_start)
        if time_end is not None:
            conditions.append("timestamp <= ?")
            params.append(time_end)

        where = ""
        if conditions:
            where = "WHERE " + " AND ".join(conditions)

        params.append(limit)
        rows = conn.execute(
            f"SELECT * FROM audit_log {where} ORDER BY id DESC LIMIT ?",
            params,
        ).fetchall()
        return [_audit_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def get_audit_record(record_id: int, db_path: Optional[str] = None) -> Optional[Dict]:
    init_audit_db(db_path)
    conn = db.connect(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM audit_log WHERE id = ?", (record_id,)
        ).fetchone()
        if row is None:
            return None
        return _audit_row_to_dict(row)
    finally:
        conn.close()


def _audit_row_to_dict(row) -> Dict:
    d = dict(row)
    if d.get("detail") and isinstance(d["detail"], str):
        try:
            d["detail"] = json.loads(d["detail"])
        except (json.JSONDecodeError, TypeError):
            pass
    return d


def _auto_cleanup(db_path: Optional[str] = None) -> None:
    try:
        config = get_audit_config(db_path)
        retention = config.get("retention_days", DEFAULT_RETENTION_DAYS)
        if retention <= 0:
            return
        conn = db.connect(db_path)
        try:
            with conn:
                conn.execute(
                    "DELETE FROM audit_log WHERE timestamp < datetime('now', ?)",
                    (f"-{retention} days",),
                )
        finally:
            conn.close()
    except Exception:
        pass


def cleanup_audit(retention_days: Optional[int] = None, db_path: Optional[str] = None) -> int:
    init_audit_db(db_path)
    if retention_days is None:
        config = get_audit_config(db_path)
        retention_days = config.get("retention_days", DEFAULT_RETENTION_DAYS)
    if retention_days <= 0:
        return 0

    conn = db.connect(db_path)
    try:
        with conn:
            cur = conn.execute(
                "DELETE FROM audit_log WHERE timestamp < datetime('now', ?)",
                (f"-{retention_days} days",),
            )
            return cur.rowcount
    finally:
        conn.close()


def export_audit_report(
    output_path: str,
    fmt: str = "csv",
    batch_id: Optional[int] = None,
    batch_name: Optional[str] = None,
    operator: Optional[str] = None,
    action: Optional[str] = None,
    result: Optional[str] = None,
    time_start: Optional[str] = None,
    time_end: Optional[str] = None,
    db_path: Optional[str] = None,
) -> str:
    if fmt not in ("csv", "json"):
        raise ValueError(f"不支持的导出格式: {fmt}，仅支持 csv/json")

    records = query_audit(
        batch_id=batch_id,
        batch_name=batch_name,
        operator=operator,
        action=action,
        result=result,
        time_start=time_start,
        time_end=time_end,
        limit=100000,
        db_path=db_path,
    )

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

    missing_refs = []
    for r in records:
        if r.get("batch_id") and not r.get("batch_name"):
            missing_refs.append(f"记录 #{r['id']}: batch_id={r['batch_id']} 但无批次名")

    if not records:
        warning_detail = "（无匹配的审计记录，将导出空文件）"
        records_with_note = records
    else:
        warning_detail = ""
        records_with_note = []
        for r in records:
            rec = dict(r)
            detail = rec.get("detail")
            if isinstance(detail, dict):
                if not rec.get("batch_name") and rec.get("batch_id"):
                    detail["_note"] = "关联批次数据缺失，仅保留审计快照"
                rec["detail"] = detail
            records_with_note.append(rec)

    if fmt == "csv":
        headers = [
            "id", "timestamp", "action", "operator",
            "batch_id", "batch_name", "match_id",
            "rule_version", "result", "detail", "error_message",
        ]
        rows = []
        for r in records_with_note:
            detail_val = r.get("detail")
            if isinstance(detail_val, (dict, list)):
                detail_str = json.dumps(detail_val, ensure_ascii=False, default=str)
            elif detail_val is not None:
                detail_str = str(detail_val)
            else:
                detail_str = ""
            rows.append([
                r.get("id", ""),
                r.get("timestamp", ""),
                r.get("action", ""),
                r.get("operator", ""),
                r.get("batch_id", ""),
                r.get("batch_name", ""),
                r.get("match_id", ""),
                r.get("rule_version", ""),
                r.get("result", ""),
                detail_str,
                r.get("error_message", ""),
            ])
        _write_csv_atomic(abs_path, headers, rows)
    else:
        export_data = []
        for r in records_with_note:
            rec = {}
            for k, v in r.items():
                if isinstance(v, (dict, list, str, int, float, bool)) or v is None:
                    rec[k] = v
                else:
                    rec[k] = str(v)
            export_data.append(rec)
        _write_json_atomic(abs_path, export_data)

    return abs_path


def _write_csv_atomic(abs_path: str, headers: list, rows: list) -> None:
    dir_path = os.path.dirname(abs_path) or "."
    fd, tmp_path = tempfile.mkstemp(dir=dir_path, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if headers:
                writer.writerow(headers)
            writer.writerows(rows)
        os.replace(tmp_path, abs_path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _write_json_atomic(abs_path: str, data: list) -> None:
    dir_path = os.path.dirname(abs_path) or "."
    fd, tmp_path = tempfile.mkstemp(dir=dir_path, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, default=str)
        os.replace(tmp_path, abs_path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
