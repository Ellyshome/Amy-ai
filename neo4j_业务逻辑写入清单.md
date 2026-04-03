# 当前项目 Neo4j 业务逻辑写入清单

## 1. 建图目标

这份清单用于把当前项目的“结构关系 + 运行链路 + 能力依赖”映射到 Neo4j，方便快速看清：

- 系统从哪里启动
- 配置如何驱动渠道、模型、Agent、语音与插件
- 消息如何从渠道进入，再进入普通聊天链路或 Agent 链路
- 插件、技能、工具、记忆、模型之间如何协作

说明：

- 当前仓库真实运行入口是 `app.py`
- `main.py` 目前只是示例占位，不参与主业务链
- `README.md` 当前内容较少，架构判断主要依据源码

---

## 2. 推荐节点模型

建议统一使用以下基础属性：

- `id`: 唯一标识
- `name`: 展示名
- `type`: 节点类型
- `layer`: 所属层
- `path`: 代表文件或目录
- `summary`: 业务说明

---

## 3. 节点写入清单

| id | name | type | layer | path | summary |
|---|---|---|---|---|---|
| project_amy_ai | Amy AI / CowAgent Project | Project | root | `/app` | 整体项目，聚合所有业务模块 |
| entry_app | app.py | EntryPoint | bootstrap | `/app/app.py` | 真正运行入口，负责加载配置、启动渠道、维持主进程 |
| entry_main_placeholder | main.py | Placeholder | bootstrap | `/app/main.py` | 示例占位入口，不参与实际业务 |
| config_center | Config Center | Config | bootstrap | `/app/config.py` | 管理全局配置、环境变量覆盖、用户数据、插件配置 |
| runtime_channel_manager | ChannelManager | RuntimeManager | runtime | `/app/app.py` | 负责创建、启动、停止、重启多个渠道实例 |
| factory_channel | channel_factory | Factory | runtime | `/app/channel/channel_factory.py` | 按 `channel_type` 创建渠道实例 |
| abstract_channel | Channel | AbstractComponent | channel | `/app/channel/channel.py` | 所有渠道的抽象基类，统一调用 Bridge |
| abstract_chat_channel | ChatChannel | AbstractComponent | channel | `/app/channel/chat_channel.py` | 处理消息预处理、上下文构造、回复生成、回复装饰、回复发送 |
| channel_web | WebChannel | Channel | channel | `/app/channel/web/web_channel.py` | 默认控制台渠道，支持 Web、SSE、文件上传 |
| channel_terminal | TerminalChannel | Channel | channel | `/app/channel/terminal/terminal_channel.py` | 命令行交互渠道 |
| channel_feishu | FeiShuChannel | Channel | channel | `/app/channel/feishu/feishu_channel.py` | 飞书接入渠道 |
| channel_dingtalk | DingTalkChannel | Channel | channel | `/app/channel/dingtalk/dingtalk_channel.py` | 钉钉接入渠道 |
| channel_wecom_bot | WecomBotChannel | Channel | channel | `/app/channel/wecom_bot/wecom_bot_channel.py` | 企微智能机器人渠道 |
| channel_wechatmp | WechatMPChannel | Channel | channel | `/app/channel/wechatmp/wechatmp_channel.py` | 微信公众号渠道 |
| channel_wechatcom_app | WechatComAppChannel | Channel | channel | `/app/channel/wechatcom/wechatcomapp_channel.py` | 企业微信应用渠道 |
| channel_weixin | WeixinChannel | Channel | channel | `/app/channel/weixin/weixin_channel.py` | 微信私域渠道 |
| channel_qq | QQChannel | Channel | channel | `/app/channel/qq/qq_channel.py` | QQ 渠道 |
| message_chat_message | ChatMessage | Message | domain | `/app/channel/chat_message.py` | 渠道消息对象 |
| context_runtime | Context | Context | domain | `/app/bridge/context.py` | 标准化消息上下文，承载 session、receiver、msg、ctype 等 |
| reply_runtime | Reply | Response | domain | `/app/bridge/reply.py` | 标准化回复对象 |
| bridge_core | Bridge | Bridge | core | `/app/bridge/bridge.py` | 聊天、语音、翻译能力总路由 |
| bridge_agent | AgentBridge | Bridge | agent | `/app/bridge/agent_bridge.py` | Agent 模式桥接层，负责会话级 Agent 复用与工具增强 |
| bridge_agent_init | AgentInitializer | Service | agent | `/app/bridge/agent_initializer.py` | 初始化 Agent、系统提示词、工具、记忆等上下文 |
| bridge_agent_event | AgentEventHandler | Service | agent | `/app/bridge/agent_event_handler.py` | 把 Agent 事件转成渠道可消费的流式输出 |
| service_agent_chat | ChatService | Service | agent | `/app/agent/chat/service.py` | 驱动 AgentStreamExecutor 执行并持久化会话消息 |
| protocol_agent | Agent Protocol | Protocol | agent | `/app/agent/protocol` | Agent、Task、Message、Stream 执行协议 |
| manager_tools | ToolManager | Manager | agent | `/app/agent/tools/tool_manager.py` | 加载、配置、实例化 Agent 工具 |
| manager_skills | SkillManager | Manager | agent | `/app/agent/skills/manager.py` | 加载内置/自定义技能，生成系统提示内容 |
| manager_memory | MemoryManager | Manager | agent | `/app/agent/memory/manager.py` | 管理长期记忆、分块、向量/关键词检索、落库 |
| service_memory | MemoryService | Service | agent | `/app/agent/memory/service.py` | 面向云端或控制台的记忆文件查询接口 |
| factory_bot | bot_factory | Factory | model | `/app/models/bot_factory.py` | 按 bot 类型实例化模型实现 |
| factory_voice | voice_factory | Factory | voice | `/app/voice/factory.py` | 选择语音识别/合成引擎 |
| factory_translate | translator_factory | Factory | translate | `/app/translate/factory.py` | 选择翻译引擎 |
| model_router | Chat Bot Router | Capability | model | `/app/bridge/bridge.py` | 根据 `model`、`bot_type`、`use_linkai` 选择实际模型实现 |
| model_openai_family | OpenAI Compatible Bots | ModelFamily | model | `/app/models/chatgpt / /app/models/openai` | OpenAI、Azure、DeepSeek 兼容链路 |
| model_gemini | Gemini Bot | ModelFamily | model | `/app/models/gemini` | Google Gemini 模型接入 |
| model_claude | Claude Bot | ModelFamily | model | `/app/models/claudeapi` | Claude 模型接入 |
| model_zhipu | Zhipu Bot | ModelFamily | model | `/app/models/zhipuai` | GLM / 智谱模型接入 |
| model_qwen | Qwen Bot | ModelFamily | model | `/app/models/ali / /app/models/dashscope` | 通义千问模型接入 |
| model_moonshot | Moonshot Bot | ModelFamily | model | `/app/models/moonshot` | Kimi / Moonshot 模型接入 |
| model_doubao | Doubao Bot | ModelFamily | model | `/app/models/doubao` | 豆包模型接入 |
| model_minimax | MiniMax Bot | ModelFamily | model | `/app/models/minimax` | MiniMax 模型接入 |
| model_linkai | LinkAI Bot | ModelFamily | model | `/app/models/linkai` | LinkAI 云端模型与云能力接入 |
| capability_voice | Voice Capability | Capability | voice | `/app/voice` | 语音识别与语音合成能力集合 |
| capability_translate | Translate Capability | Capability | translate | `/app/translate` | 翻译能力集合 |
| manager_plugins | PluginManager | Manager | plugin | `/app/plugins/plugin_manager.py` | 扫描、注册、启用、调度插件 |
| plugin_event_bus | Plugin Events | EventBus | plugin | `/app/plugins/event.py` | 插件事件总线，包含接收、处理、装饰、发送四类钩子 |
| plugin_builtin_group | Builtin Plugins | PluginGroup | plugin | `/app/plugins` | 内置插件集合，如 `godcmd`、`role`、`keyword`、`banwords`、`agent` |
| workspace_agent | Agent Workspace | Workspace | storage | `~/cow` | Agent 工作区，存放 skills、memory、tmp 等内容 |
| storage_user_datas | user_datas.pkl | Storage | storage | `appdata_dir/user_datas.pkl` | 持久化用户级配置数据 |
| storage_conversation | Conversation Store | Storage | storage | `/app/agent/memory/conversation_store.py` | Agent 会话消息持久化 |
| storage_vector_memory | Memory DB / Files | Storage | storage | `/app/agent/memory/storage.py` | 记忆向量库与记忆文件存储 |
| cloud_linkai_client | Cloud Client | ExternalService | integration | `/app/common/cloud_client.py` | LinkAI 云端部署/远程调度客户端 |
| tool_read | Read Tool | Tool | tool | `/app/agent/tools/read/read.py` | 读取文件 |
| tool_write | Write Tool | Tool | tool | `/app/agent/tools/write/write.py` | 写入文件 |
| tool_edit | Edit Tool | Tool | tool | `/app/agent/tools/edit/edit.py` | 编辑文件 |
| tool_bash | Bash Tool | Tool | tool | `/app/agent/tools/bash/bash.py` | 执行 shell 命令 |
| tool_ls | Ls Tool | Tool | tool | `/app/agent/tools/ls/ls.py` | 浏览目录结构 |
| tool_send | Send Tool | Tool | tool | `/app/agent/tools/send/send.py` | 主动发送消息 |
| tool_memory_search | MemorySearch Tool | Tool | tool | `/app/agent/tools/memory/memory_search.py` | 搜索长期记忆 |
| tool_memory_get | MemoryGet Tool | Tool | tool | `/app/agent/tools/memory/memory_get.py` | 获取指定记忆内容 |
| tool_web_search | WebSearch Tool | Tool | tool | `/app/agent/tools/web_search/web_search.py` | 联网搜索 |
| tool_web_fetch | WebFetch Tool | Tool | tool | `/app/agent/tools/web_fetch/web_fetch.py` | 抓取网页正文 |
| tool_vision | Vision Tool | Tool | tool | `/app/agent/tools/vision/vision.py` | 图片理解 |
| tool_env_config | EnvConfig Tool | Tool | tool | `/app/agent/tools/env_config/env_config.py` | 管理环境变量 |
| tool_scheduler | Scheduler Tool | Tool | tool | `/app/agent/tools/scheduler/scheduler_tool.py` | 定时任务 |

