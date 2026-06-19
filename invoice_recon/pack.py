"""
批次快照打包与解包模块 — 跨机器/目录搬运。

包格式 (.invpkg):
  ZIP 压缩包，内部结构:
    manifest.json    - 元数据（打包时间、工具版本、内容哈希等）
    snapshot.json    - 完整快照数据（批次+规则+发票+付款+匹配+裁决历史）
    checksums.json   - 各文件 SHA256 校验和
    export.csv       - 可选，最近一次导出结果（若批次已导出过）

冲突处理:
  - 同名批次: 自动加 _2、_3 后缀重命名
  - 目标文件已存在: 拒绝覆盖，需 --force
  - 快照目录缺失: 自动创建
  - 包内容不完整: 拒绝导入，列出缺失项
"""

import os
import json
import zipfile
import hashlib
import datetime
import tempfile
import shutil
import platform
from typing import Optional, List, Dict, Tuple
from pathlib import Path

from . import db, snapshot, export
from .models import BatchStatus


PACKAGE_EXT = ".invpkg"
PACKAGE_SCHEMA_VERSION = 1
TOOL_VERSION = "1.0.0"

REQUIRED_FILES = ["manifest.json", "snapshot.json", "checksums.json"]


def _sha256_file(filepath: Path) -> str:
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _sanitize_filename(name: str) -> str:
    safe = []
    for c in name:
        if c.isalnum() or c in ("-", "_", "."):
            safe.append(c)
        else:
            safe.append("_")
    return "".join(safe) or "package"


def pack_batch(batch_id: int, output_path: Optional[str] = None,
               package_name: Optional[str] = None,
               include_export: bool = True,
               force: bool = False,
               db_path: Optional[str] = None) -> dict:
    """将指定批次打包为可搬运包。

    Args:
        batch_id: 批次 ID
        output_path: 输出包文件路径（可选，自动生成时用）
        package_name: 包名称（可选，默认基于批次名）
        include_export: 是否包含最近一次导出结果
        force: 输出文件已存在时是否强制覆盖
        db_path: 数据库路径（可选）

    Returns:
        打包结果字典
    """
    batch = db.get_batch_raw(batch_id, db_path=db_path)
    if batch is None:
        raise ValueError(f"批次 {batch_id} 不存在")

    snap_info = snapshot.create_snapshot(batch_id, name=package_name, db_path=db_path)
    snap_file = Path(snap_info["file"])
    snap_data = snapshot._load_snapshot_file(snap_file)

    with tempfile.TemporaryDirectory(prefix="inv_recon_pack_") as tmpdir:
        tmp = Path(tmpdir)

        snap_target = tmp / "snapshot.json"
        shutil.copy2(snap_file, snap_target)

        export_target = None
        if include_export and batch["status"] in (
            BatchStatus.REVIEWED, BatchStatus.EXPORTED
        ):
            matches = db.get_matches_by_batch(batch_id, db_path=db_path)
            diff_matches = [m for m in matches if _should_pack_export(m)]
            if diff_matches:
                export_target = tmp / "export.csv"
                export.export_differences(str(export_target), diff_matches)

        manifest = {
            "package_version": "1.0",
            "schema_version": PACKAGE_SCHEMA_VERSION,
            "tool_version": TOOL_VERSION,
            "created_at": datetime.datetime.now().isoformat(),
            "source_machine": platform.node(),
            "source_batch_id": batch_id,
            "source_batch_name": batch["name"],
            "snapshot_id": snap_info["snapshot_id"],
            "rule_version": batch["rule_version"],
            "batch_status": batch["status"],
            "includes_export": export_target is not None,
            "record_counts": {
                "invoices": snap_info["invoice_count"],
                "payments": snap_info["payment_count"],
                "matches": snap_info["match_count"],
                "adjudications": snap_info["adjudication_count"],
            },
        }
        with open(tmp / "manifest.json", "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)

        checksums = {}
        for fname in ["manifest.json", "snapshot.json"]:
            checksums[fname] = _sha256_file(tmp / fname)
        if export_target:
            checksums["export.csv"] = _sha256_file(export_target)
        with open(tmp / "checksums.json", "w", encoding="utf-8") as f:
            json.dump(checksums, f, ensure_ascii=False, indent=2)

        if output_path is None:
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            base_name = package_name or batch["name"]
            safe_name = _sanitize_filename(base_name)
            output_path = f"{ts}_{safe_name}{PACKAGE_EXT}"

        out_path = Path(output_path).resolve()
        if out_path.exists() and not force:
            raise FileExistsError(f"输出文件已存在: {out_path}")

        with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(tmp / "manifest.json", "manifest.json")
            zf.write(tmp / "snapshot.json", "snapshot.json")
            zf.write(tmp / "checksums.json", "checksums.json")
            if export_target:
                zf.write(export_target, "export.csv")

    return {
        "package_file": str(out_path),
        "package_name": out_path.name,
        "manifest": manifest,
        "snapshot_id": snap_info["snapshot_id"],
    }


def _should_pack_export(m: dict) -> bool:
    from .rules import should_export_match
    return should_export_match(m)


def verify_package(package_path: str) -> dict:
    """校验包的完整性和有效性。

    Returns:
        校验结果字典，包含 valid、errors、warnings、manifest 等字段
    """
    pkg = Path(package_path)
    if not pkg.exists():
        return {
            "valid": False,
            "errors": [f"包文件不存在: {package_path}"],
            "warnings": [],
            "manifest": None,
        }

    errors: List[str] = []
    warnings: List[str] = []

    try:
        with zipfile.ZipFile(pkg, "r") as zf:
            namelist = zf.namelist()

            for req in REQUIRED_FILES:
                if req not in namelist:
                    errors.append(f"缺少必需文件: {req}")

            if errors:
                return {
                    "valid": False,
                    "errors": errors,
                    "warnings": warnings,
                    "manifest": None,
                }

            with tempfile.TemporaryDirectory(prefix="inv_recon_verify_") as tmpdir:
                tmp = Path(tmpdir)
                zf.extractall(tmp)

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
                        f"包 schema 版本 {manifest.get('schema_version')} "
                        f"与当前工具版本 {PACKAGE_SCHEMA_VERSION} 可能不兼容"
                    )

                try:
                    with open(tmp / "snapshot.json", encoding="utf-8") as f:
                        snap_data = json.load(f)
                except json.JSONDecodeError as e:
                    errors.append(f"snapshot.json 格式错误: {e}")
                    return {
                        "valid": False,
                        "errors": errors,
                        "warnings": warnings,
                        "manifest": manifest,
                    }

                for key in ["snapshot_id", "batch", "invoices", "payments", "matches", "adjudications"]:
                    if key not in snap_data:
                        errors.append(f"snapshot.json 缺少关键字段: {key}")

                if manifest.get("includes_export"):
                    if "export.csv" not in namelist:
                        warnings.append("manifest 声明包含导出文件，但包内未找到 export.csv")

    except zipfile.BadZipFile:
        errors.append("文件不是有效的 ZIP 包")
        return {
            "valid": False,
            "errors": errors,
            "warnings": warnings,
            "manifest": None,
        }

    return {
        "valid": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "manifest": manifest if not errors else None,
    }


