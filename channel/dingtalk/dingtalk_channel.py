"""
钉钉通道接入

@author huiwen
@Date 2023/11/28
"""
import copy
import json
# -*- coding=utf-8 -*-
import logging
import os
import time
import requests

import dingtalk_stream
from dingtalk_stream import AckMessage
from dingtalk_stream.card_replier import AICardReplier
from dingtalk_stream.card_replier import AICardStatus
from dingtalk_stream.card_replier import CardReplier

from bridge.context import Context, ContextType
from bridge.reply import Reply, ReplyType
from channel.chat_channel import ChatChannel
from common.utils import expand_path
from channel.dingtalk.dingtalk_message import DingTalkMessage
from common.expired_dict import ExpiredDict
from common.log import logger
from common.singleton import singleton
from common.time_check import time_checker
from config import conf


class CustomAICardReplier(CardReplier):
    """
    自定义AI卡片回复器，继承自钉钉SDK的CardReplier。

    为什么要自定义：钉钉SDK自带的AICardReplier.start()方法在某些场景下行为不符合预期，
    因此通过猴子补丁(monkey patch)的方式替换其start方法，确保AI卡片创建时
    流程状态(flowStatus)被正确设置为PROCESSING（处理中），让用户在等待AI回复时
    看到卡片处于加载/处理中的状态，提升交互体验。
    """

    def __init__(self, dingtalk_client, incoming_message):
        """
        初始化自定义AI卡片回复器。

        Args:
            dingtalk_client: 钉钉Stream客户端实例，用于与钉钉服务器通信
            incoming_message: 接收到的钉钉消息对象，用于确定回复的目标会话
        """
        super(AICardReplier, self).__init__(dingtalk_client, incoming_message)

    def start(
            self,
            card_template_id: str,
            card_data: dict,
            recipients: list = None,
            support_forward: bool = True,
    ) -> str:
        """
        AI卡片的创建接口。

        在原始卡片数据基础上，自动注入flowStatus=PROCESSING状态，
        表示AI正在处理中。这样卡片在钉钉客户端上会显示为"思考中"的视觉效果。

        :param support_forward: 是否允许转发该卡片，默认允许
        :param recipients: 卡片接收者列表，为None时发送给消息发送者
        :param card_template_id: 钉钉卡片模板ID，定义了卡片的布局和样式
        :param card_data: 卡片数据字典，包含填充模板的动态内容
        :return: 创建的卡片消息ID
        """
        # 深拷贝卡片数据，避免修改原始数据影响其他逻辑
        card_data_with_status = copy.deepcopy(card_data)
        # 设置流程状态为"处理中"，用户将看到加载动画
        card_data_with_status["flowStatus"] = AICardStatus.PROCESSING
        return self.create_and_send_card(
            card_template_id,
            card_data_with_status,
            at_sender=True,       # 在群聊中@发送者，提醒其查看回复
            at_all=False,         # 不@所有人，避免打扰其他群成员
            recipients=recipients,
            support_forward=support_forward,
        )


# 对 AICardReplier 进行猴子补丁（Monkey Patch）
# 原因：SDK默认的AICardReplier.start()缺少flowStatus的设置，
# 导致卡片创建时没有"处理中"的状态提示，用户体验不佳。
# 通过替换start方法，确保每次创建AI卡片都带有PROCESSING状态。
AICardReplier.start = CustomAICardReplier.start


def _check(func):
    """
    消息去重和时间校验装饰器。

    该装饰器对消息处理函数进行包装，实现三个关键校验：
    1. 消息幂等性：通过msg_id去重，防止同一条消息被重复处理
    2. 历史消息过滤：当hot_reload启用时，跳过1分钟前的历史消息，
       避免机器人重启后处理大量离线积压消息
    3. 自身消息过滤：在单聊中跳过机器人自己发出的消息，防止死循环

    Args:
        func: 被装饰的消息处理函数（handle_single或handle_group）

    Returns:
        包装后的函数，执行校验通过后才调用原函数
    """
    def wrapper(self, cmsg: DingTalkMessage):
        msgId = cmsg.msg_id
        # 幂等校验：如果该消息ID已经被处理过，则跳过
        # 使用ExpiredDict自动过期清理，避免内存无限增长
        if msgId in self.receivedMsgs:
            logger.info("DingTalk message {} already received, ignore".format(msgId))
            return
        # 记录已处理的消息ID
        self.receivedMsgs[msgId] = True
        create_time = cmsg.create_time  # 消息时间戳
        # 热重载模式下，跳过1分钟前的历史消息
        # 原因：机器人重启后钉钉会推送离线期间的所有消息，
        # 如果不过滤会导致大量历史消息涌入处理队列
        if conf().get("hot_reload") == True and int(create_time) < int(time.time()) - 60:  # 跳过1分钟前的历史消息
            logger.debug("[DingTalk] History message {} skipped".format(msgId))
            return
        # 单聊中跳过自己发的消息，防止机器人回复自己造成死循环
        if cmsg.my_msg and not cmsg.is_group:
            logger.debug("[DingTalk] My message {} skipped".format(msgId))
            return
        return func(self, cmsg)

    return wrapper


