import sys
import os
import click

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr.encoding and sys.stderr.encoding.lower() != "utf-8":
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from . import db, validators, matcher, export, rules, snapshot, pack, plan
from .models import BatchStatus, MatchStatus, Match


@click.group()
@click.version_option(version="1.0.0")
def cli():
    """离线发票勾稽工具 — 导入、配置、匹配、复核、撤销、导出"""
    pass


@cli.command()
def init():
    """初始化数据库（创建表结构和默认规则 v1）"""
    db.init_db()
    click.echo("数据库已初始化，默认规则版本 v1（容差 0.01，需供应商匹配）")


@cli.command("import", help=rules.IMPORT_RULES_HELP)
@click.option("--invoices", required=True, type=click.Path(exists=True),
              help=rules.IMPORT_INVOICES_HELP)
@click.option("--payments", required=True, type=click.Path(exists=True),
              help=rules.IMPORT_PAYMENTS_HELP)
@click.option("--name", default=None,
              help="批次名称（默认自动生成）")
@click.option("--dry-run", "--plan", "dry_run", is_flag=True, default=False,
              help=rules.PLAN_DRY_RUN_OPTION_HELP)
def import_data(invoices, payments, name, dry_run):
    if dry_run:
        _run_import_dry_run(invoices, payments, name)
        return

    # 真实导入：复用同一套校验逻辑（先跑一遍 plan，确保校验一致）
    plan_result = plan.plan_import(invoices, payments, name)
    if not plan_result.success:
        click.echo(f"{rules.PLAN_REAL_IMPORT_FAILED}", err=True)
        click.echo(f"{rules.PLAN_SECTION_ERRORS}:", err=True)
        for e in plan_result.errors:
            click.echo(f"  ✗ {e}", err=True)
        raise SystemExit(1)

    click.echo(rules.PLAN_REAL_IMPORT_PASSED)

    rule = db.get_current_rule()
    final_name = plan_result.batch_name

    # 重新解析 CSV（与 plan 复用同一套 validators）
    inv_result = validators.parse_invoices(invoices)
    pay_result = validators.parse_payments(payments)

    conn = db.connect()
    try:
        with conn:
            cur = conn.execute(
                "INSERT INTO batches (name, rule_version) VALUES (?, ?)",
                (final_name, rule.version),
            )
            batch_id = cur.lastrowid

            if inv_result.items:
                conn.executemany(
                    "INSERT INTO invoices (batch_id, invoice_no, vendor, amount, date) "
                    "VALUES (?, ?, ?, ?, ?)",
                    [(batch_id, i.invoice_no, i.vendor, i.amount, i.date) for i in inv_result.items],
                )
            if pay_result.items:
                conn.executemany(
                    "INSERT INTO payments (batch_id, payment_no, vendor, amount, date) "
                    "VALUES (?, ?, ?, ?, ?)",
                    [(batch_id, p.payment_no, p.vendor, p.amount, p.date) for p in pay_result.items],
                )
    finally:
        conn.close()

    click.echo(f"{rules.PLAN_REAL_MODE_LABEL} 批次 {batch_id} 已创建: {final_name}")
    click.echo(f"  发票: {len(inv_result.items)} 条 | 付款: {len(pay_result.items)} 条 | 规则: {rule.version}")
    click.echo(rules.IMPORT_OK_HINT_LEGACY_ROW)
    if inv_result.errors:
        click.echo(f"{rules.IMPORT_OK_HINT_BAD_ROWS_PREFIX} 发票 {len(inv_result.errors)} 行 {rules.IMPORT_OK_HINT_BAD_ROWS_SUFFIX}:")
        for e in inv_result.errors:
            click.echo(f"    - {e}")
    if pay_result.errors:
        click.echo(f"{rules.IMPORT_OK_HINT_BAD_ROWS_PREFIX} 付款 {len(pay_result.errors)} 行 {rules.IMPORT_OK_HINT_BAD_ROWS_SUFFIX}:")
        for e in pay_result.errors:
            click.echo(f"    - {e}")


def _run_import_dry_run(invoices: str, payments: str, name: str):
    """执行 import 的 dry-run 预检，输出计划结果。"""
    result = plan.plan_import(invoices, payments, name)

    if not result.success:
        click.echo(f"{rules.PLAN_MODE_PREFIX}预检失败", err=True)
        click.echo(f"{rules.PLAN_SECTION_ERRORS}:", err=True)
        for e in result.errors:
            click.echo(f"  ✗ {e}", err=True)
        raise SystemExit(1)

    click.echo(f"{rules.PLAN_MODE_PREFIX}导入计划 {rules.PLAN_MODE_LABEL}")
    click.echo()

    click.echo(rules.PLAN_SECTION_BATCH)
    click.echo(f"  批次名称: {result.batch_name}")
    if result.was_renamed:
        click.echo(f"  原名称: {result.original_name}（将自动重命名）")
    click.echo(f"  规则版本: {result.rule_version}")
    click.echo(f"  初始状态: {result.batch_status}")
    click.echo()

    click.echo(rules.PLAN_SECTION_RECORDS)
    click.echo(f"  发票: {result.invoice_count} 条合法")
    if result.bad_invoice_count:
        click.echo(f"         ({result.bad_invoice_count} 行坏数据将被跳过)")
    click.echo(f"  付款: {result.payment_count} 条合法")
    if result.bad_payment_count:
        click.echo(f"         ({result.bad_payment_count} 行坏数据将被跳过)")
    click.echo()

    click.echo(rules.PLAN_SECTION_FS_CHECK)
    if result.db_path_resolved:
        click.echo(f"{rules.PLAN_FS_DB_PATH}: {result.db_path_resolved}")
    if result.writable_ok:
        click.echo(rules.PLAN_FS_CHECK_OK)
    else:
        click.echo(rules.PLAN_FS_CHECK_FAILED)
        for e in result.writable_errors:
            click.echo(f"    ✗ {e}")
    if result.files_to_create:
        click.echo(f"{rules.PLAN_FS_FILES_TO_CREATE}:")
        for f in result.files_to_create:
            click.echo(f"    - {f}")
    if result.dirs_to_create:
        click.echo(f"{rules.PLAN_FS_DIRS_TO_CREATE}:")
        for d in result.dirs_to_create:
            click.echo(f"    - {d}")
    click.echo()

    if result.warnings:
        click.echo(f"{rules.PLAN_SECTION_WARNINGS}:")
        for w in result.warnings:
            click.echo(f"  ⚠  {w}")
        click.echo()

    click.echo(rules.PLAN_PREVIEW_COMMAND_INTRO)
    click.echo(rules.PLAN_PREVIEW_EXAMPLE_IMPORT)


