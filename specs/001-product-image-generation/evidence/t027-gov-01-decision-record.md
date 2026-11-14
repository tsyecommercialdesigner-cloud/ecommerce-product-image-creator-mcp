# T027 正式验收治理记录

> 历史状态说明：本记录反映 2026 年 11 月的无收费离线验收及当时的任务状态和生产门禁，不代表现行 Team Mode、Strict Mode 或真实供应商验收状态；当前适用范围见 [MCP 工具契约](../contracts/mcp-tools.md)、[成员手册](../../../MANUAL.md)及现行实现。

状态：**T027 已按批准的无收费本地能力范围正式闭项；完整历史 v1 Prepare 兼容验收延期；生产收费生成未批准。**

## 正式任务与范围

任务 ID：T027。以下原文来自 [tasks.md](../tasks.md)：

> 在 `src/ecommerce_product_image_creator/server.py` 实现 `submit_product_image`：读取 T023 已保存快照，复核任务、修订、提示词、图片摘要和全部执行范围，核对 Host 授权及本地资格并在发送前持久化；使用与快照一致的冻结图片字节，禁止静默替换或扩权。缺适用授权停止，本地事务不保证远端恰好一次（FR-012、018–022）。 拒绝 v0/v1 历史摘要作为新执行凭据；必须由已核验且完整的 v2 快照、适用形态审核（Mock）和独立收费授权共同满足前置条件。

本记录的验收范围是 Form／Mock 首次 MCP 提交的无收费本地控制及隔离替身，不是生产真实执行。正式 FR 映射为 FR-012、FR-018～FR-022；相关场景包括 SC-011 的 unknown 状态、SC-013 的 Mock 输入和 SC-014 的 Form 输入。这些 SC 的完整真实供应商／人工视觉验收并未由本记录完成。

## 可核验的负责人原话和决策状态

来源是本次会话中 T027-ACCEPT-V1 指令开头的用户原话（未推断姓名、身份凭证或审批时间）：

> 完整 v1 Prepare 的处理: 采用方案 C，延期完整历史兼容验收，附条件验收 T027 本地能力
>
> T027 允许以当前无收费本地能力结项

随后，本次会话的《T027 无收费本地能力正式验收决定》提供以下原始批准（未推断签署人姓名、身份凭证或审批时间）：

> **D-T027-03：批准。**
>
> 接受现有 MCP SDK 授权范围拒绝、独立 Host 与费用授权验证、SQLite 消费唯一约束、并发竞争及事务回滚的组合证据，作为 T027 无收费本地首次提交的授权机制验收依据。
>
> 该批准不构成真实 Host 身份来源或生产收费授权已经通过的证明。
>
> **D-T027-04：附条件批准。**
>
> 接受最终发送前复核完成后仍可能存在的跨系统撤销竞态，作为当前无收费离线实现的已知限制。
>
> 要求继续保持生产默认拒绝，并将真实 Host 撤销交接、远程发送边界和生产等价恢复列入后续集成验收。不得宣称远程 Exactly-Once 或取消成功。
>
> **D-T027-02：确认此前批准的方案 C。**
>
> 完整历史 v1 Prepare 兼容性验收延期，继续记录为 `evidence_missing`，不得标记为 N/A 或通过。
>
> 由项目负责人负责跟踪该问题，在正式生产上线验收前核对其适用范围与后续处置方案。
>
> **D-T027-01、D-T027-05：确认此前批准范围。**
>
> 认可 T027 的 Form／Mock 首次提交已达到无收费本地 MCP 能力的验收条件。
>
> **任务状态修改授权：批准。**
>
> 授权 Codex 在核对上述决定的原始记录后，仅将 `tasks.md` 中 T027 的复选框更新为 `[x]`，并同步正式验收记录。
>
> 不得修改其他任务的完成状态，不得改变生产授权、供应商发送或费用控制配置。
>
> 本次批准不包含 Git Add、Commit、Push、PR 或 Merge 权限。

| 决策 | 可核验状态 | 依据和限制 |
|---|---|---|
| D-T027-01 | 已明确附条件认可本地能力 | 第一条原话；最终状态测试证据见下。它不证明真实 Host 或供应商能力 |
| D-T027-02 | 已批准方案 C | 第一条原话；完整历史 v1 Prepare 兼容性延期，仍为 evidence_missing，不标 N/A 或 passed |
| D-T027-03 | 已批准 | 本次正式决定明确接受授权机制的隔离组合证据；不证明真实 Host 身份或生产收费授权 |
| D-T027-04 | 附条件批准 | 本次正式决定接受离线残余竞态，继续保持生产默认拒绝并要求后续集成验证 |
| D-T027-05 | 已批准无收费本地范围正式闭项 | 本次正式决定确认 T027 本地验收并明确授权仅修改 T027 状态；不批准真实收费 |