---

## 4. 关系写入清单

建议统一使用以下关系属性：

- `type`: 关系类型
- `summary`: 关系说明
- `phase`: 所属阶段，如 `bootstrap`、`message_flow`、`agent_flow`

### 4.1 启动与配置链路

| from | relation | to | summary |
|---|---|---|---|
| project_amy_ai | HAS_ENTRY | entry_app | 项目真实启动入口 |
| project_amy_ai | HAS_PLACEHOLDER | entry_main_placeholder | 占位入口，不参与主流程 |
| entry_app | LOADS | config_center | 启动时读取配置与环境变量 |
| entry_app | CREATES | runtime_channel_manager | 创建渠道运行管理器 |
| runtime_channel_manager | USES_FACTORY | factory_channel | 按配置创建渠道实例 |
| config_center | DRIVES | runtime_channel_manager | `channel_type`、`web_console` 等配置驱动渠道启动 |
| config_center | STORES | storage_user_datas | 用户数据持久化到 `user_datas.pkl` |
| config_center | ENABLES | cloud_linkai_client | 满足 `use_linkai + cloud_deployment_id` 时启用云端客户端 |
| runtime_channel_manager | STARTS | channel_web | 默认追加 Web 控制台 |
| runtime_channel_manager | STARTS | channel_terminal | 命令行模式时启动 terminal |
| runtime_channel_manager | STARTS | channel_feishu | 按配置启动 |
| runtime_channel_manager | STARTS | channel_dingtalk | 按配置启动 |
| runtime_channel_manager | STARTS | channel_wecom_bot | 按配置启动 |
| runtime_channel_manager | STARTS | channel_wechatmp | 按配置启动 |
| runtime_channel_manager | STARTS | channel_wechatcom_app | 按配置启动 |
| runtime_channel_manager | STARTS | channel_weixin | 按配置启动 |
| runtime_channel_manager | STARTS | channel_qq | 按配置启动 |

