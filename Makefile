# ============================================================
#  Amy-ai (CowAgent) Makefile
# ============================================================
SHELL := /bin/bash
.SILENT:

ROOT_DIR  := $(shell cd "$(dir $(realpath $(firstword $(MAKEFILE_LIST))))" && pwd)
APP_FILE  := $(ROOT_DIR)/app.py
CONFIG_FILE := $(ROOT_DIR)/config.json
CONFIG_TMPL := $(ROOT_DIR)/config-template.json
DOCKER_COMPOSE := docker compose -f docker/docker-compose.dev.yml

# ANSI colors
cBold  := \033[1m
cRed   := \033[0;31m
cGreen := \033[0;32m
cYellow:= \033[0;33m
cCyan  := \033[0;36m
cReset := \033[0m

# ---- helpers -------------------------------------------------
define _check_config
	@if [ ! -f "$(CONFIG_FILE)" ]; then \
		printf "$(cRed)❌ config.json not found.$(cReset)\n"; \
		printf "$(cYellow)Run: make config$(cReset)\n"; \
		exit 1; \
	fi
endef

define _banner
	printf "\n$(cCyan)━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━$(cReset)\n"
	printf "$(cCyan)   🤖 Amy-ai $(1)$(cReset)\n"
	printf "$(cCyan)━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━$(cReset)\n\n"
endef

# ---- default entry (interactive) ----------------------------
.PHONY: help
help: ## 显示帮助
	@clear
	@printf "$(cBold)$(cCyan)╔══════════════════════════════════════════════════════╗$(cReset)\n"
	@printf "$(cBold)$(cCyan)║              Amy-ai Management Menu                  ║$(cReset)\n"
	@printf "$(cBold)$(cCyan)╚══════════════════════════════════════════════════════╝$(cReset)\n"
	@printf "\n"
	@printf "$(cBold)开发部署:$(cReset)\n"
	@printf "  $(cGreen)make dev-deploy$(cReset)      交互式开发环境部署 (Docker)\n"
	@printf "  $(cGreen)make dev-local$(cReset)        本地开发启动 (uv + python)\n"
	@printf "\n"
	@printf "$(cBold)生产部署:$(cReset)\n"
	@printf "  $(cYellow)make prod-deploy$(cReset)     🚧 占位 - 尚未实现\n"
	@printf "\n"
	@printf "$(cBold)服务管理:$(cReset)\n"
	@printf "  $(cGreen)make start$(cReset)           启动服务\n"
	@printf "  $(cGreen)make stop$(cReset)            停止服务\n"
	@printf "  $(cGreen)make restart$(cReset)         重启服务\n"
	@printf "  $(cGreen)make status$(cReset)          查看状态\n"
	@printf "  $(cGreen)make logs$(cReset)            查看日志\n"
	@printf "\n"
	@printf "$(cBold)配置 & 工具:$(cReset)\n"
	@printf "  $(cGreen)make config$(cReset)          交互式生成 config.json\n"
	@printf "  $(cGreen)make install$(cReset)         安装 Python 依赖 (uv sync)\n"
	@printf "  $(cGreen)make clean$(cReset)           清理临时文件\n"
	@printf "  $(cGreen)make update$(cReset)          更新代码 & 重启\n"
	@printf "\n"
	@printf "运行 $(cGreen)make$(cReset) 不加参数将进入交互式菜单。\n"
	@printf "\n"

# =============================================================
# 交互式菜单 (默认 target)
# =============================================================
.PHONY: menu
menu:
	@clear
	@printf "$(cBold)$(cCyan)╔══════════════════════════════════════════════════════╗$(cReset)\n"
	@printf "$(cBold)$(cCyan)║        Amy-ai 交互式部署菜单                        ║$(cReset)\n"
	@printf "$(cBold)$(cCyan)╚══════════════════════════════════════════════════════╝$(cReset)\n"
	@printf "\n"
	@printf "  $(cGreen)[1]$(cReset) 开发环境部署 (Docker)\n"
	@printf "  $(cGreen)[2]$(cReset) 本地开发启动\n"
	@printf "  $(cGreen)[3]$(cReset) 交互式配置 (生成 config.json)\n"
	@printf "  $(cGreen)[4]$(cReset) 安装依赖\n"
	@printf "  $(cGreen)[5]$(cReset) 启动服务\n"
	@printf "  $(cGreen)[6]$(cReset) 停止服务\n"
	@printf "  $(cGreen)[7]$(cReset) 重启服务\n"
	@printf "  $(cGreen)[8]$(cReset) 查看状态\n"
	@printf "  $(cGreen)[9]$(cReset) 查看日志\n"
	@printf "  $(cGreen)[10]$(cReset) 更新代码 & 重启\n"
	@printf "  $(cYellow)[p]$(cReset) 生产部署 🚧 (占位)\n"
	@printf "  $(cRed)[q]$(cReset) 退出\n"
	@printf "\n"
	@read -p "请选择 [1-10, p, q]: " CHOICE; \
	case $$CHOICE in \
		1)  $(MAKE) dev-deploy ;; \
		2)  $(MAKE) dev-local ;; \
		3)  $(MAKE) config ;; \
		4)  $(MAKE) install ;; \
		5)  $(MAKE) start ;; \
		6)  $(MAKE) stop ;; \
		7)  $(MAKE) restart ;; \
		8)  $(MAKE) status ;; \
		9)  $(MAKE) logs ;; \
		10) $(MAKE) update ;; \
		p)  $(MAKE) prod-deploy ;; \
		q)  printf "$(cGreen)👋 再见！$(cReset)\n"; exit 0 ;; \
		*)  printf "$(cRed)无效选择，请输入 1-10, p, q$(cReset)\n"; exit 1 ;; \
	esac

