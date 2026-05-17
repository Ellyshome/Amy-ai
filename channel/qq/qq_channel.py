"""
QQ Bot channel via WebSocket long connection.

Supports:
- Group chat (@bot), single chat (C2C), guild channel, guild DM
- Text / image / file message send & receive
- Heartbeat keep-alive and auto-reconnect with session resume

QQ机器人通道，通过WebSocket长连接接收消息。

支持的消息类型和场景：
- 群聊（@机器人触发）、单聊（C2C）、频道消息、频道私信
- 文本/图片/文件消息的收发
- 心跳保活和断线自动重连（支持会话恢复）

为什么使用WebSocket而非HTTP回调：
QQ官方的WebSocket模式不需要公网IP和服务器，客户端主动建立长连接，
适合本地开发和内网部署。同时也避免了HTTP回调的URL配置和验证问题。
"""

import base64
import json
import os
import threading
import time

import requests
import websocket

from bridge.context import Context, ContextType
from bridge.reply import Reply, ReplyType
from channel.chat_channel import ChatChannel, check_prefix
from channel.qq.qq_message import QQMessage
from common.expired_dict import ExpiredDict
from common.log import logger
from common.singleton import singleton
from common.ws_client_compat import websocket_app_run_forever
from config import conf

# Rich media file_type constants
# QQ富媒体文件类型常量，用于上传API的file_type参数
QQ_FILE_TYPE_IMAGE = 1   # 图片类型
QQ_FILE_TYPE_VIDEO = 2   # 视频类型
QQ_FILE_TYPE_VOICE = 3   # 语音类型
QQ_FILE_TYPE_FILE = 4    # 文件类型

# QQ Bot API基础URL
QQ_API_BASE = "https://api.sgroup.qq.com"

# Intents: GROUP_AND_C2C_EVENT(1<<25) | PUBLIC_GUILD_MESSAGES(1<<30)
# 意图值：群聊和C2C事件(1<<25) 与 频道消息(1<<30) 的组合
# 这些intent告诉QQ服务器我们需要接收哪些类型的事件
DEFAULT_INTENTS = (1 << 25) | (1 << 30)

# OpCode constants —— WebSocket协议操作码常量
# QQ WebSocket使用这些操作码进行状态通信
OP_DISPATCH = 0          # 事件分发：服务器推送的各类事件
OP_HEARTBEAT = 1         # 心跳：客户端定期发送以保持连接
OP_IDENTIFY = 2          # 鉴权：客户端发送身份信息建立会话
OP_RESUME = 6            # 恢复：客户端请求恢复之前的会话
OP_RECONNECT = 7         # 重连：服务器要求客户端重新连接
OP_INVALID_SESSION = 9   # 无效会话：会话已失效，需要重新鉴权
OP_HELLO = 10            # 握手：连接建立后服务器首先发送的消息
OP_HEARTBEAT_ACK = 11    # 心跳确认：服务器确认收到心跳

# Resumable error codes
# 可恢复的关闭码：这些错误码下可以尝试会话恢复（Resume），
# 而不需要完全重新鉴权（Identify）
RESUMABLE_CLOSE_CODES = {4008, 4009}


