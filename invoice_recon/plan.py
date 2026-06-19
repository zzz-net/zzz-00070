"""
导入前预检与变更计划模块。

提供 dry-run / plan 模式，让用户在真正写入数据库前
就能看清发票、付款和可搬运包会带来什么变化。

设计原则：
- 预检不改数据库、不生成快照、不留半截文件
- 预检覆盖：数据格式 + 快照目录 + 导出目录 + 包解压落盘位置 + 同名批次改名
- 真实导入复用同一套校验逻辑，避免预检通过但落库失败
"""

import os
import tempfile
import json
import zipfile
from typing import Optional, List, Dict
from pathlib import Path

from . import db, validators, snapshot, pack
from .models import BatchStatus


def _check_dir_writable(dir_path: Path, label: str) -> List[str]:
    """检查目录是否可写。返回错误列表（空表示可写）。"""
    errors: List[str] = []
    if dir_path.exists():
        if not os.access(str(dir_path), os.W_OK):
            errors.append(f"{label}不可写: {dir_path}")
    else:
        parent = dir_path.parent
        while not parent.exists() and parent != parent.parent:
            parent = parent.parent
        if parent.exists() and not os.access(str(parent), os.W_OK):
            errors.append(f"{label}的父目录不可写，无法自动创建: {dir_path}")
    return errors


def _check_file_writable(file_path: Path, label: str, force: bool = False) -> List[str]:
    """检查目标文件是否可写（存在时是否可覆盖）。返回错误列表。"""
    errors: List[str] = []
    if file_path.exists():
        if not force and not os.access(str(file_path), os.W_OK):
            errors.append(f"{label}已存在且不可写: {file_path}")
    else:
        parent = file_path.parent
        dir_errors = _check_dir_writable(parent, f"{label}所在目录")
        errors.extend(dir_errors)
    return errors


def _probe_write(dir_path: Path) -> bool:
    """在目录中尝试写入并删除临时文件，验证是否真正可写。"""
    if not dir_path.exists():
        return True
    try:
        fd, tmp = tempfile.mkstemp(dir=str(dir_path), prefix=".inv_recon_probe_")
        os.close(fd)
        os.unlink(tmp)
        return True
    except (OSError, PermissionError):
        return False


class PlanResult:
    """预检结果数据类。

    包含所有预计算的信息，供 CLI 层输出。
    """

    def __init__(self):
        self.success = True
        self.errors: List[str] = []
        self.warnings: List[str] = []
        self.operation = ""
        self.is_dry_run = True

        self.batch_name = ""
        self.was_renamed = False
        self.original_name = ""
        self.rule_version = ""

        self.invoice_count = 0
        self.payment_count = 0
        self.match_count = 0
        self.adjudication_count = 0
        self.bad_invoice_count = 0
        self.bad_payment_count = 0
        self.conflict_match_count = 0

        self.batch_status = ""

        self.snapshot_file: Optional[str] = None
        self.export_file: Optional[str] = None
        self.package_file: Optional[str] = None

        self.preserved_count = 0

        self.db_path_resolved: Optional[str] = None
        self.snapshot_dir: Optional[str] = None
        self.unpack_tmp_dir: Optional[str] = None
        self.files_to_create: List[str] = []
        self.dirs_to_create: List[str] = []
        self.writable_ok = True
        self.writable_errors: List[str] = []
        self.conflict_details: List[str] = []

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "errors": self.errors,
            "warnings": self.warnings,
            "operation": self.operation,
            "is_dry_run": self.is_dry_run,
            "batch_name": self.batch_name,
            "was_renamed": self.was_renamed,
            "original_name": self.original_name,
            "rule_version": self.rule_version,
            "invoice_count": self.invoice_count,
            "payment_count": self.payment_count,
            "match_count": self.match_count,
            "adjudication_count": self.adjudication_count,
            "bad_invoice_count": self.bad_invoice_count,
            "bad_payment_count": self.bad_payment_count,
            "conflict_match_count": self.conflict_match_count,
            "batch_status": self.batch_status,
            "snapshot_file": self.snapshot_file,
            "export_file": self.export_file,
            "package_file": self.package_file,
            "preserved_count": self.preserved_count,
            "db_path_resolved": self.db_path_resolved,
            "snapshot_dir": self.snapshot_dir,
            "unpack_tmp_dir": self.unpack_tmp_dir,
            "files_to_create": self.files_to_create,
            "dirs_to_create": self.dirs_to_create,
            "writable_ok": self.writable_ok,
            "writable_errors": self.writable_errors,
            "conflict_details": self.conflict_details,
        }