# 无参数执行时进入菜单（不包括 make xxx 的情况）
.DEFAULT_GOAL := menu

# =============================================================
# 开发部署 (Docker)
# =============================================================
.PHONY: dev-deploy
dev-deploy: ## 交互式开发环境部署 (Docker)
	@$(call _banner,开发环境部署 - Docker)
	@if ! command -v docker &> /dev/null; then \
		printf "$(cRed)❌ Docker 未安装，请先安装 Docker。$(cReset)\n"; \
		exit 1; \
	fi
	@printf "$(cGreen)✅ Docker 已安装$(cReset)\n"
	@# 检查 config.json
	@if [ ! -f "$(CONFIG_FILE)" ]; then \
		printf "$(cYellow)📝 config.json 不存在，将基于模板创建...$(cReset)\n"; \
		cp "$(CONFIG_TMPL)" "$(CONFIG_FILE)"; \
		printf "$(cGreen)✅ 已创建 config.json，请根据需要编辑配置。$(cReset)\n"; \
		read -p "是否现在编辑 config.json? [Y/n]: " EDIT_NOW; \
		if [[ ! $$EDIT_NOW == [Nn]* ]]; then \
			$${EDITOR:-nano} "$(CONFIG_FILE)"; \
		fi; \
	else \
		printf "$(cGreen)✅ config.json 已存在$(cReset)\n"; \
	fi
	@# 检查 .env (docker-compose 用)
	@if [ ! -f "$(ROOT_DIR)/.env" ]; then \
		printf "$(cYellow)📝 .env 文件不存在，创建默认开发配置...$(cReset)\n"; \
		printf "CHANNEL_TYPE=web\nWEB_PORT=9899\nMODEL=deepseek-chat\nAGENT=True\nAGENT_MAX_CONTEXT_TOKENS=40000\n" > "$(ROOT_DIR)/.env"; \
		printf "$(cGreen)✅ 已创建 .env 文件$(cReset)\n"; \
		read -p "是否现在编辑 .env? [Y/n]: " EDIT_ENV; \
		if [[ ! $$EDIT_ENV == [Nn]* ]]; then \
			$${EDITOR:-nano} "$(ROOT_DIR)/.env"; \
		fi; \
	else \
		printf "$(cGreen)✅ .env 文件已存在$(cReset)\n"; \
	fi
	@printf "\n$(cCyan)🐳 构建并启动 Docker 开发环境...$(cReset)\n"
	@cd "$(ROOT_DIR)" && $(DOCKER_COMPOSE) up -d --build
	@printf "\n$(cGreen)✅ 开发环境已启动！$(cReset)\n"
	@printf "$(cGreen)   Web 控制台: http://localhost:9899/chat$(cReset)\n"
	@printf "$(cGreen)   管理命令: make [start|stop|restart|status|logs]$(cReset)\n"

