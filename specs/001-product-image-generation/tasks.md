# Tasks: 多参考资料电商产品图生成 MCP

**输入**：`spec.md`、`plan.md`、`research.md`、`data-model.md`、`contracts/`、`quickstart.md` 和项目宪法 2.0.0。既有任务均未完成；本次重新按执行顺序连续编号，未将历史供应商探测或 Host 替身测试标成项目实现通过。

**执行边界**：这是未来实施任务清单；编写本清单不安装依赖、不修改其他文件、不调用收费服务。所有真实绘图请求取得对具体测试、数据、费用、模型、端点、尝试次数的明确授权；CI 和离线测试禁止真实收费调用。人工确认不得由 Agent 冒充。历史样例不等同本项目独立 Server 验证。

**阶段约束**：形态阶段仅输入 1–6 张形态参考，一次正常绘图生成一张 F/L/R/B/T/Q 组合图并经人工确认；Mock 阶段仅输入已确认组合原图和 1–2 张设计图，一次正常绘图生成一张六视图包装 Mock 组合图并经人工审核。两阶段均可人工请求修订并保留历史版本。

## 待决差异与停止条件

- **D1 重试和切换（已决定）**：用户确认遵循 FR-013，允许在确认安全且授权覆盖的情况下有限自动重试与指定切换；未知副作用仍不得盲目重提。T007 记录决定与验证条件，实际错误分类和授权覆盖仍须测试，未验证的组合不得自动启用。
- **D2 授权证据**：采用受控 Windows 本机单用户、Host 管理权限。Host 负责实际业务和费用范围核对，Server 校验快照并记录可见证据；不能以 `approved=true`、模型自述或 API Key 代替授权。三 Host 的允许/拒绝、自动批准风险及 Server 可见性须实际验证；无法落实授权核对时停止收费，不声称宪法 VI 已通过。
- **D3 资源策略**：不设固定业务文件大小上限；须制定异常解码、路径、资源不足与供应商限制保护并测试。工程指南预算条款先前被注释，治理差异须审查；未处置不得声称生产可用。
- **D4 图像修订**：现 FR-020 仅允许原始形态参考图片及文字修订，不能将前一候选图发送上游；是否足以完成精细修订，由运营人员依据实际结果评估，不能由模型自主决定，不擅自增加图片输入。

## Phase 1：Setup（共享基础）

- [X] T001 在 `pyproject.toml`、`.python-version` 和 `uv.lock` 中依据 `plan.md` 核实并锁定 Python、MCP SDK 与运行依赖；记录版本与协议选择，不引入分布式组件；验收为锁定安装可复现。
- [X] T002 在 `pyproject.toml` 中选定并配置 Ruff、Pyright、pytest 或有依据的等价工具，分别提供格式、静态、类型及测试命令，验证成功返回零、失败返回非零。
- [X] T003 在 `src/ecommerce_product_image_creator/__init__.py`、`tests/unit/`、`tests/contract/`、`tests/integration/` 建立包及测试结构，运行无收费基础测试确认入口可用。
- [X] T004 [P] 在 `.env.example` 和 `.gitignore` 配置空的 `ZHENZHEN_GPTIMAGEGEN_API_KEY`，保护 `.env`、SQLite、产物和缓存；用顶层及子目录正反例验证测试、锁文件与共享文档未被误忽略。
- [X] T005 在 `tests/unit/test_config.py` 编写配置读取与脱敏测试：项目根 `.env` → 进程 → Windows 当前用户环境，只有缺失回退；空值、无效、重复、解析或读取失败报错，不输出密钥。
- [X] T006 在 `src/ecommerce_product_image_creator/config.py` 实现 T005 的配置规则并通过测试，启动和工具调用时不得安装或升级依赖。

## Phase 2：Foundational（所有故事的基础）

