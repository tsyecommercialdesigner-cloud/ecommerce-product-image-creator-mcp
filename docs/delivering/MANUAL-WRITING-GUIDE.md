# MCP Server 手册与交付编写规范

## 1. 定位与适用范围

本指南适用于复用 MCP Server 项目约定的项目，集中维护 README、MANUAL、客户端配置与成员安装/交付细则；工程、CI/CD 与 Git 跟踪细则见 [工程指南](../engineering/ENGINEERING-GUIDE.md)。它不预设业务服务商、项目名称、API 地址、模型、功能清单或密钥字段。Python、Node.js、各 Host 及部署分支按项目实际适用；其他技术栈补充经过验证的等效流程。

约定文件放在目标仓库根目录，本指南放在 `docs/delivering/MANUAL-WRITING-GUIDE.md`。项目专属的技术选择、兼容性例外和已验证版本记录在其 README、MANUAL 或开发文档中，明确范围和依据；不把某个项目的限制推广为所有项目的限制。

本指南中的配置是结构示例，不是已验证的业务配置。编写具体项目手册时，必须根据实现填写完整可用值，删除无关技术栈分支，并区分静态检查、隔离 CLI 测试、实际 Host 联调和未验证项。编辑规范不授权执行文中的业务命令、收费任务或修改用户现有 Host 配置。

MANUAL 的消费 Host 接入仅提供本地 stdio 配置与操作，不提供 Streamable HTTP 或 HTTP 代理接入分支。`config.toml`、`.claude.json`、`cordis.patch.yml` 等 Host 配置只给必要片段与合并要求，不列所在目录或快捷打开命令。项目若另有远程服务部署，其服务端工程要求按工程指南单独维护，不扩展本手册的 Host 接入范围。

GitHub 仓库安装、uv/npm、PowerShell 7、Windows 安装目录和服务名转换是本模板的默认工程选择，不是 MCP 协议或 spec-kit 对所有项目的要求。项目已选定的部署方式与技术约束优先；例外注明依据与验证范围，不能把某个消费项目的选择反写成全局限制。

### 与需求规范及 spec-kit 的关系

用户要求与项目规范定义预期行为，源码、锁文件和协议发现结果说明实际状态。编写使用文档时核对二者，记录缺失能力、行为差异与未验证项；未实现的需求不能列成已可用功能，现有缺陷也不能自动改写成验收标准。

采用 spec-kit 的项目读取当前功能规范、方案与契约，并在相关文档中引用其需求/验收标识及测试证据。项目原则由 `.specify/memory/constitution.md` 维护，功能材料通常位于 `specs/<feature>/`；实际目录及命令以所用版本和 Agent 集成为准。本指南维护使用与交付细则，功能规范维护需求，技术方案维护实现决策，任务系统维护执行状态；发生变更时同步受影响的材料，不复制多份独立维护的需求正文。未启用 spec-kit 的项目沿用现有规范工作流，不因编写手册自动安装工具。

## 2. 项目参数与命名映射

编写前建立参数表；不要从另一个项目的文档直接继承名称、目录、版本或凭证变量。

| 参数 | 含义与来源 |
|---|---|
| `<project_name>` | 项目/目录原名，来自用户约定或仓库元数据。 |
| `<repository_url>` | 该 MCP 项目实际 GitHub 仓库的 HTTPS/SSH 地址，核对 origin 与仓库归属；不内嵌令牌。 |
| `<release_tag>` / `<commit_sha>` | 经过 CI/CD 发布的实际标签及完整提交 SHA；不是从元数据版本推测出的字符串。 |
| `<distribution_name>` | 发行包名称，来自 `pyproject.toml` 或 `package.json`；不等于 Python 模块名。 |
| `<module_name>` | Python 可导入模块和 `-m` 入口，来自源码与入口验证。 |
| `<service_name>` | 本地 MCP 服务标识，将原服务名称的每个 `.` 替换为 `-`，再验证目标 Host 约束与唯一性。 |
| `<install_dir>` | 实际安装目录，Windows MCP 默认 `D:\MCP\<project_name>`。 |
| `<entry_file>` | Node.js 已构建入口或可直接运行的 JS 源码，示例为 `dist/index.js`。 |
| `<version>` / `<sdk_extra>` | 元数据版本、实际声明的可选依赖；没有 extra 就不提供该安装步骤。 |
| `<service_credential_env>` | 后端服务实际读取的变量名；可能有多个，也可能不需要。 |
| `<project_env_file>` | 本地 MCP 实际项目根目录中的 `.env`；默认 `<install_dir>\.env`，不由 Host 的工作目录决定。 |
| 安装方式 / 目标使用者 | 本地成员安装，由 Host 通过 stdio 启动项目服务。 |
| SDK / MCP 协议版本 | SDK 包与锁定版本、服务所支持的协议版本列表；二者独立记录，不能由 SDK 主版本推断协议版本。 |
| 传输 / Host 兼容范围 | 本地 stdio；记录 Host 版本及已验证的运行时、命令和参数组合。 |
| 规范与验证依据 | 对应功能规范、验收标识、工具契约及测试结果；采用 spec-kit 时链接现有材料。 |

仅对服务名称及其引用应用 `.` → `-`。不得全局替换配置文件中的小数点，也不得改动版本号、安装目录、模块名、文件扩展名、URL 或凭证。例如：目录 `D:\MCP\example.mcp` 保持原样，本地服务名为 `example-mcp`；Node.js 入口仍为 `D:\MCP\example.mcp\dist\index.js`。

DSH `serverName` 使用不含小数点的服务标识；还应核对所用插件版本的长度与字符限制。名称必须与对应配置键一致；若 Host 长度限制迫使使用别名，记录明确的一对一映射，不改安装路径。检查转换或缩短后是否重名，禁止静默覆盖不同服务。

## 3. 从 GitHub 安装、日常启动与更新

### 部署分支与前置条件

