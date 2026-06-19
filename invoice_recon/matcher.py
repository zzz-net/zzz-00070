from .models import Invoice, Payment, Match, MatchType, MatchStatus
from typing import List, Tuple


def match_invoices_payments(
    invoices: List[Invoice],
    payments: List[Payment],
    tolerance: float,
    require_vendor_match: bool,
) -> Tuple[List[Match], List[str]]:
    matches: List[Match] = []
    warnings: List[str] = []
    matched_inv: set = set()
    matched_pay: set = set()
    invoice_by_id = {inv.id: inv for inv in invoices}
    payment_by_id = {pay.id: pay for pay in payments}

    exact_pairs: List[Tuple[int, int, float]] = []
    for inv in invoices:
        for pay in payments:
            if inv.invoice_no == pay.payment_no:
                diff = round(inv.amount - pay.amount, 2)
                if abs(diff) <= tolerance:
                    if require_vendor_match and inv.vendor != pay.vendor:
                        continue
                    exact_pairs.append((inv.id, pay.id, diff))

    inv_exact_count: dict = {}
    for inv_id, pay_id, diff in exact_pairs:
        inv_exact_count[inv_id] = inv_exact_count.get(inv_id, 0) + 1
    pay_exact_count: dict = {}
    for inv_id, pay_id, diff in exact_pairs:
        pay_exact_count[pay_id] = pay_exact_count.get(pay_id, 0) + 1

    conflict_inv_ids = {inv_id for inv_id, c in inv_exact_count.items() if c > 1}
    conflict_pay_ids = {pay_id for pay_id, c in pay_exact_count.items() if c > 1}

    for inv_id, pay_id, diff in exact_pairs:
        inv = invoice_by_id[inv_id]
        pay = payment_by_id[pay_id]
        is_conflict = inv_id in conflict_inv_ids or pay_id in conflict_pay_ids
        if is_conflict:
            matches.append(Match(
                invoice_id=inv_id,
                payment_id=pay_id,
                match_type=MatchType.EXACT,
                amount_diff=diff,
                status=MatchStatus.CONFLICT,
            ))
            if inv_id in conflict_inv_ids:
                warnings.append(
                    f"发票 {inv.invoice_no} 被多笔付款占用 "
                    f"(付款 {pay.payment_no}，差额 {diff:.2f})"
                )
        else:
            matches.append(Match(
                invoice_id=inv_id,
                payment_id=pay_id,
                match_type=MatchType.EXACT,
                amount_diff=diff,
                status=MatchStatus.PENDING,
            ))
            matched_inv.add(inv_id)
            matched_pay.add(pay_id)

    amount_pairs: List[Tuple[int, int, float]] = []
    for inv in invoices:
        if inv.id in matched_inv:
            continue
        for pay in payments:
            if pay.id in matched_pay:
                continue
            diff = round(inv.amount - pay.amount, 2)
            if abs(diff) <= tolerance:
                if require_vendor_match and inv.vendor != pay.vendor:
                    continue
                amount_pairs.append((inv.id, pay.id, diff))

    inv_amt_count: dict = {}
    pay_amt_count: dict = {}
    for inv_id, pay_id, diff in amount_pairs:
        inv_amt_count[inv_id] = inv_amt_count.get(inv_id, 0) + 1
        pay_amt_count[pay_id] = pay_amt_count.get(pay_id, 0) + 1

    amt_conflict_inv = {iid for iid, c in inv_amt_count.items() if c > 1}
    amt_conflict_pay = {pid for pid, c in pay_amt_count.items() if c > 1}

    for inv_id, pay_id, diff in amount_pairs:
        if inv_id in matched_inv:
            continue
        if pay_id in matched_pay:
            continue
        inv = invoice_by_id[inv_id]
        pay = payment_by_id[pay_id]
        is_conflict = inv_id in amt_conflict_inv or pay_id in amt_conflict_pay
        if is_conflict:
            matches.append(Match(
                invoice_id=inv_id,
                payment_id=pay_id,
                match_type=MatchType.AMOUNT_ONLY,
                amount_diff=diff,
                status=MatchStatus.CONFLICT,
            ))
            warnings.append(
                f"发票 {inv.invoice_no} 按金额匹配到多笔付款 "
                f"(付款 {pay.payment_no}，差额 {diff:.2f})"
            )
        else:
            matches.append(Match(
                invoice_id=inv_id,
                payment_id=pay_id,
                match_type=MatchType.AMOUNT_ONLY,
                amount_diff=diff,
                status=MatchStatus.PENDING,
            ))
            matched_inv.add(inv_id)
            matched_pay.add(pay_id)

    for inv in invoices:
        if inv.id not in matched_inv:
            already = any(m.invoice_id == inv.id for m in matches)
            if not already:
                matches.append(Match(
                    invoice_id=inv.id,
                    payment_id=None,
                    match_type=MatchType.UNMATCHED_INVOICE,
                    amount_diff=inv.amount,
                    status=MatchStatus.PENDING,
                ))

    for pay in payments:
        if pay.id not in matched_pay:
            already = any(m.payment_id == pay.id for m in matches)
            if not already:
                matches.append(Match(
                    invoice_id=None,
                    payment_id=pay.id,
                    match_type=MatchType.UNMATCHED_PAYMENT,
                    amount_diff=-pay.amount,
                    status=MatchStatus.PENDING,
                ))

    return matches, warnings
