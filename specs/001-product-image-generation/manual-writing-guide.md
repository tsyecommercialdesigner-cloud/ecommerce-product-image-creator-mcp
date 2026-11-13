# 成员手册撰写指南

本指南服务于 [成员手册契约](contracts/manual-writing.md)，与当前 spec-kit 的项目宪法、功能 spec、plan、tasks 和相关 contracts 配合使用。通用工程交付细则以 [手册与交付规范](../../docs/delivering/MANUAL-WRITING-GUIDE.md) 为准。需求与实现不一致时记录差异，不能把目标写成当前事实。

## 1. 先核对实际信息

读取项目根 `AGENTS.md`、宪法及 `specs/001-product-image-generation/` 的 spec、plan、tasks 和相关 contracts，再对照 `pyproject.toml`、`uv.lock`、`.python-version`、服务入口、配置读取和验证记录。

| 项目 | 本项目当前已知值或核对方法 |
|---|---|
| 仓库 | `https://github.com/tsyecommercialdesigner-cloud/ecommerce-product-image-creator-mcp.git`；发布前重新核对来源和权限 |
| 发行包 / Python 模块 | `ecommerce-product-image-creator-mcp` / `ecommerce_product_image_creator` |
| 本地 MCP 服务及入口 | `ecommerce-product-image-creator` / `python -m ecommerce_product_image_creator.server` |
| Windows 默认安装目录 | `D:\MCP\ecommerce-product-image-creator-mcp` |
| 后端密钥 | `ZHENZHEN_GPTIMAGEGEN_API_KEY`；只描述名称和安全配置方法 |
| 依赖版本 | Python 3.12.14、uv 0.12.9；SDK 与依赖以锁文件和实际安装为准 |
| 发布版本 | 使用真实已发布、已验证的标签和完整 SHA；不存在时标记待发布，不从包版本推断 |

服务名、目录名和模块名各自核对，不对路径、扩展名或版本号进行全局替换。协议、SDK 和 Host 版本分别记录，不相互推断。更改路径时，安装、环境变量和 Host 条目必须同步。

## 2. 组织 README 和 MANUAL

README 提供用途、真实能力及最短安装入口，并链接 MANUAL。MANUAL 至少覆盖：

1. 环境、网络、仓库访问、Host、凭据与权限。
2. 固定版本获取、目录初始化、锁定依赖安装与首次配置。
3. 可用性检查、命令、预期结果、退出码与失败处理。
4. 各适用 Host 的 stdio 配置、合并、重启与工具发现。
5. 日常启动、排障与 stderr 日志；不污染 stdio 协议输出。
6. 源码、锁和共享配置与本机依赖、输入、图片、数据库及缓存的区别。
7. 实际工具、资源、提示和模板的发现结果、输入、输出及错误；不存在的类别如实说明。
8. Form、人工审核、Mock、任务查询、图片查看及可用恢复路径；仅描述实际可操作的步骤。
9. 费用、凭据、权限、素材许可、超时、取消、unknown、并发及重试边界。
10. 常见问题、更新、回滚和数据备份；无法验证的操作明确标记。

章节可以合并；不适用主题说明原因。长篇供应商或数据维护资料可单独维护并链接，避免重复维护同一详细规则。

## 3. 编写可复制示例

- 使用实际仓库、发布版本、Shell 和参数，提供完整命令与各 Host 的完整可合并配置；列出前置条件、运行位置、预期结果及失败处理。按交付指南，仅提供必要配置片段和合并要求，不列 Host 配置文件所在目录或快捷打开命令。
- 对密钥、图片路径、任务 ID 等动态值，说明成员如何取得或输入；占位符必须明确标识和替换规则，不能宣称无需替换即可执行。无已验证发布版本时说明阻断，不编造 SHA 或标签。
- `.env.example` 只提供空密钥。解释项目根 `.env` → 进程环境 → Windows 当前用户环境：仅缺失时回退，空值、非法值、重复及读取／解析失败按实际实现报错。保留已有 `.env`，不在示例和日志中公开密钥。
- 成员从仓库安装锁定依赖；日常启动复用虚拟环境或 `uv --no-sync`，不在启动时拉取、安装或升级。更新前保存用户配置、图片和数据库；数据迁移后的回滚须有经过验证的恢复步骤。
- Host 模型渠道与 MCP 绘图供应商分别说明。生成、查询或下载的费用、副作用及开关按当前代码和负责人决定填写；明确 unknown 不盲目重提。
- 素材、格式及支持数量按实际契约与实测范围填写。若确实支持 GIF、SVG、批量或其他形式，分别说明预览、原件及限制；不要继承其他项目的功能。

## 4. 三个 Host 与图片

Codex CLI、Claude Desktop、DeepSeek Harness Desktop 的 desktop Profile 分别记录实际版本、配置、工具发现、调用及图片展示。区分 MCP 返回 ImageContent、Agent 接收图片、Host 正文或工具面板显示、文件卡片及侧边查看；保留已登记原图的本地路径作为备用。

Host UI 允许或自动执行不自动证明 Server 可独立验证真人授权。Team Mode 和 Strict Mode 的实际授权与开关行为依据当前宪法、代码和工具契约说明。

仅当目标 DSH 版本及相关依赖实际存在代理绕过兼容问题时，提供服务级 `NO_PROXY` 与 `no_proxy` 的适用配置，指导保留已有自定义绕过项，并按实际证据处理 IPv6 回环写法；说明作用域和复测情况，不能将其他项目故障直接宣称为本项目事实。

## 5. 验证与维护

1. 核对 Markdown 内部链接，以及 JSON、TOML、YAML 和 Shell 示例的语法、参数与目录；列出未运行的示例。
2. 安装或启动说明变更时，按适用范围在隔离目录验证锁定安装、启动及实际 SDK 工具发现；未经授权不修改真实 Host 或发起收费调用。
3. 分别报告静态检查、隔离安装、协议、真实 Host 和真实供应商的通过、失败、不适用或未验证，不把旧提交的结果写成新版本结果。
4. 记录脱敏证据和版本；保留用户数据及必要证据，只清理本次创建的临时资源。涉及 reparse point 的清理遵循项目及用户明确限制。
5. 发布使用实际 GitHub 版本及完整 SHA，按工程流程单独审查、批准和验证；ZIP 不是默认交付。

当前 MANUAL 的实际完成情况须按本规范逐项评估；本指南不是现有功能、测试或发布状态的验收证明。