成员默认从实际 MCP Server 的 GitHub 仓库 clone 已发布且通过 CI 的版本，在自己的机器安装锁定依赖；Windows 默认路径为 `D:\MCP\<project_name>`。默认分支用于开发，发布标签与完整提交 SHA 用于复现成员版本；不在每次启动时拉取最新代码。ZIP 仅在用户明确需要时按第 9 节另行交付。

手册列出 Git、对应 Python/uv 或 Node.js/npm 的支持版本、包源/代理要求，以及私有 GitHub 仓库的访问授权。认证使用成员自己的 SSH/凭据管理机制，不把令牌写进 clone URL、示例、日志或 Git 配置模板。依赖下载和业务 API 联网分别说明；没有 GitHub 或包源访问条件时准确报告失败，不宣称离线可用。

本节只维护本地成员安装流程。项目另有远程部署时，其 CI/CD、部署与验收资料按 [工程与 CI/CD 规范](../engineering/ENGINEERING-GUIDE.md) 单独维护，不在 MANUAL 增加远程 Host 配置或连接操作。

Windows 默认安装目录为 `D:\MCP\<project_name>`：保持 `MCP\项目原名` 的目录深度与命名规则，仅调整默认盘符。安装前核对 D 盘可用空间及实际 Host/MCP 运行账号的目录访问权限；项目另行指定安装位置时，统一更新手册、启动/Host 配置和 `.env` 定位。

### 首次 clone：仓库地址与版本来自实际项目

`<repository_url>` 必须来自该 MCP 项目已确认的 GitHub 仓库（核对其 `git remote get-url origin`、仓库归属与可访问性），不能复制本规范维护仓库或另一 MCP 项目的地址。`<release_tag>` 取已发布且通过 CI 的真实标签，手册同时记录对应完整 SHA；不得编造地址、标签或用元数据版本推测标签名。尚无发布标签时，明确给出已通过 CI 的完整提交及经验证的获取步骤。

下面是 PowerShell 7 结构示例；具体项目手册必须填写真实地址、标签和项目名。只创建安装父目录，`git clone` 自动创建 `<project_name>`，不预建项目目录，不覆盖已有目录：

```powershell
$ErrorActionPreference = 'Stop'
$RepositoryUrl = '<repository_url>'
$ReleaseTag = '<release_tag>'
$InstallRoot = 'D:\MCP'
$ProjectDir = Join-Path $InstallRoot '<project_name>'
if (Test-Path -LiteralPath $ProjectDir) {
  throw '项目目录已存在，请按更新流程处理，不能覆盖安装。'
}
New-Item -ItemType Directory -Path $InstallRoot -Force | Out-Null
git clone --branch $ReleaseTag --single-branch $RepositoryUrl $ProjectDir
if ($LASTEXITCODE -ne 0) { throw '仓库获取失败，停止安装。' }
git -C $ProjectDir rev-parse HEAD
if ($LASTEXITCODE -ne 0) { throw '无法确认安装提交，停止安装。' }
Set-Location -LiteralPath $ProjectDir
```

将输出的完整 SHA 与发布记录核对，再执行对应技术栈的安装。标签 checkout 处于 detached HEAD 是成员固定版本的正常状态；开发者按工程指南另建分支。仓库若使用 submodule 或 Git LFS，手册补齐项目实际需要的初始化、授权与版本校验，不假定普通 clone 已取得全部文件。

### Python：安装运行依赖

仓库跟踪 `src/`、`pyproject.toml`、`uv.lock`、构建后端所需输入与运行 assets，不能依赖维护者本机 `.venv`。开发机使用 `uv sync --locked`；成员完成 clone 后执行：

```powershell
$ErrorActionPreference = 'Stop'
$ProjectDir = 'D:\MCP\<project_name>'
Set-Location -LiteralPath $ProjectDir
uv sync --locked --no-dev
if ($LASTEXITCODE -ne 0) { throw '依赖安装失败，停止启动。' }
```

如确有可选依赖，给出项目真实的 `uv sync --locked --no-dev --extra '<sdk_extra>'`；`--extra` 不等于 `--group`。另有默认开发依赖组时补充明确排除参数并实测运行依赖完整性。安装落在项目 `.venv`，不使用全局 pip、不重新 `uv init`，不删锁或用升级命令掩盖失败。

### Node.js/TypeScript：先判断是否需要构建

Git 不会传送被忽略的 `dist/`。TypeScript 或需要构建的项目必须从已跟踪源码构建：运行时包置于 `dependencies`，构建/测试工具置于 `devDependencies`，构建入口、配置与 assets 都随仓库提交。手册使用该项目真实存在的构建脚本；不能在省略构建工具后直接启动不存在的 `dist/index.js`。

```powershell
$ErrorActionPreference = 'Stop'
$ProjectDir = 'D:\MCP\<project_name>'
Set-Location -LiteralPath $ProjectDir
npm ci --include=dev
if ($LASTEXITCODE -ne 0) { throw '依赖安装失败，停止构建。' }
npm run build
if ($LASTEXITCODE -ne 0) { throw '构建失败，停止启动。' }
$EntryFile = Join-Path $ProjectDir '<entry_file>'
if (!(Test-Path -LiteralPath $EntryFile -PathType Leaf)) {
  throw '缺少构建入口，请核对项目构建配置。'
}
```

`--include=dev` 确保首次构建工具可用；不要求成员额外运行测试。仅直接运行已跟踪 JS、确实不需要构建的项目使用下面的安装流程，不编造 `build` 脚本：

```powershell
$ErrorActionPreference = 'Stop'
$ProjectDir = 'D:\MCP\<project_name>'
Set-Location -LiteralPath $ProjectDir
npm ci --omit=dev
if ($LASTEXITCODE -ne 0) { throw '依赖安装失败，停止启动。' }
```