@cli.command()
@click.option("--tolerance", type=float, default=None,
              help="金额容差（默认 0.01）")
@click.option("--require-vendor-match/--no-require-vendor-match", default=None,
              help="是否要求供应商一致（默认要求）")
def config(tolerance, require_vendor_match):
    """查看或修改匹配规则（修改后生成新规则版本）

    不带参数时显示当前规则；带参数时创建新版本。
    """
    current = db.get_current_rule()

    if tolerance is None and require_vendor_match is None:
        click.echo(f"当前规则版本: {current.version}")
        click.echo(f"  金额容差: {current.tolerance}")
        click.echo(f"  需供应商匹配: {'是' if current.require_vendor_match else '否'}")
        click.echo(f"  创建时间: {current.created_at}")
        return

    new_tol = tolerance if tolerance is not None else current.tolerance
    new_vendor = require_vendor_match if require_vendor_match is not None else current.require_vendor_match

    new_rule = db.create_rule_version(new_tol, new_vendor)
    click.echo(f"新规则版本: {new_rule.version}")
    click.echo(f"  金额容差: {new_rule.tolerance}")
    click.echo(f"  需供应商匹配: {'是' if new_rule.require_vendor_match else '否'}")


@cli.command()
@click.option("--batch", required=True, type=int, help="批次 ID")
def match(batch):
    """对指定批次执行匹配（按当前规则生成匹配建议）

    批次状态须为 imported。匹配结果写入数据库，
    如检测到同一发票被多笔付款占用会标记为冲突。
    """
    b = db.get_batch(batch)
    if b is None:
        click.echo(f"错误: 批次 {batch} 不存在", err=True)
        raise SystemExit(1)
    if b.status != BatchStatus.IMPORTED:
        click.echo(f"错误: 批次 {batch} 状态为 {b.status}，仅 imported 状态可执行匹配", err=True)
        raise SystemExit(1)

    rule = db.get_rule_by_version(b.rule_version)
    invoices = db.get_invoices_by_batch(batch)
    payments = db.get_payments_by_batch(batch)

    if not invoices and not payments:
        click.echo(f"错误: 批次 {batch} 无发票和付款数据", err=True)
        raise SystemExit(1)

    matches, warnings = matcher.match_invoices_payments(
        invoices, payments,
        tolerance=rule.tolerance,
        require_vendor_match=rule.require_vendor_match,
    )

    for m in matches:
        m.batch_id = batch

    conn = db.connect()
    try:
        with conn:
            conn.executemany(
                "INSERT INTO matches "
                "(batch_id, invoice_id, payment_id, match_type, amount_diff, status, review_note, adjudication) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (m.batch_id, m.invoice_id, m.payment_id, m.match_type,
                     m.amount_diff, m.status, m.review_note, m.adjudication)
                    for m in matches
                ],
            )
            conn.execute(
                "UPDATE batches SET status = ?, updated_at = datetime('now') WHERE id = ?",
                (BatchStatus.MATCHED, batch),
            )
    finally:
        conn.close()

    exact_cnt = sum(1 for m in matches if m.match_type == "exact")
    amt_cnt = sum(1 for m in matches if m.match_type == "amount_only")
    uninv_cnt = sum(1 for m in matches if m.match_type == "unmatched_invoice")
    unpay_cnt = sum(1 for m in matches if m.match_type == "unmatched_payment")
    conflict_cnt = sum(1 for m in matches if m.status == MatchStatus.CONFLICT)

    click.echo(f"批次 {batch} 匹配完成（规则 {b.rule_version}）")
    click.echo(f"  精确匹配: {exact_cnt} | 金额匹配: {amt_cnt} | 未匹配发票: {uninv_cnt} | 未匹配付款: {unpay_cnt}")
    if conflict_cnt:
        click.echo(f"  ⚠ 冲突: {conflict_cnt} 条（同一发票/付款被多次占用）")
    if warnings:
        for w in warnings:
            click.echo(f"  ⚠ {w}")


@cli.command()
@click.option("--batch", required=True, type=int, help="批次 ID")
@click.option("--match-id", type=int, default=None,
              help="指定匹配记录 ID（非交互模式）")
@click.option("--action", type=click.Choice(["confirm", "reject"]), default=None,
              help="裁决动作: confirm 或 reject（非交互模式）")
@click.option("--note", default=None, help="复核备注")
def review(batch, match_id, action, note):
    """复核匹配结果（交互或指定单条）

    交互模式: 逐条显示 pending/conflict 记录，选择确认/拒绝/跳过。
    非交互模式: 用 --match-id + --action 指定单条裁决。

    确认冲突匹配时，同发票/付款的其他冲突记录自动拒绝。
    全部记录裁决后批次状态变为 reviewed。
    """
    b = db.get_batch(batch)
    if b is None:
        click.echo(f"错误: 批次 {batch} 不存在", err=True)
        raise SystemExit(1)
    if b.status not in (BatchStatus.MATCHED, BatchStatus.REVIEWED):
        click.echo(f"错误: 批次 {batch} 状态为 {b.status}，仅 matched/reviewed 可复核", err=True)
        raise SystemExit(1)

    if match_id is not None:
        _review_single(batch, match_id, action, note)
    else:
        _review_interactive(batch)

    pending = db.count_pending_matches(batch)
    if pending == 0:
        db.update_batch_status(batch, BatchStatus.REVIEWED)
        click.echo(f"批次 {batch} 所有匹配已复核完毕，状态 → reviewed")
    else:
        click.echo(f"批次 {batch} 仍有 {pending} 条待复核")


