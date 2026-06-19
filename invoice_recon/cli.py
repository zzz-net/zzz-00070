import sys
import os
import click

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr.encoding and sys.stderr.encoding.lower() != "utf-8":
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from . import db, validators, matcher, export
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


@cli.command("import")
@click.option("--invoices", required=True, type=click.Path(exists=True),
              help="发票 CSV 文件路径。列: invoice_no,vendor,amount,date")
@click.option("--payments", required=True, type=click.Path(exists=True),
              help="付款 CSV 文件路径。列: payment_no,vendor,amount,date")
@click.option("--name", default=None,
              help="批次名称（默认自动生成）")
def import_data(invoices, payments, name):
    """导入发票和付款表，创建新批次

    发票 CSV 格式: invoice_no,vendor,amount,date
    付款 CSV 格式: payment_no,vendor,amount,date

    金额必须为数字，发票号/付款编号在批次内不可重复。
    存在非法行时跳过并保留合法数据，缺少列或文件为空时整体失败。
    """
    try:
        inv_result = validators.parse_invoices(invoices)
    except validators.ValidationError as e:
        click.echo(f"发票文件格式错误:\n" + "\n".join(f"  - {x}" for x in e.errors), err=True)
        raise SystemExit(1)

    try:
        pay_result = validators.parse_payments(payments)
    except validators.ValidationError as e:
        click.echo(f"付款文件格式错误:\n" + "\n".join(f"  - {x}" for x in e.errors), err=True)
        raise SystemExit(1)

    if not inv_result.has_items and not pay_result.has_items:
        click.echo("错误: 发票和付款文件均无合法数据", err=True)
        raise SystemExit(1)

    rule = db.get_current_rule()
    if name is None:
        name = f"batch_{rule.version}"

    conn = db.connect()
    try:
        with conn:
            cur = conn.execute(
                "INSERT INTO batches (name, rule_version) VALUES (?, ?)",
                (name, rule.version),
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

    click.echo(f"批次 {batch_id} 已创建: {name}")
    click.echo(f"  发票: {len(inv_result.items)} 条 | 付款: {len(pay_result.items)} 条 | 规则: {rule.version}")
    if inv_result.errors:
        click.echo(f"  ⚠ 发票跳过 {len(inv_result.errors)} 行:")
        for e in inv_result.errors:
            click.echo(f"    - {e}")
    if pay_result.errors:
        click.echo(f"  ⚠ 付款跳过 {len(pay_result.errors)} 行:")
        for e in pay_result.errors:
            click.echo(f"    - {e}")


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


@cli.command("review-undo")
@click.option("--batch", required=True, type=int, help="批次 ID")
@click.option("--match-id", required=True, type=int, help="匹配记录 ID")
def review_undo(batch, match_id):
    """撤销单条匹配的上一次裁决，恢复复核前的状态和备注

    仅能撤销上一次人工裁决（confirm 或 reject）。
    撤销后该条匹配回到 pending/conflict 状态。
    """
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


@cli.command("export")
@click.option("--batch", required=True, type=int, help="批次 ID")
@click.option("--output", required=True, type=click.Path(),
              help="导出文件路径 (CSV)")
def export_cmd(batch, output):
    """导出差异清单 (CSV)

    仅导出需要处理的差异记录，零差额且已确认的正常匹配不会导出。
    导出记录包含: 冲突、未解决、已拒绝、以及有差额的已确认匹配。

    批次状态须为 reviewed 或 exported。导出为原子写入，不会产生半截文件。
    导出后批次状态变为 exported（可重复导出覆盖）。
    """
    b = db.get_batch(batch)
    if b is None:
        click.echo(f"错误: 批次 {batch} 不存在", err=True)
        raise SystemExit(1)
    if b.status not in (BatchStatus.REVIEWED, BatchStatus.EXPORTED):
        click.echo(f"错误: 批次 {batch} 状态为 {b.status}，仅 reviewed/exported 状态可导出", err=True)
        raise SystemExit(1)

    all_matches = db.get_matches_by_batch(batch)
    if not all_matches:
        click.echo(f"错误: 批次 {batch} 无匹配记录", err=True)
        raise SystemExit(1)

    diff_matches = [
        m for m in all_matches
        if not (m["status"] == MatchStatus.CONFIRMED and abs(m["amount_diff"]) < 0.001)
    ]
    if not diff_matches:
        click.echo(f"批次 {batch} 无差异记录（全部为零差额已确认匹配）")
        raise SystemExit(1)

    try:
        out_path = export.export_differences(output, diff_matches)
    except Exception as e:
        click.echo(f"导出失败: {e}", err=True)
        raise SystemExit(1)

    db.update_batch_status(batch, BatchStatus.EXPORTED)
    click.echo(f"差异清单已导出: {out_path}")
    click.echo(f"  差异记录: {len(diff_matches)} 条（共 {len(all_matches)} 条匹配中过滤掉 {len(all_matches) - len(diff_matches)} 条零差额已确认记录")


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