核对 CommonJS/ESM、入口、assets、原生模块和生命周期脚本；项目 `.npmrc` 或安装脚本策略须允许实际必需的构建步骤并说明依据，不全局关闭安全策略。构建只清理自己的可重建输出，不改 `.env`。有明确需要时可在构建后用已验证流程移除开发依赖，但不能删掉运行必需内容。其他包管理器沿用既有锁定安装/构建流程。

### 首次配置与日常启动

安装成功后，需要凭据的项目按第 4 节从空值 `.env.example` 创建私有 `.env` 并填写；无认证项目跳过。完成首次协议检查，再配置 Host。以下启动命令只复用已准备的环境，不拉取代码、不安装、不构建、不升级：

```powershell
$ProjectDir = 'D:\MCP\<project_name>'
uv --directory $ProjectDir run --no-sync python -m '<module_name>'
if ($LASTEXITCODE -ne 0) { throw 'MCP 服务退出失败，请检查 stderr。' }
```

```powershell
$ProjectDir = 'D:\MCP\<project_name>'
node (Join-Path $ProjectDir '<entry_file>')
if ($LASTEXITCODE -ne 0) { throw 'MCP 服务退出失败，请检查 stderr。' }
```

项目手册只保留适用分支并填入真实入口，Host 配置使用对应绝对路径。Python 用 `--directory`，Node.js 用入口绝对路径；不要求 Host 设置 `cwd`，资源与 `.env` 定位不能依赖调用方工作目录。stdio 进程等待协议输入时没有普通输出是正常行为，不以能启动代替协议验收。缺少模块或入口时返回安装/构建步骤排查，不能在启动命令中偷偷修复环境。

### 升级与回滚

先停止 Host 管理的实际 MCP 子进程，核对仓库 origin 与目标发布记录，记录当前完整 SHA。保留成员 `.env`、其访问权限和私有数据；需要备份时放在私有位置，不进入 Git。工作区有本地源码改动或未跟踪冲突时停止，让成员先处理，不能强行覆盖。

```powershell
$ErrorActionPreference = 'Stop'
$ProjectDir = 'D:\MCP\<project_name>'
$TargetTag = '<release_tag>'
$Changes = git -C $ProjectDir status --porcelain
if ($LASTEXITCODE -ne 0) { throw '无法检查仓库状态。' }
if ($Changes) { throw '存在本地改动，请先保留并处理后再更新。' }
$PreviousCommit = git -C $ProjectDir rev-parse HEAD
if ($LASTEXITCODE -ne 0) { throw '无法记录当前版本。' }
Write-Host "更新前提交：$PreviousCommit"
git -C $ProjectDir fetch origin tag $TargetTag
if ($LASTEXITCODE -ne 0) { throw '目标版本获取失败。' }
git -C $ProjectDir switch --detach --no-overwrite-ignore "refs/tags/$TargetTag"
if ($LASTEXITCODE -ne 0) { throw '版本切换失败，保留现有配置并停止。' }
git -C $ProjectDir rev-parse HEAD
if ($LASTEXITCODE -ne 0) { throw '无法确认更新提交。' }
```

核对目标 SHA 后，按新版本重新执行对应锁定依赖安装及必要构建，合并新增配置字段并验证协议，再重启服务；任一步失败都不得启动残留旧构建。`--no-overwrite-ignore` 用于拒绝覆盖包括私有文件在内的已忽略文件；此流程不使用 `pull` 自动跟随默认分支，也不执行 `reset --hard`、`clean -fdx` 或清空目录。

回滚切换到已记录且验证过的旧标签/完整 SHA（可用相同 `git switch --detach --no-overwrite-ignore '<previous_commit>'`），随后重新锁定安装、构建和验证，不仅切换源码。手册说明数据迁移的兼容限制；版本管理不等于业务数据备份，不能承诺任意版本均可无损回滚。

### 共同验收

在独立临时目录从仓库的目标版本 clone，完成安装、必要构建、配置加载与协议检查，再验证升级/回滚保留测试 `.env` 的内容和权限。不得借用开发机的环境、全局或祖先 `node_modules`、`NODE_PATH`；网络/凭据不足时记录失败或未验证，不能只凭脚本解析通过宣称安装成功。安装、构建、启动、协议和真实业务调用分别报告。

## 4. 项目 .env 与凭证管理

### 定位与字段

- 本地 MCP 的 API 密钥默认保存到实际项目根目录的 `.env`，例如 `D:\MCP\<project_name>\.env`；`<project_name>` 表示项目目录原名，与其他模板中的 `<repo_name>` 含义相同。Python/Node.js 必须显式定位并读取此文件，不搜索 Host 工作目录或父目录中的其他 `.env`，不依赖 `cwd`。
- 根目录从项目约定的安装布局和入口位置确定，必要时使用明确的非秘密绝对路径参数；不能假定模块从 `.venv` 安装位置回溯一级就是项目根目录。验证 Python 模块入口、Node.js 源码或 `dist/` 入口从不同 Host 工作目录启动时均加载同一文件。选择解析库时显式指定路径，其依赖纳入运行依赖与锁文件。
- 列出后端服务凭据的字段名、用途、是否必填、格式约束及读取来源。`.env` 字段名与源码实际读取的字段一致；`SERVICE_API_KEY` 仅是中性示例，不要求所有项目采用，也不假定密钥有固定前缀。无需认证的项目不强加 `.env` 或密钥要求。
- 交付 UTF-8 的 `.env.example`，仅保留项目实际字段、必要注释及空的密钥值；不填真实密钥或看起来可用的假密钥。以下只是格式示例，消费项目须替换实际字段名，填入真实值的 `.env` 只留在成员机器上：

```ini
# 后端服务密钥：在私有 .env 中填写，禁止提交
SERVICE_API_KEY=
```

### 读取优先级与失败行为

按字段决定来源，优先级为项目 `.env` → 当前 MCP 进程环境 → Windows 当前用户级环境变量。必须区分“字段没有定义”和“已定义但为空”，不能用简单的真值判断或解析库默认的“环境优先”行为实现此规则。

