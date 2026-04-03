// Amy AI / CowAgent 项目业务逻辑图谱导入脚本
// 用法示例：
// cypher-shell -u neo4j -p <password> -f neo4j_业务逻辑导入脚本.cypher

CREATE CONSTRAINT entity_id_unique IF NOT EXISTS
FOR (n:Entity)
REQUIRE n.id IS UNIQUE;

WITH [
  {id:'project_amy_ai', name:'Amy AI / CowAgent Project', type:'Project', layer:'root', path:'/app', summary:'整体项目，聚合所有业务模块'},
  {id:'entry_app', name:'app.py', type:'EntryPoint', layer:'bootstrap', path:'/app/app.py', summary:'真正运行入口，负责加载配置、启动渠道、维持主进程'},
  {id:'entry_main_placeholder', name:'main.py', type:'Placeholder', layer:'bootstrap', path:'/app/main.py', summary:'示例占位入口，不参与实际业务'},
  {id:'config_center', name:'Config Center', type:'Config', layer:'bootstrap', path:'/app/config.py', summary:'管理全局配置、环境变量覆盖、用户数据、插件配置'},
  {id:'runtime_channel_manager', name:'ChannelManager', type:'RuntimeManager', layer:'runtime', path:'/app/app.py', summary:'负责创建、启动、停止、重启多个渠道实例'},
  {id:'factory_channel', name:'channel_factory', type:'Factory', layer:'runtime', path:'/app/channel/channel_factory.py', summary:'按 channel_type 创建渠道实例'},
  {id:'abstract_channel', name:'Channel', type:'AbstractComponent', layer:'channel', path:'/app/channel/channel.py', summary:'所有渠道的抽象基类，统一调用 Bridge'},
  {id:'abstract_chat_channel', name:'ChatChannel', type:'AbstractComponent', layer:'channel', path:'/app/channel/chat_channel.py', summary:'处理消息预处理、上下文构造、回复生成、回复装饰、回复发送'},
  {id:'channel_web', name:'WebChannel', type:'Channel', layer:'channel', path:'/app/channel/web/web_channel.py', summary:'默认控制台渠道，支持 Web、SSE、文件上传'},
  {id:'channel_terminal', name:'TerminalChannel', type:'Channel', layer:'channel', path:'/app/channel/terminal/terminal_channel.py', summary:'命令行交互渠道'},
  {id:'channel_feishu', name:'FeiShuChannel', type:'Channel', layer:'channel', path:'/app/channel/feishu/feishu_channel.py', summary:'飞书接入渠道'},
  {id:'channel_dingtalk', name:'DingTalkChannel', type:'Channel', layer:'channel', path:'/app/channel/dingtalk/dingtalk_channel.py', summary:'钉钉接入渠道'},
  {id:'channel_wecom_bot', name:'WecomBotChannel', type:'Channel', layer:'channel', path:'/app/channel/wecom_bot/wecom_bot_channel.py', summary:'企微智能机器人渠道'},
  {id:'channel_wechatmp', name:'WechatMPChannel', type:'Channel', layer:'channel', path:'/app/channel/wechatmp/wechatmp_channel.py', summary:'微信公众号渠道'},
  {id:'channel_wechatcom_app', name:'WechatComAppChannel', type:'Channel', layer:'channel', path:'/app/channel/wechatcom/wechatcomapp_channel.py', summary:'企业微信应用渠道'},
  {id:'channel_weixin', name:'WeixinChannel', type:'Channel', layer:'channel', path:'/app/channel/weixin/weixin_channel.py', summary:'微信私域渠道'},
  {id:'channel_qq', name:'QQChannel', type:'Channel', layer:'channel', path:'/app/channel/qq/qq_channel.py', summary:'QQ 渠道'},
  {id:'message_chat_message', name:'ChatMessage', type:'Message', layer:'domain', path:'/app/channel/chat_message.py', summary:'渠道消息对象'},
  {id:'context_runtime', name:'Context', type:'Context', layer:'domain', path:'/app/bridge/context.py', summary:'标准化消息上下文，承载 session、receiver、msg、ctype 等'},
  {id:'reply_runtime', name:'Reply', type:'Response', layer:'domain', path:'/app/bridge/reply.py', summary:'标准化回复对象'},
  {id:'bridge_core', name:'Bridge', type:'Bridge', layer:'core', path:'/app/bridge/bridge.py', summary:'聊天、语音、翻译能力总路由'},
  {id:'bridge_agent', name:'AgentBridge', type:'Bridge', layer:'agent', path:'/app/bridge/agent_bridge.py', summary:'Agent 模式桥接层，负责会话级 Agent 复用与工具增强'},
  {id:'bridge_agent_init', name:'AgentInitializer', type:'Service', layer:'agent', path:'/app/bridge/agent_initializer.py', summary:'初始化 Agent、系统提示词、工具、记忆等上下文'},
  {id:'bridge_agent_event', name:'AgentEventHandler', type:'Service', layer:'agent', path:'/app/bridge/agent_event_handler.py', summary:'把 Agent 事件转成渠道可消费的流式输出'},
  {id:'service_agent_chat', name:'ChatService', type:'Service', layer:'agent', path:'/app/agent/chat/service.py', summary:'驱动 AgentStreamExecutor 执行并持久化会话消息'},
  {id:'protocol_agent', name:'Agent Protocol', type:'Protocol', layer:'agent', path:'/app/agent/protocol', summary:'Agent、Task、Message、Stream 执行协议'},
  {id:'manager_tools', name:'ToolManager', type:'Manager', layer:'agent', path:'/app/agent/tools/tool_manager.py', summary:'加载、配置、实例化 Agent 工具'},
  {id:'manager_skills', name:'SkillManager', type:'Manager', layer:'agent', path:'/app/agent/skills/manager.py', summary:'加载内置/自定义技能，生成系统提示内容'},
  {id:'manager_memory', name:'MemoryManager', type:'Manager', layer:'agent', path:'/app/agent/memory/manager.py', summary:'管理长期记忆、分块、向量/关键词检索、落库'},
  {id:'service_memory', name:'MemoryService', type:'Service', layer:'agent', path:'/app/agent/memory/service.py', summary:'面向云端或控制台的记忆文件查询接口'},
  {id:'factory_bot', name:'bot_factory', type:'Factory', layer:'model', path:'/app/models/bot_factory.py', summary:'按 bot 类型实例化模型实现'},
  {id:'factory_voice', name:'voice_factory', type:'Factory', layer:'voice', path:'/app/voice/factory.py', summary:'选择语音识别/合成引擎'},
  {id:'factory_translate', name:'translator_factory', type:'Factory', layer:'translate', path:'/app/translate/factory.py', summary:'选择翻译引擎'},
  {id:'model_router', name:'Chat Bot Router', type:'Capability', layer:'model', path:'/app/bridge/bridge.py', summary:'根据 model、bot_type、use_linkai 选择实际模型实现'},
  {id:'model_openai_family', name:'OpenAI Compatible Bots', type:'ModelFamily', layer:'model', path:'/app/models/chatgpt;/app/models/openai', summary:'OpenAI、Azure、DeepSeek 兼容链路'},
  {id:'model_gemini', name:'Gemini Bot', type:'ModelFamily', layer:'model', path:'/app/models/gemini', summary:'Google Gemini 模型接入'},
  {id:'model_claude', name:'Claude Bot', type:'ModelFamily', layer:'model', path:'/app/models/claudeapi', summary:'Claude 模型接入'},
  {id:'model_zhipu', name:'Zhipu Bot', type:'ModelFamily', layer:'model', path:'/app/models/zhipuai', summary:'GLM / 智谱模型接入'},
  {id:'model_qwen', name:'Qwen Bot', type:'ModelFamily', layer:'model', path:'/app/models/ali;/app/models/dashscope', summary:'通义千问模型接入'},
  {id:'model_moonshot', name:'Moonshot Bot', type:'ModelFamily', layer:'model', path:'/app/models/moonshot', summary:'Kimi / Moonshot 模型接入'},
  {id:'model_doubao', name:'Doubao Bot', type:'ModelFamily', layer:'model', path:'/app/models/doubao', summary:'豆包模型接入'},
  {id:'model_minimax', name:'MiniMax Bot', type:'ModelFamily', layer:'model', path:'/app/models/minimax', summary:'MiniMax 模型接入'},
  {id:'model_linkai', name:'LinkAI Bot', type:'ModelFamily', layer:'model', path:'/app/models/linkai', summary:'LinkAI 云端模型与云能力接入'},
  {id:'capability_voice', name:'Voice Capability', type:'Capability', layer:'voice', path:'/app/voice', summary:'语音识别与语音合成能力集合'},
  {id:'capability_translate', name:'Translate Capability', type:'Capability', layer:'translate', path:'/app/translate', summary:'翻译能力集合'},
  {id:'manager_plugins', name:'PluginManager', type:'Manager', layer:'plugin', path:'/app/plugins/plugin_manager.py', summary:'扫描、注册、启用、调度插件'},
  {id:'plugin_event_bus', name:'Plugin Events', type:'EventBus', layer:'plugin', path:'/app/plugins/event.py', summary:'插件事件总线，包含接收、处理、装饰、发送四类钩子'},
  {id:'plugin_builtin_group', name:'Builtin Plugins', type:'PluginGroup', layer:'plugin', path:'/app/plugins', summary:'内置插件集合，如 godcmd、role、keyword、banwords、agent'},
  {id:'workspace_agent', name:'Agent Workspace', type:'Workspace', layer:'storage', path:'~/cow', summary:'Agent 工作区，存放 skills、memory、tmp 等内容'},
  {id:'storage_user_datas', name:'user_datas.pkl', type:'Storage', layer:'storage', path:'appdata_dir/user_datas.pkl', summary:'持久化用户级配置数据'},
  {id:'storage_conversation', name:'Conversation Store', type:'Storage', layer:'storage', path:'/app/agent/memory/conversation_store.py', summary:'Agent 会话消息持久化'},
  {id:'storage_vector_memory', name:'Memory DB / Files', type:'Storage', layer:'storage', path:'/app/agent/memory/storage.py', summary:'记忆向量库与记忆文件存储'},
  {id:'cloud_linkai_client', name:'Cloud Client', type:'ExternalService', layer:'integration', path:'/app/common/cloud_client.py', summary:'LinkAI 云端部署/远程调度客户端'},
  {id:'tool_read', name:'Read Tool', type:'Tool', layer:'tool', path:'/app/agent/tools/read/read.py', summary:'读取文件'},
  {id:'tool_write', name:'Write Tool', type:'Tool', layer:'tool', path:'/app/agent/tools/write/write.py', summary:'写入文件'},
  {id:'tool_edit', name:'Edit Tool', type:'Tool', layer:'tool', path:'/app/agent/tools/edit/edit.py', summary:'编辑文件'},
  {id:'tool_bash', name:'Bash Tool', type:'Tool', layer:'tool', path:'/app/agent/tools/bash/bash.py', summary:'执行 shell 命令'},
  {id:'tool_ls', name:'Ls Tool', type:'Tool', layer:'tool', path:'/app/agent/tools/ls/ls.py', summary:'浏览目录结构'},
  {id:'tool_send', name:'Send Tool', type:'Tool', layer:'tool', path:'/app/agent/tools/send/send.py', summary:'主动发送消息'},
  {id:'tool_memory_search', name:'MemorySearch Tool', type:'Tool', layer:'tool', path:'/app/agent/tools/memory/memory_search.py', summary:'搜索长期记忆'},
  {id:'tool_memory_get', name:'MemoryGet Tool', type:'Tool', layer:'tool', path:'/app/agent/tools/memory/memory_get.py', summary:'获取指定记忆内容'},
  {id:'tool_web_search', name:'WebSearch Tool', type:'Tool', layer:'tool', path:'/app/agent/tools/web_search/web_search.py', summary:'联网搜索'},
  {id:'tool_web_fetch', name:'WebFetch Tool', type:'Tool', layer:'tool', path:'/app/agent/tools/web_fetch/web_fetch.py', summary:'抓取网页正文'},
  {id:'tool_vision', name:'Vision Tool', type:'Tool', layer:'tool', path:'/app/agent/tools/vision/vision.py', summary:'图片理解'},
  {id:'tool_env_config', name:'EnvConfig Tool', type:'Tool', layer:'tool', path:'/app/agent/tools/env_config/env_config.py', summary:'管理环境变量'},
  {id:'tool_scheduler', name:'Scheduler Tool', type:'Tool', layer:'tool', path:'/app/agent/tools/scheduler/scheduler_tool.py', summary:'定时任务'}
] AS nodes
UNWIND nodes AS node
MERGE (n:Entity {id: node.id})
SET n.name = node.name,
    n.type = node.type,
    n.layer = node.layer,
    n.path = node.path,
    n.summary = node.summary;