- [X] T007 在 `specs/001-product-image-generation/quickstart.md` 记录用户对 D1 的决定：仅确认安全且授权覆盖时按 FR-013 有限重试和指定切换，结果不明不得重提；列明需验证的错误分类、授权范围与模型/端点组合。
- [X] T008 在 `specs/001-product-image-generation/contracts/architecture-review.md` 落实受控 Windows 单用户/Host 信任边界、可验证保证、受控运行前提和剩余风险；制定三 Host 真人允许/拒绝、自动放行、任务/提示词/参考图/费用范围展示、失效、Server 可见证据及绕过/重放测试方案，未完成授权核对时停止收费。
- [X] T009 在 `specs/001-product-image-generation/research.md` 调查内容及格式、受控路径、异常解码、资源不足、并发、磁盘、远程下载和供应商限制风险，确定技术保护策略、离线验收方法及治理差异；不设固定业务文件大小上限，不将真实供应商测试作为 T015/T049 的循环前置，未处置不得声称生产可用。
- [X] T010 [P] 在 `tests/unit/test_domain.py` 编写 Reference/Task/Attempt 状态与输入边界测试：类别 `artwork/form`、包装 1–2、形态 1–6、JPEG/PNG、任务 ID 唯一、上游 ID 可空（FR-001–004、014、018–019）。
- [X] T011 [P] 在 `tests/integration/test_storage_concurrency.py` 编写 SQLite 操作标识与摘要冲突、并发消费、提交意图持久化及重启计数测试；断言本地事务不保证远端恰好一次（FR-012–013、018–019）。
- [X] T012 在 `src/ecommerce_product_image_creator/domain.py` 实现 T010 的带类型实体，包含 Reference、Task、Form/View/Prompt revision、Confirmation、Attempt、Retry policy 和 Artifact 及数据模型中状态与关联约束。
- [X] T013 在 `specs/001-product-image-generation/contracts/mcp-tools.md` 依据 T008 明确已有任务的非收费执行快照准备、提示词版本/摘要、execution_digest、业务及费用授权交接、失效和拒绝；三个 Host 的可信展示及证据须实测，未解决时阻断收费。
- [X] T014 在 `src/ecommerce_product_image_creator/storage.py` 实现 SQLite 持久化、三维状态、唯一 task/revision/attempt 与操作标识、同标识不同摘要拒绝及原子并发资格；通过 T011。
- [X] T015 在 `tests/unit/test_file_boundary.py` 及 `src/ecommerce_product_image_creator/storage.py` 依据 T009 实现并离线验证图片实际内容与声明格式、受控根/路径/重解析点、防替换和快照摘要、解码资源保护、资源不足安全拒绝与明确报告；已声明上限时收费前拒绝，不依赖真实供应商测试（FR-014）。
- [X] T016 在 `src/ecommerce_product_image_creator/tasks.py` 实现依据 T013 的授权来源、范围、核对状态、消费及输入变更失效；缺失证据标记 `unverified` 且不得冒充通过，收费提交须满足 D2。
- [X] T017 在 `src/ecommerce_product_image_creator/server.py` 和 `tests/contract/test_mcp_protocol.py` 建立独立 stdio Server 骨架并用锁定 SDK 客户端验证初始化、工具发现、协议错误与工具失败区分、stdout 协议及 stderr 脱敏；业务任务不冒充 MCP Tasks Extension。

## Phase 3：US1 — 确认形态并获得六视图包装 Mock 组合图（P1）

**目标**：不收费准备、分阶段授权、每阶段一张组合图及人工审核。**独立测试**：用替身检查形态组合图确认后生成一张六视图包装 Mock 组合图；各阶段修订保留历史，视觉质量由人工判断。

### US1 测试先行

- [X] T018 [P] [US1] 在 `tests/contract/test_prepare_inputs.py` 覆盖包装 0/1/2/3、形态 0/1/6/7、类别缺失、可选视角、非 JPEG/PNG 和空要求；断言准备过程无 LLM 或绘图收费请求（FR-001–004、014）。
- [X] T019 [P] [US1] 在 `tests/contract/test_stage_inputs.py` 覆盖形态 1/6 张及 0/7 边界、排除设计图和旧候选；Mock 仅确认的形态组合原图加 1/2 设计图；核对快照关键内容变化失效、未确认不得提交（FR-020–022）。
- [X] T020 [P] [US1] 在 `tests/integration/test_multiview_completion.py` 用替身检查两阶段每次正常生成一张六视图组合图、形态标识与 Q 正交要求、形态确认门禁、Mock 人工审核和两阶段多轮修订历史；区分生成与业务最终接受（FR-010–011、020–023）。
- [X] T021 [P] [US1] 在 `tests/contract/test_submit_authorization.py` 验证先准备实际提示词和执行快照、确认前不可收费、提交时内容及范围复核，覆盖未授权、范围不匹配、重放、输入变化失效和并发；模型自述、布尔值、API Key 不构成授权证据（FR-012）。 新执行仅允许完整 v2 快照；旧格式仅供历史读取，覆盖伪造 v1、缺失 v2/范围、重放和不受信确认的拒绝；形态审核与收费授权分别验证。