| 项目 .env 状态 | 必须采取的行为 |
|---|---|
| 文件可读、可解析，目标字段存在且通过项目格式校验 | 使用文件值，即使进程或用户级环境里有不同值。 |
| 文件不存在，或文件可读可解析但没有定义该字段 | 先查进程环境；该字段未定义时再兼容 Windows 当前用户级设置。非 Windows 平台按实际进程环境及已明确的兼容来源处理。 |
| 字段已定义但值为空、仅空白，或不符合实际格式 | 明确报错，不使用进程或用户级旧密钥。复制空值模板后必须填写；如确需走兼容来源，应明确移除该字段。 |
| 文件存在但无法读取、语法错误或同一凭据字段重复定义 | 明确报错，不把损坏或权限错误当作“没有配置”，不回退。 |

选择环境兼容来源后，同样校验其值；已定义但为空或格式错误时报错，不继续寻找另一份旧值。必需字段在所有适用来源均缺失时失败，非必需字段按项目声明处理。Windows 用户级回退须按实际运行账号显式实现并验证，不能假定进程环境能实时读取注册表里最新的用户级设置；服务账号也不会自动取得交互用户的配置。

解析 `.env` 只读取配置数据，不执行 Shell/JavaScript；默认不展开 `${VAR}` 或进行命令替换。核对解析库对注释、引号、特殊字符和重复字段的行为，必要时增加明确校验；密钥格式来自实际服务要求，不用通用模板虚构格式。先解析/校验文件中的字段，再解析兼容来源，避免先导入环境导致来源与空值丢失。错误只包含字段名、来源、必要路径与原因，不打印值、完整文件、带秘密的异常上下文或请求头；stdio 日志写 stderr。

### 首次配置、权限与升级

首次配置使用下面流程的项目化版本；命令只复制和打开模板，不把密钥写入共享命令或历史，也不创建全局用户环境变量。不需要凭据时省略此流程：

```powershell
$ErrorActionPreference = 'Stop'
$ProjectDir = 'D:\MCP\<project_name>'
$EnvPath = Join-Path $ProjectDir '.env'
$EnvExamplePath = Join-Path $ProjectDir '.env.example'
if (Test-Path -LiteralPath $EnvPath) {
  Write-Host '保留已有 .env，请检查需要更新的字段。'
} else {
  if (!(Test-Path -LiteralPath $EnvExamplePath -PathType Leaf)) {
    throw '缺少 .env.example，请核对交付文件。'
  }
  [IO.File]::Copy($EnvExamplePath, $EnvPath, $false)
}
notepad $EnvPath
if ($LASTEXITCODE -ne 0) { throw '无法打开 .env，请使用本地文本编辑器编辑。' }
```

将项目真实字段的空值填为实际密钥，按解析器支持的规则处理引号和特殊字符，使用 UTF-8 保存。`.env` 是明文，文件权限限制为实际运行 MCP 的账号及必要管理账号，不授予无关用户或 Everyone 读取权限；Windows 核对实际 NTFS ACL，其他平台设置等效的仅授权账号可读写权限。手册说明操作方式和检查方法，不要求默认管理员权限，也不在文档验证中操作用户真实文件。

启动时加载并校验配置；修改 `.env` 后重启实际 MCP 服务进程，包括 Host 管理的 stdio 子进程，默认不承诺热加载。仅修改文件时无需重设全局变量；兼容旧用户环境变量时，其变更不更新已经运行的父进程，按实际情况重启相关 Host/服务。由文件配置迁移后不擅自删除用户旧变量。

更新、版本切换、可选 ZIP 解压、环境清理及回滚保留成员已有 `.env`，不覆盖、不自动删除，也不拿新的 `.env.example` 替换它。新增字段通过说明和手工合并处理，新增必需字段缺失时明确提示；需要备份时使用私有位置及相同访问权限，备份不得进入 Git、ZIP、镜像或共享交付目录。

### 本地 Host 配置边界

本地 stdio 的后端 API 密钥由 MCP 自身读取项目 `.env`，Host 模板默认省略用于传递该密钥的 `env`，不把真实值塞进 Host JSON/TOML/YAML 或 CLI 参数。只有兼容环境回退等实际例外才核对 Host 继承/显式注入机制；共享模板保持无秘密，普通 JSON/YAML/TOML 和 `${VAR}` 不天然展开变量。

后端项目 `.env` 不会自动供 Host 读取；Host 自身的账号或认证设置由客户端管理，不混入后端凭据模板。本手册不提供网关令牌、HTTP 请求头或代理 URL 的配置与操作。

### 本地服务端安全

stdio 子进程采用其实际凭据机制，不强加 HTTP OAuth 流程。下游 API 凭据按业务授权单独配置，不把 Host 自身令牌当作下游凭据。核对启动账号、文件访问权限与子进程环境，避免加载无关凭据。

服务端必须落实工具输入校验和访问控制。文件能力限定可访问目录并防止路径越界；URL/下载能力按业务授权限制目标及重定向，防止访问未授权网络资源；外部内容不作为 Agent 指令。根据实际风险设置请求大小、并发、速率和超时限制，日志与返回内容脱敏。敏感操作的服务端授权不能只靠工具描述或客户端确认代替。

## 5. 五项本地 stdio 配置清单与 Host 适配

这是一份文档覆盖检查表，不要求项目实际支持所有 Host。目标 Host 清单由用户或项目要求确定；仅提供本地 stdio 接入。不支持 stdio 的 Host/插件标为不适用并说明依据，不用 HTTP 代理配置替代，也不虚构客户端能力。

