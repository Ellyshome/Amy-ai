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
		printf "$(cRed)config.json not found.$(cReset)\n"; \
		printf "$(cYellow)Run: make config$(cReset)\n"; \
		exit 1; \
	fi
endef

define _banner
	printf "\n$(cCyan)========================================$(cReset)\n"
	printf "$(cCyan)   Amy-ai $(1)$(cReset)\n"
	printf "$(cCyan)========================================$(cReset)\n\n"
endef

# ---- default entry (interactive) ----------------------------
.PHONY: help
help: ## 显示帮助
	@clear
	@printf "$(cBold)$(cCyan)+------------------------------------------------------+$(cReset)\n"
	@printf "$(cBold)$(cCyan)|              Amy-ai Management Menu                  |$(cReset)\n"
	@printf "$(cBold)$(cCyan)+------------------------------------------------------+$(cReset)\n"
	@printf "\n"
	@printf "$(cBold)开发部署:$(cReset)\n"
	@printf "  $(cGreen)make dev-deploy$(cReset)      交互式开发环境部署 (Docker)\n"
	@printf "  $(cGreen)make dev-local$(cReset)        本地开发启动 (uv + python)\n"
	@printf "\n"
	@printf "$(cBold)生产部署:$(cReset)\n"
	@printf "  $(cYellow)make prod-deploy$(cReset)     占位 - 尚未实现\n"
	@printf "\n"
	@printf "$(cBold)服务管理:$(cReset)\n"
	@printf "  $(cGreen)make start$(cReset)           启动服务\n"
	@printf "  $(cGreen)make stop$(cReset)            停止服务\n"
	@printf "  $(cGreen)make restart$(cReset)         重启服务\n"
	@printf "  $(cGreen)make status$(cReset)          查看状态\n"
	@printf "  $(cGreen)make logs$(cReset)            查看日志\n"
	@printf "\n"
	@printf "$(cBold)配置 & 工具:$(cReset)\n"
	@printf "  $(cGreen)make config$(cReset)          交互式配置 (选模型/渠道/全部)\n"
	@printf "  $(cGreen)make config-model$(cReset)    仅配置 AI 模型\n"
	@printf "  $(cGreen)make config-channel$(cReset)  仅配置消息渠道\n"
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
	@printf "$(cBold)$(cCyan)+------------------------------------------------------+$(cReset)\n"
	@printf "$(cBold)$(cCyan)|        Amy-ai 交互式部署菜单                         |$(cReset)\n"
	@printf "$(cBold)$(cCyan)+------------------------------------------------------+$(cReset)\n"
	@printf "\n"
	@printf "  $(cGreen)[1]$(cReset) 开发环境部署 (Docker)\n"
	@printf "  $(cGreen)[2]$(cReset) 本地开发启动\n"
	@printf "  $(cGreen)[3]$(cReset) 配置 AI 模型\n"
	@printf "  $(cGreen)[4]$(cReset) 配置消息渠道\n"
	@printf "  $(cGreen)[5]$(cReset) 全部重新配置\n"
	@printf "  $(cGreen)[6]$(cReset) 安装依赖\n"
	@printf "  $(cGreen)[7]$(cReset) 启动服务\n"
	@printf "  $(cGreen)[8]$(cReset) 停止服务\n"
	@printf "  $(cGreen)[9]$(cReset) 重启服务\n"
	@printf "  $(cGreen)[s]$(cReset) 查看状态\n"
	@printf "  $(cGreen)[l]$(cReset) 查看日志\n"
	@printf "  $(cGreen)[u]$(cReset) 更新代码 & 重启\n"
	@printf "  $(cYellow)[p]$(cReset) 生产部署 (占位)\n"
	@printf "  $(cRed)[q]$(cReset) 退出\n"
	@printf "\n"
	@read -p "请选择: " CHOICE; \
	case $$CHOICE in \
		1)  $(MAKE) dev-deploy ;; \
		2)  $(MAKE) dev-local ;; \
		3)  $(MAKE) config-model ;; \
		4)  $(MAKE) config-channel ;; \
		5)  $(MAKE) config ;; \
		6)  $(MAKE) install ;; \
		7)  $(MAKE) start ;; \
		8)  $(MAKE) stop ;; \
		9)  $(MAKE) restart ;; \
		s)  $(MAKE) status ;; \
		l)  $(MAKE) logs ;; \
		u)  $(MAKE) update ;; \
		p)  $(MAKE) prod-deploy ;; \
		q)  printf "$(cGreen)再见!$(cReset)\n"; exit 0 ;; \
		*)  printf "$(cRed)无效选择$(cReset)\n"; exit 1 ;; \
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
		printf "$(cRed)Docker 未安装，请先安装 Docker。$(cReset)\n"; \
		exit 1; \
	fi
	@printf "$(cGreen)Docker 已安装$(cReset)\n"
	@# 检查 config.json
	@if [ ! -f "$(CONFIG_FILE)" ]; then \
		printf "$(cYellow)config.json 不存在，将基于模板创建...$(cReset)\n"; \
		cp "$(CONFIG_TMPL)" "$(CONFIG_FILE)"; \
		printf "$(cGreen)已创建 config.json，请根据需要编辑配置。$(cReset)\n"; \
		read -p "是否现在编辑 config.json? [Y/n]: " EDIT_NOW; \
		if [[ ! $$EDIT_NOW == [Nn]* ]]; then \
			$${EDITOR:-nano} "$(CONFIG_FILE)"; \
		fi; \
	else \
		printf "$(cGreen)config.json 已存在$(cReset)\n"; \
	fi
	@# 检查 .env (docker-compose 用)
	@if [ ! -f "$(ROOT_DIR)/.env" ]; then \
		printf "$(cYellow).env 文件不存在，创建默认开发配置...$(cReset)\n"; \
		printf "CHANNEL_TYPE=web\nWEB_PORT=9899\nMODEL=deepseek-chat\nAGENT=True\nAGENT_MAX_CONTEXT_TOKENS=40000\n" > "$(ROOT_DIR)/.env"; \
		printf "$(cGreen)已创建 .env 文件$(cReset)\n"; \
		read -p "是否现在编辑 .env? [Y/n]: " EDIT_ENV; \
		if [[ ! $$EDIT_ENV == [Nn]* ]]; then \
			$${EDITOR:-nano} "$(ROOT_DIR)/.env"; \
		fi; \
	else \
		printf "$(cGreen).env 文件已存在$(cReset)\n"; \
	fi
	@printf "\n$(cCyan)构建并启动 Docker 开发环境...$(cReset)\n"
	@cd "$(ROOT_DIR)" && $(DOCKER_COMPOSE) up -d --build
	@printf "\n$(cGreen)开发环境已启动！$(cReset)\n"
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
		printf "$(cRed)未找到 Python，请安装 Python >= 3.10$(cReset)\n"; \
		exit 1; \
	fi; \
	VER=$$($$PY --version 2>&1 | grep -oP '\d+\.\d+' | head -1); \
	printf "$(cGreen)Python $${VER}$(cReset)\n"
	@# 检查 uv
	@if ! command -v uv &> /dev/null; then \
		printf "$(cYellow)uv 未安装，尝试安装...$(cReset)\n"; \
		pip install uv; \
	fi
	@printf "$(cGreen)uv 就绪$(cReset)\n"
	@# 检查 config.json
	@if [ ! -f "$(CONFIG_FILE)" ]; then \
		printf "$(cYellow)config.json 不存在，运行交互式配置...$(cReset)\n"; \
		$(MAKE) config; \
	fi
	@# 检测 venv 路径: 优先使用已激活的 VIRTUAL_ENV，否则默认 .venv
	@VENV=$${VIRTUAL_ENV:-}; \
		if [ -n "$$VENV" ]; then \
			printf "$(cGreen)使用已有虚拟环境: $$VENV$(cReset)\n"; \
			export UV_PROJECT_ENVIRONMENT="$$VENV"; \
		fi; \
		printf "\n$(cCyan)同步依赖...$(cReset)\n"; \
		cd "$(ROOT_DIR)" && uv sync
	@# 启动
	@printf "\n$(cCyan)启动 Amy-ai (本地模式)...$(cReset)\n"
	@VENV=$${VIRTUAL_ENV:-}; \
		if [ -n "$$VENV" ]; then \
			export UV_PROJECT_ENVIRONMENT="$$VENV"; \
		fi; \
		cd "$(ROOT_DIR)" && uv run python app.py

