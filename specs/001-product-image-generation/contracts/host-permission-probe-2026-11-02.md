# Host 许可隔离测试记录（2026-11-02）

## 范围

Windows，本机 stdio MCP 替身；无供应商请求、无图片、无真实 API 密钥。替身提供 `probe_permission(marker)`，声明非只读和可能有副作用，实际只返回 `PROBE_OK`。替身只记录方法名、参数键名和元数据结构，不记录元数据值、会话标识或用户数据。测试使用独立临时 MCP 配置；未修改任何 Host 的持久配置。Host 模型推理本身可能消耗其既有账户用量，未请求绘图服务。

允许案例是命令行预先配置的工具许可，不是人工在弹窗中点击允许；拒绝案例是无交互审批通道时的阻止，不是人工点击拒绝。各结果仅适用于下列版本和设置。

| Host 与版本 | 测试设置及结果 | Server 可见内容 |
|---|---|---|
| Codex CLI 0.163.0 | `codex exec --ignore-user-config --ephemeral -s read-only`，隔离 MCP 配置 `default_tools_approval_mode='approve'`；工具调用完成并返回 `PROBE_OK`。 | `tools/call` 含 `name`、`arguments`、`_meta`；后者含调用、会话、进度、窗口及 turn 元数据。记录的结构未显示可验证的人工身份、任务摘要、收费授权或防重放签名；不能将标识符当作授权证据。 |
| Codex CLI 0.163.0 | 同上但不预授权；非交互默认审批策略为 `never`，Host 报告 `MCP tool call requires approval, but approval policy is never`，工具调用失败。 | 替身收到 `initialize`，未收到 `tools/call`；这证明该设置下 Host 阻止了此次调用。 |
| Claude Code 2.1.303 | `claude -p`、`--strict-mcp-config`、`--permission-mode manual`、`--allowedTools mcp__permission_probe__probe_permission`；调用完成并返回 `PROBE_OK`。 | `tools/call` 的 `_meta` 包含 `claudecode/toolUseId` 与 `progressToken`；本次未观察到可验证的人工身份、任务摘要、收费授权或防重放凭证。 |
| Claude Code 2.1.303 | 同一隔离工具配置，`--permission-mode manual --permission-prompts none`，无预授权；结果包含 `permission_denials`，报告因无审批界面而拒绝。 | 替身收到 `initialize`，未收到 `tools/call`；这证明该设置下 Host 阻止了此次调用。 |
| DeepSeek Harness 0.2.1-rc.5 | `dsh --profile headless --patch <临时 MCP 配置> --json`；替身完成初始化，但模型轮次返回 `MISSING_CREDENTIAL`（`deepseek-official` 路由无 API key）。 | 未收到 `tools/call`；由于模型未能发起调用，不能据此判断允许、拒绝或授权证据语义。 |
| DeepSeek Harness 0.2.1-rc.5，CodeProxy 后续测试 | 按用户指定在临时 patch 中设置 `codeproxy-anthropic/claude-sonnet-5-5`；Server 收到初始化和 `tools/list`。模型轮次返回 `MISSING_CREDENTIAL`：当前 headless 环境无法解析 `CODEPROXY_ANTHROPIC_API_KEY`。 | 未收到 `tools/call`；CodeProxy 路由名和模型配置成功不等于凭据可用，允许、拒绝和授权证据仍未验证。未打印或记录密钥。 |
| DeepSeek Harness 0.2.1-rc.5，CodeProxy 有凭据测试 | 用户指定从 `.dsh/.credentials.yaml` 的凭据引用注入隔离进程；`headless` 默认许可配置下调用成功，返回 `PROBE_OK`；该次未出现人工审批交互。 | 替身收到 `tools/call`，参数仅有 `name` 和 `arguments`，没有 `_meta`；未收到可验证授权证据。此结论限于本次 headless 配置和工具。 |
| DeepSeek Harness 0.2.1-rc.5，CodeProxy 拒绝测试 | 相同临时 profile 设置，额外加载仅针对测试工具的临时插件，使用 Host `ctx.tools.guard` 拒绝，模型收到 `Isolated test policy: denied`。 | 替身收到 `initialize`，未收到 `tools/call`。该结果验证运行时自定义 guard 能阻止调用，不证明默认 Host 会拦截 MCP 调用，也不证明人工点击拒绝。 |

同一 Claude 额外 `--disallowedTools` 场景未暴露该工具且 Server 未收到调用；它证明工具被隐藏，不能代替上表的拒绝证据。Codex 的 metadata 结构包括 `x-codex-turn-metadata`，此次只检查字段名称及类型，不检查或归档值；未证明不存在其他版本或交互场景能够传递凭证。

## 未覆盖及后续

- Codex、Claude 的人工弹窗允许/拒绝、授权范围与一次性消费未验证；预授权不能证明人工确认过程。
- DeepSeek Harness 的人工弹窗许可、真实业务授权和 Server 可验证授权凭证仍未验证；不能将测试用自定义 guard 等同于原生人工确认流程。凭据仅在测试进程内注入，不写入 patch、输出或共享文档。
- 此替身与项目尚未实现的独立 Server 不同；SDK 版本、真实 Host 与项目工具联调、业务授权和收费行为仍待验证。
- 官方 [OpenAI MCP Server 指南](https://developers.openai.com/plugins/build/mcp-server)说明工具注解不替代服务端授权；本文的测试现象不扩大为所有版本或 Host 的结论。