| 项目 | 具体手册需提供的内容 |
|---|---|
| 本地 MCP JSON | 完整 `.mcp.json.example`，消费方、服务名、已验证的命令和参数。 |
| Codex TOML | 若支持 Codex，提供对应作用域的 stdio 服务表、命令和参数，保留无关配置。 |
| Claude Code / Claude-3p JSON | 按实际支持的客户端分别提供对应作用域、schema 与 stdio 配置，仅合并目标服务项。 |
| DSH profile YAML | 若支持 DSH，提供插件版本核对后的完整 stdio 补丁，说明原生 DSH（`web`）、App（`desktop`）和 Multica（`multica`）三种 profile 的适用性、合并与生效步骤。 |
| CLI 添加流程 | 为每个适用 CLI 提供 PowerShell 7、Bash、Zsh 的前置条件、注册/加载、作用域和验证步骤。三个 CLI 全部适用时覆盖九种组合。 |

本地 stdio 模板的 Python `command` 为 `uv`，Node.js 为 `node`；不使用 `cwd`。Python 通过 `--directory` 定位，Node.js 通过入口绝对路径定位；资源路径相对实际安装模块，不依赖 Host 的工作目录。

下面是参数化结构示例。填入项目值后，具体手册应逐字嵌入交付模板内容，不残留未解释的占位符。Python 和 Node.js 模板择一提供；同项目确有两个后端时分别命名和说明。

Python `.mcp.json.example`：

```json
{
  "mcpServers": {
    "<service_name>": {
      "command": "uv",
      "args": [
        "--directory",
        "D:\\MCP\\<project_name>",
        "run",
        "--no-sync",
        "python",
        "-m",
        "<module_name>"
      ]
    }
  }
}
```

Node.js `.mcp.json.example`：

```json
{
  "mcpServers": {
    "<service_name>": {
      "command": "node",
      "args": [
        "D:\\MCP\\<project_name>\\dist\\index.js"
      ]
    }
  }
}
```

提供复制本地模板的完整命令，保护已存在的用户配置。以下示例仅用于交付 `.mcp.json.example` 的项目，不复制不存在或不适用的文件：

```powershell
$ErrorActionPreference = 'Stop'
$Name = '.mcp.json'
if (Test-Path -LiteralPath $Name) { throw "配置已存在，请保留并手工合并：$Name" }
Copy-Item -LiteralPath "$Name.example" -Destination $Name -ErrorAction Stop
```

### 各 Host 的要求

- **Cherry Studio**：如果项目支持它，按已核对版本写清本地 stdio 设置入口、JSON 导入或字段映射、保存、启用、发现工具及绑定助手步骤。
- **Codex**：核对当前文档与安装版本的配置作用域、`config.toml` 字段及 CLI，按受支持的 stdio 进程配置填写命令和参数，不凭空添加传输字段。
- **Claude Code / Claude-3p**：分别核对配置作用域与 JSON schema；Claude Code 的 CLI 流程按其实际版本说明。本地后端默认自行加载项目 `.env`，不在 Host 配置里显式传递后端密钥；环境兼容例外按当前 Host 能力处理，不把某个客户端的行为推断成另一客户端的通用限制。私有配置值不进入交付模板。PowerShell 若遇 Claude Code 包装脚本吞掉 `--`，验证是否需使用相应 `.cmd` 入口，再写入该平台说明。
- **DSH**：阅读实际插件文档或源码，确认本地 stdio 支持、包标识、schema 与命名限制。不能凭空发明 `dsh mcp add`。不支持注册子命令时，给出经核对的 profile 补丁或加载命令；解释临时补丁与持久 profile 的区别，避免重复加载同一实例。

### Claude Code / Claude-3p：配置片段与合并

分别标明每个 JSON 示例对应的客户端与配置作用域，按其实际版本核对 schema；不能因为都使用 JSON 就直接互换整个配置文件。只给本地 stdio 配置片段及必要合并说明，不附配置文件目录或快捷打开命令。编辑前备份待修改文件，仅合并目标 MCP 服务项，保留已有服务、认证和无关设置；配置文件中的私有内容不复制到共享手册。

### DSH：原生 DSH、App 与 Multica

DSH 同时面向原生 DSH（`web`）、App（`desktop`）与 Multica（`multica`）。先核对各客户端和插件是否支持本地 stdio；相同配置只给一个共用 YAML 示例，分别合并到适用的独立 profile。存在差异时标明各 profile 的适用补丁，不支持项说明原因。

`cordis.patch.yml` 只给补丁与合并要求，不列目录或备份/快捷打开命令。编辑前保留私有备份；目标配置缺失时按对应客户端流程初始化，不擅自创建或覆盖。保留已有插件与无关补丁，只编辑目标 patch，不编辑共享 bundle。示例中如使用插件支持的 `!!js`，只做安全语法/结构验证，不求值真实环境凭据；普通 YAML 不自动执行 JavaScript 或展开环境变量。

目标文件仅为顶层空列表 `[]` 时，用该 profile 适用的完整 YAML 补丁列表替换；已有 patch 时，将新增补丁作为同一顶层列表的条目追加，保留原有插件、配置和缩进，不另加一个顶层 `[]`，不把完整列表嵌套成列表项。同一目标 MCP 实例已存在时就地合并该项，不重复追加；保留无关实例，为新增实例使用不冲突的唯一 ID。不能通过字符串拼接或通用 YAML 重写命令破坏注释、`!!js` 标签及已有配置；消费项目若提供自动合并脚本，须按实际 schema 验证保留行为，并在手册给出真实脚本命令。

保存后分别按适用客户端的实际版本说明激活 profile 或重启，检查补丁加载、工具发现与权限预设；不根据 Tools 可用就宣称 Prompts/Resources 也被桥接。备份含私有配置，仅留本机并保留访问限制，不提交或打包。文档编辑不授权运行这些修改用户配置的步骤。

### 三种 Shell 与验证口径

