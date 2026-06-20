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

# 13. 打包批次为可搬运包（用于跨机器迁移）
inv-recon pack --batch 1 --output 2024Q1_transfer.invpkg --name 2024Q1_for_audit

# 14. （可选）在导入前校验包完整性
inv-recon verify --input 2024Q1_transfer.invpkg

# 15. （可选）查看包元信息（不导入）
inv-recon inspect --input 2024Q1_transfer.invpkg

# 16. 在新机器/新目录中导入包（自动处理同名冲突）
inv-recon unpack --input 2024Q1_transfer.invpkg

# 导入后可继续所有操作
inv-recon list
inv-recon show --batch 1
inv-recon review --batch 1 --match-id 2 --action reject --note "跨机器复核"
inv-recon review-undo --batch 1 --match-id 2
inv-recon export --batch 1 --output 2024Q1_new_machine.csv
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

快照可保存某个批次的完整状态（规则版本、匹配结果、人工裁决、备注、裁决历史），换终端或重启后可恢复继续复核或重新导出。默认快照目录为 `./snapshots/`，可通过环境变量 `INV_RECON_SNAPSHOT_DIR` 指定。

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

### `inv-recon snapshot list`

列出所有快照，按创建时间倒序。

### \inv-recon snapshot show
\inv-recon snapshot show --snapshot SNAPSHOT_REF
\
| 参数 | 必填 | 说明 |
|------|------|------|
| \--snapshot\ | 是 | 快照 ID（完整或前缀）或快照名称 |

显示指定快照的详情。

### `inv-recon snapshot restore`

```
inv-recon snapshot restore --snapshot ID_OR_NAME [--batch-name NEW_NAME]
```

| 参数 | 必填 | 说明 |
|------|------|------|
| `--snapshot` | 是 | 快照 ID（完整或前缀）或快照名称 |
| `--batch-name` | 否 | 新批次名称（默认使用快照内的批次名） |

将快照恢复为新批次：
- 总是作为**全新批次**导入，**绝不覆盖**现有批次数据；
- 所有 ID 重新分配，裁决历史完整保留，状态链路不丢失；
- 同名批次自动重命名（添加 `_2`、`_3` 后缀）；
- 快照对应的规则版本如库里不存在则自动创建；
- 已撤销 (`revoked`) 的批次快照，恢复后保持 `revoked` 状态；
- 恢复后可继续 `review` / `review-undo` / `export`。

## 快照打包与跨机器搬运

将批次连同规则版本、人工裁决备注、待导出结果和必要元数据打成可搬运包 (`.invpkg`)，在另一台机器或新目录里导入后继续 `list`/`show`/`review`/`export`。

### `inv-recon pack`

```
inv-recon pack --batch BATCH_ID [--output FILE] [--name NAME] [--include-export / --no-include-export] [--force]
```

| 参数 | 必填 | 说明 |
|------|------|------|
| `--batch` | 是 | 批次 ID |
| `--output` | 否 | 输出包文件路径（默认自动生成） |
| `--name` | 否 | 包名称（默认基于批次名） |
| `--include-export / --no-include-export` | 否 | 是否包含待导出结果（默认包含） |
| `--force` | 否 | 输出文件已存在时强制覆盖 |

将指定批次打包为 `.invpkg` 可搬运包。任意状态的批次都可打包。

**包内包含：**
- `manifest.json` — 元数据（打包时间、工具版本、源机器、记录数等）
- `snapshot.json` — 完整快照（批次+规则+发票+付款+匹配+裁决历史）
- `checksums.json` — 各文件 SHA256 校验和
- `export.csv` — 可选，待导出结果（reviewed/exported 状态时自动包含）

### `inv-recon unpack`

```
inv-recon unpack --input FILE [--batch-name NEW_NAME] [--force]
```

| 参数 | 必填 | 说明 |
|------|------|------|
| `--input` | 是 | 可搬运包文件路径 |
| `--batch-name` | 否 | 新批次名称（默认使用包内批次名） |
| `--force` | 否 | 同名快照文件已存在时强制覆盖 |

导入可搬运包，恢复为新批次：

**导入前校验：**
1. 校验包完整性（必需文件存在、校验和匹配、格式正确）；
2. 若不完整则拒绝导入并列出问题；
3. 快照目录不存在则自动创建。

**冲突处理：**
- 同名批次：自动在名称后加 `_2`、`_3` 后缀；
- 同名快照文件：报错，需 `--force` 覆盖；
- 绝不覆盖现有批次数据（始终作为新批次导入）。

**导入后校验报告：**
- 显示哪些裁决记录沿用了原状态（confirmed/rejected）；
- 显示哪些记录被重命名（ID 重新分配）；
- 显示规则版本是否需要自动创建。

导入后可继续 `list`/`show`/`review`/`review-undo`/`export`。

### `inv-recon verify`

```
inv-recon verify --input FILE
```

| 参数 | 必填 | 说明 |
|------|------|------|
| `--input` | 是 | 可搬运包文件路径 |

校验可搬运包的完整性和有效性（不导入）。检查：必需文件、校验和、格式、schema 版本兼容性。