### 4.2 渠道与通用消息处理链路

| from | relation | to | summary |
|---|---|---|---|
| factory_channel | CREATES | abstract_channel | 工厂输出渠道抽象实例 |
| abstract_chat_channel | EXTENDS | abstract_channel | 聊天渠道基类继承 Channel |
| channel_web | EXTENDS | abstract_chat_channel | Web 渠道复用通用聊天链路 |
| channel_terminal | EXTENDS | abstract_chat_channel | Terminal 渠道复用通用聊天链路 |
| channel_feishu | EXTENDS | abstract_chat_channel | 飞书渠道复用通用聊天链路 |
| channel_dingtalk | EXTENDS | abstract_chat_channel | 钉钉渠道复用通用聊天链路 |
| channel_wecom_bot | EXTENDS | abstract_chat_channel | 企微机器人复用通用聊天链路 |
| channel_wechatmp | EXTENDS | abstract_chat_channel | 公众号复用通用聊天链路 |
| channel_wechatcom_app | EXTENDS | abstract_chat_channel | 企业微信应用复用通用聊天链路 |
| channel_weixin | EXTENDS | abstract_chat_channel | 微信渠道复用通用聊天链路 |
| channel_qq | EXTENDS | abstract_chat_channel | QQ 渠道复用通用聊天链路 |
| abstract_chat_channel | RECEIVES | message_chat_message | 渠道消息进入统一消息对象 |
| abstract_chat_channel | COMPOSES | context_runtime | 将消息转成标准化 Context |
| context_runtime | CARRIES | message_chat_message | Context 内部挂载原始消息对象 |
| abstract_chat_channel | PRODUCES | reply_runtime | 统一生成 Reply |
| abstract_chat_channel | SENDS_TO | abstract_channel | 通过渠道 send 方法回传结果 |

