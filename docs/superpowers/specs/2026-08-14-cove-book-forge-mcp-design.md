# cove-book-forge-mcp 设计规范

**日期：** 2026-08-14

**状态：** 已完成产品设计，等待书面规范最终审阅

**项目名称：** `cove-book-forge-mcp`

**建议许可：** MIT

## 1. 产品定义

`cove-book-forge-mcp` 是一个本地优先、无官方阅读界面的开源 MCP Server。它把 EPUB、PDF、纯文本、Markdown，或者外部共读系统提供的结构化章节内容，炼入 Obsidian 或炼成可复用的 Agent Skill。

它面向两类用户：

1. 没有现成共读系统的用户：启用可选本地书库，直接导入书籍，并使用 MCP Tools 或自行开发阅读界面。
2. 已有共读系统的用户：保留自己的书库和数据库，只向 MCP 提交标准化书籍信息与章节快照。

项目不提供官方阅读器 UI，不包含 Cove/栖渡人格、聊天、记忆或关系系统，也不规定第三方阅读界面的技术栈和交互形态。

### 1.1 核心能力

- 导入和解析 EPUB、带文本层的 PDF、TXT 与 Markdown。
- 检测扫描型 PDF 并明确要求 OCR，不生成空 Skill。
- 通过可选本地书库保存书籍、章节、阅读进度、划线、笔记、批注与读后感。
- 接收已有共读系统提供的外部书籍标识和章节快照。
- 将当前章节炼入 Obsidian。
- 将当前章节增量炼入一本书对应的稳定 Agent Skill。
- 一次性把整本书炼成完整 Skill。
- 对整本任务进行成本预检、后台排队、暂停、恢复、取消、断点续炼和失败章节重试。
- 支持 OpenAI-compatible、Anthropic 与自定义模型 Provider。
- 首次授权输出路径后，后续炼制可以自动写入 Obsidian 和已选择的 Agent Skill 目录。
- 通过 MCP stdio 和 Streamable HTTP 暴露 Tools 与 Resources。

### 1.2 非目标

v0.1 不包含：

- 官方网页、桌面或移动阅读器。
- Cove/栖渡人格、主动陪读、聊天记录或私人记忆。
- 云端账号、跨设备同步、多人协作或托管式 SaaS。
- 内置商业书籍、商业书籍生成的示例 Skill，或生成 Skill 的公共市场。
- 自动下载大型 OCR 模型。OCR 只作为可选外部能力。
- Notion、Logseq、向量数据库等额外输出；它们可在未来通过 `OutputTarget` 扩展。
- 自动合并用户对受管 Markdown 文件所做的复杂人工修改。
- 在生成 Skill 中生成或执行脚本、Hooks、MCP 配置或可执行文件。

## 2. 已确认的设计决策

- 产品形态为无界面的 Headless MCP，不包含官方阅读器。
- 采用全新独立开源内核，而不是给 Cove FastAPI 包一层 MCP，也不是长期维护 `book-to-skill` 的重型 Fork。
- 本地书库为可选能力。
- 外部系统可以通过稳定外部 ID 和 `ChapterSnapshot` 接入，无需迁移数据库。
- 每本书对应一个稳定、持续成长的 Agent Skill。
- 逐章炼制与整本炼制复用同一套分析缓存、注册表和输出结构。
- Obsidian 与 Agent Skill 复用同一份结构化 `ChapterAnalysis`，避免重复模型调用。
- 模型层内置 OpenAI-compatible 与 Anthropic，并公开自定义 Provider 接口。
- MCP Sampling 不作为核心模型路径；服务器直接使用用户配置的 Provider。
- 输出目录首次显式授权，之后可自动写入；不允许静默扩大授权范围。
- 整本炼制默认先显示章节数、预计调用次数、token 与可得的费用估算，再确认一次。
- 高级用户可以关闭整本炼制确认。
- 所有正式输出通过 staging、验证、安全扫描和原子发布完成。
- 仓库使用 MIT License，并在所有发布入口明确感谢 `book-to-skill` 原作者 Virgilio Jr.。

## 3. 总体架构

```text
cove-book-forge-mcp/
├── src/cove_book_forge/
│   ├── contracts/
│   ├── sources/
│   ├── extractors/
│   ├── library/
│   ├── providers/
│   ├── forge/
│   ├── outputs/
│   ├── jobs/
│   └── mcp_server/
├── tests/
├── examples/
├── docs/
├── LICENSE
├── ACKNOWLEDGEMENTS.md
├── THIRD_PARTY_NOTICES.md
└── README.md
```

