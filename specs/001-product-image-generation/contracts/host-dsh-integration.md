# T053 DeepSeek Harness Desktop Host 验证

## 2026-11-12 Desktop 实测

- 负责人指定 Desktop 的 `desktop` Profile。Windows Desktop 应用实际运行；用户配置位于 `C:/Users/Administrator/.dsh/profiles/desktop/cordis.patch.yml`，原始文件已备份为同目录 `cordis.patch.yml.mvp-host-02-20261112.bak`，并在原内容之后追加隔离 MCP Server `t053_offline_mvp_host_02`，未修改其他 Profile。独立输入和状态在项目 `.cache/mvp-host-02-20261112/dsh/`。
- 观察到 Desktop 进程启动了隔离 stdio 观察器；仅此不能证明业务工具已成功发现或调用。Desktop 界面显示当前模型为 `claude-haiku-5-5`，负责人说明由 codeproxy 提供。
- 经负责人批准发送一条模型测试消息以检查工具发现；界面随后显示“已停止”，没有工具清单或业务调用结果。操作期间可能误触停止，实际模型费用未知；未自动重试。当时尚无业务调用结果；后续结果见下。
- Host 一次消息可能自动产生多次模型请求；没有逐个请求前可独立核验批准的证据。生产收费授权及真实供应商发送保持拒绝。

- Desktop 安装包声明版本 `0.2.1-rc.5`，使用 desktop Profile 和 codeproxy 的 `claude-haiku-5-5`。用户控制实际会话；工具清单包含三个业务工具及 `phase2a_probe`。Form Prepare 成功（revision 1，authorization unverified）；Get 返回本地 draft；无授权 Submit 被拒绝。隔离 SQLite 当前 task 1、attempt 0。
- 在“完全权限”下三项调用没有人工确认框。用户切换到“工作区内修改”后执行只读 Get，仍未出现确认框且查询成功；这只证明该次调用行为，不代表所有操作都自动放行。真人 Allow／Deny 未观察到。
- 后续变更测试出现重复准备调用，用户要求停止，界面显示已停止；结果不足以证明变更或重放安全。观察器累计 Prepare 9、Get 5、Submit 1；不等于成功次数。实际模型费用未知，未调用真实绘图供应商。Host 确认未被证明形成 Server 可独立验证的业务授权。T053：needs_more_evidence。

## 历史 CLI 观察（不计入 Desktop 验收）

- 2026-11-12，`dsh --version` 输出 `0.2.1-rc.5`，`dsh --profile web --help` 可执行。本机存在 web、headless 和 desktop 等配置；`tui` 配置不存在。
- 仅在 `.cache/t053-dsh-20261112/` 准备隔离图片、SQLite 目录及一次性 `--patch` stdio MCP 配置，未更改用户全局配置。配置 schema 探查识别到测试插件项，同时报告既有配置树载体诊断；这不是 Server 成功启动或工具调用证据。
- 负责人确认当前可用的 DSH 模型收费。本轮明确禁止收费操作，因此未发起模型会话，也未实测三个业务工具、Allow／Deny、自动放行、变更及重放。生产 Server 来源与发送默认拒绝。
- 后续需可确认无收费的 Host 模型或另行明确批准的模型使用范围，并在隔离配置中实际运行；人工确认仍不能自动证明 Server 可核验的业务授权。

T053 当前结论见上述 Desktop 实测；历史 CLI 不构成 Desktop 验收。
