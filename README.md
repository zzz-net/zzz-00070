# 离线发票勾稽 CLI (inv-recon)

离线发票与付款匹配工具，支持导入、规则配置、自动匹配、人工复核、撤销和导出。

## 安装

```bash
pip install -e .
```

## 样例文件

| 文件 | 说明 |
|------|------|
| `samples/invoices.csv` | 正常发票表（6 条） |
| `samples/payments.csv` | 正常付款表（6 条，含冲突） |
| `samples/invoices_bad.csv` | 错误发票表（金额非数字 + 发票号重复） |

### CSV 格式

**发票表** — 必须包含列: `invoice_no,vendor,amount,date`

```csv
invoice_no,vendor,amount,date
INV-001,ACME Corp,1000.00,2024-01-15
```

**付款表** — 必须包含列: `payment_no,vendor,amount,date`

```csv
payment_no,vendor,amount,date
PAY-001,ACME Corp,1000.00,2024-01-20
```

- `amount` 必须为非负数字；发票号/付款编号在批次内不可重复
- 编码建议使用 UTF-8（支持带 BOM 的 UTF-8）

## 命令顺序（完整流程）

```bash
# 1. 初始化数据库
inv-recon init

# 2. 导入发票和付款（含坏行会跳过合法行、旧批次不受影响）
inv-recon import --invoices samples/invoices.csv --payments samples/payments.csv --name 2024Q1

# 3. （可选）查看/修改匹配规则
inv-recon config                        # 查看当前规则
inv-recon config --tolerance 1.00       # 放宽容差，创建新规则版本

# 4. 执行匹配（产生 pending / conflict / unmatched）
inv-recon match --batch 1

# 5. 查看匹配详情
inv-recon show --batch 1

# 6. 复核（交互模式 — 逐条确认/拒绝/跳过）
inv-recon review --batch 1

# 6b. 复核（非交互模式 — 单条裁决）
inv-recon review --batch 1 --match-id 3 --action confirm --note "已核实"

# 6c. 撤销单条裁决（回到可复核状态，reviewed/exported 也允许撤销）
inv-recon review-undo --batch 1 --match-id 3

# 7. 导出待处理清单（matched/reviewed/exported 都可反复导出）
inv-recon export --batch 1 --output diff_2024Q1.csv

# 8. 撤销整个批次（标记为 revoked，不可逆）
inv-recon revoke --batch 1

# 9. 创建批次快照（保存完整状态，含裁决历史）
inv-recon snapshot create --batch 1 --name 2024Q1_after_review

# 10. 列出所有快照
inv-recon snapshot list

# 11. 查看快照详情
inv-recon snapshot show --snapshot 2024Q1_after_review

# 12. 从快照恢复为新批次（绝不覆盖现有批次）
inv-recon snapshot restore --snapshot 2024Q1_after_review --batch-name 2024Q1_restored

# 随时列出所有批次进度
inv-recon list
```

## 命令参考

### `inv-recon init`

初始化数据库，创建表结构和默认规则 v1。

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

### `inv-recon config`

```
inv-recon config [--tolerance FLOAT] [--require-vendor-match / --no-require-vendor-match]
```

| 参数 | 必填 | 说明 |
|------|------|------|
| `--tolerance` | 否 | 金额容差（默认 0.01） |
| `--require-vendor-match / --no-require-vendor-match` | 否 | 是否要求供应商一致（默认要求） |

不带参数显示当前规则；带参数创建新版本。

### `inv-recon match`

```
inv-recon match --batch BATCH_ID
```

| 参数 | 必填 | 说明 |
|------|------|------|
| `--batch` | 是 | 批次 ID |

批次须为 `imported` 状态。匹配结果包含:
- **exact**: 发票号 = 付款编号，金额在容差内
- **amount_only**: 供应商一致，金额在容差内
- **unmatched_invoice**: 无对应付款
- **unmatched_payment**: 无对应发票
- **conflict**: 同一发票/付款被多笔记录占用（需人工裁决）

### `inv-recon review`

```
inv-recon review --batch BATCH_ID [--match-id ID --action confirm|reject --note TEXT]
```

| 参数 | 必填 | 说明 |
|------|------|------|
| `--batch` | 是 | 批次 ID |
| `--match-id` | 否 | 匹配记录 ID（非交互模式必填） |
| `--action` | 否 | `confirm` 或 `reject`（非交互模式必填） |
| `--note` | 否 | 复核备注 |

交互模式逐条显示，输入 `c` 确认 / `r` 拒绝 / `s` 跳过。
确认冲突匹配时，同发票/付款的其他冲突记录自动拒绝（标记 `auto_rejected`）。
全部裁决后批次自动变为 `reviewed`。

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

