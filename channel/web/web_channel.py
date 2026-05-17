# -*- coding=utf-8 -*-
"""
Web渠道模块 —— 提供基于HTTP的Web聊天界面渠道。

本模块实现了WebChannel类，作为ChatChannel的子类，通过web.py框架提供HTTP服务，
支持消息收发、SSE流式响应、文件上传、配置管理、渠道管理、日志查看等功能。
此外还包含多个请求处理器(Handler)类，用于处理不同URL路径的HTTP请求。
"""
import time
import json
import logging
import mimetypes
import os
import threading
import time
import uuid
from queue import Queue, Empty

import web

from bridge.context import *
from bridge.reply import Reply, ReplyType
from channel.chat_channel import ChatChannel, check_prefix
from channel.chat_message import ChatMessage
from collections import OrderedDict
from common import const
from common.log import logger
from common.singleton import singleton
from config import conf

# 支持的图片扩展名集合，用于判断上传文件的类型
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg"}
# 支持的视频扩展名集合，用于判断上传文件的类型
VIDEO_EXTENSIONS = {".mp4", ".webm", ".avi", ".mov", ".mkv"}


def _get_upload_dir() -> str:
    """
    获取文件上传的临时目录路径。

    从配置中读取agent_workspace作为工作空间根目录，在其下创建tmp子目录。
    如果目录不存在则自动创建（exist_ok=True确保不会因目录已存在而报错）。
    上传的文件将保存在该目录中，便于后续读取和预览。

    Returns:
        str: 上传文件的保存目录的绝对路径
    """
    from common.utils import expand_path
    ws_root = expand_path(conf().get("agent_workspace", "~/cow"))
    tmp_dir = os.path.join(ws_root, "tmp")
    os.makedirs(tmp_dir, exist_ok=True)
    return tmp_dir


class WebMessage(ChatMessage):
    """
    Web渠道的消息封装类。

    继承自ChatMessage，用于将Web渠道接收到的用户消息封装为统一的消息对象，
    以便在ChatChannel的消息处理流程中使用。与微信等渠道不同，Web消息
    不需要复杂的消息解析，只需记录基本的发送者、接收者和内容信息。
    """

    def __init__(
            self,
            msg_id,
            content,
            ctype=ContextType.TEXT,
            from_user_id="User",
            to_user_id="Chatgpt",
            other_user_id="Chatgpt",
    ):
        """
        初始化Web消息对象。

        Args:
            msg_id: 消息唯一标识，由_generate_msg_id()生成
            content: 消息内容文本
            ctype: 消息类型，默认为TEXT文本类型
            from_user_id: 发送者ID，在Web渠道中通常为session_id
            to_user_id: 接收者ID，默认为"Chatgpt"（即AI助手）
            other_user_id: 其他用户ID，默认与to_user_id相同
        """
        self.msg_id = msg_id
        self.ctype = ctype
        self.content = content
        self.from_user_id = from_user_id
        self.to_user_id = to_user_id
        self.other_user_id = other_user_id