### 3.1 模块职责

`contracts` 定义公开、版本化的数据契约，不包含数据库或框架实现。

`sources` 通过统一 `BookSource` 接口提供书籍和章节。首版实现 `ManagedLibrarySource` 与 `ExternalSnapshotSource`。

`extractors` 负责确定性文档解析。模型不参与 ZIP 路径处理、目录遍历或原始文件读取。

`library` 负责可选本地书库、规范化外部快照缓存、炼制注册表和任务持久化。

`providers` 定义 `ModelProvider`，实现 OpenAI-compatible、Anthropic 与自定义 Provider 装配。

`forge` 负责指纹、章节分析、章内归并、跨章归并、缓存、渲染输入和验证，不依赖 FastAPI、MCP SDK、Cove 数据库或固定输出目录。

`outputs` 实现 `ObsidianOutput` 与 `AgentSkillOutput`，负责受管文件、冲突检测和原子发布。

`jobs` 实现持久化状态机、并发限制、预检、暂停、恢复、取消和重试。

`mcp_server` 只负责协议适配、参数验证、结构化结果以及 stdio/Streamable HTTP 传输，不放置业务逻辑。

### 3.2 依赖方向

```text
MCP Server
    ↓
Application Services / Jobs
    ↓
Forge Core
    ↓
Contracts
```

可替换实现通过接口注入：

```text
BookSource       ← Managed Library / External Snapshot
ModelProvider    ← OpenAI-compatible / Anthropic / Custom
OutputTarget     ← Obsidian / Agent Skill
JobRepository    ← SQLite
```

禁止 `forge` 反向依赖 MCP SDK、FastAPI、Claude Code、Codex、Cove 数据库、固定 Vault 或用户主目录。

## 4. 公开数据契约

### 4.1 书籍身份

`BookRef` 使用内部 `book_id`。外部系统同时提供：

```json
{
  "source_system": "cove",
  "external_book_id": "book_abc123"
}
```

`source_system + external_book_id` 在服务器内唯一，并稳定映射到内部 `book_id` 和 Skill slug。

### 4.2 ChapterSnapshot

```json
{
  "source_system": "cove",
  "external_book_id": "book_abc123",
  "book": {
    "title": "书名",
    "author": "作者",
    "language": "zh-CN",
    "total_chapters": 12
  },
  "chapter": {
    "index": 2,
    "title": "第三章",
    "content": "章节正文",
    "source_locator": "epub:spine-3"
  },
  "highlights": [],
  "user_notes": [],
  "annotations": [],
  "reflections": []
}
```

所有数组项支持调用方提供稳定 ID。相同稳定 ID 使用 upsert 语义，避免重复同步。

开源契约不包含 `Moon`、`栖渡` 等人格字段。Cove 私有适配器负责将自己的数据映射为通用字段。

### 4.3 ChapterAnalysis

模型生成结果必须通过版本化 JSON Schema 验证，包含：

```text
core_idea
frameworks
concepts
mental_models
methods
anti_patterns
decision_rules
worked_examples
key_takeaways
highlight_insights
annotation_insights
topic_tags
evidence_refs
quality_warnings
```

`evidence_refs` 记录章节、段落、页码或来源定位，不复制长篇原文。`quality_warnings` 标记解析缺失、OCR 噪声、模型不确定或证据不足。

## 5. 配置与首次授权

用户通过本地命令初始化：

```bash
cove-book-forge init
```

配置使用操作系统合适的用户配置目录；数据目录通过 `platformdirs` 等跨平台机制解析，不把 macOS 路径写死在核心代码中。

```yaml
library:
  enabled: true

model:
  provider: openai-compatible
  base_url: https://api.deepseek.com
  model: deepseek-v4-flash
  api_key_env: DEEPSEEK_API_KEY
  max_concurrency: 2
  requests_per_minute: 20

outputs:
  obsidian:
    enabled: true
    vault_path: /absolute/path/to/vault
    notes_folder: Books
    cards_folder: Cards

  skills:
    enabled: true
    canonical_path: /absolute/path/to/agents/skills
    install_to:
      - codex
      - claude

full_book_forge:
  require_preflight_confirmation: true
```

配置只保存 API Key 的环境变量名，不保存真实 Key。密钥由环境变量、系统密钥管理器、容器 Secret 或调用方自定义 Provider 管理。

输出根目录必须在初始化时显式授权。后续炼制只能写入授权目录的直接或受控后代路径。

