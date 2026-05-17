"""
微信公众号被动回复模块 —— 实现个人公众号（订阅号）的消息处理和被动回复。

本模块实现了passive_reply.Query类，用于处理微信公众号在"被动回复"模式下的
消息接收和回复。被动回复模式适用于个人订阅号，其核心特点是：

1. 必须在5秒内通过HTTP响应返回回复内容
2. 微信服务器会重试3次（每次间隔5秒），共15秒的窗口期
3. 超时则显示"该公众号暂时无法提供服务"

被动回复的工作流程：
1. 用户发送消息 -> 微信服务器POST推送到回调URL
2. 服务端在5秒内处理并返回XML格式的回复
3. 如果5秒内无法完成处理，微信会重试（相同message_id，最多3次）
4. 利用重试机制，服务端可以在15秒内完成处理并返回回复
5. 超过15秒仍未回复，返回提示消息让用户稍后重试

回复缓存机制：
- AI处理完成后，将回复内容缓存到channel.cache_dict中
- 下次微信重试请求时，从缓存中取出回复并返回
- 这种设计巧妙地利用了微信的重试机制，延长了有效回复时间
"""
import asyncio
import time

import web
from wechatpy import parse_message
from wechatpy.replies import ImageReply, VoiceReply, create_reply
import textwrap
from bridge.context import *
from bridge.reply import *
from channel.wechatmp.common import *
from channel.wechatmp.wechatmp_channel import WechatMPChannel
from channel.wechatmp.wechatmp_message import WeChatMPMessage
from common.log import logger
from common.utils import split_string_by_utf8_length
from config import conf, subscribe_msg