CLI 示例必须来自当前安装 CLI 的帮助或对应版本的官方文档。逐一说明 PowerShell 7、Bash、Zsh 的引号、路径、变量赋值、失败退出和秘密引用方式，不机械替换斜杠。示例必须是该项目适用的完整命令，不只写命令名。秘密引用要保留到支持解析它的层；不把 shell 展开的真实凭据写入共享命令、历史或模板。

若项目明确要求私有配置显式保存凭据，单独解释其范围并使用交互输入；不在文档验证时输入真实值。注册成功、配置解析成功、协议检查通过和业务调用成功分别报告，不能相互代替；仅对要求握手的协议版本报告握手结果。没有验证的 Shell/Host 标为未验证，不声称已通过九种组合。

### 协议与行为验收

检查前列明 SDK 锁定版本、MCP 协议版本、本地 stdio 传输、Host 版本及兼容范围。按实际版本使用 SDK 客户端或 Inspector 验证合法请求、版本兼容及 Tools、Prompts、Resources/模板的适用发现与访问；不支持的类别明确说明。旧协议要求时验证 `initialize` 和初始化通知；采用请求级元数据的协议时验证相应请求字段与版本处理。MCP `2026-07-28` 的传输模型不应无条件套用旧握手和会话要求；兼容旧 Host 时另按对应旧版协议测试，不要求所有项目追随最新协议升级。

把需求/验收标识关联到工具契约、测试及结果，覆盖正常调用、缺失/非法参数、边界值、权限失败和上游失败；有 `outputSchema` 时检查结构化返回满足声明。未知工具或请求结构错误与工具执行错误分别按 SDK/协议返回；可反馈给模型纠正的执行失败使用 `isError: true`，不返回成功标记掩盖失败。按所提供能力验证分页、超时、取消、并发和重试策略，写入/收费操作明确幂等边界，不能无条件重试。

单元/契约测试可使用受控替身验证业务映射和失败路径；协议集成检查应使用实际 SDK 客户端与本地 stdio 服务进程。真实 Host 联调和真实上游业务调用另行报告。收费、写入或需要私有凭据的调用按授权执行，缺少条件时标为未验证，不能用替身测试冒充实际业务成功。记录命令、测试环境、预期与实际结果、退出码和未覆盖项，证据中不得包含真实凭据。

配置加载是独立验收项。使用临时目录与无真实权限的测试值，验证：文件值覆盖不同的环境值；字段/文件缺失时按优先级兼容回退；字段为空、格式错误、重复、文件不可读/无法解析时明确失败；全部来源缺失的必需字段失败；Host 工作目录下的诱导 `.env` 不被读取；升级后成员文件内容与权限保留；错误与日志不泄露值。Windows 用户级回退与非 Windows 行为分别标明验证范围；不为测试创建真实用户级密钥或修改用户配置。

## 6. 格式与模板一致性

- JSON 使用严格语法：双引号、两空格缩进、无注释或尾逗号；每个属性、每个数组元素单独一行，非空嵌套结构展开，即使只有一个参数也不压成一行。
- YAML 使用空格、清晰层级和逐行参数；TOML 按实际作用域与字段类型编写。文档分别用 `json`、`yaml`、`toml` 代码块。
- 值与类型不因格式化而改变；所有可解析示例做语法检查。项目模板、手册内配置、CLI 注册效果必须对应同一服务、路径、传输和认证方式。
- 占位符只用于通用规范；项目最终手册中的运行命令填好项目固定值，仅保留明确要求用户提供的安装路径或凭据，并解释获取方式。

## 7. MANUAL 的十节结构

根目录创建 `MANUAL.md`；`README.md` 提供项目概述、最短可用安装路径及手册链接。细节在 MANUAL 维护，避免重复内容产生冲突。

1. **环境准备**：本地安装与使用者角色，Git、运行时/包管理器/SDK、MCP 协议、stdio 及 Host 兼容范围，GitHub 仓库访问、包源与网络要求，真实 `.env` 路径、字段/格式/优先级、权限及重启方式；无认证项目明确无需密钥。
2. **项目初始化**：写明实际 GitHub clone 地址、通过 CI 的发布标签及完整 SHA，clone 自动创建项目目录；分别给 Python 锁定运行安装、Node 源码安装及必要构建，检查失败和入口缺失。需要密钥时从空值 `.env.example` 安全创建私有文件，保留已有配置，不运行新项目脚手架或默认设置全局变量。
3. **可用性测试**：交付实际存在的本地检查脚本、完整命令、超时、退出码与预期输出。按所用协议版本验证初始化或请求元数据、兼容行为与适用能力发现；契约、行为验收及业务调用分别报告，注明副作用和未验证项。
4. **客户端配置**：对五项本地 stdio 清单逐项给出完整配置和操作步骤或不适用原因；每个适用 Host 的添加、保存、启用、发现与绑定流程都要完整。不附 Host 配置文件的目录或快捷打开命令，不提供 HTTP/代理接入分支。
5. **启动排障**：给与模板一致的 uv `--no-sync` 或绝对路径 node 命令，启动不拉代码、不安装或构建；解释 stdio、stderr 与配置失败。提供按发布版本更新、重装锁定依赖/构建、协议复验和回滚步骤，保留 `.env` 与私有数据。
6. **文件清单**：区分已跟踪源码/锁/CI 配置与本机生成的依赖和构建输出；列出真实 GitHub 仓库、Release/版本和运行入口。仅在明确提供 ZIP 时列白名单、打包命令及位置，不能把 ZIP 当作必需安装步骤。
7. **资源清单**：从当前源码和发现结果列出 Tools、Prompts、Resources/模板的名称、功能、类型、参数、必填项、默认值、枚举、返回结构和调用示例，并核对规范/契约；未实现需求记录差异，没有的类别写“无”。
8. **功能说明**：按实际使用流程给出输入准备、步骤、调用、输出、轮询/下载及边界，对应需求与验收标准；不沿用其他项目的能力名称、固定数量或把计划功能写成已可用。
9. **注意事项**：实际权限、文件路径语义、数据范围、费用、限额、格式、超时、取消、重试/幂等与联网边界，`.env` 明文权限及升级保留要求。说明 Host 启动账号的文件与进程访问权限。
10. **常见问题**：覆盖 GitHub 权限/地址/标签错误、命令不可用、包源/代理/TLS、锁不一致、缺少构建工具或入口/assets、SDK/协议/Host 不兼容、`.env` 错误、升级冲突、未重启、后端认证失败、超时和协议输出污染；不以删锁、强制覆盖、浮动升级或静默回退旧密钥作为通用解法。