### `inv-recon inspect`

```
inv-recon inspect --input FILE
```

| 参数 | 必填 | 说明 |
|------|------|------|
| `--input` | 是 | 可搬运包文件路径 |

查看包的元信息（不导入数据库）。显示：批次名、状态、规则版本、记录数、打包时间、源机器等。

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
| 打包输出文件已存在 | 报错不覆盖；需 --force 才覆盖；保护已有包文件 |
| 导入同名批次 | 自动重命名加 _2/_3 后缀；绝不覆盖现有批次数据 |
| 导入包内容不完整/损坏 | 导入前校验，不完整则拒绝导入并列出问题；数据库不受影响 |
| 导入快照目录缺失 | 自动创建快照目录；不报错 |
| 导入包校验和不匹配 | 拒绝导入；保护数据完整性优先 |

## 持久性

所有数据保存在当前目录的 `inv_recon.db`（SQLite）。可通过环境变量 `INV_RECON_DB` 指定其他路径。换终端重新运行后，规则版本、复核备注、撤销结果和导出的差异行保持一致。

## 任务回放与证据包

将一次复杂操作的输入、配置、关键步骤、冲突处理、撤销结果和异常日志串成可重放记录，持久化到 SQLite，跨重启可查。

### 命令一览

| 命令 | 作用 |
|------|------|
| `replay start` | 开始一个回放会话 |
| `replay list` | 列出回放会话（支持筛选） |
| `replay show <id>` | 查看会话详情和步骤 |
| `replay export` | 导出证据包（JSON/CSV/ZIP） |
| `replay import` | 导入证据包 |
| `replay undo <id>` | 撤销回放会话 |
| `replay config` | 查看/修改回放配置 |
| `replay verify` | 校验证据包完整性 |

### 开始回放会话

```bash
inv-recon replay start --name "批次导入" --description "导入2024年1月发票" \
  --operator alice --batch 1 --input '{"file": "invoices.csv"}'
```

开始后会返回会话 ID，后续可通过 API 添加步骤。

### 列出回放会话

```bash
# 全部列出
inv-recon replay list

# 按操作者筛选
inv-recon replay list --operator alice

# 按批次筛选
inv-recon replay list --batch 1

# 按结果筛选（running/success/failure/error/undone）
inv-recon replay list --result success

# 按步骤动作筛选（包含该动作的会话）
inv-recon replay list --action import

# 按时间范围
inv-recon replay list --from 2024-01-01 --to 2024-12-31

# 限制条数
inv-recon replay list --limit 10
```

### 查看回放详情

```bash
inv-recon replay show 1
```

显示会话信息、配置快照、输入摘要和所有步骤（含详情、错误信息）。

### 导出证据包

支持三种格式：JSON、CSV、ZIP（`.reppkg`）。

```bash
# JSON 格式（包含所有会话和步骤）
inv-recon replay export --output replay.json --format json

# CSV 格式（会话和步骤混合，用 type 列区分）
inv-recon replay export --output replay.csv --format csv

# ZIP 压缩包（包含 manifest、sessions、steps、校验和）
inv-recon replay export --output replay.reppkg --format zip

# 带筛选条件导出
inv-recon replay export --output filtered.json --format json --operator alice --result success
```

**导出保证**：
- 目标文件已存在时报错不覆盖，保护已有数据
- 目标目录不可写时报错
- 原子写入：失败不产生半截文件

### 导入证据包

```bash
# 普通导入
inv-recon replay import --input replay.json

# 强制导入（版本不兼容或重复会话时覆盖）
inv-recon replay import --input replay.json --force
```

**导入规则**：
- 导入前校验包完整性，不完整则拒绝导入
- 版本不兼容时报错，`--force` 可强制导入
- 重复会话（session_key 相同）默认跳过，`--force` 会覆盖
- 不覆盖已有数据，始终作为新记录导入
- 只读目录/不可写目录会报错

### 撤销回放会话

```bash
inv-recon replay undo 1 --note "操作有误，已撤销"
```

撤销后：
- 会话标记为 `undone` 状态
- 保留所有步骤记录，仍可回看
- 不能再添加新步骤

### 回放配置

```bash
# 查看当前配置
inv-recon replay config

# 开启/关闭明细采集
inv-recon replay config --detail
inv-recon replay config --no-detail

# 设置脱敏字段（逗号分隔）
inv-recon replay config --masked-fields password,secret_key,token

# 设置保留天数（0 表示永久保留）
inv-recon replay config --retention-days 90
```

**非法配置会被拦住并写清原因**：
- `retention_days` 不能为负数
- `detail_enabled` 必须是布尔值
- `masked_fields` 不能包含空字符串

### 校验证据包

```bash
inv-recon replay verify --input replay.reppkg
```

检查项：
- 文件存在且格式正确
- 必需文件齐全（manifest/sessions/steps/checksums）
- 校验和匹配
- schema 版本兼容性

## 操作录制与证据回灌

通过 `drill` 命令组实现操作自动录制。开始一次演练后，后续的导入、匹配、复核、撤销、导出等命令都会**自动挂到同一条回放轨迹中**，无需手动创建会话和补步骤。