# =============================================================
# 生产部署 (占位)
# =============================================================
.PHONY: prod-deploy
prod-deploy: ## 生产部署 (占位 - 待实现)
	@$(call _banner,生产部署)
	@printf "$(cYellow)+----------------------------------------------+$(cReset)\n"
	@printf "$(cYellow)|  生产部署尚未实现，敬请期待。                |$(cReset)\n"
	@printf "$(cYellow)|                                              |$(cReset)\n"
	@printf "$(cYellow)|  计划功能:                                    |$(cReset)\n"
	@printf "$(cYellow)|  * 多阶段 Docker 构建 (prod 镜像)            |$(cReset)\n"
	@printf "$(cYellow)|  * 环境变量安全管理                           |$(cReset)\n"
	@printf "$(cYellow)|  * 健康检查 & 自动重启                        |$(cReset)\n"
	@printf "$(cYellow)|  * Nginx 反向代理模板                         |$(cReset)\n"
	@printf "$(cYellow)|  * 日志收集与轮转                             |$(cReset)\n"
	@printf "$(cYellow)+----------------------------------------------+$(cReset)\n"
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
			printf "$(cYellow)amy-dev 容器已在运行$(cReset)\n"; \
		else \
			IS_RUNNING=$$(docker ps -a --format '{{.Names}} {{.Status}}' | grep "amy-dev" | grep "Exited" || true); \
			if [ -n "$$IS_RUNNING" ]; then \
				printf "$(cCyan)启动已存在的容器...$(cReset)\n"; \
				cd "$(ROOT_DIR)" && $(DOCKER_COMPOSE) start; \
			else \
				printf "$(cCyan)创建并启动容器...$(cReset)\n"; \
				cd "$(ROOT_DIR)" && $(DOCKER_COMPOSE) up -d; \
			fi; \
			printf "$(cGreen)服务已启动$(cReset)\n"; \
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
		printf "$(cCyan)停止 Docker 容器...$(cReset)\n"; \
		cd "$(ROOT_DIR)" && $(DOCKER_COMPOSE) stop; \
		printf "$(cGreen)服务已停止$(cReset)\n"; \
	else \
		printf "$(cYellow)未找到运行中的 Docker 容器$(cReset)\n"; \
	fi