核对本次原始批准和既有证据后，仅将 `tasks.md` 的 T027 复选框由未勾选改为 `[x]`。其他任务完成状态和生产配置保持不变。

## 测试、实现和证据

最终文件状态离线验收记录（`t027-accept-v1-20261109.md`，本地留存，未纳管）记录 2026-11-09 的实际命令：全量 pytest 762 passed、0 failed、0 xfailed、0 xpassed、0 skipped，退出码 0；锁定安装、`ruff format --check src tests`、`ruff check .`、Pyright 和 `git diff --check` 均退出 0。完整命令、环境、测试工作区和 122 个输入文件的前后摘要见该记录。此处引用历史实测；本治理轮不重新执行 pytest。

本次勾选 T027 之前，按该记录同一清单重新计算 122 个源码、测试及关键验收文件聚合 SHA-256，仍为 `9b2f0c63954a35429ba319438879d17b81ef968c53ca35b6813b9e6f5c7e7c7c`；随后经授权仅修改 tasks.md 的 T027 复选框，因此该包含 tasks.md 的旧聚合摘要不再适用于修改后的文件集合。源码和测试未修改；摘要不覆盖其他文件或证明整个 Git 工作区清洁。

实现入口为 `server.py::submit_product_image`、`preparation.py::read_prepared`、`first_authorization.py::check/consume`、`tasks.py::LocalEligibility` 和 `offline_submission.py::submit_offline`。详细 A～P 矩阵及逐项测试名见上述最终验收记录；重点证据：

- `tests/contract/test_submit_mcp_b1.py::test_sdk_first_submission_with_two_isolated_authorizations`：Form、冻结字节、授权、两独立进程竞争、重放及事务注入；使用假来源与传输。
- `tests/contract/test_submit_mock_mcp.py::test_mock_first_submission_sdk`：Mock 正向、审核来源、文件完整性、Form 修订及业务选择失效。
- `tests/contract/test_submit_legacy_auth_sdk.py`：双授权独立失效、entered 后撤销、旧 v0 读取与拒绝，以及 v1 摘要引用 v2 的拒绝。
- `tests/contract/test_submit_auth_reuse_sdk.py`：另一操作的授权作用域拒绝、持久风险授权 ID 类型隔离，以及最终复核后提交层线程交错；后一项不是 SDK 直接交错。
- `tests/integration/test_legacy_gate.py` 与 `tests/integration/test_offline_submission.py`：v1 资格防降级、事务回滚、unknown 不重发和历史读取；其领域／集成结论不冒充真实 Host。

完整 v1 Prepare 缺可信 Schema、写入器或历史 SQLite 样本。根据本次决定，由项目负责人负责跟踪，在正式生产上线验收前核对适用范围与后续处置；本记录不指定个人姓名、具体日期或新任务。历史记录不得覆盖，不完整 v2 凭据不得获得新执行资格，旧 v0/v1 不得自动提升为 v2。

## 保留生产门禁及依赖

`first_authorization._verify_source` 与 `offline_submission._verify_authorization` 生产默认返回 False；`_offline_dispatch` 默认拒绝。SQLite 本地顺序不保证外部 Host 撤销与网络发送原子化，不证明远程取消或 exactly-once。真实 Host 执行许可与撤销交接由 T051～T054 验证；Sunburst／Flare 真实执行和查询协议、真实账单和费用、真实 Artifact 发布恢复及生产等价崩溃恢复仍未验证，T055～T059 等相关门禁保持关闭。T042 的可信生成费用及查询／下载费用独立核验尚未完成。D03 多级风险谱系与解除规则仍 pending_clarification，不能与 D-T027-03 混淆。

T039～T043 正式依赖 T014、T016、T024～T028；T027 现已在无收费本地范围勾选，不能自动完成 T026、T028 或其他任务。T026、T028、T037、T038、T044 的候选验收应分别审查。源码、测试、tasks、锁文件及证据目录仍未跟踪；尚无 Git 可复现交付或 CI 通过证明。未进行 Git Add、Commit、Push、PR 或 Merge。
