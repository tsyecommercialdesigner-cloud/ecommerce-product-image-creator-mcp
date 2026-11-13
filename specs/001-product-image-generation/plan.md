# Implementation Plan: 多参考资料电商产品图生成

**Branch**: `dev` | **Date**: 2026-10-31 | **Spec**: [spec.md](spec.md)

**Input**: 已批准规格。下文保留最初设计时的状态描述；截至 2026-11-12，部分能力已完成无收费离线实现和验收，具体范围及生产缺口见 [离线开发基线](evidence/mvp-offline-baseline-20261112.md)。

**需求变更**：一次正常绘图生成一张 F/L/R/B/T/Q 六视图形态组合图，经人工确认后，再一次正常绘图生成一张包含同六视图的包装 Mock 组合图并由人工审核；两阶段均可按人工明确请求修订，历史版本保留。形态标识由模型生成，Host Agent 构造提示词；Q 为右上 45° 正交、平行线保持平行。

形态阶段仅输入一至六张形态参考图，不输入设计图或旧候选；Mock 阶段仅输入已确认形态组合原图一张及一至两张包装设计图，不输入原始形态图。每阶段每次正常生成仅返回一张组合图，不拆分或自动合成；安全重试、人工修订及 unknown 遵循 FR-012、FR-013。

## Summary

本项目须建设并运行**独立的 MCP Server**，通过本项目自己的供应商适配代码直接调用绘图 HTTP 接口，提示词由 Host Agent 构造，不调用独立 LLM；运行、安装、查询及恢复均不依赖已有的 GPT Image MCP、Nano Banana MCP 或其他 MCP Server。此前借用独立 MCP 进行的供应商测试仅为外部接口观察，不证明本项目实现通过验证。