def unpack_package(package_path: str,
                   batch_name: Optional[str] = None,
                   force: bool = False,
                   db_path: Optional[str] = None) -> dict:
    """导入包，恢复为新批次。

    Args:
        package_path: 包文件路径
        batch_name: 新批次名称（可选，默认使用包内批次名）
        force: 是否强制覆盖已存在的输出文件（仅用于临时文件）
        db_path: 数据库路径（可选）

    Returns:
        导入结果字典，含校验报告、冲突处理、状态记录等
    """
    verify_result = verify_package(package_path)
    if not verify_result["valid"]:
        error_msg = ";\n".join(verify_result["errors"])
        raise ValueError(f"包校验失败: {error_msg}")

    pkg = Path(package_path)

    with tempfile.TemporaryDirectory(prefix="inv_recon_unpack_") as tmpdir:
        tmp = Path(tmpdir)

        with zipfile.ZipFile(pkg, "r") as zf:
            zf.extractall(tmp)

        with open(tmp / "manifest.json", encoding="utf-8") as f:
            manifest = json.load(f)
        with open(tmp / "snapshot.json", encoding="utf-8") as f:
            snap_data = json.load(f)

        original_batch_name = snap_data["batch"]["name"]
        original_batch_status = snap_data["batch"]["status"]
        original_rule_version = snap_data["batch"]["rule_version"]

        base_name = batch_name or original_batch_name

        snapshot._ensure_snapshot_dir()
        snap_dir = snapshot.get_snapshot_dir()

        # 1. 预先解析最终的批次名（处理同名批次冲突）
        final_batch_name = snapshot._resolve_batch_name(base_name, db_path=db_path)
        was_renamed = final_batch_name != base_name

        # 2. 用最终批次名生成快照文件名（包含毫秒级时间戳）
        snap_file_name = snapshot._snapshot_filename(
            snap_data["snapshot_id"],
            final_batch_name + "_imported"
        )
        snap_target = snap_dir / snap_file_name

        # 3. 快照文件名冲突时自动加后缀（即使毫秒级也可能冲突）
        if snap_target.exists() and not force:
            i = 2
            while True:
                stem = snap_target.stem
                if stem.endswith((".snap", "")):
                    stem = stem.replace(".snap", "")
                candidate = snap_dir / f"{stem}_{i}.snap.json"
                if not candidate.exists():
                    snap_target = candidate
                    snap_file_name = candidate.name
                    break
                i += 1
        elif snap_target.exists() and force:
            pass  # force 模式直接覆盖

        # 4. 保存快照文件
        snapshot._save_snapshot_file(snap_target, snap_data)

        # 5. 恢复批次（传入预先解析好的名称，避免再次重命名）
        result = snapshot.restore_snapshot(
            snap_data["snapshot_id"],
            new_batch_name=final_batch_name,
            db_path=db_path,
        )

        new_batch_id = result["new_batch_id"]
        new_batch_name = result["new_batch_name"]

        preserved_records: List[dict] = []
        renamed_records: List[dict] = []

        matches = db.get_matches_by_batch(new_batch_id, db_path=db_path)
        for m in matches:
            record_info = {
                "match_id": m["id"],
                "match_type": m["match_type"],
                "status": m["status"],
                "invoice_no": m.get("invoice_no", ""),
                "payment_no": m.get("payment_no", ""),
            }
            if m["status"] in ("confirmed", "rejected"):
                preserved_records.append(record_info)
            else:
                renamed_records.append(record_info)

        export_saved = None
        if manifest.get("includes_export") and (tmp / "export.csv").exists():
            export_saved = snap_dir / f"{new_batch_name}_export.csv"
            if export_saved.exists() and not force:
                export_saved = None
            else:
                shutil.copy2(tmp / "export.csv", export_saved)

        return {
            "success": True,
            "new_batch_id": new_batch_id,
            "new_batch_name": new_batch_name,
            "was_renamed": was_renamed,
            "original_name": original_batch_name,
            "batch_status": original_batch_status,
            "rule_version": original_rule_version,
            "snapshot_file": str(snap_target),
            "export_saved_as": str(export_saved) if export_saved else None,
            "warnings": verify_result["warnings"],
            "validation_report": {
                "total_records": len(matches),
                "preserved_count": len(preserved_records),
                "renamed_count": len(renamed_records),
                "preserved": preserved_records,
                "pending": renamed_records,
            },
            "manifest": manifest,
        }