MATCH (a:Entity {id:'project_amy_ai'}), (b:Entity {id:'entry_app'})
MERGE (a)-[r:HAS_ENTRY]->(b)
SET r.summary = '项目真实启动入口', r.phase = 'bootstrap';
MATCH (a:Entity {id:'project_amy_ai'}), (b:Entity {id:'entry_main_placeholder'})
MERGE (a)-[r:HAS_PLACEHOLDER]->(b)
SET r.summary = '占位入口，不参与主流程', r.phase = 'bootstrap';
MATCH (a:Entity {id:'entry_app'}), (b:Entity {id:'config_center'})
MERGE (a)-[r:LOADS]->(b)
SET r.summary = '启动时读取配置与环境变量', r.phase = 'bootstrap';
MATCH (a:Entity {id:'entry_app'}), (b:Entity {id:'runtime_channel_manager'})
MERGE (a)-[r:CREATES]->(b)
SET r.summary = '创建渠道运行管理器', r.phase = 'bootstrap';
MATCH (a:Entity {id:'runtime_channel_manager'}), (b:Entity {id:'factory_channel'})
MERGE (a)-[r:USES_FACTORY]->(b)
SET r.summary = '按配置创建渠道实例', r.phase = 'bootstrap';
MATCH (a:Entity {id:'config_center'}), (b:Entity {id:'runtime_channel_manager'})
MERGE (a)-[r:DRIVES]->(b)
SET r.summary = 'channel_type、web_console 等配置驱动渠道启动', r.phase = 'bootstrap';
MATCH (a:Entity {id:'config_center'}), (b:Entity {id:'storage_user_datas'})
MERGE (a)-[r:STORES]->(b)
SET r.summary = '用户数据持久化到 user_datas.pkl', r.phase = 'bootstrap';
MATCH (a:Entity {id:'config_center'}), (b:Entity {id:'cloud_linkai_client'})
MERGE (a)-[r:ENABLES]->(b)
SET r.summary = '满足 use_linkai + cloud_deployment_id 时启用云端客户端', r.phase = 'bootstrap';
MATCH (a:Entity {id:'runtime_channel_manager'}), (b:Entity {id:'channel_web'})
MERGE (a)-[r:STARTS]->(b)
SET r.summary = '默认追加 Web 控制台', r.phase = 'bootstrap';
MATCH (a:Entity {id:'runtime_channel_manager'}), (b:Entity {id:'channel_terminal'})
MERGE (a)-[r:STARTS]->(b)
SET r.summary = '命令行模式时启动 terminal', r.phase = 'bootstrap';
MATCH (a:Entity {id:'runtime_channel_manager'}), (b:Entity {id:'channel_feishu'})
MERGE (a)-[r:STARTS]->(b)
SET r.summary = '按配置启动', r.phase = 'bootstrap';
MATCH (a:Entity {id:'runtime_channel_manager'}), (b:Entity {id:'channel_dingtalk'})
MERGE (a)-[r:STARTS]->(b)
SET r.summary = '按配置启动', r.phase = 'bootstrap';
MATCH (a:Entity {id:'runtime_channel_manager'}), (b:Entity {id:'channel_wecom_bot'})
MERGE (a)-[r:STARTS]->(b)
SET r.summary = '按配置启动', r.phase = 'bootstrap';
MATCH (a:Entity {id:'runtime_channel_manager'}), (b:Entity {id:'channel_wechatmp'})
MERGE (a)-[r:STARTS]->(b)
SET r.summary = '按配置启动', r.phase = 'bootstrap';
MATCH (a:Entity {id:'runtime_channel_manager'}), (b:Entity {id:'channel_wechatcom_app'})
MERGE (a)-[r:STARTS]->(b)
SET r.summary = '按配置启动', r.phase = 'bootstrap';
MATCH (a:Entity {id:'runtime_channel_manager'}), (b:Entity {id:'channel_weixin'})
MERGE (a)-[r:STARTS]->(b)
SET r.summary = '按配置启动', r.phase = 'bootstrap';
MATCH (a:Entity {id:'runtime_channel_manager'}), (b:Entity {id:'channel_qq'})
MERGE (a)-[r:STARTS]->(b)
SET r.summary = '按配置启动', r.phase = 'bootstrap';

