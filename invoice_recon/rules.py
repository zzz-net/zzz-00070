"""
规则与文案的单一来源。

所有用户可见的规则说明、CLI 帮助文本、状态流转、
导出过滤逻辑、README 片段都集中在这里，
避免代码变了说明还停留在旧规则。
"""

from typing import List

from .models import MatchStatus


# ---------------------------------------------------------------------------
# 导入规则
# ---------------------------------------------------------------------------

IMPORT_RULES_HELP = (
    "导入发票和付款表，创建新批次。\n\n"
    "发票 CSV 必需列: invoice_no,vendor,amount,date\n"
    "付款 CSV 必需列: payment_no,vendor,amount,date\n\n"
    "【规则】遇到坏行时：\n"
    "  (1) 合法行继续入库，不回滚；\n"
    "  (2) 坏行明确报出 行号 + 原因（金额非数字、编号重复、空值等）；\n"
    "  (3) 旧批次状态绝不被带坏（每批独立写入，互不影响）。\n\n"
    "整体失败场景（才会退出不写库）：\n"
    "  - 文件不存在 / 无法读取 / 为空\n"
    "  - 缺少必需列\n"
    "  - 发票和付款两边都无任何合法行"
)

IMPORT_INVOICES_HELP = (
    "发票 CSV 文件路径。必需列: invoice_no,vendor,amount,date。"
    "金额必须为非负数字，发票号在批次内不可重复；坏行跳过并报行号+原因，合法行继续入库。"
)

IMPORT_PAYMENTS_HELP = (
    "付款 CSV 文件路径。必需列: payment_no,vendor,amount,date。"
    "金额必须为非负数字，付款编号在批次内不可重复；坏行跳过并报行号+原因，合法行继续入库。"
)

IMPORT_OK_HINT_LEGACY = "  提示：其他旧批次状态未受影响"
IMPORT_OK_HINT_BAD_ROWS_PREFIX = "  ⚠ 跳过"
IMPORT_OK_HINT_BAD_ROWS_SUFFIX = "（合法数据已正常入库）"
IMPORT_OK_HINT_LEGACY_ROW = "  ✅ 旧批次状态未受影响"


# ---------------------------------------------------------------------------
# 导出规则
# ---------------------------------------------------------------------------

EXPORT_RULES_HELP = (
    "导出待处理清单 (CSV)。\n\n"
    "【只导出还需要处理的记录】\n"
    "  (a) pending / conflict —— 待人工复核的未解决记录；\n"
    "  (b) 有差额的 confirmed —— 已确认但金额有差异，需要后续跟进。\n\n"
    "【不导出】\n"
    "  - 已拒绝 (rejected / auto_rejected)\n"
    "  - 零差额且已确认的记录\n\n"
    "【可导出的批次状态】\n"
    "  matched / reviewed / exported（含撤销裁决后回到 matched 的情况），\n"
    "  可反复导出覆盖最新结果。原子写入，不会产生半截文件。\n"
    "  成功导出后批次状态标记为 exported。"
)

EXPORT_OUTPUT_HELP = "导出 CSV 文件路径（原子写入：先写临时文件再 replace，不会产生半截文件）"

EXPORT_OK_PREFIX = "待处理清单已导出"
EXPORT_OK_BREAKDOWN = (
    "  导出 {exported} 条（共 {total} 条匹配中，"
    "过滤掉 {skipped_rejected} 条已拒绝、{skipped_clean} 条零差额已确认）"
)

EXPORT_FILTER_REASON_PENDING = "待复核 (pending)"
EXPORT_FILTER_REASON_CONFLICT = "待复核 (conflict)"
EXPORT_FILTER_REASON_HAS_DIFF = "已确认但有差额"
EXPORT_FILTER_REASON_REJECTED = "已拒绝 → 不导出"
EXPORT_FILTER_REASON_CLEAN_CONFIRMED = "零差额已确认 → 不导出"


def should_export_match(m: dict, diff_epsilon: float = 0.001) -> bool:
    """导出过滤函数 —— 代码逻辑与文案共用这一处，避免漂移。

    返回 True 代表这条要导出，False 代表过滤掉。
    """
    status = m.get("status", "")
    if status in (MatchStatus.PENDING, MatchStatus.CONFLICT):
        return True
    if status == MatchStatus.CONFIRMED:
        amount_diff = float(m.get("amount_diff", 0.0))
        return abs(amount_diff) >= diff_epsilon
    return False


