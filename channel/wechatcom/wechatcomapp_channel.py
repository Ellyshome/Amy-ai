# -*- coding=utf-8 -*-
"""
企业微信自建应用渠道模块 —— 实现与企业微信自建应用的消息收发。

本模块实现了WechatComAppChannel类，作为ChatChannel的子类，通过web.py框架
提供HTTP服务，接收企业微信的回调消息并发送回复。使用wechatpy库处理
消息加解密和API调用。

企业微信自建应用的通信机制：
1. 企业微信服务器通过POST回调推送用户消息
2. 本服务解析消息后投入处理流程
3. 通过企业微信API主动发送回复消息（非被动回复）
"""
import io
import os
import sys
import time

import requests
import web
from wechatpy.enterprise import create_reply, parse_message
from wechatpy.enterprise.crypto import WeChatCrypto
from wechatpy.enterprise.exceptions import InvalidCorpIdException
from wechatpy.exceptions import InvalidSignatureException, WeChatClientException

from bridge.context import Context
from bridge.reply import Reply, ReplyType
from channel.chat_channel import ChatChannel
from channel.wechatcom.wechatcomapp_client import WechatComAppClient
from channel.wechatcom.wechatcomapp_message import WechatComAppMessage
from common.log import logger
from common.singleton import singleton
from common.utils import compress_imgfile, fsize, split_string_by_utf8_length, convert_webp_to_png, remove_markdown_symbol
from config import conf, subscribe_msg
from voice.audio_convert import any_to_amr, split_audio

# 企业微信单条消息的最大UTF-8字节数限制
# 企业微信API对单条消息内容有长度限制，超过此长度需要分条发送
MAX_UTF8_LEN = 2048