# =============================================================
# 本地开发启动 (无 Docker)
# =============================================================
.PHONY: dev-local
dev-local: ## 本地开发启动 (uv + python)
	@$(call _banner,本地开发启动)
	@# 检查 Python
	@PY=$$(command -v python3 || command -v python || echo ""); \
	if [ -z "$$PY" ]; then \
		printf "$(cRed)❌ 未找到 Python，请安装 Python >= 3.10$(cReset)\n"; \
		exit 1; \
	fi; \
	VER=$$($$PY --version 2>&1 | grep -oP '\d+\.\d+' | head -1); \
	printf "$(cGreen)✅ Python $${VER}$(cReset)\n"
	@# 检查 uv
	@if ! command -v uv &> /dev/null; then \
		printf "$(cYellow)📦 uv 未安装，尝试安装...$(cReset)\n"; \
		pip install uv; \
	fi
	@printf "$(cGreen)✅ uv 就绪$(cReset)\n"
	@# 检查 config.json
	@if [ ! -f "$(CONFIG_FILE)" ]; then \
		printf "$(cYellow)📝 config.json 不存在，运行交互式配置...$(cReset)\n"; \
		$(MAKE) config; \
	fi
	@# 安装依赖
	@printf "\n$(cCyan)📦 同步依赖...$(cReset)\n"; \
	cd "$(ROOT_DIR)" && uv sync
	@# 启动
	@printf "\n$(cCyan)🚀 启动 Amy-ai (本地模式)...$(cReset)\n"
	@cd "$(ROOT_DIR)" && uv run python app.py

# =============================================================
# 生产部署 (占位)
# =============================================================
.PHONY: prod-deploy
prod-deploy: ## 生产部署 (占位 - 待实现)
	@$(call _banner,生产部署)
	@printf "$(cYellow)╔════════════════════════════════════════════╗$(cReset)\n"
	@printf "$(cYellow)║  🚧  生产部署尚未实现，敬请期待。         ║$(cReset)\n"
	@printf "$(cYellow)║                                          ║$(cReset)\n"
	@printf "$(cYellow)║  计划功能:                                ║$(cReset)\n"
	@printf "$(cYellow)║  • 多阶段 Docker 构建 (prod 镜像)        ║$(cReset)\n"
	@printf "$(cYellow)║  • 环境变量安全管理                        ║$(cReset)\n"
	@printf "$(cYellow)║  • 健康检查 & 自动重启                     ║$(cReset)\n"
	@printf "$(cYellow)║  • Nginx 反向代理模板                      ║$(cReset)\n"
	@printf "$(cYellow)║  • 日志收集与轮转                          ║$(cReset)\n"
	@printf "$(cYellow)╚════════════════════════════════════════════╝$(cReset)\n"
	@printf "\n当前请使用 $(cGreen)make dev-deploy$(cReset) 进行开发部署。\n"

# =============================================================
# 服务管理
# =============================================================
.PHONY: start
start: ## 启动服务
	@$(call _banner,启动服务)
	@$(call _check_config)
	@# 检查是否 Docker 环境
	@if [ -f "$(ROOT_DIR)/docker/docker-compose.dev.yml" ] && docker ps &> /dev/null 2>&1; then \
		if docker ps --format '{{.Names}}' | grep -q "amy-dev"; then \
			printf "$(cYellow)⚠️  amy-dev 容器已在运行$(cReset)\n"; \
		else \
			IS_RUNNING=$$(docker ps -a --format '{{.Names}} {{.Status}}' | grep "amy-dev" | grep "Exited" || true); \
			if [ -n "$$IS_RUNNING" ]; then \
				printf "$(cCyan)🐳 启动已存在的容器...$(cReset)\n"; \
				cd "$(ROOT_DIR)" && $(DOCKER_COMPOSE) start; \
			else \
				printf "$(cCyan)🐳 创建并启动容器...$(cReset)\n"; \
				cd "$(ROOT_DIR)" && $(DOCKER_COMPOSE) up -d; \
			fi; \
			printf "$(cGreen)✅ 服务已启动$(cReset)\n"; \
			printf "$(cGreen)   Web: http://localhost:9899/chat$(cReset)\n"; \
		fi; \
	else \
		printf "$(cYellow)非 Docker 环境，使用本地启动...$(cReset)\n"; \
		$(MAKE) dev-local; \
	fi

.PHONY: stop
stop: ## 停止服务
	@$(call _banner,停止服务)
	@if docker ps --format '{{.Names}}' 2>/dev/null | grep -q "amy-dev"; then \
		printf "$(cCyan)🐳 停止 Docker 容器...$(cReset)\n"; \
		cd "$(ROOT_DIR)" && $(DOCKER_COMPOSE) stop; \
		printf "$(cGreen)✅ 服务已停止$(cReset)\n"; \
	else \
		printf "$(cYellow)⚠️  未找到运行中的 Docker 容器$(cReset)\n"; \
	fi