## 6. MCP 公共接口

### 6.1 环境检查

```text
book_forge_doctor
```

只读检查配置、Provider、解析依赖、Vault 权限、Skill 目录、路径冲突和不安全链接。

### 6.2 托管书库

```text
import_book
list_books
get_book
get_chapter
upsert_reading_note
update_reading_progress
```

`import_book` 支持复制原文件到托管书库，或只保存外部文件引用。引用失效时返回明确错误，不静默丢书。

### 6.3 外部系统同步

```text
upsert_external_book
upsert_chapter_snapshot
```

外部系统仍是事实来源。MCP 只保存规范化快照、内容指纹、分析缓存、任务状态和输出元数据，不要求复制对方全部业务数据库或原始书籍文件。

### 6.4 炼制

```text
plan_book_to_skill
forge_chapter_to_obsidian
forge_chapter_to_skill
forge_book_to_skill
get_book_skill_status
list_generated_skills
```

单章工具接受托管书库引用或内联 `ChapterSnapshot`，但一次调用必须且只能指定一种来源。

`plan_book_to_skill` 返回绑定以下内容的 `plan_id`：

- 书籍指纹；
- 待处理章节；
- Provider 与模型；
- 生成器与提示词版本；
- 预计输入 token、模型调用数和可得的费用范围。

计划默认 30 分钟内有效。书籍内容、模型配置、模板版本或输出配置发生变化时，计划立即失效并要求重新预检。无法获得准确价格时只报告 token 和调用次数，不编造金额。

`forge_book_to_skill` 默认要求有效 `plan_id` 与显式确认。关闭预检确认后，服务器可以内部生成计划并立即排队。

### 6.5 任务控制

```text
get_forge_job
list_forge_jobs
control_forge_job
retry_failed_chapters
```

`control_forge_job` 支持 `pause`、`resume` 与 `cancel`。取消只阻止尚未开始的工作，不删除已成功发布的内容，也不留下半写入输出。

### 6.6 Resources

```text
cove-book-forge://books
cove-book-forge://books/<book-id>
cove-book-forge://books/<book-id>/chapters/<index>
cove-book-forge://books/<book-id>/skill
cove-book-forge://jobs/<job-id>
cove-book-forge://skills/<skill-slug>
```

章节正文只在客户端明确读取相应 Resource 时返回，不自动注入 Agent 上下文。

### 6.7 长任务与 MCP Tasks

SQLite 内部任务表是唯一事实来源。所有长工具快速返回 `job_id`，不依赖连接持续存在。

客户端支持 MCP Tasks 扩展时，可以将内部 `ForgeJob` 映射为 MCP Task；不支持时仍通过 `job_id`、Tools 和 Resources 完成全部功能。

## 7. 本地书库与持久化

概念数据模型包括：

```text
books
chapters
reading_notes
external_sources
chapter_snapshots
chapter_analyses
forge_plans
forge_jobs
forge_job_chapters
book_skill_registry
output_manifests
```

原始书籍、解析产物、分析缓存和生成结果保存在应用数据目录。SQLite 只保存结构化元数据和适合查询的数据；大正文或产物可以使用受管文件并在数据库中记录哈希与路径。

关闭托管书库时仍保留最小的外部快照缓存、炼制注册表、任务表和输出清单，以支持增量更新、断点恢复和人工修改检测。

## 8. 文档解析

### 8.1 EPUB

优先依据 spine 与 navigation/TOC 建立章节顺序，清洗 XHTML 并保留标题、段落、列表、代码块、表格可读结构与可用脚注关系。拒绝 ZIP 路径穿越、异常压缩率、极端文件数量、嵌套压缩包和加密 EPUB。

### 8.2 PDF

在预检阶段区分文本型、技术型与扫描型 PDF。文本型使用快速文本层提取；技术型允许配置保留表格、代码和公式结构更好的可选解析器。

无文本层时返回 `OCR_REQUIRED`。核心包不自动下载 OCR 大模型，但文档提供外部 OCR 接入方式。

### 8.3 超长章节

超长章节不得截断。它按标题与自然段切为保持代码块和表格完整的子块；各子块结构化分析后进行章内归并，得到一份 `ChapterAnalysis`。

## 9. 炼制流水线

### 9.1 指纹

章节指纹由以下规范化内容计算 SHA-256：

```text
章节标题
章节正文
划线
用户笔记
批注
读后感
提炼配置
提示词模板版本
生成器版本
```