每一步自动记录：输入摘要、配置快照、批次号、结果、异常、操作者。

### 命令一览

| 命令 | 作用 |
|------|------|
| `drill begin` | 开始一次操作演练 |
| `drill end` | 结束演练，收口成 success/failure/error |
| `drill undo` | 撤销当前演练，标记为 undone |
| `drill status` | 查看当前演练状态 |

### 完整使用示例

```bash
# 1. 开始演练
inv-recon drill begin --name "2024年1月对账" \
  --description "核对2024年1月供应商发票" \
  --operator alice

# 2. 后续命令自动录制（无需手动操作）
inv-recon import --invoices invoices_202401.csv --payments payments_202401.csv
inv-recon match --batch 1
inv-recon review --batch 1 --match-id 1 --action confirm
inv-recon review-undo --batch 1 --match-id 1
inv-recon export --batch 1 --output result.csv

# 3. 结束演练（自动收口）
inv-recon drill end --result success

# 或撤销演练
inv-recon drill undo --note "操作有误，重新核对"
```

### 跨进程/跨重启使用示例

演练活动状态持久化到 SQLite，即使进程退出或切换终端，也能恢复并继续：

```bash
# 终端 1：开始演练
inv-recon drill begin --name "月末对账" --operator bob
inv-recon import --invoices jan.csv --payments jan_pay.csv

# ... 进程退出或关闭终端 ...

# 终端 2：恢复演练，继续操作
inv-recon drill resume
inv-recon match --batch 1
inv-recon drill end --result success
```

### 开始演练

```bash
# 最简用法
inv-recon drill begin --name "对账演练"

# 带操作者和描述
inv-recon drill begin --name "1月对账" --operator alice \
  --description "处理1月供应商发票与付款勾稽"

# 指定关联批次
inv-recon drill begin --name "批次1复核" --batch 1

# 带输入摘要
inv-recon drill begin --name "导入演练" \
  --input '{"source": "财务系统", "period": "2024-01"}'
```

开始后会返回会话 ID，并提示后续命令将自动录制。

### 查看演练状态

```bash
inv-recon drill status
```

输出示例：
```
● 演练进行中
  会话 ID: 1
  名称: 2024年1月对账
  开始时间: 2024-01-15 10:30:00
  操作者: alice
  当前批次: 1 (2024-01-supplier)
  已执行步骤: 5

  步骤列表:
    ✓ [1] drill_begin - 演练开始
    ✓ [2] import - 导入发票和付款数据
    ✓ [3] match - 执行发票与付款匹配
    ✓ [4] review - 复核匹配结果
    ✓ [5] export - 导出差异结果文件
```

### 结束演练

```bash
# 成功结束
inv-recon drill end --result success

# 失败结束，带错误信息
inv-recon drill end --result failure --error-message "匹配冲突未解决"

# 错误结束
inv-recon drill end --result error --error-message "程序异常"
```

### 撤销演练

```bash
# 撤销当前演练
inv-recon drill undo

# 带备注
inv-recon drill undo --note "数据有误，需要重新导入"
```

撤销后：
- 会话标记为 `undone` 状态
- 所有步骤记录保留，可通过 `replay show` 回看
- 不能再添加新步骤

### 无活动演练时的行为

- 命令正常执行，不影响正常使用
- 不产生任何录制记录
- `drill status` 显示"○ 无活动演练"

### 查看演练记录

演练录制的记录与普通回放会话共享同一套查询接口：

```bash
# 列出所有演练（支持筛选）
inv-recon replay list --operator alice --result success

# 查看演练详情（含所有步骤）
inv-recon replay show 1

# 导出演练证据包
inv-recon replay export --output drill_202401.reppkg --format zip

# 按批次筛选演练
inv-recon replay list --batch 1

# 按时间段筛选
inv-recon replay list --from 2024-01-01 --to 2024-01-31
```

### 自动录制的命令列表

以下命令在演练期间会自动录制：

| 命令 | 动作名称 | 描述 |
|------|----------|------|
| `import` | `import` | 导入发票和付款数据 |
| `match` | `match` | 执行发票与付款匹配 |
| `review` | `review` | 复核匹配结果 |
| `review-undo` | `review-undo` | 撤销单条匹配裁决 |
| `export` | `export` | 导出差异结果文件 |
| `revoke` | `revoke` | 撤销整个批次 |

### 录制内容说明

每一步自动记录：
- **输入摘要**：命令参数（脱敏后）
- **配置快照**：录制开始时的回放配置
- **批次号**：操作关联的批次 ID
- **结果**：success / failure / error
- **异常信息**：异常类型、消息、堆栈
- **操作者**：演练开始时指定的操作者
- **时间戳**：操作执行时间

### 配置对录制的影响

回放配置同样适用于操作录制：

```bash
# 关闭明细采集，不记录 input_args
inv-recon replay config --no-detail

# 设置脱敏字段
inv-recon replay config --masked-fields password,api_key,token

# 设置保留天数
inv-recon replay config --retention-days 180
```

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