@cli.command("review-undo", help=rules.REVIEW_UNDO_RULES_HELP)
@click.option("--batch", required=True, type=int, help="批次 ID")
@click.option("--match-id", required=True, type=int, help="匹配记录 ID")
def review_undo(batch, match_id):
    b = db.get_batch(batch)
    if b is None:
        click.echo(f"错误: 批次 {batch} 不存在", err=True)
        raise SystemExit(1)
    if b.status not in (BatchStatus.MATCHED, BatchStatus.REVIEWED, BatchStatus.EXPORTED):
        click.echo(f"错误: 批次 {batch} 状态为 {b.status}，不可撤销裁决", err=True)
        raise SystemExit(1)

    m = db.get_match(match_id)
    if m is None:
        click.echo(f"错误: 匹配记录 {match_id} 不存在", err=True)
        raise SystemExit(1)
    if m["batch_id"] != batch:
        click.echo(f"错误: 匹配记录 {match_id} 不属于批次 {batch}", err=True)
        raise SystemExit(1)

    if m["status"] in (MatchStatus.PENDING, MatchStatus.CONFLICT):
        click.echo(f"匹配 #{match_id} 当前状态为 {m['status']}，无需撤销")
        return

    latest = db.get_latest_adjudication(match_id)
    if latest is None or latest.get("prev_status") is None:
        click.echo(f"错误: 匹配 #{match_id} 无可撤销的裁决记录", err=True)
        raise SystemExit(1)

    prev_status = latest["prev_status"]
    prev_note = latest["prev_note"]
    prev_adj = None if prev_status in (MatchStatus.PENDING, MatchStatus.CONFLICT) else m.get("adjudication")

    was_confirmed_undo = (
        m.get("adjudication") == "confirmed"
        and prev_status == MatchStatus.CONFLICT
    )

    siblings_restored: list = []

    conn = db.connect()
    try:
        with conn:
            curr_row = conn.execute(
                "SELECT status, review_note, adjudication FROM matches WHERE id = ?",
                (match_id,),
            ).fetchone()

            conn.execute(
                "UPDATE matches SET status = ?, adjudication = ?, review_note = ? WHERE id = ?",
                (prev_status, prev_adj, prev_note, match_id),
            )
            conn.execute(
                "INSERT INTO adjudications (match_id, batch_id, action, note, prev_status, prev_note) "
                "VALUES (?, ?, 'undone', ?, ?, ?)",
                (match_id, batch, f"撤销上一次裁决",
                 curr_row["status"], curr_row["review_note"]),
            )

            if was_confirmed_undo:
                siblings = conn.execute(
                    "SELECT id, invoice_id, payment_id FROM matches "
                    "WHERE adjudication = 'auto_rejected' AND status = 'rejected' "
                    "AND id != ? AND batch_id = ? "
                    "AND (invoice_id = ? OR payment_id = ?)",
                    (match_id, batch, m.get("invoice_id"), m.get("payment_id")),
                ).fetchall()
                for sib in siblings:
                    sib_adj = conn.execute(
                        "SELECT prev_status, prev_note FROM adjudications "
                        "WHERE match_id = ? AND action = 'auto_rejected' "
                        "ORDER BY id DESC LIMIT 1",
                        (sib["id"],),
                    ).fetchone()
                    if sib_adj is None:
                        sib_prev_status = MatchStatus.CONFLICT
                        sib_prev_note = None
                    else:
                        sib_prev_status = sib_adj["prev_status"] or MatchStatus.CONFLICT
                        sib_prev_note = sib_adj["prev_note"]
                    sib_curr = conn.execute(
                        "SELECT status, review_note FROM matches WHERE id = ?",
                        (sib["id"],),
                    ).fetchone()
                    conn.execute(
                        "UPDATE matches SET status = ?, adjudication = NULL, review_note = ? "
                        "WHERE id = ?",
                        (sib_prev_status, sib_prev_note, sib["id"]),
                    )
                    conn.execute(
                        "INSERT INTO adjudications (match_id, batch_id, action, note, prev_status, prev_note) "
                        "VALUES (?, ?, 'undone_auto_rejected', ?, ?, ?)",
                        (sib["id"], batch, "因关联裁决撤销，恢复冲突可复核状态",
                         sib_curr["status"], sib_curr["review_note"]),
                    )
                    siblings_restored.append(sib["id"])

            if b.status in (BatchStatus.REVIEWED, BatchStatus.EXPORTED):
                pending = conn.execute(
                    "SELECT COUNT(*) AS cnt FROM matches "
                    "WHERE batch_id = ? AND status IN ('pending','conflict')",
                    (batch,),
                ).fetchone()["cnt"]
                if pending > 0:
                    conn.execute(
                        "UPDATE batches SET status = 'matched', updated_at = datetime('now') "
                        "WHERE id = ?",
                        (batch,),
                    )
    finally:
        conn.close()

    click.echo(f"匹配 #{match_id} 已撤销裁决")
    click.echo(f"  状态: {m['status']} → {prev_status}")
    if prev_note:
        click.echo(f"  备注已恢复: {prev_note}")
    else:
        click.echo(f"  备注已清空")
    if siblings_restored:
        click.echo(f"  关联冲突记录已恢复可复核: #{', #'.join(str(s) for s in siblings_restored)}")