@singleton
class QQChannel(ChatChannel):
    """
    QQ机器人通道主类，继承自ChatChannel。

    该类通过WebSocket长连接与QQ服务器通信，实现了完整的机器人协议：
    1. 连接管理：建立WebSocket连接、心跳保活、断线重连
    2. 会话管理：鉴权(Identify)和会话恢复(Resume)
    3. 消息收发：支持群聊、单聊、频道、私信四种场景
    4. 富媒体处理：图片、视频、文件的上传和发送

    WebSocket协议流程：
    1. 连接Gateway获取WebSocket URL
    2. 建立WebSocket连接
    3. 收到Hello消息后发送Identify或Resume
    4. 收到Ready消息表示连接成功
    5. 定期发送Heartbeat保持连接
    6. 收到Dispatch消息时处理业务逻辑

    使用单例模式确保全局只有一个通道实例，避免重复连接和状态冲突。
    """

    def __init__(self):
        """
        初始化QQ通道。

        主要初始化以下资源：
        1. 凭证信息：app_id和app_secret
        2. Token管理：access_token缓存和自动刷新
        3. WebSocket连接：客户端实例和工作线程
        4. 会话状态：session_id和last_seq用于会话恢复
        5. 消息去重：使用ExpiredDict防止重复处理
        6. 消息序列号：msg_seq_counter用于消息排序
        """
        super().__init__()
        self.app_id = ""
        self.app_secret = ""

        # Token缓存和过期时间
        self._access_token = ""
        self._token_expires_at = 0

        # WebSocket相关资源
        self._ws = None               # WebSocket客户端实例
        self._ws_thread = None         # WebSocket工作线程
        self._heartbeat_thread = None  # 心跳线程
        self._connected = False        # 连接状态标志
        self._stop_event = threading.Event()  # 停止事件，用于通知各线程退出
        self._token_lock = threading.Lock()   # Token刷新锁，防止并发刷新

        # 会话恢复相关状态
        self._session_id = None       # WebSocket会话ID，Resume时需要
        self._last_seq = None         # 最后收到的消息序列号，心跳和Resume时需要
        self._heartbeat_interval = 45000  # 心跳间隔（毫秒），默认45秒
        self._can_resume = False      # 是否可以尝试会话恢复

        # 消息去重字典，过期时间约7.1小时
        self.received_msgs = ExpiredDict(60 * 60 * 7.1)
        # 消息序列号计数器，用于QQ API的msg_seq参数
        # QQ API要求同一消息下的多条回复使用递增的msg_seq
        self._msg_seq_counter = {}

        # 配置群聊和单聊策略
        conf()["group_name_white_list"] = ["ALL_GROUP"]  # 所有群都处理
        conf()["single_chat_prefix"] = [""]              # 单聊无需前缀

    # ------------------------------------------------------------------
    # Lifecycle —— 生命周期管理
    # ------------------------------------------------------------------

    def startup(self):
        """
        启动QQ通道。

        启动流程：
        1. 从配置读取app_id和app_secret
        2. 获取初始access_token
        3. 建立WebSocket连接

        如果凭证缺失或获取token失败，会报告启动错误并返回。
        """
        self.app_id = conf().get("qq_app_id", "")
        self.app_secret = conf().get("qq_app_secret", "")

        if not self.app_id or not self.app_secret:
            err = "[QQ] qq_app_id and qq_app_secret are required"
            logger.error(err)
            self.report_startup_error(err)
            return

        # 刷新access_token，启动时必须成功获取
        self._refresh_access_token()
        if not self._access_token:
            err = "[QQ] Failed to get initial access_token"
            logger.error(err)
            self.report_startup_error(err)
            return

        self._stop_event.clear()
        self._start_ws()  # 建立WebSocket连接（阻塞方法）

    def stop(self):
        """
        停止QQ通道。

        停止流程：
        1. 设置停止事件，通知所有工作线程退出
        2. 关闭WebSocket连接
        3. 重置连接状态
        """
        logger.info("[QQ] stop() called")
        self._stop_event.set()  # 通知心跳线程和重连逻辑退出
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass
        self._ws = None
        self._connected = False

    # ------------------------------------------------------------------
    # Access Token —— 访问令牌管理
    # ------------------------------------------------------------------

    def _refresh_access_token(self):
        """
        刷新QQ Bot的access_token。

        调用QQ的getAppAccessToken接口获取新的access_token。
        token有效期通常为2小时，提前60秒刷新以避免边界问题。

        API文档: https://bot.q.qq.com/wiki/develop/api/#token
        """
        try:
            resp = requests.post(
                "https://bots.qq.com/app/getAppAccessToken",
                json={"appId": self.app_id, "clientSecret": self.app_secret},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            self._access_token = data.get("access_token", "")
            expires_in = int(data.get("expires_in", 7200))
            # 提前60秒刷新，避免token在使用时恰好过期
            self._token_expires_at = time.time() + expires_in - 60
            logger.debug(f"[QQ] Access token refreshed, expires_in={expires_in}s")
        except Exception as e:
            logger.error(f"[QQ] Failed to refresh access_token: {e}")

    def _get_access_token(self) -> str:
        """
        获取有效的access_token，如果过期则自动刷新。

        使用线程锁保护token刷新操作，防止多个线程同时触发刷新。

        Returns:
            str: 有效的access_token
        """
        with self._token_lock:
            if time.time() >= self._token_expires_at:
                self._refresh_access_token()
            return self._access_token

    def _get_auth_headers(self) -> dict:
        """
        构建QQ API的鉴权请求头。

        QQ Bot API使用"QQBot {token}"格式的Authorization头进行鉴权。

        Returns:
            dict: 包含Authorization和Content-Type的请求头字典
        """
        return {
            "Authorization": f"QQBot {self._get_access_token()}",
            "Content-Type": "application/json",
        }

    # ------------------------------------------------------------------
    # WebSocket connection —— WebSocket连接管理
    # ------------------------------------------------------------------

    def _get_ws_url(self) -> str:
        """
        获取QQ Gateway的WebSocket URL。

        每次连接前都需要通过API获取最新的Gateway URL，
        因为URL可能随服务器负载变化而改变。

        API文档: https://bot.q.qq.com/wiki/develop/api/gateway

        Returns:
            str: WebSocket连接URL，失败返回空字符串
        """
        try:
            resp = requests.get(
                f"{QQ_API_BASE}/gateway",
                headers=self._get_auth_headers(),
                timeout=10,
            )
            resp.raise_for_status()
            url = resp.json().get("url", "")
            logger.debug(f"[QQ] Gateway URL: {url}")
            return url
        except Exception as e:
            logger.error(f"[QQ] Failed to get gateway URL: {e}")
            return ""

    def _start_ws(self):
        """
        建立WebSocket连接并启动消息接收循环。

        主要流程：
        1. 获取Gateway URL
        2. 创建WebSocketApp并注册回调
        3. 在独立线程中运行WebSocket客户端
        4. 阻塞等待线程结束

        回调说明：
        - _on_open: 连接建立成功
        - _on_message: 收到消息
        - _on_error: 连接错误
        - _on_close: 连接关闭，触发重连逻辑

        重连策略：
        - 可恢复的关闭码(4008, 4009)：3秒后尝试Resume
        - 其他关闭码：5秒后重新Identify
        """
        ws_url = self._get_ws_url()
        if not ws_url:
            logger.error("[QQ] Cannot start WebSocket without gateway URL")
            self.report_startup_error("Failed to get gateway URL")
            return

        def _on_open(ws):
            """WebSocket连接建立回调，等待服务器发送Hello消息"""
            logger.debug("[QQ] WebSocket connected, waiting for Hello...")

        def _on_message(ws, raw):
            """
            WebSocket消息接收回调。

            将原始JSON字符串解析为字典，然后交给_handle_ws_message处理。

            Args:
                ws: WebSocket实例
                raw: 原始消息字符串（JSON格式）
            """
            try:
                data = json.loads(raw)
                self._handle_ws_message(data)
            except Exception as e:
                logger.error(f"[QQ] Failed to handle ws message: {e}", exc_info=True)

        def _on_error(ws, error):
            """WebSocket错误回调"""
            logger.error(f"[QQ] WebSocket error: {error}")

        def _on_close(ws, close_status_code, close_msg):
            """
            WebSocket关闭回调，触发重连逻辑。

            根据关闭码判断是否可以恢复会话：
            - 4008/4009：可以Resume，3秒后重试
            - 其他：需要重新Identify，5秒后重试

            只有在非主动停止的情况下才会触发重连。
            """
            logger.warning(f"[QQ] WebSocket closed: status={close_status_code}, msg={close_msg}")
            self._connected = False
            if not self._stop_event.is_set():
                if close_status_code in RESUMABLE_CLOSE_CODES and self._session_id:
                    # 可恢复的断开：使用Resume恢复会话，减少消息丢失
                    self._can_resume = True
                    logger.info("[QQ] Will attempt resume in 3s...")
                    time.sleep(3)
                else:
                    # 不可恢复的断开：需要重新Identify
                    self._can_resume = False
                    logger.info("[QQ] Will reconnect in 5s...")
                    time.sleep(5)
                if not self._stop_event.is_set():
                    self._start_ws()  # 递归调用，建立新连接

        self._ws = websocket.WebSocketApp(
            ws_url,
            on_open=_on_open,
            on_message=_on_message,
            on_error=_on_error,
            on_close=_on_close,
        )

        def run_forever():
            """
            WebSocket运行线程的目标函数。

            使用websocket_app_run_forever运行WebSocket客户端，
            该函数兼容不同版本的websocket-client库。
            """
            try:
                websocket_app_run_forever(self._ws, ping_interval=0, reconnect=0)
            except (SystemExit, KeyboardInterrupt):
                logger.info("[QQ] WebSocket thread interrupted")
            except Exception as e:
                logger.error(f"[QQ] WebSocket run_forever error: {e}")

        # 在守护线程中运行WebSocket客户端
        self._ws_thread = threading.Thread(target=run_forever, daemon=True)
        self._ws_thread.start()
        # 阻塞等待线程结束，保持通道运行
        self._ws_thread.join()

    def _ws_send(self, data: dict):
        """
        通过WebSocket发送JSON消息。

        Args:
            data: 要发送的字典数据，将被序列化为JSON
        """
        if self._ws:
            self._ws.send(json.dumps(data, ensure_ascii=False))

    # ------------------------------------------------------------------
    # Identify & Resume & Heartbeat —— 鉴权、会话恢复和心跳
    # ------------------------------------------------------------------

    def _send_identify(self):
        """
        发送Identify操作码，进行身份鉴权。

        Identify是WebSocket连接建立后的第一个操作，告诉QQ服务器：
        - token：机器人身份凭证
        - intents：需要订阅的事件类型
        - shard：分片信息（[0,1]表示使用第0个分片，共1个分片）

        鉴权成功后服务器会返回READY事件，包含session_id等信息。
        """
        self._ws_send({
            "op": OP_IDENTIFY,
            "d": {
                "token": f"QQBot {self._get_access_token()}",
                "intents": DEFAULT_INTENTS,  # 订阅群聊+C2C事件和频道消息
                "shard": [0, 1],  # 单分片模式，适合大多数场景
                "properties": {
                    "$os": "linux",
                    "$browser": "chatgpt-on-wechat",
                    "$device": "chatgpt-on-wechat",
                },
            },
        })
        logger.debug(f"[QQ] Identify sent with intents={DEFAULT_INTENTS}")

    def _send_resume(self):
        """
        发送Resume操作码，尝试恢复之前的会话。

        Resume用于在断线重连时恢复之前的会话状态，避免重新鉴权。
        需要提供之前会话的session_id和最后收到的消息序列号(seq)，
        服务器会从seq之后继续推送未处理的消息。

        Resume成功后服务器返回RESUMED事件。
        如果Resume失败（如会话已过期），服务器会返回OP_INVALID_SESSION，
        此时需要重新Identify。
        """
        self._ws_send({
            "op": OP_RESUME,
            "d": {
                "token": f"QQBot {self._get_access_token()}",
                "session_id": self._session_id,
                "seq": self._last_seq,
            },
        })
        logger.debug(f"[QQ] Resume sent: session_id={self._session_id}, seq={self._last_seq}")

    def _start_heartbeat(self, interval_ms: int):
        """
        启动心跳线程，定期发送心跳保持WebSocket连接。

        QQ服务器要求客户端定期发送心跳（由Hello消息指定间隔），
        如果服务器长时间未收到心跳，会主动断开连接。

        Args:
            interval_ms: 心跳间隔（毫秒），由服务器的Hello消息指定
        """
        if self._heartbeat_thread and self._heartbeat_thread.is_alive():
            # 避免重复启动心跳线程
            return
        self._heartbeat_interval = interval_ms
        interval_sec = interval_ms / 1000.0

        def heartbeat_loop():
            """
            心跳循环，定期发送心跳消息。

            心跳内容为最后收到的消息序列号(last_seq)，
            服务器据此判断客户端是否落后于事件流。
            """
            while not self._stop_event.is_set() and self._connected:
                try:
                    self._ws_send({
                        "op": OP_HEARTBEAT,
                        "d": self._last_seq,  # 发送最后的序列号
                    })
                except Exception as e:
                    logger.warning(f"[QQ] Heartbeat send failed: {e}")
                    break
                # 使用Event.wait代替time.sleep，可以快速响应停止信号
                self._stop_event.wait(interval_sec)

        self._heartbeat_thread = threading.Thread(target=heartbeat_loop, daemon=True)
        self._heartbeat_thread.start()

    # ------------------------------------------------------------------
    # Incoming message dispatch —— 收到消息的分发处理
    # ------------------------------------------------------------------

    def _handle_ws_message(self, data: dict):
        """
        处理WebSocket收到的所有消息，根据操作码分发到不同的处理逻辑。

        消息分发规则：
        - OP_HELLO(10): 连接握手完成，开始鉴权或恢复
        - OP_HEARTBEAT_ACK(11): 心跳确认，无需处理
        - OP_HEARTBEAT(1): 服务器要求客户端立即发送心跳
        - OP_RECONNECT(7): 服务器要求重连
        - OP_INVALID_SESSION(9): 会话无效，需要重新鉴权
        - OP_DISPATCH(0): 业务事件分发，根据事件类型(t)进一步处理

        Args:
            data: WebSocket消息字典，包含op(操作码)、d(数据)、t(事件类型)、s(序列号)
        """
        op = data.get("op")   # 操作码
        d = data.get("d")     # 数据负载
        t = data.get("t")     # 事件类型（仅OP_DISPATCH有）
        s = data.get("s")     # 消息序列号

        # 记录最新的序列号，心跳和Resume时需要
        if s is not None:
            self._last_seq = s

        if op == OP_HELLO:
            # 收到Hello消息，连接握手成功
            heartbeat_interval = d.get("heartbeat_interval", 45000) if d else 45000
            logger.debug(f"[QQ] Received Hello, heartbeat_interval={heartbeat_interval}ms")
            self._heartbeat_interval = heartbeat_interval
            if self._can_resume and self._session_id:
                # 优先尝试Resume恢复之前的会话，减少消息丢失
                self._send_resume()
            else:
                # 无法Resume，发送Identify进行全新鉴权
                self._send_identify()

        elif op == OP_HEARTBEAT_ACK:
            # 心跳确认，连接正常，无需额外处理
            pass

        elif op == OP_HEARTBEAT:
            # 服务器要求立即发送心跳（通常在客户端心跳超时前触发）
            self._ws_send({"op": OP_HEARTBEAT, "d": self._last_seq})

        elif op == OP_RECONNECT:
            # 服务器要求重连（如服务器维护、负载均衡调整等）
            logger.warning("[QQ] Server requested reconnect")
            self._can_resume = True  # 标记可以Resume
            if self._ws:
                self._ws.close()  # 关闭当前连接，触发_on_close中的重连逻辑

        elif op == OP_INVALID_SESSION:
            # 会话已无效，需要重新Identify
            # 可能原因：session_id过期、服务器重启等
            logger.warning("[QQ] Invalid session, re-identifying...")
            self._session_id = None
            self._can_resume = False
            time.sleep(2)  # 短暂等待后重新鉴权
            self._send_identify()

        elif op == OP_DISPATCH:
            # 事件分发，根据事件类型t处理不同的业务事件
            if t == "READY":
                # 鉴权成功，连接就绪
                self._session_id = d.get("session_id", "")
                user = d.get("user", {})
                bot_name = user.get('username', '')
                logger.info(f"[QQ] ✅ Connected successfully (bot={bot_name})")
                self._connected = True
                self._can_resume = False  # 连接成功，清除Resume标志
                self._start_heartbeat(self._heartbeat_interval)  # 启动心跳
                self.report_startup_success()

            elif t == "RESUMED":
                # 会话恢复成功
                logger.info("[QQ] Session resumed successfully")
                self._connected = True
                self._can_resume = False
                self._start_heartbeat(self._heartbeat_interval)  # 重新启动心跳

            elif t in ("GROUP_AT_MESSAGE_CREATE", "C2C_MESSAGE_CREATE",
                        "AT_MESSAGE_CREATE", "DIRECT_MESSAGE_CREATE"):
                # 收到聊天消息事件，分发到消息处理逻辑
                # 四种消息类型：群@消息、C2C私聊、频道@消息、频道私信
                self._handle_msg_event(d, t)

            elif t in ("GROUP_ADD_ROBOT", "FRIEND_ADD"):
                # 机器人被添加到群或被添加好友的事件
                logger.info(f"[QQ] Event: {t}")

            else:
                # 其他事件类型，如成员加入、消息反应等
                logger.debug(f"[QQ] Dispatch event: {t}")

    # ------------------------------------------------------------------
    # Message event handling —— 消息事件处理
    # ------------------------------------------------------------------

    def _handle_msg_event(self, event_data: dict, event_type: str):
        """
        处理收到的聊天消息事件。

        处理流程：
        1. 消息去重（通过消息ID判断）
        2. 解析消息为QQMessage对象
        3. 文件缓存处理（图片+文本联合理解模式）
        4. 构建上下文并分发到消息处理队列

        Args:
            event_data: QQ事件数据字典，包含消息内容、发送者信息等
            event_type: 事件类型，如GROUP_AT_MESSAGE_CREATE等
        """
        msg_id = event_data.get("id", "")
        # 消息去重：防止同一条消息被重复处理
        if self.received_msgs.get(msg_id):
            logger.debug(f"[QQ] Duplicate msg filtered: {msg_id}")
            return
        self.received_msgs[msg_id] = True

        try:
            # 将QQ原始事件数据解析为统一的QQMessage对象
            qq_msg = QQMessage(event_data, event_type)
        except NotImplementedError as e:
            # 不支持的事件类型
            logger.warning(f"[QQ] {e}")
            return
        except Exception as e:
            logger.error(f"[QQ] Failed to parse message: {e}", exc_info=True)
            return

        is_group = qq_msg.is_group

        # 处理文件缓存逻辑 —— 支持先发图片后发文字的多模态交互
        from channel.file_cache import get_file_cache
        file_cache = get_file_cache()

        # 确定session_id：群聊使用group_openid，私聊使用user_openid
        if is_group:
            session_id = qq_msg.other_user_id
        else:
            session_id = qq_msg.from_user_id

        if qq_msg.ctype == ContextType.IMAGE:
            # 单张图片消息：缓存图片路径，等待用户后续的文本消息
            if hasattr(qq_msg, "image_path") and qq_msg.image_path:
                file_cache.add(session_id, qq_msg.image_path, file_type="image")
                logger.info(f"[QQ] Image cached for session {session_id}")
            return  # 图片不直接处理，等待文字消息

        if qq_msg.ctype == ContextType.TEXT:
            cached_files = file_cache.get(session_id)
            if cached_files:
                # 将缓存的文件引用附加到文本消息中，实现多模态理解
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
                qq_msg.content = qq_msg.content + "\n" + "\n".join(file_refs)
                logger.info(f"[QQ] Attached {len(cached_files)} cached file(s)")
                file_cache.clear(session_id)  # 清除已使用的缓存

        context = self._compose_context(
            qq_msg.ctype,
            qq_msg.content,
            isgroup=is_group,
            msg=qq_msg,
            no_need_at=True,  # 消息已经过@过滤
        )
        if context:
            self.produce(context)

    # ------------------------------------------------------------------
    # _compose_context —— 上下文构建
    # ------------------------------------------------------------------

    def _compose_context(self, ctype: ContextType, content, **kwargs):
        """
        构建消息处理上下文。

        该方法在父类ChatChannel的基础上，增加了QQ特有的上下文处理逻辑：
        1. 设置session_id（群聊和私聊使用不同的ID策略）
        2. 设置receiver（消息回复目标）
        3. 检查图片生成前缀

        Args:
            ctype: 消息内容类型
            content: 消息内容
            **kwargs: 额外参数

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

        if cmsg.is_group:
            # 群聊：使用group_openid作为session_id
            context["session_id"] = cmsg.other_user_id
        else:
            # 私聊：使用user_openid作为session_id
            context["session_id"] = cmsg.from_user_id

        context["receiver"] = cmsg.other_user_id

        if ctype == ContextType.TEXT:
            # 检查是否包含图片生成前缀
            img_match_prefix = check_prefix(content, conf().get("image_create_prefix"))
            if img_match_prefix:
                # 去除前缀，将消息类型改为IMAGE_CREATE
                content = content.replace(img_match_prefix, "", 1)
                context.type = ContextType.IMAGE_CREATE
            else:
                context.type = ContextType.TEXT
            context.content = content.strip()

        return context

    # ------------------------------------------------------------------
    # Send reply —— 回复消息发送
    # ------------------------------------------------------------------

    def send(self, reply: Reply, context: Context):
        """
        发送回复消息的统一入口。

        根据reply类型选择不同的发送策略：
        1. TEXT: 发送文本消息
        2. IMAGE_URL/IMAGE: 上传图片后发送
        3. FILE: 先发送附加文本，再上传文件发送
        4. VIDEO/VIDEO_URL: 上传视频后发送
        5. 其他类型: 降级为文本发送

        如果没有原始消息（如定时任务场景），使用主动发送API。

        Args:
            reply: 回复对象
            context: 上下文对象
        """
        msg = context.get("msg")
        is_group = context.get("isgroup", False)
        receiver = context.get("receiver", "")

        if not msg:
            # Active send (e.g. scheduled tasks), no original message to reply to
            # 定时任务等无原始消息的场景，使用主动发送API
            self._active_send_text(reply.content if reply.type == ReplyType.TEXT else str(reply.content),
                                   receiver, is_group)
            return

        event_type = getattr(msg, "event_type", "")
        msg_id = getattr(msg, "msg_id", "")

        if reply.type == ReplyType.TEXT:
            self._send_text(reply.content, msg, event_type, msg_id)
        elif reply.type in (ReplyType.IMAGE_URL, ReplyType.IMAGE):
            self._send_image(reply.content, msg, event_type, msg_id)
        elif reply.type == ReplyType.FILE:
            # 文件发送前，先发送附加的文本说明
            if hasattr(reply, "text_content") and reply.text_content:
                self._send_text(reply.text_content, msg, event_type, msg_id)
                time.sleep(0.3)  # 延迟确保文本先到达
            self._send_file(reply.content, msg, event_type, msg_id)
        elif reply.type in (ReplyType.VIDEO, ReplyType.VIDEO_URL):
            self._send_media(reply.content, msg, event_type, msg_id, QQ_FILE_TYPE_VIDEO)
        else:
            # 不支持的回复类型，降级为文本发送
            logger.warning(f"[QQ] Unsupported reply type: {reply.type}, falling back to text")
            self._send_text(str(reply.content), msg, event_type, msg_id)

    # ------------------------------------------------------------------
    # Send helpers —— 发送辅助方法
    # ------------------------------------------------------------------

    def _get_next_msg_seq(self, msg_id: str) -> int:
        """
        获取下一个消息序列号。

        QQ API要求同一原始消息下的多条回复使用递增的msg_seq，
        以确保消息的顺序性。每次调用返回当前值并递增。

        Args:
            msg_id: 原始消息ID，作为计数器的key

        Returns:
            int: 当前消息序列号
        """
        seq = self._msg_seq_counter.get(msg_id, 1)
        self._msg_seq_counter[msg_id] = seq + 1
        return seq

    def _build_msg_url_and_base_body(self, msg: QQMessage, event_type: str, msg_id: str):
        """
        根据事件类型构建API URL和基础请求体。

        QQ的不同消息场景使用不同的API端点和参数格式：
        - 群聊: /v2/groups/{group_openid}/messages
        - C2C: /v2/users/{user_openid}/messages
        - 频道: /channels/{channel_id}/messages
        - 私信: /dms/{guild_id}/messages

        Args:
            msg: QQ消息对象
            event_type: 事件类型
            msg_id: 原始消息ID

        Returns:
            tuple: (url, body, msg_scene, scene_id)
                - url: API端点URL
                - body: 基础请求体（包含msg_id和msg_seq）
                - msg_scene: 消息场景标识 (group/c2c/channel/dm)
                - scene_id: 场景ID
        """
        if event_type == "GROUP_AT_MESSAGE_CREATE":
            # 群聊@消息
            group_openid = msg._rawmsg.get("group_openid", "")
            url = f"{QQ_API_BASE}/v2/groups/{group_openid}/messages"
            body = {
                "msg_id": msg_id,
                "msg_seq": self._get_next_msg_seq(msg_id),
            }
            return url, body, "group", group_openid

        elif event_type == "C2C_MESSAGE_CREATE":
            # C2C私聊消息
            user_openid = msg._rawmsg.get("author", {}).get("user_openid", "") or msg.from_user_id
            url = f"{QQ_API_BASE}/v2/users/{user_openid}/messages"
            body = {
                "msg_id": msg_id,
                "msg_seq": self._get_next_msg_seq(msg_id),
            }
            return url, body, "c2c", user_openid

        elif event_type == "AT_MESSAGE_CREATE":
            # 频道@消息
            channel_id = msg._rawmsg.get("channel_id", "")
            url = f"{QQ_API_BASE}/channels/{channel_id}/messages"
            body = {"msg_id": msg_id}
            return url, body, "channel", channel_id

        elif event_type == "DIRECT_MESSAGE_CREATE":
            # 频道私信
            guild_id = msg._rawmsg.get("guild_id", "")
            url = f"{QQ_API_BASE}/dms/{guild_id}/messages"
            body = {"msg_id": msg_id}
            return url, body, "dm", guild_id

        return None, None, None, None

    def _post_message(self, url: str, body: dict, event_type: str):
        """
        发送HTTP POST请求到QQ API。

        统一的API请求方法，处理鉴权头和错误日志。

        Args:
            url: API端点URL
            body: 请求体
            event_type: 事件类型，用于日志标识
        """
        try:
            resp = requests.post(url, json=body, headers=self._get_auth_headers(), timeout=10)
            if resp.status_code in (200, 201, 202, 204):
                logger.info(f"[QQ] Message sent successfully: event_type={event_type}")
            else:
                logger.error(f"[QQ] Failed to send message: status={resp.status_code}, "
                             f"body={resp.text}")
        except Exception as e:
            logger.error(f"[QQ] Send message error: {e}")

    # ------------------------------------------------------------------
    # Active send (no original message, e.g. scheduled tasks) —— 主动发送
    # ------------------------------------------------------------------

    def _active_send_text(self, content: str, receiver: str, is_group: bool):
        """
        主动发送文本消息（无原始消息上下文）。

        用于定时任务等场景，机器人主动推送消息给用户或群。
        QQ限制主动消息每月每个用户只能接收4条，因此不宜频繁使用。

        Args:
            content: 消息内容
            receiver: 接收者ID (group_openid或user_openid)
            is_group: 是否为群聊
        """
        if not receiver:
            logger.warning("[QQ] No receiver for active send")
            return
        if is_group:
            url = f"{QQ_API_BASE}/v2/groups/{receiver}/messages"
        else:
            url = f"{QQ_API_BASE}/v2/users/{receiver}/messages"
        body = {
            "content": content,
            "msg_type": 0,  # 0表示文本消息
        }
        event_label = "GROUP_ACTIVE" if is_group else "C2C_ACTIVE"
        self._post_message(url, body, event_label)

    # ------------------------------------------------------------------
    # Send text —— 发送文本消息
    # ------------------------------------------------------------------

    def _send_text(self, content: str, msg: QQMessage, event_type: str, msg_id: str):
        """
        回复文本消息。

        根据事件类型构建对应的API请求，发送文本内容。

        Args:
            content: 文本内容
            msg: QQ消息对象
            event_type: 事件类型
            msg_id: 原始消息ID
        """
        url, body, _, _ = self._build_msg_url_and_base_body(msg, event_type, msg_id)
        if not url:
            logger.warning(f"[QQ] Cannot send reply for event_type: {event_type}")
            return
        body["content"] = content
        body["msg_type"] = 0  # 文本消息类型
        self._post_message(url, body, event_type)

    # ------------------------------------------------------------------
    # Rich media upload & send (image / video / file) —— 富媒体上传和发送
    # ------------------------------------------------------------------

    def _upload_rich_media(self, file_url: str, file_type: int, msg: QQMessage,
                           event_type: str) -> str:
        """
        通过URL上传富媒体文件到QQ服务器。

        QQ的富媒体上传API支持通过URL直接上传，无需先下载到本地。
        上传成功后返回file_info，用于后续发送富媒体消息。

        仅支持群聊和C2C场景，频道和私信不支持富媒体上传。

        Args:
            file_url: 文件的HTTP URL
            file_type: 文件类型常量 (QQ_FILE_TYPE_IMAGE/VIDEO/VOICE/FILE)
            msg: QQ消息对象
            event_type: 事件类型

        Returns:
            str: file_info字符串，用于发送消息；失败返回空字符串
        """
        if event_type == "GROUP_AT_MESSAGE_CREATE":
            group_openid = msg._rawmsg.get("group_openid", "")
            upload_url = f"{QQ_API_BASE}/v2/groups/{group_openid}/files"
        elif event_type == "C2C_MESSAGE_CREATE":
            user_openid = (msg._rawmsg.get("author", {}).get("user_openid", "")
                           or msg.from_user_id)
            upload_url = f"{QQ_API_BASE}/v2/users/{user_openid}/files"
        else:
            logger.warning(f"[QQ] Rich media upload not supported for event_type: {event_type}")
            return ""

        upload_body = {
            "file_type": file_type,
            "url": file_url,
            "srv_send_msg": False,  # 不自动发送，由我们控制发送时机
        }

        try:
            resp = requests.post(
                upload_url, json=upload_body,
                headers=self._get_auth_headers(), timeout=30,
            )
            if resp.status_code in (200, 201):
                data = resp.json()
                file_info = data.get("file_info", "")
                logger.info(f"[QQ] Rich media uploaded: file_type={file_type}, "
                            f"file_uuid={data.get('file_uuid', '')}")
                return file_info
            else:
                logger.error(f"[QQ] Rich media upload failed: status={resp.status_code}, "
                             f"body={resp.text}")
                return ""
        except Exception as e:
            logger.error(f"[QQ] Rich media upload error: {e}")
            return ""

    def _upload_rich_media_base64(self, file_path: str, file_type: int, msg: QQMessage,
                                  event_type: str) -> str:
        """
        通过base64编码上传本地文件到QQ服务器。

        当文件在本地而非HTTP URL时，使用base64方式上传。
        读取本地文件内容，编码为base64字符串，通过file_data字段上传。

        Args:
            file_path: 本地文件路径
            file_type: 文件类型常量
            msg: QQ消息对象
            event_type: 事件类型

        Returns:
            str: file_info字符串；失败返回空字符串
        """
        if event_type == "GROUP_AT_MESSAGE_CREATE":
            group_openid = msg._rawmsg.get("group_openid", "")
            upload_url = f"{QQ_API_BASE}/v2/groups/{group_openid}/files"
        elif event_type == "C2C_MESSAGE_CREATE":
            user_openid = (msg._rawmsg.get("author", {}).get("user_openid", "")
                           or msg.from_user_id)
            upload_url = f"{QQ_API_BASE}/v2/users/{user_openid}/files"
        else:
            logger.warning(f"[QQ] Rich media upload not supported for event_type: {event_type}")
            return ""

        try:
            with open(file_path, "rb") as f:
                file_data = base64.b64encode(f.read()).decode("utf-8")
        except Exception as e:
            logger.error(f"[QQ] Failed to read file for upload: {e}")
            return ""

        upload_body = {
            "file_type": file_type,
            "file_data": file_data,  # base64编码的文件内容
            "srv_send_msg": False,
        }

        try:
            resp = requests.post(
                upload_url, json=upload_body,
                headers=self._get_auth_headers(), timeout=30,
            )
            if resp.status_code in (200, 201):
                data = resp.json()
                file_info = data.get("file_info", "")
                logger.info(f"[QQ] Rich media uploaded (base64): file_type={file_type}, "
                            f"file_uuid={data.get('file_uuid', '')}")
                return file_info
            else:
                logger.error(f"[QQ] Rich media upload (base64) failed: status={resp.status_code}, "
                             f"body={resp.text}")
                return ""
        except Exception as e:
            logger.error(f"[QQ] Rich media upload (base64) error: {e}")
            return ""

    def _send_media_msg(self, file_info: str, msg: QQMessage, event_type: str, msg_id: str):
        """
        发送富媒体消息（msg_type=7）。

        QQ的富媒体消息使用msg_type=7，通过media.file_info字段
        引用已上传的文件。这是图片、视频、文件等非文本消息的统一发送方式。

        Args:
            file_info: 上传后获得的文件信息字符串
            msg: QQ消息对象
            event_type: 事件类型
            msg_id: 原始消息ID
        """
        url, body, _, _ = self._build_msg_url_and_base_body(msg, event_type, msg_id)
        if not url:
            return
        body["msg_type"] = 7  # 7表示富媒体消息
        body["media"] = {"file_info": file_info}
        self._post_message(url, body, event_type)

    def _send_image(self, img_path_or_url: str, msg: QQMessage, event_type: str, msg_id: str):
        """
        发送图片回复。

        根据图片来源选择不同的上传方式：
        - HTTP URL: 通过URL直接上传（_upload_rich_media）
        - 本地文件: 通过base64编码上传（_upload_rich_media_base64）

        仅群聊和C2C场景支持图片上传，其他场景降级为文本发送。

        Args:
            img_path_or_url: 图片路径或URL
            msg: QQ消息对象
            event_type: 事件类型
            msg_id: 原始消息ID
        """
        if event_type not in ("GROUP_AT_MESSAGE_CREATE", "C2C_MESSAGE_CREATE"):
            # 频道和私信场景不支持富媒体上传，降级为发送URL文本
            self._send_text(str(img_path_or_url), msg, event_type, msg_id)
            return

        if img_path_or_url.startswith("file://"):
            img_path_or_url = img_path_or_url[7:]  # 去除file://协议前缀

        if img_path_or_url.startswith(("http://", "https://")):
            # HTTP URL：通过URL上传
            file_info = self._upload_rich_media(
                img_path_or_url, QQ_FILE_TYPE_IMAGE, msg, event_type)
        elif os.path.exists(img_path_or_url):
            # 本地文件：通过base64上传
            file_info = self._upload_rich_media_base64(
                img_path_or_url, QQ_FILE_TYPE_IMAGE, msg, event_type)
        else:
            logger.error(f"[QQ] Image not found: {img_path_or_url}")
            self._send_text("[Image send failed]", msg, event_type, msg_id)
            return

        if file_info:
            self._send_media_msg(file_info, msg, event_type, msg_id)
        else:
            # 上传失败，降级为文本提示
            self._send_text("[Image upload failed]", msg, event_type, msg_id)

    def _send_file(self, file_path_or_url: str, msg: QQMessage, event_type: str, msg_id: str):
        """
        发送文件回复。

        逻辑与_send_image类似，但使用QQ_FILE_TYPE_FILE类型。

        Args:
            file_path_or_url: 文件路径或URL
            msg: QQ消息对象
            event_type: 事件类型
            msg_id: 原始消息ID
        """
        if event_type not in ("GROUP_AT_MESSAGE_CREATE", "C2C_MESSAGE_CREATE"):
            self._send_text(str(file_path_or_url), msg, event_type, msg_id)
            return

        if file_path_or_url.startswith("file://"):
            file_path_or_url = file_path_or_url[7:]

        if file_path_or_url.startswith(("http://", "https://")):
            file_info = self._upload_rich_media(
                file_path_or_url, QQ_FILE_TYPE_FILE, msg, event_type)
        elif os.path.exists(file_path_or_url):
            file_info = self._upload_rich_media_base64(
                file_path_or_url, QQ_FILE_TYPE_FILE, msg, event_type)
        else:
            logger.error(f"[QQ] File not found: {file_path_or_url}")
            self._send_text("[File send failed]", msg, event_type, msg_id)
            return

        if file_info:
            self._send_media_msg(file_info, msg, event_type, msg_id)
        else:
            self._send_text("[File upload failed]", msg, event_type, msg_id)

    def _send_media(self, path_or_url: str, msg: QQMessage, event_type: str,
                    msg_id: str, file_type: int):
        """
        通用媒体发送方法，用于视频、语音等类型的文件。

        与_send_image和_send_file的逻辑相同，但file_type由调用方指定，
        支持QQ_FILE_TYPE_VIDEO和QQ_FILE_TYPE_VOICE等类型。

        Args:
            path_or_url: 文件路径或URL
            msg: QQ消息对象
            event_type: 事件类型
            msg_id: 原始消息ID
            file_type: QQ文件类型常量
        """
        if event_type not in ("GROUP_AT_MESSAGE_CREATE", "C2C_MESSAGE_CREATE"):
            # 非群聊/C2C场景，降级为文本发送
            self._send_text(str(path_or_url), msg, event_type, msg_id)
            return

        if path_or_url.startswith("file://"):
            path_or_url = path_or_url[7:]

        if path_or_url.startswith(("http://", "https://")):
            file_info = self._upload_rich_media(path_or_url, file_type, msg, event_type)
        elif os.path.exists(path_or_url):
            file_info = self._upload_rich_media_base64(path_or_url, file_type, msg, event_type)
        else:
            logger.error(f"[QQ] Media not found: {path_or_url}")
            return

        if file_info:
            self._send_media_msg(file_info, msg, event_type, msg_id)
        else:
            logger.error(f"[QQ] Media upload failed: {path_or_url}")