@singleton
class WebChannel(ChatChannel):
    """
    Web渠道的主类，使用单例模式确保全局只有一个Web渠道实例。

    继承自ChatChannel，提供基于HTTP的Web聊天服务。支持两种响应模式：
    1. SSE（Server-Sent Events）流式响应：实时推送AI回复的增量内容
    2. 轮询（Polling）模式：前端定时请求获取完整回复

    主要功能包括：
    - 消息接收与分发
    - 文件上传处理
    - SSE流式推送
    - 轮询响应获取
    - 配置管理
    - 渠道管理
    - 日志查看等

    不支持的回复类型：语音（VOICE），因为Web界面暂无语音播放功能。
    """
    NOT_SUPPORT_REPLYTYPE = [ReplyType.VOICE]
    _instance = None

    # def __new__(cls):
    #     if cls._instance is None:
    #         cls._instance = super(WebChannel, cls).__new__(cls)
    #     return cls._instance

    def __init__(self):
        """
        初始化Web渠道实例。

        设置消息ID计数器、会话队列、SSE队列等数据结构。
        - session_queues: 按session_id存储的响应队列，用于轮询模式
        - request_to_session: request_id到session_id的映射，用于关联请求与会话
        - sse_queues: 按request_id存储的SSE事件队列，用于流式推送
        """
        super().__init__()
        self.msg_id_counter = 0
        self.session_queues = {}  # session_id -> Queue (fallback polling)  # session_id -> 响应队列（轮询模式的备选方案）
        self.request_to_session = {}  # request_id -> session_id  # 请求ID到会话ID的映射关系
        self.sse_queues = {}  # request_id -> Queue (SSE streaming)  # 请求ID到SSE事件队列的映射
        self._http_server = None

    def _generate_msg_id(self):
        """生成唯一的消息ID"""
        # 使用时间戳+递增计数器组合确保唯一性
        # 时间戳提供粗粒度唯一性，计数器解决同一秒内的并发问题
        self.msg_id_counter += 1
        return str(int(time.time())) + str(self.msg_id_counter)

    def _generate_request_id(self):
        """生成唯一的请求ID"""
        # 使用UUID v4生成全局唯一的请求标识，用于追踪单次请求的完整生命周期
        return str(uuid.uuid4())

    def send(self, reply: Reply, context: Context):
        """
        将AI生成的回复发送给前端。

        根据回复类型和当前使用的响应模式（SSE或轮询），将回复内容
        推送到对应的队列中。SSE模式下推送done事件，轮询模式下推送到会话队列。

        处理流程：
        1. 检查回复类型是否受支持
        2. 从context中获取request_id，进而找到对应的session_id
        3. 优先检查SSE队列是否存在，若存在则推送done事件（流式内容已通过callback推送）
        4. 若SSE队列不存在，则回退到轮询模式，将响应推入会话队列

        Args:
            reply: AI生成的回复对象，包含回复类型和内容
            context: 消息上下文，包含request_id、session_id等关联信息
        """
        try:
            # 检查Web渠道是否支持该回复类型
            if reply.type in self.NOT_SUPPORT_REPLYTYPE:
                logger.warning(f"Web channel doesn't support {reply.type} yet")
                return

            # IMAGE_URL类型需要短暂延迟，给前端一些准备时间
            if reply.type == ReplyType.IMAGE_URL:
                time.sleep(0.5)

            # 从上下文中获取请求ID，用于关联回复与原始请求
            request_id = context.get("request_id", None)
            if not request_id:
                logger.error("No request_id found in context, cannot send message")
                return

            # 根据request_id查找对应的session_id
            session_id = self.request_to_session.get(request_id)
            if not session_id:
                logger.error(f"No session_id found for request {request_id}")
                return

            # SSE mode: push done event to SSE queue
            # SSE模式：将完成事件推送到SSE队列，通知前端流式传输结束
            # 此时流式内容的delta事件已通过_make_sse_callback推送完毕
            if request_id in self.sse_queues:
                content = reply.content if reply.content is not None else ""
                self.sse_queues[request_id].put({
                    "type": "done",
                    "content": content,
                    "request_id": request_id,
                    "timestamp": time.time()
                })
                logger.debug(f"SSE done sent for request {request_id}")
                return

            # Fallback: polling mode
            # 轮询模式的备选方案：将完整响应推入会话队列，等待前端轮询获取
            if session_id in self.session_queues:
                response_data = {
                    "type": str(reply.type),
                    "content": reply.content,
                    "timestamp": time.time(),
                    "request_id": request_id
                }
                self.session_queues[session_id].put(response_data)
                logger.debug(f"Response sent to poll queue for session {session_id}, request {request_id}")
            else:
                # 如果会话队列不存在，说明该会话已失效，响应被丢弃
                logger.warning(f"No response queue found for session {session_id}, response dropped")

        except Exception as e:
            logger.error(f"Error in send method: {e}")

    def _make_sse_callback(self, request_id: str):
        """
        构建一个SSE事件回调函数，用于将Agent流式执行事件推送到SSE队列。

        在Agent模式下，AI的思考、工具调用等过程会生成多种事件，
        通过此回调函数将这些事件实时推送到前端，实现流式展示效果。
        支持的事件类型包括：
        - message_update: 文本增量更新（AI正在输出文本）
        - tool_execution_start: 工具开始执行
        - tool_execution_end: 工具执行结束（包含结果和耗时）

        Args:
            request_id: 请求ID，用于定位对应的SSE队列

        Returns:
            on_event回调函数，接受event字典参数
        """

        def on_event(event: dict):
            """
            SSE事件回调处理函数。

            根据事件类型将不同格式的数据推入SSE队列，前端通过
            EventSource接收到这些事件后进行相应的UI更新。

            Args:
                event: 事件字典，包含type和data字段
            """
            # 检查SSE队列是否还存在，可能已被清理（如请求超时）
            if request_id not in self.sse_queues:
                return
            q = self.sse_queues[request_id]
            event_type = event.get("type")
            data = event.get("data", {})

            if event_type == "message_update":
                # AI文本增量输出，推送delta内容给前端实时显示
                delta = data.get("delta", "")
                if delta:
                    q.put({"type": "delta", "content": delta})

            elif event_type == "tool_execution_start":
                # 工具开始执行，通知前端显示工具调用状态
                tool_name = data.get("tool_name", "tool")
                arguments = data.get("arguments", {})
                q.put({"type": "tool_start", "tool": tool_name, "arguments": arguments})

            elif event_type == "tool_execution_end":
                # 工具执行结束，推送执行结果和耗时信息
                tool_name = data.get("tool_name", "tool")
                status = data.get("status", "success")
                result = data.get("result", "")
                exec_time = data.get("execution_time", 0)
                # Truncate long results to avoid huge SSE payloads
                # 截断过长的结果，避免SSE负载过大导致传输延迟
                result_str = str(result)
                if len(result_str) > 2000:
                    result_str = result_str[:2000] + "…"
                q.put({
                    "type": "tool_end",
                    "tool": tool_name,
                    "status": status,
                    "result": result_str,
                    "execution_time": round(exec_time, 2)
                })

        return on_event

    def upload_file(self):
        """
        处理文件上传请求。

        接收multipart/form-data格式的文件上传，将文件保存到工作空间的tmp目录下。
        根据文件扩展名判断文件类型（图片/视频/普通文件），返回文件的元数据信息。

        安全处理：
        - 使用UUID生成安全文件名，避免文件名注入攻击
        - 根据扩展名分类文件类型，方便前端选择合适的预览方式

        Returns:
            JSON字符串，包含上传状态、文件路径、文件名、文件类型和预览URL
        """
        try:
            params = web.input(file={}, session_id="")
            file_obj = params.get("file")
            session_id = params.get("session_id", "")
            # 校验文件对象是否有效
            if file_obj is None or not hasattr(file_obj, "filename") or not file_obj.filename:
                return json.dumps({"status": "error", "message": "No file uploaded"})

            upload_dir = _get_upload_dir()

            original_name = file_obj.filename
            ext = os.path.splitext(original_name)[1].lower()
            # 使用UUID生成安全文件名，防止路径遍历和文件名冲突
            safe_name = f"web_{uuid.uuid4().hex[:8]}{ext}"
            save_path = os.path.join(upload_dir, safe_name)

            # 写入文件内容
            with open(save_path, "wb") as f:
                f.write(file_obj.read() if hasattr(file_obj, "read") else file_obj.value)

            # 根据扩展名判断文件类型
            if ext in IMAGE_EXTENSIONS:
                file_type = "image"
            elif ext in VIDEO_EXTENSIONS:
                file_type = "video"
            else:
                file_type = "file"

            # 生成预览URL，供前端通过/uploads/路径访问
            preview_url = f"/uploads/{safe_name}"

            logger.info(f"[WebChannel] File uploaded: {original_name} -> {save_path} ({file_type})")

            return json.dumps({
                "status": "success",
                "file_path": save_path,
                "file_name": original_name,
                "file_type": file_type,
                "preview_url": preview_url,
            }, ensure_ascii=False)

        except Exception as e:
            logger.error(f"[WebChannel] File upload error: {e}", exc_info=True)
            return json.dumps({"status": "error", "message": str(e)})

    def post_message(self):
        """
        处理用户发送消息的POST请求。

        解析JSON格式的请求体，提取消息内容、会话ID、流式选项和附件信息。
        将消息封装为WebMessage对象，构造上下文后投入消息处理流程。

        处理流程：
        1. 解析请求体，提取session_id、message、stream、attachments
        2. 将附件信息拼接到消息文本中（与QQ渠道格式保持一致）
        3. 生成request_id并建立映射关系
        4. 根据stream参数决定是否创建SSE队列
        5. 检查消息前缀，不满足前缀要求时自动添加
        6. 构造消息上下文，在后台线程中启动消息处理

        Returns:
            JSON字符串，包含处理状态、request_id和stream标志
        """
        try:
            data = web.data()
            json_data = json.loads(data)
            session_id = json_data.get('session_id', f'session_{int(time.time())}')
            prompt = json_data.get('message', '')
            use_sse = json_data.get('stream', True)
            attachments = json_data.get('attachments', [])

            # Append file references to the prompt (same format as QQ channel)
            # 将附件引用追加到消息文本中，采用与QQ渠道相同的格式，便于AI理解附件内容
            if attachments:
                file_refs = []
                for att in attachments:
                    ftype = att.get("file_type", "file")
                    fpath = att.get("file_path", "")
                    if not fpath:
                        continue
                    if ftype == "image":
                        file_refs.append(f"[图片: {fpath}]")
                    elif ftype == "video":
                        file_refs.append(f"[视频: {fpath}]")
                    else:
                        file_refs.append(f"[文件: {fpath}]")
                if file_refs:
                    prompt = prompt + "\n" + "\n".join(file_refs)
                    logger.info(f"[WebChannel] Attached {len(file_refs)} file(s) to message")

            # 生成唯一的请求ID并建立映射
            request_id = self._generate_request_id()
            self.request_to_session[request_id] = session_id

            # 确保会话队列存在
            if session_id not in self.session_queues:
                self.session_queues[session_id] = Queue()

            # SSE模式下创建SSE事件队列
            if use_sse:
                self.sse_queues[request_id] = Queue()

            # 检查消息前缀配置，如果消息不匹配任何触发前缀则自动添加默认前缀
            # 这是为了让用户无需手动输入前缀即可触发AI回复
            trigger_prefixs = conf().get("single_chat_prefix", [""])
            if check_prefix(prompt, trigger_prefixs) is None:
                if trigger_prefixs:
                    prompt = trigger_prefixs[0] + prompt
                    logger.debug(f"[WebChannel] Added prefix to message: {prompt}")

            # 构造Web消息对象
            msg = WebMessage(self._generate_msg_id(), prompt)
            msg.from_user_id = session_id

            # 构造消息上下文
            context = self._compose_context(ContextType.TEXT, prompt, msg=msg, isgroup=False)

            # 上下文为空表示消息被过滤（如触发词不匹配等）
            if context is None:
                logger.warning(f"[WebChannel] Context is None for session {session_id}, message may be filtered")
                if request_id in self.sse_queues:
                    del self.sse_queues[request_id]
                return json.dumps({"status": "error", "message": "Message was filtered"})

            # 填充上下文中的会话信息
            context["session_id"] = session_id
            context["receiver"] = session_id
            context["request_id"] = request_id

            # SSE模式下设置事件回调，用于流式推送Agent执行事件
            if use_sse:
                context["on_event"] = self._make_sse_callback(request_id)

            # 在后台线程中启动消息处理，避免阻塞HTTP请求
            threading.Thread(target=self.produce, args=(context,)).start()

            return json.dumps({"status": "success", "request_id": request_id, "stream": use_sse})

        except Exception as e:
            logger.error(f"Error processing message: {e}")
            return json.dumps({"status": "error", "message": str(e)})

    def stream_response(self, request_id: str):
        """
        SSE（Server-Sent Events）响应生成器。

        为指定的request_id生成SSE事件流。前端通过EventSource连接后，
        可以实时接收AI回复的增量内容。生成器会持续运行直到收到done事件
        或超时。

        工作原理：
        1. 从SSE队列中逐个取出事件
        2. 将事件序列化为JSON并通过SSE格式推送
        3. 当收到done类型事件时结束流
        4. 空闲时发送keepalive注释，防止连接被代理服务器关闭

        使用UTF-8编码输出，避免WSGI默认的Latin-1编码导致中文乱码。

        Args:
            request_id: 请求ID，用于定位对应的SSE队列

        Yields:
            bytes: UTF-8编码的SSE事件数据
        """
        if request_id not in self.sse_queues:
            yield b"data: {\"type\": \"error\", \"message\": \"invalid request_id\"}\n\n"
            return

        q = self.sse_queues[request_id]
        timeout = 300  # 5 minutes max  # 最大等待5分钟，防止连接无限挂起
        deadline = time.time() + timeout

        try:
            while time.time() < deadline:
                try:
                    # 设置1秒超时获取事件，便于定期发送keepalive
                    item = q.get(timeout=1)
                except Empty:
                    # 队列为空时发送SSE注释行作为keepalive
                    # 这可以防止Nginx等反向代理因长时间无数据而关闭连接
                    yield b": keepalive\n\n"
                    continue

                # 将事件数据序列化为JSON并通过SSE data字段推送
                payload = json.dumps(item, ensure_ascii=False)
                yield f"data: {payload}\n\n".encode("utf-8")

                # 收到done事件表示回复完成，结束SSE流
                if item.get("type") == "done":
                    break
        finally:
            # 清理SSE队列，释放资源
            self.sse_queues.pop(request_id, None)

    def poll_response(self):
        """
        轮询模式下的响应获取接口。

        前端通过POST请求携带session_id，从会话队列中获取已准备好的响应。
        如果队列为空则返回has_content=False，前端据此判断是否继续轮询。

        注意：此方法使用get(block=False)而非peek，获取后消息即从队列移除。
        如果前端处理失败，消息将丢失。这是轮询模式的简化实现。

        Returns:
            JSON字符串，包含响应状态、内容和请求ID
        """
        try:
            data = web.data()
            json_data = json.loads(data)
            session_id = json_data.get('session_id')

            if not session_id or session_id not in self.session_queues:
                return json.dumps({"status": "error", "message": "Invalid session ID"})

            # 尝试从队列获取响应，不等待
            try:
                # 使用peek而不是get，这样如果前端没有成功处理，下次还能获取到
                response = self.session_queues[session_id].get(block=False)

                # 返回响应，包含请求ID以区分不同请求
                return json.dumps({
                    "status": "success",
                    "has_content": True,
                    "content": response["content"],
                    "request_id": response["request_id"],
                    "timestamp": response["timestamp"]
                })

            except Empty:
                # 没有新响应
                return json.dumps({"status": "success", "has_content": False})

        except Exception as e:
            logger.error(f"Error polling response: {e}")
            return json.dumps({"status": "error", "message": str(e)})

    def chat_page(self):
        """Serve the chat HTML page."""
        file_path = os.path.join(os.path.dirname(__file__), 'chat.html')  # 使用绝对路径
        with open(file_path, 'r', encoding='utf-8') as f:
            return f.read()

    def startup(self):
        """
        启动Web渠道的HTTP服务器。

        配置URL路由映射，创建web.py应用实例，启动WSGI服务器监听指定端口。
        同时禁用web.py的默认HTTP日志输出，避免控制台日志过于嘈杂。

        服务器配置：
        - 默认端口9899（可通过web_port配置项修改）
        - 启用daemon_threads允许并发请求处理
        - 禁用web.py的HTTP日志输出
        """
        port = conf().get("web_port", 9899)

        # 打印可用渠道类型提示
        logger.info(
            "[WebChannel] 全部可用通道如下，可修改 config.json 配置文件中的 channel_type 字段进行切换，多个通道用逗号分隔：")
        logger.info("[WebChannel]   1. weixin           - 微信")
        logger.info("[WebChannel]   2. web              - 网页")
        logger.info("[WebChannel]   3. terminal         - 终端")
        logger.info("[WebChannel]   4. feishu           - 飞书")
        logger.info("[WebChannel]   5. dingtalk         - 钉钉")
        logger.info("[WebChannel]   6. wecom_bot        - 企微智能机器人")
        logger.info("[WebChannel]   7. wechatcom_app    - 企微自建应用")
        logger.info("[WebChannel]   8. wechatmp         - 个人公众号")
        logger.info("[WebChannel]   9. wechatmp_service - 企业公众号")
        logger.info("[WebChannel] ✅ Web控制台已运行")
        logger.info(f"[WebChannel] 🌐 本地访问: http://localhost:{port}")
        logger.info(f"[WebChannel] 🌍 服务器访问: http://YOUR_IP:{port} (请将YOUR_IP替换为服务器IP)")

        # 确保静态文件目录存在
        static_dir = os.path.join(os.path.dirname(__file__), 'static')
        if not os.path.exists(static_dir):
            os.makedirs(static_dir)
            logger.debug(f"[WebChannel] Created static directory: {static_dir}")

        # URL路由映射表，将不同路径映射到对应的处理器类
        urls = (
            '/', 'RootHandler',
            '/message', 'MessageHandler',
            '/upload', 'UploadHandler',
            '/uploads/(.*)', 'UploadsHandler',
            '/poll', 'PollHandler',
            '/stream', 'StreamHandler',
            '/chat', 'ChatHandler',
            '/config', 'ConfigHandler',
            '/api/channels', 'ChannelsHandler',
            '/api/weixin/qrlogin', 'WeixinQrHandler',
            '/api/tools', 'ToolsHandler',
            '/api/skills', 'SkillsHandler',
            '/api/memory', 'MemoryHandler',
            '/api/memory/content', 'MemoryContentHandler',
            '/api/scheduler', 'SchedulerHandler',
            '/api/history', 'HistoryHandler',
            '/api/logs', 'LogsHandler',
            '/assets/(.*)', 'AssetsHandler',
        )
        app = web.application(urls, globals(), autoreload=False)

        # 完全禁用web.py的HTTP日志输出
        # 每个HTTP请求都会触发日志输出，在Web渠道高并发场景下会产生大量无用日志
        web.httpserver.LogMiddleware.log = lambda self, status, environ: None

        # 配置web.py的日志级别为ERROR
        logging.getLogger("web").setLevel(logging.ERROR)
        logging.getLogger("web.httpserver").setLevel(logging.ERROR)

        # Build WSGI app with middleware (same as runsimple but without print)
        # 构建WSGI应用并添加静态文件中间件和日志中间件
        func = web.httpserver.StaticMiddleware(app.wsgifunc())
        func = web.httpserver.LogMiddleware(func)
        server = web.httpserver.WSGIServer(("0.0.0.0", port), func)
        # Allow concurrent requests by not blocking on in-flight handler threads
        # 启用守护线程模式，允许并发处理请求而不阻塞
        server.daemon_threads = True
        self._http_server = server
        try:
            server.start()
        except (KeyboardInterrupt, SystemExit):
            server.stop()

    def stop(self):
        """
        停止Web渠道的HTTP服务器。

        安全关闭HTTP服务器，释放端口占用。在停止过程中捕获异常
        防止因服务器状态异常导致程序崩溃。
        """
        if self._http_server:
            try:
                self._http_server.stop()
                logger.info("[WebChannel] HTTP server stopped")
            except Exception as e:
                logger.warning(f"[WebChannel] Error stopping HTTP server: {e}")
            self._http_server = None


