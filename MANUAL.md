# 成员安装与使用手册

本手册对应 [v0.1.0](https://github.com/tsyecommercialdesigner-cloud/ecommerce-product-image-creator-mcp/releases/tag/v0.1.0)，完整提交为 `994a45f5a8ff5d8cc421315492e42e4a55379750`。该版本支持 Team Mode；实际验证范围与限制见第 5 节。

## 1. 安装

需要 Windows、Git、PowerShell 7、uv 0.12.9，访问 GitHub 和 Python 包源的网络，以及 Python 3.12.14。首次安装会下载依赖；日常启动不安装或升级依赖。确认 `D:\MCP` 有足够空间并且目标目录不存在：

```powershell
$ErrorActionPreference = 'Stop'
$ProjectDir = 'D:\MCP\ecommerce-product-image-creator-mcp'
if (Test-Path -LiteralPath $ProjectDir) { throw '目标目录已存在，请勿覆盖。' }
$ReleaseTag = 'v0.1.0'
$ReleaseCommit = '994a45f5a8ff5d8cc421315492e42e4a55379750'
New-Item -ItemType Directory -Force -Path 'D:\MCP' | Out-Null
git clone --branch $ReleaseTag --single-branch https://github.com/tsyecommercialdesigner-cloud/ecommerce-product-image-creator-mcp.git $ProjectDir
if ($LASTEXITCODE -ne 0) { throw 'clone 失败' }
$ActualCommit = git -C $ProjectDir rev-parse HEAD
if ($LASTEXITCODE -ne 0 -or $ActualCommit -ne $ReleaseCommit) { throw '检出提交与发布记录不一致' }
Set-Location -LiteralPath $ProjectDir
uv python install
if ($LASTEXITCODE -ne 0) { throw 'Python 安装失败' }
uv sync --locked --no-dev
if ($LASTEXITCODE -ne 0) { throw '锁定依赖安装失败' }
uv run --no-sync python -c "import sys; assert sys.version_info[:3] == (3,12,14)"
if ($LASTEXITCODE -ne 0) { throw 'Python 版本不符' }
New-Item -ItemType Directory -Force -Path 'runtime-data/inputs','runtime-data/state' | Out-Null
```

`runtime-data` 保存本机输入、数据库和结果；升级时保留，不放入 Git。不要将输入目录放在状态目录之内或反过来。已有安装不要重新 clone 覆盖；先备份 `.env` 与 `runtime-data`，等新版本提供经过验证的数据兼容和更新步骤后再更新。

## 2. 配置和收费

管理员在项目根目录创建 `.env`，填入网关账号的密钥，不要提交或发送该文件：

```dotenv
ZHENZHEN_GPTIMAGEGEN_API_KEY=在本机填写真实密钥
```

只填真实密钥、不能留空；读取优先级是项目根 `.env`、进程环境、Windows 当前用户环境，只有缺失才回退。`.env` 中有空值或错误不会回退。`.env.example` 只是空模板，不要直接把空值当成有效配置。

Team 模式默认开启，实际生成、远程查询和下载的三个开关默认关闭。以下 Host 示例保持这些开关关闭；要实际使用时，由管理员按需要将它们分别设为 `true` 并重启对应 Host。Team Live 下，Host 自动执行工具也可能产生费用，应先明确任务与模型；服务端不验证真人签名。真实费用以网关账单为准。

已实测的网关端点为 `https://ai.t8star.org/v1/images/edits?async=true`，分组 `reverse`，模型 `gpt-image-2.5-sunburst` 和 `gpt-image-2.5-flare`。仅在管理员信任并确认网关、下载域名及费用条件时启用。`TEAM_MODE=false` 选择 Strict 模式；Strict 的独立授权交接不属于当前团队自助流程。

## 3. Host 本地 stdio 配置

以下片段以第 1 节安装路径为准；合并到现有配置，保留其他 Server，勿在配置中填 API Key。输入和状态目录须预先创建；`PRODUCT_IMAGE_MAX_*` 是技术资源预算，不是统一业务尺寸政策。`ENABLE_TEAM_QUERY=true` 仍需调用方显式刷新；开启下载后一次刷新可能顺序查询并下载。

### Codex CLI

将以下内容合并进现有 `config.toml`（同名表存在时先核对）：

```toml
[mcp_servers.ecommerce-product-image-creator]
command = 'D:/MCP/ecommerce-product-image-creator-mcp/.venv/Scripts/python.exe'
args = ['-m', 'ecommerce_product_image_creator.server']

[mcp_servers.ecommerce-product-image-creator.env]
PRODUCT_IMAGE_INPUT_ROOT = 'D:/MCP/ecommerce-product-image-creator-mcp/runtime-data/inputs'
PRODUCT_IMAGE_STATE_DIR = 'D:/MCP/ecommerce-product-image-creator-mcp/runtime-data/state'
PRODUCT_IMAGE_MAX_INPUT_BYTES = '10000000'
PRODUCT_IMAGE_MAX_PIXELS = '10000000'
PRODUCT_IMAGE_MAX_DECODED_BYTES = '50000000'
TEAM_MODE = 'true'
ENABLE_REAL_GENERATION = 'false'
ENABLE_TEAM_QUERY = 'false'
ENABLE_TEAM_DOWNLOAD = 'false'
TEAM_GATEWAY_URL = 'https://ai.t8star.org/v1/images/edits?async=true'
TEAM_DOWNLOAD_HOSTS = 'files.anyroutes.cn'
```

### Claude Desktop

将下面的条目合并到现有 `mcpServers` 对象，不覆盖其他条目：

```json
{
  "mcpServers": {
    "ecommerce-product-image-creator": {
      "command": "D:/MCP/ecommerce-product-image-creator-mcp/.venv/Scripts/python.exe",
      "args": ["-m", "ecommerce_product_image_creator.server"],
      "env": {
        "PRODUCT_IMAGE_INPUT_ROOT": "D:/MCP/ecommerce-product-image-creator-mcp/runtime-data/inputs",
        "PRODUCT_IMAGE_STATE_DIR": "D:/MCP/ecommerce-product-image-creator-mcp/runtime-data/state",
        "PRODUCT_IMAGE_MAX_INPUT_BYTES": "10000000",
        "PRODUCT_IMAGE_MAX_PIXELS": "10000000",
        "PRODUCT_IMAGE_MAX_DECODED_BYTES": "50000000",
        "TEAM_MODE": "true",
        "ENABLE_REAL_GENERATION": "false",
        "ENABLE_TEAM_QUERY": "false",
        "ENABLE_TEAM_DOWNLOAD": "false",
        "TEAM_GATEWAY_URL": "https://ai.t8star.org/v1/images/edits?async=true",
        "TEAM_DOWNLOAD_HOSTS": "files.anyroutes.cn"
      }
    }
  }
}
```

### DeepSeek Harness Desktop

将以下条目合并到 desktop Profile 的 `cordis.patch.yml` 顶层数组，不覆盖其他 Profile 或现有 `insert` 条目：

```yaml
- insert:
    - id: mcp-ecommerce-product-image-creator
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: ecommerce-product-image-creator
        transport: stdio
        command: D:/MCP/ecommerce-product-image-creator-mcp/.venv/Scripts/python.exe
        args: ['-m', 'ecommerce_product_image_creator.server']
        env:
          PRODUCT_IMAGE_INPUT_ROOT: D:/MCP/ecommerce-product-image-creator-mcp/runtime-data/inputs
          PRODUCT_IMAGE_STATE_DIR: D:/MCP/ecommerce-product-image-creator-mcp/runtime-data/state
          PRODUCT_IMAGE_MAX_INPUT_BYTES: '10000000'
          PRODUCT_IMAGE_MAX_PIXELS: '10000000'
          PRODUCT_IMAGE_MAX_DECODED_BYTES: '50000000'
          TEAM_MODE: 'true'
          ENABLE_REAL_GENERATION: 'false'
          ENABLE_TEAM_QUERY: 'false'
          ENABLE_TEAM_DOWNLOAD: 'false'
          TEAM_GATEWAY_URL: 'https://ai.t8star.org/v1/images/edits?async=true'
          TEAM_DOWNLOAD_HOSTS: 'files.anyroutes.cn'
        toolCallTimeoutMs: 630000
        failOnStartupError: true
```

重启对应 Host，确认 Server 运行并发现六个工具。既有真实 Host 验证发生在开发环境；以上新安装路径和新审核工具的实际 Host 配置尚待实测。

## 4. 使用

1. 将有使用权的商品形态照片和包装设计素材放入 `runtime-data/inputs`。告诉 Agent 使用 `prepare_product_image` 准备 Form，提供至少一张 `form` 和一张 `artwork` 参考图、说明及提示词，明确选择 Sunburst 或 Flare，输出一张标记 F/L/R/B/T/Q 的组合图。当前 Form 请求登记两类素材，实际绘图参考仅选用 `form`；准备只冻结输入和提示词，不生成图片。
2. 核对图片、提示词、模型和费用风险。管理员启用生成后，让 Agent 使用 Prepare 返回的身份与摘要调用 `submit_product_image` 一次；一次提交可能收费。不要让 Agent 自行循环重试或切换模型。
3. 使用 `get_product_image_task` 查看本地状态。管理员启用查询后，明确要求 `refresh_remote=true` 查询原任务；启用下载且来源检查通过时会交付图片。若为 pending，可稍后由用户再次明确刷新；状态 unknown 或超时先检查原任务，禁止盲目重新生成。
4. 图片交付后人工检查六个视角及商品结构。`get_product_image_file` 读取已登记原图，可让 Host 保存到自身工作区；本地路径仍可作为备用。Codex CLI 终端不能直接内嵌图片；Claude Desktop 已观察到文件卡片及侧边预览，DSH Desktop 已观察到 Markdown 图片正文展示，实际显示取决于 Host。
5. 在 Team 模式下，成员查看 Form 后明确告知 Agent 接受或拒绝；Agent 使用 `review_form` 记录该 Task、Revision、Attempt 和 Artifact 的决定。接受范围为内部概念原型及后续 Mock，未提供的商品结构须另行核实。记录的来源是本地声明，Server 不能独立验证真人身份；Host 不应在成员未作决定时自行调用。Strict 模式下该入口拒绝。
6. Mock 使用已接受的 Form 和有权使用的设计素材。若更换设计素材，先由成员明确确认，调用 `confirm_mock_inputs`，再用返回的 `revision_decision_id` 调用 `prepare_product_image` 准备 Mock；素材未变时无需该 ID。审核和确认仅写本地记录，不生成图片；准备后仍须另行明确提交。Form 通过不代表 Mock 自动通过。

例如，将本人有权使用的图片分别保存为 `runtime-data/inputs/product.png` 和 `runtime-data/inputs/design.png` 后，可向已连接的 Agent 发送：

```text
只使用 ecommerce-product-image-creator。先不生成：用
D:/MCP/ecommerce-product-image-creator-mcp/runtime-data/inputs/product.png 作为 form，
D:/MCP/ecommerce-product-image-creator-mcp/runtime-data/inputs/design.png 作为 artwork，
准备 Sunburst Form：一张白底六视角组合图，标记 F/L/R/B/T/Q，保持商品形状。
展示准备结果、模型、费用是否已知和输入摘要，等待我确认，先不要调用 Submit。
```

上述提示要求 Agent 仅准备。确认后再明确要求一次提交；若生成开关未启用，将不会执行真实生成。实际文件格式与工具的 MIME 参数必须一致。

首次测试只验证了每个模型 Form 1 张、Mock 2 张输入；更多输入数量、供应商账单及多实例并发下载未充分验证。不得把本地 SHA-256 说成供应商独立摘要。遇到下载或查询失败，不重新提交生成；记录任务 ID 并联系维护者。

## 5. 验证及限制

v0.1.0 的 [Windows CI](https://github.com/tsyecommercialdesigner-cloud/ecommerce-product-image-creator-mcp/actions/runs/43372471051) 在发布提交上完成 1095 项免费测试；[发布 CD](https://github.com/tsyecommercialdesigner-cloud/ecommerce-product-image-creator-mcp/actions/runs/43373460155) 从标签验证了锁定运行依赖安装与 MCP 工具发现。此前真实双模型 Form／Mock 验收及 Host 测试的范围以对应记录为准；发布过程没有重新执行真实供应商或 Host 验收。Host 模型交互本身也可能收费，与图片网关费用分开。