MATCH (a:Entity {id:'factory_channel'}), (b:Entity {id:'abstract_channel'})
MERGE (a)-[r:CREATES]->(b)
SET r.summary = '工厂输出渠道抽象实例', r.phase = 'message_flow';
MATCH (a:Entity {id:'abstract_chat_channel'}), (b:Entity {id:'abstract_channel'})
MERGE (a)-[r:EXTENDS]->(b)
SET r.summary = '聊天渠道基类继承 Channel', r.phase = 'message_flow';
MATCH (a:Entity {id:'channel_web'}), (b:Entity {id:'abstract_chat_channel'})
MERGE (a)-[r:EXTENDS]->(b)
SET r.summary = 'Web 渠道复用通用聊天链路', r.phase = 'message_flow';
MATCH (a:Entity {id:'channel_terminal'}), (b:Entity {id:'abstract_chat_channel'})
MERGE (a)-[r:EXTENDS]->(b)
SET r.summary = 'Terminal 渠道复用通用聊天链路', r.phase = 'message_flow';
MATCH (a:Entity {id:'channel_feishu'}), (b:Entity {id:'abstract_chat_channel'})
MERGE (a)-[r:EXTENDS]->(b)
SET r.summary = '飞书渠道复用通用聊天链路', r.phase = 'message_flow';
MATCH (a:Entity {id:'channel_dingtalk'}), (b:Entity {id:'abstract_chat_channel'})
MERGE (a)-[r:EXTENDS]->(b)
SET r.summary = '钉钉渠道复用通用聊天链路', r.phase = 'message_flow';
MATCH (a:Entity {id:'channel_wecom_bot'}), (b:Entity {id:'abstract_chat_channel'})
MERGE (a)-[r:EXTENDS]->(b)
SET r.summary = '企微机器人复用通用聊天链路', r.phase = 'message_flow';
MATCH (a:Entity {id:'channel_wechatmp'}), (b:Entity {id:'abstract_chat_channel'})
MERGE (a)-[r:EXTENDS]->(b)
SET r.summary = '公众号复用通用聊天链路', r.phase = 'message_flow';
MATCH (a:Entity {id:'channel_wechatcom_app'}), (b:Entity {id:'abstract_chat_channel'})
MERGE (a)-[r:EXTENDS]->(b)
SET r.summary = '企业微信应用复用通用聊天链路', r.phase = 'message_flow';
MATCH (a:Entity {id:'channel_weixin'}), (b:Entity {id:'abstract_chat_channel'})
MERGE (a)-[r:EXTENDS]->(b)
SET r.summary = '微信渠道复用通用聊天链路', r.phase = 'message_flow';
MATCH (a:Entity {id:'channel_qq'}), (b:Entity {id:'abstract_chat_channel'})
MERGE (a)-[r:EXTENDS]->(b)
SET r.summary = 'QQ 渠道复用通用聊天链路', r.phase = 'message_flow';
MATCH (a:Entity {id:'abstract_chat_channel'}), (b:Entity {id:'message_chat_message'})
MERGE (a)-[r:RECEIVES]->(b)
SET r.summary = '渠道消息进入统一消息对象', r.phase = 'message_flow';
MATCH (a:Entity {id:'abstract_chat_channel'}), (b:Entity {id:'context_runtime'})
MERGE (a)-[r:COMPOSES]->(b)
SET r.summary = '将消息转成标准化 Context', r.phase = 'message_flow';
MATCH (a:Entity {id:'context_runtime'}), (b:Entity {id:'message_chat_message'})
MERGE (a)-[r:CARRIES]->(b)
SET r.summary = 'Context 内部挂载原始消息对象', r.phase = 'message_flow';
MATCH (a:Entity {id:'abstract_chat_channel'}), (b:Entity {id:'reply_runtime'})
MERGE (a)-[r:PRODUCES]->(b)
SET r.summary = '统一生成 Reply', r.phase = 'message_flow';
MATCH (a:Entity {id:'abstract_chat_channel'}), (b:Entity {id:'abstract_channel'})
MERGE (a)-[r:SENDS_TO]->(b)
SET r.summary = '通过渠道 send 方法回传结果', r.phase = 'message_flow';