class RootHandler:
    """
    根路径处理器，处理对"/"的GET请求。

    将根路径请求重定向到聊天页面，确保用户直接访问域名时
    能看到聊天界面而非空白页面。
    """

    def GET(self):
        # 重定向到/chat
        raise web.seeother('/chat')


class MessageHandler:
    """
    消息处理器，处理POST到"/message"的请求。

    将消息发送请求委托给WebChannel的post_message方法处理。
    这是前端发送聊天消息的主要入口。
    """

    def POST(self):
        return WebChannel().post_message()


class UploadHandler:
    """
    文件上传处理器，处理POST到"/upload"的请求。

    将文件上传请求委托给WebChannel的upload_file方法处理。
    设置响应头为JSON格式，确保前端正确解析返回数据。
    """

    def POST(self):
        web.header('Content-Type', 'application/json; charset=utf-8')
        return WebChannel().upload_file()


class UploadsHandler:
    """
    上传文件访问处理器，处理GET到"/uploads/<file_name>"的请求。

    提供已上传文件的访问服务，支持文件预览功能。
    包含路径安全检查，防止目录遍历攻击。
    """

    def GET(self, file_name):
        """Serve uploaded files from workspace/tmp/ for preview."""
        try:
            upload_dir = _get_upload_dir()
            full_path = os.path.normpath(os.path.join(upload_dir, file_name))
            # 安全检查：规范化路径后验证文件确实在上传目录内
            # 防止通过../等路径遍历手段访问系统其他文件
            if not os.path.abspath(full_path).startswith(os.path.abspath(upload_dir)):
                raise web.notfound()
            if not os.path.isfile(full_path):
                raise web.notfound()
            content_type = mimetypes.guess_type(full_path)[0] or "application/octet-stream"
            web.header('Content-Type', content_type)
            # 设置缓存控制，允许浏览器缓存24小时，减少重复请求
            web.header('Cache-Control', 'public, max-age=86400')
            with open(full_path, 'rb') as f:
                return f.read()
        except web.HTTPError:
            raise
        except Exception as e:
            logger.error(f"[WebChannel] Error serving upload: {e}")
            raise web.notfound()


class PollHandler:
    """
    轮询响应处理器，处理POST到"/poll"的请求。

    将轮询请求委托给WebChannel的poll_response方法处理。
    前端在非SSE模式下通过此接口定时获取AI回复。
    """

    def POST(self):
        return WebChannel().poll_response()


class StreamHandler:
    """
    SSE流式响应处理器，处理GET到"/stream"的请求。

    建立SSE（Server-Sent Events）连接，实时推送AI回复的增量内容。
    设置必要的HTTP响应头以支持SSE协议：
    - Content-Type: text/event-stream
    - Cache-Control: no-cache（禁用缓存，确保事件实时推送）
    - X-Accel-Buffering: no（禁用Nginx缓冲）
    - Access-Control-Allow-Origin: *（允许跨域）
    """

    def GET(self):
        params = web.input(request_id='')
        request_id = params.request_id
        if not request_id:
            raise web.badrequest()

        web.header('Content-Type', 'text/event-stream; charset=utf-8')
        web.header('Cache-Control', 'no-cache')
        # 禁用Nginx等反向代理的响应缓冲，确保SSE事件立即推送到客户端
        web.header('X-Accel-Buffering', 'no')
        web.header('Access-Control-Allow-Origin', '*')

        return WebChannel().stream_response(request_id)


class ChatHandler:
    """
    聊天页面处理器，处理GET到"/chat"的请求。

    读取并返回聊天界面的HTML文件，作为前端聊天应用的入口页面。
    """

    def GET(self):
        # 正常返回聊天页面
        file_path = os.path.join(os.path.dirname(__file__), 'chat.html')
        with open(file_path, 'r', encoding='utf-8') as f:
            return f.read()