### US1 实现

- [X] T022 [US1] 在 `src/ecommerce_product_image_creator/domain.py` 实现 T018 的分类、数量、非空自然语言、JPEG/PNG 与逐图类别约束，视角可选，异常返回受影响图片或类别。
- [X] T023 [US1] 在 `src/ecommerce_product_image_creator/server.py` 及必要的现有存储模块实现非收费 `prepare_product_image`：创建或修订任务，验证并持久化冻结图片字节及摘要、实际提示词正文/版本/摘要和涵盖阶段、模型、端点、分组、输出及操作范围的可恢复执行快照与 `execution_digest`；返回任务摘要及费用/耗时或 unknown，以替身验证且不调用绘图或 LLM（FR-001–004、012–014）。不依赖 T024；适配现有领域 ViewRevision 和本地资格中 Mock 必填单视图的旧约束，改为组合版本及范围并保持已有数据兼容，后续提交使用冻结字节。 形态/Mock 准备与记录校验可先以隔离夹具验证；生产 Mock 须接入 T026 定义的审核记录及可用的 Host 来源核对机制，缺失或不满足信任前提时拒绝。T023 的隔离验收依赖稳定审核记录/校验契约及夹具，不以 T051–T054 的真实 Host 联调通过为完成前置；生产 Mock 启用另须对应 Host 真实核验，不把夹具或 unverified 当成人工确认。新执行资格必须使用并核验完整 v2 快照，旧格式仅用于历史读取；阻断 LocalEligibility 的 v1 新操作降级路径并保留旧记录。
- [X] T024 [US1] 在 `src/ecommerce_product_image_creator/tasks.py` 基于已稳定的形态阶段准备接口与 v2 快照实现 Host Agent 提示词的阶段契约、版本管理、多轮修订与后续生成上下文；仅无收费部分可在 T023 Mock 和真实 Host 核验完成前独立开发，使用受控夹具；关键变化使旧确认失效，不引入独立 LLM（FR-012、020）。
- [X] T025 [US1] 在 `src/ecommerce_product_image_creator/provider.py` 实现独立供应商适配：默认 `gpt-image-2.5-sunburst`、异步 Edits、候选逆向分组，分别按 T019 组装形态和 mock 输入；实际分组与图片兼容性待验证。
- [x] T026 [US1] 实现形态组合图多轮修订与审核记录契约：绑定 task、形态版本、Artifact ID/摘要、审核决定、来源和信任状态及失效条件；先以无收费夹具实现记录校验及拒绝，保留 unverified，不能凭布尔值、ID 或摘要认定真人批准。真实 Host 来源交接交 T051–T054；生成仍依赖后续适配和授权。提示词要求一张 F/L/R/B/T/Q 标识组合图，不合成（FR-020–021）。该无收费子范围依赖已实现的形态准备接口，不等待 T023 完整 Mock 或 T024 完成。
- [x] T027 [US1] 在 `src/ecommerce_product_image_creator/server.py` 实现 `submit_product_image`：读取 T023 已保存快照，复核任务、修订、提示词、图片摘要和全部执行范围，核对 Host 授权及本地资格并在发送前持久化；使用与快照一致的冻结图片字节，禁止静默替换或扩权。缺适用授权停止，本地事务不保证远端恰好一次（FR-012、018–022）。 拒绝 v0/v1 历史摘要作为新执行凭据；必须由已核验且完整的 v2 快照、适用形态审核（Mock）和独立收费授权共同满足前置条件。
- [x] T028 [US1] 在 `src/ecommerce_product_image_creator/tasks.py` 实现包装 Mock 组合图的人工审核与多轮明确请求修订：每次正常绘图一张含六视图组合图，关联已确认形态版本、包装版本、attempt、产物及审核状态，保留历史；未经审核不得标记批准（FR-010、018、022–023）。
- [X] T029 [US1] 在 `src/ecommerce_product_image_creator/storage.py` 和 `src/ecommerce_product_image_creator/server.py` 返回经可读性、摘要、任务、阶段及版本核验的形态与包装 Mock 组合候选及审核信息；保留历史，最终裁决由外部业务流程负责（FR-011、018）。
- [X] T030 [US1] 执行 `tests/contract/test_prepare_inputs.py`、`tests/contract/test_stage_inputs.py`、`tests/contract/test_submit_authorization.py` 和 `tests/integration/test_multiview_completion.py`；归档无收费结果，失败不得标记完成。