MATCH (a:Entity {id:'entry_app'}), (b:Entity {id:'manager_plugins'})
MERGE (a)-[r:LOADS]->(b)
SET r.summary = '首次启动时加载插件', r.phase = 'plugin_flow';
MATCH (a:Entity {id:'manager_plugins'}), (b:Entity {id:'plugin_builtin_group'})
MERGE (a)-[r:MANAGES]->(b)
SET r.summary = '扫描并启用内置插件', r.phase = 'plugin_flow';
MATCH (a:Entity {id:'manager_plugins'}), (b:Entity {id:'plugin_event_bus'})
MERGE (a)-[r:EMITS]->(b)
SET r.summary = '负责事件分发', r.phase = 'plugin_flow';
MATCH (a:Entity {id:'abstract_chat_channel'}), (b:Entity {id:'plugin_event_bus'})
MERGE (a)-[r:EMITS]->(b)
SET r.summary = '收到消息后先触发插件钩子', r.phase = 'plugin_flow';
MATCH (a:Entity {id:'plugin_event_bus'}), (b:Entity {id:'context_runtime'})
MERGE (a)-[r:INTERCEPTS]->(b)
SET r.summary = '插件可修改或中断上下文', r.phase = 'plugin_flow';
MATCH (a:Entity {id:'plugin_event_bus'}), (b:Entity {id:'reply_runtime'})
MERGE (a)-[r:INTERCEPTS]->(b)
SET r.summary = '插件可修改或阻断回复', r.phase = 'plugin_flow';
MATCH (a:Entity {id:'plugin_event_bus'}), (b:Entity {id:'manager_plugins'})
MERGE (a)-[r:INCLUDES]->(b)
SET r.summary = '事件由 PluginManager 驱动', r.phase = 'plugin_flow';

