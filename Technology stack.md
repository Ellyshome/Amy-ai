我需要查看项目的关键文件来了解技术栈，包括 requirements.txt、package.json 等依赖文件，以及项目的主要代码结构。
        
            
toolName: file_search
            
status: success
          
file_pattern: *requirements*.txt
          

        
            
toolName: file_search
            
status: success
          
file_pattern: package.json
          

        
            
toolName: file_search
            
status: success
          
file_pattern: pyproject.toml
          

        
            
toolName: file_search
            
status: success
          
file_pattern: setup.py
          
我找到了 pyproject.toml 文件，让我读取它来了解项目的依赖和技术栈。
        
            
toolName: view_files
            
status: success
          
            
filePath: d:\app\Amy-ai\pyproject.toml
          

        
            
toolName: view_folder
            
status: success
          
d:\app\Amy-ai
          
根据 pyproject.toml 和项目结构，我可以为您详细介绍本项目的技术栈：

## 核心技术栈

### 1. **编程语言**
- **Python 3.10+**：项目主要使用 Python 开发，要求 Python 版本 >= 3.10

### 2. **核心依赖库**

#### AI/LLM 相关
- **openai (0.27.8)**：OpenAI API 客户端，用于调用 GPT 系列模型
- **agentmesh-sdk (>=0.1.3)**：Agent Mesh SDK，用于构建 Agent 系统
- **dashscope (>=1.25.15)**：阿里云灵积平台 SDK，用于调用通义千问等模型
- **linkai (>=0.0.6.0)**：LinkAI 平台 SDK
- **zai-sdk (>=0.2.2)**：智谱 AI SDK

#### Web 框架与网络
- **web-py (>=0.62)**：轻量级 Web 框架，用于 Web 控制台
- **aiohttp (>=3.8.6,<3.10)**：异步 HTTP 客户端/服务器
- **requests (>=2.28.2)**：同步 HTTP 请求库
- **websocket-client (>=1.4.0)**：WebSocket 客户端

#### 企业通讯集成
- **lark-oapi (>=1.5.3)**：飞书开放平台 SDK
- **dingtalk-stream (>=0.20.0)**：钉钉 Stream SDK
- **wechatpy (>=1.8.18)**：微信开发 SDK

#### 数据处理与工具
- **pillow (>=12.1.1)**：图像处理库
- **pyyaml (>=6.0)**：YAML 解析
- **python-dotenv (>=1.0.0)**：环境变量管理
- **pycryptodome (>=3.23.0)**：加密库
- **chardet (>=5.1.0)**：字符编码检测
- **croniter (>=2.0.0)**：Cron 表达式解析
- **qrcode (>=8.2)**：二维码生成

### 3. **架构设计**

#### 模块结构
```
项目根目录
├── agent/          # Agent 核心系统
│   ├── protocol/   # Agent 协议定义（Agent、任务、消息等）
│   ├── tools/      # 工具集（bash、web_search、scheduler 等）
│   ├── skills/     # 技能管理系统
│   ├── memory/     # 记忆管理系统
│   └── chat/       # 聊天服务
├── bridge/         # 桥接层（连接 Agent 与 COW）
├── channel/        # 消息通道（飞书、钉钉、微信等）
├── models/         # AI 模型实现（OpenAI、Claude、Gemini 等）
├── plugins/        # 插件系统
├── voice/          # 语音处理（TTS/ASR）
├── common/         # 公共工具与常量
└── docs/           # 文档（多语言支持）
```

### 4. **支持的 AI 模型**
- OpenAI (GPT-3.5/4/4o/o1 系列)
- Claude (Anthropic)
- Gemini (Google)
- 通义千问 (阿里云)
- 文心一言 (百度)
- 讯飞星火
- 智谱 GLM
- Moonshot (Kimi)
- MiniMax
- DeepSeek
- 豆包 (火山方舟)
- ModelScope

### 5. **支持的消息渠道**
- **飞书** (Lark/Feishu)
- **钉钉** (DingTalk)
- **企业微信** (WeCom)
- **微信公众号** (WeChat MP)
- **微信** (Weixin)
- **QQ**
- **Web** (Web 控制台)
- **终端** (Terminal)

### 6. **Agent 工具集**
- **bash**：执行 Shell 命令
- **web_search**：网络搜索
- **web_fetch**：网页抓取
- **read**：文件读取
- **write**：文件写入
- **edit**：文件编辑
- **ls**：目录列表
- **scheduler**：任务调度
- **memory**：记忆管理
- **send**：消息发送
- **vision**：图像识别

### 7. **部署方式**
- **Docker**：支持容器化部署
- **Shell 脚本**：提供 start.sh、run.sh 等管理脚本
- **Python 应用**：可直接运行 app.py 或 main.py

### 8. **开发特性**
- **插件系统**：支持自定义插件扩展
- **技能系统**：支持通过 Markdown 文件定义技能
- **记忆管理**：支持长期记忆和短期记忆
- **多语言文档**：支持中文、英文、日文文档
- **流式传输**：支持 SSE (Server-Sent Events) 实时传输

这是一个功能完善的 AI Agent 平台，集成了多种主流 AI 模型和通讯渠道，提供了丰富的工具集和扩展机制。