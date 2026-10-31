# 数据与状态模型（候选）

本文件是设计，不是已实现 schema；其受信授权机制仍待决定。业务任务、尝试及上游 ID 不同，均须显式区分。

## 实体

| 实体 | 核心字段和约束 |
|---|---|
| Reference | `ref_id`、类别 `artwork/form`、受限根内文件引用、实际 MIME JPEG/PNG、字节数、内容摘要；可选视角、区域、版本、重要性。每任务 artwork 1–2、form 1–6；单图及总大小上限待决，读取并验证真实内容。 |
| Task | `task_id` 唯一、参考集合及其内容摘要、自然语言要求、确认的包装版本/冲突/不确定性、选择结果和修订版本、创建时间。输入或选择变更生成新 revision 并作废原授权。 |
| Confirmation / Approval | 分开记录可信来源的业务选择与收费授权，包含主体/范围、task/revision/input_digest、版本/子集/不确定性确认、模型/端点/分组/费用操作、失效时间和风险确认；收费授权一次性事务消费。来源与验证方式待决定，不接受未经验证的布尔值。不得保存令牌明文或秘密。 |
| Attempt | 唯一 `attempt_id`、task_id、可选 previous_attempt_id、授权引用、供应商/模型/端点/分组、配置摘要、上游状态与本地交付状态、上游 ID（可空）、上游原生状态、脱敏请求 ID、时间、失败/未知原因、证据来源。结果不明的新尝试必须有独立新授权和原尝试关联。 |
| Artifact | 仅成功尝试可有一个主要候选图，包含 attempt_id/task_id、受控路径、MIME、内容摘要及可得审核信息；不得将旧尝试当成新结果。候选与人工验收结果分开，外部业务系统记录最终裁决。 |
| Measurement | 尝试级别的 usage、model/provider、预计价格、供应商响应 `cost`、调用及端到端耗时：每项包含值、单位、来源、采集时间、`known/unknown`；有效 `cost` 为供应商报告费用，缺失或无效为 unknown，币种和单位独立判定，同一费用多次查询不重复累加。估计和实际分开，不以 token 估计作为账单。 |

## 状态和证据

状态分为独立的本地任务、上游尝试及本地交付三维，不将上游成功等同交付成功；完整定义、转移条件及冲突恢复以 [架构审查决策](contracts/architecture-review.md) 为准。

- **本地任务**：`draft → awaiting_confirmation → ready → active → closed`，允许按实际条件调整；`ready` 不代表授权已取得，`closed` 不代表上游失败。
- **上游尝试**：`not_submitted → submission_uncertain → accepted/running → succeeded/failed/unknown`，可依实际证据跳过 accepted/running；持久化提交意图在网络发送前，重启不得自动重提。unknown 原尝试仅凭可靠查询证据更新；新授权产生独立关联尝试。
- **本地交付**：`not_available / pending / delivered / download_failed / save_failed / missing_or_corrupt`；以受控文件可读、摘要及数据库关联为依据，下载或保存失败不改写上游成功。

## 一致性与故障边界

- 使用 SQLite 事务约束唯一 attempt、revision 和授权消费，两个进程并发相同批准最多一个取得提交资格；事务提交后服务崩溃也可能实际未发送，宁可 unknown。确认可安全重复不等于允许自动重试。
- 数据库、外部服务、图片文件不在同一个原子事务：产物先写私有临时文件、校验并持久化，再原子命名及登记；任一崩溃点应可审计地恢复或报告不完整，不把历史文件伪装成当前结果。
- 供应商结果需区分上游成功与本地下载/保存成功；只对已确认的模型/端点组合查询已持久化的原 task_id，状态映射见 [供应商适配契约](contracts/provider-adapters.md)。
- 输入可变或任务修订使审批失效；防路径穿越、链接逃逸与读取期间文件替换。实际授权发行/身份、数据库权限、数据保留和并发限制仍待验证或决定。