def plan_import(
    invoices_path: str,
    payments_path: str,
    name: Optional[str] = None,
    db_path: Optional[str] = None,
) -> PlanResult:
    """预检 CSV 导入会产生什么变化。

    - 解析并校验 CSV 文件（与真实导入用同一套解析逻辑）
    - 计算最终批次名（处理同名冲突）
    - 统计合法记录数和坏行数
    - 获取当前规则版本
    - 检查数据库目录可写性
    - 计算将落盘的文件和目录
    - 不改数据库、不写文件
    """
    result = PlanResult()
    result.operation = "import"

    resolved_db = db_path or db.get_db_path()
    result.db_path_resolved = resolved_db

    db_dir = str(Path(resolved_db).parent.resolve())

    try:
        inv_result = validators.parse_invoices(invoices_path)
    except validators.ValidationError as e:
        result.success = False
        result.errors.extend([f"发票文件格式错误: {x}" for x in e.errors])
        return result

    try:
        pay_result = validators.parse_payments(payments_path)
    except validators.ValidationError as e:
        result.success = False
        result.errors.extend([f"付款文件格式错误: {x}" for x in e.errors])
        return result

    if not inv_result.has_items and not pay_result.has_items:
        result.success = False
        result.errors.append("发票和付款文件均无合法数据")
        return result

    rule = db.get_current_rule(db_path=db_path)
    result.rule_version = rule.version

    base_name = name if name else f"batch_{rule.version}"
    final_name = snapshot._resolve_batch_name(base_name, db_path=db_path)
    result.batch_name = final_name
    result.was_renamed = final_name != base_name
    result.original_name = base_name

    if result.was_renamed:
        result.warnings.append(
            f"同名批次已存在，将自动重命名为: {final_name}"
        )

    result.invoice_count = len(inv_result.items)
    result.payment_count = len(pay_result.items)
    result.bad_invoice_count = len(inv_result.errors)
    result.bad_payment_count = len(pay_result.errors)
    result.batch_status = BatchStatus.IMPORTED

    if inv_result.errors:
        result.warnings.append(
            f"发票文件有 {len(inv_result.errors)} 行坏数据将被跳过"
        )
    if pay_result.errors:
        result.warnings.append(
            f"付款文件有 {len(pay_result.errors)} 行坏数据将被跳过"
        )

    result.files_to_create.append(resolved_db)
    if not Path(resolved_db).exists():
        result.dirs_to_create.append(db_dir)

    db_path_obj = Path(resolved_db)
    if db_path_obj.exists():
        if not os.access(str(db_path_obj), os.W_OK):
            result.writable_ok = False
            result.writable_errors.append(f"数据库文件不可写: {resolved_db}")
        if not _probe_write(db_path_obj.parent):
            result.writable_ok = False
            result.writable_errors.append(f"数据库所在目录不可写: {db_path_obj.parent}")
    else:
        dir_errs = _check_dir_writable(db_path_obj.parent, "数据库目录")
        if dir_errs:
            result.writable_ok = False
            result.writable_errors.extend(dir_errs)

    if not result.writable_ok:
        result.success = False
        for e in result.writable_errors:
            result.errors.append(e)

    return result