.PHONY: restart
restart: ## 重启服务
	@$(call _banner,重启服务)
	@if docker ps --format '{{.Names}}' 2>/dev/null | grep -q "amy-dev"; then \
		cd "$(ROOT_DIR)" && $(DOCKER_COMPOSE) restart; \
		printf "$(cGreen)服务已重启$(cReset)\n"; \
	else \
		printf "$(cYellow)服务未运行，直接启动...$(cReset)\n"; \
		$(MAKE) start; \
	fi

.PHONY: status
status: ## 查看服务状态
	@$(call _banner,服务状态)
	@if docker ps --format '{{.Names}}' 2>/dev/null | grep -q "amy-dev"; then \
		printf "$(cGreen)状态: 运行中 (Docker)$(cReset)\n"; \
		printf "\n$(cCyan)容器详情:$(cReset)\n"; \
		docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}" --filter "name=amy-dev"; \
		printf "\n$(cCyan)最近日志:$(cReset)\n"; \
		docker logs --tail 20 amy-dev 2>/dev/null || true; \
	else \
		if docker ps -a --format '{{.Names}}' 2>/dev/null | grep -q "amy-dev"; then \
			printf "$(cYellow)状态: 已停止 (Docker 容器存在)$(cReset)\n"; \
			printf "  运行 $(cGreen)make start$(cReset) 启动服务\n"; \
		else \
			printf "$(cYellow)状态: 未部署 (无容器)$(cReset)\n"; \
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
		printf "$(cCyan)Docker 容器日志 (Ctrl+C 退出):$(cReset)\n"; \
		docker logs --tail 50 -f amy-dev; \
	else \
		printf "$(cYellow)未找到运行中的容器$(cReset)\n"; \
		printf "  运行 $(cGreen)make start$(cReset) 启动后再查看日志\n"; \
	fi

