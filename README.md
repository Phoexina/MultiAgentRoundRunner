# MultiAgentCycleRunner

多 Agent 回合制循环执行框架，支持用户随时介入。将多个 AI Agent（如 Codex CLI、Claude Code）组织为可配置的 Pipeline，自动循环执行任务，并通过 Web 界面实时监控每个 Agent 的运行过程。

---

## 目录

1. [系统概述](#1-系统概述)
2. [架构实现](#2-架构实现)
3. [数据存储](#3-数据存储)
4. [WebSocket 接口](#4-websocket-接口)
5. [HTTP API 接口](#5-http-api-接口)
6. [核心功能实现](#6-核心功能实现)
7. [配置参考](#7-配置参考)
8. [快速开始](#8-快速开始)

---

## 1. 系统概述

### 1.1 项目定位

MultiAgentCycleRunner 是一个**回合制多 Agent 自动化调度框架**，核心模式是：

- 将多个 AI Agent 配置为顺序执行的 **Pipeline**（如：Codex 决策 → Claude 执行）
- 以 **Round N-M** 为单位循环迭代，每轮可自动推进或等待用户确认
- 用户可**随时**向正在运行的 Agent 发送消息，消息会在下一步骤的提示词中注入
- 提供 Web 界面实时监控每个 Agent 的流式输出、工具调用和执行状态

与传统"聊天机器人"的区别：用户不是主导对话的一方，而是**监督者/介入者**——系统自动运行，用户在需要时干预。

### 1.2 整体架构

```
┌─────────────────────────────────────────────────────┐
│                   浏览器 (Web UI)                     │
│  ConversationSidebar │ ChatUI │ PipelinePanel        │
└────────────────┬────────────────────────────────────┘
                 │ WebSocket (ws://host:port/ws)
┌────────────────▼────────────────────────────────────┐
│                  ChatServer (server.py)               │
│  HTTP 静态服务 + WebSocket 消息路由                   │
└────┬──────────────┬──────────────────────────────────┘
     │              │
     ▼              ▼
  Database      RoundController (round_controller.py)
  (db.py)         │
  SQLite          ├── _build_prompt()   提示词构建
                  ├── run_loop()        回合调度
                  └── ProcessManager (process_manager.py)
                        ├── AgentProcess: Claude CLI 子进程
                        └── AgentProcess: Codex CLI 子进程
                              │
                        stream_parser.py  实时流解析
```

### 1.3 技术栈

| 层 | 技术 |
|---|---|
| 后端语言 | Python 3.11+，全异步（asyncio） |
| WebSocket | websockets（legacy server API） |
| 数据库 | SQLite，aiosqlite 异步驱动 |
| 配置 | YAML，pyyaml |
| 前端 | 纯 HTML / CSS / JavaScript，无构建工具 |
| Agent CLI | Claude Code（`claude`）、Codex CLI（`codex`） |

---

## 2. 架构实现

### 2.1 进程模型

系统启动后是一个单 Python 进程，内部全异步驱动。Agent 以**子进程**方式启动：

```
Python 主进程 (asyncio event loop)
├── WebSocket Server Task
├── RoundController.run_loop Task
│     └── ProcessManager._active_process
│           ├── Claude CLI 子进程 (asyncio.subprocess)
│           │     stdout → stream_parser.parse_ndjson → 事件流
│           │     stderr → stream_parser.read_lines → 错误流
│           └── Codex CLI 子进程 (同上)
└── _status_task (每 2s 广播进程状态)
```

同一时刻只有一个 Agent 子进程在运行（`ProcessManager` 持有 `_active_process` 单例）。

**Windows 特殊处理**：使用 `asyncio.WindowsProactorEventLoopPolicy` 支持子进程，终止时通过 `taskkill /F /T /PID` 递归杀死整个进程树（否则 cmd.exe shell 退出但子进程仍在运行）。

### 2.2 核心模块职责

#### `main.py` — 启动入口

按依赖顺序初始化各组件：Config → Database → ProcessManager → RoundController → ChatServer，然后将 `server.broadcast` 注入 `RoundController` 作为广播回调，启动 WebSocket 服务器。

#### `config.py` — 配置加载

从 `config.yaml` 加载配置，填充 `Config` dataclass。支持运行时通过 `save_config()` 将修改持久化回文件。模块级 `get_config()` 提供缓存单例。

#### `server.py` — WebSocket 服务器

同时承担两个职责：
- **HTTP**：通过 `process_request` hook 拦截 HTTP 请求，提供静态文件（`frontend/`）和 REST API（`/api/...`），非 HTTP 请求则放行走 WebSocket 升级
- **WebSocket**：管理 `clients` 集合，接收客户端消息并路由到对应处理函数，通过 `broadcast()` 向所有客户端推送事件

#### `round_controller.py` — 流程调度核心

系统最核心的模块，负责：
- 维护 `RoundData`（当前 round/step 编号、pipeline 步骤、resume_id、user_inputs）
- `run_loop()` 驱动 Pipeline 逐步执行，自动推进 Round N-M
- `_build_prompt()` 拼装每步的提示词（注入历史、用户输入）
- 提供 `start` / `stop` / `continue_next` / `retry` / `resume_session` 控制接口
- API Error 检测与自动重试（等待 60s 后 resume）

#### `process_manager.py` — Agent 子进程生命周期

`AgentProcess` 封装单个 Agent 子进程：
- `_build_claude_command()` / `_build_codex_command()`：构建 CLI 命令
- `start()`：启动子进程，Codex 通过 stdin 传入提示词
- `stream_output()`：合并 stdout/stderr 为统一事件队列，逐条 yield `AgentOutput`
- `stop()`：触发进程树终止

`ProcessManager` 是对外接口，持有当前活跃进程，`start_agent()` 为异步生成器，在流结束后补发 `done` 事件。

#### `stream_parser.py` — 运行时流解析

- `parse_ndjson(stream)`：chunk 读取 stdout，逐行解析 JSON，解析失败时 yield `__parseError` 对象
- `read_lines(stream)`：逐行读取 stderr，返回原始字符串

#### `session_reader.py` — Session JSONL 文件读取

进程结束后从磁盘读取 Agent 写下的 JSONL 文件：
- Claude：`~/.claude/projects/**/{session_id}.jsonl`
- Codex：`~/.codex/sessions/**/*{thread_id}*.jsonl`

提供 `get_session_messages()`（结构化消息列表，供前端"查看运行日志"）和 `extract_session_summary()`（提取最后一条 assistant 消息，用于填充被中断 session 的 chat 摘要）。

#### `db.py` — 数据持久化

基于 aiosqlite 的异步 SQLite 封装，所有写操作通过 `_lock`（`asyncio.Lock`）串行化，防止并发写冲突。

### 2.3 前端模块职责

前端为纯 JS，无框架，分为四个类：

#### `WSClient`

管理 WebSocket 连接与断线重连（指数退避，最大 10s）。重连后自动重新发送 `select_conversation` 恢复上下文。

#### `ConversationSidebar`

左侧边栏，管理：
- 会话列表（切换/新建/重命名/删除，通过弹窗 Modal）
- Session 列表（显示 Round N-M、Agent 类型、运行状态徽章）
- 点击 Session → 触发 `ChatUI.openSessionFromSidebar()`
- Resume Modal：输入新提示词后发送 `resume_session` 消息

#### `PipelinePanel`

右侧面板，可视化编辑 Pipeline：
- 每个步骤可选 Agent 类型（Codex/Claude）并填写提示词模板
- 模板为空时显示 `config.yaml` 默认模板作为 placeholder
- 保存时发送 `save_pipeline` 消息

#### `ChatUI`

中间主区域，双视图切换：
- **Chat 视图**：显示每轮摘要气泡（user / codex / claude / system），底部输入框发送用户消息，顶部控制栏（开始/继续/重试/停止）
- **Session 视图**：实时显示单个 Session 的流式日志，支持 text_delta 渐进合并，点击"返回"退回 Chat 视图

#### `App`

消息路由总线，将 WebSocket 消息分发给对应组件，并维护组件间的状态同步（如 `sidebar.sessionStatus` 同时更新 sidebar 和 session view header）。

---

## 3. 数据存储

### 3.1 SQLite 数据库结构

数据库路径由 `config.yaml` 的 `database.path` 配置，默认 `./data/sessions.db`。

#### `conversations` 表

每个"项目"对应一条记录：

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | INTEGER PK | 自增主键 |
| `name` | TEXT | 项目名称 |
| `created_at` | DATETIME | 创建时间 |
| `working_dir` | TEXT | Agent 工作目录（可为 NULL） |

#### `pipeline_steps` 表

每个项目的 Pipeline 步骤配置：

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | INTEGER PK | 自增主键 |
| `conversation_id` | INTEGER | 所属项目 |
| `step_order` | INTEGER | 步骤顺序（0-based） |
| `agent_type` | TEXT | `'codex'` 或 `'claude'` |
| `prompt_template` | TEXT | 自定义提示词模板（NULL 时用全局默认） |

#### `sessions` 表

系统唯一的主数据表，同时存储 Agent 执行记录和用户消息：

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | INTEGER PK | 自增主键，也作为前端 session_id |
| `conv_id` | INTEGER | 所属项目 |
| `round_index` | INTEGER | Round 编号（N，1-based） |
| `step_index` | INTEGER | Step 编号（M，1-based） |
| `agent_type` | TEXT | `'codex'` / `'claude'` / `'user'` |
| `session_id` | TEXT | CLI session UUID（用于 `--resume`，Agent 启动后异步写入） |
| `chat` | TEXT | 摘要文本；用户消息时为消息内容；被中断时为 NULL |
| `created_at` | DATETIME | 创建时间 |

**设计要点**：`agent_type='user'` 的行代表用户消息，与 Agent 执行记录共存于同一张表，使对话历史查询（`get_chat_history`）可以按时间顺序返回完整上下文，无需 JOIN。

### 3.2 Session JSONL 文件

Agent CLI 在运行时将完整对话写入本地 JSONL 文件（每行一个 JSON 事件）。

**Claude** 文件路径：`~/.claude/projects/{encoded_dir}/{session_id}.jsonl`  
每行事件类型：`system`（初始化）/ `user` / `assistant` / `result`（结束，含 `is_error`）

**Codex** 文件路径：`~/.codex/sessions/YYYY/MM/DD/rollout-{datetime}-{thread_id}.jsonl`  
每行事件类型：`response_item`，payload 类型为 `message` / `function_call` / `function_call_output`

两种格式由 `session_reader.py` 分别解析，统一转换为：
```json
{ "role": "user|assistant|tool", "type": "text|tool_use|tool_result|thinking", "content": "...", "metadata": {} }
```

### 3.3 配置文件 `config.yaml`

```yaml
server:
  host: 127.0.0.1
  port: 8765

database:
  path: ./data/sessions.db

agents:
  codex:
    command: codex           # CLI 可执行文件名
    working_dir: /path/to/project
  claude:
    command: claude
    working_dir: /path/to/project

workflow:
  max_rounds: 0              # 0 = 无限循环
  auto_continue_on_error: false
  no_progress_max_rounds: 3

prompts:
  codex_template: "..."      # Codex 全局默认提示词模板
  claude_template: "..."     # Claude 全局默认提示词模板
```

---

## 4. WebSocket 接口

连接地址：`ws://{host}:{port}/ws`

### 4.1 客户端 → 服务端

#### `user_message` — 发送用户消息

```json
{ "type": "user_message", "content": "消息内容", "conversation_id": 1 }
```

消息保存到 DB，广播给所有客户端。若当前有 Agent 正在运行，消息同时进入 `user_inputs`，下一步骤提示词的 `{user_input}` 会注入该内容。

#### `control` — 流程控制

```json
{ "type": "control", "action": "start|stop|stop_after_current|continue|retry", "conversation_id": 1 }
```

| action | 说明 |
|---|---|
| `start` | 从 Round 1-1 开始全新运行 |
| `stop` | 立即终止当前子进程，状态回 idle |
| `stop_after_current` | 当前步骤完成后停止，不推进下一步 |
| `continue` | 从上次停止处继续（推进到下一步骤） |
| `retry` | 原位重试当前步骤（不推进） |

#### `select_conversation` — 切换项目

```json
{ "type": "select_conversation", "id": 1 }
```

服务端返回 `conversation_selected`，包含历史消息、Pipeline 配置、Session 列表。

#### `create_conversation` / `rename_conversation` / `delete_conversation`

```json
{ "type": "create_conversation", "name": "新项目" }
{ "type": "rename_conversation", "id": 1, "name": "新名称" }
{ "type": "delete_conversation", "id": 1 }
```

#### `save_pipeline` — 保存 Pipeline 配置

```json
{
  "type": "save_pipeline",
  "conversation_id": 1,
  "working_dir": "/path/to/project",
  "steps": [
    { "agent_type": "codex", "prompt_template": "..." },
    { "agent_type": "claude", "prompt_template": null }
  ]
}
```

`prompt_template` 为 `null` 时运行时使用 `config.yaml` 全局默认模板。

#### `resume_session` — 恢复历史 Session

```json
{ "type": "resume_session", "session_id": 42, "user_input": "继续" }
```

以指定 Session 的 CLI session ID 为起点，使用 `--resume` 继续运行，不创建新的 Pipeline 步骤位置。

#### `get_sessions` / `delete_session`

```json
{ "type": "get_sessions", "conversation_id": 1 }
{ "type": "delete_session", "id": 42 }
```

### 4.2 服务端 → 客户端

#### `conversations_list` — 初始化时推送

```json
{
  "type": "conversations_list",
  "conversations": [...],
  "default_codex_template": "...",
  "default_claude_template": "..."
}
```

#### `conversation_selected` — 切换项目响应

```json
{
  "type": "conversation_selected",
  "id": 1, "name": "项目名",
  "working_dir": "/path",
  "history": [...],
  "pipeline": [...],
  "sessions": [...],
  "round_number": 3, "step_index": 1, "total_steps": 2
}
```

#### `agent_status` — Agent 状态变化

```json
{
  "type": "agent_status",
  "state": "running|idle",
  "round": 3, "step_index": 1, "total_steps": 2,
  "agent": "claude"
}
```

前端据此更新顶部状态栏和按钮可用状态。

#### `session_created` — 新 Session 开始

```json
{
  "type": "session_created",
  "session": { "id": 42, "agent_type": "claude", "round_index": 3, "step_index": 1, ... }
}
```

#### `session_log` — 实时 NDJSON 事件（每条 Agent 输出）

```json
{
  "type": "session_log",
  "session_id": 42,
  "stream": "event|stderr",
  "content": "{...原始 JSON 字符串...}",
  "event_type": "stream_event|assistant|result|done|...",
  "timestamp": "2026-04-17T12:00:00"
}
```

前端 Session 视图监听此消息实现实时日志流。

#### `session_status` — 进程运行状态（每 2s）

```json
{
  "type": "session_status",
  "session_id": 42,
  "pid": 12345,
  "duration": 23.4,
  "idle": 1.2
}
```

#### `chat_message` — 轮次摘要气泡

```json
{
  "type": "chat_message",
  "role": "user|codex|claude|system",
  "content": "摘要文本",
  "round_index": 3, "step_index": 1,
  "session_id": 42,
  "conversation_id": 1,
  "timestamp": "..."
}
```

每个 Agent 步骤完成后推送一次，`content` 为 `[SUMMARY]...[/SUMMARY]` 提取的摘要。

#### `system` — 系统提示

```json
{ "type": "system", "content": "API 错误，等待 60 秒后继续会话..." }
```

---

## 5. HTTP API 接口

服务器同时在 WebSocket 端口上提供 HTTP 服务。

| 端点 | 说明 |
|---|---|
| `GET /` | 主界面 `index.html` |
| `GET /logs` | 日志页面 `logs.html` |
| `GET /static/*` | 静态资源（JS/CSS） |
| `GET /api/sessions/{id}` | 查询 Session 元数据 |
| `GET /api/sessions/{id}/messages` | 读取 Session JSONL 结构化消息列表 |
| `GET /api/conversations/{id}` | 查询项目信息 |
| `GET /api/conversations/{id}/pipeline` | 查询 Pipeline 步骤配置 |
| `GET /api/conversations/{id}/history` | 查询完整对话历史（含用户消息） |
| `GET /api/conversations/{id}/sessions` | 查询 Session 列表 |

#### `GET /api/sessions/{id}/messages` 响应示例

```json
{
  "session": { "id": 42, "agent_type": "claude", ... },
  "source": "file|db_only|not_found",
  "path": "/home/user/.claude/projects/.../abc.jsonl",
  "messages": [
    { "role": "user", "type": "text", "content": "提示词内容" },
    { "role": "assistant", "type": "tool_use", "content": "mcp_tool", "metadata": { "tool_name": "...", "tool_input": {} } },
    { "role": "tool", "type": "tool_result", "content": "工具返回内容" },
    { "role": "assistant", "type": "text", "content": "[SUMMARY]本轮总结[/SUMMARY]" }
  ]
}
```

`source` 字段含义：
- `file`：从 JSONL 文件成功读取
- `db_only`：session_id 尚未写入（Agent 刚启动）
- `not_found`：JSONL 文件不存在（已删除或跨机器）

---

## 6. 核心功能实现

### 6.1 Round N-M 流程调度

**Round N-M** 表示第 N 轮第 M 步：
- N（round_index）：每当所有步骤跑完一遍，N 自动 +1
- M（step_index）：当前执行的是 Pipeline 中第几个步骤，1-based

```
Round 1-1 (Codex) → Round 1-2 (Claude)
→ Round 2-1 (Codex) → Round 2-2 (Claude)
→ ...（直到 max_rounds 或用户停止）
```

`run_loop()` 核心流程：

```
while True:
    step = _current_step()
    api_error = await _run_step(step)     # 执行当前步骤
    if api_error:
        等待 60s → await _run_step(step)  # API Error 自动重试
    推进 step_index（到头则 round_index++，step_index 归 1）
```

**重要约束**：Round 编号只在**新 session 创建时**变化，`stop` / `retry` 不改变 Round 显示。

### 6.2 提示词构建

`_build_prompt()` 在每步执行前调用，将模板变量替换为运行时数据：

| 变量 | 内容 |
|---|---|
| `{round_number}` | 当前 round_index |
| `{chat_history}` | 最近 3 个 Agent Session 窗口内的所有消息（含用户消息），oldest-first |
| `{user_input}` | 用户消息，见下方优先级 |
| `{codex_summary}` / `{last_summary}` | 历史中最后一条非 NULL 的 chat 摘要 |

**`{user_input}` 来源优先级**：
1. `current_round.user_inputs`（当前轮次运行期间通过 WebSocket 发来的消息，用完即清）
2. 若为空，查 DB `get_latest_user_message()` 取最近一条 `agent_type='user'` 的记录

**Resume 时的历史注入策略**：使用 `--resume` 继续 Session 时，Agent 已有完整的对话记忆，`_build_prompt` 跳过历史注入（`rows = []`），只注入 `user_input`，避免上下文重复。

### 6.3 Agent 子进程管理

**Claude CLI** 启动命令：
```
claude -p --output-format stream-json --include-partial-messages
       --verbose --permission-mode bypassPermissions [--resume {session_id}]
```
提示词通过 stdin 传入，输出为 NDJSON 流（每行一个事件）。

**Codex CLI** 启动命令：
```
codex exec [resume {thread_id}] --skip-git-repo-check
      -s danger-full-access --json --config approval_policy="on-request" -- -
```
提示词同样通过 stdin 传入（`-` 表示从 stdin 读取）。

**stdout 合并队列**：`stream_output()` 内部用 `asyncio.Queue` 将 stdout（NDJSON 解析后）和 stderr（原始行）合并为单一事件流，保证顺序性和无锁竞争。

### 6.4 Session 恢复机制

系统提供三种"继续"方式，均复用 `_run_step()` 的 `--resume` 路径：

| 方式 | 场景 | resume_id |
|---|---|---|
| `resume_session(session_id)` | 从历史任意 Session 恢复 | 该 Session 的 CLI session UUID |
| `retry()` | 原位重跑当前步骤 | None（清除，创建全新 session） |
| `continue_next()` | 推进到下一步骤 | None（下一步骤正常运行） |
| **API Error 自动重试** | 检测到 `result.is_error` | 本次运行的 resume_id（已在运行中写入） |

`--resume` 关键路径：
1. Agent 启动时 emit `system(subtype=init)` 事件，携带 `session_id`（UUID）
2. `_run_step` 捕获并写入 `current_round.resume_id` 和 DB `sessions.session_id`
3. 下次调用 `_run_step` 时若 `resume_id` 非空，CLI 携带 `--resume {uuid}` 启动

### 6.5 API Error 自动重试

检测逻辑：Claude CLI 在 Session JSONL 末尾写入 `result` 事件，通过 NDJSON 流透传到 `_run_step`：

```python
elif out.event_type == 'result':
    data = json.loads(out.content)
    if data.get('is_error'):
        api_error = True
```

`run_loop` 收到 `api_error=True` 后：
1. 广播系统消息提示用户
2. `asyncio.sleep(60)`
3. 再次调用 `_run_step(step)`，此时 `resume_id` 已存在，自动携带 `--resume`

### 6.6 摘要提取

Agent 被要求在输出末尾写入 `[SUMMARY]...[/SUMMARY]` 标记，系统从中提取摘要写入 `sessions.chat`。

**兜底策略**：若 `[SUMMARY]` 不存在（如 Session 被强制终止），`_run_step` 在 `finally` 块中调用 `session_reader.extract_session_summary()`，从磁盘 JSONL 文件逆序查找最后一条 assistant 消息作为摘要。

### 6.7 Session 日志视图

前端 Session 视图支持两种模式：

**实时模式**（Session 正在运行时）：监听 `session_log` WebSocket 消息，逐条渲染。Claude 的 `text_delta`（stream_event）累积追加到当前 bubble，收到最终 `assistant` 事件后替换内容（避免重复显示）。

**历史模式**（Session 已结束）：调用 `GET /api/sessions/{id}/messages`，从 JSONL 文件加载结构化消息，渲染完整对话（包含工具调用、工具结果、思考过程）。

---

## 7. 配置参考

### 7.1 `config.yaml` 完整字段

```yaml
server:
  host: 127.0.0.1       # 监听地址
  port: 8765            # 监听端口

database:
  path: ./data/sessions.db   # SQLite 文件路径（支持 ~ 展开）

agents:
  codex:
    command: codex            # CLI 可执行文件名（需在 PATH 中）
    working_dir: .            # Agent 工作目录（MCP 配置从此目录读取）
  claude:
    command: claude
    working_dir: .

workflow:
  max_rounds: 0               # 最大循环轮数，0 = 无限
  auto_continue_on_error: false
  no_progress_max_rounds: 3

prompts:
  codex_template: |           # Codex 全局默认提示词模板
    ...
  claude_template: |          # Claude 全局默认提示词模板
    ...
```

### 7.2 提示词模板变量速查

| 变量 | 适用 Agent | 说明 |
|---|---|---|
| `{round_number}` | 全部 | 当前轮次编号（整数） |
| `{chat_history}` | 全部 | 最近若干轮的消息历史（含用户消息），首次 session 注入，resume 时为空 |
| `{user_input}` | 全部 | 用户最新输入，无则为 `无` |
| `{codex_summary}` | Claude | 上一步骤（通常为 Codex）的摘要 |
| `{last_summary}` | 全部 | 同 `{codex_summary}`，历史中最后一条非空摘要 |

---

## 8. 快速开始

### 安装依赖

```bash
pip install -r requirements.txt
```

### 安装 Agent CLI

```bash
# Claude Code
npm install -g @anthropic-ai/claude-code

# Codex CLI
npm install -g @openai/codex
```

### 配置

复制并编辑 `config.yaml`，设置 `agents.*.working_dir` 为你的项目目录。

### 启动

```bash
python -m backend.main
```

浏览器访问 `http://127.0.0.1:8765/`

### 基本操作流程

1. 在右侧 **Pipeline 面板**配置步骤和提示词模板，点击"保存流程"
2. 点击"**开始**"启动第一轮，系统依次运行 Pipeline 中每个 Agent
3. 左侧 Session 列表点击任意条目可**实时或历史**查看 Agent 的详细运行日志
4. 在底部输入框随时发送消息，内容将注入到下一步骤的 `{user_input}`
5. 使用"**停止**"/"**继续**"/"**重试**"控制执行流程
6. 在 Session 视图点击"**继续**"可携带新提示词从指定历史 Session 恢复运行
