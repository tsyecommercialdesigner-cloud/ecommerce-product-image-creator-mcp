# Implementation Plan: 多参考资料电商产品图生成

**Branch**: `dev` | **Date**: 2026-10-31 | **Spec**: [spec.md](spec.md)

**Input**: 已批准规格；本文件是待人工审查的设计，尚未实现或安装依赖。

## Summary

建议同机本地文件输入的 stdio Python MCP Server：准备并校验任务、取得可验证授权后提交一次生成、按任务和尝试查询结果。优先研究独立于 Agent 可伪造输入的本地可信用户确认组件，具体信任前提尚未验证，失败时拒绝收费；比较和确认流程见 [架构审查决策](contracts/architecture-review.md)。单进程分模块，SQLite 持久化状态和尝试，隔离输入及产物根；不以进程内任务或 Host 会话作为恢复证据。默认使用 `gpt-image-2.5-flare-2k` 或 `gpt-image-2.5-sunburst-2k`，逆向分组和 `POST https://ai.t8star.org/v1/images/edits?async=true`；其他端点仅在用户显式指定后使用，不自动回退。首版优先验证一个默认组合，不将全部候选作为必要交付。按模型、端点、分组维护能力映射，详见 [供应商接口适配](contracts/provider-adapters.md)。获取不到 ID 时仍须保留结果不明。设计见 [research.md](research.md)、[data-model.md](data-model.md)、[MCP 契约](contracts/mcp-tools.md)。

## Technical Context

- **语言及环境**：Python 3.12 候选；本机只读检查为 3.12.14、uv 0.12.9，不代表项目版本约束或跨平台验证。uv 与项目独立 `.venv`，依赖锁后再确定精确版本。
- **MCP**：官方 Python SDK v2.3.0 候选；具体 Python/SDK 约束、协议协商与 Host 兼容需实现后验证；本地 stdio。当前仓库没有依赖锁或 SDK 实测。
- **任务机制**：自定义业务 `task_id/attempt_id` 与 MCP Tasks Extension 独立，首版不声明实现标准扩展；SDK 客户端契约测试及各 Host 联调分别执行。
- **质量工具候选**：Ruff、Pyright、pytest、GitHub Actions；尚未安装或锁定。
- **存储**：本机 SQLite + 私有输出目录；目录归属、备份、保留期限及访问策略待确定。不得使用网络共享文件系统假定 SQLite 并发和锁安全。
- **平台及范围**：优先 Windows 本地 Host；Codex、Claude Code 的实际版本和 Host 路径访问待联调。每次 1–2 张设计、1–6 张形态图，仅一张候选图。
- **服务**：用户指定默认两个 `-2k` 模型、异步 Edits 和逆向分组；备选端点由用户显式指定，`nano-banana-pro` 按支持多参考图编辑处理。文档历史样例不等于本项目实测；输入和输出限制、账号权限需逐组合确认。单图 10 MiB 仍只是讨论候选。
- **耗时/费用**：没有业务硬上限；按返回的有效 `cost` 记录供应商报告费用，缺失时 unknown，币种/单位及计费语义需确认；网络超时与取消不等同上游失败。

## Constitution Check

| 原则 | 设计前与设计后审查 | 后续验证 |
|---|---|---|
| I 追溯 | FR-001–019 对应契约、状态及测试映射；不改规格 | 建立任务与验收证据链接 |
| II 职责 | 一个服务内分离协议、任务规则、适配器和持久化，不引入额外服务 | 单元与边界测试 |
| III Python | 类型、uv、锁文件、真实非零检查入口是实施门禁 | 锁定安装、lint、类型和测试 |
| IV MCP | 契约区分协议、业务错误、未知状态和副作用 | 用锁定 SDK 客户端检验 schema 与错误 |
| V 验证 | 明确替身、协议、Host、真实服务、人工验收分层 | 分别记录证据，禁止用替身冒充真实 |
| VI 安全 | 独立受信授权、范围校验、持久化幂等防护和超时未知 | 真实权限拒绝、并发及崩溃注入测试 |
| VII 决策 | 依据和未验证项见 research；精确依赖版本实施时锁定 | 复核 SDK/Host/上游版本及兼容 |

设计审查无已知宪法豁免；通过指方案层面的意图，非实现、CI 或 Host 验收。必须先落实可信授权机制、供应商/限制决策及实际接口验证；否则不能进入收费实现或发布。

## Project Structure

### Documentation (this feature)

```text
specs/001-product-image-generation/
  spec.md  plan.md  research.md  data-model.md  quickstart.md
  contracts/mcp-tools.md  contracts/provider-adapters.md  contracts/architecture-review.md
  contracts/api-endpoints_gptimage-2.5-series.md
  contracts/api-endpoints_nano-banana-series.md
```

### Source Code (repository root)

以下为候选布局，尚不存在；名称和拆分在实施计划中复核。

```text
src/ecommerce_product_image_creator/
  server.py        # MCP 边界与结果映射
  domain.py        # 输入、决策与状态规则
  tasks.py         # 任务、授权消费和恢复
  provider.py      # 同一供应商网关的按模型/接口分支适配
  storage.py       # SQLite 和本地文件
  config.py        # 项目根配置与权限
tests/             # unit / contract / integration
```

**Structure Decision**: 单仓库单进程，无消息队列、3D 系统、Agent 集群或多租户服务；依赖树和实际模块名在实现前复核。

## Complexity Tracking

无建议的宪法豁免。SQLite 比分散文件状态复杂，但事务可约束并发授权消费及崩溃恢复；它不能与远端收费请求构成原子事务，未知状态仍不可避免。技术与业务未决项见 research 的决策门禁。