指纹未变化时不调用模型、不重写输出。Provider 变化不会自动使历史分析失效，除非用户选择重新生成或配置要求 Provider 纳入缓存策略；模板和 Schema 变化必须使相关缓存失效。

### 9.2 单章

```text
ChapterSnapshot
→ 规范化与指纹
→ 复用或生成 ChapterAnalysis
→ ObsidianRenderer / AgentSkillRenderer
→ 验证与原子发布
```

先炼 OB 再炼 Skill，或反向操作，必须复用同一份有效 `ChapterAnalysis`。

### 9.3 整本

```text
解析或读取所有章节
→ 成本预检
→ 跳过未变化章节
→ 有限并发生成章节分析
→ 分组归并
→ 最终书级归并
→ 验证与原子发布
```

整本模式不在每章完成后完整重算全书；它只在检查点做必要分组归并，全部章节完成后做最终归并。

已逐章炼过的章节可直接复用。完整 Skill 生成后，后续新增划线或批注只更新受影响章节和书级索引。

## 10. 模型 Provider

`ModelProvider` 至少提供：

```text
generate_json
generate_text
healthcheck
capabilities
usage
```

首版实现：

- `OpenAICompatibleProvider`：覆盖 OpenAI、DeepSeek、OpenRouter、Ollama、LM Studio、vLLM 及兼容端点。
- `AnthropicProvider`：原生 Claude API。
- `CustomProvider` 接口与最小示例。

Provider 能力声明包括 JSON Mode、上下文限制、最大输出、推理参数和速率限制。严格 JSON 不可用时，核心使用受控提示、解析与有限结构修复。结构修复有明确上限，不能无限重试。

服务器绝不在 Provider 失败后静默切换到另一个云服务。本地 Provider 失败时内容仍留在本地，除非用户显式修改配置。

## 11. Obsidian 输出

默认但可配置的结构：

```text
<Vault>/
├── Books/<书名>/
│   ├── <书名> MOC.md
│   └── Chapters/01 <章节名>.md
└── Cards/<概念卡片>.md
```

章节笔记包含来源、核心思想、框架、概念、方法、反模式、决策规则、用户划线、用户笔记、共读洞见和关联章节。

原子卡片一张只表达一个概念或规则，使用稳定 ID，链接来源章节与书籍 MOC。MOC 维护目录、已炼覆盖、核心框架、主题索引、卡片与尚未处理章节。

输出只更新带受管元数据的文件。发现人工修改时默认停止覆盖并返回 `EXTERNAL_MODIFICATION`；v0.1 提供保留人工版、生成新版本或显式覆盖的后续动作，但不自动合并复杂 Markdown 修改。

## 12. Agent Skill 输出

每本书使用稳定 slug 和目录：

```text
<skill-slug>/
├── SKILL.md
├── chapters/ch01-*.md
├── glossary.md
├── patterns.md
├── cheatsheet.md
└── .cove-book-forge.json
```

`SKILL.md` 包含名称、触发描述、书籍信息、覆盖情况、核心框架、调用方法和主题到章节索引。主文件保持紧凑，章节内容按需加载。

章节文件包含核心思想、框架、心智模型、方法、决策规则、反模式、示例、适用场景与来源定位。书级文件分别承担术语表、可复用模式和速查规则。

`.cove-book-forge.json` 保存书籍身份、外部 ID、slug、章节指纹、分析位置、生成器版本、受管文件哈希和更新时间，不进入正常 Agent 上下文。

唯一正式内容位于用户配置的 canonical 目录。Codex、Claude Code 与通用 Agent 入口优先使用符号链接；不支持符号链接的平台可以使用带清单和冲突检测的复制安装。任何现有非受管目标都不被覆盖。

## 13. 后台任务与并发

任务状态：

```text
queued
planning
parsing
analyzing
synthesizing
validating
publishing
paused
interrupted
completed
failed
cancelled
```

同一本书同一时间只允许一个发布协调器。整本任务可有限并发分析章节，但最终写入串行完成。不同书籍可以并行，并受全局 Provider 并发与速率限制约束。

相同 `idempotency_key` 或同书同章活动请求复用现有任务，避免重复收费。服务重启时 `queued` 保持排队，`running` 类状态转为 `interrupted`，可从最近检查点恢复。

## 14. 错误模型

所有错误使用：

```json
{
  "ok": false,
  "error": {
    "code": "MODEL_RATE_LIMITED",
    "message": "模型服务触发限流，任务将在稍后重试。",
    "retryable": true,
    "details": {}
  }
}
```