.PHONY: update
update: ## 更新代码 & 重启
	@$(call _banner,更新代码)
	@cd "$(ROOT_DIR)" && if [ -d .git ]; then \
		printf "$(cGreen)拉取最新代码...$(cReset)\n"; \
		git pull; \
		printf "$(cGreen)代码已更新$(cReset)\n"; \
	else \
		printf "$(cRed)非 git 仓库，无法自动更新$(cReset)\n"; \
		exit 1; \
	fi
	@if docker ps --format '{{.Names}}' 2>/dev/null | grep -q "amy-dev"; then \
		printf "\n$(cCyan)重建并重启容器...$(cReset)\n"; \
		cd "$(ROOT_DIR)" && $(DOCKER_COMPOSE) up -d --build; \
		printf "$(cGreen)更新完成，服务已重启$(cReset)\n"; \
	else \
		printf "$(cGreen)更新完成 (服务未运行)$(cReset)\n"; \
	fi

# =============================================================
# 配置 & 依赖
# =============================================================

# --- config: 总入口，选择走哪条通道 ---
.PHONY: config
config: ## 交互式配置 (选择配置范围)
	@$(call _banner,交互式配置)
	@# 显示当前配置概览
	@if [ -f "$(CONFIG_FILE)" ]; then \
		printf "$(cCyan)当前配置:$(cReset)\n"; \
		MODEL=$$(grep -o '"model"[[:space:]]*:[[:space:]]*"[^"]*"' "$(CONFIG_FILE)" 2>/dev/null | cut -d'"' -f4 || echo "N/A"); \
		CH=$$(grep -o '"channel_type"[[:space:]]*:[[:space:]]*"[^"]*"' "$(CONFIG_FILE)" 2>/dev/null | cut -d'"' -f4 || echo "N/A"); \
		printf "  Model:   $(cGreen)%s$(cReset)\n" "$$MODEL"; \
		printf "  Channel: $(cGreen)%s$(cReset)\n" "$$CH"; \
		printf "\n"; \
	else \
		printf "$(cYellow)config.json 不存在，需要完整配置。$(cReset)\n\n"; \
	fi
	@printf "$(cBold)选择配置范围:$(cReset)\n"
	@printf "  $(cGreen)[1]$(cReset) 仅配置 AI 模型\n"
	@printf "  $(cGreen)[2]$(cReset) 仅配置消息渠道\n"
	@printf "  $(cGreen)[3]$(cReset) 全部重新配置\n"
	@printf "  $(cRed)[q]$(cReset)   取消\n"
	@read -p "请选择 [1-3, q]: " CFG_CHOICE; \
	case $$CFG_CHOICE in \
		1)  $(MAKE) config-model ;; \
		2)  $(MAKE) config-channel ;; \
		3)  $(MAKE) config-model && $(MAKE) config-channel ;; \
		q)  printf "$(cGreen)已取消$(cReset)\n" ;; \
		*)  printf "$(cRed)无效选择$(cReset)\n"; exit 1 ;; \
	esac