## 8. 仓库内容与交付边界

| 对象/方式 | 必须可取得的内容 |
|---|---|
| GitHub 开发仓库 | 源码、元数据与锁、运行时/构建/测试配置、测试和安全示例、共享规范与契约、所选 Agent 约定、两份指南、`.gitignore`、`.github/workflows/`、README/MANUAL、运行 assets/许可证、无秘密的模板及实际检查脚本。 |
| 本地 Python 成员 clone | 仓库包含 `src/`、`pyproject.toml`、`uv.lock` 与构建后端输入；成员本机生成 `.venv`，安装声明的运行依赖，使用模块入口。 |
| 本地 Node.js 成员 clone | 仓库包含源码、`package.json`、锁、必要构建配置及 assets；成员按项目流程生成 `node_modules` 和实际 JS 入口，不假定未跟踪的 `dist/` 随仓库取得。 |
| 远程服务维护者 | CI/CD 构建的应用/镜像、部署配置及秘密引用、版本追溯、启动/停止、升级/回滚与日志/协议验证流程。 |
| 明确需要 ZIP 的用户 | 另按第 9 节白名单准备运行文件，不因此改变 GitHub 仓库的完整性或跟踪规则。 |

普通 clone 会取得已提交的开发文档、规范和测试；它们留在仓库是正常行为，不为模拟旧成员 ZIP 而从 Git 忽略。构建输出、依赖与测试报告由安装/CI 生成。实际运行脚本和全部输入必须能从目标版本取得，不能依赖开发机未提交文件。