## Phase 4：US2 — 多视角与补充说明（P1）

**目标**：分别保留包装区域、版本与形态视角、用途、不确定性。**独立测试**：用两张不同区域设计及多个形态视角的假输入核对上下文和提示词，不将单视角缺失结构或竞品品牌宣称为目标事实。

- [X] T031 [P] [US2] 在 `tests/unit/test_reference_context.py` 编写区域、版本、可选视角、用途/重要性、互补视角及未观察结构测试（FR-003–008）。
- [X] T032 [P] [US2] 在 `tests/integration/test_reference_context.py` 使用受控替身核对形态阶段不传包装图、mock 阶段仅传已确认组合与设计图，验证竞品品牌隔离及目标品牌视觉约束（FR-004、009、017、020、022）。
- [X] T033 [US2] 在 `src/ecommerce_product_image_creator/domain.py` 实现区域、版本、视角、可信度、用途与重要性记录；不把不同包装区域误判为不同版本或同一平面（FR-003–006）。
- [X] T034 [US2] 记录 Host Agent 所供提示词及阶段上下文：形态仅用形态资料，Mock 用已确认组合原图与包装设计，要求目标品牌视觉并排除竞品品牌；不得声称缺失结构真实还原，效果由人工审核（FR-006–009、017）。
- [X] T035 [US2] 运行 `tests/unit/test_reference_context.py` 与 `tests/integration/test_reference_context.py` 并记录结果；语义理解和视觉准确性仍需人工案例，不以替身冒充真实验证。

## Phase 5：US3 — 冲突、成本、故障与恢复（P1）

**目标**：清晰处理业务确认、费用信息、失败和 unknown。**独立测试**：假上游构造版本/结构冲突、429、已确认可安全失败、无 ID 超时及可查询任务；验证暂停、范围授权、持久计数和不盲目重提。

- [X] T036 [P] [US3] 在 `tests/unit/test_confirmation.py` 覆盖包装版本确认、结构冲突暂停、接受不确定性后的预览、建议子集人工确认、输入改变使旧确认失效（FR-005、007、015–016）。
- [x] T037 [P] [US3] 在 `tests/unit/test_retry_policy.py` 使用假时钟测试每逻辑操作每模型/接口最多 3 次、两个已批准模型总计最多 6 次均含首次、重启与 attempt 更换不重置、人工修订不限轮、5 秒间隔和 429 有效 Retry-After；无有效值停止，429/5xx/超时均不单独证明未执行（FR-013）。
- [x] T038 [P] [US3] 在 `tests/integration/test_unknown_recovery.py` 覆盖有 ID 优先查询、无 ID/不可查询保留 unknown、原任务可能仍执行及重复费用警示、新明确授权的关联尝试不覆盖原状态（FR-013、018–019）。
- [x] T039 [US3] 在 `src/ecommerce_product_image_creator/tasks.py` 实现版本、结构不确定性及资料子集的暂停与确认；无法可靠识别时如实报告，mock 固定排除原始形态图不视为额外自动筛选（FR-005、007、015–016）。
- [x] T040 [US3] 在 `src/ecommerce_product_image_creator/provider.py` 实现故障分类和有限安全恢复：仅确认安全且授权覆盖时按 Sunburst→Flare 切换；每模型/接口最多 3 次，总计最多 6 次含首次；429、5xx、超时均不单独证明未执行，unknown 不重提；不实现独立 LLM 回退。
  - T040-A 仅为可独立离线验证的故障分类及候选判断，不发送请求、不调度重试或切换；T040 完整实施仍遵守原有依赖及业务控制、授权、供应商能力验证门禁，T039 仍未完成。
