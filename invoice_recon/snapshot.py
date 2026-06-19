"""
批次快照与恢复模块。

快照是一个 JSON 文件，包含某个批次的完整状态：
- 批次基本信息 (batches)
- 使用的规则版本 (rule_versions)
- 发票数据 (invoices)
- 付款数据 (payments)
- 匹配记录 (matches)
- 裁决历史 (adjudications)

恢复时，快照作为全新批次导入，所有 ID 重新分配，
不覆盖现有任何批次数据。
"""

import os
import json
import uuid
import datetime
from typing import Optional, List, Dict
from pathlib import Path

from . import db
from .models import BatchStatus


DEFAULT_SNAPSHOT_DIR = "snapshots"
SNAPSHOT_FILE_EXT = ".snap.json"


def get_snapshot_dir() -> Path:
    """获取快照目录，可通过 INV_RECON_SNAPSHOT_DIR 环境变量覆盖。"""
    custom = os.environ.get("INV_RECON_SNAPSHOT_DIR")
    if custom:
        return Path(custom)
    return Path(DEFAULT_SNAPSHOT_DIR)


def _ensure_snapshot_dir() -> Path:
    d = get_snapshot_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


def _sanitize_filename(name: str) -> str:
    """清理文件名，移除不安全字符。"""
    safe = []
    for c in name:
        if c.isalnum() or c in ("-", "_", "."):
            safe.append(c)
        else:
            safe.append("_")
    return "".join(safe) or "snapshot"


def _snapshot_filename(snap_id: str, name: str) -> str:
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_name = _sanitize_filename(name)
    return f"{ts}_{safe_name}_{snap_id[:8]}{SNAPSHOT_FILE_EXT}"


def _load_snapshot_file(filepath: Path) -> dict:
    with open(filepath, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_snapshot_file(filepath: Path, data: dict) -> None:
    tmp = filepath.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)
    os.replace(tmp, filepath)


def create_snapshot(batch_id: int, name: Optional[str] = None,
                    db_path: Optional[str] = None) -> dict:
    """为指定批次创建快照。

    返回快照元数据字典。
    """
    batch = db.get_batch_raw(batch_id, db_path=db_path)
    if batch is None:
        raise ValueError(f"批次 {batch_id} 不存在")

    rule = db.get_rule_version_raw(batch["rule_version"], db_path=db_path)
    invoices = db.get_invoices_raw_by_batch(batch_id, db_path=db_path)
    payments = db.get_payments_raw_by_batch(batch_id, db_path=db_path)
    matches = db.get_matches_raw_by_batch(batch_id, db_path=db_path)
    adjudications = db.get_adjudications_by_batch(batch_id, db_path=db_path)

    snap_id = uuid.uuid4().hex
    snap_name = name or f"{batch['name']}_snap"
    created_at = datetime.datetime.now().isoformat()

    snapshot_data = {
        "snapshot_id": snap_id,
        "snapshot_name": snap_name,
        "created_at": created_at,
        "source_batch_id": batch_id,
        "source_batch_name": batch["name"],
        "schema_version": 1,
        "rule_version": rule,
        "batch": batch,
        "invoices": invoices,
        "payments": payments,
        "matches": matches,
        "adjudications": adjudications,
    }

    snap_dir = _ensure_snapshot_dir()
    filename = _snapshot_filename(snap_id, snap_name)
    filepath = snap_dir / filename
    _save_snapshot_file(filepath, snapshot_data)

    return {
        "snapshot_id": snap_id,
        "snapshot_name": snap_name,
        "created_at": created_at,
        "source_batch_id": batch_id,
        "source_batch_name": batch["name"],
        "file": str(filepath),
        "invoice_count": len(invoices),
        "payment_count": len(payments),
        "match_count": len(matches),
        "adjudication_count": len(adjudications),
        "batch_status": batch["status"],
        "rule_version": batch["rule_version"],
    }


