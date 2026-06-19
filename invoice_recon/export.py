import csv
import tempfile
import os
from typing import List


def export_differences(filepath: str, matches: List[dict]) -> str:
    header = [
        "match_id", "match_type", "status",
        "invoice_no", "inv_vendor", "inv_amount", "inv_date",
        "payment_no", "pay_vendor", "pay_amount", "pay_date",
        "amount_diff", "adjudication", "review_note",
    ]
    rows = []
    for m in matches:
        rows.append([
            m.get("id", ""),
            m.get("match_type", ""),
            m.get("status", ""),
            m.get("invoice_no", ""),
            m.get("inv_vendor", ""),
            m.get("inv_amount", ""),
            m.get("inv_date", ""),
            m.get("payment_no", ""),
            m.get("pay_vendor", ""),
            m.get("pay_amount", ""),
            m.get("pay_date", ""),
            f"{m.get('amount_diff', 0):.2f}",
            m.get("adjudication", ""),
            m.get("review_note", ""),
        ])

    abs_path = os.path.abspath(filepath)
    dir_path = os.path.dirname(abs_path)
    if dir_path:
        os.makedirs(dir_path, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=dir_path or ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(header)
            writer.writerows(rows)
        os.replace(tmp_path, abs_path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    return abs_path