def _review_single(batch_id, match_id, action, note):
    if action is None:
        click.echo("错误: 非交互模式必须指定 --action (confirm/reject)", err=True)
        raise SystemExit(1)

    m = db.get_match(match_id)
    if m is None:
        click.echo(f"错误: 匹配记录 {match_id} 不存在", err=True)
        raise SystemExit(1)
    if m["batch_id"] != batch_id:
        click.echo(f"错误: 匹配记录 {match_id} 不属于批次 {batch_id}", err=True)
        raise SystemExit(1)
    if m["status"] not in (MatchStatus.PENDING, MatchStatus.CONFLICT):
        click.echo(f"匹配记录 {match_id} 状态为 {m['status']}，无需复核")
        return

    _apply_review(batch_id, match_id, m, action, note)


def _review_interactive(batch_id):
    all_matches = db.get_matches_by_batch(batch_id)
    pending = [m for m in all_matches if m["status"] in (MatchStatus.PENDING, MatchStatus.CONFLICT)]

    if not pending:
        click.echo("没有待复核的匹配记录")
        return

    click.echo(f"批次 {batch_id} 共 {len(pending)} 条待复核\n")

    for m in pending:
        _print_match(m)
        while True:
            choice = click.prompt("  [c]确认 / [r]拒绝 / [s]跳过", default="s")
            if choice in ("c", "confirm"):
                note = click.prompt("  备注 (可直接回车跳过)", default="", show_default=False)
                _apply_review(batch_id, m["id"], m, "confirm", note or None)
                break
            elif choice in ("r", "reject"):
                note = click.prompt("  备注 (可直接回车跳过)", default="", show_default=False)
                _apply_review(batch_id, m["id"], m, "reject", note or None)
                break
            elif choice in ("s", "skip"):
                break
            else:
                click.echo("  请输入 c/r/s")
        click.echo()


def _apply_review(batch_id, match_id, m, action, note):
    adjudication = "confirmed" if action == "confirm" else "rejected"
    new_status = MatchStatus.CONFIRMED if action == "confirm" else MatchStatus.REJECTED

    prev_status = m.get("status")
    prev_note = m.get("review_note")

    conn = db.connect()
    try:
        with conn:
            conn.execute(
                "UPDATE matches SET status = ?, adjudication = ?, review_note = ? WHERE id = ?",
                (new_status, adjudication, note, match_id),
            )
            conn.execute(
                "INSERT INTO adjudications (match_id, batch_id, action, note, prev_status, prev_note) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (match_id, batch_id, adjudication, note, prev_status, prev_note),
            )

            if action == "confirm":
                if m.get("invoice_id"):
                    rows = conn.execute(
                        "SELECT id, status, review_note FROM matches "
                        "WHERE invoice_id = ? AND id != ? AND status = 'conflict'",
                        (m["invoice_id"], match_id),
                    ).fetchall()
                    for r in rows:
                        conn.execute(
                            "INSERT INTO adjudications (match_id, batch_id, action, note, prev_status, prev_note) "
                            "VALUES (?, ?, 'auto_rejected', ?, ?, ?)",
                            (r["id"], batch_id, note, r["status"], r["review_note"]),
                        )
                    conn.execute(
                        "UPDATE matches SET status = 'rejected', adjudication = 'auto_rejected' "
                        "WHERE invoice_id = ? AND id != ? AND status = 'conflict'",
                        (m["invoice_id"], match_id),
                    )
                if m.get("payment_id"):
                    rows = conn.execute(
                        "SELECT id, status, review_note FROM matches "
                        "WHERE payment_id = ? AND id != ? AND status = 'conflict'",
                        (m["payment_id"], match_id),
                    ).fetchall()
                    for r in rows:
                        conn.execute(
                            "INSERT INTO adjudications (match_id, batch_id, action, note, prev_status, prev_note) "
                            "VALUES (?, ?, 'auto_rejected', ?, ?, ?)",
                            (r["id"], batch_id, note, r["status"], r["review_note"]),
                        )
                    conn.execute(
                        "UPDATE matches SET status = 'rejected', adjudication = 'auto_rejected' "
                        "WHERE payment_id = ? AND id != ? AND status = 'conflict'",
                        (m["payment_id"], match_id),
                    )
    finally:
        conn.close()

    label = "确认" if action == "confirm" else "拒绝"
    click.echo(f"  匹配 #{match_id} 已{label}")


def _print_match(m):
    click.echo(f"--- 匹配 #{m['id']} [{m['status']}] 类型={m['match_type']} ---")
    if m.get("invoice_no"):
        click.echo(f"  发票: {m['invoice_no']} | {m['inv_vendor']} | ¥{m['inv_amount']:.2f} | {m['inv_date']}")
    else:
        click.echo("  发票: (无)")
    if m.get("payment_no"):
        click.echo(f"  付款: {m['payment_no']} | {m['pay_vendor']} | ¥{m['pay_amount']:.2f} | {m['pay_date']}")
    else:
        click.echo("  付款: (无)")
    click.echo(f"  差额: ¥{m['amount_diff']:.2f}")


@cli.command()
@click.option("--batch", required=True, type=int, help="批次 ID")
def revoke(batch):
    """撤销批次（标记为 revoked，不影响其他批次数据）

    批次不存在或已撤销时返回错误。撤销后不可恢复。
    """
    b = db.get_batch(batch)
    if b is None:
        click.echo(f"错误: 批次 {batch} 不存在", err=True)
        raise SystemExit(1)
    if b.status == BatchStatus.REVOKED:
        click.echo(f"错误: 批次 {batch} 已撤销，不可重复操作", err=True)
        raise SystemExit(1)

    conn = db.connect()
    try:
        with conn:
            conn.execute(
                "UPDATE batches SET status = ?, updated_at = datetime('now') WHERE id = ?",
                (BatchStatus.REVOKED, batch),
            )
            conn.execute(
                "INSERT INTO adjudications (match_id, batch_id, action, note) "
                "VALUES (NULL, ?, 'batch_revoked', ?)",
                (batch, f"批次 {b.name} 已撤销"),
            )
    finally:
        conn.close()

    click.echo(f"批次 {batch} ({b.name}) 已撤销")


