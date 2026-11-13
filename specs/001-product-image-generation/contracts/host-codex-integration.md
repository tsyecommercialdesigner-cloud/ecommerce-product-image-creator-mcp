# T051 Codex Host 无收费接入验证

## 2026-11-12：真实 Codex 无收费观察

- 本机 Codex CLI：`codex-cli 0.163.0`。独立测试 Server 使用项目 `.venv/Scripts/python.exe -m ecommerce_product_image_creator.server`，隔离输入和 SQLite 状态位于 `.cache/t051-codex-20261112/`。`config.toml` 及 `start-t051.ps1` 只供隔离测试，未修改全局 Host 配置；设置测试网络防护，未提供真实供应商密钥。
- `codex mcp get` 使用进程级覆盖配置，成功识别启用的 `t051_offline` stdio Server。该命令只证明配置被识别，不证明 Server 已由真实 Host 启动或工具已被调用。
- 补充证据：独立 MCP SDK 客户端运行 `tests/contract/test_mcp_protocol.py::test_stdio_business_schemas_calls_and_read_only_boundaries`，1 passed；该测试覆盖独立 Server 的工具发现、Form Prepare、任务查询和无授权 Submit 拒绝。它不是 Codex Host 端到端证据。
- 已准备真人操作的独立 Codex 启动脚本；下述 Host 观察来自负责人操作时提供的截图及会话记录，不以 SDK 结果冒充。
- 真人提供的 Codex 终端截图显示 Host 已发现并准备调用 `t051_offline.prepare_product_image`，显示 Allow、Allow for this session、Always allow 和 Cancel 选项。该确认画面可见工具名与两个相对路径；截图中未见完整 task/revision、prompt、模型、费用或重试范围。截图发生在确认之前，不证明已选择 Allow、Server 已执行或操作已获业务授权；所示参数也未展示完整的 Prepare 必填字段。
- 负责人随后确认曾选择 **Allow**。其提供的真实 Codex 会话记录显示：独立 Server 启动并发现三个业务工具以及 `phase2a_probe`；初始不完整请求被拒绝，之后 Form Prepare 成功且确认状态为 `unverified`，Get 查询本地任务成功，无授权 Submit 返回 `Submission cannot be safely authorized or completed`，再次查询仍为 draft。通过完整参数第二次 Prepare 和查询也成功。对隔离 SQLite 的只读核对显示 2 个 Task、0 个 Attempt；上述调用不构成生产授权或真实供应商验证。
- 负责人随后对一次 `get_product_image_task` 调用选择 **Cancel**；Codex 显示 `user cancelled MCP tool call`，没有取得查询结果。这是只读调用，数据库未变化本身不能独立证明 Server 未收到请求。
- 本轮另启用隔离 stdio 观察器，只记录 MCP 方法、工具名及请求 ID，不记录参数。真人对 Get 查询选择 Cancel 后，观察器未收到 `tools/call`；同一会话随后对同一查询选择 Allow，观察器收到一条 `tools/call` 且查询成功。该正向对照支持本次 Cancel 在工具请求到达 Server 前发生；不推广到其他版本或取消时机。
- 单次 Allow 后再查询时，Host 再次展示确认。提示仅展示调用工具及当次参数；本次查询提示显示 task_id，并未展示完整费用、尝试预算和业务授权范围。该结果不证明其他界面或 Host 版本的显示能力。
- 2026-11-12 后续真实 Codex 会话中，将 Form 参考图换为隔离新图片后，修订请求返回 `Subset or input revision confirmation is required`；同时更换 Flare 模型再次返回相同错误，当前仍为修订 2，没有生成尝试。这证明缺乏输入修订确认时的拒绝；未创建新修订，不能据此声称冻结图片摘要已改变或旧执行身份失效。
- 随后通过真实 Codex 分别准备新的 Flare Form 任务：原图和替换图的冻结 SHA-256 与本地实际图片字节一致且不同，返回的执行摘要亦不同；两个任务的身份和模型／图片并非全部相同，不能仅凭跨任务摘要差异证明单一变量造成的变化。混用旧 Sunburst 执行摘要与新 Flare 任务身份的无授权提交被拒绝，通用错误未指明是身份不匹配还是缺少授权导致，故不能单独证明已授权路径的 Candidate 防误用。最终隔离数据库有 4 个 Task、0 个 Attempt。
- 对 Flare Prepare 的另一张真实确认画面显示了 instructions、model 和 prompt；该画面未显示完整参考图、费用或重试预算。不同调用的实际显示内容不同，不能推断所有参数均被展示。
- 保持两张测试图片、修改 Prompt 后创建 Form 修订 2；通过 Prepare 恢复读取修订 1、2，提示词和执行摘要均不同，授权均为 `unverified`。旧修订的无授权提交被拒绝。因没有已验证的真实 Host 授权来源，本次不能证明可信授权在内容变化后失效；Candidate、参考图变化及已批准授权失效仍未直接验证。
- 对提交参数加入普通 `approved=true` 时 Server 返回 `isError` 和安全授权失败；实际返回未说明额外字段是被忽略还是参与校验，不得推断具体处理方式。
- 负责人确认分别对查询工具及提交工具选择 **Allow for this session** 后，各自第二次调用没有再次提示。两次查询成功且仅涉及本地状态；两次使用相同未授权参数的提交均被 Server 拒绝。SQLite 最终仍为 2 个 Task、0 个 Attempt。会话级 Host 放行不能替代服务端授权，也不证明远端 exactly-once。
- 当前 Server 生产来源验证器及发送器默认拒绝；隔离测试不签发生产授权。独立逐次收费许可、真实 Host 来源、真实供应商及生产下载授权仍未验证。普通 `approved=true`、工具调用成功或 Host 放行本身不构成 Server 可验证的收费授权。

T051 目前为 `needs_more_evidence`，复选框保持未完成。真实 Host 已覆盖调用、单次 Allow、带正向对照的 Cancel、会话级放行、隔离参考图冻结以及未经授权重放拒绝；尚未在真实可信授权下证明内容或 Candidate 变化后的失效，也未证明 Server 可独立验证 Host 人工身份或决定。生产收费继续拒绝；Host 证据能力局限由 T054 汇总，不能凭空等待或伪造真人批准。