### 4.3 插件事件链路

| from | relation | to | summary |
|---|---|---|---|
| entry_app | LOADS | manager_plugins | 首次启动时加载插件 |
| manager_plugins | MANAGES | plugin_builtin_group | 扫描并启用内置插件 |
| manager_plugins | EMITS | plugin_event_bus | 负责事件分发 |
| abstract_chat_channel | EMITS | plugin_event_bus | 收到消息后先触发插件钩子 |
| plugin_event_bus | INTERCEPTS | context_runtime | 插件可修改或中断上下文 |
| plugin_event_bus | INTERCEPTS | reply_runtime | 插件可修改或阻断回复 |
| plugin_event_bus | INCLUDES | manager_plugins | 事件由 PluginManager 驱动 |

### 4.4 普通聊天链路

| from | relation | to | summary |
|---|---|---|---|
| abstract_channel | USES | bridge_core | 普通聊天、语音、翻译统一由 Bridge 调用 |
| bridge_core | USES_FACTORY | factory_bot | 创建聊天模型实例 |
| bridge_core | USES_FACTORY | factory_voice | 创建语音识别/合成实例 |
| bridge_core | USES_FACTORY | factory_translate | 创建翻译实例 |
| bridge_core | ROUTES_TO | model_router | 根据配置判断最终模型路由 |
| model_router | RESOLVES_TO | model_openai_family | OpenAI/Azure/DeepSeek 兼容实现 |
| model_router | RESOLVES_TO | model_gemini | Gemini 实现 |
| model_router | RESOLVES_TO | model_claude | Claude 实现 |
| model_router | RESOLVES_TO | model_zhipu | GLM/智谱实现 |
| model_router | RESOLVES_TO | model_qwen | 通义千问实现 |
| model_router | RESOLVES_TO | model_moonshot | Moonshot/Kimi 实现 |
| model_router | RESOLVES_TO | model_doubao | 豆包实现 |
| model_router | RESOLVES_TO | model_minimax | MiniMax 实现 |
| model_router | RESOLVES_TO | model_linkai | LinkAI 平台实现 |
| bridge_core | CALLS | capability_voice | 处理语音转文本、文本转语音 |
| bridge_core | CALLS | capability_translate | 处理翻译 |

### 4.5 Agent 链路

| from | relation | to | summary |
|---|---|---|---|
| config_center | ENABLES | bridge_agent | `agent=true` 时进入 Agent 模式 |
| abstract_channel | USES | bridge_agent | Channel 在 Agent 模式下通过 AgentBridge 处理回复 |
| bridge_agent | INITIALIZES | bridge_agent_init | 初始化会话级 Agent |
| bridge_agent | STREAMS_VIA | bridge_agent_event | 处理 Agent 事件回调 |
| bridge_agent | USES | service_agent_chat | 运行 Agent 流式执行服务 |
| service_agent_chat | RUNS | protocol_agent | 基于 Agent 协议执行消息流 |
| bridge_agent | USES | manager_tools | 为 Agent 注入可用工具 |
| bridge_agent | USES | manager_skills | 为 Agent 生成技能提示词 |
| bridge_agent | USES | manager_memory | 为 Agent 提供长期记忆能力 |
| service_agent_chat | PERSISTS_TO | storage_conversation | 把新增消息写入会话存储 |
| manager_memory | PERSISTS_TO | storage_vector_memory | 记忆写入向量库与文件系统 |
| service_memory | READS_FROM | storage_vector_memory | 读取记忆文件或元数据 |
| manager_skills | READS_FROM | workspace_agent | 从工作区技能目录加载技能 |
| manager_memory | READS_FROM | workspace_agent | 从工作区记忆目录加载与维护记忆 |
| channel_web | STREAMS_WITH | service_agent_chat | Web 渠道可通过 SSE 接收 Agent 流式事件 |