@cli.command("export", help=rules.EXPORT_RULES_HELP)
@click.option("--batch", required=True, type=int, help="批次 ID")
@click.option("--output", required=True, type=click.Path(),
              help=rules.EXPORT_OUTPUT_HELP)
def export_cmd(batch, output):
    b = db.get_batch(batch)
    if b is None:
        click.echo(f"错误: 批次 {batch} 不存在", err=True)
        raise SystemExit(1)
    if b.status not in (BatchStatus.MATCHED, BatchStatus.REVIEWED, BatchStatus.EXPORTED):
        click.echo(f"错误: 批次 {batch} 状态为 {b.status}，仅 matched/reviewed/exported 状态可导出", err=True)
        raise SystemExit(1)

    all_matches = db.get_matches_by_batch(batch)
    if not all_matches:
        click.echo(f"错误: 批次 {batch} 无匹配记录", err=True)
        raise SystemExit(1)

    diff_matches = [m for m in all_matches if rules.should_export_match(m)]
    if not diff_matches:
        click.echo(f"批次 {batch} 无待处理记录（全部为零差额已确认或已拒绝）")
        raise SystemExit(1)

    try:
        out_path = export.export_differences(output, diff_matches)
    except Exception as e:
        click.echo(f"导出失败: {e}", err=True)
        raise SystemExit(1)

    db.update_batch_status(batch, BatchStatus.EXPORTED)
    click.echo(f"{rules.EXPORT_OK_PREFIX}: {out_path}")

    skipped_rejected = sum(1 for m in all_matches if m["status"] == MatchStatus.REJECTED)
    skipped_clean = sum(
        1 for m in all_matches
        if m["status"] == MatchStatus.CONFIRMED
        and abs(float(m.get("amount_diff", 0.0))) < 0.001
    )
    click.echo(rules.EXPORT_OK_BREAKDOWN.format(
        exported=len(diff_matches),
        total=len(all_matches),
        skipped_rejected=skipped_rejected,
        skipped_clean=skipped_clean,
    ))


@cli.command("list")
def list_cmd():
    """列出所有批次及进度"""
    batches = db.list_batches()
    if not batches:
        click.echo("暂无批次。请先运行 inv-recon import 创建批次。")
        return

    click.echo(
        f"{'ID':>4}  {'名称':<24} {'状态':<10} {'规则':<6} "
        f"{'发票':>4} {'付款':>4} {'匹配':>4} {'确认':>4} {'待审':>4} {'创建时间':<20}"
    )
    click.echo("-" * 96)
    for b in batches:
        click.echo(
            f"{b['id']:>4}  {b['name']:<24} {b['status']:<10} {b['rule_version']:<6} "
            f"{b['invoice_count']:>4} {b['payment_count']:>4} {b['match_count']:>4} "
            f"{b['confirmed_count']:>4} {b['pending_count'] + b['conflict_count']:>4} "
            f"{b['created_at']:<20}"
        )


@cli.command()
@click.option("--batch", required=True, type=int, help="批次 ID")
def show(batch):
    """显示批次详情和匹配记录"""
    b = db.get_batch(batch)
    if b is None:
        click.echo(f"错误: 批次 {batch} 不存在", err=True)
        raise SystemExit(1)

    click.echo(f"批次 {b.id}: {b.name}")
    click.echo(f"  状态: {b.status} | 规则: {b.rule_version}")
    click.echo(f"  创建: {b.created_at} | 更新: {b.updated_at}")

    invoices = db.get_invoices_by_batch(batch)
    payments = db.get_payments_by_batch(batch)
    click.echo(f"  发票: {len(invoices)} 条 | 付款: {len(payments)} 条")

    matches = db.get_matches_by_batch(batch)
    if not matches:
        click.echo("  尚未执行匹配")
        return

    pending = sum(1 for m in matches if m["status"] == MatchStatus.PENDING)
    confirmed = sum(1 for m in matches if m["status"] == MatchStatus.CONFIRMED)
    rejected = sum(1 for m in matches if m["status"] == MatchStatus.REJECTED)
    conflict = sum(1 for m in matches if m["status"] == MatchStatus.CONFLICT)

    click.echo(f"  匹配: {len(matches)} 条 (待审 {pending} | 确认 {confirmed} | 拒绝 {rejected} | 冲突 {conflict})")
    click.echo()
    for m in matches:
        _print_match(m)
        if m.get("adjudication"):
            click.echo(f"  裁决: {m['adjudication']}" + (f" — {m['review_note']}" if m.get("review_note") else ""))
        click.echo()


@cli.group("snapshot", help=rules.SNAPSHOT_RULES_HELP)
def snapshot_cmd():
    """批次快照与恢复命令组。"""
    pass


@snapshot_cmd.command("create", help=rules.SNAPSHOT_CREATE_HELP)
@click.option("--batch", required=True, type=int, help="批次 ID")
@click.option("--name", default=None, help="快照名称（默认自动生成）")
def snapshot_create(batch, name):
    b = db.get_batch(batch)
    if b is None:
        click.echo(f"错误: 批次 {batch} 不存在", err=True)
        raise SystemExit(1)

    try:
        info = snapshot.create_snapshot(batch, name=name)
    except ValueError as e:
        click.echo(f"错误: {e}", err=True)
        raise SystemExit(1)

    click.echo(f"{rules.SNAPSHOT_OK_CREATED}")
    click.echo(f"  快照 ID: {info['snapshot_id']}")
    click.echo(f"  快照名称: {info['snapshot_name']}")
    click.echo(f"  创建时间: {info['created_at']}")
    click.echo(f"  源批次: #{info['source_batch_id']} {info['source_batch_name']}")
    click.echo(f"  批次状态: {info['batch_status']} | 规则: {info['rule_version']}")
    click.echo(f"  发票: {info['invoice_count']} | 付款: {info['payment_count']} | 匹配: {info['match_count']} | 裁决记录: {info['adjudication_count']}")
    click.echo(f"  文件: {info['file']}")