- [x] T041 [US3] 在 `src/ecommerce_product_image_creator/tasks.py` 持久化逻辑操作、每模型/接口计数、等待及 attempt 链，实施 T037 策略；人工修订是新关联操作，质量不满意不自动重试（FR-013）。
  - T041-A 的持久化基础可先于 T040-B 离线调度；同组合三次尝试链、逐次等待及意图事务已有隔离实现和测试。跨模型候选／组合预算、完整跨修订关联和真实恢复仍须实现。完整 T040/T041 的业务、Host、供应商和授权门禁不变。
- [x] T042 [US3] 在 `src/ecommerce_product_image_creator/server.py` 实现 `get_product_image_task`，区分本地任务、上游尝试和本地交付，返回绘图耗时和供应商 cost 原值及 ฿，缺失为 unknown，重复查询不重复累计；Host token 不由 Server 计量（FR-011、018–019）。
- [x] T043 [US3] 在 `src/ecommerce_product_image_creator/tasks.py` 实现 T038 的 unknown 恢复与原任务关联，保留原有真实状态；不因网络超时、无 ID 或本地保存失败重新提交（FR-013、019）。
- [x] T044 [US3] 运行 `tests/unit/test_confirmation.py`、`tests/unit/test_retry_policy.py`、`tests/integration/test_unknown_recovery.py`，验证仅对确认安全且授权覆盖的故障按 FR-013 有限重试/切换，unknown 不重提；不将未验证分类标为安全。

## Phase 6：Polish、分层验证与交付