`.gitignore` 的分类、例外及检查方式统一见 [工程指南第 5 节](../engineering/ENGINEERING-GUIDE.md#5-git-跟踪与-gitignore) 与 [根目录模板](../../.gitignore)：忽略缓存、测试结果和私有状态；跟踪测试源码、锁、CI、规范和空密钥示例。不整体忽略 `tests/`、`specs/`、共享 Agent 配置或正式维护的 `outputs/`。

忽略规则不会移除已跟踪文件或清除历史秘密；发现泄露按凭据处置流程报告并处理。Git 忽略、CI artifacts、镜像及可选 ZIP 的内容分别验收，不能用 `.gitignore` 代替发布检查。不同 Host 的私有配置可能含其他私人设置，不整体复制到仓库或发布物；共享模板只保留该项目无秘密的值。

## 9. 可选 ZIP 打包与验收

本节仅在用户明确需要 ZIP 分发时适用，不能替代第 3 节的默认 GitHub clone 流程；未采用 ZIP 的项目无需创建打包脚本或白名单。纯远程托管项目按 CI/CD 验证实际部署产物、秘密注入、升级/回滚和协议可用性，不机械套用 ZIP 排除规则。

成员 ZIP 排除开发用途的 `.agents/`、`.claude/`、`.codex/`、`.specify/`、`specs/`、任务数据库（如 `.beads/`）、约定文档、两份指南、开发期 `docs/`、`tests/`、`examples/`、`output/`、`outputs/`、`.git/`、`.venv/`、`node_modules/`、缓存、日志、真实 `.env`、秘密变体/备份与私有配置。必须的入口、assets、检查脚本和空密钥 `.env.example` 明确列入白名单；检查不能依赖被排除的开发依赖。此清单只控制 ZIP，不控制 Git 跟踪。

启用 ZIP 交付时，提供实际可运行的 `scripts/package-user.ps1`，从根目录执行：

```powershell
pwsh -NoProfile -File '.\scripts\package-user.ps1'
```

脚本根据 `user-files.json` 的明确相对文件路径列表生成 `output/<project_name>-<version>-user.zip`，版本取自元数据。ZIP 根部必须只有一个 `<project_name>/` 顶层目录，所有运行和初始化文件位于其中；直接解压到 `D:\MCP` 后自动形成 `D:\MCP\<project_name>\`。ZIP 文件名使用版本字段，顶层目录保持 `<project_name>` 原名，不额外追加 `<version>`、不使用服务名转换结果，不额外包一层开发目录或 ZIP 同名目录。所有示例使用项目名称占位符，具体项目按已确认的目录命名约定替换。

白名单仍列相对于项目根目录的文件路径，不预加顶层目录；打包时统一映射为 `<project_name>/<白名单相对路径>`，ZIP 条目使用 `/` 分隔。例如白名单中的 `README.md`、`scripts/smoke-test.ps1` 分别写入 `<project_name>/README.md`、`<project_name>/scripts/smoke-test.ps1`。源文件布局保持不变，不在源码根目录另建一层同名项目目录；若使用临时打包目录，按白名单复制到其中的 `<project_name>/` 后归档，防止遗漏或重复包层。

校验 `<project_name>` 是目标平台允许的单一目录名，拒绝空名、`.`、`..`、路径分隔符、盘符和绝对路径等非法值。对白名单和最终 ZIP 条目均检查缺失、重复、绝对路径、越界路径、逃逸的 reparse point 和禁入文件；即使白名单误列真实 `.env`、秘密副本或含非空密钥的 `.env.example` 也必须失败。不得静默覆盖旧发布包或把 `output/` 自身再次打包。

打包前完成应用构建，验证入口、assets、元数据与锁一致。使用能保留点开头模板文件的归档方式，例如 PowerShell 7 调用 .NET ZIP API，并显式保留 `<project_name>/` 前缀，不能只把该目录的内容放到 ZIP 根部。验收时同时检查唯一顶层目录及文件清单：ZIP 文件条目必须精确等于给每条白名单路径加上 `<project_name>/` 前缀后的集合；去掉这一个前缀后与原白名单精确比较，目录条目单独核对，不将目录条目混作文件。不能仅检查 ZIP 文件是否生成。

在空的临时安装父目录中解压验证，不预建项目目录；确认自动出现 `<project_name>/`，文件只位于该目录内，且不存在散落的顶层文件、额外的版本目录或重复包装层。随后从自动形成的项目目录执行成员初始化和可用性检查；不能通过手工新建、移动文件或更名目录来掩盖错误的 ZIP 结构。

核对 `.env.example` 只含实际字段和空密钥值、ZIP 不含真实 `.env`。在临时目录中模拟已有成员 `.env` 的升级，确认解压/升级流程保留原文件及访问权限；不能通过整目录清空、重建安装目录或覆盖模板来丢失成员配置。升级/回滚的验证使用无真实权限的测试值，不操作成员真实文件。

在独立临时目录、空专用缓存中验证成员联网安装，之后执行成员可用性检查；Node.js 不得从开发/祖先/global `node_modules` 或 `NODE_PATH` 偷用依赖。保护开发环境不被清理。网络或凭据缺失时准确记录失败或未验证项，不宣称通过。

默认发布按工程指南经 CI/CD 提供 GitHub 版本及相应安装资料；仅在明确需要时交付 ZIP，并将其关联到同一版本提交和验证结果。两份规范指南属于开发资料，不进入业务项目成员 ZIP。

## 10. 最终核对

- 三种核心约定短小且正文相同，仅标题和适用对象不同；按相对路径引用工程指南与本指南，工程和使用细则分别集中维护。
- 采用 spec-kit 时，相关原则、功能规范、方案、契约和任务已读取并保持一致；预期行为与实际状态有差异时如实记录，需求/验收项对应验证证据，任务状态与既有任务系统一致。
- 没有业务项目名称、专有密钥字段、固定能力数量、私人用户名或项目历史被当成通用事实；Host 产品名仅用于其适用集成分支。
- 参数表有明确来源；服务名符合约束且不重名，安装目录不随名称转换而改名。
- SDK、协议、本地 stdio 传输及 Host 版本分别明确；协议检查按对应版本验证，不统一强制旧握手；工具契约、错误语义、权限与适用的超时/取消/重试行为已验证或明确标为未验证。
- 成员从实际 GitHub 仓库获取通过 CI 的真实标签/提交；干净 clone 可完成锁定安装、必要构建及协议验证。日常启动复用环境，更新/回滚保留 `.env`。
- 测试源码、规范、锁、CI 配置、共享 Agent 配置与正式产物按用途纳入 Git；秘密、缓存及测试生成物排除，规则与模板经过正反例检查。
- PR 门禁、默认分支 CI、受控发布 CD 及适用部署流程在消费项目实际落地并验证；记录 CI、发布与安装证据，不能把规范要求写成已实施事实。
- 本地后端固定读取实际项目根目录 `.env`，逐字段文件优先、缺失才兼容进程/用户环境；空值、格式、重复、读取/解析错误不回退。优先级、Host 工作目录隔离和失败脱敏有验证证据。
- 仅交付空密钥 `.env.example`；真实 `.env` 与秘密备份不进入 Git/ZIP/镜像。首次配置、权限限制、服务重启和升级/回滚保留规则明确；后端文件不代替 Host 自身认证。
- 五项本地 stdio 配置及适用 CLI/Shell 组合的覆盖范围、不适用项明确；无 HTTP/代理 Host 模板及相关连接操作。Host 配置文件不附目录或快捷打开命令；示例解析通过与实际连接通过分别报告。
- 仓库内容、安装/构建步骤、入口、模板、版本与 README/MANUAL 一致；仅启用 ZIP 时验证唯一 `<project_name>/` 顶层目录、白名单及直接解压布局，远程部署产物按声明验收。所有发布物均无真实凭据或无关用户配置。
- 不执行文档中的示例来修改用户 Host 配置或调用收费业务，除非本次任务明确授权。

### 维护依据

以下官方资料用于核对工作流与协议要求，不替代消费项目锁定版本的文档；升级时重新核对兼容范围，不因链接更新而自动改变依赖或规范。

- [spec-kit 工作流](https://github.com/github/spec-kit)、[需求模板](https://github.com/github/spec-kit/blob/main/templates/spec-template.md)、[方案模板](https://github.com/github/spec-kit/blob/main/templates/plan-template.md)：区分需求、技术方案、任务和验收材料。
- [MCP 2026-07-28 传输模型](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports)：核对本地 stdio 与对应请求处理规则；支持旧版本的项目查阅其对应协议规范。
- [工具契约与错误处理](https://modelcontextprotocol.io/specification/2026-07-28/server/tools)、[安全实践](https://modelcontextprotocol.io/docs/2026-07-28/tutorials/security/security_best_practices)：工具输入/输出、执行失败与令牌信任边界。
- [Python SDK](https://github.com/modelcontextprotocol/python-sdk)、[TypeScript SDK](https://github.com/modelcontextprotocol/typescript-sdk)：核对锁定版本接口及迁移说明。
- [Windows 环境变量](https://learn.microsoft.com/windows/win32/procthread/environment-variables)：默认子进程继承父进程环境；显式环境块和 Host 行为可能改变继承。此机制用于兼容回退，不替代项目 `.env` 加载规则。
- [Git clone](https://git-scm.com/docs/git-clone)、[uv 锁定与同步](https://docs.astral.sh/uv/concepts/projects/sync/)、[npm ci](https://docs.npmjs.com/cli/v11/commands/npm-ci/)：源码获取、锁定安装与构建依赖；按消费项目锁定版本验证。