@snapshot_cmd.command("list", help=rules.SNAPSHOT_LIST_HELP)
def snapshot_list():
    snaps = snapshot.list_snapshots()
    if not snaps:
        click.echo("暂无快照。")
        click.echo(rules.SNAPSHOT_DIR_DEFAULT)
        return

    click.echo(
        f"{'快照ID':<10} {'名称':<24} {'状态':<10} {'规则':<6} "
        f"{'匹配':>4} {'创建时间':<20} {'源批次':<20}"
    )
    click.echo("-" * 100)
    for s in snaps:
        short_id = s["snapshot_id"][:8]
        source = f"#{s['source_batch_id']} {s['source_batch_name']}" if s.get("source_batch_id") else "N/A"
        click.echo(
            f"{short_id:<10} {s['snapshot_name']:<24} {s['batch_status']:<10} {s['rule_version']:<6} "
            f"{s['match_count']:>4} {s['created_at'][:19]:<20} {source:<20}"
        )


@snapshot_cmd.command("show", help=rules.SNAPSHOT_SHOW_HELP)
@click.option("--snapshot", "snapshot_ref", required=True, help="快照 ID（完整/前缀）或名称")
def snapshot_show(snapshot_ref):
    info = snapshot.get_snapshot_info(snapshot_ref)
    if info is None:
        click.echo(f"错误: 快照 {snapshot_ref} 不存在", err=True)
        raise SystemExit(1)

    click.echo(f"快照 ID: {info['snapshot_id']}")
    click.echo(f"快照名称: {info['snapshot_name']}")
    click.echo(f"创建时间: {info['created_at']}")
    click.echo(f"源批次: #{info['source_batch_id']} {info['source_batch_name']}" if info.get("source_batch_id") else "源批次: N/A")
    click.echo(f"批次状态: {info['batch_status']}")
    click.echo(f"规则版本: {info['rule_version']}")
    click.echo(f"发票: {info['invoice_count']} 条")
    click.echo(f"付款: {info['payment_count']} 条")
    click.echo(f"匹配: {info['match_count']} 条")
    click.echo(f"裁决历史: {info['adjudication_count']} 条")


@snapshot_cmd.command("restore", help=rules.SNAPSHOT_RESTORE_HELP)
@click.option("--snapshot", "snapshot_ref", required=True, help="快照 ID（完整/前缀）或名称")
@click.option("--batch-name", default=None, help="新批次名称（默认使用快照内批次名）")
def snapshot_restore(snapshot_ref, batch_name):
    info = snapshot.get_snapshot_info(snapshot_ref)
    if info is None:
        click.echo(f"错误: 快照 {snapshot_ref} 不存在", err=True)
        raise SystemExit(1)

    try:
        result = snapshot.restore_snapshot(snapshot_ref, new_batch_name=batch_name)
    except ValueError as e:
        click.echo(f"错误: {e}", err=True)
        raise SystemExit(1)

    click.echo(f"{rules.SNAPSHOT_OK_RESTORED}")
    click.echo(f"  新批次 ID: {result['new_batch_id']}")
    click.echo(f"  新批次名称: {result['new_batch_name']}")
    if result["was_renamed"]:
        click.echo(rules.SNAPSHOT_RENAMED_HINT)
        click.echo(f"  原名称: {result['original_name']}")
    click.echo(f"  状态: {result['status']} | 规则: {result['rule_version']}")
    click.echo(f"  发票: {result['invoice_count']} | 付款: {result['payment_count']} | 匹配: {result['match_count']} | 裁决记录: {result['adjudication_count']}")


# ======================================================================
# 打包与验包命令组
# ======================================================================

@cli.command("pack", help=rules.PACK_CREATE_HELP)
@click.option("--batch", required=True, type=int, help="批次 ID")
@click.option("--output", type=click.Path(), default=None,
              help="输出包文件路径（默认自动生成）")
@click.option("--name", default=None, help="包名称（默认基于批次名）")
@click.option("--include-export/--no-include-export", default=True,
              help="是否包含待导出结果（默认包含）")
@click.option("--force", is_flag=True, default=False,
              help="输出文件已存在时强制覆盖")
def pack_cmd(batch, output, name, include_export, force):
    b = db.get_batch(batch)
    if b is None:
        click.echo(f"错误: 批次 {batch} 不存在", err=True)
        raise SystemExit(1)

    try:
        info = pack.pack_batch(
            batch_id=batch,
            output_path=output,
            package_name=name,
            include_export=include_export,
            force=force,
        )
    except FileExistsError as e:
        click.echo(f"错误: {e}", err=True)
        click.echo(rules.PACK_FORCE_HINT, err=True)
        raise SystemExit(1)
    except ValueError as e:
        click.echo(f"错误: {e}", err=True)
        raise SystemExit(1)

    click.echo(rules.PACK_OK_PACKED)
    click.echo(f"  包文件: {info['package_file']}")
    m = info["manifest"]
    click.echo(f"  源批次: #{m['source_batch_id']} {m['source_batch_name']}")
    click.echo(f"  批次状态: {m['batch_status']} | 规则: {m['rule_version']}")
    click.echo(f"  发票: {m['record_counts']['invoices']} | 付款: {m['record_counts']['payments']} | 匹配: {m['record_counts']['matches']} | 裁决: {m['record_counts']['adjudications']}")
    if m["includes_export"]:
        click.echo(f"  包含导出结果: 是")
    click.echo(f"  快照 ID: {info['snapshot_id']}")


@cli.command("unpack", help=rules.PACK_UNPACK_HELP)
@click.option("--input", "input_file", required=True, type=click.Path(),
              help="可搬运包文件路径")
@click.option("--batch-name", default=None, help="新批次名称（默认使用包内批次名）")
@click.option("--force", is_flag=True, default=False,
              help="同名快照文件已存在时强制覆盖")
