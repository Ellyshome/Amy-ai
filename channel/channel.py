"""
Message sending channel abstract class
消息发送通道的抽象基类，定义了所有通道必须实现的接口和通用行为。

该类是整个消息通道体系的核心抽象，所有具体的通道实现（如微信、飞书、钉钉、终端等）
都必须继承此类并实现其抽象方法。通道负责：
1. 启动和停止通道的生命周期管理
2. 接收用户消息并传递给处理链路
3. 将处理后的回复发送回用户
4. 支持普通模式和 Agent 模式两种回复构建方式
"""

from bridge.bridge import Bridge
from bridge.context import Context
from bridge.reply import *
from common.log import logger
from config import conf


class Channel(object):
    """
    消息通道的抽象基类。

    所有具体的通道实现（微信、飞书、钉钉、Web、终端等）都必须继承此类，
    并实现 startup()、handle_text()、send() 等抽象方法。

    类属性:
        channel_type: 通道类型标识符，由子类或在工厂创建时赋值，
                      用于在上下文中标识当前通道类型，影响插件和 Agent 的行为
        NOT_SUPPORT_REPLYTYPE: 该通道不支持的回复类型列表，默认不支持语音和图片，
                               因为这些类型需要特殊处理（如文件上传），
                               子类可覆盖此列表以声明自身不支持的类型
    """
    channel_type = ""
    NOT_SUPPORT_REPLYTYPE = [ReplyType.VOICE, ReplyType.IMAGE]

    def __init__(self):
        """
        初始化通道实例。

        创建线程事件用于启动同步机制：外部调用者可通过 wait_startup() 阻塞等待
        通道启动完成，而通道在启动成功或失败后分别调用 report_startup_success()
        或 report_startup_error() 来通知等待方。这种设计确保了在多通道并行启动时，
        管理器能可靠地知道每个通道的启动状态。

        Attributes:
            _startup_event: 线程事件，用于启动结果的同步通知
            _startup_error: 启动失败时的错误信息，为 None 表示启动成功
            cloud_mode: 是否运行在云客户端模式下，由 ChannelManager 在云端部署时设置为 True，
                        通道可据此调整行为（如消息路由、认证方式等）
        """
        import threading
        self._startup_event = threading.Event()
        self._startup_error = None
        self.cloud_mode = False  # set to True by ChannelManager when running with cloud client
        # 云模式标志，由 ChannelManager 在与云客户端协同运行时设为 True
        # 云模式下通道的消息收发逻辑可能与本地模式不同

    def startup(self):
        """
        init channel
        初始化并启动通道。

        这是一个抽象方法，子类必须实现。启动过程通常包括：
        - 建立与消息平台的连接（如 WebSocket、HTTP 长轮询等）
        - 注册消息回调处理器
        - 启动消息监听循环

        启动完成后应调用 report_startup_success() 或 report_startup_error()
        来通知等待启动结果的调用方。

        Raises:
            NotImplementedError: 子类未实现此方法时抛出
        """
        raise NotImplementedError

    def report_startup_success(self):
        """
        报告通道启动成功。

        将错误信息置为 None 并设置启动事件，唤醒所有在 wait_startup() 上
        阻塞等待的线程，通知它们通道已就绪。
        """
        self._startup_error = None
        self._startup_event.set()

    def report_startup_error(self, error: str):
        """
        报告通道启动失败。

        记录错误信息并设置启动事件，唤醒所有在 wait_startup() 上阻塞等待的线程，
        通知它们通道启动失败。调用方可通过返回值获取具体的错误信息。

        Args:
            error: 启动失败的错误描述信息
        """
        self._startup_error = error
        self._startup_event.set()

    def wait_startup(self, timeout: float = 3) -> (bool, str):
        """
        Wait for channel startup result.
        Returns (success: bool, error_msg: str).
        等待通道启动结果。

        阻塞当前线程，直到通道启动完成（成功或失败）或超时。
        此方法通常由 ChannelManager 调用，用于在启动所有通道后
        检查每个通道是否正常就绪。

        注意：超时未收到结果时默认返回成功，因为某些通道的启动
        可能是异步的，不一定会在超时前发出通知。

        Args:
            timeout: 等待超时时间（秒），默认 3 秒

        Returns:
            元组 (success, error_msg):
                - success=True, error_msg="": 启动成功或超时（保守认为成功）
                - success=False, error_msg=具体错误: 启动失败，附带错误信息
        """
        ready = self._startup_event.wait(timeout=timeout)
        if not ready:
            # 超时未收到启动结果，保守地认为启动成功
            # 这是因为某些通道可能采用异步启动方式，不会主动通知
            return True, ""
        if self._startup_error:
            # 收到启动失败通知
            return False, self._startup_error
        # 收到启动成功通知
        return True, ""

    def stop(self):
        """
        stop channel gracefully, called before restart
        优雅地停止通道，通常在重启或关闭时调用。

        子类可覆盖此方法以实现通道的优雅关闭逻辑，例如：
        - 断开与消息平台的连接
        - 停止消息监听循环
        - 清理资源（线程、文件句柄等）

        默认实现为空操作，表示无需特殊清理。
        """
        pass

    def handle_text(self, msg):
        """
        process received msg
        处理接收到的用户消息。

        这是消息处理的核心入口，子类必须实现。当通道从消息平台接收到
        用户消息后，应调用此方法将消息传递到处理链路中。
        典型的处理流程为：
        1. 解析消息内容，构建 Context 对象
        2. 触发插件事件（ON_HANDLE_CONTEXT 等）
        3. 调用 build_reply_content() 获取回复
        4. 调用 send() 发送回复

        Args:
            msg: 消息对象，具体类型由子类定义（通常为 ChatMessage）

        Raises:
            NotImplementedError: 子类未实现此方法时抛出
        """
        raise NotImplementedError

    # 统一的发送函数，每个Channel自行实现，根据reply的type字段发送不同类型的消息
    def send(self, reply: Reply, context: Context):
        """
        send message to user
        向用户发送消息。

        这是消息发送的统一接口，子类必须实现。根据 Reply 的 type 字段
        决定发送方式（文本、图片、文件等）。如果遇到不支持的回复类型
        （参见 NOT_SUPPORT_REPLYTYPE），应做降级处理（如转为文本描述）。

        :param msg: message content
        :param receiver: receiver channel account
        :return:
        Args:
            reply: 回复对象，包含回复类型和内容
            context: 上下文对象，包含会话信息和通道元数据

        Raises:
            NotImplementedError: 子类未实现此方法时抛出
        """
        raise NotImplementedError

    def build_reply_content(self, query, context: Context = None) -> Reply:
        """
        Build reply content, using agent if enabled in config
        构建回复内容，根据配置选择 Agent 模式或普通模式。

        这是回复构建的核心方法，封装了两种处理模式的分支逻辑：
        1. Agent 模式（config 中 agent=True）：使用 AgentBridge 执行多步推理
           和工具调用循环，支持流式事件输出，适合复杂任务处理
        2. 普通模式：使用 Bridge 单例直接调用模型获取回复，适合简单问答

        Agent 模式失败时会自动降级到普通模式，确保系统可用性。
        此方法由 ChatChannel 等基类的 handle_text() 调用。

        Args:
            query: 用户输入的查询文本
            context: 上下文对象，可选。包含会话信息、通道类型、事件回调等

        Returns:
            Reply: 构建好的回复对象
        """
        # Check if agent mode is enabled
        # 检查配置是否启用了 Agent 模式
        use_agent = conf().get("agent", False)

        if use_agent:
            try:
                logger.info("[Channel] Using agent mode")
                # Agent 模式：支持多步推理和工具调用

                # Add channel_type to context if not present
                # 将通道类型注入上下文，以便 Agent 层能识别消息来源的通道类型
                # 这会影响 Agent 的行为，例如不同通道可能有不同的消息格式要求
                if context and "channel_type" not in context:
                    context["channel_type"] = self.channel_type

                # Read on_event callback injected by the channel (e.g. web SSE)
                # 获取通道注入的事件回调函数（如 Web 通道的 SSE 流式推送回调）
                # 通过此回调，Agent 执行过程中的中间事件可以实时推送给用户
                on_event = context.get("on_event") if context else None

                # Use agent bridge to handle the query
                # 使用 AgentBridge 处理查询，支持多步工具调用循环
                # clear_history=False 表示不在此处清除对话历史，历史管理由上层控制
                return Bridge().fetch_agent_reply(
                    query=query,
                    context=context,
                    on_event=on_event,
                    clear_history=False
                )
            except Exception as e:
                logger.error(f"[Channel] Agent mode failed, fallback to normal mode: {e}")
                # Fallback to normal mode if agent fails
                # Agent 模式异常时自动降级到普通模式，确保用户仍能获得回复
                # 这是系统容错设计的关键环节
                return Bridge().fetch_reply_content(query, context)
        else:
            # Normal mode
            # 普通模式：直接调用模型获取单轮回复，不涉及工具调用
            return Bridge().fetch_reply_content(query, context)

    def build_voice_to_text(self, voice_file) -> Reply:
        """
        将语音文件转换为文本。

        通过 Bridge 调用语音识别（ASR）服务，将语音消息转为文字，
        以便后续作为文本输入进行处理。Bridge 内部会根据配置选择
        对应的 ASR 提供商（如 OpenAI Whisper、Azure Speech 等）。

        Args:
            voice_file: 语音文件的本地路径

        Returns:
            Reply: 包含识别文本的回复对象
        """
        return Bridge().fetch_voice_to_text(voice_file)

    def build_text_to_voice(self, text) -> Reply:
        """
        将文本转换为语音。

        通过 Bridge 调用语音合成（TTS）服务，将文字消息转为语音，
        以便通过支持语音的通道发送。Bridge 内部会根据配置选择
        对应的 TTS 提供商（如 OpenAI TTS、Azure TTS 等）。

        Args:
            text: 需要合成为语音的文本内容

        Returns:
            Reply: 包含语音文件路径的回复对象
        """
        return Bridge().fetch_text_to_voice(text)