# --- config-model: 仅配置 AI 模型 ---
.PHONY: config-model
config-model: ## 配置 AI 模型
	@$(call _banner,AI 模型配置)
	@# 显示当前模型
	@if [ -f "$(CONFIG_FILE)" ]; then \
		MODEL=$$(grep -o '"model"[[:space:]]*:[[:space:]]*"[^"]*"' "$(CONFIG_FILE)" 2>/dev/null | cut -d'"' -f4 || echo "N/A"); \
		printf "$(cCyan)当前模型: $(cGreen)%s$(cReset)\n\n" "$$MODEL"; \
	fi
	@printf "  $(cGreen)[1]$(cReset) DeepSeek\n"
	@printf "  $(cGreen)[2]$(cReset) MiniMax\n"
	@printf "  $(cGreen)[3]$(cReset) Zhipu AI (智谱)\n"
	@printf "  $(cGreen)[4]$(cReset) Kimi (Moonshot)\n"
	@printf "  $(cGreen)[5]$(cReset) Doubao (火山引擎 Ark)\n"
	@printf "  $(cGreen)[6]$(cReset) Qwen (通义千问 DashScope)\n"
	@printf "  $(cGreen)[7]$(cReset) Claude (Anthropic)\n"
	@printf "  $(cGreen)[8]$(cReset) Gemini (Google)\n"
	@printf "  $(cGreen)[9]$(cReset) OpenAI GPT\n"
	@printf "  $(cYellow)[0]$(cReset) 跳过 (保留当前模型配置)\n"
	@read -p "选择模型 [0-9, 默认 0]: " MC; MC=$${MC:-0}; \
	 if [ "$$MC" = "0" ]; then \
		printf "$(cGreen)保留当前模型配置$(cReset)\n"; \
		exit 0; \
	 fi; \
	 case $$MC in \
		1) MODEL="deepseek-chat"; \
		   read -p "DeepSeek API Key: " AK; \
		   read -p "API Base [默认: https://api.deepseek.com/v1]: " AB; \
		   AB=$${AB:-https://api.deepseek.com/v1}; \
		   MODEL_JSON="\"model\": \"$$MODEL\", \"open_ai_api_key\": \"$$AK\", \"open_ai_api_base\": \"$$AB\"" ;; \
		2) MODEL="MiniMax-M2.7"; \
		   read -p "MiniMax API Key: " AK; \
		   read -p "Group ID [可选, 回车跳过]: " GI; \
		   MODEL_JSON="\"model\": \"$$MODEL\", \"minimax_api_key\": \"$$AK\", \"Minimax_group_id\": \"$$GI\", \"Minimax_base_url\": \"\"" ;; \
		3) MODEL="glm-5-turbo"; \
		   read -p "Zhipu API Key: " AK; \
		   MODEL_JSON="\"model\": \"$$MODEL\", \"zhipu_ai_api_key\": \"$$AK\", \"zhipu_ai_api_base\": \"https://open.bigmodel.cn/api/paas/v4\"" ;; \
		4) MODEL="kimi-k2.5"; \
		   read -p "Moonshot API Key: " AK; \
		   MODEL_JSON="\"model\": \"$$MODEL\", \"moonshot_api_key\": \"$$AK\", \"moonshot_base_url\": \"https://api.moonshot.cn/v1\"" ;; \
		5) MODEL="doubao-seed-2-0-code-preview-260215"; \
		   read -p "Ark API Key: " AK; \
		   MODEL_JSON="\"model\": \"$$MODEL\", \"ark_api_key\": \"$$AK\", \"ark_base_url\": \"https://ark.cn-beijing.volces.com/api/v3\"" ;; \
		6) MODEL="qwen3.5-plus"; \
		   read -p "DashScope API Key: " AK; \
		   MODEL_JSON="\"model\": \"$$MODEL\", \"dashscope_api_key\": \"$$AK\"" ;; \
		7) MODEL="claude-sonnet-4-6"; \
		   read -p "Claude API Key: " AK; \
		   read -p "API Base [默认: https://api.anthropic.com/v1]: " AB; \
		   AB=$${AB:-https://api.anthropic.com/v1}; \
		   MODEL_JSON="\"model\": \"$$MODEL\", \"claude_api_key\": \"$$AK\", \"claude_api_base\": \"$$AB\"" ;; \
		8) MODEL="gemini-3.1-pro-preview"; \
		   read -p "Gemini API Key: " AK; \
		   MODEL_JSON="\"model\": \"$$MODEL\", \"gemini_api_key\": \"$$AK\", \"gemini_api_base\": \"https://generativelanguage.googleapis.com\"" ;; \
		9) MODEL="gpt-5.4"; \
		   read -p "OpenAI API Key: " AK; \
		   read -p "API Base [默认: https://api.openai.com/v1]: " AB; \
		   AB=$${AB:-https://api.openai.com/v1}; \
		   MODEL_JSON="\"model\": \"$$MODEL\", \"open_ai_api_key\": \"$$AK\", \"open_ai_api_base\": \"$$AB\"" ;; \
		*) printf "$(cRed)无效选择$(cReset)\n"; exit 1 ;; \
	 esac; \
	 if [ -f "$(CONFIG_FILE)" ]; then \
		python3 -c " \
import json, sys; \
f='$(CONFIG_FILE)'; \
d=json.load(open(f)); \
patch={$$MODEL_JSON}; \
for k,v in patch.items(): d[k]=v; \
json.dump(d, open(f,'w'), indent=2, ensure_ascii=False); \
print('model config updated')" || { printf "$(cRed)更新 config.json 失败$(cReset)\n"; exit 1; }; \
	 else \
		printf '{\n  %s,\n  "channel_type": "web",\n  "web_port": 9899,\n  "agent": true,\n  "agent_max_context_tokens": 50000,\n  "agent_max_steps": 15\n}\n' "$$MODEL_JSON" > "$(CONFIG_FILE)"; \
	 fi
	@printf "$(cGreen)模型配置已更新$(cReset)\n"