def inspect_package(package_path: str) -> dict:
    """查看包的元信息（不解包到数据库）。

    Returns:
        包信息字典
    """
    verify_result = verify_package(package_path)
    if not verify_result["valid"]:
        return {
            "valid": False,
            "errors": verify_result["errors"],
            "manifest": None,
            "snapshot_info": None,
        }

    pkg = Path(package_path)
    with tempfile.TemporaryDirectory(prefix="inv_recon_inspect_") as tmpdir:
        tmp = Path(tmpdir)
        with zipfile.ZipFile(pkg, "r") as zf:
            zf.extractall(tmp)

        with open(tmp / "manifest.json", encoding="utf-8") as f:
            manifest = json.load(f)
        with open(tmp / "snapshot.json", encoding="utf-8") as f:
            snap_data = json.load(f)

    return {
        "valid": True,
        "errors": [],
        "warnings": verify_result["warnings"],
        "manifest": manifest,
        "snapshot_info": {
            "snapshot_id": snap_data["snapshot_id"],
            "snapshot_name": snap_data["snapshot_name"],
            "created_at": snap_data.get("created_at", ""),
            "source_batch_id": snap_data.get("source_batch_id"),
            "source_batch_name": snap_data.get("source_batch_name"),
            "batch_status": snap_data["batch"]["status"],
            "rule_version": snap_data["batch"]["rule_version"],
            "invoice_count": len(snap_data.get("invoices", [])),
            "payment_count": len(snap_data.get("payments", [])),
            "match_count": len(snap_data.get("matches", [])),
            "adjudication_count": len(snap_data.get("adjudications", [])),
        },
        "package_file": str(pkg),
        "package_size": pkg.stat().st_size,
    }