首版错误码：

```text
CONFIG_INVALID
MODEL_UNAVAILABLE
MODEL_AUTH_FAILED
MODEL_RATE_LIMITED
MODEL_OUTPUT_INVALID
SOURCE_NOT_FOUND
SOURCE_CHANGED
UNSUPPORTED_FORMAT
ENCRYPTED_DOCUMENT
OCR_REQUIRED
EXTRACTION_FAILED
EXTERNAL_BOOK_INCOMPLETE
OUTPUT_NOT_CONFIGURED
OUTPUT_PERMISSION_DENIED
EXTERNAL_MODIFICATION
INSTALL_CONFLICT
PATH_NOT_ALLOWED
JOB_CONFLICT
JOB_INTERRUPTED
JOB_CANCELLED
```

公开错误不包含 API Key、认证头、完整模型请求或正文。内部日志保留错误链，但必须脱敏。

## 15. 安全与隐私

### 15.1 文件系统

- 所有写入目标必须位于已授权根目录。
- 每次写入验证 realpath，拒绝 `../`、链接逃逸和压缩包路径穿越。
- 不把磁盘根、用户主目录或宽泛工作区作为递归操作目标。
- 不覆盖非受管文件、目录或符号链接。
- 输入文件实施大小、页数、文件数量、压缩率、解析时间和内存限制。

### 15.2 数据与日志

默认无遥测、无云同步、无远程日志。默认日志只记录内部 ID、章节序号、字符数、指纹前缀、Provider/模型、耗时、token 与错误码，不记录正文、私人笔记、完整批注、Key 或认证头。

记录完整模型请求的调试模式必须显式开启并显示隐私警告。

### 15.3 提示注入与生成 Skill

书籍内容是不可信输入。提示必须把系统指令、用户配置与来源内容分隔。书中要求忽略指令、执行工具或读取密钥的文本只作为书籍内容，不转化为 Skill 行为。

生成 Skill：

- frontmatter 只允许受控字段；
- 默认没有 `allowed-tools`；
- 不包含脚本、Hook、MCP 配置或可执行文件；
- 不允许指向目录外的链接；
- 不允许来源内容决定输出路径；
- 安全扫描失败时拒绝发布。

### 15.4 Streamable HTTP

默认只绑定 loopback。非 loopback 部署必须显式配置认证、允许的 Origin/Host、TLS 终止方案和请求大小限制；未配置认证时拒绝启动远程监听。

## 16. 原子发布与恢复

所有正式输出遵循：

```text
生成 staging
→ 验证结构和 Schema
→ 安全扫描
→ 检查人工修改
→ 计算受管哈希
→ 保留最近成功备份
→ 原子切换
→ 更新安装入口
```

失败时正式版本不变，staging 安全清理，最近成功备份保留，任务记录可重试错误。默认只保留最近一个成功备份，用户可以配置更高保留数。

## 17. 测试策略

### 17.1 单元测试

覆盖契约、路径、链接逃逸、指纹、slug、缓存、模型响应、Provider 配置、错误映射、哈希、任务状态、成本估算与计划失效。

### 17.2 解析器测试

使用合法可分发 fixtures：标准/无目录/嵌套目录 EPUB，文本/技术/扫描/加密/损坏 PDF，超长章节、表格与代码块。测试仓库不包含商业书籍。

### 17.3 Golden Tests

使用固定输入和 Fake Provider 输出，验证 Obsidian 章节笔记、卡片、MOC、`SKILL.md`、章节文件、术语表、模式、速查表与清单。

### 17.4 集成测试

使用临时目录、临时 SQLite 与 Fake Provider，覆盖导入、外部快照、两种单章输出、共享分析缓存、整本炼制、暂停恢复、服务重启、人工修改冲突、原子回滚和安装冲突。

### 17.5 MCP 契约测试

覆盖 stdio、Streamable HTTP、Tool Schema、Resource URI、结构化错误、长任务 `job_id` 和可选 MCP Tasks 映射。

### 17.6 安全测试

覆盖路径穿越、ZIP Slip、ZIP Bomb 限制、链接逃逸、恶意 frontmatter、来源提示注入、Secret 脱敏、未授权 Vault、非受管覆盖和取消时半写入。

### 17.7 Cove 私有回归

在 Cove 私有仓库验证“炼入 OB”“炼成 Skill”、`Skill x/y`、刷新恢复、不新增隐藏聊天消息和私人字段隔离。