- [x] T045 [P] 在 `tests/contract/test_provider_multipart.py` 和 `tests/contract/test_provider_matrix.py` 用接收替身验证输入字段、顺序、MIME、字节/摘要、阶段映射、异步 ID、状态、结果及已知错误；`image` 与 `image[]` 的选择不凭其他模型推断。
- [x] T046 [P] 在 `tests/integration/test_crash_recovery.py` 注入 SQLite 提交意图、发送前后、上游 ID 落库、临时写入、文件发布与数据库登记之间的崩溃及并发，核验 unknown 不自动重提。
- [x] T047 [P] 在 `tests/integration/test_artifact_reconciliation.py` 覆盖上游成功但下载/保存失败、文件存在 DB 未登记、DB 已登记但缺失或损坏、摘要不符；仅在可靠证据下登记或重取原结果，不重生成。
- [x] T048 在 `src/ecommerce_product_image_creator/storage.py` 实现 T046–T047 的临时文件校验、原子发布、DB 登记与对账；分别报告上游成功及本地交付失败，安全重复操作仅限查询或重取同一产物。
- [x] T049 在 `src/ecommerce_product_image_creator/provider.py` 及 `tests/integration/test_download_safety.py` 依据 T009 实现并离线验证受控 URL/协议/DNS/跳转、下载资源与超时保护、内容格式校验、保存失败及磁盘/内存不足的安全报告，保留上游成功与本地交付失败；不依赖真实供应商测试或擅设固定业务阈值。
- [x] T050 在 `tests/contract/test_mcp_protocol.py` 使用实际锁定 SDK 验证三个工具输入输出 schema、协议/工具错误、结构化结果、stdio stdout 与 stderr，并保存独立协议证据。
- [ ] T051 [P] 在 `specs/001-product-image-generation/contracts/host-codex-integration.md` 用独立 Server 和无收费替身逐项验证 Codex 的实际 MCP 工具调用、真人允许/拒绝、自动放行、任务/提示词/参考图/费用及尝试范围展示、内容变化失效、Server 可见证据、Agent 绕过/重放；记录版本、受控前提和未覆盖。
- [ ] T052 [P] 在 `specs/001-product-image-generation/contracts/host-claude-integration.md` 用独立 Server 和无收费替身逐项验证 Claude Code 的实际 MCP 工具调用、真人允许/拒绝、自动放行、任务/提示词/参考图/费用及尝试范围展示、内容变化失效、Server 可见证据、Agent 绕过/重放；记录版本、受控前提和未覆盖。
- [ ] T053 [P] 在 `specs/001-product-image-generation/contracts/host-dsh-integration.md` 用独立 Server 和无收费替身逐项验证 DeepSeek Harness 的实际 MCP 工具调用、真人允许/拒绝、自动放行、任务/提示词/参考图/费用及尝试范围展示、内容变化失效、Server 可见证据、Agent 绕过/重放；记录版本、受控前提和未覆盖，不把测试 guard 当人工确认。
- [ ] T054 在 `specs/001-product-image-generation/contracts/architecture-review.md` 汇总 T051–T053 的实际可验证保证、运行前提、可见证据及自然人确认/防伪剩余风险；若不满足宪法授权核对则阻断对应 Host 的真实收费提交并报告治理决策需求。
- [ ] T055 在 `specs/001-product-image-generation/contracts/provider-adapters.md` 建立逐模型/端点/分组/阶段矩阵，分列供应商声明、历史样例、T045 替身、本项目真实服务证据；形态 1–6 张与 mock 2–3 张未实测前为未验证。
- [ ] T056 在 `specs/001-product-image-generation/contracts/provider-validation-plan.md` 编制真实绘图测试计划：素材、隐私、分组、模型、端点、张数、费用、等待和 unknown 不重提；列明逐次授权、成功/失败/未验证证据，不包含独立 LLM 测试。
- [ ] T057 在 `specs/001-product-image-generation/contracts/provider-validation-plan.md` 核验 T007–T009、T050、T054–T056 和人工对具体测试的授权；安全/授权条件 D1/D2 不满足或配置/能力未知时停止收费调用，D3 未处置时不得声称生产可用。
- [ ] T058 在 `specs/001-product-image-generation/evidence/provider-project/` 经 T057 对具体测试授权后，使用已通过 T015/T049 离线保护的独立 Server 实测绘图端到端和该次输入约束，记录模型、端点、可见分组、输入及提示词摘要、ID、状态、产物、cost/耗时和未核账单；unknown 不重提。
- [ ] T059 在 `specs/001-product-image-generation/evidence/provider-project/` 经每次请求另行授权，验证形态 1/6 输入和 Mock 2/3 输入及各阶段单张六视图组合输出、供应商限制与端到端行为；未知限制及未执行边界保留 unknown，不以历史样例外推。
- [ ] T060 在 `specs/001-product-image-generation/quickstart.md` 安排运营人员分别审核真实形态六视图组合图及单张六视图包装 Mock 组合图，判断形态仅用原参考与文字是否足以修订；记录任务、版本、授权及反馈，不以模型自评代替人工裁决。
- [ ] T061 [P] 在 `.github/workflows/ci.yml` 配置锁定安装、格式、静态、类型及免费测试；使用假凭据和替身，验证 CI 不调用真实供应商。
- [ ] T062 在 `README.md` 和 `MANUAL.md` 按交付指南记录已验证的独立 Server 本地 stdio、安装、根 `.env` 优先级、Host 交互、已知限制和 unknown 恢复；不声称未验证 Host 或供应商兼容。
- [ ] T063 在 `specs/001-product-image-generation/quickstart.md` 汇总 SDK 协议、离线测试、独立 Server/三个 Host、崩溃恢复、真实供应商和外部人工验收的分层证据及未完成项，脱敏并只清理本次临时资源。

## 依赖、并行及人工停止

