from enum import Enum
from dataclasses import dataclass, field
from typing import Optional


class BatchStatus(str, Enum):
    IMPORTED = "imported"
    MATCHED = "matched"
    REVIEWED = "reviewed"
    EXPORTED = "exported"
    REVOKED = "revoked"


class MatchType(str, Enum):
    EXACT = "exact"
    AMOUNT_ONLY = "amount_only"
    UNMATCHED_INVOICE = "unmatched_invoice"
    UNMATCHED_PAYMENT = "unmatched_payment"


class MatchStatus(str, Enum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    CONFLICT = "conflict"


@dataclass
class RuleVersion:
    id: Optional[int] = None
    version: str = ""
    tolerance: float = 0.01
    require_vendor_match: bool = True
    created_at: str = ""


@dataclass
class Batch:
    id: Optional[int] = None
    name: str = ""
    status: str = BatchStatus.IMPORTED
    rule_version: str = ""
    created_at: str = ""
    updated_at: str = ""


@dataclass
class Invoice:
    id: Optional[int] = None
    batch_id: Optional[int] = None
    invoice_no: str = ""
    vendor: str = ""
    amount: float = 0.0
    date: str = ""


@dataclass
class Payment:
    id: Optional[int] = None
    batch_id: Optional[int] = None
    payment_no: str = ""
    vendor: str = ""
    amount: float = 0.0
    date: str = ""


@dataclass
class Match:
    id: Optional[int] = None
    batch_id: Optional[int] = None
    invoice_id: Optional[int] = None
    payment_id: Optional[int] = None
    match_type: str = MatchType.EXACT
    amount_diff: float = 0.0
    status: str = MatchStatus.PENDING
    review_note: Optional[str] = None
    adjudication: Optional[str] = None


@dataclass
class Adjudication:
    id: Optional[int] = None
    match_id: Optional[int] = None
    batch_id: Optional[int] = None
    action: str = ""
    note: Optional[str] = None
    created_at: str = ""
