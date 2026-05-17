"""
WeCom (企业微信) AI Bot channel via WebSocket long connection.
企业微信AI机器人通道，通过WebSocket长连接实现消息收发。

Supports:
- Single chat and group chat (text / image / file input & output)
  支持单聊和群聊（文本/图片/文件输入和输出）
- Scheduled task push via aibot_send_msg
  通过 aibot_send_msg 支持定时任务主动推送
- Heartbeat keep-alive and auto-reconnect
  心跳保活和自动重连机制
"""

import base64
import hashlib
import json
import math
import os
import threading
import time
import uuid

import requests
import websocket

from bridge.context import Context, ContextType
from bridge.reply import Reply, ReplyType
from channel.chat_channel import ChatChannel, check_prefix
from channel.wecom_bot.wecom_bot_message import WecomBotMessage
from common.expired_dict import ExpiredDict
from common.log import logger
from common.singleton import singleton
from common.ws_client_compat import websocket_app_run_forever
from config import conf

# 企业微信WebSocket服务地址，用于建立长连接
WECOM_WS_URL = "wss://openws.work.weixin.qq.com"
# 心跳发送间隔（秒），定期发送ping包保持连接活跃
HEARTBEAT_INTERVAL = 30
# 媒体分块上传时每块的大小（512KB），base64编码前的大小
MEDIA_CHUNK_SIZE = 512 * 1024  # 512KB per chunk (before base64 encoding)