def explain_export_filter(m: dict, diff_epsilon: float = 0.001) -> str:
    """返回单条记录在导出过滤中的理由（调试 / 测试用）。"""
    status = m.get("status", "")
    if status == MatchStatus.PENDING:
        return EXPORT_FILTER_REASON_PENDING
    if status == MatchStatus.CONFLICT:
        return EXPORT_FILTER_REASON_CONFLICT
    if status == MatchStatus.CONFIRMED:
        amount_diff = float(m.get("amount_diff", 0.0))
        if abs(amount_diff) >= diff_epsilon:
            return EXPORT_FILTER_REASON_HAS_DIFF
        return EXPORT_FILTER_REASON_CLEAN_CONFIRMED
    if status in (MatchStatus.REJECTED,):
        return EXPORT_FILTER_REASON_REJECTED
    return f"状态 {status} → 不导出"


# ---------------------------------------------------------------------------
# 批次状态流转 —— 文字说明 + ASCII 图
# ---------------------------------------------------------------------------

BATCH_STATUS_FLOW_DIAGRAM = """\
imported ──match──► matched ──review──► reviewed ──export──► exported
    │                  │                   │                  │
    │ revoke           │ revoke            │ revoke           │ revoke
    ▼                  ▼                   ▼                  ▼
  revoked            revoked             revoked            revoked

                       │                   │                  │
                       │ review-undo       │ review-undo      │ review-undo
                       │ (单条裁决撤销)    │ (单条裁决撤销)   │ (单条裁决撤销)
                       ▼                   ▼                  ▼
                    matched ◄──────────────┘                  │
                       ▲                                      │
                       └──────────────────────────────────────┘
"""

BATCH_STATUS_FLOW_CAPTION = (
    "说明：review-undo 只撤销单条匹配的上一次裁决，\n"
    "当 reviewed/exported 批次因撤销重新出现待审记录时，\n"
    "批次状态会回到 matched，可继续复核或重新导出。"
)

REVIEW_UNDO_RULES_HELP = (
    "撤销单条匹配的上一次裁决，恢复复核前的状态和备注。\n\n"
    "【规则】\n"
    "  - 仅能撤销上一次人工裁决（confirm 或 reject）。\n"
    "  - 撤销后该条匹配回到 pending / conflict 状态，可再次复核。\n"
    "  - 如撤销的是对冲突记录的 confirm，\n"
    "    被 auto_rejected 自动拒绝的关联冲突记录也会一并恢复到 conflict。\n"
    "  - 批次在 reviewed / exported 状态时也允许撤销；\n"
    "    只要重新出现待审记录，批次状态就会回到 matched，\n"
    "    之后可以继续 review 或重新 export。\n"
    "  - 其他批次不受影响。"
)


# ---------------------------------------------------------------------------
# 错误处理速查表（README 表格内容）
# ---------------------------------------------------------------------------

ERROR_TABLE_ROWS = [
    ("金额不是数字",
     "导入阶段逐行报告 行号+原因；合法行正常入库，不回滚；旧批次不受影响"),
    ("同一发票号/付款编号重复",
     "导入阶段逐行报告 行号+原因；合法行正常入库，不回滚；旧批次不受影响"),
    ("文件缺少必需列 / 为空 / 无法读取",
     "整体失败，不创建新批次，旧批次不受影响"),
    ("同一发票被两笔付款占用（冲突）",
     "匹配阶段标记为 conflict，需人工裁决；其他非冲突匹配正常入库"),
    ("撤销不存在的批次",
     "返回错误；数据库不变"),
    ("撤销已撤销的批次",
     "返回错误；数据库不变"),
    ("导出失败（磁盘/权限等）",
     "原子写入：不产生半截文件；原导出文件仍在；批次状态不变"),
    ("匹配失败",
     "旧批次状态保持 imported，不写任何匹配记录"),
    ("review-undo 撤销裁决",
     "合法范围内撤销，相关联 auto_rejected 也恢复；其他记录不变"),
]


# ---------------------------------------------------------------------------
# README 命令说明片段（保持 CLI --help 与 README 一致）
# ---------------------------------------------------------------------------