### 4.6 工具层关系

| from | relation | to | summary |
|---|---|---|---|
| manager_tools | LOADS | tool_read | 文件读取工具 |
| manager_tools | LOADS | tool_write | 文件写入工具 |
| manager_tools | LOADS | tool_edit | 文件编辑工具 |
| manager_tools | LOADS | tool_bash | shell 执行工具 |
| manager_tools | LOADS | tool_ls | 目录浏览工具 |
| manager_tools | LOADS | tool_send | 主动消息发送工具 |
| manager_tools | LOADS | tool_memory_search | 记忆搜索工具 |
| manager_tools | LOADS | tool_memory_get | 记忆读取工具 |
| manager_tools | LOADS | tool_web_search | 联网搜索工具 |
| manager_tools | LOADS | tool_web_fetch | 网页抓取工具 |
| manager_tools | LOADS | tool_vision | 图像理解工具 |
| manager_tools | LOADS | tool_env_config | 环境变量工具 |
| manager_tools | LOADS | tool_scheduler | 定时任务工具 |
| tool_memory_search | READS_FROM | manager_memory | 通过 MemoryManager 搜索长期记忆 |
| tool_memory_get | READS_FROM | manager_memory | 通过 MemoryManager 获取记忆正文 |

---

## 5. 推荐重点观察路径

如果你在 Neo4j 里只看几条主路径，建议优先看这 4 条：

1. `entry_app -> config_center -> runtime_channel_manager -> factory_channel -> channel_web`
2. `abstract_chat_channel -> context_runtime -> bridge_core -> model_router -> model_*`
3. `abstract_chat_channel -> plugin_event_bus -> manager_plugins -> plugin_builtin_group`
4. `abstract_channel -> bridge_agent -> manager_tools / manager_skills / manager_memory -> service_agent_chat`

---

## 6. 推荐 Cypher 写法

下面这段不是完整导入脚本，而是建议的写入模式。你后续可以按这份清单批量补齐。

```cypher
MERGE (p:Project {id: 'project_amy_ai'})
SET p.name = 'Amy AI / CowAgent Project',
    p.layer = 'root',
    p.path = '/app',
    p.summary = '多渠道 + 多模型 + Agent + 插件的智能助手项目';

MERGE (n:EntryPoint {id: 'entry_app'})
SET n.name = 'app.py',
    n.layer = 'bootstrap',
    n.path = '/app/app.py',
    n.summary = '真实运行入口';

MERGE (c:Config {id: 'config_center'})
SET c.name = 'Config Center',
    c.layer = 'bootstrap',
    c.path = '/app/config.py',
    c.summary = '加载配置、环境变量、用户数据与插件配置';

MERGE (m:RuntimeManager {id: 'runtime_channel_manager'})
SET m.name = 'ChannelManager',
    m.layer = 'runtime',
    m.path = '/app/app.py',
    m.summary = '管理多渠道启动、停止、重启';

MERGE (b:Bridge {id: 'bridge_core'})
SET b.name = 'Bridge',
    b.layer = 'core',
    b.path = '/app/bridge/bridge.py',
    b.summary = '模型、语音、翻译统一路由';

MERGE (a:Bridge {id: 'bridge_agent'})
SET a.name = 'AgentBridge',
    a.layer = 'agent',
    a.path = '/app/bridge/agent_bridge.py',
    a.summary = 'Agent 模式桥接层';

MERGE (p)-[:HAS_ENTRY {phase: 'bootstrap'}]->(n)
MERGE (n)-[:LOADS {phase: 'bootstrap'}]->(c)
MERGE (n)-[:CREATES {phase: 'bootstrap'}]->(m)
MERGE (b)-[:USES {phase: 'message_flow'}]->(a);
```

---

## 7. 建图建议

建议在 Neo4j Browser 或 Bloom 里按以下维度上色：

- `layer=bootstrap` 用一种颜色
- `layer=channel` 用一种颜色
- `layer=core/model` 用一种颜色
- `layer=agent/tool/plugin` 用一种颜色
- `layer=storage/integration` 用一种颜色

如果你后面希望，我还可以继续把这份清单直接转换成：

- 一份可执行的 `.cypher` 导入脚本
- 两份 `nodes.csv / relationships.csv`
- 或者一份更偏“业务流程图”的精简版 Neo4j 数据集