建议同机本地文件输入的 stdio Python MCP Server：准备并校验任务、取得授权后提交生成、按任务和尝试查询结果。用户决定授权挂靠 Codex、Claude Code、DeepSeek Harness 等 Host 原生 Interrupt/Permission 机制；具体行为和 Server 是否能独立验证证据须按 Host 版本测试，详见 [架构审查决策](contracts/architecture-review.md)。单进程分模块，SQLite 持久化状态和尝试，隔离输入及产物根。用户指定默认绘图 `gpt-image-2.5-sunburst`，默认逆向组与异步 Edits；技术故障的有限重试与指定备选路由受授权及可安全重试条件约束，详见 [供应商适配契约](contracts/provider-adapters.md#技术故障恢复及备选路由)。获取不到 ID 且结果不明时保留未知，不自动切换或重提。

## Technical Context

- **语言及环境**：Python 3.12 候选；本机只读检查为 3.12.14、uv 0.12.9，不代表项目版本约束或跨平台验证。uv 与项目独立 `.venv`，依赖锁后再确定精确版本。
- **MCP**：官方 Python SDK v2.3.0 候选；具体 Python/SDK 约束、协议协商与 Host 兼容需实现后验证；本地 stdio。当前仓库没有依赖锁或 SDK 实测。
- **任务机制**：自定义业务 `task_id/attempt_id` 与 MCP Tasks Extension 独立，首版不声明实现标准扩展；SDK 客户端契约测试及各 Host 联调分别执行。
- **质量工具候选**：Ruff、Pyright、pytest、GitHub Actions；尚未安装或锁定。
- **存储**：本机 SQLite + 私有输出目录；目录归属、备份、保留期限及访问策略待确定。不得使用网络共享文件系统假定 SQLite 并发和锁安全。
- **平台及范围**：优先 Windows 本地 Host；Codex、Claude Code、DeepSeek Harness 的实际版本、权限机制和路径访问待分别联调。输入 1–2 张设计、1–6 张形态图；一张形态中间候选须经人工确认，最终交付一张六视图包装 Mock 组合图。
- **服务与提示词**：Host Agent 自主构造和修订提示词，Server 接收并记录实际文本、版本及摘要，与任务、参考快照和尝试绑定；不调用独立 LLM。绘图默认 `gpt-image-2.5-sunburst`、异步 Edits、逆向组，凭据 `ZHENZHEN_GPTIMAGEGEN_API_KEY` 遵循项目根 `.env` → 进程 → Windows 当前用户环境。
- **耗时/费用**：无业务硬上限；Server 记录绘图供应商返回的有效 `cost`（用户确认单位 ฿）及耗时，缺失为 unknown；Host 自身 token 消耗不由 Server 准确计量。网络超时与取消不等同上游失败。
- **最新执行约定**：绘图提交由 Server 实现持久化本地去重，供应商不支持幂等键；本地去重不保证远端恰好一次。普通安全重试默认等待 5 秒，429 遵循有效 `Retry-After`，缺失或无效则停止并报告；429、5xx、超时均不足以单独证明未执行。仅授权覆盖且有可靠安全证据才可重试。
- **技术重试与人工修订**：每个逻辑生成操作内，每个模型与接口组合 `max_attempts=3` 含首次；同一操作重启或更换 attempt ID 不重置。运营人员主动发起的形态和包装修订不设轮数上限，每轮是关联历史的新操作，技术重试独立计数；每轮仍核对授权，unknown 的恢复不变。
- **Host 与资源**：受控 Windows 本机单用户、Host 权限管理；Codex、Claude Code、DeepSeek Harness 均须实际验证。默认无固定业务图片大小上限，但内容、路径、异常解码、资源不足和供应商限制须以可测试的技术保护落实；具体机制与数值待设计验证，未完成不得宣称生产可用。

## Constitution Check

| 原则 | 设计前与设计后审查 | 后续验证 |
|---|---|---|
| I 追溯 | FR-001–023 对应契约、状态及测试映射 | 建立任务与验收证据链接 |
| II 职责 | 一个服务内分离协议、任务规则、适配器和持久化，不引入额外服务 | 单元与边界测试 |
| III Python | 类型、uv、锁文件、真实非零检查入口是实施门禁 | 锁定安装、lint、类型和测试 |
| IV MCP | 契约区分协议、业务错误、未知状态和副作用 | 用锁定 SDK 客户端检验 schema 与错误 |
| V 验证 | 明确替身、协议、Host、真实服务、人工验收分层 | 分别记录证据，禁止用替身冒充真实 |
| VI 安全 | 用户选定 Host 许可；实际业务授权核对和范围绑定尚待落实；持久化去重与超时未知需验证 | 三 Host 人工交互、授权核对、权限拒绝、并发及崩溃注入测试 |
| VII 决策 | 依据和未验证项见 research；精确依赖版本实施时锁定 | 复核 SDK/Host/上游版本及兼容 |

方案尚未通过完整宪法和工程安全验收：三 Host 的隔离替身已测试，但未证明 Server 可验证收费授权；DSH 默认配置下替身调用没有人工审批，测试 guard 并非默认行为。用户选择受控本机 Host 权限管理，未取消业务授权核对。具体核对方式、授权范围与人工交互需要落实；不能把无条件工具放行当成授权。工程指南资源预算条款此前已注释；本次恢复资源安全要求，需明确措施、治理差异和验证，不代表风险已处置。可拆分研究、实现和验证任务，但不得把待验证事项标成通过或将测试门禁取消扩展为发布批准。

## 实施就绪与执行边界

- 已确定独立 Server、默认模型和端点、两类输入及形态确认后生成一张六视图包装 Mock 组合图，分别人工审核；异步恢复和外部审核仍待实现及验证。
- 各阶段、修订、尝试、人工确认和组合产物分别关联；成本和失败按尝试记录，不覆盖历史产物。形态和 Mock 均由绘图模型生成一张六视图组合图，视觉效果由人工审核；阶段输入组合及供应商能力须逐模型/端点/分组验证。
- **阶段输入适配待验证**：每次形态生成或修订只输入一至六张形态参考图；每次 mock 只输入组合图加一至两张包装设计图。任务级资料仍包括两类。原先九图组合不再是首版 mock 要求；已有九图失败及历史八图观察仅为历史证据，不证明根因、真实上限或新阶段组合可用。[证据及分析](contracts/provider-adapters.md#九图失败分析与适配策略)区分客户端字段、服务端解析和模型实际使用。
- 遇到上述输入限制或不兼容时保留原任务、明确报告，不把 HTTP 200 当成功，不自动重试、换字段/模型/端点或静默减少图片。若建议减少允许使用的参考图，应展示拟使用/省略的集合、风险与可得费用，取得运营人员确认；mock 仍须包含已确认的形态组合原图及至少一张包装设计图。所选方案和适用授权确认后作为新操作执行。未确认时停止，不假定精简后一定兼容，也不改变任务的两类输入要求。
- Host Agent 构造实际绘图提示词；Server 在收费前通过现有 `prepare_product_image` 的任务快照准备模式保存文本、版本及摘要，形成涵盖参考内容、任务、提示词、模型、端点、分组与授权范围的执行摘要，供 Host 展示并交运营人员确认；提交时复核摘要和实际内容。此为候选契约，三个 Host 的展示、绑定与确认交接仍须实测；Host 自身消耗不由 Server 计量。
- 可继续任务拆分：SDK 锁定、Host 授权范围核对、绘图适配与崩溃恢复均为明确验证任务；未通过前不得宣称生产就绪或通过真实 Host 验收。
- Host 许可证据与范围核对是基础设计任务：先对接各 Host 的真实交互，明确执行环境如何落实 FR-012 和授权范围，再实现需要收费授权的完整流程；若未得到可实施方案，保留阻塞及未验证状态，不以替身测试或工具可见性通过安全验收。详见 [Host 许可测试记录](contracts/host-permission-probe-2026-11-02.md)和 [架构审查决策](contracts/architecture-review.md)。
- 形态和包装 Mock 组合图的人工审核由当前 Host 承载；展示、暂停、恢复和确认交接须逐 Host 实测，不预设各 Host 接口。

## Project Structure

### Documentation (this feature)

```text
specs/001-product-image-generation/
  spec.md  plan.md  research.md  data-model.md  quickstart.md
  contracts/mcp-tools.md  contracts/provider-adapters.md  contracts/architecture-review.md
  contracts/api-endpoints_gptimage-2.5-series.md
  contracts/api-endpoints_nano-banana-series.md  contracts/api-endpoints_gemini-llms.md
```

### Source Code (repository root)

以下为候选布局，尚不存在；名称和拆分在实施计划中复核。

```text
src/ecommerce_product_image_creator/
  server.py        # MCP 边界与结果映射
  domain.py        # 输入、决策与状态规则
  tasks.py         # 任务、授权消费和恢复
  provider.py      # 本项目直接调用供应商 HTTP 的模型/接口适配
  storage.py       # SQLite 和本地文件
  config.py        # 项目根配置与权限
tests/             # unit / contract / integration
```

**Structure Decision**: 单仓库单进程，无消息队列、3D 系统、Agent 集群或多租户服务；依赖树和实际模块名在实现前复核。

## Complexity Tracking

无建议的宪法豁免。SQLite 比分散文件状态复杂，但事务可约束并发授权消费及崩溃恢复；它不能与远端收费请求构成原子事务，未知状态仍不可避免。技术与业务未决项见 research 的决策门禁。