class ConfigHandler:
    """
    配置管理处理器，处理对"/config"的GET和POST请求。

    GET请求：返回当前配置信息，包括模型、API密钥（脱敏显示）、
    供应商和模型元数据等，供前端配置界面展示。

    POST请求：更新配置项，同时更新内存中的配置和config.json文件，
    确保配置在重启后依然生效。

    安全措施：
    - 只允许修改EDITABLE_KEYS中列出的配置项
    - API密钥脱敏显示，只保留前4位和后4位
    - 数值类型配置项强制转换为int
    """

    _RECOMMENDED_MODELS = [
        const.MINIMAX_M2_5, const.MINIMAX_M2_1, const.MINIMAX_M2_1_LIGHTNING,
        const.GLM_5, const.GLM_4_7,
        const.QWEN3_MAX, const.QWEN35_PLUS,
        const.KIMI_K2_5, const.KIMI_K2,
        const.DOUBAO_SEED_2_PRO, const.DOUBAO_SEED_2_CODE,
        const.CLAUDE_4_6_SONNET, const.CLAUDE_4_6_OPUS, const.CLAUDE_4_5_SONNET,
        const.GEMINI_31_FLASH_LITE_PRE, const.GEMINI_31_PRO_PRE, const.GEMINI_3_FLASH_PRE,
        const.GPT_54, const.GPT_54_MINI, const.GPT_54_NANO, const.GPT_5, const.GPT_41, const.GPT_4o,
        const.DEEPSEEK_CHAT, const.DEEPSEEK_REASONER,
    ]

    # 供应商模型映射表，定义了各LLM供应商的基本信息和可用模型
    # 包含供应商的显示名称、API密钥字段名、API基础URL和模型列表
    PROVIDER_MODELS = OrderedDict([
        ("minimax", {
            "label": "MiniMax",
            "api_key_field": "minimax_api_key",
            "api_base_key": None,
            "api_base_default": None,
            "models": [const.MINIMAX_M2_5, const.MINIMAX_M2_1, const.MINIMAX_M2_1_LIGHTNING],
        }),
        ("zhipu", {
            "label": "智谱AI",
            "api_key_field": "zhipu_ai_api_key",
            "api_base_key": "zhipu_ai_api_base",
            "api_base_default": "https://open.bigmodel.cn/api/paas/v4",
            "models": [const.GLM_5, const.GLM_4_7],
        }),
        ("dashscope", {
            "label": "通义千问",
            "api_key_field": "dashscope_api_key",
            "api_base_key": None,
            "api_base_default": None,
            "models": [const.QWEN3_MAX, const.QWEN35_PLUS],
        }),
        ("moonshot", {
            "label": "Kimi",
            "api_key_field": "moonshot_api_key",
            "api_base_key": "moonshot_base_url",
            "api_base_default": "https://api.moonshot.cn/v1",
            "models": [const.KIMI_K2_5, const.KIMI_K2],
        }),
        ("doubao", {
            "label": "豆包",
            "api_key_field": "ark_api_key",
            "api_base_key": "ark_base_url",
            "api_base_default": "https://ark.cn-beijing.volces.com/api/v3",
            "models": [const.DOUBAO_SEED_2_PRO, const.DOUBAO_SEED_2_CODE],
        }),
        ("claudeAPI", {
            "label": "Claude",
            "api_key_field": "claude_api_key",
            "api_base_key": "claude_api_base",
            "api_base_default": "https://api.anthropic.com/v1",
            "models": [const.CLAUDE_4_6_SONNET, const.CLAUDE_4_6_OPUS, const.CLAUDE_4_5_SONNET],
        }),
        ("gemini", {
            "label": "Gemini",
            "api_key_field": "gemini_api_key",
            "api_base_key": "gemini_api_base",
            "api_base_default": "https://generativelanguage.googleapis.com",
            "models": [const.GEMINI_31_FLASH_LITE_PRE, const.GEMINI_31_PRO_PRE, const.GEMINI_3_FLASH_PRE],
        }),
        ("openai", {
            "label": "OpenAI",
            "api_key_field": "open_ai_api_key",
            "api_base_key": "open_ai_api_base",
            "api_base_default": "https://api.openai.com/v1",
            "models": [const.GPT_54, const.GPT_54_MINI, const.GPT_54_NANO, const.GPT_5, const.GPT_41, const.GPT_4o],
        }),
        ("deepseek", {
            "label": "DeepSeek",
            "api_key_field": "open_ai_api_key",
            "api_base_key": None,
            "api_base_default": None,
            "models": [const.DEEPSEEK_CHAT, const.DEEPSEEK_REASONER],
        }),
        ("linkai", {
            "label": "LinkAI",
            "api_key_field": "linkai_api_key",
            "api_base_key": None,
            "api_base_default": None,
            "models": _RECOMMENDED_MODELS,
        }),
    ])

    # 允许通过Web界面修改的配置项白名单
    # 出于安全考虑，只暴露必要的配置项，防止用户修改敏感配置
    EDITABLE_KEYS = {
        "model", "bot_type", "use_linkai",
        "open_ai_api_base", "claude_api_base", "gemini_api_base",
        "zhipu_ai_api_base", "moonshot_base_url", "ark_base_url",
        "open_ai_api_key", "claude_api_key", "gemini_api_key",
        "zhipu_ai_api_key", "dashscope_api_key", "moonshot_api_key",
        "ark_api_key", "minimax_api_key", "linkai_api_key",
        "agent_max_context_tokens", "agent_max_context_turns", "agent_max_steps",
    }

    @staticmethod
    def _mask_key(value: str) -> str:
        """
        对API密钥进行脱敏处理，只保留前4位和后4位，中间用星号替代。

        这样既能让用户确认密钥是否正确，又不会泄露完整密钥。

        Args:
            value: 原始API密钥字符串

        Returns:
            脱敏后的密钥字符串，如 "sk-a****1234"
        """
        """Mask the middle part of an API key for display."""
        if not value or len(value) <= 8:
            return value
        return value[:4] + "*" * (len(value) - 8) + value[-4:]

    def GET(self):
        """
        获取当前配置信息的GET请求处理。

        返回JSON格式的配置数据，包括：
        - 当前使用的模型和机器人类型
        - 各供应商的API基础URL
        - 脱敏后的API密钥
        - 供应商和模型元数据（供前端选择器使用）
        - Agent相关配置参数

        Returns:
            JSON字符串，包含完整的配置信息
        """
        """Return configuration info and provider/model metadata."""
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            local_config = conf()
            use_agent = local_config.get("agent", False)
            title = "CowAgent" if use_agent else "AI Assistant"

            # 收集各供应商的API基础URL配置
            api_bases = {}
            # 收集脱敏后的API密钥
            api_keys_masked = {}
            for pid, pinfo in self.PROVIDER_MODELS.items():
                base_key = pinfo.get("api_base_key")
                if base_key:
                    api_bases[base_key] = local_config.get(base_key, pinfo["api_base_default"])
                key_field = pinfo.get("api_key_field")
                if key_field and key_field not in api_keys_masked:
                    raw = local_config.get(key_field, "")
                    api_keys_masked[key_field] = self._mask_key(raw) if raw else ""

            # 构建供应商信息供前端使用
            providers = {}
            for pid, p in self.PROVIDER_MODELS.items():
                providers[pid] = {
                    "label": p["label"],
                    "models": p["models"],
                    "api_base_key": p["api_base_key"],
                    "api_base_default": p["api_base_default"],
                    "api_key_field": p.get("api_key_field"),
                }

            return json.dumps({
                "status": "success",
                "use_agent": use_agent,
                "title": title,
                "model": local_config.get("model", ""),
                "bot_type": "openai" if local_config.get("bot_type") == "chatGPT" else local_config.get("bot_type", ""),
                "use_linkai": bool(local_config.get("use_linkai", False)),
                "channel_type": local_config.get("channel_type", ""),
                "agent_max_context_tokens": local_config.get("agent_max_context_tokens", 50000),
                "agent_max_context_turns": local_config.get("agent_max_context_turns", 20),
                "agent_max_steps": local_config.get("agent_max_steps", 15),
                "api_bases": api_bases,
                "api_keys": api_keys_masked,
                "providers": providers,
            }, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Error getting config: {e}")
            return json.dumps({"status": "error", "message": str(e)})

    def POST(self):
        """
        更新配置项的POST请求处理。

        接收JSON格式的更新数据，只修改EDITABLE_KEYS白名单中的配置项。
        同时更新内存中的运行时配置和config.json文件，确保配置持久化。

        类型转换规则：
        - agent_max_context_tokens/turns/steps 强制转换为int
        - use_linkai 强制转换为bool
        - 其他配置项保持原始类型

        Args:
            请求体中的updates字段，包含要修改的配置键值对

        Returns:
            JSON字符串，包含更新状态和已应用的配置项
        """
        """Update configuration values in memory and persist to config.json."""
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            data = json.loads(web.data())
            updates = data.get("updates", {})
            if not updates:
                return json.dumps({"status": "error", "message": "no updates provided"})

            local_config = conf()
            applied = {}
            for key, value in updates.items():
                # 只修改白名单中的配置项，忽略其他键
                if key not in self.EDITABLE_KEYS:
                    continue
                # 数值类型配置项强制转换
                if key in ("agent_max_context_tokens", "agent_max_context_turns", "agent_max_steps"):
                    value = int(value)
                # 布尔类型配置项强制转换
                if key == "use_linkai":
                    value = bool(value)
                local_config[key] = value
                applied[key] = value

            if not applied:
                return json.dumps({"status": "error", "message": "no valid keys to update"})

            # 将更新持久化到config.json文件
            # 通过向上三层目录找到项目根目录下的config.json
            config_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__)))), "config.json")
            if os.path.exists(config_path):
                with open(config_path, "r", encoding="utf-8") as f:
                    file_cfg = json.load(f)
            else:
                file_cfg = {}
            file_cfg.update(applied)
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(file_cfg, f, indent=4, ensure_ascii=False)

            logger.info(f"[WebChannel] Config updated: {list(applied.keys())}")
            return json.dumps({"status": "success", "applied": applied}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Error updating config: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class ChannelsHandler:
    """
    渠道管理处理器，提供外部渠道（飞书、钉钉、企微等）的配置和连接管理。

    GET请求：获取所有渠道的配置状态，包括是否激活、配置字段值等。
    POST请求：执行渠道操作（保存配置/连接/断开）。

    支持的渠道：微信、飞书、钉钉、企微智能机器人、QQ机器人、企微自建应用、公众号。

    安全措施：
    - 密钥类型字段脱敏显示
    - 保存时忽略包含星号的密钥值（表示未修改）
    - 连接时先停止已有实例再启动新实例
    """

    """API for managing external channel configurations (feishu, dingtalk, etc)."""

    # 渠道定义表，包含各渠道的显示信息、图标、颜色和配置字段
    CHANNEL_DEFS = OrderedDict([
        ("weixin", {
            "label": {"zh": "微信", "en": "WeChat"},
            "icon": "fa-comment",
            "color": "emerald",
            "fields": [],
        }),
        ("feishu", {
            "label": {"zh": "飞书", "en": "Feishu"},
            "icon": "fa-paper-plane",
            "color": "blue",
            "fields": [
                {"key": "feishu_app_id", "label": "App ID", "type": "text"},
                {"key": "feishu_app_secret", "label": "App Secret", "type": "secret"},
                {"key": "feishu_token", "label": "Verification Token", "type": "secret"},
                {"key": "feishu_bot_name", "label": "Bot Name", "type": "text"},
            ],
        }),
        ("dingtalk", {
            "label": {"zh": "钉钉", "en": "DingTalk"},
            "icon": "fa-comments",
            "color": "blue",
            "fields": [
                {"key": "dingtalk_client_id", "label": "Client ID", "type": "text"},
                {"key": "dingtalk_client_secret", "label": "Client Secret", "type": "secret"},
            ],
        }),
        ("wecom_bot", {
            "label": {"zh": "企微智能机器人", "en": "WeCom Bot"},
            "icon": "fa-robot",
            "color": "emerald",
            "fields": [
                {"key": "wecom_bot_id", "label": "Bot ID", "type": "text"},
                {"key": "wecom_bot_secret", "label": "Secret", "type": "secret"},
            ],
        }),
        ("qq", {
            "label": {"zh": "QQ 机器人", "en": "QQ Bot"},
            "icon": "fa-comment",
            "color": "blue",
            "fields": [
                {"key": "qq_app_id", "label": "App ID", "type": "text"},
                {"key": "qq_app_secret", "label": "App Secret", "type": "secret"},
            ],
        }),
        ("wechatcom_app", {
            "label": {"zh": "企微自建应用", "en": "WeCom App"},
            "icon": "fa-building",
            "color": "emerald",
            "fields": [
                {"key": "wechatcom_corp_id", "label": "Corp ID", "type": "text"},
                {"key": "wechatcomapp_agent_id", "label": "Agent ID", "type": "text"},
                {"key": "wechatcomapp_secret", "label": "Secret", "type": "secret"},
                {"key": "wechatcomapp_token", "label": "Token", "type": "secret"},
                {"key": "wechatcomapp_aes_key", "label": "AES Key", "type": "secret"},
                {"key": "wechatcomapp_port", "label": "Port", "type": "number", "default": 9898},
            ],
        }),
        ("wechatmp", {
            "label": {"zh": "公众号", "en": "WeChat MP"},
            "icon": "fa-comment-dots",
            "color": "emerald",
            "fields": [
                {"key": "wechatmp_app_id", "label": "App ID", "type": "text"},
                {"key": "wechatmp_app_secret", "label": "App Secret", "type": "secret"},
                {"key": "wechatmp_token", "label": "Token", "type": "secret"},
                {"key": "wechatmp_aes_key", "label": "AES Key", "type": "secret"},
                {"key": "wechatmp_port", "label": "Port", "type": "number", "default": 8080},
            ],
        }),
    ])

    @staticmethod
    def _get_weixin_login_status() -> str:
        """
        获取微信渠道的登录状态。

        通过反射获取ChannelManager中的微信渠道实例，读取其login_status属性。
        用于在Web界面显示微信扫码登录的状态信息。

        Returns:
            登录状态字符串，获取失败时返回"unknown"
        """
        try:
            import sys
            # 通过sys.modules获取主模块，再获取ChannelManager实例
            app_module = sys.modules.get('__main__') or sys.modules.get('app')
            mgr = getattr(app_module, '_channel_mgr', None) if app_module else None
            if mgr:
                ch = mgr.get_channel("weixin")
                if ch and hasattr(ch, 'login_status'):
                    return ch.login_status
        except Exception:
            pass
        return "unknown"

    @staticmethod
    def _mask_secret(value: str) -> str:
        """
        对密钥值进行脱敏处理。

        与_mask_key方法功能相同，只保留前4位和后4位，中间用星号替代。
        用于在API响应中安全地显示密钥信息。

        Args:
            value: 原始密钥字符串

        Returns:
            脱敏后的密钥字符串
        """
        if not value or len(value) <= 8:
            return value
        return value[:4] + "*" * (len(value) - 8) + value[-4:]

    @staticmethod
    def _parse_channel_list(raw) -> list:
        """
        解析渠道列表配置。

        支持两种格式：
        - 列表格式：直接返回去除空格后的列表
        - 逗号分隔字符串：拆分为列表

        这是因为config.json中channel_type可能是列表或逗号分隔字符串。

        Args:
            raw: 原始渠道列表数据（列表或逗号分隔字符串）

        Returns:
            去除空白项的渠道名称列表
        """
        if isinstance(raw, list):
            return [ch.strip() for ch in raw if ch.strip()]
        if isinstance(raw, str):
            return [ch.strip() for ch in raw.split(",") if ch.strip()]
        return []

    @classmethod
    def _active_channel_set(cls) -> set:
        """
        获取当前激活的渠道名称集合。

        从配置中读取channel_type字段，解析为集合，便于快速判断某个渠道是否已激活。

        Returns:
            已激活渠道名称的集合
        """
        return set(cls._parse_channel_list(conf().get("channel_type", "")))

    def GET(self):
        """
        获取所有渠道的配置状态。

        遍历所有已定义的渠道，收集其配置字段值（密钥脱敏显示），
        以及是否已激活的状态信息。对于微信渠道，额外返回登录状态。

        Returns:
            JSON字符串，包含所有渠道的配置和状态信息
        """
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            local_config = conf()
            active_channels = self._active_channel_set()
            channels = []
            for ch_name, ch_def in self.CHANNEL_DEFS.items():
                fields_out = []
                for f in ch_def["fields"]:
                    raw_val = local_config.get(f["key"], f.get("default", ""))
                    # 密钥类型字段脱敏显示
                    if f["type"] == "secret" and raw_val:
                        display_val = self._mask_secret(str(raw_val))
                    else:
                        display_val = raw_val
                    fields_out.append({
                        "key": f["key"],
                        "label": f["label"],
                        "type": f["type"],
                        "value": display_val,
                        "default": f.get("default", ""),
                    })
                ch_info = {
                    "name": ch_name,
                    "label": ch_def["label"],
                    "icon": ch_def["icon"],
                    "color": ch_def["color"],
                    "active": ch_name in active_channels,
                    "fields": fields_out,
                }
                # 微信渠道额外返回登录状态
                if ch_name == "weixin" and ch_name in active_channels:
                    ch_info["login_status"] = self._get_weixin_login_status()
                channels.append(ch_info)
            return json.dumps({"status": "success", "channels": channels}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] Channels API error: {e}")
            return json.dumps({"status": "error", "message": str(e)})

    def POST(self):
        """
        执行渠道操作的POST请求处理。

        根据action字段执行不同操作：
        - save: 保存渠道配置（不改变连接状态）
        - connect: 保存配置并启动渠道
        - disconnect: 停止渠道并从channel_type中移除

        Args:
            请求体中需包含action和channel字段

        Returns:
            JSON字符串，包含操作结果
        """
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            body = json.loads(web.data())
            action = body.get("action")
            channel_name = body.get("channel")

            if not action or not channel_name:
                return json.dumps({"status": "error", "message": "action and channel required"})

            if channel_name not in self.CHANNEL_DEFS:
                return json.dumps({"status": "error", "message": f"unknown channel: {channel_name}"})

            if action == "save":
                return self._handle_save(channel_name, body.get("config", {}))
            elif action == "connect":
                return self._handle_connect(channel_name, body.get("config", {}))
            elif action == "disconnect":
                return self._handle_disconnect(channel_name)
            else:
                return json.dumps({"status": "error", "message": f"unknown action: {action}"})
        except Exception as e:
            logger.error(f"[WebChannel] Channels POST error: {e}")
            return json.dumps({"status": "error", "message": str(e)})

    def _handle_save(self, channel_name: str, updates: dict):
        """
        处理保存渠道配置的操作。

        只更新该渠道定义中声明的配置字段，忽略其他字段。
        密钥字段如果包含星号（脱敏占位符）则跳过更新，保留原值。
        如果该渠道当前已激活，保存后会自动重启以使配置生效。

        Args:
            channel_name: 渠道名称
            updates: 要更新的配置键值对

        Returns:
            JSON字符串，包含已应用的配置项和是否重启了渠道
        """
        ch_def = self.CHANNEL_DEFS[channel_name]
        valid_keys = {f["key"] for f in ch_def["fields"]}
        secret_keys = {f["key"] for f in ch_def["fields"] if f["type"] == "secret"}

        local_config = conf()
        applied = {}
        for key, value in updates.items():
            # 只处理该渠道定义中声明的字段
            if key not in valid_keys:
                continue
            # 密钥字段如果包含星号占位符，说明前端未修改，跳过更新
            if key in secret_keys:
                if not value or (len(value) > 8 and "*" * 4 in value):
                    continue
            # 根据字段类型进行类型转换
            field_def = next((f for f in ch_def["fields"] if f["key"] == key), None)
            if field_def:
                if field_def["type"] == "number":
                    value = int(value)
                elif field_def["type"] == "bool":
                    value = bool(value)
            local_config[key] = value
            applied[key] = value

        if not applied:
            return json.dumps({"status": "error", "message": "no valid fields to update"})

        # 将配置持久化到config.json
        config_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)))), "config.json")
        if os.path.exists(config_path):
            with open(config_path, "r", encoding="utf-8") as f:
                file_cfg = json.load(f)
        else:
            file_cfg = {}
        file_cfg.update(applied)
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(file_cfg, f, indent=4, ensure_ascii=False)

        logger.info(f"[WebChannel] Channel '{channel_name}' config updated: {list(applied.keys())}")

        # 如果该渠道当前已激活，保存配置后需要重启以使配置生效
        should_restart = False
        active_channels = self._active_channel_set()
        if channel_name in active_channels:
            should_restart = True
            try:
                import sys
                # 通过ChannelManager重启渠道
                app_module = sys.modules.get('__main__') or sys.modules.get('app')
                mgr = getattr(app_module, '_channel_mgr', None) if app_module else None
                if mgr:
                    # 在后台线程中执行重启，避免阻塞HTTP请求
                    threading.Thread(
                        target=mgr.restart,
                        args=(channel_name,),
                        daemon=True,
                    ).start()
                    logger.info(f"[WebChannel] Channel '{channel_name}' restart triggered")
            except Exception as e:
                logger.warning(f"[WebChannel] Failed to restart channel '{channel_name}': {e}")

        return json.dumps({
            "status": "success",
            "applied": list(applied.keys()),
            "restarted": should_restart,
        }, ensure_ascii=False)

    def _handle_connect(self, channel_name: str, updates: dict):
        """
        处理连接渠道的操作。

        保存配置字段、将渠道添加到channel_type中、启动渠道。
        对于飞书渠道，强制使用websocket模式（因为通过Web控制台连接
        需要长连接模式，而非回调模式）。

        启动流程：
        1. 保存配置到内存和config.json
        2. 将渠道名称添加到channel_type列表
        3. 如果该渠道已有运行实例，先停止旧实例
        4. 等待5秒让远端服务释放旧连接（如钉钉会在重复连接时丢弃回调）
        5. 清除单例缓存并启动新实例

        Args:
            channel_name: 渠道名称
            updates: 要更新的配置键值对

        Returns:
            JSON字符串，包含操作结果和更新后的channel_type
        """
        """Save config fields, add channel to channel_type, and start it."""
        ch_def = self.CHANNEL_DEFS[channel_name]
        valid_keys = {f["key"] for f in ch_def["fields"]}
        secret_keys = {f["key"] for f in ch_def["fields"] if f["type"] == "secret"}

        # Feishu connected via web console must use websocket (long connection) mode
        # 通过Web控制台连接飞书时必须使用websocket长连接模式
        # 因为Web控制台无法接收外部回调，只能使用主动拉取消息的方式
        if channel_name == "feishu":
            updates.setdefault("feishu_event_mode", "websocket")
            valid_keys.add("feishu_event_mode")

        local_config = conf()
        applied = {}
        for key, value in updates.items():
            if key not in valid_keys:
                continue
            # 密钥字段包含星号占位符时跳过更新
            if key in secret_keys:
                if not value or (len(value) > 8 and "*" * 4 in value):
                    continue
            # 根据字段类型进行类型转换
            field_def = next((f for f in ch_def["fields"] if f["key"] == key), None)
            if field_def:
                if field_def["type"] == "number":
                    value = int(value)
                elif field_def["type"] == "bool":
                    value = bool(value)
            local_config[key] = value
            applied[key] = value

        # 将渠道名称添加到channel_type列表中
        existing = self._parse_channel_list(conf().get("channel_type", ""))
        if channel_name not in existing:
            existing.append(channel_name)
        new_channel_type = ",".join(existing)
        local_config["channel_type"] = new_channel_type

        # 将配置持久化到config.json
        config_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)))), "config.json")
        if os.path.exists(config_path):
            with open(config_path, "r", encoding="utf-8") as f:
                file_cfg = json.load(f)
        else:
            file_cfg = {}
        file_cfg.update(applied)
        file_cfg["channel_type"] = new_channel_type
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(file_cfg, f, indent=4, ensure_ascii=False)

        logger.info(f"[WebChannel] Channel '{channel_name}' connecting, channel_type={new_channel_type}")

        def _do_start():
            """
            在后台线程中执行渠道启动操作。

            包括停止旧实例、等待旧连接释放、清除单例缓存和启动新实例。
            这些步骤必须在后台线程中执行，避免阻塞HTTP响应。
            """
            try:
                import sys
                app_module = sys.modules.get('__main__') or sys.modules.get('app')
                clear_fn = getattr(app_module, '_clear_singleton_cache', None) if app_module else None
                mgr = getattr(app_module, '_channel_mgr', None) if app_module else None
                if mgr is None:
                    logger.warning(f"[WebChannel] ChannelManager not available, cannot start '{channel_name}'")
                    return
                # Stop existing instance first if still running (e.g. re-connect without disconnect)
                # 先停止已有的运行实例（例如用户未断开就重新连接的情况）
                existing_ch = mgr.get_channel(channel_name)
                if existing_ch is not None:
                    logger.info(f"[WebChannel] Stopping existing '{channel_name}' before reconnect...")
                    mgr.stop(channel_name)
                # Always wait for the remote service to release the old connection before
                # establishing a new one (DingTalk drops callbacks on duplicate connections)
                # 等待5秒让远端服务释放旧连接
                # 某些平台（如钉钉）在检测到重复连接时会丢弃回调，必须确保旧连接完全释放
                logger.info(f"[WebChannel] Waiting for '{channel_name}' old connection to close...")
                time.sleep(5)
                # 清除单例缓存，否则新实例无法创建（因为旧单例仍被缓存）
                if clear_fn:
                    clear_fn(channel_name)
                logger.info(f"[WebChannel] Starting channel '{channel_name}'...")
                mgr.start([channel_name], first_start=False)
                logger.info(f"[WebChannel] Channel '{channel_name}' start completed")
            except Exception as e:
                logger.error(f"[WebChannel] Failed to start channel '{channel_name}': {e}",
                             exc_info=True)

        # 在后台线程中启动，避免阻塞HTTP响应
        threading.Thread(target=_do_start, daemon=True).start()

        return json.dumps({
            "status": "success",
            "channel_type": new_channel_type,
        }, ensure_ascii=False)

    def _handle_disconnect(self, channel_name: str):
        """
        处理断开渠道连接的操作。

        从channel_type列表中移除该渠道，更新内存配置和config.json文件，
        然后在后台线程中停止渠道实例并清除单例缓存。

        Args:
            channel_name: 要断开的渠道名称

        Returns:
            JSON字符串，包含操作结果和更新后的channel_type
        """
        # 从channel_type列表中移除该渠道
        existing = self._parse_channel_list(conf().get("channel_type", ""))
        existing = [ch for ch in existing if ch != channel_name]
        new_channel_type = ",".join(existing)

        local_config = conf()
        local_config["channel_type"] = new_channel_type

        # 更新config.json文件
        config_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)))), "config.json")
        if os.path.exists(config_path):
            with open(config_path, "r", encoding="utf-8") as f:
                file_cfg = json.load(f)
        else:
            file_cfg = {}
        file_cfg["channel_type"] = new_channel_type
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(file_cfg, f, indent=4, ensure_ascii=False)

        def _do_stop():
            """
            在后台线程中执行渠道停止操作。

            停止渠道实例并清除单例缓存，确保后续可以重新创建新实例。
            """
            try:
                import sys
                app_module = sys.modules.get('__main__') or sys.modules.get('app')
                mgr = getattr(app_module, '_channel_mgr', None) if app_module else None
                clear_fn = getattr(app_module, '_clear_singleton_cache', None) if app_module else None
                if mgr:
                    mgr.stop(channel_name)
                else:
                    logger.warning(f"[WebChannel] ChannelManager not found, cannot stop '{channel_name}'")
                # 清除单例缓存，确保下次连接时可以创建新实例
                if clear_fn:
                    clear_fn(channel_name)
                logger.info(f"[WebChannel] Channel '{channel_name}' disconnected, "
                            f"channel_type={new_channel_type}")
            except Exception as e:
                logger.warning(f"[WebChannel] Failed to stop channel '{channel_name}': {e}",
                               exc_info=True)

        # 在后台线程中停止，避免阻塞HTTP响应
        threading.Thread(target=_do_stop, daemon=True).start()

        return json.dumps({
            "status": "success",
            "channel_type": new_channel_type,
        }, ensure_ascii=False)