- Phase 1 → Phase 2 → 各故事；T001–T006 提供工程入口，T010–T017 提供基础。T009 先完成风险、策略及离线验收设计；T015 依 T009 实施本地保护，T049 依 T009 及初版实现离线验证下载保护；T058/T059 在此之后、具体授权下验证真实供应商，不能倒置为 T015/T049 前置。T013/T016 的授权方案依赖 T008，收费 T057–T059 必须满足 D2 和具体人工批准。
- US1 为主流程；US2 的 T033–T034 依赖 T012、T024；US3 的 T039–T043 依赖 T014、T016、T024–T028。三个故事均为 P1；US1 可用替身单独演示，但完整业务 MVP 须覆盖 US1–US3。
- 先写 T018–T021、T031–T032、T036–T038 的可观察测试，再实现对应行为。T023 的形态准备与 v2 快照为 T024 无收费部分及 T026 审核契约子范围的共同前置；T026 的隔离记录与来源校验契约可先行，T023 Mock 消费该契约，以夹具验证，不依赖 T026 的真实绘图实现。T051–T054 另验证各 Host 真实交接；无可信来源时 Mock 保守拒绝，收费 T027 必须同时通过形态审核、独立收费授权及 v2 防降级核对。T023 整体状态须依据其完整验收，不能用 Host 联调或夹具互相冒充；T040、T046–T050 等后续条件不变。
- T051–T053 依赖独立 Server 与测试参与者，可在隔离配置及不同记录文件上并行；T054 汇总后才可评价 T057 授权门禁。T055 先记录历史及替身，T058 和每个 T059 真实请求均须各自明确授权。
- `[P]` 仅表示同一阶段前置满足后可分别处理独立文件；同一 `domain.py`、`provider.py`、`tasks.py`、`storage.py`、`server.py` 和 SQLite schema 的修改串行合并。单人可顺序执行。

### 并行示例

- US1：在 T012–T017 完成后，可同时准备 T018、T019、T020、T021；实现 T022–T029 因共享模块按依赖顺序执行。
- US2：在基础接口确定后，T031 与 T032 写入不同测试文件，可并行；T033、T034 的共享模块变更须与 US1 合并。
- US3：T036、T037、T038 可并行编写独立测试；T039–T043 按共享文件和状态依赖顺序实现。
- 跨领域：T045、T046、T047、T061 在其前置完成后使用不同文件，可并行；T051–T053 不共享持久 Host 配置或输出文件。

## 实施里程碑

| 里程碑 | 完成条件 |
|---|---|
| M1 工程入口 | T001–T006 完成；锁定安装、格式/静态/类型/测试入口可运行，凭据与忽略规则经过验证。 |
| M2 免费基础 | T009 的资源策略和离线验收设计、T010–T017、T018–T024 及 T031–T035 的离线部分通过，收费前提示词快照及 SQLite 可测；不以此声称 Host 授权或供应商限制已验证。 |
| M3 免费控制闭环 | T025–T030、T036–T049 的适用替身通过，T015/T049 分别验证本地输入和远程下载/保存保护；D1 按已确认策略验证，未确认安全或未经授权的重试和切换不启用，D2 未解决仍阻断真实收费。 |
| M4 协议与 Host | T050–T054 分别完成；三个 Host 的人工允许/拒绝、审核交接和证据独立记录；未覆盖者不标通过。 |
| M5 授权真实验证 | T015/T049 离线保护及 T055–T058 完成，对具体测试取得明确授权，保留独立 Server 的任务、状态、产物、费用与已观察限制；T059 未经授权的边界仍未验证，与生产发布分开。 |
| M6 业务及交付 | T059–T063 的适用部分完成，运营人员审核两阶段组合图；D3 已有批准处置，质量与 CI、Host、供应商证据分别报告，发布另依授权流程。 |

## FR 追溯与验收

| FR | 任务 |
|---|---|
| FR-001–004 | T010、T015、T018、T022–T023、T031–T034 |
| FR-005–009 | T031–T036、T039 |
| FR-010–011 | T020、T028–T030、T042、T060 |
| FR-012 | T008、T013、T016、T021、T027、T051–T054、T057 |
| FR-013–014 | T007、T009–T011、T015、T018、T022、T037–T038、T040–T044 |
| FR-015–017 | T032、T034、T036、T039 |
| FR-018–019 | T011–T012、T014、T027–T029、T038、T041–T043、T046–T048 |
| FR-020–023 | T019–T020、T024–T030、T032、T034、T059–T060 |

**策略**：先交付单人可运行、无费用替身验证的基础和 US1 最小流程，再补齐 US2/US3；真实调用、Host 和人工业务验收分别通过相应门禁。任务拆解不代表能力已实现或可发布。