@singleton
class DingTalkChanel(ChatChannel, dingtalk_stream.ChatbotHandler):
    """
    钉钉通道主类，同时继承ChatChannel和钉钉SDK的ChatbotHandler。

    该类负责：
    1. 建立和维护钉钉Stream长连接，接收实时消息
    2. 消息的接收、去重、解析和分发（单聊/群聊）
    3. 回复消息的发送（文本、图片、文件、视频等）
    4. 钉钉API的access_token管理和媒体文件上传

    设计模式：
    - 使用singleton装饰器确保全局只有一个通道实例
    - 使用Stream模式而非Webhook模式，无需公网IP即可接收消息
    - 自管理WebSocket连接生命周期，支持优雅停止和自动重连

    使用单例模式的原因：通道需要维护连接状态、消息去重字典和token缓存，
    多实例会导致状态不一致和资源浪费。
    """
    dingtalk_client_id = conf().get('dingtalk_client_id')
    dingtalk_client_secret = conf().get('dingtalk_client_secret')

    def setup_logger(self):
        """
        配置日志记录器。

        将钉钉SDK的日志级别设为WARNING，避免其大量DEBUG日志干扰主程序日志输出。
        返回专用于钉钉通道的logger实例。

        Returns:
            logging.Logger: 钉钉通道专用的日志记录器
        """
        # Suppress verbose logs from dingtalk_stream SDK
        logging.getLogger("dingtalk_stream").setLevel(logging.WARNING)
        return logging.getLogger("DingTalk")

    def __init__(self):
        """
        初始化钉钉通道。

        主要完成以下工作：
        1. 调用两个父类的初始化方法，确保ChatChannel和ChatbotHandler都被正确初始化
        2. 创建消息去重字典，使用ExpiredDict自动过期清理
        3. 初始化access_token缓存，避免频繁请求钉钉API
        4. 配置群聊白名单和单聊前缀，使所有群聊和单聊消息都能被处理
        """
        super().__init__()
        super(dingtalk_stream.ChatbotHandler, self).__init__()
        self.logger = self.setup_logger()
        # 历史消息id暂存，用于幂等控制
        # 过期时间默认3600秒（1小时），与access_token生命周期对齐
        self.receivedMsgs = ExpiredDict(conf().get("expires_in_seconds", 3600))
        self._stream_client = None       # 钉钉Stream客户端实例
        self._running = False            # 通道运行状态标志，用于控制启动循环的退出
        self._event_loop = None          # asyncio事件循环，用于WebSocket会话
        logger.debug("[DingTalk] client_id={}, client_secret={} ".format(
            self.dingtalk_client_id, self.dingtalk_client_secret))
        # 无需群校验和前缀 —— 设置所有群都在白名单中
        conf()["group_name_white_list"] = ["ALL_GROUP"]
        # 单聊无需前缀 —— 空字符串表示所有消息都触发处理
        conf()["single_chat_prefix"] = [""]
        # Access token cache —— 缓存钉钉API的access_token，避免每次请求都重新获取
        self._access_token = None
        self._access_token_expires_at = 0  # token过期时间戳，用于判断是否需要刷新
        # Robot code cache (extracted from incoming messages)
        # 从收到的消息中提取并缓存robot_code，用于后续主动发送消息
        self._robot_code = None

    def _open_connection(self, client):
        """
        手动建立钉钉Stream连接，绕过SDK内部的错误吞没机制。

        为什么要绕过SDK：dingtalk_stream SDK内部的connect方法会捕获并吞掉连接错误，
        只返回模糊的错误信息，导致难以定位连接失败的根本原因（如凭证错误、网络问题等）。
        因此我们直接调用钉钉的Gateway API获取连接参数，获取详细的错误信息。

        API文档: https://open.dingtalk.com/document/orgapp/establish-stream-connection

        Args:
            client: 钉钉Stream客户端实例，包含凭证信息

        Returns:
            tuple: (connection_dict, error_str)
                - 成功时: connection_dict包含endpoint和ticket，error_str为空
                - 失败时: connection_dict为None，error_str包含可读的错误信息
        """
        try:
            resp = requests.post(
                "https://api.dingtalk.com/v1.0/gateway/connections/open",
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                json={
                    "clientId": client.credential.client_id,
                    "clientSecret": client.credential.client_secret,
                    # 订阅机器人消息回调事件
                    "subscriptions": [{"type": "CALLBACK",
                                       "topic": dingtalk_stream.chatbot.ChatbotMessage.TOPIC}],
                    "ua": "dingtalk-sdk-python/cow",
                    "localIp": "",
                },
                timeout=10,
            )
            body = resp.json()
            if not resp.ok:
                # 提取API返回的错误码和错误消息，便于排查问题
                code = body.get("code", resp.status_code)
                message = body.get("message", resp.reason)
                return None, f"open connection failed: [{code}] {message}"
            return body, ""
        except Exception as e:
            return None, f"open connection failed: {e}"

    def startup(self):
        """
        启动钉钉通道，建立Stream长连接并进入消息接收循环。

        启动流程：
        1. 从配置中读取并刷新凭证
        2. 创建钉钉Stream客户端并注册消息回调
        3. 进入自管理的连接循环（而非使用SDK的client.start()）
        4. 通过WebSocket接收实时消息

        为什么自管理连接循环而不使用SDK的start()：
        - SDK的start()方法内部会吞掉连接错误，不利于问题排查
        - 自管理循环可以实现更快的停止响应（_running标志检查）
        - 可以在连接失败时获取详细错误信息并报告给上层
        - 支持可中断的睡眠，避免stop()时长时间等待
        """
        import asyncio
        self.dingtalk_client_id = conf().get('dingtalk_client_id')
        self.dingtalk_client_secret = conf().get('dingtalk_client_secret')
        self._running = True
        # 创建凭证和Stream客户端
        credential = dingtalk_stream.Credential(self.dingtalk_client_id, self.dingtalk_client_secret)
        client = dingtalk_stream.DingTalkStreamClient(credential)
        self._stream_client = client
        # 注册消息回调处理器，当收到机器人消息时调用self.process()
        client.register_callback_handler(dingtalk_stream.chatbot.ChatbotMessage.TOPIC, self)
        logger.info("[DingTalk] ✅ Stream client initialized, ready to receive messages")

        # Run the connection loop ourselves instead of delegating to client.start(),
        # so we can get detailed error messages and respond to stop() quickly.
        import urllib.parse as _urlparse
        import websockets as _ws
        import json as _json
        client.pre_start()
        _first_connect = True  # 标记是否为首次连接，用于区分首次连接失败和断线重连
        while self._running:
            # Open connection using our own request so we get detailed error info.
            connection, err_msg = self._open_connection(client)

            if connection is None:
                # 连接失败的处理
                if _first_connect:
                    # 首次连接失败，报告启动错误
                    logger.warning(f"[DingTalk] {err_msg}")
                    self.report_startup_error(err_msg)
                    _first_connect = False
                else:
                    # 非首次连接失败（断线重连场景），10秒后重试
                    logger.warning(f"[DingTalk] {err_msg}, retrying in 10s...")

                # Interruptible sleep: checks _running every 100ms.
                # 使用分段睡眠而非time.sleep(10)，以便在stop()调用时能快速响应
                for _ in range(100):
                    if not self._running:
                        break
                    time.sleep(0.1)
                continue

            if _first_connect:
                # 首次连接成功
                logger.info("[DingTalk] ✅ Connected to DingTalk stream")
                self.report_startup_success()
                _first_connect = False
            else:
                # 断线重连成功
                logger.info("[DingTalk] Reconnected to DingTalk stream")

            # Run the WebSocket session in an asyncio loop.
            # 构建WebSocket连接URL，包含从Gateway API获取的ticket
            uri = '%s?ticket=%s' % (
                connection['endpoint'],
                _urlparse.quote_plus(connection['ticket'])
            )
            # 为每个WebSocket会话创建独立的asyncio事件循环
            # 避免不同会话之间的事件循环冲突
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._event_loop = loop
            try:
                async def _session():
                    """
                    WebSocket会话协程。

                    持续监听WebSocket消息，将收到的JSON消息路由到SDK的处理方法。
                    当SDK返回TAG_DISCONNECT时，主动断开连接触发重连。
                    """
                    async with _ws.connect(uri) as websocket:
                        client.websocket = websocket
                        async for raw_message in websocket:
                            json_message = _json.loads(raw_message)
                            result = await client.route_message(json_message)
                            # SDK返回断开标签时，退出当前会话，外层循环将建立新连接
                            if result == dingtalk_stream.DingTalkStreamClient.TAG_DISCONNECT:
                                break

                loop.run_until_complete(_session())
            except (KeyboardInterrupt, SystemExit):
                # 收到进程中断信号，优雅退出
                logger.info("[DingTalk] Session loop received stop signal, exiting")
                break
            except Exception as e:
                if not self._running:
                    # 如果是主动停止导致的异常，直接退出
                    break
                # WebSocket会话异常（如网络断开），3秒后重连
                logger.warning(f"[DingTalk] Stream session error: {e}, reconnecting in 3s...")
                # 同样使用分段睡眠以支持快速停止
                for _ in range(30):
                    if not self._running:
                        break
                    time.sleep(0.1)
            finally:
                # 清理当前会话的事件循环
                self._event_loop = None
                try:
                    loop.close()
                except Exception:
                    pass

        logger.info("[DingTalk] Startup loop exited")

    def stop(self):
        """
        停止钉钉通道。

        停止流程：
        1. 设置_running标志为False，通知启动循环退出
        2. 停止asyncio事件循环，中断正在进行的WebSocket会话
        3. 清空Stream客户端引用

        事件循环的停止通过call_soon_threadsafe实现，因为stop()可能从
        非事件循环线程调用，需要线程安全地停止循环。
        """
        logger.info("[DingTalk] stop() called, setting _running=False")
        self._running = False
        loop = self._event_loop
        if loop and not loop.is_closed():
            try:
                # 从外部线程安全地停止事件循环
                loop.call_soon_threadsafe(loop.stop)
                logger.info("[DingTalk] Sent stop signal to event loop")
            except Exception as e:
                logger.warning(f"[DingTalk] Error stopping event loop: {e}")
        self._stream_client = None
        logger.info("[DingTalk] stop() completed")

    def get_access_token(self):
        """
        获取企业内部应用的 access_token。

        该方法实现了token缓存机制：
        - 如果缓存的token尚未过期，直接返回缓存值，避免频繁请求API
        - 如果token已过期或不存在，向钉钉OAuth2接口请求新token
        - 新token的有效期通常为2小时(7200秒)，提前5分钟刷新以避免边界问题

        文档: https://open.dingtalk.com/document/orgapp/obtain-orgapp-token

        Returns:
            str: 有效的access_token，失败时返回None
        """
        current_time = time.time()

        # 如果 token 还没过期，直接返回缓存的 token
        # 这是性能优化的关键：避免每次API调用都请求新token
        if self._access_token and current_time < self._access_token_expires_at:
            return self._access_token

        # 获取新的 access_token
        url = "https://api.dingtalk.com/v1.0/oauth2/accessToken"
        headers = {"Content-Type": "application/json"}
        data = {
            "appKey": self.dingtalk_client_id,
            "appSecret": self.dingtalk_client_secret
        }

        try:
            response = requests.post(url, headers=headers, json=data, timeout=10)
            result = response.json()

            if response.status_code == 200 and "accessToken" in result:
                self._access_token = result["accessToken"]
                # Token 有效期为 2 小时，提前 5 分钟刷新
                # 提前刷新的原因：如果在过期瞬间发起请求，可能因为网络延迟
                # 导致token在使用时已经过期
                self._access_token_expires_at = current_time + result.get("expireIn", 7200) - 300
                logger.info("[DingTalk] Access token refreshed successfully")
                return self._access_token
            else:
                logger.error(f"[DingTalk] Failed to get access token: {result}")
                return None
        except Exception as e:
            logger.error(f"[DingTalk] Error getting access token: {e}")
            return None

    def send_single_message(self, user_id: str, content: str, robot_code: str) -> bool:
        """
        主动发送单聊消息给指定用户。

        该方法用于在没有原始消息上下文时（如定时任务推送），主动向用户发送消息。
        与回复消息不同，主动发送需要指定robot_code来标识发送的机器人身份。

        API: https://open.dingtalk.com/document/orgapp/chatbots-send-one-on-one-chat-messages-in-batches

        Args:
            user_id: 接收者的staff_id（钉钉员工ID）
            content: 消息文本内容
            robot_code: 机器人编码，标识由哪个机器人发送消息

        Returns:
            bool: 发送成功返回True，失败返回False
        """
        access_token = self.get_access_token()
        if not access_token:
            logger.error("[DingTalk] Failed to send single message: Access token not available.")
            return False

        if not robot_code:
            # robot_code是必填参数，没有它钉钉API无法确定消息来源
            logger.error("[DingTalk] Cannot send single message: robot_code is required")
            return False

        url = "https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend"
        headers = {
            "x-acs-dingtalk-access-token": access_token,
            "Content-Type": "application/json"
        }
        data = {
            "msgParam": json.dumps({"content": content}),
            "msgKey": "sampleText",  # 消息类型：文本消息
            "userIds": [user_id],
            "robotCode": robot_code
        }

        logger.info(f"[DingTalk] Sending single message to user {user_id} with robot_code {robot_code}")
        try:
            response = requests.post(url, headers=headers, json=data, timeout=10)
            result = response.json()

            # processQueryKey存在表示消息已成功提交到发送队列
            if response.status_code == 200 and result.get("processQueryKey"):
                logger.info(f"[DingTalk] Single message sent successfully to {user_id}")
                return True
            else:
                logger.error(f"[DingTalk] Failed to send single message: {result}")
                return False
        except Exception as e:
            logger.error(f"[DingTalk] Error sending single message: {e}")
            return False

    def send_group_message(self, conversation_id: str, content: str, robot_code: str = None):
        """
        主动发送群消息。

        与单聊消息不同，群消息需要通过openConversationId指定目标群聊。
        同样需要robot_code来标识机器人身份。

        文档: https://open.dingtalk.com/document/orgapp/the-robot-sends-a-group-message

        Args:
            conversation_id: 会话ID (openConversationId)，标识目标群聊
            content: 消息内容
            robot_code: 机器人编码，默认使用 dingtalk_client_id

        Returns:
            bool: 发送成功返回True，失败返回False
        """
        access_token = self.get_access_token()
        if not access_token:
            logger.error("[DingTalk] Cannot send group message: no access token")
            return False

        # Validate robot_code
        if not robot_code:
            logger.error("[DingTalk] Cannot send group message: robot_code is required")
            return False

        url = "https://api.dingtalk.com/v1.0/robot/groupMessages/send"
        headers = {
            "x-acs-dingtalk-access-token": access_token,
            "Content-Type": "application/json"
        }
        data = {
            "msgParam": json.dumps({"content": content}),
            "msgKey": "sampleText",
            "openConversationId": conversation_id,
            "robotCode": robot_code
        }

        try:
            response = requests.post(url, headers=headers, json=data, timeout=10)
            result = response.json()

            if response.status_code == 200:
                logger.info(f"[DingTalk] Group message sent successfully to {conversation_id}")
                return True
            else:
                logger.error(f"[DingTalk] Failed to send group message: {result}")
                return False
        except Exception as e:
            logger.error(f"[DingTalk] Error sending group message: {e}")
            return False

    def upload_media(self, file_path: str, media_type: str = "image") -> str:
        """
        上传媒体文件到钉钉。

        该方法支持多种文件来源：
        1. 本地文件路径（直接上传）
        2. file://协议的本地路径（去除协议前缀后上传）
        3. HTTP/HTTPS URL（先下载到临时文件再上传）

        上传成功后返回media_id，用于后续发送图片、视频、文件等消息。

        Args:
            file_path: 本地文件路径或URL
            media_type: 媒体类型 (image, video, voice, file)，影响钉钉的存储策略

        Returns:
            str: media_id，用于发送媒体消息；上传失败返回None
        """
        access_token = self.get_access_token()
        if not access_token:
            logger.error("[DingTalk] Cannot upload media: no access token")
            return None

        # 处理 file:// URL —— 统一协议格式
        if file_path.startswith("file://"):
            file_path = file_path[7:]

        # 如果是 HTTP URL，先下载到本地临时文件
        # 原因：钉钉上传API要求提供本地文件，不支持直接从URL上传
        if file_path.startswith("http://") or file_path.startswith("https://"):
            try:
                import uuid
                response = requests.get(file_path, timeout=(5, 60))
                if response.status_code != 200:
                    logger.error(f"[DingTalk] Failed to download file from URL: {file_path}")
                    return None

                # 保存到临时文件
                file_name = os.path.basename(file_path) or f"media_{uuid.uuid4()}"
                workspace_root = expand_path(conf().get("agent_workspace", "~/cow"))
                tmp_dir = os.path.join(workspace_root, "tmp")
                os.makedirs(tmp_dir, exist_ok=True)
                temp_file = os.path.join(tmp_dir, file_name)

                with open(temp_file, "wb") as f:
                    f.write(response.content)

                file_path = temp_file
                logger.info(f"[DingTalk] Downloaded file to {file_path}")
            except Exception as e:
                logger.error(f"[DingTalk] Error downloading file: {e}")
                return None

        if not os.path.exists(file_path):
            logger.error(f"[DingTalk] File not found: {file_path}")
            return None

        # 上传到钉钉
        # 钉钉上传媒体文件 API: https://open.dingtalk.com/document/orgapp/upload-media-files
        url = "https://oapi.dingtalk.com/media/upload"
        params = {
            "access_token": access_token,
            "type": media_type
        }

        try:
            with open(file_path, "rb") as f:
                files = {"media": (os.path.basename(file_path), f)}
                response = requests.post(url, params=params, files=files, timeout=(5, 60))
                result = response.json()

                # errcode=0表示上传成功
                if result.get("errcode") == 0:
                    media_id = result.get("media_id")
                    logger.info(f"[DingTalk] Media uploaded successfully, media_id={media_id}")
                    return media_id
                else:
                    logger.error(f"[DingTalk] Failed to upload media: {result}")
                    return None
        except Exception as e:
            logger.error(f"[DingTalk] Error uploading media: {e}")
            return None

    def send_image_with_media_id(self, access_token: str, media_id: str, incoming_message, is_group: bool) -> bool:
        """
        发送图片消息（使用已上传的media_id）。

        该方法通过已上传到钉钉的media_id发送图片消息，根据会话类型（群聊/单聊）
        选择不同的API端点。

        注意：该方法是旧版发送方式，保留了与incoming_message的兼容性。
        新代码建议使用send_image_message方法。

        Args:
            access_token: 访问令牌
            media_id: 已上传的媒体文件ID
            incoming_message: 钉钉原始消息对象，用于获取robot_code和会话信息
            is_group: 是否为群聊

        Returns:
            bool: 是否发送成功
        """
        headers = {
            "x-acs-dingtalk-access-token": access_token,
            'Content-Type': 'application/json'
        }

        msg_param = {
            "photoURL": media_id  # 钉钉图片消息使用 photoURL 字段
        }

        body = {
            "robotCode": incoming_message.robot_code,
            "msgKey": "sampleImageMsg",
            "msgParam": json.dumps(msg_param),
        }

        if is_group:
            # 群聊：使用群消息发送API
            url = "https://api.dingtalk.com/v1.0/robot/groupMessages/send"
            body["openConversationId"] = incoming_message.conversation_id
        else:
            # 单聊：使用单聊消息发送API
            url = "https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend"
            body["userIds"] = [incoming_message.sender_staff_id]

        try:
            response = requests.post(url=url, headers=headers, json=body, timeout=10)
            result = response.json()

            logger.info(f"[DingTalk] Image send result: {response.text}")

            if response.status_code == 200:
                return True
            else:
                logger.error(f"[DingTalk] Send image error: {response.text}")
                return False
        except Exception as e:
            logger.error(f"[DingTalk] Send image exception: {e}")
            return False

    def send_image_message(self, receiver: str, media_id: str, is_group: bool, robot_code: str) -> bool:
        """
        发送图片消息（推荐使用的方法）。

        与send_image_with_media_id不同，该方法不依赖incoming_message对象，
        而是直接接收receiver和robot_code参数，更适合主动发送场景。

        Args:
            receiver: 接收者ID (单聊为user_id，群聊为conversation_id)
            media_id: 已上传的媒体文件ID
            is_group: 是否为群聊
            robot_code: 机器人编码

        Returns:
            bool: 是否发送成功
        """
        access_token = self.get_access_token()
        if not access_token:
            logger.error("[DingTalk] Cannot send image: no access token")
            return False

        if not robot_code:
            logger.error("[DingTalk] Cannot send image: robot_code is required")
            return False

        if is_group:
            # 发送群聊图片
            url = "https://api.dingtalk.com/v1.0/robot/groupMessages/send"
            headers = {
                "x-acs-dingtalk-access-token": access_token,
                "Content-Type": "application/json"
            }
            data = {
                "msgParam": json.dumps({"mediaId": media_id}),
                "msgKey": "sampleImageMsg",
                "openConversationId": receiver,
                "robotCode": robot_code
            }
        else:
            # 发送单聊图片
            url = "https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend"
            headers = {
                "x-acs-dingtalk-access-token": access_token,
                "Content-Type": "application/json"
            }
            data = {
                "msgParam": json.dumps({"mediaId": media_id}),
                "msgKey": "sampleImageMsg",
                "userIds": [receiver],
                "robotCode": robot_code
            }

        try:
            response = requests.post(url, headers=headers, json=data, timeout=10)
            result = response.json()

            if response.status_code == 200:
                logger.info(f"[DingTalk] Image message sent successfully")
                return True
            else:
                logger.error(f"[DingTalk] Failed to send image message: {result}")
                return False
        except Exception as e:
            logger.error(f"[DingTalk] Error sending image message: {e}")
            return False

    def get_image_download_url(self, download_code: str) -> str:
        """
        获取图片下载地址。

        该方法不直接返回HTTP URL，而是返回一个特殊的钉钉协议URL：
        dingtalk://download/{robot_code}:{download_code}

        这样设计的原因：钉钉图片下载需要先获取access_token，再调用下载API，
        这个过程比较复杂，因此将实际的下载逻辑延迟到download_image_file函数中处理。
        该函数能够识别并处理这种特殊的协议格式。

        Returns:
            str: 特殊格式的下载URL，包含robot_code和download_code；
                 获取失败返回None
        """
        # 获取 robot_code —— 需要从之前收到的消息中缓存
        if not hasattr(self, '_robot_code_cache'):
            self._robot_code_cache = None

        robot_code = self._robot_code_cache

        if not robot_code:
            logger.error("[DingTalk] robot_code not available for image download")
            return None

        # 返回一个特殊的 URL，包含 robot_code 和 download_code
        logger.info(f"[DingTalk] Successfully got image download URL for code: {download_code}")
        return f"dingtalk://download/{robot_code}:{download_code}"

    async def process(self, callback: dingtalk_stream.CallbackMessage):
        """
        钉钉Stream消息回调处理入口。

        当钉钉服务器通过Stream推送消息时，SDK会调用此方法。
        主要完成：
        1. 解析回调数据为ChatbotMessage对象
        2. 缓存robot_code用于后续图片下载和主动发送
        3. 过滤离线积压的过期消息
        4. 根据消息类型分发到单聊或群聊处理方法

        Args:
            callback: 钉钉Stream回调消息对象

        Returns:
            tuple: (AckMessage状态, 状态描述)，始终返回STATUS_OK避免SDK重试
        """
        try:
            incoming_message = dingtalk_stream.ChatbotMessage.from_dict(callback.data)

            # 缓存 robot_code，用于后续图片下载
            # robot_code标识了消息来源的机器人，在主动发送消息时必须提供
            if hasattr(incoming_message, 'robot_code'):
                self._robot_code_cache = incoming_message.robot_code

            # Filter out stale messages from before channel startup (offline backlog)
            # 过滤离线积压消息：如果消息创建时间距离当前超过60秒，
            # 说明是机器人离线期间的消息，应当跳过避免重复处理
            create_at = getattr(incoming_message, 'create_at', None)
            if create_at:
                msg_age_s = time.time() - int(create_at) / 1000  # 钉钉时间戳为毫秒，需转换为秒
                if msg_age_s > 60:
                    logger.warning(f"[DingTalk] stale msg filtered (age={msg_age_s:.0f}s), "
                                   f"msg_id={getattr(incoming_message, 'message_id', 'N/A')}")
                    return AckMessage.STATUS_OK, 'OK'

            # 创建DingTalkMessage对象，传入self作为图片下载处理器
            image_download_handler = self
            dingtalk_msg = DingTalkMessage(incoming_message, image_download_handler)

            if dingtalk_msg.is_group:
                self.handle_group(dingtalk_msg)
            else:
                self.handle_single(dingtalk_msg)
            # 始终返回STATUS_OK，即使处理出错也不让SDK重试
            # 因为重试会导致消息重复处理
            return AckMessage.STATUS_OK, 'OK'
        except Exception as e:
            logger.error(f"[DingTalk] process error: {e}", exc_info=True)
            return AckMessage.STATUS_SYSTEM_EXCEPTION, 'ERROR'

    @time_checker
    @_check
    def handle_single(self, cmsg: DingTalkMessage):
        """
        处理单聊消息。

        装饰器说明：
        - @time_checker: 检查是否在允许的时间段内处理消息
        - @_check: 消息去重和历史消息过滤

        处理流程：
        1. 记录消息日志
        2. 如果是图片消息，缓存图片路径并等待用户提问（图片+文本的联合理解模式）
        3. 如果是文本消息，检查是否有之前缓存的图片，将图片引用附加到文本中
        4. 构建上下文并提交到消息处理队列

        为什么图片要缓存：在多模态场景下，用户可能先发送图片，再发送文字提问。
        缓存机制允许将图片和后续的文本消息关联起来，实现"看图回答"的能力。

        Args:
            cmsg: 钉钉消息对象，包含消息内容和元数据
        """
        # 处理单聊消息
        if cmsg.ctype == ContextType.VOICE:
            logger.debug("[DingTalk]receive voice msg: {}".format(cmsg.content))
        elif cmsg.ctype == ContextType.IMAGE:
            logger.debug("[DingTalk]receive image msg: {}".format(cmsg.content))
        elif cmsg.ctype == ContextType.IMAGE_CREATE:
            logger.debug("[DingTalk]receive image create msg: {}".format(cmsg.content))
        elif cmsg.ctype == ContextType.PATPAT:
            logger.debug("[DingTalk]receive patpat msg: {}".format(cmsg.content))
        elif cmsg.ctype == ContextType.TEXT:
            logger.debug("[DingTalk]receive text msg: {}".format(cmsg.content))
        else:
            logger.debug("[DingTalk]receive other msg: {}".format(cmsg.content))

        # 处理文件缓存逻辑
        from channel.file_cache import get_file_cache
        file_cache = get_file_cache()

        # 单聊的 session_id 就是 sender_id
        # 单聊场景下，每个用户有独立的会话上下文
        session_id = cmsg.from_user_id

        # 如果是单张图片消息，缓存起来
        # 图片不直接处理，等待用户后续的文本提问后再一起处理
        if cmsg.ctype == ContextType.IMAGE:
            if hasattr(cmsg, 'image_path') and cmsg.image_path:
                file_cache.add(session_id, cmsg.image_path, file_type='image')
                logger.info(f"[DingTalk] Image cached for session {session_id}, waiting for user query...")
            # 单张图片不直接处理，等待用户提问
            return

        # 如果是文本消息，检查是否有缓存的文件
        # 将之前缓存的图片引用附加到当前文本消息中，实现多模态理解
        if cmsg.ctype == ContextType.TEXT:
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

                cmsg.content = cmsg.content + "\n" + "\n".join(file_refs)
                logger.info(f"[DingTalk] Attached {len(cached_files)} cached file(s) to user query")
                # 清除缓存 —— 文件引用已附加，无需再保留
                file_cache.clear(session_id)

        context = self._compose_context(cmsg.ctype, cmsg.content, isgroup=False, msg=cmsg)
        if context:
            self.produce(context)


    @time_checker
    @_check
    def handle_group(self, cmsg: DingTalkMessage):
        """
        处理群聊消息。

        与handle_single类似，但有以下区别：
        1. session_id的确定方式不同：群聊可以选择共享会话或按用户隔离
        2. 构建上下文时设置no_need_at=True，因为群聊消息已经过@过滤
        3. 群聊中所有消息默认都会被处理（已在__init__中配置ALL_GROUP白名单）

        session_id策略：
        - group_shared_session=True（默认）：群内所有人共享一个会话上下文，
          适合团队协作场景，群内的对话历史对所有人可见
        - group_shared_session=False：每个用户在群内有独立的会话上下文，
          适合需要个性化回复的场景

        Args:
            cmsg: 钉钉消息对象
        """
        # 处理群聊消息
        if cmsg.ctype == ContextType.VOICE:
            logger.debug("[DingTalk]receive voice msg: {}".format(cmsg.content))
        elif cmsg.ctype == ContextType.IMAGE:
            logger.debug("[DingTalk]receive image msg: {}".format(cmsg.content))
        elif cmsg.ctype == ContextType.IMAGE_CREATE:
            logger.debug("[DingTalk]receive image create msg: {}".format(cmsg.content))
        elif cmsg.ctype == ContextType.PATPAT:
            logger.debug("[DingTalk]receive patpat msg: {}".format(cmsg.content))
        elif cmsg.ctype == ContextType.TEXT:
            logger.debug("[DingTalk]receive text msg: {}".format(cmsg.content))
        else:
            logger.debug("[DingTalk]receive other msg: {}".format(cmsg.content))

        # 处理文件缓存逻辑
        from channel.file_cache import get_file_cache
        file_cache = get_file_cache()

        # 群聊的 session_id —— 根据配置决定是否共享会话
        if conf().get("group_shared_session", True):
            session_id = cmsg.other_user_id  # conversation_id，群内所有人共享
        else:
            session_id = cmsg.from_user_id + "_" + cmsg.other_user_id  # 每人独立会话

        # 如果是单张图片消息，缓存起来
        if cmsg.ctype == ContextType.IMAGE:
            if hasattr(cmsg, 'image_path') and cmsg.image_path:
                file_cache.add(session_id, cmsg.image_path, file_type='image')
                logger.info(f"[DingTalk] Image cached for session {session_id}, waiting for user query...")
            # 单张图片不直接处理，等待用户提问
            return

        # 如果是文本消息，检查是否有缓存的文件
        if cmsg.ctype == ContextType.TEXT:
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

                cmsg.content = cmsg.content + "\n" + "\n".join(file_refs)
                logger.info(f"[DingTalk] Attached {len(cached_files)} cached file(s) to user query")
                # 清除缓存
                file_cache.clear(session_id)

        context = self._compose_context(cmsg.ctype, cmsg.content, isgroup=True, msg=cmsg)
        # no_need_at=True：群聊消息不需要@前缀即可触发处理
        # 因为到达这里的消息已经经过了@过滤
        context['no_need_at'] = True
        if context:
            self.produce(context)


    def send(self, reply: Reply, context: Context):
        """
        发送回复消息的统一入口。

        根据reply类型和上下文信息，选择合适的发送方式：
        1. 定时任务场景（无原始消息）：使用主动发送API
        2. 图片消息(IMAGE_URL)：上传图片后通过媒体API发送
        3. 文件消息(FILE)：区分视频和其他文件类型，分别发送
        4. 文本消息(TEXT)：支持普通文本和AI卡片两种模式

        Args:
            reply: 回复对象，包含回复类型和内容
            context: 上下文对象，包含消息来源和会话信息
        """
        logger.debug(f"[DingTalk] send() called with reply.type={reply.type}, content_length={len(str(reply.content))}")
        receiver = context["receiver"]

        # Check if msg exists (for scheduled tasks, msg might be None)
        msg = context.kwargs.get('msg')
        if msg is None:
            # 定时任务场景：使用主动发送 API
            # 定时任务没有原始消息上下文，需要使用robot_code主动推送
            is_group = context.get("isgroup", False)
            logger.info(f"[DingTalk] Sending scheduled task message to {receiver} (is_group={is_group})")

            # 使用缓存的 robot_code 或配置的值
            robot_code = self._robot_code or conf().get("dingtalk_robot_code")
            logger.info(f"[DingTalk] Using robot_code: {robot_code}, cached: {self._robot_code}, config: {conf().get('dingtalk_robot_code')}")

            if not robot_code:
                logger.error(f"[DingTalk] Cannot send scheduled task: robot_code not available. Please send at least one message to the bot first, or configure dingtalk_robot_code in config.json")
                return

            # 根据是否群聊选择不同的 API
            if is_group:
                success = self.send_group_message(receiver, reply.content, robot_code)
            else:
                # 单聊场景：尝试从 context 中获取 dingtalk_sender_staff_id
                sender_staff_id = context.get("dingtalk_sender_staff_id")
                if not sender_staff_id:
                    logger.error(f"[DingTalk] Cannot send single chat scheduled message: sender_staff_id not available in context")
                    return

                logger.info(f"[DingTalk] Sending single message to staff_id: {sender_staff_id}")
                success = self.send_single_message(sender_staff_id, reply.content, robot_code)

            if not success:
                logger.error(f"[DingTalk] Failed to send scheduled task message")
            return

        # 从正常消息中提取并缓存 robot_code
        # 每次收到消息时更新缓存，确保robot_code始终是最新的
        if hasattr(msg, 'robot_code'):
            robot_code = msg.robot_code
            if robot_code and robot_code != self._robot_code:
                self._robot_code = robot_code
                logger.debug(f"[DingTalk] Cached robot_code: {robot_code}")

        isgroup = msg.is_group
        incoming_message = msg.incoming_message
        robot_code = self._robot_code or conf().get("dingtalk_robot_code")

        # 处理图片和视频发送
        if reply.type == ReplyType.IMAGE_URL:
            logger.info(f"[DingTalk] Sending image: {reply.content}")

            # 如果有附加的文本内容，先发送文本
            # 某些场景下图片和文本需要一起发送，先发文本让用户看到上下文
            if hasattr(reply, 'text_content') and reply.text_content:
                self.reply_text(reply.text_content, incoming_message)
                import time
                time.sleep(0.3)  # 短暂延迟，确保文本先到达，避免顺序错乱

            # 上传图片到钉钉获取media_id，然后发送
            media_id = self.upload_media(reply.content, media_type="image")
            if media_id:
                # 使用主动发送 API 发送图片
                access_token = self.get_access_token()
                if access_token:
                    success = self.send_image_with_media_id(
                        access_token,
                        media_id,
                        incoming_message,
                        isgroup
                    )
                    if not success:
                        logger.error("[DingTalk] Failed to send image message")
                        self.reply_text("抱歉，图片发送失败", incoming_message)
                else:
                    logger.error("[DingTalk] Cannot get access token")
                    self.reply_text("抱歉，图片发送失败（无法获取token）", incoming_message)
            else:
                logger.error("[DingTalk] Failed to upload image")
                self.reply_text("抱歉，图片上传失败", incoming_message)
            return

        elif reply.type == ReplyType.FILE:
            # 如果有附加的文本内容，先发送文本
            if hasattr(reply, 'text_content') and reply.text_content:
                self.reply_text(reply.text_content, incoming_message)
                import time
                time.sleep(0.3)  # 短暂延迟，确保文本先到达

            # 判断是否为视频文件 —— 根据文件扩展名区分视频和其他文件
            file_path = reply.content
            if file_path.startswith("file://"):
                file_path = file_path[7:]

            is_video = file_path.lower().endswith(('.mp4', '.avi', '.mov', '.wmv', '.flv'))

            access_token = self.get_access_token()
            if not access_token:
                logger.error("[DingTalk] Cannot get access token")
                self.reply_text("抱歉，文件发送失败（无法获取token）", incoming_message)
                return

            if is_video:
                # 视频文件处理：上传后发送视频消息
                logger.info(f"[DingTalk] Sending video: {reply.content}")
                media_id = self.upload_media(reply.content, media_type="video")
                if media_id:
                    # 发送视频消息
                    msg_param = {
                        "duration": "30",  # TODO: 获取实际视频时长
                        "videoMediaId": media_id,
                        "videoType": "mp4",
                        "height": "400",
                        "width": "600",
                    }
                    success = self._send_file_message(
                        access_token,
                        incoming_message,
                        "sampleVideo",
                        msg_param,
                        isgroup
                    )
                    if not success:
                        self.reply_text("抱歉，视频发送失败", incoming_message)
                else:
                    logger.error("[DingTalk] Failed to upload video")
                    self.reply_text("抱歉，视频上传失败", incoming_message)
            else:
                # 其他文件类型：上传后发送文件消息
                logger.info(f"[DingTalk] Sending file: {reply.content}")
                media_id = self.upload_media(reply.content, media_type="file")
                if media_id:
                    file_name = os.path.basename(file_path)
                    file_base, file_extension = os.path.splitext(file_name)
                    msg_param = {
                        "mediaId": media_id,
                        "fileName": file_name,
                        "fileType": file_extension[1:] if file_extension else "file"
                    }
                    success = self._send_file_message(
                        access_token,
                        incoming_message,
                        "sampleFile",
                        msg_param,
                        isgroup
                    )
                    if not success:
                        self.reply_text("抱歉，文件发送失败", incoming_message)
                else:
                    logger.error("[DingTalk] Failed to upload file")
                    self.reply_text("抱歉，文件上传失败", incoming_message)
            return

        # 处理文本消息
        elif reply.type == ReplyType.TEXT:
            logger.info(f"[DingTalk] Sending text message, length={len(reply.content)}")
            if conf().get("dingtalk_card_enabled"):
                # 启用AI卡片模式：将文本内容渲染为钉钉AI卡片，支持Markdown格式
                logger.info("[Dingtalk] sendMsg={}, receiver={}".format(reply, receiver))
                def reply_with_text():
                    """纯文本回复（回退方案）"""
                    self.reply_text(reply.content, incoming_message)
                def reply_with_at_text():
                    """群聊中的@提醒回复，提示用户查看AI卡片"""
                    self.reply_text("📢 您有一条新的消息，请查看。", incoming_message)
                def reply_with_ai_markdown():
                    """AI Markdown卡片回复，将回复内容渲染为交互式卡片"""
                    button_list, markdown_content = self.generate_button_markdown_content(context, reply)
                    self.reply_ai_markdown_button(incoming_message, markdown_content, button_list, "", "📌 内容由AI生成", "",[incoming_message.sender_staff_id])

                if reply.type in [ReplyType.IMAGE_URL, ReplyType.IMAGE, ReplyType.TEXT]:
                    if isgroup:
                        # 群聊中：发送AI卡片 + @提醒，确保用户注意到回复
                        reply_with_ai_markdown()
                        reply_with_at_text()
                    else:
                        # 单聊中：仅发送AI卡片
                        reply_with_ai_markdown()
                else:
                    # 暂不支持其它类型消息回复
                    reply_with_text()
            else:
                # 未启用AI卡片：使用普通文本回复
                self.reply_text(reply.content, incoming_message)
            return

    def _send_file_message(self, access_token: str, incoming_message, msg_key: str, msg_param: dict, is_group: bool) -> bool:
        """
        发送文件/视频消息的通用方法。

        该方法抽象了群聊和单聊的发送差异，根据is_group参数自动选择
        对应的API端点和消息体格式。

        为什么需要通用方法：群聊和单聊的发送API URL和参数格式不同，
        但消息体的核心结构（robotCode、msgKey、msgParam）相同，
        抽取通用方法避免代码重复。

        Args:
            access_token: 访问令牌
            incoming_message: 钉钉消息对象，用于获取robot_code和会话信息
            msg_key: 消息类型键 (sampleFile=文件, sampleVideo=视频, sampleAudio=音频)
            msg_param: 消息参数字典，不同类型的消息有不同的参数结构
            is_group: 是否为群聊

        Returns:
            bool: 是否发送成功
        """
        headers = {
            "x-acs-dingtalk-access-token": access_token,
            'Content-Type': 'application/json'
        }

        body = {
            "robotCode": incoming_message.robot_code,
            "msgKey": msg_key,
            "msgParam": json.dumps(msg_param),
        }

        if is_group:
            # 群聊：通过openConversationId指定目标群
            url = "https://api.dingtalk.com/v1.0/robot/groupMessages/send"
            body["openConversationId"] = incoming_message.conversation_id
        else:
            # 单聊：通过userIds指定目标用户
            url = "https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend"
            body["userIds"] = [incoming_message.sender_staff_id]

        try:
            response = requests.post(url=url, headers=headers, json=body, timeout=10)
            result = response.json()

            logger.info(f"[DingTalk] File send result: {response.text}")

            if response.status_code == 200:
                return True
            else:
                logger.error(f"[DingTalk] Send file error: {response.text}")
                return False
        except Exception as e:
            logger.error(f"[DingTalk] Send file exception: {e}")
            return False

    def generate_button_markdown_content(self, context, reply):
        """
        生成AI卡片的Markdown内容和按钮列表。

        当回复内容包含图片生成结果时，会添加"查看原图"按钮，
        并在Markdown中嵌入图片。纯文本回复则只包含文本内容。

        Args:
            context: 上下文对象，可能包含image_url和promptEn
            reply: 回复对象，包含回复文本内容

        Returns:
            tuple: (button_list, markdown_content)
                - button_list: 按钮列表，每个按钮包含text、url等属性
                - markdown_content: Markdown格式的卡片内容
        """
        image_url = context.kwargs.get("image_url")
        promptEn = context.kwargs.get("promptEn")
        reply_text = reply.content
        button_list = []
        # 默认卡片内容：纯文本回复
        markdown_content = f"""
{reply.content}
                                """
        if image_url is not None and promptEn is not None:
            # 如果是图片生成结果，添加查看原图按钮和图片展示
            button_list = [
                {"text": "查看原图", "url": image_url, "iosUrl": image_url, "color": "blue"}
            ]
            # 构建包含图片的Markdown内容
            markdown_content = f"""
{promptEn}

!["图片"]({image_url})

{reply_text}

                                """
        logger.debug(f"[Dingtalk] generate_button_markdown_content, button_list={button_list} , markdown_content={markdown_content}")

        return button_list, markdown_content