# --- config-channel: 仅配置消息渠道 ---
.PHONY: config-channel
config-channel: ## 配置消息渠道
	@$(call _banner,消息渠道配置)
	@# 显示当前渠道
	@if [ -f "$(CONFIG_FILE)" ]; then \
		CH=$$(grep -o '"channel_type"[[:space:]]*:[[:space:]]*"[^"]*"' "$(CONFIG_FILE)" 2>/dev/null | cut -d'"' -f4 || echo "N/A"); \
		printf "$(cCyan)当前渠道: $(cGreen)%s$(cReset)\n\n" "$$CH"; \
	fi
	@printf "  $(cGreen)[1]$(cReset) Web (浏览器控制台)\n"
	@printf "  $(cGreen)[2]$(cReset) 飞书\n"
	@printf "  $(cGreen)[3]$(cReset) 钉钉\n"
	@printf "  $(cGreen)[4]$(cReset) 企微智能机器人\n"
	@printf "  $(cGreen)[5]$(cReset) QQ\n"
	@printf "  $(cGreen)[6]$(cReset) 微信 (个人号)\n"
	@printf "  $(cGreen)[7]$(cReset) 企微自建应用\n"
	@printf "  $(cGreen)[8]$(cReset) 微信公众号\n"
	@printf "  $(cGreen)[9]$(cReset) 终端 (Terminal)\n"
	@printf "  $(cYellow)[0]$(cReset) 跳过 (保留当前渠道配置)\n"
	@read -p "选择渠道 [0-9, 默认 0]: " CC; CC=$${CC:-0}; \
	 if [ "$$CC" = "0" ]; then \
		printf "$(cGreen)保留当前渠道配置$(cReset)\n"; \
		exit 0; \
	 fi; \
	 case $$CC in \
		1) CHANNEL_TYPE="web"; \
		   read -p "Web 端口 [默认: 9899]: " WP; WP=$${WP:-9899}; \
		   CHANNEL_JSON="\"channel_type\": \"web\", \"web_port\": $$WP, \"web_console\": true" ;; \
		2) CHANNEL_TYPE="feishu"; \
		   read -p "飞书 App ID: " FID; \
		   read -p "飞书 App Secret: " FSEC; \
		   read -p "飞书 Verification Token: " FTOK; \
		   read -p "飞书机器人名称: " FBN; \
		   read -p "事件模式 websocket/http [默认: websocket]: " FEM; FEM=$${FEM:-websocket}; \
		   CHANNEL_JSON="\"channel_type\": \"feishu\", \"feishu_app_id\": \"$$FID\", \"feishu_app_secret\": \"$$FSEC\", \"feishu_token\": \"$$FTOK\", \"feishu_bot_name\": \"$$FBN\", \"feishu_event_mode\": \"$$FEM\"" ;; \
		3) CHANNEL_TYPE="dingtalk"; \
		   read -p "钉钉 Client ID: " DID; \
		   read -p "钉钉 Client Secret: " DSEC; \
		   CHANNEL_JSON="\"channel_type\": \"dingtalk\", \"dingtalk_client_id\": \"$$DID\", \"dingtalk_client_secret\": \"$$DSEC\", \"dingtalk_card_enabled\": false" ;; \
		4) CHANNEL_TYPE="wecom_bot"; \
		   read -p "企微机器人 Bot ID: " WBID; \
		   read -p "企微机器人 Bot Secret: " WBSEC; \
		   CHANNEL_JSON="\"channel_type\": \"wecom_bot\", \"wecom_bot_id\": \"$$WBID\", \"wecom_bot_secret\": \"$$WBSEC\"" ;; \
		5) CHANNEL_TYPE="qq"; \
		   read -p "QQ App ID: " QID; \
		   read -p "QQ App Secret: " QSEC; \
		   CHANNEL_JSON="\"channel_type\": \"qq\", \"qq_app_id\": \"$$QID\", \"qq_app_secret\": \"$$QSEC\"" ;; \
		6) CHANNEL_TYPE="weixin"; \
		   read -p "微信 Token: " WTOK; \
		   CHANNEL_JSON="\"channel_type\": \"weixin\", \"weixin_token\": \"$$WTOK\"" ;; \
		7) CHANNEL_TYPE="wechatcom_app"; \
		   read -p "企微 Corp ID: " WCID; \
		   read -p "企微应用 Secret: " WCSEC; \
		   read -p "企微应用 Agent ID: " WCAID; \
		   read -p "企微 Token: " WCTOK; \
		   read -p "企微 EncodingAESKey: " WCAES; \
		   CHANNEL_JSON="\"channel_type\": \"wechatcom_app\", \"wechatcom_corp_id\": \"$$WCID\", \"wechatcomapp_secret\": \"$$WCSEC\", \"wechatcomapp_agent_id\": \"$$WCAID\", \"wechatcomapp_token\": \"$$WCTOK\", \"wechatcomapp_aes_key\": \"$$WCAES\"" ;; \
		8) CHANNEL_TYPE="wechatmp"; \
		   read -p "公众号 App ID: " MPID; \
		   read -p "公众号 App Secret: " MPSEC; \
		   read -p "公众号 Token: " MPTOK; \
		   read -p "公众号 EncodingAESKey: " MPAES; \
		   read -p "公众号端口 [默认: 8080]: " MPPORT; MPPORT=$${MPPORT:-8080}; \
		   CHANNEL_JSON="\"channel_type\": \"wechatmp\", \"wechatmp_app_id\": \"$$MPID\", \"wechatmp_app_secret\": \"$$MPSEC\", \"wechatmp_token\": \"$$MPTOK\", \"wechatmp_aes_key\": \"$$MPAES\", \"wechatmp_port\": $$MPPORT" ;; \
		9) CHANNEL_TYPE="terminal"; \
		   CHANNEL_JSON="\"channel_type\": \"terminal\"" ;; \
		*) printf "$(cRed)无效选择$(cReset)\n"; exit 1 ;; \
	 esac; \
	 if [ -f "$(CONFIG_FILE)" ]; then \
		python3 -c " \
import json; \
f='$(CONFIG_FILE)'; \
d=json.load(open(f)); \
patch={$$CHANNEL_JSON}; \
for k,v in patch.items(): d[k]=v; \
json.dump(d, open(f,'w'), indent=2, ensure_ascii=False); \
print('channel config updated')" || { printf "$(cRed)更新 config.json 失败$(cReset)\n"; exit 1; }; \
	 else \
		printf '{\n  "model": "",\n  %s,\n  "agent": true,\n  "agent_max_context_tokens": 50000,\n  "agent_max_steps": 15\n}\n' "$$CHANNEL_JSON" > "$(CONFIG_FILE)"; \
	 fi
	@printf "$(cGreen)渠道配置已更新$(cReset)\n"

