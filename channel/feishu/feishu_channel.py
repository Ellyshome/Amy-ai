"""
飞书通道接入

支持两种事件接收模式:
1. webhook模式: 通过HTTP服务器接收事件(需要公网IP)
2. websocket模式: 通过长连接接收事件(本地开发友好)

通过配置项 feishu_event_mode 选择模式: "webhook" 或 "websocket"

为什么支持两种模式：
- webhook模式适合生产环境部署，服务器有公网IP，飞书服务器主动推送事件
- websocket模式适合本地开发和内网环境，无需公网IP，客户端主动建立长连接接收事件

@author Saboteur7
@Date 2023/11/19
"""

import importlib.util
import json
import logging
import os
import ssl
import threading
# -*- coding=utf-8 -*-
import uuid

import requests
import web

from bridge.context import Context
from bridge.context import ContextType
from bridge.reply import Reply, ReplyType
from channel.chat_channel import ChatChannel, check_prefix
from channel.feishu.feishu_message import FeishuMessage
from common import utils
from common.expired_dict import ExpiredDict
from common.log import logger
from common.singleton import singleton
from config import conf

# Suppress verbose logs from Lark SDK
# 抑制Lark SDK的冗余日志输出，避免干扰主程序日志
logging.getLogger("Lark").setLevel(logging.WARNING)

URL_VERIFICATION = "url_verification"
# URL验证类型常量，用于飞书事件订阅的首次验证握手

# Lazy-check for lark_oapi SDK availability without importing it at module level.
# The full `import lark_oapi` pulls in 10k+ files and takes 4-10s, so we defer
# the actual import to _startup_websocket() where it is needed.
# 延迟导入lark_oapi SDK：完整导入会加载1万多个文件，耗时4-10秒，
# 因此只在websocket模式启动时才真正导入，避免webhook模式下的不必要开销
LARK_SDK_AVAILABLE = importlib.util.find_spec("lark_oapi") is not None
lark = None  # will be populated on first use via _ensure_lark_imported()


def _ensure_lark_imported():
    """
    延迟导入lark_oapi SDK。

    该函数实现了按需导入策略：只有在实际需要websocket模式时才导入SDK。
    首次调用时执行真正的import操作（耗时4-10秒），后续调用直接返回缓存的对象。

    为什么延迟导入：
    1. lark_oapi SDK非常庞大（1万+文件），导入耗时4-10秒
    2. webhook模式完全不需要这个SDK，提前导入会拖慢启动速度
    3. 用户可能只使用webhook模式，不应强制安装lark_oapi

    Returns:
        module: lark_oapi模块对象
    """
    global lark
    if lark is None:
        import lark_oapi as _lark
        lark = _lark
    return lark


