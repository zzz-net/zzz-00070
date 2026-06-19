import csv
from typing import List, Tuple
from .models import Invoice, Payment


INVOICE_COLUMNS = {"invoice_no", "vendor", "amount", "date"}
PAYMENT_COLUMNS = {"payment_no", "vendor", "amount", "date"}


class ValidationError(Exception):
    def __init__(self, errors: List[str]):
        self.errors = errors
        super().__init__("\n".join(errors))


def parse_invoices(filepath: str) -> List[Invoice]:
    errors: List[str] = []
    rows = _read_csv(filepath)
    if not rows:
        raise ValidationError([f"文件 {filepath} 为空或无法读取"])

    headers = set(rows[0].keys())
    missing = INVOICE_COLUMNS - headers
    if missing:
        raise ValidationError([f"发票文件缺少列: {', '.join(sorted(missing))}"])

    invoices: List[Invoice] = []
    seen_nos: dict = {}
    for i, row in enumerate(rows, start=2):
        inv_no = row["invoice_no"].strip()
        vendor = row["vendor"].strip()
        amount_str = row["amount"].strip()
        date_str = row["date"].strip()

        if not inv_no:
            errors.append(f"第 {i} 行: 发票号为空")
            continue
        if inv_no in seen_nos:
            errors.append(f"第 {i} 行: 发票号 {inv_no} 与第 {seen_nos[inv_no]} 行重复")
            continue
        seen_nos[inv_no] = i

        amount, err = _parse_amount(amount_str, i, "发票")
        if err:
            errors.append(err)
            continue

        if not vendor:
            errors.append(f"第 {i} 行: 供应商为空")
            continue
        if not date_str:
            errors.append(f"第 {i} 行: 日期为空")
            continue

        invoices.append(Invoice(
            invoice_no=inv_no,
            vendor=vendor,
            amount=amount,
            date=date_str,
        ))

    if errors:
        raise ValidationError(errors)
    return invoices


def parse_payments(filepath: str) -> List[Payment]:
    errors: List[str] = []
    rows = _read_csv(filepath)
    if not rows:
        raise ValidationError([f"文件 {filepath} 为空或无法读取"])

    headers = set(rows[0].keys())
    missing = PAYMENT_COLUMNS - headers
    if missing:
        raise ValidationError([f"付款文件缺少列: {', '.join(sorted(missing))}"])

    payments: List[Payment] = []
    seen_nos: dict = {}
    for i, row in enumerate(rows, start=2):
        pay_no = row["payment_no"].strip()
        vendor = row["vendor"].strip()
        amount_str = row["amount"].strip()
        date_str = row["date"].strip()

        if not pay_no:
            errors.append(f"第 {i} 行: 付款编号为空")
            continue
        if pay_no in seen_nos:
            errors.append(f"第 {i} 行: 付款编号 {pay_no} 与第 {seen_nos[pay_no]} 行重复")
            continue
        seen_nos[pay_no] = i

        amount, err = _parse_amount(amount_str, i, "付款")
        if err:
            errors.append(err)
            continue

        if not vendor:
            errors.append(f"第 {i} 行: 供应商为空")
            continue
        if not date_str:
            errors.append(f"第 {i} 行: 日期为空")
            continue

        payments.append(Payment(
            payment_no=pay_no,
            vendor=vendor,
            amount=amount,
            date=date_str,
        ))

    if errors:
        raise ValidationError(errors)
    return payments


def _parse_amount(raw: str, line: int, label: str) -> Tuple[float, str]:
    raw = raw.replace(",", "")
    try:
        val = float(raw)
    except (ValueError, TypeError):
        return 0.0, f"第 {line} 行: {label}金额不是有效数字 '{raw}'"
    if val < 0:
        return 0.0, f"第 {line} 行: {label}金额不能为负数 '{raw}'"
    return round(val, 2), ""


def _read_csv(filepath: str) -> List[dict]:
    with open(filepath, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))