def list_snapshots() -> List[dict]:
    """列出所有快照，按创建时间倒序。"""
    snap_dir = get_snapshot_dir()
    if not snap_dir.exists():
        return []

    snapshots = []
    for f in snap_dir.glob(f"*{SNAPSHOT_FILE_EXT}"):
        try:
            data = _load_snapshot_file(f)
            snapshots.append({
                "snapshot_id": data["snapshot_id"],
                "snapshot_name": data["snapshot_name"],
                "created_at": data["created_at"],
                "source_batch_id": data.get("source_batch_id"),
                "source_batch_name": data.get("source_batch_name"),
                "file": str(f),
                "batch_status": data["batch"]["status"],
                "rule_version": data["batch"]["rule_version"],
                "match_count": len(data.get("matches", [])),
                "invoice_count": len(data.get("invoices", [])),
                "payment_count": len(data.get("payments", [])),
            })
        except (json.JSONDecodeError, KeyError):
            continue

    snapshots.sort(key=lambda s: s["created_at"], reverse=True)
    return snapshots


def _find_snapshot_file(snapshot_ref: str) -> Optional[Path]:
    """根据快照 ID 或名称查找快照文件。"""
    snap_dir = get_snapshot_dir()
    if not snap_dir.exists():
        return None

    for f in snap_dir.glob(f"*{SNAPSHOT_FILE_EXT}"):
        try:
            data = _load_snapshot_file(f)
            if data.get("snapshot_id") == snapshot_ref:
                return f
            if data.get("snapshot_name") == snapshot_ref:
                return f
            if data.get("snapshot_id", "").startswith(snapshot_ref):
                return f
        except (json.JSONDecodeError, KeyError):
            continue
    return None


def get_snapshot(snapshot_ref: str) -> Optional[dict]:
    """获取快照的完整数据。

    snapshot_ref 可以是完整 ID、ID 前缀或快照名称。
    """
    f = _find_snapshot_file(snapshot_ref)
    if f is None:
        return None
    return _load_snapshot_file(f)


def get_snapshot_info(snapshot_ref: str) -> Optional[dict]:
    """获取快照元信息（不加载完整数据，用于列表显示）。"""
    data = get_snapshot(snapshot_ref)
    if data is None:
        return None
    return {
        "snapshot_id": data["snapshot_id"],
        "snapshot_name": data["snapshot_name"],
        "created_at": data["created_at"],
        "source_batch_id": data.get("source_batch_id"),
        "source_batch_name": data.get("source_batch_name"),
        "batch_status": data["batch"]["status"],
        "rule_version": data["batch"]["rule_version"],
        "invoice_count": len(data.get("invoices", [])),
        "payment_count": len(data.get("payments", [])),
        "match_count": len(data.get("matches", [])),
        "adjudication_count": len(data.get("adjudications", [])),
    }