# This class is instantiated once per query
class Query:
    """
    微信公众号被动回复的请求处理器。

    处理微信公众号服务器的GET（URL验证）和POST（消息推送）请求。
    被动回复模式需要在HTTP响应中返回XML格式的回复内容。

    核心策略：利用微信的重试机制延长回复时间窗口
    - 微信会在5秒后重试，共重试3次
    - 第一次请求时启动AI处理，标记用户为"运行中"状态
    - 后续重试请求检查AI是否已完成处理
    - 如果在15秒内完成处理，返回缓存的回复
    - 如果超时，返回提示消息让用户稍后重试
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
        处理微信公众号推送的用户消息（被动回复模式）。

        处理流程：
        1. 验证消息签名，解密加密消息（如果有）
        2. 解析消息类型和内容
        3. 判断是否为新请求（首次请求 vs 微信重试请求）
        4. 新请求：启动AI处理，标记为运行中，等待回复
        5. 重试请求：检查AI是否已完成，返回缓存回复或继续等待
        6. 超时：返回提示消息

        微信重试机制的关键：
        - 微信服务器会对同一条消息重试3次，每次间隔5秒
        - 通过message_id判断是否为重试请求
        - 通过channel.running集合判断AI是否仍在处理中

        Returns:
            加密后的XML格式回复内容，或"success"字符串
        """
        try:
            args = web.input()
            # 验证消息签名
            verify_server(args)
            request_time = time.time()  # 记录请求时间，用于计算等待超时
            channel = WechatMPChannel()
            message = web.data()
            # 加密函数：如果消息是加密的，回复也需要加密
            encrypt_func = lambda x: x
            if args.get("encrypt_type") == "aes":
                logger.debug("[wechatmp] Receive encrypted post data:\n" + message.decode("utf-8"))
                if not channel.crypto:
                    raise Exception("Crypto not initialized, Please set wechatmp_aes_key in config.json")
                message = channel.crypto.decrypt_message(message, args.msg_signature, args.timestamp, args.nonce)
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

                # 判断消息内容是否被微信标记为不支持
                supported = True
                if "【收到不支持的消息类型，暂无法显示】" in content:
                    supported = False  # not supported, used to refresh  # 微信标记为不支持的消息，用于触发刷新

                # New request
                # 判断是否为新请求（首次请求）
                # 新请求条件：
                # 1. 该用户没有缓存且不在运行中 -> 新用户或已处理完的用户的首次消息
                # 2. 消息以#开头且message_id不在request_cnt中 -> 插入godcmd（管理员命令）
                if (
                    channel.cache_dict.get(from_user) is None
                    and from_user not in channel.running
                    or content.startswith("#")
                    and message_id not in channel.request_cnt  # insert the godcmd
                ):
                    # The first query begin
                    # 首次请求：构造消息上下文并启动AI处理
                    if msg.type == "voice" and wechatmp_msg.ctype == ContextType.TEXT and conf().get("voice_reply_voice", False):
                        context = channel._compose_context(wechatmp_msg.ctype, content, isgroup=False, desire_rtype=ReplyType.VOICE, msg=wechatmp_msg)
                    else:
                        context = channel._compose_context(wechatmp_msg.ctype, content, isgroup=False, msg=wechatmp_msg)
                    logger.debug("[wechatmp] context: {} {} {}".format(context, wechatcom_msg, supported))

                    if supported and context:
                        # 消息受支持且上下文有效：标记为运行中并投入处理
                        channel.running.add(from_user)
                        channel.produce(context)
                    else:
                        # 消息不支持或上下文无效：返回提示消息
                        trigger_prefix = conf().get("single_chat_prefix", [""])[0]
                        if trigger_prefix or not supported:
                            if trigger_prefix:
                                # 提示用户使用前缀触发AI回复
                                reply_text = textwrap.dedent(
                                    f"""\
                                    请输入'{trigger_prefix}'接你想说的话跟我说话。
                                    例如:
                                    {trigger_prefix}你好，很高兴见到你。"""
                                )
                            else:
                                # 无前缀要求时提示用户直接对话
                                reply_text = textwrap.dedent(
                                    """\
                                    你好，很高兴见到你。
                                    请跟我说话吧。"""
                                )
                        else:
                            logger.error(f"[wechatmp] unknown error")
                            reply_text = textwrap.dedent(
                                """\
                                未知错误，请稍后再试"""
                            )

                        replyPost = create_reply(reply_text, msg)
                        return encrypt_func(replyPost.render())

                # Wechat official server will request 3 times (5 seconds each), with the same message_id.
                # Because the interval is 5 seconds, here assumed that do not have multithreading problems.
                # 微信服务器会对同一条消息重试3次（每次间隔5秒），使用相同的message_id
                # 由于间隔5秒，此处假设不存在多线程并发问题
                request_cnt = channel.request_cnt.get(message_id, 0) + 1
                channel.request_cnt[message_id] = request_cnt
                logger.info(
                    "[wechatmp] Request {} from {} {} {}:{}\n{}".format(
                        request_cnt, from_user, message_id, web.ctx.env.get("REMOTE_ADDR"), web.ctx.env.get("REMOTE_PORT"), content
                    )
                )

                # 等待AI处理完成，最多等待4秒（留1秒给HTTP响应传输）
                task_running = True
                waiting_until = request_time + 4
                while time.time() < waiting_until:
                    if from_user in channel.running:
                        # AI仍在处理中，短暂等待后重试
                        time.sleep(0.1)
                    else:
                        # AI处理完成
                        task_running = False
                        break

                reply_text = ""
                if task_running:
                    # AI仍在处理中（超时未完成）
                    if request_cnt < 3:
                        # waiting for timeout (the POST request will be closed by Wechat official server)
                        # 还有重试机会：等待2秒让当前请求超时，微信会发送下一次重试
                        time.sleep(2)
                        # and do nothing, waiting for the next request
                        # 不返回任何回复，等待微信的下一次重试请求
                        return "success"
                    else:  # request_cnt == 3:
                        # return timeout message
                        # 第3次重试仍未完成：返回超时提示消息
                        # 用户回复任意文字即可触发缓存中的回复
                        reply_text = "【正在思考中，回复任意文字尝试获取回复】"
                        replyPost = create_reply(reply_text, msg)
                        return encrypt_func(replyPost.render())

                # reply is ready
                # AI处理完成，从缓存中获取回复
                channel.request_cnt.pop(message_id)

                # no return because of bandwords or other reasons
                # 没有缓存回复（可能因为违禁词过滤或其他原因被丢弃）
                if from_user not in channel.cache_dict and from_user not in channel.running:
                    return "success"

                # Only one request can access to the cached data
                # 从缓存中弹出第一条回复（支持多条回复队列）
                try:
                    (reply_type, reply_content) = channel.cache_dict[from_user].pop(0)
                    if not channel.cache_dict[from_user]:  # If popping the message makes the list empty, delete the user entry from cache
                        # 缓存队列已空，删除该用户的缓存条目，释放内存
                        del channel.cache_dict[from_user]
                except IndexError:
                    # 缓存为空，没有可返回的回复
                    return "success"

                if reply_type == "text":
                    # 文本回复处理：检查长度是否超过限制
                    if len(reply_content.encode("utf8")) <= MAX_UTF8_LEN:
                        # 长度在限制内，直接返回
                        reply_text = reply_content
                    else:
                        # 超长文本：截取第一段，其余放入缓存队列等待用户触发继续
                        continue_text = "\n【未完待续，回复任意文字以继续】"
                        splits = split_string_by_utf8_length(
                            reply_content,
                            MAX_UTF8_LEN - len(continue_text.encode("utf-8")),
                            max_split=1,
                        )
                        reply_text = splits[0] + continue_text
                        # 将剩余部分追加到缓存队列，用户回复任意文字后可获取
                        channel.cache_dict[from_user].append(("text", splits[1]))

                    logger.info(
                        "[wechatmp] Request {} do send to {} {}: {}\n{}".format(
                            request_cnt,
                            from_user,
                            message_id,
                            content,
                            reply_text,
                        )
                    )
                    replyPost = create_reply(reply_text, msg)
                    return encrypt_func(replyPost.render())

                elif reply_type == "voice":
                    # 语音回复：使用VoiceReply，通过media_id引用已上传的语音素材
                    media_id = reply_content
                    # 异步删除临时素材，避免素材数量超限
                    # 微信公众号永久素材数量有限制，需要在发送后及时删除
                    asyncio.run_coroutine_threadsafe(channel.delete_media(media_id), channel.delete_media_loop)
                    logger.info(
                        "[wechatmp] Request {} do send to {} {}: {} voice media_id {}".format(
                            request_cnt,
                            from_user,
                            message_id,
                            content,
                            media_id,
                        )
                    )
                    replyPost = VoiceReply(message=msg)
                    replyPost.media_id = media_id
                    return encrypt_func(replyPost.render())

                elif reply_type == "image":
                    # 图片回复：使用ImageReply，通过media_id引用已上传的图片素材
                    media_id = reply_content
                    # 异步删除临时素材，避免素材数量超限
                    asyncio.run_coroutine_threadsafe(channel.delete_media(media_id), channel.delete_media_loop)
                    logger.info(
                        "[wechatmp] Request {} do send to {} {}: {} image media_id {}".format(
                            request_cnt,
                            from_user,
                            message_id,
                            content,
                            media_id,
                        )
                    )
                    replyPost = ImageReply(message=msg)
                    replyPost.media_id = media_id
                    return encrypt_func(replyPost.render())

            elif msg.type == "event":
                # 事件消息处理（如关注/取消关注/扫码等）
                logger.info("[wechatmp] Event {} from {}".format(msg.event, msg.source))
                if msg.event in ["subscribe", "subscribe_scan"]:
                    # 关注/扫码关注事件：返回欢迎消息
                    reply_text = subscribe_msg()
                    if reply_text:
                        replyPost = create_reply(reply_text, msg)
                        return encrypt_func(replyPost.render())
                else:
                    return "success"
            else:
                logger.info("暂且不处理")  # 暂不支持的消息类型
            return "success"
        except Exception as exc:
            logger.exception(exc)
            return exc
