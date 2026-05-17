# -*- coding: utf-8 -*-
"""
微信公众号渠道模块 —— 实现微信公众号（订阅号/服务号）的消息收发。

本模块实现了WechatMPChannel类，作为ChatChannel的子类，支持两种回复模式：

1. 被动回复模式（passive_reply=True）：
   - 适用于个人订阅号，必须在5秒内通过HTTP响应返回回复
   - 利用微信重试机制，将回复缓存后在下一次请求中返回
   - 使用永久素材API上传媒体文件
   - 支持文本、语音、图片、视频回复

2. 主动回复模式（passive_reply=False）：
   - 适用于认证服务号，通过客服消息API主动推送回复
   - 不受5秒回复时间限制
   - 使用临时素材API上传媒体文件
   - 支持文本、语音、图片、视频回复

两种模式的区别主要在于：
- 被动回复使用material.add（永久素材）上传，需要手动删除避免超限
- 主动回复使用media.upload（临时素材）上传，3天后自动过期
- 被动回复需要缓存机制配合微信重试，主动回复直接推送
"""
import asyncio
import imghdr
import io
import os
import threading
import time

import requests
import web
from wechatpy.crypto import WeChatCrypto
from wechatpy.exceptions import WeChatClientException
from collections import defaultdict

from bridge.context import *
from bridge.reply import *
from channel.chat_channel import ChatChannel
from channel.wechatmp.common import *
from channel.wechatmp.wechatmp_client import WechatMPClient
from common.log import logger
from common.singleton import singleton
from common.utils import split_string_by_utf8_length, remove_markdown_symbol
from config import conf

try:
    from voice.audio_convert import any_to_mp3, split_audio
except ImportError as e:
    logger.debug("import voice.audio_convert failed, voice features will not be supported: {}".format(e))

# If using SSL, uncomment the following lines, and modify the certificate path.
# from cheroot.server import HTTPServer
# from cheroot.ssl.builtin import BuiltinSSLAdapter
# HTTPServer.ssl_adapter = BuiltinSSLAdapter(
#         certificate='/ssl/cert.pem',
#         private_key='/ssl/cert.key')