def restore_snapshot(snapshot_ref: str, new_batch_name: Optional[str] = None,
                     db_path: Optional[str] = None) -> dict:
    """将快照恢复为新批次。

    - 总是作为新批次导入（不覆盖现有批次）
    - 所有 ID 重新分配，状态链路完整保留
    - 规则版本如不存在则创建
    - 同名批次：自动在名称后加编号

    返回新批次的信息字典。
    """
    snap = get_snapshot(snapshot_ref)
    if snap is None:
        raise ValueError(f"快照 {snapshot_ref} 不存在")

    snap_batch = snap["batch"]
    snap_rule = snap.get("rule_version")
    snap_invoices = snap.get("invoices", [])
    snap_payments = snap.get("payments", [])
    snap_matches = snap.get("matches", [])
    snap_adjs = snap.get("adjudications", [])

    # 决定新批次名称
    base_name = new_batch_name or snap_batch["name"]
    final_name = _resolve_batch_name(base_name, db_path=db_path)

    conn = db.connect(db_path)
    try:
        with conn:
            # 1. 确保规则版本存在
            if snap_rule:
                existing = conn.execute(
                    "SELECT version FROM rule_versions WHERE version = ?",
                    (snap_rule["version"],),
                ).fetchone()
                if existing is None:
                    conn.execute(
                        "INSERT INTO rule_versions (version, tolerance, require_vendor_match, created_at) "
                        "VALUES (?, ?, ?, ?)",
                        (
                            snap_rule["version"],
                            snap_rule["tolerance"],
                            snap_rule.get("require_vendor_match", 1),
                            snap_rule.get("created_at"),
                        ),
                    )

            # 2. 插入新批次
            cur = conn.execute(
                "INSERT INTO batches (name, status, rule_version, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    final_name,
                    snap_batch["status"],
                    snap_batch["rule_version"],
                    snap_batch.get("created_at"),
                    snap_batch.get("updated_at"),
                ),
            )
            new_batch_id = cur.lastrowid

            # 3. 插入发票，构建 ID 映射
            inv_map: Dict[int, int] = {}
            if snap_invoices:
                for inv in snap_invoices:
                    cur = conn.execute(
                        "INSERT INTO invoices (batch_id, invoice_no, vendor, amount, date) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (new_batch_id, inv["invoice_no"], inv["vendor"],
                         inv["amount"], inv["date"]),
                    )
                    inv_map[inv["id"]] = cur.lastrowid

            # 4. 插入付款，构建 ID 映射
            pay_map: Dict[int, int] = {}
            if snap_payments:
                for pay in snap_payments:
                    cur = conn.execute(
                        "INSERT INTO payments (batch_id, payment_no, vendor, amount, date) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (new_batch_id, pay["payment_no"], pay["vendor"],
                         pay["amount"], pay["date"]),
                    )
                    pay_map[pay["id"]] = cur.lastrowid

            # 5. 插入匹配记录，构建 ID 映射
            match_map: Dict[int, int] = {}
            if snap_matches:
                for m in snap_matches:
                    new_inv_id = inv_map.get(m["invoice_id"]) if m.get("invoice_id") else None
                    new_pay_id = pay_map.get(m["payment_id"]) if m.get("payment_id") else None
                    cur = conn.execute(
                        "INSERT INTO matches "
                        "(batch_id, invoice_id, payment_id, match_type, amount_diff, "
                        "status, review_note, adjudication) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            new_batch_id, new_inv_id, new_pay_id,
                            m["match_type"], m["amount_diff"],
                            m["status"], m.get("review_note"), m.get("adjudication"),
                        ),
                    )
                    match_map[m["id"]] = cur.lastrowid

            # 6. 插入裁决历史（保留状态链路）
            if snap_adjs:
                adj_rows = []
                for a in snap_adjs:
                    new_match_id = match_map.get(a["match_id"]) if a.get("match_id") else None
                    adj_rows.append((
                        new_match_id,
                        new_batch_id,
                        a["action"],
                        a.get("note"),
                        a.get("prev_status"),
                        a.get("prev_note"),
                    ))
                conn.executemany(
                    "INSERT INTO adjudications (match_id, batch_id, action, note, prev_status, prev_note) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    adj_rows,
                )

        new_batch = db.get_batch_raw(new_batch_id, db_path=db_path)
        return {
            "new_batch_id": new_batch_id,
            "new_batch_name": final_name,
            "status": new_batch["status"],
            "rule_version": new_batch["rule_version"],
            "invoice_count": len(inv_map),
            "payment_count": len(pay_map),
            "match_count": len(match_map),
            "adjudication_count": len(snap_adjs),
            "was_renamed": final_name != base_name,
            "original_name": base_name,
        }
    finally:
        conn.close()


def _resolve_batch_name(base_name: str, db_path: Optional[str] = None) -> str:
    """如果同名批次已存在，自动在名称后加编号。"""
    existing = _get_existing_batch_names(db_path=db_path)
    if base_name not in existing:
        return base_name

    i = 2
    while True:
        candidate = f"{base_name}_{i}"
        if candidate not in existing:
            return candidate
        i += 1


def _get_existing_batch_names(db_path: Optional[str] = None) -> set:
    conn = db.connect(db_path)
    try:
        rows = conn.execute("SELECT name FROM batches").fetchall()
        return {r["name"] for r in rows}
    finally:
        conn.close()


def delete_snapshot(snapshot_ref: str) -> bool:
    """删除快照文件。返回是否成功删除。"""
    f = _find_snapshot_file(snapshot_ref)
    if f is None:
        return False
    f.unlink()
    return True
