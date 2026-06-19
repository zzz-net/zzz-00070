"""
导入前预检与变更计划模块。

提供 dry-run / plan 模式，让用户在真正写入数据库前
就能看清发票、付款和可搬运包会带来什么变化。

设计原则：
- 预检不改数据库、不生成快照、不留半截文件
- 真实导入复用同一套校验逻辑，避免预检通过但落库失败
"""

import os
from typing import Optional, List, Dict
from pathlib import Path

from . import db, validators, snapshot, pack
from .models import BatchStatus


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

        # 批次相关
        self.batch_name = ""
        self.was_renamed = False
        self.original_name = ""
        self.rule_version = ""

        # 记录数量
        self.invoice_count = 0
        self.payment_count = 0
        self.match_count = 0
        self.adjudication_count = 0
        self.bad_invoice_count = 0
        self.bad_payment_count = 0
        self.conflict_match_count = 0

        # 状态
        self.batch_status = ""

        # 文件相关
        self.snapshot_file: Optional[str] = None
        self.export_file: Optional[str] = None
        self.package_file: Optional[str] = None

        # 沿用原状态的记录数
        self.preserved_count = 0

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
    - 不改数据库、不写文件

    Args:
        invoices_path: 发票 CSV 文件路径
        payments_path: 付款 CSV 文件路径
        name: 批次名称（可选）
        db_path: 数据库路径（可选）

    Returns:
        PlanResult 预检结果
    """
    result = PlanResult()
    result.operation = "import"

    # 1. 解析发票（与真实导入完全相同的解析逻辑）
    try:
        inv_result = validators.parse_invoices(invoices_path)
    except validators.ValidationError as e:
        result.success = False
        result.errors.extend([f"发票文件格式错误: {x}" for x in e.errors])
        return result

    # 2. 解析付款（与真实导入完全相同的解析逻辑）
    try:
        pay_result = validators.parse_payments(payments_path)
    except validators.ValidationError as e:
        result.success = False
        result.errors.extend([f"付款文件格式错误: {x}" for x in e.errors])
        return result

    # 3. 检查两边都无合法数据
    if not inv_result.has_items and not pay_result.has_items:
        result.success = False
        result.errors.append("发票和付款文件均无合法数据")
        return result

    # 4. 获取当前规则版本
    rule = db.get_current_rule(db_path=db_path)
    result.rule_version = rule.version

    # 5. 计算批次名（处理同名冲突，与真实导入同一套命名逻辑）
    base_name = name if name else f"batch_{rule.version}"
    final_name = snapshot._resolve_batch_name(base_name, db_path=db_path)
    result.batch_name = final_name
    result.was_renamed = final_name != base_name
    result.original_name = base_name

    if result.was_renamed:
        result.warnings.append(
            f"同名批次已存在，将自动重命名为: {final_name}"
        )

    # 6. 统计记录数
    result.invoice_count = len(inv_result.items)
    result.payment_count = len(pay_result.items)
    result.bad_invoice_count = len(inv_result.errors)
    result.bad_payment_count = len(pay_result.errors)
    result.batch_status = BatchStatus.IMPORTED

    # 7. 坏行警告
    if inv_result.errors:
        result.warnings.append(
            f"发票文件有 {len(inv_result.errors)} 行坏数据将被跳过"
        )
    if pay_result.errors:
        result.warnings.append(
            f"付款文件有 {len(pay_result.errors)} 行坏数据将被跳过"
        )

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
    - 检查快照文件是否会冲突
    - 统计记录数量
    - 预估快照文件和导出文件的位置
    - 不改数据库、不生成快照文件

    Args:
        package_path: 可搬运包文件路径
        batch_name: 新批次名称（可选）
        force: 是否强制覆盖已存在的快照文件
        db_path: 数据库路径（可选）

    Returns:
        PlanResult 预检结果
    """
    result = PlanResult()
    result.operation = "unpack"
    result.package_file = package_path

    # 1. 校验包完整性（与真实导入完全相同的校验逻辑）
    verify_result = pack.verify_package(package_path)
    if not verify_result["valid"]:
        result.success = False
        result.errors.extend(verify_result["errors"])
        result.warnings.extend(verify_result["warnings"])
        return result

    result.warnings.extend(verify_result["warnings"])

    # 2. 读取包内元数据（不解包到数据库）
    pkg = Path(package_path)
    import tempfile
    import json
    import zipfile

    with tempfile.TemporaryDirectory(prefix="inv_recon_plan_") as tmpdir:
        tmp = Path(tmpdir)
        with zipfile.ZipFile(pkg, "r") as zf:
            zf.extractall(tmp)

        with open(tmp / "manifest.json", encoding="utf-8") as f:
            manifest = json.load(f)
        with open(tmp / "snapshot.json", encoding="utf-8") as f:
            snap_data = json.load(f)

        # 3. 计算批次名（处理同名冲突）
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

        # 4. 规则版本
        result.rule_version = snap_data["batch"]["rule_version"]

        # 5. 记录统计
        result.invoice_count = len(snap_data.get("invoices", []))
        result.payment_count = len(snap_data.get("payments", []))
        result.match_count = len(snap_data.get("matches", []))
        result.adjudication_count = len(snap_data.get("adjudications", []))
        result.batch_status = snap_data["batch"]["status"]

        # 6. 沿用原状态的裁决记录数
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

        # 7. 预估快照文件位置
        snap_dir = snapshot.get_snapshot_dir()
        snap_file_name = snapshot._snapshot_filename(
            snap_data["snapshot_id"],
            final_name + "_imported"
        )
        snap_target = snap_dir / snap_file_name

        # 检查快照文件是否会冲突
        if snap_target.exists() and not force:
            result.warnings.append(
                f"快照文件已存在: {snap_target}，导入时需使用 --force 覆盖"
            )
        result.snapshot_file = str(snap_target)

        # 8. 预估导出文件位置（如果包内有导出结果）
        if manifest.get("includes_export") and (tmp / "export.csv").exists():
            export_path = snap_dir / f"{final_name}_export.csv"
            result.export_file = str(export_path)

    return result