.PHONY: restart
restart: ## 重启服务
	@$(call _banner,重启服务)
	@if docker ps --format '{{.Names}}' 2>/dev/null | grep -q "amy-dev"; then \
		cd "$(ROOT_DIR)" && $(DOCKER_COMPOSE) restart; \
		printf "$(cGreen)✅ 服务已重启$(cReset)\n"; \
	else \
		printf "$(cYellow)服务未运行，直接启动...$(cReset)\n"; \
		$(MAKE) start; \
	fi

.PHONY: status
status: ## 查看服务状态
	@$(call _banner,服务状态)
	@if docker ps --format '{{.Names}}' 2>/dev/null | grep -q "amy-dev"; then \
		printf "$(cGreen)状态: ✅ 运行中 (Docker)$(cReset)\n"; \
		printf "\n$(cCyan)容器详情:$(cReset)\n"; \
		docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}" --filter "name=amy-dev"; \
		printf "\n$(cCyan)最近日志:$(cReset)\n"; \
		docker logs --tail 20 amy-dev 2>/dev/null || true; \
	else \
		if docker ps -a --format '{{.Names}}' 2>/dev/null | grep -q "amy-dev"; then \
			printf "$(cYellow)状态: ⏸  已停止 (Docker 容器存在)$(cReset)\n"; \
			printf "  运行 $(cGreen)make start$(cReset) 启动服务\n"; \
		else \
			printf "$(cYellow)状态: ⭐ 未部署 (无容器)$(cReset)\n"; \
		fi; \
	fi
	@# config info
	@if [ -f "$(CONFIG_FILE)" ]; then \
		printf "\n$(cCyan)配置信息:$(cReset)\n"; \
		MODEL=$$(grep -o '"model"[[:space:]]*:[[:space:]]*"[^"]*"' "$(CONFIG_FILE)" 2>/dev/null | cut -d'"' -f4 || echo "N/A"); \
		CH=$$(grep -o '"channel_type"[[:space:]]*:[[:space:]]*"[^"]*"' "$(CONFIG_FILE)" 2>/dev/null | cut -d'"' -f4 || echo "N/A"); \
		printf "  Model:   %s\n" "$$MODEL"; \
		printf "  Channel: %s\n" "$$CH"; \
	fi

.PHONY: logs
logs: ## 查看日志 (Docker 容器)
	@if docker ps --format '{{.Names}}' 2>/dev/null | grep -q "amy-dev"; then \
		printf "$(cCyan)📋 Docker 容器日志 (Ctrl+C 退出):$(cReset)\n"; \
		docker logs --tail 50 -f amy-dev; \
	else \
		printf "$(cYellow)⚠️  未找到运行中的容器$(cReset)\n"; \
		printf "  运行 $(cGreen)make start$(cReset) 启动后再查看日志\n"; \
	fi

.PHONY: update
update: ## 更新代码 & 重启
	@$(call _banner,更新代码)
	@cd "$(ROOT_DIR)" && if [ -d .git ]; then \
		printf "$(cGreen)🔄 拉取最新代码...$(cReset)\n"; \
		git pull; \
		printf "$(cGreen)✅ 代码已更新$(cReset)\n"; \
	else \
		printf "$(cRed)❌ 非 git 仓库，无法自动更新$(cReset)\n"; \
		exit 1; \
	fi
	@if docker ps --format '{{.Names}}' 2>/dev/null | grep -q "amy-dev"; then \
		printf "\n$(cCyan)🐳 重建并重启容器...$(cReset)\n"; \
		cd "$(ROOT_DIR)" && $(DOCKER_COMPOSE) up -d --build; \
		printf "$(cGreen)✅ 更新完成，服务已重启$(cReset)\n"; \
	else \
		printf "$(cGreen)✅ 更新完成 (服务未运行)$(cReset)\n"; \
	fi

