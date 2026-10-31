# 技术调研与决策记录

调研日期：2026-10-31（用户时区）。本轮复核用户提供的供应商接口资料及此前官方研究，未安装依赖、未调用付费 API、未做 Host 联调。下列候选均待项目维护者审查；供应商资料中的历史响应属于文档记录，不等于本项目实测。

## R1 MCP SDK、协议和 Host

- **问题**：选择 Python SDK、发现工具、结构化结果和图片返回。
- **候选**：官方 MCP Python SDK v2.3.0；stdio；`MCPServer` 高层工具接口。备选 v1 或低层 Server；v1 与当前工程默认 v2 不一致，除非兼容性证据要求迁移不作优先选项。SDK v2 和协议版本不是同一概念。
- **证据**：[Python SDK](https://py.sdk.modelcontextprotocol.io/) 显示 v2 稳定线及文档要求 Python >=3.11；但 [v2.3.0 标签项目元数据](https://raw.githubusercontent.com/modelcontextprotocol/python-sdk/v2.3.0/pyproject.toml) 写 `>=3.10`。二者口径冲突，本项目不推断版本下限，Python 3.12 仅为候选；实际依赖尚未安装。[v2.3.0 标签导出](https://raw.githubusercontent.com/modelcontextprotocol/python-sdk/v2.3.0/src/mcp/server/__init__.py) 有 `MCPServer` 和低层 `Server`。SDK 文档列 stdio、Streamable HTTP 与 SSE，项目只设计 stdio。
- **依据**：[工具定义](https://py.sdk.modelcontextprotocol.io/servers/tools/) 支持类型注解和输入 schema；[结构化输出](https://py.sdk.modelcontextprotocol.io/servers/structured-output/) 支持输出 schema；[工具规范](https://modelcontextprotocol.io/specification/2026-07-28/server/tools) 定义 structuredContent 和工具错误；[SDK 错误](https://py.sdk.modelcontextprotocol.io/servers/handling-errors/) 区分工具错误和协议错误。协议版本及握手参见 [版本](https://py.sdk.modelcontextprotocol.io/protocol-versions/)，必须按实际锁定版本和 Host 验证，不预设全部 Host 支持新协议。
- **图片风险**：[媒体](https://py.sdk.modelcontextprotocol.io/servers/media/) 的高层图片结果是 ImageContent；不能假定它自动同时提供结构化输出。产物以本地受限文件为权威副本，MCP 图片块为可选预览；具体结果组合按锁定 SDK/Host 测试。
- **Host**：[Codex MCP](https://developers.openai.com/codex/mcp/) 与 [Claude Code MCP](https://code.claude.com/docs/en/mcp) 有 stdio 接入说明，不能据此声称本项目通过。Claude Code 可能缩放或持久化图片且 server 可能随 Host 退出；本地后台任务不保证存活。不同版本、权限和工具标识均未验证。
- **未验证**：SDK 实际安装、`isError` 与 structuredContent 在两个具体 Host 的表现；根目录 `.env` / 权限与本服务的真实联调。

## R2 图片输入和大小

- **问题**：8 张分组输入、合法性、权限与本地/Host 限制。
- **候选**：由受控文件根内的绝对路径引用 JPEG/PNG，服务器打开校验并固化内容摘要；经授权的产物根与输入根分离。可选 MCP 图片内容块作预览，不假定每个 Host 都能把图片作为工具参数传入。备选 base64 内嵌：易膨胀约三分之一、占用上下文；远程 URL：增加重定向和 SSRF 边界，首版不默认支持。
- **大小候选**：单张 **10 MiB** 仅供讨论，八张原始最大约 80 MiB，编码、协议、内存与服务总请求限制可能更严格；不是已批准上限，也不是供应商保证。逐张实际读取大小和格式，先拒绝并指出违规图片，再进行收费请求；同时须制定总大小、像素/解码内存限制。文件句柄、重解析点与并发替换需防 TOCTOU；拒绝网络路径或重解析点越界，禁止任意 URL。不能依赖客户端声明大小或扩展名。
- **来源边界**：此前 [OpenAI Images edit](https://developers.openai.com/api/reference/resources/images/methods/edit) 中的数量和大小说明属于 OpenAI 接口，不能作为 `ai.t8star.org` 对应模型、分组和接口的限制。本次供应商文件未给出可靠的单张、总量和最大图片数上限；10 MiB 仍只是待评估候选，不代表供应商支持。具体上限需供应商确认、实际服务支持、运营体验评估与业务批准后写契约和验收；不接受自动压缩或忽略图片替代确认。
- **未验证**：8 张最大候选输入的端到端总量、尺寸上限、图片数据本地可访问及 Host 的路径/审批行为。

## R3 图像服务与成本

**2026-10-31 适配复核：本节后文直接 OpenAI/Gemini 接口的研究仅为历史比较，不代表本项目实际供应商契约、模型身份、参数、限制、定价或恢复能力。实际实现以 [供应商适配矩阵](contracts/provider-adapters.md) 及两份用户提供的接口文档为依据。**

- **问题**：文字/形态质量、多图、可查询性及费用。
- **候选 A**：[OpenAI Images edit](https://developers.openai.com/api/reference/resources/images/methods/edit) 文档给出多图、生成参数及编辑能力；[图像指南](https://developers.openai.com/api/docs/guides/image-generation) 列模型及能力说明。文本和形态还原只是供应商描述，本项目未比较；Images 同步调用不能假定有可查询远程任务 ID 或幂等保证。文档中 usage 的模型适用表述需按实际响应确认，usage 可缺失。
- **候选 B**：[Gemini 图像生成](https://ai.google.dev/gemini-api/docs/image-generation) 描述多图能力，模型的输入数量与高保真数量不同；同页高保真说明口径存在差异，不能把 8 张都当作高保真已确认。模型是否严格只输出一图、输出可用性、模型在目标账户的可用性及对应计费均未实测。若返回多图，不能静默丢弃作为已满足单图要求，必须先确认接口/模型满足需求或反馈不兼容。
- **备选异步**：[OpenAI background](https://developers.openai.com/api/docs/guides/background) 可按已获得的 response ID 查询，但没收到 ID 的超时不能保证恢复；服务保留期限有限。Google [Batch](https://ai.google.dev/gemini-api/docs/batch-api) 创建可能非幂等且批处理时延不适合作为默认交互恢复机制。远端异步不等同 MCP 后台执行。
- **计费和时间**：[OpenAI pricing](https://developers.openai.com/api/docs/pricing)、[Gemini pricing](https://ai.google.dev/gemini-api/docs/pricing) 随模型/模式变化；请求前展示实际可得价格与时间资料、来源及时间；缺失用 `unknown`，不把估算冒充账单或承诺耗时。分别记录任务墙钟、调用耗时、上游实际 usage/request ID；实际费用无权威数据时 unknown。
- **待确认**：默认两个 `-2k` 模型中的任务选择、费用单位及数据处理条件、账号接口访问、各组合的服务侧大小和输出张数；不得在无业务授权时进行收费验证。不得静默回退或自动提交来解决不兼容。

## R4 状态、授权与存储

- **问题**：收费请求在崩溃/并发/超时下不重复，保留可恢复证据。
- **候选**：SQLite 事务保存 request digest、授权引用、attempt、提交意图，提交网络请求前原子消费授权；提交窗口先标记结果不明，以免崩溃误认为未发送。远端 ID 确认后持久化；无 ID 或查询不可用保留 unknown。进程退出后不自动重新发起调用。实际远端接收无法与本地事务原子提交；不承诺 exactly-once 或零重复费用。
- **备选**：JSON/文件可原子替换但并发锁、恢复和一票一用更难；SQLite [atomic commit](https://www.sqlite.org/atomiccommit.html) 提供本地事务，不包括外部网络副作用。SQLite 配置、文件/目录权限、跨进程争用及系统崩溃仍需真实平台测试。
- **授权关键缺口**：MCP 参数 `approved=true`、工具注解、对话文本或模型自述都不能证明真实业务授权。三种方案及本地推荐详见 [架构审查决策](contracts/architecture-review.md)：外部系统、本地可信确认组件、可验证的 Host 机制。暂推荐同机本地可信确认组件，但隔离同账号 Agent、验证实际用户及防伪能力均未证实；未取得证据和批准之前仍须 fail-closed。组件或代理均不得自行签发已授权的确认记录。
- **其他未知**：产物私有存储位置、ACL、保留期、备份与人工清理政策；特定模型/端点组合是否支持异步、是否含 idempotency key、是否可获取 usage/费用。GPT 资料已有按 ID 查询能力及历史样例，具体组合见供应商适配矩阵；仍不能推定所有 Nano 接口支持同样查询。

## R6 本次供应商适配与证据

**资料与时间**：[GPT 接口](contracts/api-endpoints_gptimage-2.5-series.md) 和 [Nano 接口](contracts/api-endpoints_nano-banana-series.md) 为用户本次提供，文件中的历史响应日期为 2026-10-20；本次仅文档复核，未独立核验原始证据或作供应商调用。Nano 文件的 `validation/...` 是相对该文件的历史证据引用；该目录当前不在本功能目录，不能假装本次已核验原始记录。

| 问题 | 资料支持或观察 | 风险和决策 |
|---|---|---|
| GPT 多图 | GPT 资料的 Edits 声明支持多图，历史 Sunburst 示例用 `image[]` 单图成功；其他示例/属性使用 `image`，键名不一致。 | 重复 multipart 字段名与 2–8 张上限、分组和异步组合待供应商明确与受控测试；不据上游兼容格式推定同义。 |
| GPT 异步 | 文档 Generations 的 `?async=true` 和 `GET /v1/images/tasks/{task_id}`；历史 Flare 提交返回顶层 task_id，查询观察到 `NOT_START` 与 `SUCCESS`。 | 用户本轮确认无后缀、`-2k`、`-4k` 两个系列在 Generations/Edits 的异步查询覆盖，默认使用异步 Edits；这些组合尚无本项目实测。schema 另写 `code/message/data`；仅识别确认的变体；未知状态 fail-closed。 |
| Nano 支持矩阵 | `nano-banana-pro`: Generations/Edits；`gemini-nano-banana-2.1`: Gemini/Chat/Generations，不支持 Edits；`gemini-3.1-flash-lite-image`: Gemini/Chat，不支持 Generations/Edits。 | 本轮用户确认 pro 支持多参考图编辑；重复 `image` 为规范示例，历史单图返回 451，尚无本项目多图成功样例。 |
| 获取结果 | GPT 异步样例的 `data.status`、`data.data.data` 和 URL；Nano Generations/Edits 样例 URL；Gemini `parts[].inlineData`；Chat 可能是 Markdown URL。 | 实施按已选模型/端点显式解析；空 `b64_json` 不当图片。结果数非一时不静默丢图；下载 URL 经受控网络策略、域名/重定向/私网防护及大小/MIME 校验。 |
| 费用与状态 | 用户要求实际价格按响应的 `cost` 记录，缺失可为未知；GPT 样例 `cost` 无币种单位，Nano 空图不扣费未核对账单。 | 有效 `cost` 作为供应商报告费用保存；缺失或无效为 unknown，币种/单位不明不推断，不同查询同一费用不重复累加；不声称已核对账单。 |

用户已确认默认 `gpt-image-2.5-flare-2k` / `gpt-image-2.5-sunburst-2k`、逆向分组及 `POST /v1/images/edits?async=true`，其他端点仅在用户显式指定时使用。仍需按每个模型/端点/分组确认最多八张 JPEG/PNG 的字段、编码、数量、单张与总大小、输出张数及 MIME；10 MiB 仅为讨论候选。不能凭官方直连文档或一次 HTTP 200 扩大支持矩阵。

## R5 测试与交付决策

优先真实 SDK 本地协议 + 无权限替身；空密钥 `.env.example`，逐字段项目根 `.env` → 进程 → Windows 用户环境，仅缺失回退，空值、无效、重复、读/解析错误 fail-closed。测试后脱敏归档，仅清理本次资源。需分别取得 Host 及收费真实测试授权；CI 不得收费。评估 Ruff/Pyright/pytest 与 uv 锁，不在本次安装。PR、合并后 CI、受控 CD 是未来门禁，不视为现有能力。

## R7 SDK 版本边界与业务任务

- **SDK 接口证据**：v2.3.0 标签导出 `MCPServer`；[工具](https://py.sdk.modelcontextprotocol.io/servers/tools/)、[结构化结果](https://py.sdk.modelcontextprotocol.io/servers/structured-output/)与[错误处理](https://py.sdk.modelcontextprotocol.io/servers/handling-errors/)为当前 v2 文档，非本项目锁定安装或运行证据；标签源码、实际安装和 Host 兼容还须独立测试。
- **业务任务不是 MCP Tasks**：本项目普通工具返回自己的 `task_id/attempt_id` 并查询 SQLite。标准 [MCP Tasks Extension](https://modelcontextprotocol.io/extensions/tasks/overview) 定义能力协商及 `tasks/get` 等协议机制，版本可能改变；本方案未实现或声明该扩展，不把业务 ID、后台线程或上游 ID 冒充标准 Tasks。扩展和 Host 支持均未验证，后续如启用须独立设计及测试。
