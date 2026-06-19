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

- `amount` 必须为数字，发票号/付款编号在批次内不可重复

## 命令顺序（完整流程）

```bash
# 1. 初始化数据库
inv-recon init

# 2. 导入发票和付款
inv-recon import --invoices samples/invoices.csv --payments samples/payments.csv --name 2024Q1

# 3. （可选）查看/修改匹配规则
inv-recon config                        # 查看当前规则
inv-recon config --tolerance 1.00       # 放宽容差，创建新规则版本

# 4. 执行匹配
inv-recon match --batch 1

# 5. 查看匹配详情
inv-recon show --batch 1

# 6. 复核（交互模式 — 逐条确认/拒绝）
inv-recon review --batch 1

# 6b. 复核（非交互模式 — 单条裁决）
inv-recon review --batch 1 --match-id 3 --action confirm --note "已核实"

# 7. 导出差异清单
inv-recon export --batch 1 --output diff_2024Q1.csv

# 8. 撤销批次
inv-recon revoke --batch 1

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
| `--invoices` | 是 | 发票 CSV 文件路径 |
| `--payments` | 是 | 付款 CSV 文件路径 |
| `--name` | 否 | 批次名称 |

校验失败（金额非数字、发票号重复等）时不写入任何数据。

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
- **conflict**: 同一发票/付款被多笔记录占用

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
确认冲突匹配时，同发票/付款的其他冲突记录自动拒绝。
全部裁决后批次自动变为 `reviewed`。

### `inv-recon revoke`

```
inv-recon revoke --batch BATCH_ID
```

| 参数 | 必填 | 说明 |
|------|------|------|
| `--batch` | 是 | 批次 ID |

批次不存在或已撤销时返回错误。撤销不可恢复。

### `inv-recon export`

```
inv-recon export --batch BATCH_ID --output FILE
```

| 参数 | 必填 | 说明 |
|------|------|------|
| `--batch` | 是 | 批次 ID |
| `--output` | 是 | 导出 CSV 文件路径 |

批次须为 `reviewed` 状态。导出为原子写入，失败时不会产生半截文件。

### `inv-recon list`

列出所有批次及进度（ID、名称、状态、规则版本、发票/付款/匹配/确认/待审数量）。

### `inv-recon show`

```
inv-recon show --batch BATCH_ID
```

| 参数 | 必填 | 说明 |
|------|------|------|
| `--batch` | 是 | 批次 ID |

显示批次详情和所有匹配记录。

## 错误处理

| 场景 | 行为 |
|------|------|
| 金额不是数字 | 导入阶段拒绝，不写入任何数据 |
| 同一发票号重复 | 导入阶段拒绝，不写入任何数据 |
| 同一发票被两笔付款占用 | 匹配阶段标记为 conflict，需人工裁决 |
| 撤销不存在的批次 | 返回错误 |
| 撤销已撤销的批次 | 返回错误 |
| 导出失败 | 不产生半截文件，旧数据不受影响 |
| 匹配失败 | 旧批次状态不变（imported） |

## 持久性

所有数据保存在当前目录的 `inv_recon.db`（SQLite）。可通过环境变量 `INV_RECON_DB` 指定其他路径。换终端重新运行后，规则版本、复核备注、撤销结果和导出的差异行保持一致。

## 批次状态流转

```
imported → matched → reviewed → exported
    ↘          ↘          ↘
     revoked    revoked    revoked
```