@singleton
class WechatMPChannel(ChatChannel):
    """
    微信公众号渠道类，使用单例模式确保全局只有一个实例。

    继承自ChatChannel，提供微信公众号的消息收发功能。
    支持被动回复和主动回复两种模式，通过构造参数passive_reply切换。

    被动回复模式下的数据结构：
    - cache_dict: 按用户ID缓存回复内容的字典，值是(reply_type, content)元组列表
    - running: 记录当前正在处理消息的用户ID集合
    - request_cnt: 按message_id统计微信重试次数的字典
    - delete_media_loop: 异步事件循环，用于延迟删除永久素材

    配置项（config.json）：
    - wechatmp_app_id: 公众号AppID
    - wechatmp_app_secret: 公众号AppSecret
    - wechatmp_token: 回调Token
    - wechatmp_aes_key: 回调AES Key（可选，启用消息加密时需要）
    - wechatmp_port: 服务监听端口（默认8080）
    """

    def __init__(self, passive_reply=True):
        """
        初始化微信公众号渠道。

        Args:
            passive_reply: 是否使用被动回复模式，默认True
                - True: 被动回复模式（订阅号）
                - False: 主动回复模式（服务号，使用客服消息API）
        """
        super().__init__()
        self.passive_reply = passive_reply
        self.NOT_SUPPORT_REPLYTYPE = []
        self._http_server = None
        # 从配置中读取微信公众号参数
        appid = conf().get("wechatmp_app_id")
        secret = conf().get("wechatmp_app_secret")
        token = conf().get("wechatmp_token")
        aes_key = conf().get("wechatmp_aes_key")
        # 创建API客户端
        self.client = WechatMPClient(appid, secret)
        # 创建消息加解密器（仅在配置了aes_key时启用）
        self.crypto = None
        if aes_key:
            self.crypto = WeChatCrypto(token, aes_key, appid)
        if self.passive_reply:
            # Cache the reply to the user's first message
            # 缓存用户回复内容，键为用户ID，值为回复元组列表
            # 被动回复模式下，AI处理完成后将回复缓存到此字典，
            # 等待微信重试请求时从缓存中取出返回
            self.cache_dict = defaultdict(list)
            # Record whether the current message is being processed
            # 记录当前消息是否正在处理中
            # 用户ID在此集合中表示AI正在处理该用户的消息
            self.running = set()
            # Count the request from wechat official server by message_id
            # 按message_id统计微信重试请求次数
            # 微信会对同一条消息重试3次，需要计数判断是否为最后一次重试
            self.request_cnt = dict()
            # The permanent media need to be deleted to avoid media number limit
            # 创建异步事件循环，用于延迟删除永久素材
            # 微信公众号永久素材数量有限制（图片10万，语音1万等），
            # 需要在发送后及时删除，避免超限
            self.delete_media_loop = asyncio.new_event_loop()
            t = threading.Thread(target=self.start_loop, args=(self.delete_media_loop,))
            t.setDaemon(True)
            t.start()

    def startup(self):
        """
        启动微信公众号的HTTP服务器。

        根据passive_reply模式选择不同的URL路由处理器：
        - 被动回复模式：使用passive_reply.Query
        - 主动回复模式：使用active_reply.Query

        默认端口为8080，可通过wechatmp_port配置项修改。
        """
        if self.passive_reply:
            urls = ("/wx", "channel.wechatmp.passive_reply.Query")
        else:
            urls = ("/wx", "channel.wechatmp.active_reply.Query")
        app = web.application(urls, globals(), autoreload=False)
        port = conf().get("wechatmp_port", 8080)
        func = web.httpserver.StaticMiddleware(app.wsgifunc())
        func = web.httpserver.LogMiddleware(func)
        server = web.httpserver.WSGIServer(("0.0.0.0", port), func)
        self._http_server = server
        try:
            server.start()
        except (KeyboardInterrupt, SystemExit):
            server.stop()

    def stop(self):
        """
        停止微信公众号的HTTP服务器。

        安全关闭HTTP服务器，释放端口占用。
        """
        if self._http_server:
            try:
                self._http_server.stop()
                logger.info("[wechatmp] HTTP server stopped")
            except Exception as e:
                logger.warning(f"[wechatmp] Error stopping HTTP server: {e}")
            self._http_server = None

    def start_loop(self, loop):
        """
        在子线程中运行异步事件循环。

        用于在独立线程中运行asyncio事件循环，以便在同步代码中
        通过run_coroutine_threadsafe提交协程任务。

        Args:
            loop: asyncio事件循环对象
        """
        asyncio.set_event_loop(loop)
        loop.run_forever()

    async def delete_media(self, media_id):
        """
        异步删除永久素材，延迟10秒执行。

        微信公众号的永久素材数量有限制，发送完毕后需要及时删除。
        延迟10秒是为了确保微信服务器已经完成素材的读取和转发，
        避免删除过早导致用户收到"该消息已失效"的提示。

        Args:
            media_id: 要删除的永久素材ID
        """
        logger.debug("[wechatmp] permanent media {} will be deleted in 10s".format(media_id))
        await asyncio.sleep(10)
        self.client.material.delete(media_id)
        logger.info("[wechatmp] permanent media {} has been deleted".format(media_id))

    def send(self, reply: Reply, context: Context):
        """
        将AI生成的回复发送给微信公众号用户。

        根据回复模式（被动/主动）和回复类型（文本/语音/图片/视频）
        选择不同的发送策略。

        被动回复模式：
        - 将回复内容缓存到cache_dict，等待微信重试请求时返回
        - 使用永久素材API（material.add）上传媒体文件
        - 语音回复支持自动分段（每段不超过60秒）

        主动回复模式：
        - 直接通过客服消息API推送回复
        - 使用临时素材API（media.upload）上传媒体文件
        - 超长文本自动分段发送

        Args:
            reply: AI生成的回复对象，包含回复类型和内容
            context: 消息上下文，包含receiver（用户OpenID）等信息
        """
        receiver = context["receiver"]
        if self.passive_reply:
            # ===== 被动回复模式：缓存回复内容，等待微信重试请求 =====
            if reply.type == ReplyType.TEXT or reply.type == ReplyType.INFO or reply.type == ReplyType.ERROR:
                # 文本回复：移除Markdown符号后缓存
                reply_text = remove_markdown_symbol(reply.content)
                logger.info("[wechatmp] text cached, receiver {}\n{}".format(receiver, reply_text))
                self.cache_dict[receiver].append(("text", reply_text))
            elif reply.type == ReplyType.VOICE:
                # 语音回复：上传为永久素材后缓存media_id
                try:
                    voice_file_path = reply.content
                    # 将长语音按60秒分段（微信语音消息最长60秒）
                    duration, files = split_audio(voice_file_path, 60 * 1000)
                    if len(files) > 1:
                        logger.info("[wechatmp] voice too long {}s > 60s , split into {} parts".format(duration / 1000.0, len(files)))

                    for path in files:
                        # support: <2M, <60s, mp3/wma/wav/amr
                        # 微信公众号语音素材限制：文件不超过2MB，时长不超过60秒
                        try:
                            with open(path, "rb") as f:
                                # 使用永久素材API上传，因为被动回复需要media_id
                                response = self.client.material.add("voice", f)
                                logger.debug("[wechatmp] upload voice response: {}".format(response))
                                f_size = os.fstat(f.fileno()).st_size
                                # 根据文件大小计算等待时间，避免上传后立即删除导致素材未就绪
                                # 等待1秒 + 2秒/MB的传输缓冲时间
                                time.sleep(1.0 + 2 * f_size / 1024 / 1024)
                                # todo check media_id
                        except WeChatClientException as e:
                            logger.error("[wechatmp] upload voice failed: {}".format(e))
                            return
                        media_id = response["media_id"]
                        logger.info("[wechatmp] voice uploaded, receiver {}, media_id {}".format(receiver, media_id))
                        # 缓存语音回复的media_id
                        self.cache_dict[receiver].append(("voice", media_id))
                except ImportError as e:
                    logger.error("[wechatmp] voice conversion failed: {}".format(e))
                    logger.error("[wechatmp] please install pydub: pip install pydub")
                    return

            elif reply.type == ReplyType.IMAGE_URL:  # 从网络下载图片
                # 网络图片回复：下载后上传为永久素材，缓存media_id
                img_url = reply.content
                pic_res = requests.get(img_url, stream=True)
                image_storage = io.BytesIO()
                for block in pic_res.iter_content(1024):
                    image_storage.write(block)
                image_storage.seek(0)
                # 检测图片格式
                image_type = imghdr.what(image_storage)
                filename = receiver + "-" + str(context["msg"].msg_id) + "." + image_type
                content_type = "image/" + image_type
                try:
                    # 上传为永久素材
                    response = self.client.material.add("image", (filename, image_storage, content_type))
                    logger.debug("[wechatmp] upload image response: {}".format(response))
                except WeChatClientException as e:
                    logger.error("[wechatmp] upload image failed: {}".format(e))
                    return
                media_id = response["media_id"]
                logger.info("[wechatmp] image uploaded, receiver {}, media_id {}".format(receiver, media_id))
                self.cache_dict[receiver].append(("image", media_id))
            elif reply.type == ReplyType.IMAGE:  # 从文件读取图片
                # 本地图片回复：上传为永久素材，缓存media_id
                image_storage = reply.content
                image_storage.seek(0)
                image_type = imghdr.what(image_storage)
                filename = receiver + "-" + str(context["msg"].msg_id) + "." + image_type
                content_type = "image/" + image_type
                try:
                    response = self.client.material.add("image", (filename, image_storage, content_type))
                    logger.debug("[wechatmp] upload image response: {}".format(response))
                except WeChatClientException as e:
                    logger.error("[wechatmp] upload image failed: {}".format(e))
                    return
                media_id = response["media_id"]
                logger.info("[wechatmp] image uploaded, receiver {}, media_id {}".format(receiver, media_id))
                self.cache_dict[receiver].append(("image", media_id))
            elif reply.type == ReplyType.VIDEO_URL:  # 从网络下载视频
                # 网络视频回复：下载后上传为永久素材，缓存media_id
                video_url = reply.content
                video_res = requests.get(video_url, stream=True)
                video_storage = io.BytesIO()
                for block in video_res.iter_content(1024):
                    video_storage.write(block)
                video_storage.seek(0)
                # 视频统一使用mp4格式
                video_type = 'mp4'
                filename = receiver + "-" + str(context["msg"].msg_id) + "." + video_type
                content_type = "video/" + video_type
                try:
                    response = self.client.material.add("video", (filename, video_storage, content_type))
                    logger.debug("[wechatmp] upload video response: {}".format(response))
                except WeChatClientException as e:
                    logger.error("[wechatmp] upload video failed: {}".format(e))
                    return
                media_id = response["media_id"]
                logger.info("[wechatmp] video uploaded, receiver {}, media_id {}".format(receiver, media_id))
                self.cache_dict[receiver].append(("video", media_id))

            elif reply.type == ReplyType.VIDEO:  # 从文件读取视频
                # 本地视频回复：上传为永久素材，缓存media_id
                video_storage = reply.content
                video_storage.seek(0)
                video_type = 'mp4'
                filename = receiver + "-" + str(context["msg"].msg_id) + "." + video_type
                content_type = "video/" + video_type
                try:
                    response = self.client.material.add("video", (filename, video_storage, content_type))
                    logger.debug("[wechatmp] upload video response: {}".format(response))
                except WeChatClientException as e:
                    logger.error("[wechatmp] upload video failed: {}".format(e))
                    return
                media_id = response["media_id"]
                logger.info("[wechatmp] video uploaded, receiver {}, media_id {}".format(receiver, media_id))
                self.cache_dict[receiver].append(("video", media_id))

        else:
            # ===== 主动回复模式：通过客服消息API直接推送回复 =====
            if reply.type == ReplyType.TEXT or reply.type == ReplyType.INFO or reply.type == ReplyType.ERROR:
                # 文本回复：超长文本自动分段发送
                reply_text = reply.content
                texts = split_string_by_utf8_length(reply_text, MAX_UTF8_LEN)
                if len(texts) > 1:
                    logger.info("[wechatmp] text too long, split into {} parts".format(len(texts)))
                for i, text in enumerate(texts):
                    self.client.message.send_text(receiver, text)
                    if i != len(texts) - 1:
                        time.sleep(0.5)  # 休眠0.5秒，防止发送过快乱序
                logger.info("[wechatmp] Do send text to {}: {}".format(receiver, reply_text))
            elif reply.type == ReplyType.VOICE:
                # 语音回复：转换格式、分段、上传临时素材后发送
                try:
                    file_path = reply.content
                    file_name = os.path.basename(file_path)
                    file_type = os.path.splitext(file_name)[1]
                    # 微信公众号支持的语音格式：MP3和AMR
                    # 其他格式需要先转换为MP3
                    if file_type == ".mp3":
                        file_type = "audio/mpeg"
                    elif file_type == ".amr":
                        file_type = "audio/amr"
                    else:
                        # 非MP3/AMR格式，转换为MP3
                        mp3_file = os.path.splitext(file_path)[0] + ".mp3"
                        any_to_mp3(file_path, mp3_file)
                        file_path = mp3_file
                        file_name = os.path.basename(file_path)
                        file_type = "audio/mpeg"
                    logger.info("[wechatmp] file_name: {}, file_type: {} ".format(file_name, file_type))
                    media_ids = []
                    # 将长语音按60秒分段
                    duration, files = split_audio(file_path, 60 * 1000)
                    if len(files) > 1:
                        logger.info("[wechatmp] voice too long {}s > 60s , split into {} parts".format(duration / 1000.0, len(files)))
                    for path in files:
                        # support: <2M, <60s, AMR\MP3
                        # 上传为临时素材（3天后自动过期，无需手动删除）
                        response = self.client.media.upload("voice", (os.path.basename(path), open(path, "rb"), file_type))
                        logger.debug("[wechatcom] upload voice response: {}".format(response))
                        media_ids.append(response["media_id"])
                        # 上传后删除本地临时文件
                        os.remove(path)
                except ImportError as e:
                    logger.error("[wechatmp] voice conversion failed: {}".format(e))
                    logger.error("[wechatmp] please install pydub: pip install pydub")
                    return
                except WeChatClientException as e:
                    logger.error("[wechatmp] upload voice failed: {}".format(e))
                    return

                # 清理原始语音文件
                try:
                    os.remove(file_path)
                except Exception:
                    pass

                # 逐段发送语音消息
                for media_id in media_ids:
                    self.client.message.send_voice(receiver, media_id)
                    time.sleep(1)
                logger.info("[wechatmp] Do send voice to {}".format(receiver))
            elif reply.type == ReplyType.IMAGE_URL:  # 从网络下载图片
                # 网络图片回复：下载后上传为临时素材，通过客服消息发送
                img_url = reply.content
                pic_res = requests.get(img_url, stream=True)
                image_storage = io.BytesIO()
                for block in pic_res.iter_content(1024):
                    image_storage.write(block)
                image_storage.seek(0)
                image_type = imghdr.what(image_storage)
                filename = receiver + "-" + str(context["msg"].msg_id) + "." + image_type
                content_type = "image/" + image_type
                try:
                    # 上传为临时素材
                    response = self.client.media.upload("image", (filename, image_storage, content_type))
                    logger.debug("[wechatmp] upload image response: {}".format(response))
                except WeChatClientException as e:
                    logger.error("[wechatmp] upload image failed: {}".format(e))
                    return
                self.client.message.send_image(receiver, response["media_id"])
                logger.info("[wechatmp] Do send image to {}".format(receiver))
            elif reply.type == ReplyType.IMAGE:  # 从文件读取图片
                # 本地图片回复：上传为临时素材后发送
                image_storage = reply.content
                image_storage.seek(0)
                image_type = imghdr.what(image_storage)
                filename = receiver + "-" + str(context["msg"].msg_id) + "." + image_type
                content_type = "image/" + image_type
                try:
                    response = self.client.media.upload("image", (filename, image_storage, content_type))
                    logger.debug("[wechatmp] upload image response: {}".format(response))
                except WeChatClientException as e:
                    logger.error("[wechatmp] upload image failed: {}".format(e))
                    return
                self.client.message.send_image(receiver, response["media_id"])
                logger.info("[wechatmp] Do send image to {}".format(receiver))
            elif reply.type == ReplyType.VIDEO_URL:  # 从网络下载视频
                # 网络视频回复：下载后上传为临时素材，通过客服消息发送
                video_url = reply.content
                video_res = requests.get(video_url, stream=True)
                video_storage = io.BytesIO()
                for block in video_res.iter_content(1024):
                    video_storage.write(block)
                video_storage.seek(0)
                video_type = 'mp4'
                filename = receiver + "-" + str(context["msg"].msg_id) + "." + video_type
                content_type = "video/" + video_type
                try:
                    response = self.client.media.upload("video", (filename, video_storage, content_type))
                    logger.debug("[wechatmp] upload video response: {}".format(response))
                except WeChatClientException as e:
                    logger.error("[wechatmp] upload video failed: {}".format(e))
                    return
                self.client.message.send_video(receiver, response["media_id"])
                logger.info("[wechatmp] Do send video to {}".format(receiver))
            elif reply.type == ReplyType.VIDEO:  # 从文件读取视频
                # 本地视频回复：上传为临时素材后发送
                video_storage = reply.content
                video_storage.seek(0)
                video_type = 'mp4'
                filename = receiver + "-" + str(context["msg"].msg_id) + "." + video_type
                content_type = "video/" + video_type
                try:
                    response = self.client.media.upload("video", (filename, video_storage, content_type))
                    logger.debug("[wechatmp] upload video response: {}".format(response))
                except WeChatClientException as e:
                    logger.error("[wechatmp] upload video failed: {}".format(e))
                    return
                self.client.message.send_video(receiver, response["media_id"])
                logger.info("[wechatmp] Do send video to {}".format(receiver))
        return

    def _success_callback(self, session_id, context, **kwargs):  # 线程异常结束时的回调函数
        """
        AI回复生成成功后的回调函数。

        在被动回复模式下，将用户从running集合中移除，
        表示该用户的消息处理已完成，下次微信重试请求时
        可以从缓存中获取回复。

        Args:
            session_id: 会话ID（通常是用户的OpenID）
            context: 消息上下文
        """
        logger.debug("[wechatmp] Success to generate reply, msgId={}".format(context["msg"].msg_id))
        if self.passive_reply:
            # 处理完成，从运行中集合移除
            self.running.remove(session_id)

    def _fail_callback(self, session_id, exception, context, **kwargs):  # 线程异常结束时的回调函数
        """
        AI回复生成失败后的回调函数。

        在被动回复模式下，确保用户从running集合中移除，
        避免用户一直处于"运行中"状态导致后续消息无法处理。
        同时断言该用户不在cache_dict中（因为处理失败了，不应该有缓存回复）。

        Args:
            session_id: 会话ID（通常是用户的OpenID）
            exception: 导致失败的异常对象
            context: 消息上下文

        Raises:
            AssertionError: 如果session_id仍在cache_dict中（不应该出现此情况）
        """
        logger.exception("[wechatmp] Fail to generate reply to user, msgId={}, exception={}".format(context["msg"].msg_id, exception))
        if self.passive_reply:
            # 处理失败时，不应该有缓存的回复内容
            assert session_id not in self.cache_dict
            # 从运行中集合移除，允许用户发送新消息
            self.running.remove(session_id)