README_SECTION_IMPORT = """\
### `inv-recon import`

```
inv-recon import --invoices FILE --payments FILE [--name NAME]
```

| 参数 | 必填 | 说明 |
|------|------|------|
| `--invoices` | 是 | 发票 CSV 文件路径（必需列: invoice_no,vendor,amount,date） |
| `--payments` | 是 | 付款 CSV 文件路径（必需列: payment_no,vendor,amount,date） |
| `--name` | 否 | 批次名称（默认自动生成） |

【遇到坏行的三条保证】
1. **合法行继续入库** —— 不因为某几行坏数据而整体回滚；
2. **坏行明确报 行号 + 原因** —— 金额非数字、编号重复、空值等逐一列出；
3. **旧批次状态不被带坏** —— 导入只写新批次，绝不触碰已有批次的任何数据。

整体失败（不写库、直接退出）场景：文件不存在 / 无法读取 / 为空 / 缺少必需列 / 两边都无任何合法行。
"""

README_SECTION_EXPORT = """\
### `inv-recon export`

```
inv-recon export --batch BATCH_ID --output FILE
```

| 参数 | 必填 | 说明 |
|------|------|------|
| `--batch` | 是 | 批次 ID |
| `--output` | 是 | 导出 CSV 文件路径（原子写入，不会产生半截文件） |

【只导出还需要处理的记录】
- `pending` / `conflict` —— 待人工复核的未解决记录；
- 有差额的 `confirmed` —— 已确认但金额有差异，需要后续跟进。

【不导出】已拒绝 (`rejected` / `auto_rejected`) 和零差额已确认的记录。

【可导出的批次状态】`matched` / `reviewed` / `exported`（含撤销裁决后回到 `matched` 的情况），可反复导出覆盖最新结果。成功导出后批次状态变为 `exported`。
"""

README_SECTION_REVIEW_UNDO = """\
### `inv-recon review-undo`

```
inv-recon review-undo --batch BATCH_ID --match-id MATCH_ID
```

| 参数 | 必填 | 说明 |
|------|------|------|
| `--batch` | 是 | 批次 ID |
| `--match-id` | 是 | 要撤销的匹配记录 ID |

撤销单条匹配的**上一次**人工裁决，恢复复核前的状态和备注：
- 仅能撤销 `confirm` 或 `reject`；
- 撤销后回到 `pending` / `conflict`，可再次复核；
- 如撤销的是冲突记录的 `confirm`，被 `auto_rejected` 自动拒绝的关联冲突记录会一并恢复；
- 批次在 `reviewed` / `exported` 状态时也允许撤销；只要重新出现待审记录，批次状态会回到 `matched`，之后可继续 `review` 或重新 `export`；
- 其他批次不受影响。
"""


README_SECTION_STATUS_FLOW = """\
## 批次状态流转

```
""" + BATCH_STATUS_FLOW_DIAGRAM.rstrip() + """
```

""" + BATCH_STATUS_FLOW_CAPTION


README_SECTION_ERROR_HANDLING_HEADER = (
    "## 错误处理\n\n"
    "| 场景 | 行为 |\n"
    "|------|------|\n"
)


def build_readme_error_table() -> str:
    """把 ERROR_TABLE_ROWS 渲染成 Markdown 表格，供 README 和测试共用。"""
    lines = [README_SECTION_ERROR_HANDLING_HEADER.rstrip()]
    for scenario, behavior in ERROR_TABLE_ROWS:
        lines.append(f"| {scenario} | {behavior} |")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# 测试辅助：所有对外承诺的清单（测试用来核对 README 与真实行为一致）
# ---------------------------------------------------------------------------

PUBLIC_CONTRACTS: List[str] = [
    "导入坏行三条保证：合法行入库、坏行报行号+原因、旧批次不被带坏",
    "导出只含 pending/conflict/有差额confirmed；不含 rejected/零差额confirmed",
    "导出允许状态：matched / reviewed / exported / 撤销后回到 matched",
    "review-undo：撤销 confirm 时 auto_rejected 兄弟一并恢复",
    "review-undo：reviewed/exported 因撤销出现待审 → 批次回到 matched",
    "持久性：重启 CLI 后 show/list/export 状态与操作前一致",
    "导出原子写入：失败不产生半截文件",
]