class WeixinQrHandler:
    """
    微信二维码登录处理器，处理来自Web控制台的微信扫码登录请求。

    GET  /api/weixin/qrlogin          → 获取新的二维码
    POST /api/weixin/qrlogin          → 轮询二维码状态或登录后启动渠道

    支持的操作：
    - 获取二维码图片（data URI格式）
    - 轮询扫码状态（等待/已确认/已过期）
    - 扫码确认后自动保存凭据并启动微信渠道
    - 二维码过期后自动刷新
    """

    """Handle WeChat QR code login from the web console.

    GET  /api/weixin/qrlogin          → fetch a new QR code
    POST /api/weixin/qrlogin          → poll QR status or start channel after login
    """

    # 类级别状态字典，存储当前二维码会话信息
    _qr_state = {}

    @staticmethod
    def _qr_to_data_uri(data: str) -> str:
        """
        将二维码内容生成PNG格式的data URI。

        使用qrcode库将字符串内容编码为QR码图片，再转为base64编码的data URI，
        以便直接在HTML的<img>标签中使用，无需额外的图片文件。

        Args:
            data: 二维码内容字符串（通常是URL）

        Returns:
            PNG格式的data URI字符串，如 "data:image/png;base64,xxxxx"
            如果qrcode库未安装则返回空字符串
        """
        """Generate a QR code as a PNG data URI."""
        try:
            import qrcode as qr_lib
            import io
            import base64
            qr = qr_lib.QRCode(error_correction=qr_lib.constants.ERROR_CORRECT_L, box_size=6, border=2)
            qr.add_data(data)
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white")
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            b64 = base64.b64encode(buf.getvalue()).decode("ascii")
            return f"data:image/png;base64,{b64}"
        except ImportError:
            return ""

    @staticmethod
    def _get_running_channel():
        """
        获取当前正在运行的微信渠道实例。

        通过反射从ChannelManager中获取微信渠道实例，用于读取其二维码URL等信息。

        Returns:
            微信渠道实例，如果不存在或获取失败则返回None
        """
        try:
            import sys
            app_module = sys.modules.get('__main__') or sys.modules.get('app')
            mgr = getattr(app_module, '_channel_mgr', None) if app_module else None
            if mgr:
                return mgr.get_channel("weixin")
        except Exception:
            pass
        return None

    def GET(self):
        """
        获取微信登录二维码。

        优先从正在运行的微信渠道实例获取二维码URL，因为渠道可能已经在
        等待扫码。如果没有运行中的实例，则通过WeixinApi主动请求新的二维码。

        Returns:
            JSON字符串，包含二维码URL和图片data URI
        """
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            # 优先从运行中的渠道获取二维码
            running_ch = self._get_running_channel()
            if running_ch and hasattr(running_ch, '_current_qr_url') and running_ch._current_qr_url:
                qr_image = self._qr_to_data_uri(running_ch._current_qr_url)
                return json.dumps({
                    "status": "success",
                    "qrcode_url": running_ch._current_qr_url,
                    "qr_image": qr_image,
                    "source": "channel",
                })

            # 没有运行中的渠道，主动请求新的二维码
            from channel.weixin.weixin_api import WeixinApi, DEFAULT_BASE_URL
            base_url = conf().get("weixin_base_url", DEFAULT_BASE_URL)
            api = WeixinApi(base_url=base_url)
            qr_resp = api.fetch_qr_code()
            qrcode = qr_resp.get("qrcode", "")
            qrcode_url = qr_resp.get("qrcode_img_content", "")
            if not qrcode:
                return json.dumps({"status": "error", "message": "No QR code returned"})
            qr_image = self._qr_to_data_uri(qrcode_url)
            # 保存二维码会话状态，用于后续轮询
            WeixinQrHandler._qr_state = {
                "qrcode": qrcode,
                "qrcode_url": qrcode_url,
                "base_url": base_url,
            }
            return json.dumps({"status": "success", "qrcode_url": qrcode_url, "qr_image": qr_image})
        except Exception as e:
            logger.error(f"[WebChannel] WeixinQr GET error: {e}")
            return json.dumps({"status": "error", "message": str(e)})

    def POST(self):
        """
        处理微信二维码登录的POST请求。

        根据action字段执行不同操作：
        - poll: 轮询扫码状态
        - refresh: 刷新二维码（等同于GET请求）

        Args:
            请求体中需包含action字段

        Returns:
            JSON字符串，包含操作结果
        """
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            body = json.loads(web.data())
            action = body.get("action", "poll")

            if action == "poll":
                return self._poll_status()
            elif action == "refresh":
                return self.GET()
            else:
                return json.dumps({"status": "error", "message": f"unknown action: {action}"})
        except Exception as e:
            logger.error(f"[WebChannel] WeixinQr POST error: {e}")
            return json.dumps({"status": "error", "message": str(e)})

    def _poll_status(self):
        """
        轮询微信扫码登录的状态。

        使用保存的qrcode标识查询扫码状态，支持三种结果：
        - confirmed: 扫码确认，保存登录凭据到文件和内存配置
        - expired: 二维码已过期，自动获取新二维码并返回
        - 其他状态（如wait）: 返回当前状态让前端继续轮询

        登录确认后的处理：
        1. 从响应中提取bot_token、bot_id、base_url、user_id
        2. 保存凭据到凭据文件（~/.weixin_cow_credentials.json）
        3. 更新内存配置中的token和base_url
        4. 清除二维码会话状态

        Returns:
            JSON字符串，包含扫码状态信息
        """
        state = WeixinQrHandler._qr_state
        qrcode = state.get("qrcode", "")
        base_url = state.get("base_url", "")
        if not qrcode:
            return json.dumps({"status": "error", "message": "No active QR session"})

        from channel.weixin.weixin_api import WeixinApi, DEFAULT_BASE_URL
        api = WeixinApi(base_url=base_url or DEFAULT_BASE_URL)
        try:
            # 轮询扫码状态，设置10秒超时
            status_resp = api.poll_qr_status(qrcode, timeout=10)
        except Exception as e:
            return json.dumps({"status": "error", "message": str(e)})

        qr_status = status_resp.get("status", "wait")

        if qr_status == "confirmed":
            # 扫码确认登录，提取凭据信息
            bot_token = status_resp.get("bot_token", "")
            bot_id = status_resp.get("ilink_bot_id", "")
            result_base_url = status_resp.get("baseurl", base_url)
            user_id = status_resp.get("ilink_user_id", "")

            if not bot_token or not bot_id:
                return json.dumps({"status": "error", "message": "Login confirmed but missing token"})

            # 保存凭据到文件，确保重启后仍可自动登录
            cred_path = os.path.expanduser(
                conf().get("weixin_credentials_path", "~/.weixin_cow_credentials.json")
            )
            from channel.weixin.weixin_channel import _save_credentials
            _save_credentials(cred_path, {
                "token": bot_token,
                "base_url": result_base_url,
                "bot_id": bot_id,
                "user_id": user_id,
            })
            # 更新内存中的配置，使后续消息收发使用新的凭据
            conf()["weixin_token"] = bot_token
            conf()["weixin_base_url"] = result_base_url

            # 清除二维码会话状态
            WeixinQrHandler._qr_state = {}
            logger.info(f"[WebChannel] WeChat QR login confirmed: bot_id={bot_id}")

            return json.dumps({
                "status": "success",
                "qr_status": "confirmed",
                "bot_id": bot_id,
            })

        if qr_status == "expired":
            # 二维码已过期，自动获取新的二维码
            new_resp = api.fetch_qr_code()
            new_qrcode = new_resp.get("qrcode", "")
            new_qrcode_url = new_resp.get("qrcode_img_content", "")
            new_qr_image = self._qr_to_data_uri(new_qrcode_url)
            # 更新会话状态，使后续轮询使用新的二维码
            WeixinQrHandler._qr_state["qrcode"] = new_qrcode
            WeixinQrHandler._qr_state["qrcode_url"] = new_qrcode_url
            return json.dumps({
                "status": "success",
                "qr_status": "expired",
                "qrcode_url": new_qrcode_url,
                "qr_image": new_qr_image,
            })

        # 其他状态（如wait），前端继续轮询
        return json.dumps({"status": "success", "qr_status": qr_status})