## 18. v0.1 验收标准

1. 可安装并通过 `book_forge_doctor`。
2. 可导入 EPUB 和带文本层 PDF。
3. 扫描型 PDF 返回 `OCR_REQUIRED`。
4. 可关闭托管书库并使用外部章节快照。
5. DeepSeek 等 OpenAI-compatible 服务可配置接入。
6. Anthropic Provider 可接入。
7. 有自定义 Provider 最小示例。
8. 单章可炼入 Obsidian。
9. 单章可炼成 Skill。
10. 两种输出复用章节分析缓存。
11. 一本书可一次性完整炼成 Skill。
12. 整本任务有预检、暂停、恢复、取消和失败重试。
13. 未变化章节不重复调用模型。
14. 人工修改不被静默覆盖。
15. 发布失败不破坏上一个可用版本。
16. Codex、Claude Code 和通用 Agent 可发现生成 Skill。
17. 核心没有 UI 依赖。
18. 测试仓库不包含受版权保护书籍。
19. README 和首个 Release 明确感谢 `book-to-skill` 原作者 Virgilio Jr.。
20. `ACKNOWLEDGEMENTS.md` 和 `THIRD_PARTY_NOTICES.md` 完整保留上游许可与复用说明。

## 19. 上游致谢与版权边界

README 必须包含：

```markdown
## Acknowledgements

This project is inspired by and builds upon ideas and tooling from
[book-to-skill](https://github.com/virgiliojr94/book-to-skill),
created by Virgilio Jr.

We are grateful for the project's document extraction work,
Agent Skill structure, and open-source contribution.
```

仓库还必须：

- 使用 `ACKNOWLEDGEMENTS.md` 说明思想与工具来源；
- 使用 `THIRD_PARTY_NOTICES.md` 收录实际复用部分和对应 MIT 许可；
- 对直接复制或修改的上游源文件保留版权和许可头；
- 在首个 Changelog 与 GitHub Release 中再次感谢原作者并附仓库链接；
- 不暗示本文档解析和 Agent Skill 结构全部为本项目原创；
- 不公开由受版权保护书籍生成的 Skill；
- 示例只使用原创、公共领域或明确开放许可的内容。

上游项目：<https://github.com/virgiliojr94/book-to-skill>

## 20. Cove 迁移边界

Cove 私有仓库实现：

```text
CoveChapterSnapshotAdapter
CoveModelProvider
CoveOutputConfiguration
```

迁移步骤：

1. 独立 Core 与契约稳定。
2. MCP Tools、存储和输出通过测试。
3. Cove 将现有书籍、章节、划线、批注和读后感映射为 `ChapterSnapshot`。
4. Cove 的“炼入 OB”“炼成 Skill”和状态查询改为调用 MCP。
5. 完成私有回归后，才移除 Cove 中重复的旧实现。

迁移期间旧实现保持可用，避免开源工作破坏现有共读体验。

## 21. 实施与提交阶段

每个阶段通过相应测试后独立提交：

1. 项目骨架、MIT 许可、致谢和第三方声明。
2. 数据契约、配置和错误模型。
3. SQLite 存储、外部身份映射和任务状态机。
4. EPUB/PDF/TXT/Markdown 解析与安全限制。
5. OpenAI-compatible、Anthropic 与自定义 Provider 接口。
6. 章节分析、长章切分、指纹与缓存。
7. Obsidian 输出、受管文件与冲突检测。
8. Agent Skill 输出、扫描、原子发布与安装。
9. 整本炼制、成本预检、分组归并和断点恢复。
10. MCP Tools、Resources、stdio 与 Streamable HTTP。
11. 安全加固、故障注入和跨平台验证。
12. README、示例、安装文档、Changelog 和 v0.1 发布检查。
13. Cove 私有适配、回归验证和旧实现迁移。

## 22. 完成定义

v0.1 的“完成”要求：

- 本文所有验收标准通过自动化或记录清晰的人工验证；
- 测试、静态检查和包构建通过；
- 没有真实 API Key、私人书籍、私人笔记、Cove 数据或绝对私人路径进入仓库；
- 安装与卸载说明可由全新环境复现；
- README 能让没有 Cove 的用户理解和使用项目；
- 外部系统示例能展示如何提交 `ChapterSnapshot`；
- Git 历史由可审阅的小步提交组成；
- 开源致谢、第三方许可和版权边界完整；
- Cove 切换到 MCP 后，原有共读炼制体验不退化。