MATCH (a:Entity {id:'abstract_channel'}), (b:Entity {id:'bridge_core'})
MERGE (a)-[r:USES]->(b)
SET r.summary = '普通聊天、语音、翻译统一由 Bridge 调用', r.phase = 'message_flow';
MATCH (a:Entity {id:'bridge_core'}), (b:Entity {id:'factory_bot'})
MERGE (a)-[r:USES_FACTORY]->(b)
SET r.summary = '创建聊天模型实例', r.phase = 'message_flow';
MATCH (a:Entity {id:'bridge_core'}), (b:Entity {id:'factory_voice'})
MERGE (a)-[r:USES_FACTORY]->(b)
SET r.summary = '创建语音识别/合成实例', r.phase = 'message_flow';
MATCH (a:Entity {id:'bridge_core'}), (b:Entity {id:'factory_translate'})
MERGE (a)-[r:USES_FACTORY]->(b)
SET r.summary = '创建翻译实例', r.phase = 'message_flow';
MATCH (a:Entity {id:'bridge_core'}), (b:Entity {id:'model_router'})
MERGE (a)-[r:ROUTES_TO]->(b)
SET r.summary = '根据配置判断最终模型路由', r.phase = 'message_flow';
MATCH (a:Entity {id:'model_router'}), (b:Entity {id:'model_openai_family'})
MERGE (a)-[r:RESOLVES_TO]->(b)
SET r.summary = 'OpenAI/Azure/DeepSeek 兼容实现', r.phase = 'message_flow';
MATCH (a:Entity {id:'model_router'}), (b:Entity {id:'model_gemini'})
MERGE (a)-[r:RESOLVES_TO]->(b)
SET r.summary = 'Gemini 实现', r.phase = 'message_flow';
MATCH (a:Entity {id:'model_router'}), (b:Entity {id:'model_claude'})
MERGE (a)-[r:RESOLVES_TO]->(b)
SET r.summary = 'Claude 实现', r.phase = 'message_flow';
MATCH (a:Entity {id:'model_router'}), (b:Entity {id:'model_zhipu'})
MERGE (a)-[r:RESOLVES_TO]->(b)
SET r.summary = 'GLM/智谱实现', r.phase = 'message_flow';
MATCH (a:Entity {id:'model_router'}), (b:Entity {id:'model_qwen'})
MERGE (a)-[r:RESOLVES_TO]->(b)
SET r.summary = '通义千问实现', r.phase = 'message_flow';
MATCH (a:Entity {id:'model_router'}), (b:Entity {id:'model_moonshot'})
MERGE (a)-[r:RESOLVES_TO]->(b)
SET r.summary = 'Moonshot/Kimi 实现', r.phase = 'message_flow';
MATCH (a:Entity {id:'model_router'}), (b:Entity {id:'model_doubao'})
MERGE (a)-[r:RESOLVES_TO]->(b)
SET r.summary = '豆包实现', r.phase = 'message_flow';
MATCH (a:Entity {id:'model_router'}), (b:Entity {id:'model_minimax'})
MERGE (a)-[r:RESOLVES_TO]->(b)
SET r.summary = 'MiniMax 实现', r.phase = 'message_flow';
MATCH (a:Entity {id:'model_router'}), (b:Entity {id:'model_linkai'})
MERGE (a)-[r:RESOLVES_TO]->(b)
SET r.summary = 'LinkAI 平台实现', r.phase = 'message_flow';
MATCH (a:Entity {id:'bridge_core'}), (b:Entity {id:'capability_voice'})
MERGE (a)-[r:CALLS]->(b)
SET r.summary = '处理语音转文本、文本转语音', r.phase = 'message_flow';
MATCH (a:Entity {id:'bridge_core'}), (b:Entity {id:'capability_translate'})
MERGE (a)-[r:CALLS]->(b)
SET r.summary = '处理翻译', r.phase = 'message_flow';