@singleton
class FeiShuChanel(ChatChannel):
    """
    飞书通道主类，继承自ChatChannel。

    该类负责：
    1. 飞书消息的接收和分发（支持webhook和websocket两种模式）
    2. 消息的回复发送（文本、图片、文件、视频等）
    3. 飞书API的access_token获取和媒体文件上传
    4. 消息去重、过期消息过滤和@机器人识别

    设计模式：
    - 使用singleton装饰器确保全局只有一个通道实例
    - webhook模式使用web.py框架启动HTTP服务器接收事件
    - websocket模式使用lark_oapi SDK建立长连接接收事件

    两种模式共享_handle_message_event方法处理消息核心逻辑，
    实现了模式无关的消息处理架构。
    """
    feishu_app_id = conf().get('feishu_app_id')
    feishu_app_secret = conf().get('feishu_app_secret')
    feishu_token = conf().get('feishu_token')          # 事件订阅验证令牌
    feishu_event_mode = conf().get('feishu_event_mode', 'websocket')  # webhook 或 websocket

    def __init__(self):
        """
        初始化飞书通道。

        主要完成以下工作：
        1. 创建消息去重字典，过期时间为7.1小时（略大于飞书消息的最大保留时间）
        2. 初始化连接相关资源（HTTP服务器和WebSocket客户端）
        3. 获取机器人的open_id用于@识别
        4. 配置群聊白名单和单聊前缀
        5. 验证配置完整性
        """
        super().__init__()
        # 历史消息id暂存，用于幂等控制
        # 过期时间7.1小时≈25560秒，确保在飞书可能重推的时间窗口内都能去重
        self.receivedMsgs = ExpiredDict(60 * 60 * 7.1)
        self._http_server = None     # webhook模式的HTTP服务器实例
        self._ws_client = None       # websocket模式的客户端实例
        self._ws_thread = None       # websocket模式的工作线程
        self._bot_open_id = None  # cached bot open_id for @-mention matching
        # 缓存机器人自身的open_id，用于在群聊中判断@是否指向本机器人，
        # 避免需要用户额外配置feishu_bot_name
        logger.debug("[FeiShu] app_id={}, app_secret={}, verification_token={}, event_mode={}".format(
            self.feishu_app_id, self.feishu_app_secret, self.feishu_token, self.feishu_event_mode))
        # 无需群校验和前缀 —— 所有群聊都处理，单聊不需要前缀触发
        conf()["group_name_white_list"] = ["ALL_GROUP"]
        conf()["single_chat_prefix"] = [""]

        # 验证配置
        # websocket模式依赖lark_oapi SDK，如果未安装则无法使用
        if self.feishu_event_mode == 'websocket' and not LARK_SDK_AVAILABLE:
            logger.error("[FeiShu] websocket mode requires lark_oapi. Please install: pip install lark-oapi")
            raise Exception("lark_oapi not installed")

    def startup(self):
        """
        启动飞书通道。

        根据配置的feishu_event_mode选择不同的启动方式：
        - websocket：通过长连接接收事件，适合本地开发
        - webhook：通过HTTP服务器接收事件，适合生产部署

        启动前会刷新配置（支持热更新）并获取机器人open_id。
        """
        self.feishu_app_id = conf().get('feishu_app_id')
        self.feishu_app_secret = conf().get('feishu_app_secret')
        self.feishu_token = conf().get('feishu_token')
        self.feishu_event_mode = conf().get('feishu_event_mode', 'websocket')
        self._fetch_bot_open_id()  # 预获取机器人open_id，用于后续@判断
        if self.feishu_event_mode == 'websocket':
            self._startup_websocket()
        else:
            self._startup_webhook()

    def _fetch_bot_open_id(self):
        """
        通过API获取机器人自身的open_id。

        该方法在启动时调用，将机器人的open_id缓存到_bot_open_id属性中。
        后续在群聊中判断@是否指向本机器人时，可以直接通过open_id匹配，
        无需用户额外配置feishu_bot_name。

        这是@识别的优先策略：open_id匹配 > 名称匹配 > 默认假设。

        API文档: https://open.feishu.cn/document/server-docs/authentication/authorization/authorization-code
        """
        try:
            access_token = self.fetch_access_token()
            if not access_token:
                logger.warning("[FeiShu] Cannot fetch bot info: no access_token")
                return
            headers = {"Authorization": "Bearer " + access_token}
            resp = requests.get("https://open.feishu.cn/open-apis/bot/v3/info/", headers=headers, timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("code") == 0:
                    self._bot_open_id = data.get("bot", {}).get("open_id")
                    logger.info(f"[FeiShu] Bot open_id fetched: {self._bot_open_id}")
                else:
                    logger.warning(f"[FeiShu] Fetch bot info failed: code={data.get('code')}, msg={data.get('msg')}")
        except Exception as e:
            logger.warning(f"[FeiShu] Fetch bot open_id error: {e}")

    def stop(self):
        """
        停止飞书通道。

        停止流程：
        1. 强制中断WebSocket工作线程（因为lark SDK的start()方法是阻塞的，
           没有提供优雅的停止方法，只能通过ctypes注入异常来中断）
        2. 停止HTTP服务器（如果是webhook模式）

        为什么使用ctypes强制中断线程：
        lark_oapi的ws.Client.start()方法是阻塞式的，没有提供stop()方法。
        当需要停止通道时，必须从外部强制中断该线程，否则它会一直阻塞。
        PyThreadState_SetAsyncExc可以向指定线程注入异常，使其从阻塞点退出。
        """
        import ctypes
        logger.info("[FeiShu] stop() called")
        ws_client = self._ws_client
        self._ws_client = None
        ws_thread = self._ws_thread
        self._ws_thread = None
        # Interrupt the ws thread first so its blocking start() unblocks
        # 首先中断WebSocket线程，让它的阻塞式start()方法退出
        if ws_thread and ws_thread.is_alive():
            try:
                tid = ws_thread.ident
                if tid:
                    # 向目标线程注入SystemExit异常，使其从阻塞点退出
                    res = ctypes.pythonapi.PyThreadState_SetAsyncExc(
                        ctypes.c_ulong(tid), ctypes.py_object(SystemExit)
                    )
                    if res == 1:
                        logger.info("[FeiShu] Interrupted ws thread via ctypes")
                    elif res > 1:
                        # 注入异常失败（res>1表示多个线程受影响），需要重置
                        ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_ulong(tid), None)
            except Exception as e:
                logger.warning(f"[FeiShu] Error interrupting ws thread: {e}")
        # lark.ws.Client has no stop() method; thread interruption above is sufficient
        if self._http_server:
            try:
                self._http_server.stop()
                logger.info("[FeiShu] HTTP server stopped")
            except Exception as e:
                logger.warning(f"[FeiShu] Error stopping HTTP server: {e}")
            self._http_server = None
        logger.info("[FeiShu] stop() completed")

    def _startup_webhook(self):
        """
        启动HTTP服务器接收事件（webhook模式）。

        使用web.py框架创建HTTP服务器，监听飞书的事件推送。
        服务器绑定到0.0.0.0，端口从配置feishu_port读取（默认9891）。

        webhook模式需要：
        1. 服务器有公网IP或通过内网穿透暴露
        2. 在飞书开放平台配置事件订阅URL
        3. 飞书服务器会主动推送事件到配置的URL
        """
        logger.debug("[FeiShu] Starting in webhook mode...")
        urls = (
            '/', 'channel.feishu.feishu_channel.FeishuController'
        )
        app = web.application(urls, globals(), autoreload=False)
        port = conf().get("feishu_port", 9891)
        func = web.httpserver.StaticMiddleware(app.wsgifunc())
        func = web.httpserver.LogMiddleware(func)
        server = web.httpserver.WSGIServer(("0.0.0.0", port), func)
        self._http_server = server
        try:
            server.start()
        except (KeyboardInterrupt, SystemExit):
            server.stop()

    def _startup_websocket(self):
        """
        启动长连接接收事件（websocket模式）。

        该方法创建lark_oapi的WebSocket客户端，在一个独立线程中运行。
        客户端会与飞书服务器建立长连接，实时接收事件推送。

        主要流程：
        1. 延迟导入lark_oapi SDK（仅在需要时导入，避免拖慢启动）
        2. 注册消息事件处理器
        3. 构建事件分发器
        4. 在独立线程中启动WebSocket客户端

        特殊处理：
        - SSL证书验证问题：某些环境下可能遇到证书验证失败，
          代码会在首次连接失败后自动禁用SSL验证重试
        - 事件循环冲突：前一个ws线程被强制中断后，其事件循环可能仍标记为"运行中"，
          导致新线程启动失败，代码会替换模块级的事件循环来修复此问题
        """
        _ensure_lark_imported()
        logger.debug("[FeiShu] Starting in websocket mode...")

        # 创建事件处理器
        def handle_message_event(data: lark.im.v1.P2ImMessageReceiveV1) -> None:
            """
            处理接收消息事件 v2.0。

            这是lark_oapi SDK的事件回调，当收到新消息时触发。
            主要逻辑：
            1. 将SDK消息对象转换为字典格式
            2. 过滤不需要处理的群聊消息（未@机器人的纯文本消息）
            3. 调用共用的消息处理方法

            Args:
                data: lark_oapi SDK的消息事件对象
            """
            try:
                # 将SDK对象序列化为JSON再反序列化为字典，统一数据格式
                event_dict = json.loads(lark.JSON.marshal(data))
                event = event_dict.get("event", {})
                msg = event.get("message", {})

                # Skip group messages that don't @-mention the bot (reduce log noise)
                # 跳过群聊中未@机器人的纯文本消息，减少日志噪音和不必要的处理
                # 飞书websocket模式会推送群内所有消息，需要在此过滤
                if msg.get("chat_type") == "group" and not msg.get("mentions") and msg.get("message_type") == "text":
                    return

                logger.debug(f"[FeiShu] websocket receive event: {lark.JSON.marshal(data, indent=2)}")

                # 处理消息 —— 调用与webhook模式共用的处理方法
                self._handle_message_event(event)

            except Exception as e:
                logger.error(f"[FeiShu] websocket handle message error: {e}", exc_info=True)

        # 构建事件分发器
        # 空字符串参数为verification_token和encrypt_key，websocket模式不需要
        event_handler = lark.EventDispatcherHandler.builder("", "") \
            .register_p2_im_message_receive_v1(handle_message_event) \
            .build()

        def start_client_with_retry():
            """
            在当前线程中启动WebSocket客户端，支持SSL错误重试。

            为什么在独立线程中运行：
            lark_oapi的ws.Client.start()是阻塞调用，会占用当前线程，
            因此必须在独立线程中运行以避免阻塞主线程。

            特殊处理：
            1. SSL证书验证：某些Linux环境下可能遇到证书验证失败，
               首次失败后会禁用SSL验证重试
            2. 事件循环冲突修复：前一个ws线程被ctypes强制终止后，
               其asyncio事件循环可能仍被lark_oapi模块缓存为"运行中"，
               导致新线程创建事件循环时冲突，此处替换模块级缓存解决
            """
            import asyncio
            import ssl as ssl_module
            # 保存原始的SSL上下文创建函数，用于重试失败后恢复
            original_create_default_context = ssl_module.create_default_context

            def create_unverified_context(*args, **kwargs):
                """创建不验证SSL证书的上下文，作为SSL验证失败后的降级方案"""
                context = original_create_default_context(*args, **kwargs)
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
                return context

            # lark_oapi.ws.client captures the event loop at module-import time as a module-
            # level global variable.  When a previous ws thread is force-killed via ctypes its
            # loop may still be marked as "running", which causes the next ws_client.start()
            # call (in this new thread) to raise "This event loop is already running".
            # Fix: replace the module-level loop with a brand-new, idle loop before starting.
            # 修复lark_oapi模块级事件循环缓存问题：
            # 当上一个ws线程被ctypes强制终止后，lark_oapi模块缓存的asyncio事件循环
            # 可能仍处于"running"状态，导致新线程中调用ws_client.start()时抛出
            # "This event loop is already running"异常。
            # 解决方案：在启动前替换模块级缓存的事件循环为新的空闲循环。
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                import lark_oapi.ws.client as _lark_ws_client_mod
                _lark_ws_client_mod.loop = loop
            except Exception:
                pass

            startup_error = None
            for attempt in range(2):
                # 最多重试2次：第一次正常连接，第二次禁用SSL验证
                try:
                    if attempt == 1:
                        # 第二次尝试：禁用SSL验证
                        logger.warning("[FeiShu] Retrying with SSL verification disabled...")
                        ssl_module.create_default_context = create_unverified_context
                        ssl_module._create_unverified_context = create_unverified_context

                    # 创建WebSocket客户端
                    ws_client = lark.ws.Client(
                        self.feishu_app_id,
                        self.feishu_app_secret,
                        event_handler=event_handler,
                        log_level=lark.LogLevel.WARNING  # 只记录WARNING及以上级别日志
                    )
                    self._ws_client = ws_client
                    logger.debug("[FeiShu] Websocket client starting...")
                    ws_client.start()  # 阻塞调用，直到连接断开
                    break

                except (SystemExit, KeyboardInterrupt):
                    # 收到停止信号，退出重试循环
                    logger.info("[FeiShu] Websocket thread received stop signal")
                    break
                except Exception as e:
                    error_msg = str(e)
                    is_ssl_error = ("CERTIFICATE_VERIFY_FAILED" in error_msg
                                    or "certificate verify failed" in error_msg.lower())
                    if is_ssl_error and attempt == 0:
                        # SSL证书验证错误，尝试禁用验证重试
                        logger.warning(f"[FeiShu] SSL error: {error_msg}, retrying...")
                        continue
                    # 非SSL错误或重试仍失败，记录错误
                    logger.error(f"[FeiShu] Websocket client error: {e}", exc_info=True)
                    startup_error = error_msg
                    # 恢复原始SSL上下文创建函数
                    ssl_module.create_default_context = original_create_default_context
                    break
            if startup_error:
                self.report_startup_error(startup_error)
            try:
                loop.close()
            except Exception:
                pass
            logger.info("[FeiShu] Websocket thread exited")

        # 在守护线程中启动WebSocket客户端
        # daemon=True确保主进程退出时该线程也会被终止
        ws_thread = threading.Thread(target=start_client_with_retry, daemon=True)
        self._ws_thread = ws_thread
        ws_thread.start()
        logger.info("[FeiShu] ✅ Websocket thread started, ready to receive messages")
        # 阻塞等待线程结束，保持通道运行
        ws_thread.join()

    def _is_mention_bot(self, mentions: list) -> bool:
        """
        判断@列表中是否包含本机器人。

        该方法实现了三级匹配策略，优先使用最可靠的方式：

        Priority:
        1. Match by open_id (obtained from /bot/v3/info at startup, no config needed)
           通过open_id匹配（启动时从API获取，无需额外配置，最可靠）
        2. Fallback to feishu_bot_name config for backward compatibility
           通过机器人名称匹配（需要配置feishu_bot_name，向后兼容）
        3. If neither is available, assume the first mention is the bot (Feishu only
           delivers group messages that @-mention the bot, so this is usually correct)
           如果以上都不可用，假设@的就是机器人（因为飞书事件订阅只会推送@机器人的消息）

        Args:
            mentions: 飞书消息中的@列表，每个元素包含id和name信息

        Returns:
            bool: 如果@了本机器人返回True
        """
        if self._bot_open_id:
            # 策略1：通过open_id匹配，最准确
            return any(
                m.get("id", {}).get("open_id") == self._bot_open_id
                for m in mentions
            )
        bot_name = conf().get("feishu_bot_name")
        if bot_name:
            # 策略2：通过机器人名称匹配，向后兼容
            return any(m.get("name") == bot_name for m in mentions)
        # Feishu event subscription only delivers messages that @-mention the bot,
        # so reaching here means the bot was indeed mentioned.
        # 策略3：飞书事件订阅机制保证只推送@机器人的消息，因此可以安全假设
        return True

    def _handle_message_event(self, event: dict):
        """
        处理消息事件的核心逻辑。

        该方法是webhook和websocket模式共用的消息处理入口，
        实现了模式无关的消息处理架构。

        处理流程：
        1. 消息有效性校验
        2. 消息去重（幂等控制）
        3. 过期消息过滤
        4. 群聊@判断
        5. 构建飞书消息对象
        6. 文件缓存处理（图片+文本的联合理解）
        7. 上下文构建和消息分发

        Args:
            event: 飞书事件字典，包含message和sender等字段
        """
        if not event.get("message") or not event.get("sender"):
            logger.warning(f"[FeiShu] invalid message, event={event}")
            return

        msg = event.get("message")

        # 幂等判断 —— 防止消息重复处理
        # 飞书可能在网络抖动时重推同一条消息，必须去重
        msg_id = msg.get("message_id")
        if self.receivedMsgs.get(msg_id):
            logger.warning(f"[FeiShu] repeat msg filtered, msg_id={msg_id}")
            return
        self.receivedMsgs[msg_id] = True

        # Filter out stale messages from before channel startup (offline backlog)
        # 过滤离线积压消息：机器人离线期间的消息如果超过60秒则跳过
        import time as _time
        create_time_ms = msg.get("create_time")
        if create_time_ms:
            msg_age_s = _time.time() - int(create_time_ms) / 1000  # 飞书时间戳为毫秒
            if msg_age_s > 60:
                logger.warning(f"[FeiShu] stale msg filtered (age={msg_age_s:.0f}s), msg_id={msg_id}")
                return

        is_group = False
        chat_type = msg.get("chat_type")

        if chat_type == "group":
            # 群聊消息处理
            if not msg.get("mentions") and msg.get("message_type") == "text":
                # 群聊中未@不响应 —— 减少不必要的AI响应和资源消耗
                return
            if msg.get("mentions") and msg.get("message_type") == "text":
                # 有@但不是@本机器人，不响应
                if not self._is_mention_bot(msg.get("mentions")):
                    return
            # 群聊
            is_group = True
            receive_id_type = "chat_id"  # 群聊使用chat_id标识接收者
        elif chat_type == "p2p":
            # 私聊
            receive_id_type = "open_id"  # 私聊使用open_id标识接收者
        else:
            logger.warning("[FeiShu] message ignore")
            return

        # 构造飞书消息对象 —— 将平台消息转换为系统统一格式
        feishu_msg = FeishuMessage(event, is_group=is_group, access_token=self.fetch_access_token())
        if not feishu_msg:
            return

        # 处理文件缓存逻辑
        # 文件缓存机制：支持"先发图片后发文字"的多模态交互模式
        from channel.file_cache import get_file_cache
        file_cache = get_file_cache()

        # 获取 session_id（用于缓存关联）
        # session_id决定了文件缓存和对话上下文的作用域
        if is_group:
            if conf().get("group_shared_session", True):
                session_id = msg.get("chat_id")  # 群共享会话 —— 群内所有人共享上下文
            else:
                session_id = feishu_msg.from_user_id + "_" + msg.get("chat_id")  # 每人独立会话
        else:
            session_id = feishu_msg.from_user_id  # 私聊使用用户ID作为session_id

        # 如果是单张图片消息，缓存起来
        # 图片不直接处理，等待用户后续的文本提问后再一起处理，
        # 这样AI可以同时理解图片内容和用户问题
        if feishu_msg.ctype == ContextType.IMAGE:
            if hasattr(feishu_msg, 'image_path') and feishu_msg.image_path:
                file_cache.add(session_id, feishu_msg.image_path, file_type='image')
                logger.info(f"[FeiShu] Image cached for session {session_id}, waiting for user query...")
            # 单张图片不直接处理，等待用户提问
            return

        # 如果是文本消息，检查是否有缓存的文件
        # 将之前缓存的图片引用附加到当前文本消息中，实现多模态理解
        if feishu_msg.ctype == ContextType.TEXT:
            cached_files = file_cache.get(session_id)
            if cached_files:
                # 将缓存的文件附加到文本消息中
                file_refs = []
                for file_info in cached_files:
                    file_path = file_info['path']
                    file_type = file_info['type']
                    if file_type == 'image':
                        file_refs.append(f"[图片: {file_path}]")
                    elif file_type == 'video':
                        file_refs.append(f"[视频: {file_path}]")
                    else:
                        file_refs.append(f"[文件: {file_path}]")

                feishu_msg.content = feishu_msg.content + "\n" + "\n".join(file_refs)
                logger.info(f"[FeiShu] Attached {len(cached_files)} cached file(s) to user query")
                # 清除缓存 —— 文件引用已附加，无需再保留
                file_cache.clear(session_id)

        context = self._compose_context(
            feishu_msg.ctype,
            feishu_msg.content,
            isgroup=is_group,
            msg=feishu_msg,
            receive_id_type=receive_id_type,
            no_need_at=True  # 消息已通过@过滤，不需要再检查前缀
        )
        if context:
            self.produce(context)
        logger.debug(f"[FeiShu] query={feishu_msg.content}, type={feishu_msg.ctype}")

    def send(self, reply: Reply, context: Context):
        """
        发送回复消息的统一入口。

        根据reply类型选择不同的发送策略：
        1. IMAGE_URL：上传图片后发送图片消息
        2. FILE：区分视频和其他文件类型
           - 视频：上传后以media类型发送（飞书API要求mp4必须使用media类型）
           - 其他文件：上传后以file类型发送
        3. TEXT：发送文本消息

        发送方式：
        - 群聊中优先使用"回复"API（在原消息下显示回复，便于上下文关联）
        - 私聊或无原始消息时使用"发送"API（创建新消息）

        Args:
            reply: 回复对象，包含类型和内容
            context: 上下文对象，包含消息来源和会话信息
        """
        msg = context.get("msg")
        is_group = context["isgroup"]
        if msg:
            # 从消息对象中获取access_token（消息解析时已缓存）
            access_token = msg.access_token
        else:
            # 无原始消息时（如定时任务），重新获取token
            access_token = self.fetch_access_token()
        headers = {
            "Authorization": "Bearer " + access_token,
            "Content-Type": "application/json",
        }
        msg_type = "text"
        logger.debug(f"[FeiShu] sending reply, type={context.type}, content={reply.content[:100]}...")
        reply_content = reply.content
        content_key = "text"
        if reply.type == ReplyType.IMAGE_URL:
            # 图片上传 —— 将图片上传到飞书获取image_key
            reply_content = self._upload_image_url(reply.content, access_token)
            if not reply_content:
                logger.warning("[FeiShu] upload image failed")
                return
            msg_type = "image"
            content_key = "image_key"
        elif reply.type == ReplyType.FILE:
            # 如果有附加的文本内容，先发送文本
            # 某些场景下需要同时发送文本说明和文件，先发文本让用户看到上下文
            if hasattr(reply, 'text_content') and reply.text_content:
                logger.info(f"[FeiShu] Sending text before file: {reply.text_content[:50]}...")
                text_reply = Reply(ReplyType.TEXT, reply.text_content)
                self._send(text_reply, context)
                import time
                time.sleep(0.3)  # 短暂延迟，确保文本先到达，避免顺序错乱

            # 判断是否为视频文件 —— 根据文件扩展名区分
            file_path = reply.content
            if file_path.startswith("file://"):
                file_path = file_path[7:]

            is_video = file_path.lower().endswith(('.mp4', '.avi', '.mov', '.wmv', '.flv'))

            if is_video:
                # 视频上传（包含duration信息）
                upload_data = self._upload_video_url(reply.content, access_token)
                if not upload_data or not upload_data.get('file_key'):
                    logger.warning("[FeiShu] upload video failed")
                    return

                # 视频使用 media 类型（根据官方文档）
                # 错误码 230055 说明：上传 mp4 时必须使用 msg_type="media"
                # 这是飞书API的限制，使用file类型发送mp4会报错
                msg_type = "media"
                reply_content = upload_data  # 完整的上传响应数据（包含file_key和duration）
                logger.info(
                    f"[FeiShu] Sending video: file_key={upload_data.get('file_key')}, duration={upload_data.get('duration')}ms")
                content_key = None  # 直接序列化整个对象，不包装在content_key下
            else:
                # 其他文件使用 file 类型
                file_key = self._upload_file_url(reply.content, access_token)
                if not file_key:
                    logger.warning("[FeiShu] upload file failed")
                    return
                reply_content = file_key
                msg_type = "file"
                content_key = "file_key"

        # Check if we can reply to an existing message (need msg_id)
        # 群聊中优先使用"回复"方式，这样回复会显示在原消息下方，便于理解上下文
        can_reply = is_group and msg and hasattr(msg, 'msg_id') and msg.msg_id

        # Build content JSON
        # content_key为None时直接序列化（如视频消息），否则包装在content_key字段下
        content_json = json.dumps(reply_content, ensure_ascii=False) if content_key is None else json.dumps({content_key: reply_content}, ensure_ascii=False)
        logger.debug(f"[FeiShu] Sending message: msg_type={msg_type}, content={content_json[:200]}")

        if can_reply:
            # 群聊中回复已有消息 —— 使用reply API，回复会关联到原消息
            url = f"https://open.feishu.cn/open-apis/im/v1/messages/{msg.msg_id}/reply"
            data = {
                "msg_type": msg_type,
                "content": content_json
            }
            res = requests.post(url=url, headers=headers, json=data, timeout=(5, 10))
        else:
            # 发送新消息（私聊或群聊中无msg_id的情况，如定时任务）
            url = "https://open.feishu.cn/open-apis/im/v1/messages"
            params = {"receive_id_type": context.get("receive_id_type") or "open_id"}
            data = {
                "receive_id": context.get("receiver"),
                "msg_type": msg_type,
                "content": content_json
            }
            res = requests.post(url=url, headers=headers, params=params, json=data, timeout=(5, 10))
        res = res.json()
        if res.get("code") == 0:
            logger.info(f"[FeiShu] send message success")
        else:
            logger.error(f"[FeiShu] send message failed, code={res.get('code')}, msg={res.get('msg')}")

    def fetch_access_token(self) -> str:
        """
        获取飞书tenant_access_token。

        该token用于调用飞书开放平台的所有API，是API鉴权的凭证。
        tenant_access_token的有效期为2小时，但该方法不实现缓存，
        每次调用都会重新获取（简化实现，避免缓存过期问题）。

        API文档: https://open.feishu.cn/document/server-docs/authentication/tenant_access_token/tenant-access-token

        Returns:
            str: tenant_access_token字符串，失败返回空字符串
        """
        url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal/"
        headers = {
            "Content-Type": "application/json"
        }
        req_body = {
            "app_id": self.feishu_app_id,
            "app_secret": self.feishu_app_secret
        }
        data = bytes(json.dumps(req_body), encoding='utf8')
        response = requests.post(url=url, data=data, headers=headers)
        if response.status_code == 200:
            res = response.json()
            if res.get("code") != 0:
                logger.error(f"[FeiShu] get tenant_access_token error, code={res.get('code')}, msg={res.get('msg')}")
                return ""
            else:
                return res.get("tenant_access_token")
        else:
            logger.error(f"[FeiShu] fetch token error, res={response}")

    def _upload_image_url(self, img_url, access_token):
        """
        上传图片到飞书并返回image_key。

        支持两种图片来源：
        1. 本地文件（file://协议）：直接读取并上传
        2. HTTP URL：先下载到临时文件再上传

        上传后的image_key用于发送图片消息。

        Args:
            img_url: 图片URL，支持file://和http(s)://协议
            access_token: 飞书API访问令牌

        Returns:
            str: image_key，用于后续发送图片消息；失败返回None
        """
        logger.debug(f"[FeiShu] start process image, img_url={img_url}")

        # Check if it's a local file path (file:// protocol)
        if img_url.startswith("file://"):
            local_path = img_url[7:]  # Remove "file://" prefix
            logger.info(f"[FeiShu] uploading local file: {local_path}")

            if not os.path.exists(local_path):
                logger.error(f"[FeiShu] local file not found: {local_path}")
                return None

            # Upload directly from local file —— 直接从本地文件上传，无需中间步骤
            upload_url = "https://open.feishu.cn/open-apis/im/v1/images"
            data = {'image_type': 'message'}
            headers = {'Authorization': f'Bearer {access_token}'}

            with open(local_path, "rb") as file:
                upload_response = requests.post(upload_url, files={"image": file}, data=data, headers=headers)
                logger.info(f"[FeiShu] upload file, res={upload_response.content}")

                response_data = upload_response.json()
                if response_data.get("code") == 0:
                    return response_data.get("data").get("image_key")
                else:
                    logger.error(f"[FeiShu] upload failed: {response_data}")
                    return None

        # Original logic for HTTP URLs —— 先下载再上传
        response = requests.get(img_url)
        suffix = utils.get_path_suffix(img_url)
        temp_name = str(uuid.uuid4()) + "." + suffix  # 使用UUID生成唯一临时文件名
        if response.status_code == 200:
            # 将图片内容保存为临时文件
            with open(temp_name, "wb") as file:
                file.write(response.content)

        # upload —— 上传临时文件到飞书
        upload_url = "https://open.feishu.cn/open-apis/im/v1/images"
        data = {
            'image_type': 'message'
        }
        headers = {
            'Authorization': f'Bearer {access_token}',
        }
        with open(temp_name, "rb") as file:
            upload_response = requests.post(upload_url, files={"image": file}, data=data, headers=headers)
            logger.info(f"[FeiShu] upload file, res={upload_response.content}")
            os.remove(temp_name)  # 上传完成后删除临时文件
            return upload_response.json().get("data").get("image_key")

    def _get_video_duration(self, file_path: str) -> int:
        """
        获取视频时长（毫秒）。

        使用ffprobe命令行工具获取视频文件的时长信息。
        ffprobe是ffmpeg工具套件的一部分，可以分析媒体文件的元数据。

        为什么需要视频时长：飞书发送视频消息时，duration字段是必填项，
        缺少duration会导致API返回错误。

        Args:
            file_path: 视频文件的本地路径

        Returns:
            int: 视频时长（毫秒），获取失败返回0
        """
        try:
            import subprocess

            # 使用 ffprobe 获取视频时长
            # -v error: 只输出错误信息
            # -show_entries format=duration: 只显示时长
            # -of default=noprint_wrappers=1:nokey=1: 输出纯数值
            cmd = [
                'ffprobe',
                '-v', 'error',
                '-show_entries', 'format=duration',
                '-of', 'default=noprint_wrappers=1:nokey=1',
                file_path
            ]

            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if result.returncode == 0:
                duration_seconds = float(result.stdout.strip())
                # 转换为毫秒，因为飞书API要求duration以毫秒为单位
                duration_ms = int(duration_seconds * 1000)
                logger.info(f"[FeiShu] Video duration: {duration_seconds:.2f}s ({duration_ms}ms)")
                return duration_ms
            else:
                logger.warning(f"[FeiShu] Failed to get video duration via ffprobe: {result.stderr}")
                return 0
        except FileNotFoundError:
            # ffprobe未安装，视频时长将为0，可能导致部分飞书客户端无法正确显示视频
            logger.warning("[FeiShu] ffprobe not found, video duration will be 0. Install ffmpeg to fix this.")
            return 0
        except Exception as e:
            logger.warning(f"[FeiShu] Failed to get video duration: {e}")
            return 0

    def _upload_video_url(self, video_url, access_token):
        """
        上传视频到飞书并返回视频信息。

        与图片不同，视频上传需要额外提供duration（时长）信息，
        这是飞书API的硬性要求。

        支持两种视频来源：
        1. file:// URL：直接上传本地视频文件
        2. http(s):// URL：先下载到临时文件再上传

        Args:
            video_url: 视频URL
            access_token: 飞书API访问令牌

        Returns:
            dict: 包含file_key和duration（毫秒）的字典；失败返回None
        """
        local_path = None   # 本地视频文件路径
        temp_file = None    # 临时下载文件路径（仅HTTP URL场景使用）

        try:
            # For file:// URLs (local files), upload directly
            if video_url.startswith("file://"):
                local_path = video_url[7:]  # Remove file:// prefix
                if not os.path.exists(local_path):
                    logger.error(f"[FeiShu] local video file not found: {local_path}")
                    return None
            else:
                # For HTTP URLs, download first —— 先下载到本地再上传
                logger.info(f"[FeiShu] Downloading video from URL: {video_url}")
                response = requests.get(video_url, timeout=(5, 60))
                if response.status_code != 200:
                    logger.error(f"[FeiShu] download video failed, status={response.status_code}")
                    return None

                # Save to temp file —— 保存为临时文件
                import uuid
                file_name = os.path.basename(video_url) or "video.mp4"
                temp_file = str(uuid.uuid4()) + "_" + file_name  # UUID前缀确保文件名唯一

                with open(temp_file, "wb") as file:
                    file.write(response.content)

                logger.info(f"[FeiShu] Video downloaded, size={len(response.content)} bytes")
                local_path = temp_file

            # Get video duration —— 获取视频时长，飞书API要求必填
            duration = self._get_video_duration(local_path)

            # Upload to Feishu —— 上传视频文件
            file_name = os.path.basename(local_path)
            file_ext = os.path.splitext(file_name)[1].lower()
            # 目前飞书只支持mp4格式的file_type参数
            file_type_map = {'.mp4': 'mp4'}
            file_type = file_type_map.get(file_ext, 'mp4')

            upload_url = "https://open.feishu.cn/open-apis/im/v1/files"
            data = {
                'file_type': file_type,
                'file_name': file_name
            }
            # Add duration only if available (required for video/audio)
            # duration必须为整数类型，飞书API不接受字符串
            if duration:
                data['duration'] = duration  # Must be int, not string

            headers = {'Authorization': f'Bearer {access_token}'}

            logger.info(f"[FeiShu] Uploading video: file_name={file_name}, duration={duration}ms")

            with open(local_path, "rb") as file:
                upload_response = requests.post(
                    upload_url,
                    files={"file": file},
                    data=data,
                    headers=headers,
                    timeout=(5, 60)  # 5秒连接超时，60秒读取超时（视频文件可能较大）
                )
                logger.info(
                    f"[FeiShu] upload video response, status={upload_response.status_code}, res={upload_response.content}")

                response_data = upload_response.json()
                if response_data.get("code") == 0:
                    # Add duration to the response data (API doesn't return it)
                    # 飞书API不返回duration，但发送消息时需要，因此手动添加
                    upload_data = response_data.get("data")
                    upload_data['duration'] = duration  # Add our calculated duration
                    logger.info(
                        f"[FeiShu] Upload complete: file_key={upload_data.get('file_key')}, duration={duration}ms")
                    return upload_data
                else:
                    logger.error(f"[FeiShu] upload video failed: {response_data}")
                    return None

        except Exception as e:
            logger.error(f"[FeiShu] upload video exception: {e}")
            return None

        finally:
            # Clean up temp file —— 清理临时下载文件，避免磁盘空间浪费
            if temp_file and os.path.exists(temp_file):
                try:
                    os.remove(temp_file)
                except Exception as e:
                    logger.warning(f"[FeiShu] Failed to remove temp file {temp_file}: {e}")

    def _upload_file_url(self, file_url, access_token):
        """
        上传文件到飞书并返回file_key。

        支持两种文件来源：
        1. 本地文件（file://协议）：直接读取并上传
        2. HTTP URL：先下载到临时文件再上传

        飞书对文件类型有特定要求，不同类型的文件使用不同的file_type参数：
        - opus: 音频文件
        - mp4: 视频文件
        - pdf/doc/xls/ppt: 对应Office文档
        - stream: 其他所有类型（通用二进制流）

        Args:
            file_url: 文件URL，支持file://和http(s)://协议
            access_token: 飞书API访问令牌

        Returns:
            str: file_key，用于发送文件消息；失败返回None
        """
        logger.debug(f"[FeiShu] start process file, file_url={file_url}")

        # Check if it's a local file path (file:// protocol)
        if file_url.startswith("file://"):
            local_path = file_url[7:]  # Remove "file://" prefix
            logger.info(f"[FeiShu] uploading local file: {local_path}")

            if not os.path.exists(local_path):
                logger.error(f"[FeiShu] local file not found: {local_path}")
                return None

            # Get file info —— 获取文件元信息
            file_name = os.path.basename(local_path)
            file_ext = os.path.splitext(file_name)[1].lower()

            # Determine file type for Feishu API
            # Feishu supports: opus, mp4, pdf, doc, xls, ppt, stream (other types)
            # 飞书文件类型映射：扩展名到飞书file_type参数的转换
            file_type_map = {
                '.opus': 'opus',
                '.mp4': 'mp4',
                '.pdf': 'pdf',
                '.doc': 'doc', '.docx': 'doc',
                '.xls': 'xls', '.xlsx': 'xls',
                '.ppt': 'ppt', '.pptx': 'ppt',
            }
            file_type = file_type_map.get(file_ext, 'stream')  # Default to stream for other types
            # 不识别的扩展名统一使用stream类型，飞书会作为通用二进制文件处理

            # Upload file to Feishu —— 上传文件到飞书
            upload_url = "https://open.feishu.cn/open-apis/im/v1/files"
            data = {'file_type': file_type, 'file_name': file_name}
            headers = {'Authorization': f'Bearer {access_token}'}

            try:
                with open(local_path, "rb") as file:
                    upload_response = requests.post(
                        upload_url,
                        files={"file": file},
                        data=data,
                        headers=headers,
                        timeout=(5, 30)  # 5s connect, 30s read timeout
                    )
                    logger.info(
                        f"[FeiShu] upload file response, status={upload_response.status_code}, res={upload_response.content}")

                    response_data = upload_response.json()
                    if response_data.get("code") == 0:
                        return response_data.get("data").get("file_key")
                    else:
                        logger.error(f"[FeiShu] upload file failed: {response_data}")
                        return None
            except Exception as e:
                logger.error(f"[FeiShu] upload file exception: {e}")
                return None

        # For HTTP URLs, download first then upload —— HTTP URL场景：先下载再上传
        try:
            response = requests.get(file_url, timeout=(5, 30))
            if response.status_code != 200:
                logger.error(f"[FeiShu] download file failed, status={response.status_code}")
                return None

            # Save to temp file —— 保存为临时文件
            import uuid
            file_name = os.path.basename(file_url)
            temp_name = str(uuid.uuid4()) + "_" + file_name

            with open(temp_name, "wb") as file:
                file.write(response.content)

            # Upload —— 上传到飞书
            file_ext = os.path.splitext(file_name)[1].lower()
            file_type_map = {
                '.opus': 'opus', '.mp4': 'mp4', '.pdf': 'pdf',
                '.doc': 'doc', '.docx': 'doc',
                '.xls': 'xls', '.xlsx': 'xls',
                '.ppt': 'ppt', '.pptx': 'ppt',
            }
            file_type = file_type_map.get(file_ext, 'stream')

            upload_url = "https://open.feishu.cn/open-apis/im/v1/files"
            data = {'file_type': file_type, 'file_name': file_name}
            headers = {'Authorization': f'Bearer {access_token}'}

            with open(temp_name, "rb") as file:
                upload_response = requests.post(upload_url, files={"file": file}, data=data, headers=headers)
                logger.info(f"[FeiShu] upload file, res={upload_response.content}")

                response_data = upload_response.json()
                os.remove(temp_name)  # Clean up temp file —— 上传完成后清理临时文件

                if response_data.get("code") == 0:
                    return response_data.get("data").get("file_key")
                else:
                    logger.error(f"[FeiShu] upload file failed: {response_data}")
                    return None
        except Exception as e:
            logger.error(f"[FeiShu] upload file from URL exception: {e}")
            return None

    def _compose_context(self, ctype: ContextType, content, **kwargs):
        """
        构建消息处理上下文。

        该方法在父类ChatChannel的基础上，增加了飞书特有的上下文处理逻辑：
        1. 设置session_id（区分群聊共享会话和用户独立会话）
        2. 设置receiver（消息回复目标）
        3. 处理图片生成前缀
        4. 处理语音回复偏好

        session_id策略对AI的对话记忆至关重要：
        - 群聊共享会话：群内所有人的消息共享同一个对话历史，AI能看到群内完整对话
        - 用户独立会话：同一用户在不同群和私聊中有独立的对话历史

        Args:
            ctype: 消息内容类型
            content: 消息内容
            **kwargs: 额外参数，如isgroup、msg、receive_id_type等

        Returns:
            Context: 构建好的上下文对象
        """
        context = Context(ctype, content)
        context.kwargs = kwargs
        if "channel_type" not in context:
            context["channel_type"] = self.channel_type
        if "origin_ctype" not in context:
            context["origin_ctype"] = ctype

        cmsg = context["msg"]

        # Set session_id based on chat type —— 根据会话类型设置session_id
        if cmsg.is_group:
            # Group chat: check if group_shared_session is enabled
            if conf().get("group_shared_session", True):
                # All users in the group share the same session context
                # 群内共享会话：所有群成员共享对话历史，适合团队协作
                context["session_id"] = cmsg.other_user_id  # group_id
            else:
                # Each user has their own session within the group
                # This ensures:
                # - Same user in different groups have separate conversation histories
                # - Same user in private chat and group chat have separate histories
                # 用户独立会话：同一用户在不同群有独立历史，避免跨群上下文混淆
                context["session_id"] = f"{cmsg.from_user_id}:{cmsg.other_user_id}"
        else:
            # Private chat: use user_id only
            # 私聊：直接使用用户ID，每个用户有独立的对话历史
            context["session_id"] = cmsg.from_user_id

        context["receiver"] = cmsg.other_user_id

        if ctype == ContextType.TEXT:
            # 1.文本请求
            # 图片生成处理 —— 检查是否包含图片生成前缀（如"画"）
            img_match_prefix = check_prefix(content, conf().get("image_create_prefix"))
            if img_match_prefix:
                # 去除图片生成前缀，将消息类型改为IMAGE_CREATE
                content = content.replace(img_match_prefix, "", 1)
                context.type = ContextType.IMAGE_CREATE
            else:
                context.type = ContextType.TEXT
            context.content = content.strip()

        elif context.type == ContextType.VOICE:
            # 2.语音请求
            # 如果配置了语音回复语音，设置desire_rtype为VOICE
            if "desire_rtype" not in context and conf().get("voice_reply_voice"):
                context["desire_rtype"] = ReplyType.VOICE

        return context