### `inv-recon revoke`

```
inv-recon revoke --batch BATCH_ID
```

| 参数 | 必填 | 说明 |
|------|------|------|
| `--batch` | 是 | 批次 ID |

批次不存在或已撤销时返回错误。撤销后标记为 `revoked`，不可逆（不删除数据，仅标记）。

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

### `inv-recon list`

列出所有批次及进度（ID、名称、状态、规则版本、发票/付款/匹配/确认/待审数量）。

### `inv-recon show`

```
inv-recon show --batch BATCH_ID
```

| 参数 | 必填 | 说明 |
|------|------|------|
| `--batch` | 是 | 批次 ID |

显示批次详情和所有匹配记录（含裁决结果和备注）。


## 批次快照与恢复

快照可保存某个批次的完整状态（规则版本、匹配结果、人工裁决、备注、裁决历史），换终端或重启后可恢复继续复核或重新导出。默认快照目录为 \./snapshots/\，可通过环境变量 \INV_RECON_SNAPSHOT_DIR\ 指定。

### \inv-recon snapshot create
\inv-recon snapshot create --batch BATCH_ID [--name NAME]
\
| 参数 | 必填 | 说明 |
|------|------|------|
| \--batch\ | 是 | 批次 ID |
| \--name\ | 否 | 快照名称（默认自动生成） |

为指定批次创建快照。任意状态的批次都可建快照（matched / reviewed / exported / revoked）。

**快照包含：**
- 批次基本信息和状态
- 所使用的规则版本
- 匹配结果和裁决备注
- 完整裁决历史（状态链路不丢失）

### \inv-recon snapshot list
列出所有快照，按创建时间倒序。

### \inv-recon snapshot show
\inv-recon snapshot show --snapshot SNAPSHOT_REF
\
| 参数 | 必填 | 说明 |
|------|------|------|
| \--snapshot\ | 是 | 快照 ID（完整或前缀）或快照名称 |

显示指定快照的详情。

### \inv-recon snapshot restore
\inv-recon snapshot restore --snapshot SNAPSHOT_REF [--batch-name NAME]
\
| 参数 | 必填 | 说明 |
|------|------|------|
| \--snapshot\ | 是 | 快照 ID（完整或前缀）或快照名称 |
| \--batch-name\ | 否 | 新批次名称（默认使用快照内的批次名） |

将快照恢复为新批次：
- 总是作为**全新批次**导入，**绝不覆盖**现有批次数据；
- 所有 ID 重新分配，裁决历史完整保留，状态链路不丢失；
- 同名批次自动重命名（添加 \_2\、\_3\ 后缀）；
- 快照对应的规则版本如库里不存在则自动创建；
- 已撤销 (evoked\) 的批次快照，恢复后保持 evoked\ 状态；
- 恢复后可继续 eview\ / eview-undo\ / \xport\。

## 错误处理

| 场景 | 行为 |
|------|------|
| 金额不是数字 | 导入阶段逐行报告 行号+原因；合法行正常入库，不回滚；旧批次不受影响 |
| 同一发票号/付款编号重复 | 导入阶段逐行报告 行号+原因；合法行正常入库，不回滚；旧批次不受影响 |
| 文件缺少必需列 / 为空 / 无法读取 | 整体失败，不创建新批次，旧批次不受影响 |
| 同一发票被两笔付款占用（冲突） | 匹配阶段标记为 conflict，需人工裁决；其他非冲突匹配正常入库 |
| 撤销不存在的批次 | 返回错误；数据库不变 |
| 撤销已撤销的批次 | 返回错误；数据库不变 |
| 导出失败（磁盘/权限等） | 原子写入：不产生半截文件；原导出文件仍在；批次状态不变 |
| 匹配失败 | 旧批次状态保持 imported，不写任何匹配记录 |
| review-undo 撤销裁决 | 合法范围内撤销，相关联 auto_rejected 也恢复；其他记录不变 |
| 快照创建 | 完整保存批次+规则+匹配+裁决历史；不影响现有批次和数据库 |
| 快照恢复 | 作为全新批次导入，绝不覆盖现有数据；同名自动重命名；裁决历史完整保留 |

## 持久性

所有数据保存在当前目录的 `inv_recon.db`（SQLite）。可通过环境变量 `INV_RECON_DB` 指定其他路径。换终端重新运行后，规则版本、复核备注、撤销结果和导出的差异行保持一致。

## 批次状态流转

```
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
```

说明：review-undo 只撤销单条匹配的上一次裁决，
当 reviewed/exported 批次因撤销重新出现待审记录时，
批次状态会回到 matched，可继续复核或重新导出。