@singleton
class WecomBotChannel(ChatChannel):
    """企业微信AI机器人通道类。

    通过WebSocket长连接与企业微信AI机器人平台通信，支持：
    - 接收单聊/群聊的文本、图片、文件、视频消息
    - 发送文本、图片、文件、视频回复
    - 流式消息推送（支持Agent模式的中间过程展示）
    - 心跳保活与断线自动重连
    - 媒体文件分块上传

    使用 @singleton 装饰器确保全局只有一个通道实例，
    避免重复连接和资源浪费。
    """

    def __init__(self):
        """初始化企业微信机器人通道。

        设置WebSocket连接相关状态、消息去重字典、流式消息状态等。
        同时配置群聊和单聊的默认行为：
        - group_name_white_list 设为 ["ALL_GROUP"] 表示响应所有群
        - single_chat_prefix 设为 [""] 表示无需前缀即可触发
        """
        super().__init__()
        # 机器人的唯一标识ID，从配置文件中读取
        self.bot_id = ""
        # 机器人的密钥，用于WebSocket订阅认证，从配置文件中读取
        self.bot_secret = ""
        # 已接收消息的去重缓存，过期时间为7.1小时，防止重复处理同一条消息
        self.received_msgs = ExpiredDict(60 * 60 * 7.1)
        # WebSocket连接实例
        self._ws = None
        # WebSocket线程，以守护线程方式运行
        self._ws_thread = None
        # 心跳线程，定期发送ping包
        self._heartbeat_thread = None
        # WebSocket连接状态标志
        self._connected = False
        # 停止事件，用于优雅地关闭连接和线程
        self._stop_event = threading.Event()
        # 等待响应的请求映射表：req_id -> (threading.Event, result_holder)
        # 用于实现发送请求后等待对应响应的同步机制
        self._pending_responses = {}  # req_id -> (threading.Event, result_holder)
        # 保护 _pending_responses 的线程锁，防止并发修改
        self._pending_lock = threading.Lock()
        # 流式消息状态映射表：req_id -> {"stream_id": str, "content": str}
        # 用于Agent模式下的流式响应，记录每个请求的流状态
        self._stream_states = {}  # req_id -> {"stream_id": str, "content": str}

        # 配置群聊白名单为所有群，即不限制群聊响应
        conf()["group_name_white_list"] = ["ALL_GROUP"]
        # 配置单聊前缀为空字符串，表示任何消息都会触发回复
        conf()["single_chat_prefix"] = [""]

    # ------------------------------------------------------------------
    # Lifecycle
    # 生命周期管理：启动和停止
    # ------------------------------------------------------------------

    def startup(self):
        """启动企业微信机器人通道。

        从配置中读取bot_id和bot_secret，然后建立WebSocket连接。
        如果缺少必要配置，记录错误并上报启动失败。
        """
        self.bot_id = conf().get("wecom_bot_id", "")
        self.bot_secret = conf().get("wecom_bot_secret", "")

        # 校验必要的配置项是否存在
        if not self.bot_id or not self.bot_secret:
            err = "[WecomBot] wecom_bot_id and wecom_bot_secret are required"
            logger.error(err)
            self.report_startup_error(err)
            return

        # 清除停止事件标志，允许连接和线程运行
        self._stop_event.clear()
        # 建立WebSocket连接
        self._start_ws()

    def stop(self):
        """停止企业微信机器人通道。

        设置停止事件标志，关闭WebSocket连接，清理相关状态。
        由于WebSocket线程会检测stop_event并退出，此处无需显式join线程。
        """
        logger.info("[WecomBot] stop() called")
        # 通知所有线程停止运行
        self._stop_event.set()
        # 关闭WebSocket连接
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass
        self._ws = None
        self._connected = False

    # ------------------------------------------------------------------
    # WebSocket connection
    # WebSocket连接管理：建立连接、发送消息、生成请求ID
    # ------------------------------------------------------------------

    def _start_ws(self):
        """建立WebSocket长连接。

        设置四个回调函数：
        - _on_open: 连接建立成功后发送订阅请求
        - _on_message: 收到消息后解析并处理
        - _on_error: 连接出错时记录错误日志
        - _on_close: 连接关闭时尝试自动重连

        WebSocket线程以守护线程方式启动，并通过join阻塞主线程，
        确保通道不会在连接存活期间退出。
        """

        def _on_open(ws):
            """WebSocket连接建立成功回调。

            连接成功后立即发送订阅请求，以注册机器人身份。
            """
            logger.info("[WecomBot] WebSocket connected, sending subscribe...")
            self._send_subscribe()

        def _on_message(ws, raw):
            """WebSocket消息接收回调。

            将原始JSON字符串解析为字典后交给 _handle_ws_message 处理。
            解析失败时记录错误但不中断连接。
            """
            try:
                data = json.loads(raw)
                self._handle_ws_message(data)
            except Exception as e:
                logger.error(f"[WecomBot] Failed to handle ws message: {e}", exc_info=True)

        def _on_error(ws, error):
            """WebSocket错误回调。记录错误日志，具体重连逻辑在_on_close中处理。"""
            logger.error(f"[WecomBot] WebSocket error: {error}")

        def _on_close(ws, close_status_code, close_msg):
            """WebSocket连接关闭回调。

            当连接非主动关闭时（即stop_event未设置），
            等待5秒后自动重连，避免因网络抖动导致永久断线。
            """
            logger.warning(f"[WecomBot] WebSocket closed: status={close_status_code}, msg={close_msg}")
            self._connected = False
            # 仅在非主动关闭的情况下尝试重连
            if not self._stop_event.is_set():
                logger.info("[WecomBot] Will reconnect in 5s...")
                time.sleep(5)
                # 再次检查stop_event，因为在sleep期间可能收到了stop指令
                if not self._stop_event.is_set():
                    self._start_ws()

        # 创建WebSocket应用实例，绑定回调函数
        self._ws = websocket.WebSocketApp(
            WECOM_WS_URL,
            on_open=_on_open,
            on_message=_on_message,
            on_error=_on_error,
            on_close=_on_close,
        )

        def run_forever():
            """WebSocket运行线程的目标函数。

            以守护线程方式运行WebSocket事件循环，
            ping_interval=0 表示禁用库内置的心跳（使用自定义心跳），
            reconnect=0 表示禁用库内置的重连（使用自定义重连逻辑）。
            """
            try:
                websocket_app_run_forever(self._ws, ping_interval=0, reconnect=0)
            except (SystemExit, KeyboardInterrupt):
                logger.info("[WecomBot] WebSocket thread interrupted")
            except Exception as e:
                logger.error(f"[WecomBot] WebSocket run_forever error: {e}")

        # 启动守护线程运行WebSocket事件循环
        self._ws_thread = threading.Thread(target=run_forever, daemon=True)
        self._ws_thread.start()
        # 阻塞主线程，保持通道运行直到WebSocket断开
        self._ws_thread.join()

    def _ws_send(self, data: dict):
        """通过WebSocket发送JSON数据。

        Args:
            data: 要发送的字典数据，会被序列化为JSON字符串。
                  ensure_ascii=False 确保中文字符不被转义。
        """
        if self._ws:
            self._ws.send(json.dumps(data, ensure_ascii=False))

    def _gen_req_id(self) -> str:
        """生成唯一的请求ID。

        使用UUID的前16位作为请求标识，用于请求-响应匹配。
        返回16位十六进制字符串。
        """
        return uuid.uuid4().hex[:16]

    # ------------------------------------------------------------------
    # Subscribe & heartbeat
    # 订阅与心跳：WebSocket订阅认证和心跳保活
    # ------------------------------------------------------------------

    def _send_subscribe(self):
        """发送机器人订阅请求。

        连接建立后必须发送此请求以注册机器人身份，
        服务端会验证bot_id和secret，验证成功后开始推送消息。
        """
        self._ws_send({
            "cmd": "aibot_subscribe",
            "headers": {"req_id": self._gen_req_id()},
            "body": {
                "bot_id": self.bot_id,
                "secret": self.bot_secret,
            },
        })

    def _start_heartbeat(self):
        """启动心跳线程。

        订阅成功后启动，定期发送ping命令保持连接活跃。
        如果心跳线程已在运行则不重复启动。
        心跳失败时（如连接已断开）自动退出循环，
        依赖_on_close回调进行重连。
        """
        # 避免重复启动心跳线程
        if self._heartbeat_thread and self._heartbeat_thread.is_alive():
            return

        def heartbeat_loop():
            """心跳循环：定期发送ping包。

            使用 _stop_event.wait() 代替 time.sleep()，
            这样在收到stop信号时可以立即退出而无需等待超时。
            """
            while not self._stop_event.is_set() and self._connected:
                try:
                    self._ws_send({
                        "cmd": "ping",
                        "headers": {"req_id": self._gen_req_id()},
                    })
                except Exception as e:
                    # 心跳发送失败，可能连接已断开，退出循环等待重连
                    logger.warning(f"[WecomBot] Heartbeat send failed: {e}")
                    break
                # 使用wait代替sleep，可以被stop_event立即唤醒
                self._stop_event.wait(HEARTBEAT_INTERVAL)

        self._heartbeat_thread = threading.Thread(target=heartbeat_loop, daemon=True)
        self._heartbeat_thread.start()

    # ------------------------------------------------------------------
    # Incoming message dispatch
    # 入站消息分发：请求-响应同步和消息路由
    # ------------------------------------------------------------------

    def _send_and_wait(self, data: dict, timeout: float = 15) -> dict:
        """发送WebSocket消息并等待匹配的响应。

        通过req_id实现请求-响应的同步匹配：
        1. 将请求的req_id注册到等待表
        2. 发送请求
        3. 阻塞等待响应（最长timeout秒）
        4. 返回响应数据或空字典（超时）

        此方法主要用于分块上传流程中，需要等待每一步的确认响应。

        Args:
            data: 要发送的请求数据，必须包含headers.req_id
            timeout: 等待响应的超时时间（秒），默认15秒

        Returns:
            响应数据字典，超时或出错时返回空字典
        """
        req_id = data.get("headers", {}).get("req_id", "")
        # 创建事件对象用于同步等待，holder用于存放响应数据
        event = threading.Event()
        holder = {"data": None}
        with self._pending_lock:
            self._pending_responses[req_id] = (event, holder)
        # 发送请求
        self._ws_send(data)
        # 阻塞等待响应，超时后自动解除
        event.wait(timeout=timeout)
        # 清理等待表
        with self._pending_lock:
            self._pending_responses.pop(req_id, None)
        return holder["data"] or {}

    def _handle_ws_message(self, data: dict):
        """处理收到的WebSocket消息，根据cmd字段分发到不同的处理逻辑。

        处理流程：
        1. 检查是否为已注册的等待响应，如果是则唤醒等待线程
        2. 处理订阅响应（订阅成功/失败）
        3. 处理消息回调（用户发送的消息）
        4. 处理事件回调（用户进入会话等事件）
        5. 处理其他响应错误

        Args:
            data: 解析后的WebSocket消息字典
        """
        cmd = data.get("cmd", "")
        errcode = data.get("errcode")
        req_id = data.get("headers", {}).get("req_id", "")

        # 检查是否为某个等待中请求的响应，如果是则唤醒对应的等待线程
        # Check if this is a response to a pending request
        if req_id:
            with self._pending_lock:
                pending = self._pending_responses.get(req_id)
            if pending:
                event, holder = pending
                # 将响应数据存入holder并唤醒等待线程
                holder["data"] = data
                event.set()
                return

        # 处理订阅响应（仅在连接未建立时处理）
        # Subscribe response (only handle once before connected)
        if errcode is not None and cmd == "":
            if not self._connected:
                if errcode == 0:
                    # 订阅成功，标记已连接，启动心跳
                    logger.info("[WecomBot] ✅ Subscribe success")
                    self._connected = True
                    self._start_heartbeat()
                    self.report_startup_success()
                else:
                    # 订阅失败，记录错误并上报启动失败
                    errmsg = data.get("errmsg", "unknown error")
                    logger.error(f"[WecomBot] Subscribe failed: errcode={errcode}, errmsg={errmsg}")
                    self.report_startup_error(errmsg)
            return

        # 根据cmd类型分发到具体的处理函数
        if cmd == "aibot_msg_callback":
            # 用户消息回调，包含文本、图片、文件等内容
            self._handle_msg_callback(data)
        elif cmd == "aibot_event_callback":
            # 事件回调，如用户进入会话、连接被抢占等
            self._handle_event_callback(data)
        elif cmd == "":
            # 非订阅响应的错误，记录警告
            if errcode and errcode != 0:
                logger.warning(f"[WecomBot] Response error: {data}")

    # ------------------------------------------------------------------
    # Message callback
    # 消息回调处理：解析入站消息并投递到处理队列
    # ------------------------------------------------------------------

    def _handle_msg_callback(self, data: dict):
        """处理用户消息回调。

        完整的处理流程：
        1. 消息去重：通过msgid过滤重复消息
        2. 解析消息：创建WecomBotMessage对象
        3. 文件缓存：图片和文件类型消息先缓存，等待后续文本消息一起处理
        4. 文本消息：如果有缓存的文件，将文件引用附加到文本内容中
        5. 组装上下文：创建Context对象并投递到处理队列

        Args:
            data: 消息回调的原始数据字典
        """
        body = data.get("body", {})
        req_id = data.get("headers", {}).get("req_id", "")
        msg_id = body.get("msgid", "")

        # 消息去重：如果msgid已存在则跳过，防止同一消息被处理多次
        if self.received_msgs.get(msg_id):
            logger.debug(f"[WecomBot] Duplicate msg filtered: {msg_id}")
            return
        self.received_msgs[msg_id] = True

        # 判断消息来源是单聊还是群聊
        chattype = body.get("chattype", "single")
        is_group = chattype == "group"

        # 尝试解析消息内容，不支持的消息类型会抛出NotImplementedError
        try:
            wecom_msg = WecomBotMessage(body, is_group=is_group)
        except NotImplementedError as e:
            logger.warning(f"[WecomBot] {e}")
            return
        except Exception as e:
            logger.error(f"[WecomBot] Failed to parse message: {e}", exc_info=True)
            return

        # 保存req_id，用于后续回复时关联到原始请求
        wecom_msg.req_id = req_id

        # 文件缓存逻辑：图片和文件先缓存，等后续文本消息到来时一并处理
        # File cache logic (same pattern as feishu)
        from channel.file_cache import get_file_cache
        file_cache = get_file_cache()

        # 根据会话类型确定session_id：
        # - 群聊+共享会话：使用chatid作为session_id
        # - 群聊+非共享会话：使用userid_chatid组合
        # - 单聊：使用userid
        if is_group:
            if conf().get("group_shared_session", True):
                session_id = body.get("chatid", "")
            else:
                session_id = wecom_msg.from_user_id + "_" + body.get("chatid", "")
        else:
            session_id = wecom_msg.from_user_id

        # 图片消息：下载后缓存路径，等待后续文本消息引用
        if wecom_msg.ctype == ContextType.IMAGE:
            if hasattr(wecom_msg, "image_path") and wecom_msg.image_path:
                file_cache.add(session_id, wecom_msg.image_path, file_type="image")
                logger.info(f"[WecomBot] Image cached for session {session_id}")
            return

        # 文件消息：先执行下载（prepare），然后缓存路径
        if wecom_msg.ctype == ContextType.FILE:
            wecom_msg.prepare()
            file_cache.add(session_id, wecom_msg.content, file_type="file")
            logger.info(f"[WecomBot] File cached for session {session_id}: {wecom_msg.content}")
            return

        # 文本消息：检查是否有缓存的文件，如果有则附加文件引用
        if wecom_msg.ctype == ContextType.TEXT:
            cached_files = file_cache.get(session_id)
            if cached_files:
                file_refs = []
                for fi in cached_files:
                    ftype = fi["type"]
                    fpath = fi["path"]
                    if ftype == "image":
                        file_refs.append(f"[图片: {fpath}]")
                    elif ftype == "video":
                        file_refs.append(f"[视频: {fpath}]")
                    else:
                        file_refs.append(f"[文件: {fpath}]")
                # 将文件引用追加到文本内容后面
                wecom_msg.content = wecom_msg.content + "\n" + "\n".join(file_refs)
                logger.info(f"[WecomBot] Attached {len(cached_files)} cached file(s)")
                # 清除已使用的缓存
                file_cache.clear(session_id)

        # 组装上下文并投递到消息处理队列
        context = self._compose_context(
            wecom_msg.ctype,
            wecom_msg.content,
            isgroup=is_group,
            msg=wecom_msg,
            no_need_at=True,
        )
        if context:
            # 如果有req_id，设置流式回调函数，用于Agent模式下的中间过程推送
            if req_id:
                context["on_event"] = self._make_stream_callback(req_id)
            self.produce(context)

    # ------------------------------------------------------------------
    # Event callback
    # 事件回调处理
    # ------------------------------------------------------------------

    def _handle_event_callback(self, data: dict):
        """处理事件回调。

        支持的事件类型：
        - enter_chat: 用户进入会话
        - disconnected_event: 另一个连接占用了当前机器人（被踢出）

        Args:
            data: 事件回调的原始数据字典
        """
        body = data.get("body", {})
        event = body.get("event", {})
        event_type = event.get("eventtype", "")

        if event_type == "enter_chat":
            # 用户进入会话事件，仅记录日志
            logger.info(f"[WecomBot] User entered chat: {body.get('from', {}).get('userid')}")
        elif event_type == "disconnected_event":
            # 收到断连事件，说明另一个WebSocket连接占用了当前机器人
            # 可能是另一个实例使用了相同的bot_id和secret
            logger.warning("[WecomBot] Received disconnected_event, another connection took over")
        else:
            logger.debug(f"[WecomBot] Event: {event_type}")

    # ------------------------------------------------------------------
    # Stream callback (for agent on_event)
    # 流式回调：用于Agent模式的中间过程推送
    # ------------------------------------------------------------------

    def _make_stream_callback(self, req_id: str):
        """构建流式回调函数，用于将Agent执行过程中的中间结果推送到企业微信。

        在Agent模式下，模型可能会进行多轮"思考-调用工具-观察"循环，
        此回调将每一轮的思考内容和最终答案通过流式消息推送到前端，
        使用户可以看到Agent的推理过程。

        流式消息机制：
        - 每个请求有独立的stream_id，前端通过此ID关联流消息
        - committed: 已完成的前几轮内容（用"---"分隔）
        - current: 当前轮次正在流式输出的内容
        - 当遇到工具调用时，将当前内容提交到committed并开始新一轮
        - 当没有工具调用时（最终回答），直接将内容追加到committed

        Args:
            req_id: 原始消息的请求ID，用于关联回复

        Returns:
            on_event 回调函数，供AgentStreamExecutor调用
        """
        # 为每个请求生成独立的stream_id
        stream_id = uuid.uuid4().hex[:16]
        self._stream_states[req_id] = {
            "stream_id": stream_id,
            "committed": "",  # 已完成的内容（之前的轮次）
            "current": "",    # 当前轮次正在流式输出的内容
        }

        def _push_stream(state: dict):
            """推送当前流式内容到企业微信。

            将committed和current拼接后作为流式消息发送，
            finish=False 表示流尚未结束，前端应持续显示。
            """
            self._ws_send({
                "cmd": "aibot_respond_msg",
                "headers": {"req_id": req_id},
                "body": {
                    "msgtype": "stream",
                    "stream": {
                        "id": state["stream_id"],
                        "finish": False,
                        "content": state["committed"] + state["current"],
                    },
                },
            })

        def on_event(event: dict):
            """Agent事件回调函数。

            处理以下事件类型：
            - turn_start: 新一轮开始，清空current
            - message_update: 收到增量文本，追加到current并推送
            - message_end: 一轮结束，检查是否有工具调用
              - 有工具调用：将current提交到committed，用"---"分隔
              - 无工具调用：直接将current追加到committed

            Args:
                event: Agent事件字典，包含type和data字段
            """
            event_type = event.get("type")
            data = event.get("data", {})
            state = self._stream_states.get(req_id)
            if not state:
                return

            if event_type == "turn_start":
                # 新一轮开始，清空当前轮次的内容
                state["current"] = ""

            elif event_type == "message_update":
                # 收到增量文本，追加并推送
                delta = data.get("delta", "")
                if delta:
                    state["current"] += delta
                    _push_stream(state)

            elif event_type == "message_end":
                # 一轮结束，根据是否有工具调用决定如何处理当前内容
                tool_calls = data.get("tool_calls", [])
                if tool_calls:
                    # 有工具调用，说明这一轮是"思考"阶段，
                    # 将内容提交到committed，用分隔符标记不同轮次
                    if state["current"].strip():
                        state["committed"] += state["current"].strip() + "\n\n---\n\n"
                        state["current"] = ""
                else:
                    # 没有工具调用，说明这是最终回答，
                    # 直接追加到committed，不再需要分隔
                    state["committed"] += state["current"]
                    state["current"] = ""

        return on_event

    # ------------------------------------------------------------------
    # _compose_context (same pattern as feishu)
    # 上下文组装：将消息封装为统一的Context对象
    # ------------------------------------------------------------------

    def _compose_context(self, ctype: ContextType, content, **kwargs):
        """组装消息上下文Context对象。

        将不同来源的消息统一封装为Context对象，包含：
        - 消息类型和内容
        - 会话ID（用于区分不同的对话）
        - 接收者ID
        - 是否为图片创作请求

        与飞书通道使用相同的模式。

        Args:
            ctype: 消息类型（TEXT, IMAGE, FILE等）
            content: 消息内容
            **kwargs: 额外参数，包括isgroup, msg, no_need_at等

        Returns:
            组装完成的Context对象
        """
        context = Context(ctype, content)
        context.kwargs = kwargs
        # 设置通道类型
        if "channel_type" not in context:
            context["channel_type"] = self.channel_type
        # 记录原始消息类型，后续可能会被修改（如TEXT -> IMAGE_CREATE）
        if "origin_ctype" not in context:
            context["origin_ctype"] = ctype

        cmsg = context["msg"]

        # 确定会话ID：群聊共享会话用chatid，非共享用userid:chatid，单聊用userid
        if cmsg.is_group:
            if conf().get("group_shared_session", True):
                context["session_id"] = cmsg.other_user_id
            else:
                context["session_id"] = f"{cmsg.from_user_id}:{cmsg.other_user_id}"
        else:
            context["session_id"] = cmsg.from_user_id

        # 设置消息接收者
        context["receiver"] = cmsg.other_user_id

        # 处理文本消息：检查是否为图片创作请求
        if ctype == ContextType.TEXT:
            # 检查是否匹配图片创作前缀（如"画"）
            img_match_prefix = check_prefix(content, conf().get("image_create_prefix"))
            if img_match_prefix:
                # 去掉前缀，将类型改为IMAGE_CREATE
                content = content.replace(img_match_prefix, "", 1)
                context.type = ContextType.IMAGE_CREATE
            else:
                context.type = ContextType.TEXT
            context.content = content.strip()

        return context

    # ------------------------------------------------------------------
    # Send reply
    # 发送回复：根据回复类型选择不同的发送方式
    # ------------------------------------------------------------------

    def send(self, reply: Reply, context: Context):
        """发送回复消息。

        根据回复类型分发到不同的发送方法：
        - TEXT: 发送文本/Markdown消息
        - IMAGE/IMAGE_URL: 发送图片
        - FILE: 发送文件（如果附带文字内容，先发文字再发文件）
        - VIDEO/VIDEO_URL: 发送视频
        - 其他类型: 降级为文本发送

        Args:
            reply: 回复对象，包含类型和内容
            context: 上下文对象，包含接收者和消息来源信息
        """
        msg = context.get("msg")
        is_group = context.get("isgroup", False)
        receiver = context.get("receiver", "")

        # 获取原始消息的req_id，用于回复时关联到原始请求
        # 如果没有req_id，说明是定时任务主动推送，使用aibot_send_msg命令
        # Determine req_id for responding or use send_msg for scheduled push
        req_id = getattr(msg, "req_id", None) if msg else None

        if reply.type == ReplyType.TEXT:
            self._send_text(reply.content, receiver, is_group, req_id)
        elif reply.type in (ReplyType.IMAGE_URL, ReplyType.IMAGE):
            self._send_image(reply.content, receiver, is_group, req_id)
        elif reply.type == ReplyType.FILE:
            # 文件回复：如果同时有文字内容，先发送文字再发送文件
            if hasattr(reply, "text_content") and reply.text_content:
                self._send_text(reply.text_content, receiver, is_group, req_id)
                # 短暂延迟，避免消息顺序混乱
                time.sleep(0.3)
            self._send_file(reply.content, receiver, is_group, req_id)
        elif reply.type == ReplyType.VIDEO or reply.type == ReplyType.VIDEO_URL:
            self._send_file(reply.content, receiver, is_group, req_id, media_type="video")
        else:
            # 不支持的回复类型，降级为文本发送
            logger.warning(f"[WecomBot] Unsupported reply type: {reply.type}, falling back to text")
            self._send_text(str(reply.content), receiver, is_group, req_id)

    # ------------------------------------------------------------------
    # Respond message (via websocket)
    # 回复消息（通过WebSocket发送）
    # ------------------------------------------------------------------

    def _send_text(self, content: str, receiver: str, is_group: bool, req_id: str = None):
        """发送文本/Markdown回复。

        两种发送模式：
        1. 有req_id时：使用流式消息（stream）发送，复用已有的流状态
        2. 无req_id时：使用主动发送（aibot_send_msg），以Markdown格式推送

        流式消息模式下，如果存在之前Agent推送的流状态，
        会将最终内容与之前的流内容合并，确保用户看到完整回复。

        Args:
            content: 要发送的文本内容
            receiver: 接收者ID（群聊为chatid，单聊为userid）
            is_group: 是否为群聊
            req_id: 原始消息的请求ID，为None时使用主动发送模式
        """
        if req_id:
            # 流式消息模式：复用或创建流状态
            state = self._stream_states.pop(req_id, None)
            if state:
                # 存在之前的流状态，使用committed内容作为最终内容
                # 这确保Agent中间过程的思考内容也包含在最终回复中
                final_content = state["committed"]
                stream_id = state["stream_id"]
            else:
                # 没有流状态，创建新的流ID
                final_content = content
                stream_id = uuid.uuid4().hex[:16]
            # 发送流式消息，finish=True表示流结束
            self._ws_send({
                "cmd": "aibot_respond_msg",
                "headers": {"req_id": req_id},
                "body": {
                    "msgtype": "stream",
                    "stream": {
                        "id": stream_id,
                        "finish": True,
                        "content": final_content,
                    },
                },
            })
        else:
            # 主动发送模式：用于定时任务等没有原始消息的场景
            self._active_send_markdown(content, receiver, is_group)

    def _send_image(self, img_path_or_url: str, receiver: str, is_group: bool, req_id: str = None):
        """发送图片回复。

        处理流程：
        1. 下载网络图片到本地（如果是URL）
        2. 检查并转换图片格式为JPG/PNG（企业微信仅支持这两种格式）
        3. 压缩超大图片（超过2MB时逐步降低质量）
        4. 上传图片到企业微信获取media_id
        5. 通过WebSocket发送图片消息

        Args:
            img_path_or_url: 图片的本地路径或URL
            receiver: 接收者ID
            is_group: 是否为群聊
            req_id: 原始消息的请求ID，为None时使用主动发送模式
        """
        local_path = img_path_or_url
        # 处理file://协议的路径
        if local_path.startswith("file://"):
            local_path = local_path[7:]

        # 如果是网络URL，先下载到本地临时文件
        if local_path.startswith(("http://", "https://")):
            try:
                resp = requests.get(local_path, timeout=30)
                resp.raise_for_status()
                # 根据Content-Type确定文件扩展名
                ct = resp.headers.get("Content-Type", "")
                if "jpeg" in ct or "jpg" in ct:
                    ext = ".jpg"
                elif "webp" in ct:
                    ext = ".webp"
                elif "gif" in ct:
                    ext = ".gif"
                else:
                    ext = ".png"
                tmp_path = f"/tmp/wecom_img_{uuid.uuid4().hex[:8]}{ext}"
                with open(tmp_path, "wb") as f:
                    f.write(resp.content)
                logger.info(f"[WecomBot] Image downloaded: size={len(resp.content)}, "
                            f"content-type={ct}, path={tmp_path}")
                local_path = tmp_path
            except Exception as e:
                logger.error(f"[WecomBot] Failed to download image for sending: {e}")
                self._send_text("[Image send failed]", receiver, is_group, req_id)
                return

        # 检查本地文件是否存在
        if not os.path.exists(local_path):
            logger.error(f"[WecomBot] Image file not found: {local_path}")
            return

        # 企业微信图片上传大小限制为2MB
        max_image_size = 2 * 1024 * 1024  # 2MB limit for image upload
        # 确保图片格式为JPG或PNG
        local_path = self._ensure_image_format(local_path)
        if not local_path:
            self._send_text("[Image format conversion failed]", receiver, is_group, req_id)
            return

        # 如果图片超过大小限制，进行压缩
        if os.path.getsize(local_path) > max_image_size:
            local_path = self._compress_image(local_path, max_image_size)
            if not local_path:
                self._send_text("[Image too large]", receiver, is_group, req_id)
                return

        # 上传图片到企业微信，获取media_id
        file_size = os.path.getsize(local_path)
        logger.info(f"[WecomBot] Uploading image: path={local_path}, size={file_size} bytes")
        media_id = self._upload_media(local_path, "image")
        if not media_id:
            logger.error("[WecomBot] Failed to upload image")
            self._send_text("[Image upload failed]", receiver, is_group, req_id)
            return

        # 根据是否有req_id选择不同的发送命令
        if req_id:
            # 回复消息模式
            self._ws_send({
                "cmd": "aibot_respond_msg",
                "headers": {"req_id": req_id},
                "body": {
                    "msgtype": "image",
                    "image": {"media_id": media_id},
                },
            })
        else:
            # 主动推送模式
            self._ws_send({
                "cmd": "aibot_send_msg",
                "headers": {"req_id": self._gen_req_id()},
                "body": {
                    "chatid": receiver,
                    "chat_type": 2 if is_group else 1,  # 2=群聊, 1=单聊
                    "msgtype": "image",
                    "image": {"media_id": media_id},
                },
            })

    @staticmethod
    def _ensure_image_format(file_path: str) -> str:
        """确保图片格式为JPG或PNG（企业微信仅支持这两种格式）。

        处理逻辑：
        1. 如果已经是JPG/PNG但文件扩展名不匹配，重命名文件
        2. 如果是不支持的格式（WebP, GIF, BMP等），转换为PNG或JPG：
           - RGBA模式（带透明度）转换为PNG
           - 其他模式转换为JPG

        Args:
            file_path: 原始图片文件路径

        Returns:
            转换后的图片文件路径，失败时返回空字符串
        """
        try:
            from PIL import Image
            img = Image.open(file_path)
            fmt = (img.format or "").upper()
            if fmt in ("JPEG", "PNG"):
                # Already a supported format, but make sure the filename extension matches
                # 格式已支持，但需要确保文件扩展名匹配，否则企业微信可能无法正确识别
                ext = os.path.splitext(file_path)[1].lower()
                if fmt == "JPEG" and ext in (".jpg", ".jpeg"):
                    return file_path
                if fmt == "PNG" and ext == ".png":
                    return file_path
                # Extension doesn't match — rename/copy with correct extension
                # 扩展名不匹配——用正确的扩展名复制文件
                correct_ext = ".jpg" if fmt == "JPEG" else ".png"
                out_path = f"/tmp/wecom_fmt_{uuid.uuid4().hex[:8]}{correct_ext}"
                img.save(out_path, fmt)
                logger.info(f"[WecomBot] Image renamed: {file_path} -> {out_path} ({fmt})")
                return out_path

            # Unsupported format (WebP, GIF, BMP, etc.) — convert to PNG
            # 不支持的格式（WebP, GIF, BMP等）——转换为PNG或JPG
            if img.mode == "RGBA":
                # 带透明通道的图片转为PNG保留透明度
                out_path = f"/tmp/wecom_fmt_{uuid.uuid4().hex[:8]}.png"
                img.save(out_path, "PNG")
            else:
                # 不带透明通道的图片转为JPG，质量90%
                out_path = f"/tmp/wecom_fmt_{uuid.uuid4().hex[:8]}.jpg"
                img.convert("RGB").save(out_path, "JPEG", quality=90)
            logger.info(f"[WecomBot] Image converted from {fmt} -> {out_path}")
            return out_path
        except Exception as e:
            logger.error(f"[WecomBot] Image format check failed: {e}")
            return file_path

    @staticmethod
    def _compress_image(file_path: str, max_bytes: int) -> str:
        """压缩图片以适应大小限制。

        采用两阶段压缩策略：
        1. 降低JPEG质量（从85逐步降至30）
        2. 如果降低质量仍不够，按比例缩小图片尺寸

        Args:
            file_path: 原始图片文件路径
            max_bytes: 最大允许字节数

        Returns:
            压缩后的图片文件路径，失败时返回空字符串
        """
        try:
            from PIL import Image
            img = Image.open(file_path)
            # RGBA模式需先转换为RGB才能保存为JPEG
            if img.mode == "RGBA":
                img = img.convert("RGB")

            out_path = f"/tmp/wecom_compressed_{uuid.uuid4().hex[:8]}.jpg"
            # 第一阶段：逐步降低JPEG质量
            quality = 85
            while quality >= 30:
                img.save(out_path, "JPEG", quality=quality, optimize=True)
                if os.path.getsize(out_path) <= max_bytes:
                    logger.info(f"[WecomBot] Image compressed: quality={quality}, "
                                f"size={os.path.getsize(out_path)} bytes")
                    return out_path
                quality -= 10

            # Still too large — resize
            # 第二阶段：降低质量仍不够，按比例缩小图片
            # 根据目标大小与当前大小的比值计算缩放比例
            ratio = (max_bytes / os.path.getsize(out_path)) ** 0.5
            new_size = (int(img.width * ratio), int(img.height * ratio))
            img = img.resize(new_size, Image.LANCZOS)
            img.save(out_path, "JPEG", quality=70, optimize=True)
            if os.path.getsize(out_path) <= max_bytes:
                logger.info(f"[WecomBot] Image compressed with resize: {new_size}, "
                            f"size={os.path.getsize(out_path)} bytes")
                return out_path

            logger.error(f"[WecomBot] Cannot compress image below {max_bytes} bytes")
            return ""
        except Exception as e:
            logger.error(f"[WecomBot] Image compression failed: {e}")
            return ""

    def _send_file(self, file_path: str, receiver: str, is_group: bool,
                   req_id: str = None, media_type: str = "file"):
        """发送文件/视频回复。

        处理流程：
        1. 下载网络文件到本地（如果是URL）
        2. 上传文件到企业微信获取media_id
        3. 通过WebSocket发送文件/视频消息

        Args:
            file_path: 文件的本地路径或URL
            receiver: 接收者ID
            is_group: 是否为群聊
            req_id: 原始消息的请求ID，为None时使用主动发送模式
            media_type: 媒体类型，"file"或"video"
        """
        local_path = file_path
        # 处理file://协议的路径
        if local_path.startswith("file://"):
            local_path = local_path[7:]

        # 如果是网络URL，先下载到本地临时文件
        if local_path.startswith(("http://", "https://")):
            try:
                resp = requests.get(local_path, timeout=60)
                resp.raise_for_status()
                ext = os.path.splitext(local_path)[1] or ".bin"
                tmp_path = f"/tmp/wecom_file_{uuid.uuid4().hex[:8]}{ext}"
                with open(tmp_path, "wb") as f:
                    f.write(resp.content)
                local_path = tmp_path
            except Exception as e:
                logger.error(f"[WecomBot] Failed to download file for sending: {e}")
                return

        # 检查本地文件是否存在
        if not os.path.exists(local_path):
            logger.error(f"[WecomBot] File not found: {local_path}")
            return

        # 上传文件到企业微信，获取media_id
        media_id = self._upload_media(local_path, media_type)
        if not media_id:
            logger.error(f"[WecomBot] Failed to upload {media_type}")
            return

        # 根据是否有req_id选择不同的发送命令
        if req_id:
            # 回复消息模式
            self._ws_send({
                "cmd": "aibot_respond_msg",
                "headers": {"req_id": req_id},
                "body": {
                    "msgtype": media_type,
                    media_type: {"media_id": media_id},
                },
            })
        else:
            # 主动推送模式
            self._ws_send({
                "cmd": "aibot_send_msg",
                "headers": {"req_id": self._gen_req_id()},
                "body": {
                    "chatid": receiver,
                    "chat_type": 2 if is_group else 1,  # 2=群聊, 1=单聊
                    "msgtype": media_type,
                    media_type: {"media_id": media_id},
                },
            })

    def _active_send_markdown(self, content: str, receiver: str, is_group: bool):
        """主动发送Markdown消息。

        用于定时任务等没有原始消息的场景，
        通过aibot_send_msg命令主动推送消息给指定用户/群。

        Args:
            content: Markdown格式的消息内容
            receiver: 接收者ID
            is_group: 是否为群聊
        """
        self._ws_send({
            "cmd": "aibot_send_msg",
            "headers": {"req_id": self._gen_req_id()},
            "body": {
                "chatid": receiver,
                "chat_type": 2 if is_group else 1,  # 2=群聊, 1=单聊
                "msgtype": "markdown",
                "markdown": {"content": content},
            },
        })

    # ------------------------------------------------------------------
    # Media upload (chunked)
    # 媒体文件上传（分块上传协议）
    # ------------------------------------------------------------------

    def _upload_media(self, file_path: str, media_type: str = "file") -> str:
        """通过分块上传协议将本地文件上传到企业微信。

        上传流程分三步：
        1. 初始化上传（aibot_upload_media_init）：
           告知服务端文件名、大小、分块数和MD5，获取upload_id
        2. 逐块上传（aibot_upload_media_chunk）：
           将文件按512KB分块，base64编码后逐块上传
        3. 完成上传（aibot_upload_media_finish）：
           通知服务端所有分块已上传完毕，获取media_id

        限制：
        - 单块大小不超过512KB（base64编码前）
        - 总分块数不超过100（即文件最大约50MB）
        - 文件最小5字节（空文件不允许上传）

        Args:
            file_path: 本地文件路径
            media_type: 媒体类型（"image", "file", "video"等）

        Returns:
            上传成功返回media_id，失败返回空字符串
        """
        if not os.path.exists(file_path):
            logger.error(f"[WecomBot] Upload file not found: {file_path}")
            return ""

        file_size = os.path.getsize(file_path)
        # 文件太小，可能是空文件或损坏的文件
        if file_size < 5:
            logger.error(f"[WecomBot] File too small: {file_size} bytes")
            return ""

        filename = os.path.basename(file_path)
        # 计算分块数量，每块最大512KB
        total_chunks = math.ceil(file_size / MEDIA_CHUNK_SIZE)
        # 企业微信限制分块数不超过100
        if total_chunks > 100:
            logger.error(f"[WecomBot] Too many chunks: {total_chunks} > 100")
            return ""

        # 计算整个文件的MD5值，用于上传完成后的完整性校验
        file_md5 = hashlib.md5()
        with open(file_path, "rb") as f:
            for block in iter(lambda: f.read(8192), b""):
                file_md5.update(block)
        md5_hex = file_md5.hexdigest()

        # 步骤1：初始化上传，获取upload_id
        # 1. Init upload
        init_resp = self._send_and_wait({
            "cmd": "aibot_upload_media_init",
            "headers": {"req_id": self._gen_req_id()},
            "body": {
                "type": media_type,
                "filename": filename,
                "total_size": file_size,
                "total_chunks": total_chunks,
                "md5": md5_hex,
            },
        }, timeout=15)

        if init_resp.get("errcode") != 0:
            logger.error(f"[WecomBot] Upload init failed: {init_resp}")
            return ""

        upload_id = init_resp.get("body", {}).get("upload_id")
        if not upload_id:
            logger.error("[WecomBot] Failed to get upload_id")
            return ""

        # 步骤2：逐块上传文件数据
        # 2. Upload chunks
        with open(file_path, "rb") as f:
            for idx in range(total_chunks):
                # 读取一块数据
                chunk = f.read(MEDIA_CHUNK_SIZE)
                # base64编码后传输（企业微信要求）
                b64_data = base64.b64encode(chunk).decode("utf-8")
                chunk_resp = self._send_and_wait({
                    "cmd": "aibot_upload_media_chunk",
                    "headers": {"req_id": self._gen_req_id()},
                    "body": {
                        "upload_id": upload_id,
                        "chunk_index": idx,
                        "base64_data": b64_data,
                    },
                }, timeout=30)
                # 任一分块上传失败则终止整个上传
                if chunk_resp.get("errcode") != 0:
                    logger.error(f"[WecomBot] Chunk {idx} upload failed: {chunk_resp}")
                    return ""

        # 步骤3：通知服务端上传完成，获取media_id
        # 3. Finish upload
        finish_resp = self._send_and_wait({
            "cmd": "aibot_upload_media_finish",
            "headers": {"req_id": self._gen_req_id()},
            "body": {"upload_id": upload_id},
        }, timeout=30)

        if finish_resp.get("errcode") != 0:
            logger.error(f"[WecomBot] Upload finish failed: {finish_resp}")
            return ""

        media_id = finish_resp.get("body", {}).get("media_id", "")
        if media_id:
            logger.info(f"[WecomBot] Media uploaded: media_id={media_id}")
        else:
            logger.error("[WecomBot] Failed to get media_id from finish response")
        return media_id
