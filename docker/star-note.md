Docker 开发环境启动清单
1. 前置条件
安装 Docker 和 Docker Compose（桌面版 Docker Desktop 自带 compose）
2. 配置 API Key
编辑 docker/.env，填入你的 DeepSeek API Key：


DEEPSEEK_API_KEY=sk-你的key
当前已有一个 key 在里面，确认是否有效。

3. 启动容器
在项目根目录执行：

docker compose -f docker/docker-compose.dev.yml up -d
首次启动会自动：

开发阶段用的：
CMD ["tail", "-f", "/dev/null"]
容器默认只是挂着（tail -f /dev/null），并没有执行 python app.py。
需要手动启动：
docker exec -it amy-dev python app.py

构建镜像（安装系统依赖 + uv 包管理器）
创建 Python 虚拟环境并安装项目依赖
启动容器 amy-dev
4. 访问
浏览器打开 **http://localhost:9899**，即可进入 Web 对话界面。

补充说明
项目	说明
源码热更新	整个项目目录以 volume 挂载进容器，改代码即时生效
依赖变更	entrypoint 脚本自动检测 pyproject.toml / uv.lock 变化并重装依赖
模型切换	修改 docker/.env 中的 MODEL 变量（默认 deepseek-chat）
查看日志	docker logs -f amy-dev
进入容器	docker exec -it amy-dev bash
停止容器	docker compose -f docker/docker-compose.dev.yml down
容器默认是 tail -f /dev/null 保持运行，启动后需要执行 python app.py 来真正启动服务。如果你希望容器自动启动应用，可以把 docker-compose.dev.yml 里的 command 改为 python app.py。