@singleton
class WechatComAppChannel(ChatChannel):
    """
    企业微信自建应用渠道类，使用单例模式确保全局只有一个实例。

    继承自ChatChannel，提供与企业微信自建应用的消息收发功能。
    与微信公众号不同，企业微信自建应用采用"主动发送"模式，
    即收到消息后通过API主动推送回复，而非在HTTP响应中返回被动回复。

    主要功能：
    - 接收企业微信回调消息（文本、语音、图片等）
    - 发送文本、语音、图片回复
    - 支持长文本自动分段发送
    - 支持长语音自动分段发送
    - 支持图片压缩和WebP格式转换
    - 消息加解密（AES）

    配置项（config.json）：
    - wechatcom_corp_id: 企业ID
    - wechatcomapp_secret: 应用Secret
    - wechatcomapp_agent_id: 应用AgentId
    - wechatcomapp_token: 回调Token
    - wechatcomapp_aes_key: 回调AES Key
    - wechatcomapp_port: 服务监听端口（默认9898）
    """
    NOT_SUPPORT_REPLYTYPE = []

    def __init__(self):
        """
        初始化企业微信自建应用渠道。

        从配置中读取企业微信相关参数，创建加解密器和API客户端。
        - WeChatCrypto: 用于消息的AES加解密和签名验证
        - WechatComAppClient: 封装企业微信API调用，支持自动刷新access_token
        """
        super().__init__()
        self.corp_id = conf().get("wechatcom_corp_id")
        self.secret = conf().get("wechatcomapp_secret")
        self.agent_id = conf().get("wechatcomapp_agent_id")
        self.token = conf().get("wechatcomapp_token")
        self.aes_key = conf().get("wechatcomapp_aes_key")
        self._http_server = None
        logger.info(
            "[wechatcom] Initializing WeCom app channel, corp_id: {}, agent_id: {}".format(self.corp_id, self.agent_id)
        )
        # 创建消息加解密器，用于验证回调签名和解密消息内容
        self.crypto = WeChatCrypto(self.token, self.aes_key, self.corp_id)
        # 创建API客户端，用于主动发送消息和上传素材
        self.client = WechatComAppClient(self.corp_id, self.secret)

    def startup(self):
        """
        启动企业微信自建应用的HTTP服务器。

        配置URL路由，创建web.py应用，启动WSGI服务器监听指定端口。
        企业微信服务器会将用户消息通过POST请求发送到此服务。

        默认端口为9898，可通过wechatcomapp_port配置项修改。
        """
        # start message listener
        # 配置URL路由，将/wxcomapp/路径映射到Query处理器
        urls = ("/wxcomapp/?", "channel.wechatcom.wechatcomapp_channel.Query")
        app = web.application(urls, globals(), autoreload=False)
        port = conf().get("wechatcomapp_port", 9898)
        logger.info("[wechatcom] ✅ WeCom app channel started successfully")
        logger.info("[wechatcom] 📡 Listening on http://0.0.0.0:{}/wxcomapp/".format(port))
        logger.info("[wechatcom] 🤖 Ready to receive messages")

        # Build WSGI app with middleware (same as runsimple but without print)
        # 构建WSGI应用，添加静态文件中间件和日志中间件
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
        停止企业微信自建应用的HTTP服务器。

        安全关闭HTTP服务器，释放端口占用。在停止过程中捕获异常
        防止因服务器状态异常导致程序崩溃。
        """
        if self._http_server:
            try:
                self._http_server.stop()
                logger.info("[wechatcom] HTTP server stopped")
            except Exception as e:
                logger.warning(f"[wechatcom] Error stopping HTTP server: {e}")
            self._http_server = None

    def send(self, reply: Reply, context: Context):
        """
        将AI生成的回复发送给企业微信用户。

        企业微信采用主动发送模式，通过API推送消息给指定用户。
        根据回复类型（文本/语音/图片）选择对应的API方法发送。

        处理逻辑：
        1. 文本类型：移除Markdown符号，超长文本自动分段发送
        2. 语音类型：转换为AMR格式，超长语音自动分段，上传素材后发送
        3. IMAGE_URL类型：从网络下载图片，压缩后上传素材发送
        4. IMAGE类型：直接上传图片素材发送

        Args:
            reply: AI生成的回复对象，包含回复类型和内容
            context: 消息上下文，包含receiver（接收者ID）等信息
        """
        receiver = context["receiver"]
        if reply.type in [ReplyType.TEXT, ReplyType.ERROR, ReplyType.INFO]:
            # 文本消息处理：移除Markdown符号（企业微信不支持Markdown渲染）
            reply_text = remove_markdown_symbol(reply.content)
            # 按UTF-8字节长度分段，避免超过企业微信消息长度限制
            texts = split_string_by_utf8_length(reply_text, MAX_UTF8_LEN)
            if len(texts) > 1:
                logger.info("[wechatcom] text too long, split into {} parts".format(len(texts)))
            for i, text in enumerate(texts):
                self.client.message.send_text(self.agent_id, receiver, text)
                if i != len(texts) - 1:
                    time.sleep(0.5)  # 休眠0.5秒，防止发送过快乱序
            logger.info("[wechatcom] Do send text to {}: {}".format(receiver, reply_text))
        elif reply.type == ReplyType.VOICE:
            # 语音消息处理：需要转换格式、分段、上传素材后发送
            try:
                media_ids = []
                file_path = reply.content
                # 将音频文件转换为AMR格式（企业微信语音消息要求AMR格式）
                amr_file = os.path.splitext(file_path)[0] + ".amr"
                any_to_amr(file_path, amr_file)
                # 将长音频按60秒分段（企业微信语音消息最长60秒）
                duration, files = split_audio(amr_file, 60 * 1000)
                if len(files) > 1:
                    logger.info("[wechatcom] voice too long {}s > 60s , split into {} parts".format(duration / 1000.0, len(files)))
                # 逐段上传语音素材
                for path in files:
                    response = self.client.media.upload("voice", open(path, "rb"))
                    logger.debug("[wechatcom] upload voice response: {}".format(response))
                    media_ids.append(response["media_id"])
            except ImportError as e:
                logger.error("[wechatcom] voice conversion failed: {}".format(e))
                logger.error("[wechatcom] please install pydub: pip install pydub")
                return
            except WeChatClientException as e:
                logger.error("[wechatcom] upload voice failed: {}".format(e))
                return
            # 清理临时文件
            try:
                os.remove(file_path)
                if amr_file != file_path:
                    os.remove(amr_file)
            except Exception:
                pass
            # 逐段发送语音消息
            for media_id in media_ids:
                self.client.message.send_voice(self.agent_id, receiver, media_id)
                time.sleep(1)
            logger.info("[wechatcom] sendVoice={}, receiver={}".format(reply.content, receiver))
        elif reply.type == ReplyType.IMAGE_URL:  # 从网络下载图片
            # 网络图片消息处理：下载、压缩、上传素材后发送
            img_url = reply.content
            pic_res = requests.get(img_url, stream=True)
            image_storage = io.BytesIO()
            for block in pic_res.iter_content(1024):
                image_storage.write(block)
            # 检查图片大小，超过10MB需要压缩（企业微信图片消息限制10MB）
            sz = fsize(image_storage)
            if sz >= 10 * 1024 * 1024:
                logger.info("[wechatcom] image too large, ready to compress, sz={}".format(sz))
                image_storage = compress_imgfile(image_storage, 10 * 1024 * 1024 - 1)
                logger.info("[wechatcom] image compressed, sz={}".format(fsize(image_storage)))
            image_storage.seek(0)
            # WebP格式需要转换为PNG（企业微信不支持WebP格式）
            if ".webp" in img_url:
                try:
                    image_storage = convert_webp_to_png(image_storage)
                except Exception as e:
                    logger.error(f"Failed to convert image: {e}")
                    return
            # 上传图片素材到企业微信服务器
            try:
                response = self.client.media.upload("image", image_storage)
                logger.debug("[wechatcom] upload image response: {}".format(response))
            except WeChatClientException as e:
                logger.error("[wechatcom] upload image failed: {}".format(e))
                return

            self.client.message.send_image(self.agent_id, receiver, response["media_id"])
            logger.info("[wechatcom] sendImage url={}, receiver={}".format(img_url, receiver))
        elif reply.type == ReplyType.IMAGE:  # 从文件读取图片
            # 本地图片消息处理：压缩、上传素材后发送
            image_storage = reply.content
            sz = fsize(image_storage)
            if sz >= 10 * 1024 * 1024:
                logger.info("[wechatcom] image too large, ready to compress, sz={}".format(sz))
                image_storage = compress_imgfile(image_storage, 10 * 1024 * 1024 - 1)
                logger.info("[wechatcom] image compressed, sz={}".format(fsize(image_storage)))
            image_storage.seek(0)
            try:
                response = self.client.media.upload("image", image_storage)
                logger.debug("[wechatcom] upload image response: {}".format(response))
            except WeChatClientException as e:
                logger.error("[wechatcom] upload image failed: {}".format(e))
                return
            self.client.message.send_image(self.agent_id, receiver, response["media_id"])
            logger.info("[wechatcom] sendImage, receiver={}".format(receiver))


class Query:
    """
    企业微信回调请求处理器，处理企业微信服务器的GET和POST请求。

    GET请求用于URL验证（企业微信配置回调URL时需要验证），
    POST请求用于接收用户发送的消息。

    企业微信回调URL验证流程：
    1. 企业微信发送GET请求，携带msg_signature、timestamp、nonce、echostr参数
    2. 服务端验证签名并解密echostr，返回明文echostr
    3. 企业微信验证返回值一致后，确认回调URL有效
    """

    def GET(self):
        """
        处理企业微信的URL验证请求。

        企业微信配置回调URL时，会发送GET请求进行验证。
        需要验证签名并解密echostr参数，返回明文echostr值。

        Returns:
            解密后的echostr明文字符串

        Raises:
            web.Forbidden: 签名验证失败时返回403
        """
        channel = WechatComAppChannel()
        params = web.input()
        logger.info("[wechatcom] receive params: {}".format(params))
        try:
            signature = params.msg_signature
            timestamp = params.timestamp
            nonce = params.nonce
            echostr = params.echostr
            # 验证签名并解密echostr，确保请求来自企业微信服务器
            echostr = channel.crypto.check_signature(signature, timestamp, nonce, echostr)
        except InvalidSignatureException:
            raise web.Forbidden()
        return echostr

    def POST(self):
        """
        处理企业微信推送的用户消息。

        企业微信将用户发送的消息通过POST请求推送到回调URL。
        处理流程：
        1. 验证签名并解密消息内容
        2. 解析消息类型（文本/语音/图片/事件等）
        3. 将消息封装为WechatComAppMessage对象
        4. 构造消息上下文并投入处理流程

        事件消息（如关注事件）当前被忽略，只处理文本、语音、图片消息。

        Returns:
            "success"字符串，告知企业微信服务器消息已收到
            （企业微信要求5秒内返回响应，否则会重试）
        """
        channel = WechatComAppChannel()
        params = web.input()
        logger.info("[wechatcom] receive params: {}".format(params))
        try:
            signature = params.msg_signature
            timestamp = params.timestamp
            nonce = params.nonce
            # 验证签名并解密消息内容
            message = channel.crypto.decrypt_message(web.data(), signature, timestamp, nonce)
        except (InvalidSignatureException, InvalidCorpIdException):
            raise web.Forbidden()
        # 解析XML格式的消息为Python对象
        msg = parse_message(message)
        logger.debug("[wechatcom] receive message: {}, msg= {}".format(message, msg))
        if msg.type == "event":
            # 事件消息处理（如关注/取消关注）
            if msg.event == "subscribe":
                pass
                # reply_content = subscribe_msg()
                # if reply_content:
                #     reply = create_reply(reply_content, msg).render()
                #     res = channel.crypto.encrypt_message(reply, nonce, timestamp)
                #     return res
        else:
            # 非事件消息：将企业微信消息封装为统一的消息对象
            try:
                wechatcom_msg = WechatComAppMessage(msg, client=channel.client)
            except NotImplementedError as e:
                # 不支持的消息类型（如视频、文件等），直接返回success避免重试
                logger.debug("[wechatcom] " + str(e))
                return "success"
            # 构造消息上下文，标记为非群聊消息
            context = channel._compose_context(
                wechatcom_msg.ctype,
                wechatcom_msg.content,
                isgroup=False,
                msg=wechatcom_msg,
            )
            if context:
                # 将消息投入处理流程，由ChatChannel处理
                channel.produce(context)
        return "success"