@click.option("--dry-run", "--plan", "dry_run", is_flag=True, default=False,
              help=rules.PLAN_DRY_RUN_OPTION_HELP)
def unpack_cmd(input_file, batch_name, force, dry_run):
    if dry_run:
        _run_unpack_dry_run(input_file, batch_name, force)
        return

    if not os.path.exists(input_file):
        click.echo(f"错误: 包文件不存在: {input_file}", err=True)
        raise SystemExit(1)

    # 真实导入：复用同一套校验逻辑（先跑一遍 plan，确保校验一致）
    plan_result = plan.plan_unpack(input_file, batch_name, force)
    if not plan_result.success:
        click.echo(f"{rules.PLAN_REAL_IMPORT_FAILED}", err=True)
        error_msg = ";\n".join(plan_result.errors)
        click.echo(f"错误: 包校验失败: {error_msg}", err=True)
        raise SystemExit(1)

    click.echo(rules.PLAN_REAL_IMPORT_PASSED)

    try:
        result = pack.unpack_package(
            package_path=input_file,
            batch_name=batch_name,
            force=force,
        )
    except FileExistsError as e:
        click.echo(f"错误: {e}", err=True)
        click.echo(rules.PACK_FORCE_HINT, err=True)
        raise SystemExit(1)
    except ValueError as e:
        click.echo(f"错误: {e}", err=True)
        raise SystemExit(1)

    click.echo(f"{rules.PLAN_REAL_MODE_LABEL} {rules.PACK_OK_UNPACKED}")
    click.echo(f"  新批次 ID: {result['new_batch_id']}")
    click.echo(f"  新批次名称: {result['new_batch_name']}")
    if result["was_renamed"]:
        click.echo(rules.PACK_RENAMED_HINT)
        click.echo(f"  原名称: {result['original_name']}")
    click.echo(f"  状态: {result['batch_status']} | 规则: {result['rule_version']}")
    click.echo(f"  快照文件: {result['snapshot_file']}")
    if result["export_saved_as"]:
        click.echo(f"  导出结果已保存为: {result['export_saved_as']}")

    vr = result["validation_report"]
    click.echo()
    click.echo("--- 导入后校验报告 ---")
    click.echo(f"  总记录数: {vr['total_records']}")
    click.echo(f"  {rules.PACK_PRESERVED_PREFIX} {vr['preserved_count']} 条（已裁决，状态不变）")
    if vr["preserved"]:
        for r in vr["preserved"][:5]:
            status_label = "✓ 确认" if r["status"] == "confirmed" else "✗ 拒绝"
            inv = r["invoice_no"] or "-"
            pay = r["payment_no"] or "-"
            click.echo(f"    #{r['match_id']} [{status_label}] {r['match_type']} 发票:{inv} 付款:{pay}")
        if len(vr["preserved"]) > 5:
            click.echo(f"    ... 还有 {len(vr['preserved']) - 5} 条")
    click.echo(f"  {rules.PACK_PENDING_PREFIX} {vr['renamed_count']} 条（ID 已重分配，待复核）")
    if vr["pending"]:
        for r in vr["pending"][:5]:
            inv = r["invoice_no"] or "-"
            pay = r["payment_no"] or "-"
            click.echo(f"    #{r['match_id']} [{r['status']}] {r['match_type']} 发票:{inv} 付款:{pay}")
        if len(vr["pending"]) > 5:
            click.echo(f"    ... 还有 {len(vr['pending']) - 5} 条")


def _run_unpack_dry_run(input_file: str, batch_name: str, force: bool):
    """执行 unpack 的 dry-run 预检，输出计划结果。"""
    result = plan.plan_unpack(input_file, batch_name, force)

    if not result.success:
        click.echo(f"{rules.PLAN_MODE_PREFIX}预检失败", err=True)
        click.echo(f"{rules.PLAN_SECTION_ERRORS}:", err=True)
        for e in result.errors:
            click.echo(f"  ✗ {e}", err=True)
        raise SystemExit(1)

    click.echo(f"{rules.PLAN_MODE_PREFIX}包导入计划 {rules.PLAN_MODE_LABEL}")
    click.echo()

    click.echo(rules.PLAN_SECTION_BATCH)
    click.echo(f"  批次名称: {result.batch_name}")
    if result.was_renamed:
        click.echo(f"  原名称: {result.original_name}（将自动重命名）")
    click.echo(f"  规则版本: {result.rule_version}")
    click.echo(f"  批次状态: {result.batch_status}")
    click.echo()

    click.echo(rules.PLAN_SECTION_RECORDS)
    click.echo(f"  发票: {result.invoice_count} 条")
    click.echo(f"  付款: {result.payment_count} 条")
    click.echo(f"  匹配: {result.match_count} 条")
    if result.conflict_match_count:
        click.echo(f"    其中冲突: {result.conflict_match_count} 条")
    click.echo(f"  裁决历史: {result.adjudication_count} 条")
    click.echo(f"  沿用原状态: {result.preserved_count} 条（已裁决，状态不变）")
    pending_count = result.match_count - result.preserved_count
    click.echo(f"  待复核/重分配: {pending_count} 条")
    click.echo()

    click.echo(rules.PLAN_SECTION_FILES)
    if result.snapshot_file:
        click.echo(f"  快照文件: {result.snapshot_file}")
    if result.export_file:
        click.echo(f"  导出结果: {result.export_file}")
    click.echo()

    click.echo(rules.PLAN_SECTION_FS_CHECK)
    if result.db_path_resolved:
        click.echo(f"{rules.PLAN_FS_DB_PATH}: {result.db_path_resolved}")
    if result.snapshot_dir:
        click.echo(f"{rules.PLAN_FS_SNAPSHOT_DIR}: {result.snapshot_dir}")
    if result.unpack_tmp_dir:
        click.echo(f"{rules.PLAN_FS_TMP_DIR}: {result.unpack_tmp_dir}")
    if result.writable_ok:
        click.echo(rules.PLAN_FS_CHECK_OK)
    else:
        click.echo(rules.PLAN_FS_CHECK_FAILED)
        for e in result.writable_errors:
            click.echo(f"    ✗ {e}")
    if result.files_to_create:
        click.echo(f"{rules.PLAN_FS_FILES_TO_CREATE}:")
        for f in result.files_to_create:
            click.echo(f"    - {f}")
    if result.dirs_to_create:
        click.echo(f"{rules.PLAN_FS_DIRS_TO_CREATE}:")
        for d in result.dirs_to_create:
            click.echo(f"    - {d}")
    click.echo()

    if result.conflict_details:
        click.echo(rules.PLAN_SECTION_CONFLICTS)
        for c in result.conflict_details:
            click.echo(f"  ✗ {c}")
        click.echo()
    else:
        click.echo(rules.PLAN_SECTION_CONFLICTS)
        click.echo(rules.PLAN_CONFLICT_NONE)
        click.echo()

    if result.warnings:
        click.echo(f"{rules.PLAN_SECTION_WARNINGS}:")
        for w in result.warnings:
            click.echo(f"  ⚠  {w}")
        click.echo()

    click.echo(rules.PLAN_PREVIEW_COMMAND_INTRO)
    click.echo(rules.PLAN_PREVIEW_EXAMPLE_UNPACK)