# =============================================================
# 配置 & 依赖
# =============================================================
.PHONY: config
config: ## 交互式生成 config.json
	@$(call _banner,交互式配置)
	@if [ -f "$(CONFIG_FILE)" ]; then \
		printf "$(cYellow)⚠️  config.json 已存在$(cReset)\n"; \
		read -p "是否备份并重新配置? [y/N]: " DO_RECONFIG; \
		if [[ ! $$DO_RECONFIG == [Yy]* ]]; then \
			printf "$(cGreen)保留现有配置，退出。$(cReset)\n"; \
			exit 0; \
		fi; \
		cp "$(CONFIG_FILE)" "$(CONFIG_FILE).backup.$$(date +%s)"; \
		printf "$(cGreen)✅ 已备份旧配置$(cReset)\n"; \
	fi
	@printf "\n$(cBold)=== 模型配置 ===$(cReset)\n"
	@printf "  $(cGreen)[1]$(cReset) DeepSeek\n"
	@printf "  $(cGreen)[2]$(cReset) MiniMax\n"
	@printf "  $(cGreen)[3]$(cReset) Zhipu AI\n"
	@printf "  $(cGreen)[4]$(cReset) Kimi (Moonshot)\n"
	@printf "  $(cGreen)[5]$(cReset) Doubao (Volcengine Ark)\n"
	@printf "  $(cGreen)[6]$(cReset) Qwen (DashScope)\n"
	@printf "  $(cGreen)[7]$(cReset) Claude (Anthropic)\n"
	@printf "  $(cGreen)[8]$(cReset) Gemini (Google)\n"
	@printf "  $(cGreen)[9]$(cReset) OpenAI GPT\n"
	@read -p "选择模型 [1-9, 默认 1]: " MC; MC=$${MC:-1}; \
	 case $$MC in \
		1) MODEL="deepseek-chat"; \
		   read -p "DeepSeek API Key: " AK; \
		   read -p "API Base [默认: https://api.deepseek.com/v1]: " AB; \
		   AB=$${AB:-https://api.deepseek.com/v1}; \
		   printf '{\n  "model": "%s",\n  "open_ai_api_key": "%s",\n  "open_ai_api_base": "%s",\n  "channel_type": "web",\n  "web_port": 9899,\n  "agent": true\n}\n' "$$MODEL" "$$AK" "$$AB" > "$(CONFIG_FILE)" ;; \
		2) MODEL="MiniMax-M2.7"; \
		   read -p "MiniMax API Key: " AK; \
		   printf '{\n  "model": "%s",\n  "minimax_api_key": "%s",\n  "Minimax_base_url": "",\n  "Minimax_group_id": "",\n  "channel_type": "web",\n  "web_port": 9899,\n  "agent": true\n}\n' "$$MODEL" "$$AK" > "$(CONFIG_FILE)" ;; \
		3) MODEL="glm-5-turbo"; \
		   read -p "Zhipu API Key: " AK; \
		   printf '{\n  "model": "%s",\n  "zhipu_ai_api_key": "%s",\n  "zhipu_ai_api_base": "https://open.bigmodel.cn/api/paas/v4",\n  "channel_type": "web",\n  "web_port": 9899,\n  "agent": true\n}\n' "$$MODEL" "$$AK" > "$(CONFIG_FILE)" ;; \
		4) MODEL="kimi-k2.5"; \
		   read -p "Moonshot API Key: " AK; \
		   printf '{\n  "model": "%s",\n  "moonshot_api_key": "%s",\n  "moonshot_base_url": "https://api.moonshot.cn/v1",\n  "channel_type": "web",\n  "web_port": 9899,\n  "agent": true\n}\n' "$$MODEL" "$$AK" > "$(CONFIG_FILE)" ;; \
		5) MODEL="doubao-seed-2-0-code-preview-260215"; \
		   read -p "Ark API Key: " AK; \
		   printf '{\n  "model": "%s",\n  "ark_api_key": "%s",\n  "ark_base_url": "https://ark.cn-beijing.volces.com/api/v3",\n  "channel_type": "web",\n  "web_port": 9899,\n  "agent": true\n}\n' "$$MODEL" "$$AK" > "$(CONFIG_FILE)" ;; \
		6) MODEL="qwen3.5-plus"; \
		   read -p "DashScope API Key: " AK; \
		   printf '{\n  "model": "%s",\n  "dashscope_api_key": "%s",\n  "channel_type": "web",\n  "web_port": 9899,\n  "agent": true\n}\n' "$$MODEL" "$$AK" > "$(CONFIG_FILE)" ;; \
		7) MODEL="claude-sonnet-4-6"; \
		   read -p "Claude API Key: " AK; \
		   read -p "API Base [默认: https://api.anthropic.com/v1]: " AB; \
		   AB=$${AB:-https://api.anthropic.com/v1}; \
		   printf '{\n  "model": "%s",\n  "claude_api_key": "%s",\n  "claude_api_base": "%s",\n  "channel_type": "web",\n  "web_port": 9899,\n  "agent": true\n}\n' "$$MODEL" "$$AK" "$$AB" > "$(CONFIG_FILE)" ;; \
		8) MODEL="gemini-3.1-pro-preview"; \
		   read -p "Gemini API Key: " AK; \
		   printf '{\n  "model": "%s",\n  "gemini_api_key": "%s",\n  "gemini_api_base": "https://generativelanguage.googleapis.com",\n  "channel_type": "web",\n  "web_port": 9899,\n  "agent": true\n}\n' "$$MODEL" "$$AK" > "$(CONFIG_FILE)" ;; \
		9) MODEL="gpt-5.4"; \
		   read -p "OpenAI API Key: " AK; \
		   read -p "API Base [默认: https://api.openai.com/v1]: " AB; \
		   AB=$${AB:-https://api.openai.com/v1}; \
		   printf '{\n  "model": "%s",\n  "open_ai_api_key": "%s",\n  "open_ai_api_base": "%s",\n  "channel_type": "web",\n  "web_port": 9899,\n  "agent": true\n}\n' "$$MODEL" "$$AK" "$$AB" > "$(CONFIG_FILE)" ;; \
		*) printf "$(cRed)无效选择$(cReset)\n"; exit 1 ;; \
	 esac
	@printf "\n$(cBold)=== 渠道配置 ===$(cReset)\n"
	@printf "  $(cGreen)[1]$(cReset) Web (默认)\n"
	@printf "  $(cGreen)[2]$(cReset) 飞书\n"
	@printf "  $(cGreen)[3]$(cReset) 钉钉\n"
	@printf "  $(cGreen)[4]$(cReset) 企微智能机器人\n"
	@printf "  $(cGreen)[5]$(cReset) QQ\n"
	@printf "  $(cGreen)[6]$(cReset) 微信\n"
	@printf "  $(cGreen)[7]$(cReset) 企微自建应用\n"
	@read -p "选择渠道 [1-7, 默认 1]: " CC; CC=$${CC:-1}; \
	if [ "$$CC" != "1" ]; then \
		case $$CC in \
			2) printf "$(cYellow)飞书渠道请在 config.json 中手动填写 feishu_app_id / feishu_app_secret$(cReset)\n" ;; \
			3) printf "$(cYellow)钉钉渠道请在 config.json 中手动填写 dingtalk_client_id / dingtalk_client_secret$(cReset)\n" ;; \
			4) printf "$(cYellow)企微机器人渠道请在 config.json 中手动填写 wecom_bot_id / wecom_bot_secret$(cReset)\n" ;; \
			5) printf "$(cYellow)QQ 渠道请在 config.json 中手动填写 qq_app_id / qq_app_secret$(cReset)\n" ;; \
			6) printf "$(cYellow)微信渠道请在 config.json 中手动填写 weixin 相关配置$(cReset)\n" ;; \
			7) printf "$(cYellow)企微自建应用渠道请在 config.json 中手动填写 wechatcom 相关配置$(cReset)\n" ;; \
		esac; \
	fi
	@printf "\n$(cGreen)✅ 配置文件已生成: $(CONFIG_FILE)$(cReset)\n"
	@printf "$(cYellow)💡 提示: 更多高级配置项请手动编辑 config.json$(cReset)\n"

.PHONY: install
install: ## 安装 Python 依赖
	@$(call _banner,安装依赖)
	@if ! command -v uv &> /dev/null; then \
		printf "$(cYellow)📦 uv 未安装，使用 pip 安装...$(cReset)\n"; \
		pip install uv; \
	fi
	@cd "$(ROOT_DIR)" && uv sync
	@printf "$(cGreen)✅ 依赖安装完成$(cReset)\n"

.PHONY: clean
clean: ## 清理临时文件
	@$(call _banner,清理)
	@printf "$(cGreen)🧹 清理 __pycache__ 目录...$(cReset)\n"
	@cd "$(ROOT_DIR)" && find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	@printf "$(cGreen)🧹 清理 .pyc 文件...$(cReset)\n"
	@cd "$(ROOT_DIR)" && find . -type f -name "*.pyc" -delete 2>/dev/null || true
	@printf "$(cGreen)🧹 清理 .egg-info...$(cReset)\n"
	@cd "$(ROOT_DIR)" && find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
	@printf "$(cGreen)✅ 清理完成$(cReset)\n"