MATCH (a:Entity {id:'config_center'}), (b:Entity {id:'bridge_agent'})
MERGE (a)-[r:ENABLES]->(b)
SET r.summary = 'agent=true 时进入 Agent 模式', r.phase = 'agent_flow';
MATCH (a:Entity {id:'abstract_channel'}), (b:Entity {id:'bridge_agent'})
MERGE (a)-[r:USES]->(b)
SET r.summary = 'Channel 在 Agent 模式下通过 AgentBridge 处理回复', r.phase = 'agent_flow';
MATCH (a:Entity {id:'bridge_agent'}), (b:Entity {id:'bridge_agent_init'})
MERGE (a)-[r:INITIALIZES]->(b)
SET r.summary = '初始化会话级 Agent', r.phase = 'agent_flow';
MATCH (a:Entity {id:'bridge_agent'}), (b:Entity {id:'bridge_agent_event'})
MERGE (a)-[r:STREAMS_VIA]->(b)
SET r.summary = '处理 Agent 事件回调', r.phase = 'agent_flow';
MATCH (a:Entity {id:'bridge_agent'}), (b:Entity {id:'service_agent_chat'})
MERGE (a)-[r:USES]->(b)
SET r.summary = '运行 Agent 流式执行服务', r.phase = 'agent_flow';
MATCH (a:Entity {id:'service_agent_chat'}), (b:Entity {id:'protocol_agent'})
MERGE (a)-[r:RUNS]->(b)
SET r.summary = '基于 Agent 协议执行消息流', r.phase = 'agent_flow';
MATCH (a:Entity {id:'bridge_agent'}), (b:Entity {id:'manager_tools'})
MERGE (a)-[r:USES]->(b)
SET r.summary = '为 Agent 注入可用工具', r.phase = 'agent_flow';
MATCH (a:Entity {id:'bridge_agent'}), (b:Entity {id:'manager_skills'})
MERGE (a)-[r:USES]->(b)
SET r.summary = '为 Agent 生成技能提示词', r.phase = 'agent_flow';
MATCH (a:Entity {id:'bridge_agent'}), (b:Entity {id:'manager_memory'})
MERGE (a)-[r:USES]->(b)
SET r.summary = '为 Agent 提供长期记忆能力', r.phase = 'agent_flow';
MATCH (a:Entity {id:'service_agent_chat'}), (b:Entity {id:'storage_conversation'})
MERGE (a)-[r:PERSISTS_TO]->(b)
SET r.summary = '把新增消息写入会话存储', r.phase = 'agent_flow';
MATCH (a:Entity {id:'manager_memory'}), (b:Entity {id:'storage_vector_memory'})
MERGE (a)-[r:PERSISTS_TO]->(b)
SET r.summary = '记忆写入向量库与文件系统', r.phase = 'agent_flow';
MATCH (a:Entity {id:'service_memory'}), (b:Entity {id:'storage_vector_memory'})
MERGE (a)-[r:READS_FROM]->(b)
SET r.summary = '读取记忆文件或元数据', r.phase = 'agent_flow';
MATCH (a:Entity {id:'manager_skills'}), (b:Entity {id:'workspace_agent'})
MERGE (a)-[r:READS_FROM]->(b)
SET r.summary = '从工作区技能目录加载技能', r.phase = 'agent_flow';
MATCH (a:Entity {id:'manager_memory'}), (b:Entity {id:'workspace_agent'})
MERGE (a)-[r:READS_FROM]->(b)
SET r.summary = '从工作区记忆目录加载与维护记忆', r.phase = 'agent_flow';
MATCH (a:Entity {id:'channel_web'}), (b:Entity {id:'service_agent_chat'})
MERGE (a)-[r:STREAMS_WITH]->(b)
SET r.summary = 'Web 渠道可通过 SSE 接收 Agent 流式事件', r.phase = 'agent_flow';