.PHONY: install
install: ## 安装 Python 依赖
	@$(call _banner,安装依赖)
	@if ! command -v uv &> /dev/null; then \
		printf "$(cYellow)uv 未安装，使用 pip 安装...$(cReset)\n"; \
		pip install uv; \
	fi
	@VENV=$${VIRTUAL_ENV:-}; \
		if [ -n "$$VENV" ]; then \
			printf "$(cGreen)使用已有虚拟环境: $$VENV$(cReset)\n"; \
			export UV_PROJECT_ENVIRONMENT="$$VENV"; \
		fi; \
		cd "$(ROOT_DIR)" && uv sync
	@printf "$(cGreen)依赖安装完成$(cReset)\n"

.PHONY: clean
clean: ## 清理临时文件
	@$(call _banner,清理)
	@printf "$(cGreen)清理 __pycache__ 目录...$(cReset)\n"
	@cd "$(ROOT_DIR)" && find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	@printf "$(cGreen)清理 .pyc 文件...$(cReset)\n"
	@cd "$(ROOT_DIR)" && find . -type f -name "*.pyc" -delete 2>/dev/null || true
	@printf "$(cGreen)清理 .egg-info...$(cReset)\n"
	@cd "$(ROOT_DIR)" && find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
	@printf "$(cGreen)清理完成$(cReset)\n"
