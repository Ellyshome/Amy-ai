"""
微信公众号主动回复模块 —— 实现认证公众号（服务号）的消息处理和主动回复。

本模块实现了active_reply.Query类，用于处理微信公众号在"主动回复"模式下的
消息接收和回复。与被动回复模式不同，主动回复模式下：
1. 收到用户消息后立即返回"success"，不在HTTP响应中携带回复内容
2. 通过客服消息API主动向用户推送回复
3. 适用于认证服务号，不受5秒回复时间限制

此模式的优势：
- 不受微信公众号5秒被动回复时间限制
- 可以发送较长的回复内容
- 支持多轮处理后再回复

此模式的限制：
- 需要认证服务号才能使用客服消息API
- 用户必须在48小时内与公众号互动才能收到客服消息
"""
import time

import web
from wechatpy import parse_message
from wechatpy.replies import create_reply

from bridge.context import *
from bridge.reply import *
from channel.wechatmp.common import *
from channel.wechatmp.wechatmp_channel import WechatMPChannel
from channel.wechatmp.wechatmp_message import WeChatMPMessage
from common.log import logger
from config import conf, subscribe_msg


# This class is instantiated once per query
class Query:
    """
    微信公众号主动回复的请求处理器。

    处理微信公众号服务器的GET（URL验证）和POST（消息推送）请求。
    每次请求都会创建新的Query实例，通过@singleton装饰器确保
    WechatMPChannel使用同一个实例。

    主动回复模式的核心逻辑：
    1. 收到用户消息后，解析消息内容
    2. 将消息投入处理流程
    3. 立即返回"success"，告知微信服务器消息已收到
    4. AI处理完成后，通过WechatMPChannel.send()方法使用客服消息API推送回复
    """

    def GET(self):
        """
        处理微信公众号的URL验证请求。

        委托给common.verify_server函数完成签名验证。

        Returns:
            验证通过时返回echostr，失败时抛出403异常
        """
        return verify_server(web.input())

    def POST(self):
        """
        处理微信公众号推送的用户消息（主动回复模式）。

        处理流程：
        1. 验证消息签名
        2. 如果消息加密，则解密消息内容
        3. 解析消息类型（文本/语音/图片/事件）
        4. 将消息封装为WeChatMPMessage对象
        5. 构造消息上下文并投入处理流程
        6. 立即返回"success"

        与被动回复模式的关键区别：
        - 不需要在HTTP响应中返回回复内容
        - AI处理完成后通过客服消息API主动推送
        - 不受5秒超时限制

        Returns:
            "success"字符串，告知微信服务器消息已收到
            事件消息可能返回加密的XML回复（如关注自动回复）
        """
        # Make sure to return the instance that first created, @singleton will do that.
        # 确保使用@singleton创建的WechatMPChannel实例，保证状态共享
        try:
            args = web.input()
            # 验证消息签名，确保请求来自微信服务器
            verify_server(args)
            channel = WechatMPChannel()
            message = web.data()
            # 加密函数：如果消息是加密的，回复也需要加密
            encrypt_func = lambda x: x
            if args.get("encrypt_type") == "aes":
                # 加密消息模式：解密消息内容，并设置加密函数用于回复
                logger.debug("[wechatmp] Receive encrypted post data:\n" + message.decode("utf-8"))
                if not channel.crypto:
                    raise Exception("Crypto not initialized, Please set wechatmp_aes_key in config.json")
                message = channel.crypto.decrypt_message(message, args.msg_signature, args.timestamp, args.nonce)
                # 回复消息时需要加密，保存加密函数
                encrypt_func = lambda x: channel.crypto.encrypt_message(x, args.nonce, args.timestamp)
            else:
                logger.debug("[wechatmp] Receive post data:\n" + message.decode("utf-8"))
            # 解析XML格式的消息
            msg = parse_message(message)
            if msg.type in ["text", "voice", "image"]:
                # 支持的消息类型：文本、语音、图片
                wechatmp_msg = WeChatMPMessage(msg, client=channel.client)
                from_user = wechatmp_msg.from_user_id
                content = wechatmp_msg.content
                message_id = wechatmp_msg.msg_id

                logger.info(
                    "[wechatmp] {}:{} Receive post query {} {}: {}".format(
                        web.ctx.env.get("REMOTE_ADDR"),
                        web.ctx.env.get("REMOTE_PORT"),
                        from_user,
                        message_id,
                        content,
                    )
                )
                # 语音消息且配置了"语音回复语音"模式时，设置desire_rtype为VOICE
                if msg.type == "voice" and wechatmp_msg.ctype == ContextType.TEXT and conf().get("voice_reply_voice", False):
                    context = channel._compose_context(wechatmp_msg.ctype, content, isgroup=False, desire_rtype=ReplyType.VOICE, msg=wechatmp_msg)
                else:
                    context = channel._compose_context(wechatmp_msg.ctype, content, isgroup=False, msg=wechatmp_msg)
                if context:
                    # 将消息投入处理流程，AI处理完成后通过channel.send()主动推送回复
                    channel.produce(context)
                # The reply will be sent by channel.send() in another thread
                # 回复将由channel.send()在另一个线程中发送（主动推送模式）
                return "success"
            elif msg.type == "event":
                # 事件消息处理
                logger.info("[wechatmp] Event {} from {}".format(msg.event, msg.source))
                if msg.event in ["subscribe", "subscribe_scan"]:
                    # 关注事件：返回关注欢迎消息
                    reply_text = subscribe_msg()
                    if reply_text:
                        replyPost = create_reply(reply_text, msg)
                        return encrypt_func(replyPost.render())
                else:
                    return "success"
            else:
                logger.info("暂且不处理")  # 暂不支持的消息类型（如视频、位置等）
            return "success"
        except Exception as exc:
            logger.exception(exc)
            return exc