MATCH (a:Entity {id:'manager_tools'}), (b:Entity {id:'tool_read'})
MERGE (a)-[r:LOADS]->(b)
SET r.summary = '文件读取工具', r.phase = 'tool_flow';
MATCH (a:Entity {id:'manager_tools'}), (b:Entity {id:'tool_write'})
MERGE (a)-[r:LOADS]->(b)
SET r.summary = '文件写入工具', r.phase = 'tool_flow';
MATCH (a:Entity {id:'manager_tools'}), (b:Entity {id:'tool_edit'})
MERGE (a)-[r:LOADS]->(b)
SET r.summary = '文件编辑工具', r.phase = 'tool_flow';
MATCH (a:Entity {id:'manager_tools'}), (b:Entity {id:'tool_bash'})
MERGE (a)-[r:LOADS]->(b)
SET r.summary = 'shell 执行工具', r.phase = 'tool_flow';
MATCH (a:Entity {id:'manager_tools'}), (b:Entity {id:'tool_ls'})
MERGE (a)-[r:LOADS]->(b)
SET r.summary = '目录浏览工具', r.phase = 'tool_flow';
MATCH (a:Entity {id:'manager_tools'}), (b:Entity {id:'tool_send'})
MERGE (a)-[r:LOADS]->(b)
SET r.summary = '主动消息发送工具', r.phase = 'tool_flow';
MATCH (a:Entity {id:'manager_tools'}), (b:Entity {id:'tool_memory_search'})
MERGE (a)-[r:LOADS]->(b)
SET r.summary = '记忆搜索工具', r.phase = 'tool_flow';
MATCH (a:Entity {id:'manager_tools'}), (b:Entity {id:'tool_memory_get'})
MERGE (a)-[r:LOADS]->(b)
SET r.summary = '记忆读取工具', r.phase = 'tool_flow';
MATCH (a:Entity {id:'manager_tools'}), (b:Entity {id:'tool_web_search'})
MERGE (a)-[r:LOADS]->(b)
SET r.summary = '联网搜索工具', r.phase = 'tool_flow';
MATCH (a:Entity {id:'manager_tools'}), (b:Entity {id:'tool_web_fetch'})
MERGE (a)-[r:LOADS]->(b)
SET r.summary = '网页抓取工具', r.phase = 'tool_flow';
MATCH (a:Entity {id:'manager_tools'}), (b:Entity {id:'tool_vision'})
MERGE (a)-[r:LOADS]->(b)
SET r.summary = '图像理解工具', r.phase = 'tool_flow';
MATCH (a:Entity {id:'manager_tools'}), (b:Entity {id:'tool_env_config'})
MERGE (a)-[r:LOADS]->(b)
SET r.summary = '环境变量工具', r.phase = 'tool_flow';
MATCH (a:Entity {id:'manager_tools'}), (b:Entity {id:'tool_scheduler'})
MERGE (a)-[r:LOADS]->(b)
SET r.summary = '定时任务工具', r.phase = 'tool_flow';
MATCH (a:Entity {id:'tool_memory_search'}), (b:Entity {id:'manager_memory'})
MERGE (a)-[r:READS_FROM]->(b)
SET r.summary = '通过 MemoryManager 搜索长期记忆', r.phase = 'tool_flow';
MATCH (a:Entity {id:'tool_memory_get'}), (b:Entity {id:'manager_memory'})
MERGE (a)-[r:READS_FROM]->(b)
SET r.summary = '通过 MemoryManager 获取记忆正文', r.phase = 'tool_flow';

MATCH (n:Entity)
RETURN count(n) AS total_nodes;
