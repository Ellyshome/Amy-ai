"""
企业微信自建应用消息封装模块 —— 将企业微信原始消息转换为统一的消息对象。

本模块实现了WechatComAppMessage类，继承自ChatMessage，用于将企业微信
通过回调推送的原始消息（wechatpy解析后的对象）转换为系统内部统一的消息格式。

支持的消息类型：
- text: 文本消息，直接使用文本内容
- voice: 语音消息，下载语音文件到临时目录，存储文件路径
- image: 图片消息，下载图片文件到临时目录，存储文件路径

对于语音和图片类型，由于需要从企业微信服务器下载媒体文件，
下载操作被封装为_prepare_fn函数，延迟到消息处理流程中执行，
避免在消息解析阶段阻塞。
"""
from wechatpy.enterprise import WeChatClient

from bridge.context import ContextType
from channel.chat_message import ChatMessage
from common.log import logger
from common.tmp_dir import TmpDir


class WechatComAppMessage(ChatMessage):
    """
    企业微信自建应用消息封装类。

    继承自ChatMessage，将企业微信的原始消息对象转换为系统内部
    统一的消息格式。解析消息类型、内容和发送者信息。

    对于媒体类型消息（语音、图片），不立即下载媒体文件，
    而是将下载操作封装为_prepare_fn，在ChatChannel的消息处理
    流程中调用，实现延迟下载。

    不支持的消息类型会抛出NotImplementedError异常，
    由上层捕获后返回"success"避免企业微信重试。
    """

    def __init__(self, msg, client: WeChatClient, is_group=False):
        """
        初始化企业微信消息对象。

        根据消息类型解析内容：
        - 文本消息：直接使用msg.content
        - 语音消息：设置文件路径并封装下载函数，ctype设为VOICE
        - 图片消息：设置文件路径并封装下载函数，ctype设为IMAGE
        - 其他类型：抛出NotImplementedError

        Args:
            msg: wechatpy解析后的消息对象，包含消息类型、内容、发送者等信息
            client: 企业微信API客户端，用于下载媒体文件
            is_group: 是否为群聊消息（企业微信自建应用当前不支持群聊，默认False）

        Raises:
            NotImplementedError: 遇到不支持的消息类型时抛出
        """
        super().__init__(msg)
        self.msg_id = msg.id
        self.create_time = msg.time
        self.is_group = is_group

        if msg.type == "text":
            # 文本消息：直接使用消息内容
            self.ctype = ContextType.TEXT
            self.content = msg.content
        elif msg.type == "voice":
            # 语音消息：需要从企业微信服务器下载语音文件
            # content存储临时文件的本地路径，使用media_id作为文件名前缀确保唯一性
            self.ctype = ContextType.VOICE
            self.content = TmpDir().path() + msg.media_id + "." + msg.format  # content直接存临时目录路径

            def download_voice():
                """
                延迟下载语音文件的闭包函数。

                从企业微信服务器下载语音文件到本地临时目录。
                下载成功（HTTP 200）时写入文件，失败时记录日志。
                此函数将在ChatChannel的消息处理流程中被调用。
                """
                # 如果响应状态码是200，则将响应内容写入本地文件
                response = client.media.download(msg.media_id)
                if response.status_code == 200:
                    with open(self.content, "wb") as f:
                        f.write(response.content)
                else:
                    logger.info(f"[wechatcom] Failed to download voice file, {response.content}")

            # 将下载函数赋值给_prepare_fn，ChatChannel会在处理消息时调用
            self._prepare_fn = download_voice
        elif msg.type == "image":
            # 图片消息：需要从企业微信服务器下载图片文件
            # content存储临时文件的本地路径，固定使用.png扩展名
            self.ctype = ContextType.IMAGE
            self.content = TmpDir().path() + msg.media_id + ".png"  # content直接存临时目录路径

            def download_image():
                """
                延迟下载图片文件的闭包函数。

                从企业微信服务器下载图片文件到本地临时目录。
                下载成功时写入文件，失败时记录日志。
                此函数将在ChatChannel的消息处理流程中被调用。
                """
                # 如果响应状态码是200，则将响应内容写入本地文件
                response = client.media.download(msg.media_id)
                if response.status_code == 200:
                    with open(self.content, "wb") as f:
                        f.write(response.content)
                else:
                    logger.info(f"[wechatcom] Failed to download image file, {response.content}")

            # 将下载函数赋值给_prepare_fn，ChatChannel会在处理消息时调用
            self._prepare_fn = download_image
        else:
            # 不支持的消息类型（如视频、文件、位置等），抛出异常
            # 上层会捕获此异常并返回"success"，避免企业微信重试
            raise NotImplementedError("Unsupported message type: Type:{} ".format(msg.type))

        # 设置消息的发送者和接收者信息
        # source: 消息发送者的UserID
        # target: 消息接收者的UserID（即应用本身）
        self.from_user_id = msg.source
        self.to_user_id = msg.target
        # other_user_id设置为发送者ID，用于消息路由
        self.other_user_id = msg.source
