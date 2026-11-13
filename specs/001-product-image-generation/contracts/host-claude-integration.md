# T052 Claude Desktop Host 验证

## 2026-11-12 Desktop 实测

- 负责人将 T052 对象从 Claude Code CLI 改为 Claude Desktop。Windows 安装包版本 `4.23118.1.0`；实际应用启动。其本地 MCP 配置在 `C:/Users/Administrator/AppData/Local/Claude-3p/claude_desktop_config.json`，原文件已原样备份为同目录 `claude_desktop_config.json.mvp-host-02-20261112.bak`。仅增加隔离 Server `t052-offline-mvp-host-02`；输入及状态在项目 `.cache/mvp-host-02-20261112/claude/`，发送器保持默认拒绝。
- 重启后 Desktop 开发者页面显示测试 Server Running。经负责人针对本次测试授权，使用 codeproxy 的 `claude-haiku-5-5` 发送一条工具发现消息，实际模型费用未知；负责人在工具确认框选择 Allow once。确认画面显示 `phase2a_probe`、`fail: false` 及 Deny／Allow 选项。
- 真实 Host 成功调用 `phase2a_probe`，返回 `phase2a-ok`；隔离 stdio 观察器记录一条对应 `tools/call`。Host 列出 `phase2a_probe` 和三个业务工具。发现与探针成功不代表业务工具通过。
- 一条消息可能自动产生多次模型请求，无法保证逐个请求前由 Server 独立核验授权。

- 真实 Desktop 业务交互：用户控制会话，Form Prepare 成功（revision 1，authorization unverified），Get 返回本地 draft；无业务授权 Submit 返回拒绝，之后 Get 仍为 draft、无尝试。隔离观察器记录 Prepare 1、Get 2、Submit 1；SQLite 现有 task 1、attempt 0。输入为隔离的 8×8 测试 PNG，artwork 已登记但未选入 form 阶段。
- 人工 Allow once 在 probe 上观察到。用户随后对 Get 实际点击 Deny，Host 返回请求拒绝；观察器没有新增对应 tools/call，有此前成功 Get 作为正对照。并未验证会话级放行。界面曾显示工具名和探针参数，不能推断其总是展示完整任务、费用或重试范围。
- Prompt／参考图／Candidate 变化、重复调用及 approved=true 的 Desktop 直接测试尚未完成；Host 确认没有被证明形成 Server 可独立验证的人类业务授权。模型使用 codeproxy 的 claude-haiku-5-5，实际模型费用未知；未调用真实绘图供应商。T052：needs_more_evidence，保留生产拒绝。

## 历史 Claude Code CLI 观察（不计入 T052 Desktop 验收）

- 2026-11-12 本机 npm 包声明 `@anthropic-ai/claude-code` 版本为 `2.1.319`；实际 `claude --version` 无法运行。安装目录中的 `bin/claude.exe` 为 500 字节、不是 Windows PE 可执行文件的占位文本，因此版本仅为包元数据，未通过实际 Host 运行确认。
- 尚未启动独立 Server 或接入 MCP；三个业务工具、Prepare、Get、无授权 Submit、Allow／Deny、自动放行、参数展示及身份变化／重放均未通过真实 Claude Host 测试。不能将其他 Host 或 MCP SDK 的结果冒充 Claude 证据。
- 生产 Server 来源与发送默认拒绝。修复该安装需负责人通过其既有安装流程恢复可执行 Claude Code；本轮未安装、升级或修改用户配置。恢复后仍须独立无收费 Host 验证。

T052 当前结论见上述 Desktop 实测；历史 CLI 不构成 Desktop 验收。