@cli.command("verify", help=rules.PACK_VERIFY_HELP)
@click.option("--input", "input_file", required=True, type=click.Path(),
              help="可搬运包文件路径")
def verify_cmd(input_file):
    if not os.path.exists(input_file):
        click.echo(f"错误: 包文件不存在: {input_file}", err=True)
        raise SystemExit(1)
    result = pack.verify_package(input_file)

    if result["valid"]:
        click.echo(rules.PACK_OK_VERIFIED)
        m = result["manifest"]
        if m:
            click.echo(f"  源批次: #{m['source_batch_id']} {m['source_batch_name']}")
            click.echo(f"  批次状态: {m['batch_status']} | 规则: {m['rule_version']}")
            click.echo(f"  打包时间: {m['created_at']}")
            click.echo(f"  工具版本: {m['tool_version']}")
            if m.get("source_machine"):
                click.echo(f"  源机器: {m['source_machine']}")
            click.echo(f"  发票: {m['record_counts']['invoices']} | 付款: {m['record_counts']['payments']} | 匹配: {m['record_counts']['matches']} | 裁决: {m['record_counts']['adjudications']}")
            if m["includes_export"]:
                click.echo(f"  包含导出结果: 是")
        if result["warnings"]:
            click.echo()
            click.echo("⚠  警告:")
            for w in result["warnings"]:
                click.echo(f"  - {w}")
    else:
        click.echo("包校验失败:", err=True)
        for e in result["errors"]:
            click.echo(f"  ✗ {e}", err=True)
        raise SystemExit(1)


@cli.command("inspect", help=rules.PACK_INSPECT_HELP)
@click.option("--input", "input_file", required=True, type=click.Path(),
              help="可搬运包文件路径")
def inspect_cmd(input_file):
    if not os.path.exists(input_file):
        click.echo(f"错误: 包文件不存在: {input_file}", err=True)
        raise SystemExit(1)
    result = pack.inspect_package(input_file)

    if not result["valid"]:
        click.echo("包无效:", err=True)
        for e in result["errors"]:
            click.echo(f"  ✗ {e}", err=True)
        raise SystemExit(1)

    si = result["snapshot_info"]
    click.echo(f"包文件: {result['package_file']}")
    click.echo(f"包大小: {result['package_size']} 字节")
    click.echo()
    click.echo("--- 快照信息 ---")
    click.echo(f"  快照 ID: {si['snapshot_id']}")
    click.echo(f"  快照名称: {si['snapshot_name']}")
    click.echo(f"  源批次: #{si['source_batch_id']} {si['source_batch_name']}")
    click.echo(f"  批次状态: {si['batch_status']}")
    click.echo(f"  规则版本: {si['rule_version']}")
    click.echo(f"  发票: {si['invoice_count']} 条")
    click.echo(f"  付款: {si['payment_count']} 条")
    click.echo(f"  匹配: {si['match_count']} 条")
    click.echo(f"  裁决历史: {si['adjudication_count']} 条")
    click.echo()
    click.echo("--- 打包元数据 ---")
    m = result["manifest"]
    click.echo(f"  打包时间: {m['created_at']}")
    click.echo(f"  工具版本: {m['tool_version']}")
    if m.get("source_machine"):
        click.echo(f"  源机器: {m['source_machine']}")
    click.echo(f"  包含导出结果: {'是' if m['includes_export'] else '否'}")

    if result["warnings"]:
        click.echo()
        click.echo("⚠  警告:")
        for w in result["warnings"]:
            click.echo(f"  - {w}")


# ======================================================================
# 统一为命令函数设置 __doc__ —— 让 --help 显示 rules 模块的共享文案。
# 必须放在所有命令定义之后（否则函数还不存在）。
# 这样做可以保证 README / --help / 实际提示使用同一份文案源。
# ======================================================================
import_data.__doc__ = rules.IMPORT_RULES_HELP
export_cmd.__doc__ = rules.EXPORT_RULES_HELP
review_undo.__doc__ = rules.REVIEW_UNDO_RULES_HELP
snapshot_create.__doc__ = rules.SNAPSHOT_CREATE_HELP
snapshot_restore.__doc__ = rules.SNAPSHOT_RESTORE_HELP
pack_cmd.__doc__ = rules.PACK_CREATE_HELP
unpack_cmd.__doc__ = rules.PACK_UNPACK_HELP
verify_cmd.__doc__ = rules.PACK_VERIFY_HELP
inspect_cmd.__doc__ = rules.PACK_INSPECT_HELP