def _get_workspace_root():
    """
    解析并返回Agent工作空间根目录路径。

    从配置中读取agent_workspace设置，使用expand_path展开路径中的
    特殊符号（如~表示用户主目录）。

    Returns:
        工作空间根目录的绝对路径
    """
    """Resolve the agent workspace directory."""
    from common.utils import expand_path
    return expand_path(conf().get("agent_workspace", "~/cow"))


class ToolsHandler:
    """
    工具列表处理器，处理GET到"/api/tools"的请求。

    返回当前已注册的所有Agent工具的名称和描述，
    供前端展示工具管理界面使用。
    """

    def GET(self):
        """
        获取工具列表。

        从ToolManager加载所有工具类，实例化后获取工具名称和描述。
        实例化失败的工具仍会返回名称，描述为空字符串。

        Returns:
            JSON字符串，包含工具列表
        """
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            from agent.tools.tool_manager import ToolManager
            tm = ToolManager()
            if not tm.tool_classes:
                tm.load_tools()
            tools = []
            for name, cls in tm.tool_classes.items():
                try:
                    instance = cls()
                    tools.append({
                        "name": name,
                        "description": instance.description,
                    })
                except Exception:
                    # 实例化失败时仍返回工具名称，描述为空
                    tools.append({"name": name, "description": ""})
            return json.dumps({"status": "success", "tools": tools}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] Tools API error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class SkillsHandler:
    """
    技能管理处理器，处理对"/api/skills"的GET和POST请求。

    GET请求：获取所有可用技能的列表。
    POST请求：执行技能开关操作（open/close）。

    技能是声明式的Markdown文件，通过YAML frontmatter定义元数据。
    """

    def GET(self):
        """
        获取技能列表。

        从SkillManager加载所有技能，通过SkillService查询技能详情。

        Returns:
            JSON字符串，包含技能列表
        """
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            from agent.skills.service import SkillService
            from agent.skills.manager import SkillManager
            workspace_root = _get_workspace_root()
            manager = SkillManager(custom_dir=os.path.join(workspace_root, "skills"))
            service = SkillService(manager)
            skills = service.query()
            return json.dumps({"status": "success", "skills": skills}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] Skills API error: {e}")
            return json.dumps({"status": "error", "message": str(e)})

    def POST(self):
        """
        执行技能开关操作。

        Args:
            请求体中需包含action（open/close）和name（技能名称）

        Returns:
            JSON字符串，包含操作结果
        """
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            from agent.skills.service import SkillService
            from agent.skills.manager import SkillManager
            body = json.loads(web.data())
            action = body.get("action")
            name = body.get("name")
            if not action or not name:
                return json.dumps({"status": "error", "message": "action and name are required"})
            workspace_root = _get_workspace_root()
            manager = SkillManager(custom_dir=os.path.join(workspace_root, "skills"))
            service = SkillService(manager)
            if action == "open":
                service.open({"name": name})
            elif action == "close":
                service.close({"name": name})
            else:
                return json.dumps({"status": "error", "message": f"unknown action: {action}"})
            return json.dumps({"status": "success"}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] Skills POST error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class MemoryHandler:
    """
    记忆文件列表处理器，处理GET到"/api/memory"的请求。

    返回Agent长期记忆文件的分页列表，供前端记忆管理界面使用。
    """

    def GET(self):
        """
        获取记忆文件列表。

        支持分页查询，通过page和page_size参数控制。

        Returns:
            JSON字符串，包含记忆文件列表和分页信息
        """
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            from agent.memory.service import MemoryService
            params = web.input(page='1', page_size='20')
            workspace_root = _get_workspace_root()
            service = MemoryService(workspace_root)
            result = service.list_files(page=int(params.page), page_size=int(params.page_size))
            return json.dumps({"status": "success", **result}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] Memory API error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class MemoryContentHandler:
    """
    记忆文件内容处理器，处理GET到"/api/memory/content"的请求。

    返回指定记忆文件的详细内容，供前端查看和编辑。
    """

    def GET(self):
        """
        获取记忆文件内容。

        Args:
            filename查询参数：要查看的记忆文件名

        Returns:
            JSON字符串，包含文件内容
        """
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            from agent.memory.service import MemoryService
            params = web.input(filename='')
            if not params.filename:
                return json.dumps({"status": "error", "message": "filename required"})
            workspace_root = _get_workspace_root()
            service = MemoryService(workspace_root)
            result = service.get_content(params.filename)
            return json.dumps({"status": "success", **result}, ensure_ascii=False)
        except FileNotFoundError:
            return json.dumps({"status": "error", "message": "file not found"})
        except Exception as e:
            logger.error(f"[WebChannel] Memory content API error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class SchedulerHandler:
    """
    定时任务处理器，处理GET到"/api/scheduler"的请求。

    返回当前已注册的所有定时任务列表，供前端任务管理界面使用。
    """

    def GET(self):
        """
        获取定时任务列表。

        从TaskStore加载所有任务数据，返回完整的任务列表。

        Returns:
            JSON字符串，包含任务列表
        """
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            from agent.tools.scheduler.task_store import TaskStore
            workspace_root = _get_workspace_root()
            store_path = os.path.join(workspace_root, "scheduler", "tasks.json")
            store = TaskStore(store_path)
            tasks = store.list_tasks()
            return json.dumps({"status": "success", "tasks": tasks}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] Scheduler API error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class HistoryHandler:
    """
    对话历史处理器，处理GET到"/api/history"的请求。

    返回指定会话的分页对话历史记录，供前端聊天界面加载历史消息使用。
    """

    def GET(self):
        """
        返回指定会话的分页对话历史记录。

        Query params:
            session_id  (required)
            page        int, default 1  (1 = most recent messages)
            page_size   int, default 20
        """
        web.header('Content-Type', 'application/json; charset=utf-8')
        web.header('Access-Control-Allow-Origin', '*')
        try:
            params = web.input(session_id='', page='1', page_size='20')
            session_id = params.session_id.strip()
            if not session_id:
                return json.dumps({"status": "error", "message": "session_id required"})

            from agent.memory import get_conversation_store
            store = get_conversation_store()
            result = store.load_history_page(
                session_id=session_id,
                page=int(params.page),
                page_size=int(params.page_size),
            )
            return json.dumps({"status": "success", **result}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] History API error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class LogsHandler:
    """
    运行日志处理器，处理GET到"/api/logs"的请求。

    使用SSE（Server-Sent Events）协议实时推送日志内容：
    1. 首先推送最近200行日志作为初始数据
    2. 然后持续监听日志文件的新增内容并推送
    3. 设置10分钟最大连接时间，超时后自动断开

    这种实时推送方式避免了前端反复轮询，能更高效地展示日志。
    """

    def GET(self):
        """Stream the last N lines of run.log as SSE, then tail new lines."""
        web.header('Content-Type', 'text/event-stream; charset=utf-8')
        web.header('Cache-Control', 'no-cache')
        web.header('X-Accel-Buffering', 'no')

        from config import get_root
        log_path = os.path.join(get_root(), "run.log")

        def generate():
            """
            SSE日志流生成器。

            先发送最近200行日志作为初始内容，然后持续监听新增行并推送。
            使用SSE注释行(": keepalive")作为心跳，防止连接被关闭。
            """
            if not os.path.isfile(log_path):
                yield b"data: {\"type\": \"error\", \"message\": \"run.log not found\"}\n\n"
                return

            # Read last 200 lines for initial display
            # 首先推送最近200行日志，让用户立即看到当前日志状态
            try:
                with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
                    lines = f.readlines()
                tail_lines = lines[-200:]
                chunk = ''.join(tail_lines)
                payload = json.dumps({"type": "init", "content": chunk}, ensure_ascii=False)
                yield f"data: {payload}\n\n".encode('utf-8')
            except Exception as e:
                yield f"data: {{\"type\": \"error\", \"message\": \"{e}\"}}\n\n".encode('utf-8')
                return

            # Tail new lines
            # 持续监听日志文件的新增内容并实时推送
            try:
                with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
                    f.seek(0, 2)  # seek to end  # 跳到文件末尾，只读取新增内容
                    deadline = time.time() + 600  # 10 min max  # 最大连接时间10分钟
                    while time.time() < deadline:
                        line = f.readline()
                        if line:
                            # 有新日志行，推送内容
                            payload = json.dumps({"type": "line", "content": line}, ensure_ascii=False)
                            yield f"data: {payload}\n\n".encode('utf-8')
                        else:
                            # 无新内容，发送keepalive心跳
                            yield b": keepalive\n\n"
                            time.sleep(1)
            except GeneratorExit:
                # 客户端断开连接时退出
                return
            except Exception:
                return

        return generate()


class AssetsHandler:
    """
    静态资源处理器，处理GET到"/assets/<file_path>"的请求。

    提供Web渠道static目录下静态文件的访问服务，包括CSS、JS等资源文件。
    包含路径安全检查，防止目录遍历攻击。
    """

    def GET(self, file_path):  # 修改默认参数
        """
        返回静态文件内容。

        Args:
            file_path: 请求的文件相对路径

        安全处理：
        - 路径规范化后检查是否在static目录内
        - 不在目录内的请求返回404，防止目录遍历攻击
        """
        try:
            # 如果请求是/static/，需要处理
            if file_path == '':
                # 返回目录列表...
                pass

            # 获取当前文件的绝对路径
            current_dir = os.path.dirname(os.path.abspath(__file__))
            static_dir = os.path.join(current_dir, 'static')

            full_path = os.path.normpath(os.path.join(static_dir, file_path))

            # 安全检查：确保请求的文件在static目录内
            # 防止通过../等路径遍历手段访问系统文件
            if not os.path.abspath(full_path).startswith(os.path.abspath(static_dir)):
                logger.error(f"Security check failed for path: {full_path}")
                raise web.notfound()

            if not os.path.exists(full_path) or not os.path.isfile(full_path):
                logger.error(f"File not found: {full_path}")
                raise web.notfound()

            # 设置正确的Content-Type
            content_type = mimetypes.guess_type(full_path)[0]
            if content_type:
                web.header('Content-Type', content_type)
            else:
                # 默认为二进制流
                web.header('Content-Type', 'application/octet-stream')

            # 读取并返回文件内容
            with open(full_path, 'rb') as f:
                return f.read()

        except Exception as e:
            logger.error(f"Error serving static file: {e}", exc_info=True)  # 添加更详细的错误信息
            raise web.notfound()
