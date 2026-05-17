# -*- coding: utf-8 -*-#
"""
微信公众号消息封装模块 —— 将微信公众号原始消息转换为统一的消息对象。

本模块实现了WeChatMPMessage类，继承自ChatMessage，用于将微信公众号
通过回调推送的原始消息（wechatpy解析后的对象）转换为系统内部统一的消息格式。

支持的消息类型：
- text: 文本消息，直接使用文本内容
- voice: 语音消息，有两种处理方式：
  1. 如果微信已识别语音内容（msg.recognition不为空），则作为文本消息处理
  2. 如果微信未识别，则下载语音文件到临时目录，存储文件路径
- image: 图片消息，下载图片文件到临时目录，存储文件路径

对于媒体类型消息，下载操作被封装为_prepare_fn函数，延迟到消息处理流程中执行，
避免在消息解析阶段阻塞HTTP请求（微信公众号要求5秒内返回响应）。
"""
from bridge.context import ContextType
from channel.chat_message import ChatMessage
from common.log import logger
from common.tmp_dir import TmpDir


class WeChatMPMessage(ChatMessage):
    """
    微信公众号消息封装类。

    继承自ChatMessage，将微信公众号的原始消息对象转换为系统内部
    统一的消息格式。解析消息类型、内容和发送者信息。

    语音消息的特殊处理：
    微信公众号可以对语音消息进行语音识别（需在公众号后台开启），
    如果识别成功（msg.recognition不为空），则将识别结果作为文本消息处理，
    避免需要额外下载和转换语音文件。
    如果识别失败，则下载语音文件，由语音识别模块处理。

    不支持的消息类型会抛出NotImplementedError异常。
    """

    def __init__(self, msg, client=None):
        """
        初始化微信公众号消息对象。

        根据消息类型解析内容：
        - 文本消息：直接使用msg.content
        - 语音消息：优先使用微信的语音识别结果（msg.recognition），
          如果没有识别结果则下载语音文件
        - 图片消息：下载图片文件到临时目录
        - 其他类型：抛出NotImplementedError

        Args:
            msg: wechatpy解析后的消息对象，包含消息类型、内容、发送者等信息
            client: 微信公众号API客户端，用于下载媒体文件
                被动回复模式下可为None（不在此处下载）
                主动回复模式下需要传入client以下载媒体文件

        Raises:
            NotImplementedError: 遇到不支持的消息类型时抛出
        """
        super().__init__(msg)
        self.msg_id = msg.id
        self.create_time = msg.time
        self.is_group = False  # 公众号消息不支持群聊

        if msg.type == "text":
            # 文本消息：直接使用消息内容
            self.ctype = ContextType.TEXT
            self.content = msg.content
        elif msg.type == "voice":
            # 语音消息：优先使用微信的语音识别结果
            if msg.recognition == None:
                # 没有语音识别结果，需要下载语音文件进行本地识别
                # content存储临时文件的本地路径，使用media_id作为文件名前缀确保唯一性
                self.ctype = ContextType.VOICE
                self.content = TmpDir().path() + msg.media_id + "." + msg.format  # content直接存临时目录路径

                def download_voice():
                    """
                    延迟下载语音文件的闭包函数。

                    从微信服务器下载语音文件到本地临时目录。
                    下载成功时写入文件，失败时记录日志。
                    此函数将在ChatChannel的消息处理流程中被调用。
                    """
                    # 如果响应状态码是200，则将响应内容写入本地文件
                    response = client.media.download(msg.media_id)
                    if response.status_code == 200:
                        with open(self.content, "wb") as f:
                            f.write(response.content)
                    else:
                        logger.info(f"[wechatmp] Failed to download voice file, {response.content}")

                # 将下载函数赋值给_prepare_fn，ChatChannel会在处理消息时调用
                self._prepare_fn = download_voice
            else:
                # 有语音识别结果，直接使用识别文本作为消息内容
                # 这样可以避免下载语音文件和本地语音识别的开销
                self.ctype = ContextType.TEXT
                self.content = msg.recognition
        elif msg.type == "image":
            # 图片消息：需要从微信服务器下载图片文件
            # content存储临时文件的本地路径，固定使用.png扩展名
            self.ctype = ContextType.IMAGE
            self.content = TmpDir().path() + msg.media_id + ".png"  # content直接存临时目录路径

            def download_image():
                """
                延迟下载图片文件的闭包函数。

                从微信服务器下载图片文件到本地临时目录。
                下载成功时写入文件，失败时记录日志。
                此函数将在ChatChannel的消息处理流程中被调用。
                """
                # 如果响应状态码是200，则将响应内容写入本地文件
                response = client.media.download(msg.media_id)
                if response.status_code == 200:
                    with open(self.content, "wb") as f:
                        f.write(response.content)
                else:
                    logger.info(f"[wechatmp] Failed to download image file, {response.content}")

            # 将下载函数赋值给_prepare_fn，ChatChannel会在处理消息时调用
            self._prepare_fn = download_image
        else:
            # 不支持的消息类型（如视频、位置、链接等），抛出异常
            # 上层会捕获此异常并返回"success"或进行其他处理
            raise NotImplementedError("Unsupported message type: Type:{} ".format(msg.type))

        # 设置消息的发送者和接收者信息
        # source: 消息发送者的OpenID
        # target: 消息接收者的公众号原始ID
        self.from_user_id = msg.source
        self.to_user_id = msg.target
        # other_user_id设置为发送者ID，用于消息路由
        self.other_user_id = msg.source