def plan_unpack(
    package_path: str,
    batch_name: Optional[str] = None,
    force: bool = False,
    db_path: Optional[str] = None,
) -> PlanResult:
    """预检包导入会产生什么变化。

    - 校验包完整性（与真实导入同一套校验逻辑）
    - 计算最终批次名（处理同名冲突）
    - 检查快照目录和导出目录可写性
    - 计算包解压后快照文件和导出文件的落盘位置
    - 检测文件冲突
    - 不改数据库、不生成快照文件
    """
    result = PlanResult()
    result.operation = "unpack"
    result.package_file = package_path

    resolved_db = db_path or db.get_db_path()
    result.db_path_resolved = resolved_db

    verify_result = pack.verify_package(package_path)
    if not verify_result["valid"]:
        result.success = False
        result.errors.extend(verify_result["errors"])
        result.warnings.extend(verify_result["warnings"])
        return result

    result.warnings.extend(verify_result["warnings"])

    pkg = Path(package_path)

    with tempfile.TemporaryDirectory(prefix="inv_recon_plan_") as tmpdir:
        tmp = Path(tmpdir)
        with zipfile.ZipFile(pkg, "r") as zf:
            zf.extractall(tmp)

        with open(tmp / "manifest.json", encoding="utf-8") as f:
            manifest = json.load(f)
        with open(tmp / "snapshot.json", encoding="utf-8") as f:
            snap_data = json.load(f)

        original_batch_name = snap_data["batch"]["name"]
        base_name = batch_name or original_batch_name
        final_name = snapshot._resolve_batch_name(base_name, db_path=db_path)
        result.batch_name = final_name
        result.was_renamed = final_name != base_name
        result.original_name = base_name

        if result.was_renamed:
            result.warnings.append(
                f"同名批次已存在，将自动重命名为: {final_name}"
            )

        result.rule_version = snap_data["batch"]["rule_version"]

        result.invoice_count = len(snap_data.get("invoices", []))
        result.payment_count = len(snap_data.get("payments", []))
        result.match_count = len(snap_data.get("matches", []))
        result.adjudication_count = len(snap_data.get("adjudications", []))
        result.batch_status = snap_data["batch"]["status"]

        preserved = 0
        conflict_count = 0
        for m in snap_data.get("matches", []):
            status = m.get("status", "")
            if status in ("confirmed", "rejected"):
                preserved += 1
            if status == "conflict":
                conflict_count += 1
        result.preserved_count = preserved
        result.conflict_match_count = conflict_count

        snap_dir = snapshot.get_snapshot_dir()
        result.snapshot_dir = str(snap_dir)

        snap_file_name = snapshot._snapshot_filename(
            snap_data["snapshot_id"],
            final_name + "_imported"
        )
        snap_target = snap_dir / snap_file_name

        if snap_target.exists() and not force:
            result.warnings.append(
                f"快照文件已存在: {snap_target}，导入时需使用 --force 覆盖"
            )
            result.conflict_details.append(
                f"快照文件冲突: {snap_target}（已存在，需 --force）"
            )
        result.snapshot_file = str(snap_target)
        result.files_to_create.append(str(snap_target))

        if not snap_dir.exists():
            result.dirs_to_create.append(str(snap_dir))

        if manifest.get("includes_export") and (tmp / "export.csv").exists():
            export_path = snap_dir / f"{final_name}_export.csv"
            result.export_file = str(export_path)
            result.files_to_create.append(str(export_path))
            if export_path.exists() and not force:
                result.warnings.append(
                    f"导出结果文件已存在: {export_path}，导入时将被跳过"
                )
                result.conflict_details.append(
                    f"导出结果文件冲突: {export_path}（已存在，将被跳过）"
                )

        result.unpack_tmp_dir = tempfile.gettempdir()

        fs_errors: List[str] = []
        if not snap_dir.exists():
            parent_errs = _check_dir_writable(snap_dir.parent, "快照目录的父目录")
            fs_errors.extend(parent_errs)
        else:
            if not _probe_write(snap_dir):
                fs_errors.append(f"快照目录不可写: {snap_dir}")

        db_path_obj = Path(resolved_db)
        if db_path_obj.exists():
            if not os.access(str(db_path_obj), os.W_OK):
                fs_errors.append(f"数据库文件不可写: {resolved_db}")
            if not _probe_write(db_path_obj.parent):
                fs_errors.append(f"数据库所在目录不可写: {db_path_obj.parent}")
        else:
            dir_errs = _check_dir_writable(db_path_obj.parent, "数据库目录")
            fs_errors.extend(dir_errs)

        tmp_dir = Path(tempfile.gettempdir())
        if not _probe_write(tmp_dir):
            fs_errors.append(f"临时目录不可写: {tmp_dir}")

        if fs_errors:
            result.writable_ok = False
            result.writable_errors.extend(fs_errors)
            result.success = False
            for e in fs_errors:
                result.errors.append(e)

    return result