class FeishuController:
    """
    HTTP服务器控制器，用于webhook模式。

    该类处理飞书通过HTTP推送的事件回调，包括：
    1. URL验证握手：飞书配置事件订阅时的首次验证
    2. 消息事件接收：处理收到的聊天消息

    该类作为web.py框架的控制器使用，URL映射在_startup_webhook中配置。
    """
    # 类常量
    FAILED_MSG = '{"success": false}'     # 失败响应消息
    SUCCESS_MSG = '{"success": true}'      # 成功响应消息
    MESSAGE_RECEIVE_TYPE = "im.message.receive_v1"  # 消息接收事件类型

    def GET(self):
        """
        处理GET请求，用于健康检查。

        Returns:
            str: 服务状态提示信息
        """
        return "Feishu service start success!"

    def POST(self):
        """
        处理POST请求，接收飞书事件推送。

        处理流程：
        1. URL验证握手：首次配置事件订阅时，飞书会发送验证请求，
           需要返回challenge值以确认服务器身份
        2. Token校验：验证请求中的token是否匹配配置的feishu_token，
           防止恶意请求
        3. 消息事件分发：将消息事件交给FeiShuChanel处理

        Returns:
            str: JSON格式的响应消息
        """
        try:
            channel = FeiShuChanel()

            request = json.loads(web.data().decode("utf-8"))
            logger.debug(f"[FeiShu] receive request: {request}")

            # 1.事件订阅回调验证
            # 飞书在配置事件订阅URL时，会发送type=url_verification的请求，
            # 服务器必须返回challenge值以完成验证
            if request.get("type") == URL_VERIFICATION:
                varify_res = {"challenge": request.get("challenge")}
                return json.dumps(varify_res)

            # 2.消息接收处理
            # token 校验 —— 确保请求来自飞书，而非恶意第三方
            header = request.get("header")
            if not header or header.get("token") != channel.feishu_token:
                return self.FAILED_MSG

            # 处理消息事件 —— 仅处理消息接收事件
            event = request.get("event")
            if header.get("event_type") == self.MESSAGE_RECEIVE_TYPE and event:
                channel._handle_message_event(event)

            return self.SUCCESS_MSG

        except Exception as e:
            logger.error(e)
            return self.FAILED_MSG
