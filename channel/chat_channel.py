"""
消息通道核心基类，承载与平台无关的通用消息处理流水线。
采用生产者-消费者模式解耦消息收发，produce入队、consume轮询出队、线程池并发处理。
核心流程：_compose_context → _generate_reply → _decorate_reply → _send_reply，子类只需实现send/start即可接入。
"""
import os
import re
import threading
import time
from asyncio import CancelledError
from concurrent.futures import Future, ThreadPoolExecutor

from bridge.context import *
from bridge.reply import *
from channel.channel import Channel
from common.dequeue import Dequeue
from common import memory
from plugins import *

try:
    from voice.audio_convert import any_to_wav
except Exception as e:
    pass

# 全局线程池，用于并发处理消息。所有频道实例共享此线程池。
# 由于 LLM API 调用是 I/O 密集型，GIL 不构成瓶颈，8 个 worker 即可支撑较高的并发。
handler_pool = ThreadPoolExecutor(max_workers=8)  # 处理消息的线程池


# 抽象类, 它包含了与消息通道无关的通用处理逻辑
class ChatChannel(Channel):
    """
    聊天频道基类，封装了与具体消息平台无关的通用消息处理流水线。
    核心流程：produce（入队）→ consume（出队）→ _handle → _generate_reply → _decorate_reply → _send_reply
    各子类（如飞书、钉钉、Web 等）只需实现 send() 和 startup() 即可接入。
    """

    name = None  # 登录的用户名
    user_id = None  # 登录的用户id

    def __init__(self):
        super().__init__()
        # Instance-level attributes so each channel subclass has its own
        # independent session queue and lock. Previously these were class-level,
        # which caused contexts from one channel (e.g. Feishu) to be consumed
        # by another channel's consume() thread (e.g. Web), leading to errors
        # like "No request_id found in context".
        # 实例级属性，确保每个频道子类拥有独立的会话队列和锁。
        # 此前这些是类级属性，导致一个频道（如飞书）的上下文被另一个频道（如 Web）
        # 的 consume() 线程消费，引发 "No request_id found in context" 等错误。
        self.futures = {}    # session_id -> [Future]，记录每个会话的线程池任务
        self.sessions = {}   # session_id -> [Dequeue, BoundedSemaphore]，每个会话的消息队列和并发信号量
        self.lock = threading.Lock()  # 保护 sessions/futures 字典的线程安全锁
        _thread = threading.Thread(target=self.consume)
        _thread.setDaemon(True)  # 设为守护线程，主线程退出时自动结束
        _thread.start()

    # 根据消息构造context，消息内容相关的触发项写在这里
    def _compose_context(self, ctype: ContextType, content, **kwargs):
        """
        根据原始消息构建消息上下文（Context）对象。
        这是消息进入处理流水线的第一步，负责：
        1. 设置 session_id 和 receiver（会话标识和回复目标）
        2. 触发 ON_RECEIVE_MESSAGE 插件事件
        3. 群聊白名单过滤、昵称黑名单过滤
        4. 触发前缀匹配（如 @bot、特定关键词）
        5. 判断消息类型（文本/图片生成）并设置 context.type
        返回 None 表示该消息不需要处理（被过滤或未匹配触发条件）。
        """
        context = Context(ctype, content)
        context.kwargs = kwargs
        if "channel_type" not in context:
            context["channel_type"] = self.channel_type
        if "origin_ctype" not in context:
            context["origin_ctype"] = ctype
        # context首次传入时，receiver是None，根据类型设置receiver
        first_in = "receiver" not in context
        # 群名匹配过程，设置session_id和receiver
        if first_in:  # context首次传入时，receiver是None，根据类型设置receiver
            config = conf()
            cmsg = context["msg"]
            user_data = conf().get_user_data(cmsg.from_user_id)
            context["openai_api_key"] = user_data.get("openai_api_key")
            context["gpt_model"] = user_data.get("gpt_model")
            if context.get("isgroup", False):
                group_name = cmsg.other_user_nickname
                group_id = cmsg.other_user_id

                group_name_white_list = config.get("group_name_white_list", [])
                group_name_keyword_white_list = config.get("group_name_keyword_white_list", [])
                if any(
                    [
                        group_name in group_name_white_list,
                        "ALL_GROUP" in group_name_white_list,
                        check_contain(group_name, group_name_keyword_white_list),
                    ]
                ):
                    # 群名在白名单中，确定 session_id 的归属方式
                    # Check global group_shared_session config first
                    group_shared_session = conf().get("group_shared_session", True)
                    if group_shared_session:
                        # All users in the group share the same session
                        # 群内所有用户共享同一个会话（上下文互通）
                        session_id = group_id
                    else:
                        # Check group-specific whitelist (legacy behavior)
                        # 按旧逻辑检查群聊共享白名单
                        group_chat_in_one_session = conf().get("group_chat_in_one_session", [])
                        session_id = cmsg.actual_user_id
                        if any(
                            [
                                group_name in group_chat_in_one_session,
                                "ALL_GROUP" in group_chat_in_one_session,
                            ]
                        ):
                            session_id = group_id
                else:
                    logger.debug(f"No need reply, groupName not in whitelist, group_name={group_name}")
                    return None
                context["session_id"] = session_id
                context["receiver"] = group_id
            else:
                context["session_id"] = cmsg.other_user_id
                context["receiver"] = cmsg.other_user_id
            # 触发 ON_RECEIVE_MESSAGE 插件事件，插件可拦截或修改消息
            e_context = PluginManager().emit_event(EventContext(Event.ON_RECEIVE_MESSAGE, {"channel": self, "context": context}))
            context = e_context["context"]
            if e_context.is_pass() or context is None:
                # 插件拦截了消息，直接返回（None 表示不处理）
                return context
            # 过滤机器人自己发送的消息（防止死循环），除非配置了 trigger_by_self=True
            if cmsg.from_user_id == self.user_id and not config.get("trigger_by_self", True):
                logger.debug("[chat_channel]self message skipped")
                return None

        # 消息内容匹配过程，并处理content
        # 以下逻辑根据消息类型进行前缀匹配、关键词检查、@提及剥离等，
        if ctype == ContextType.TEXT:
            if first_in and "」\n- - - - - - -" in content:  # 初次匹配 过滤引用消息（微信引用回复特征串）
                logger.debug(content)
                logger.debug("[chat_channel]reference query skipped")
                return None

            nick_name_black_list = conf().get("nick_name_black_list", [])
            if context.get("isgroup", False):  # 群聊
                # 校验关键字
                match_prefix = check_prefix(content, conf().get("group_chat_prefix"))
                match_contain = check_contain(content, conf().get("group_chat_keyword"))
                flag = False
                if context["msg"].to_user_id != context["msg"].actual_user_id:
                    if match_prefix is not None or match_contain is not None:
                        flag = True
                        if match_prefix:
                            content = content.replace(match_prefix, "", 1).strip()
                    if context["msg"].is_at:
                        nick_name = context["msg"].actual_user_nickname
                        if nick_name and nick_name in nick_name_black_list:
                            # 黑名单过滤
                            logger.warning(f"[chat_channel] Nickname {nick_name} in In BlackList, ignore")
                            return None

                        logger.info("[chat_channel]receive group at")
                        if not conf().get("group_at_off", False):
                            # 未关闭 @触发，标记为需回复
                            flag = True
                        self.name = self.name if self.name is not None else ""  # 部分渠道self.name可能没有赋值
                        pattern = f"@{re.escape(self.name)}(\u2005|\u0020)"
                        subtract_res = re.sub(pattern, r"", content)
                        if isinstance(context["msg"].at_list, list):
                            # 同时移除消息中 @其他用户 的提及标记
                            for at in context["msg"].at_list:
                                pattern = f"@{re.escape(at)}(\u2005|\u0020)"
                                subtract_res = re.sub(pattern, r"", subtract_res)
                        if subtract_res == content and context["msg"].self_display_name:
                            # 前缀移除后没有变化，使用群昵称再次移除
                            pattern = f"@{re.escape(context['msg'].self_display_name)}(\u2005|\u0020)"
                            subtract_res = re.sub(pattern, r"", content)
                        content = subtract_res
                if not flag:
                    # 未匹配任何触发条件（前缀/关键词/@），跳过此消息
                    if context["origin_ctype"] == ContextType.VOICE:
                        logger.info("[chat_channel]receive group voice, but checkprefix didn't match")
                    return None
            else:  # 单聊
                # 私聊模式下的触发前缀检查
                nick_name = context["msg"].from_user_nickname
                if nick_name and nick_name in nick_name_black_list:
                    # 黑名单过滤
                    logger.warning(f"[chat_channel] Nickname '{nick_name}' in In BlackList, ignore")
                    return None

                match_prefix = check_prefix(content, conf().get("single_chat_prefix", [""]))
                if match_prefix is not None:  # 判断如果匹配到自定义前缀，则返回过滤掉前缀+空格后的内容
                    content = content.replace(match_prefix, "", 1).strip()
                elif context["origin_ctype"] == ContextType.VOICE:  # 如果源消息是私聊的语音消息，允许不匹配前缀，放宽条件（语音转文字后通常不含前缀）
                    pass
                else:
                    logger.info("[chat_channel]receive single chat msg, but checkprefix didn't match")
                    return None
            content = content.strip()
            # 检查是否匹配图片生成前缀（如"画"、"看"、"找"），匹配则将消息类型改为 IMAGE_CREATE
            img_match_prefix = check_prefix(content, conf().get("image_create_prefix",[""]))
            if img_match_prefix:
                content = content.replace(img_match_prefix, "", 1)
                context.type = ContextType.IMAGE_CREATE
            else:
                context.type = ContextType.TEXT
            context.content = content.strip()
            # 如果配置了 always_reply_voice 且频道支持语音，则标记期望回复类型为语音
            if "desire_rtype" not in context and conf().get("always_reply_voice") and ReplyType.VOICE not in self.NOT_SUPPORT_REPLYTYPE:
                context["desire_rtype"] = ReplyType.VOICE
        elif context.type == ContextType.VOICE:
            if "desire_rtype" not in context and conf().get("voice_reply_voice") and ReplyType.VOICE not in self.NOT_SUPPORT_REPLYTYPE:
                context["desire_rtype"] = ReplyType.VOICE
        return context

    def _handle(self, context: Context):
        """
        消息处理主流程，由线程池 worker 调用。
        流程：生成回复 → 装饰回复 → 发送回复
        """
        if context is None or not context.content:
            return
        logger.debug("[chat_channel] handling context: {}".format(context))
        # reply的构建步骤
        reply = self._generate_reply(context)

        logger.debug("[chat_channel] decorating reply: {}".format(reply))

        # reply的包装步骤
        if reply and reply.content:
            reply = self._decorate_reply(context, reply)

            # reply的发送步骤
            self._send_reply(context, reply)

    def _generate_reply(self, context: Context, reply: Reply = Reply()) -> Reply:
        """
        根据消息上下文生成回复。
        先触发 ON_HANDLE_CONTEXT 插件事件，再按消息类型分发处理：
        - TEXT/IMAGE_CREATE：调用 Bridge 获取 LLM 回复或生成图片
        - VOICE：语音转文字后递归处理
        - IMAGE：缓存图片到 USER_IMAGE_CACHE
        - SHARING/FUNCTION/FILE：暂无默认处理逻辑
        """
        e_context = PluginManager().emit_event(
            EventContext(
                Event.ON_HANDLE_CONTEXT,
                {"channel": self, "context": context, "reply": reply},
            )
        )
        reply = e_context["reply"]
        if not e_context.is_pass():
            logger.debug("[chat_channel] type={}, content={}".format(context.type, context.content))
            if context.type == ContextType.TEXT or context.type == ContextType.IMAGE_CREATE:  # 文字和图片消息
                # 调用 Bridge 获取 LLM 回复（文本或图片生成）
                context["channel"] = e_context["channel"]
                reply = super().build_reply_content(context.content, context)
            elif context.type == ContextType.VOICE:  # 语音消息
                cmsg = context["msg"]
                cmsg.prepare()
                file_path = context.content
                wav_path = os.path.splitext(file_path)[0] + ".wav"
                try:
                    any_to_wav(file_path, wav_path)  # 将音频转换为 WAV 格式以提高语音识别兼容性
                except Exception as e:  # 转换失败，直接使用mp3，对于某些api，mp3也可以识别
                    logger.warning("[chat_channel]any to wav error, use raw path. " + str(e))
                    wav_path = file_path
                # 语音识别
                reply = super().build_voice_to_text(wav_path)
                # 删除临时文件
                try:
                    os.remove(file_path)
                    if wav_path != file_path:
                        os.remove(wav_path)
                except Exception as e:
                    pass
                    # logger.warning("[chat_channel]delete temp file error: " + str(e))

                if reply.type == ReplyType.TEXT:
                    # 语音识别成功，将识别出的文本重新构建上下文并递归处理（走文本回复流程）
                    new_context = self._compose_context(ContextType.TEXT, reply.content, **context.kwargs)
                    if new_context:
                        reply = self._generate_reply(new_context)
                    else:
                        return
            elif context.type == ContextType.IMAGE:  # 图片消息，缓存到 USER_IMAGE_CACHE 供后续 Agent 视觉工具使用
                memory.USER_IMAGE_CACHE[context["session_id"]] = {
                    "path": context.content,
                    "msg": context.get("msg")
                }
            elif context.type == ContextType.SHARING:  # 分享信息，当前无默认逻辑
                pass
            elif context.type == ContextType.FUNCTION or context.type == ContextType.FILE:  # 文件消息及函数调用等，当前无默认逻辑
                pass
            else:
                logger.warning("[chat_channel] unknown context type: {}".format(context.type))
                return
        return reply

    def _decorate_reply(self, context: Context, reply: Reply) -> Reply:
        """
        装饰回复内容，在发送前进行最终包装。
        处理逻辑：
        1. 触发 ON_DECORATE_REPLY 插件事件
        2. 不支持的回复类型转为错误提示
        3. 文本回复：添加前缀/后缀，群聊时 @发送者，如期望语音则转为语音
        4. ERROR/INFO 类型：添加类型标签前缀
        """
        if reply and reply.type:
            e_context = PluginManager().emit_event(
                EventContext(
                    Event.ON_DECORATE_REPLY,
                    {"channel": self, "context": context, "reply": reply},
                )
            )
            reply = e_context["reply"]
            desire_rtype = context.get("desire_rtype")
            if not e_context.is_pass() and reply and reply.type:
                if reply.type in self.NOT_SUPPORT_REPLYTYPE:
                    # 当前频道不支持该回复类型（如终端不支持语音），转为错误提示
                    logger.error("[chat_channel]reply type not support: " + str(reply.type))
                    reply.type = ReplyType.ERROR
                    reply.content = "不支持发送的消息类型: " + str(reply.type)

                if reply.type == ReplyType.TEXT:
                    reply_text = reply.content
                    # 如果期望语音回复且频道支持语音，则将文本转为语音
                    if desire_rtype == ReplyType.VOICE and ReplyType.VOICE not in self.NOT_SUPPORT_REPLYTYPE:
                        reply = super().build_text_to_voice(reply.content)
                        return self._decorate_reply(context, reply)
                    if context.get("isgroup", False):
                        # 群聊回复：@发送者 + 添加前后缀
                        if not context.get("no_need_at", False):
                            reply_text = "@" + context["msg"].actual_user_nickname + "\n" + reply_text.strip()
                        reply_text = conf().get("group_chat_reply_prefix", "") + reply_text + conf().get("group_chat_reply_suffix", "")
                    else:
                        # 私聊回复：添加前后缀
                        reply_text = conf().get("single_chat_reply_prefix", "") + reply_text + conf().get("single_chat_reply_suffix", "")
                    reply.content = reply_text
                elif reply.type == ReplyType.ERROR or reply.type == ReplyType.INFO:
                    # 错误/信息类型：添加类型标签前缀，便于用户识别
                    reply.content = "[" + str(reply.type) + "]\n" + reply.content
                elif reply.type == ReplyType.IMAGE_URL or reply.type == ReplyType.VOICE or reply.type == ReplyType.IMAGE or reply.type == ReplyType.FILE or reply.type == ReplyType.VIDEO or reply.type == ReplyType.VIDEO_URL:
                    # 图片/语音/文件/视频等媒体类型回复，无需额外装饰，直接发送
                    pass
                else:
                    logger.error("[chat_channel] unknown reply type: {}".format(reply.type))
                    return
            # 期望回复类型与实际类型不一致时打警告（如期望语音但实际返回文本）
            if desire_rtype and desire_rtype != reply.type and reply.type not in [ReplyType.ERROR, ReplyType.INFO]:
                logger.warning("[chat_channel] desire_rtype: {}, but reply type: {}".format(context.get("desire_rtype"), reply.type))
            return reply

    def _send_reply(self, context: Context, reply: Reply):
        """
        发送回复的最后一步。触发 ON_SEND_REPLY 插件事件后，根据回复类型选择发送策略：
        - 文本回复：提取其中的图片/视频 URL 单独发送
        - 带 text_content 的图片回复：先发文本，延迟后发图片
        - 其他：直接发送
        """
        if reply and reply.type:
            e_context = PluginManager().emit_event(
                EventContext(
                    Event.ON_SEND_REPLY,
                    {"channel": self, "context": context, "reply": reply},
                )
            )
            reply = e_context["reply"]
            if not e_context.is_pass() and reply and reply.type:
                logger.debug("[chat_channel] sending reply: {}, context: {}".format(reply, context))
                
                # 如果是文本回复，尝试提取其中的图片/视频 URL 并单独发送
                if reply.type == ReplyType.TEXT:
                    self._extract_and_send_images(reply, context)
                # 如果是图片回复但带有文本内容，先发文本再发图片
                elif reply.type == ReplyType.IMAGE_URL and hasattr(reply, 'text_content') and reply.text_content:
                    # 先发送文本
                    text_reply = Reply(ReplyType.TEXT, reply.text_content)
                    self._send(text_reply, context)
                    # 短暂延迟后发送图片
                    time.sleep(0.3)
                    self._send(reply, context)
                else:
                    self._send(reply, context)
    
    def _extract_and_send_images(self, reply: Reply, context: Context):
        """
        从文本回复中提取图片/视频URL并单独发送。
        支持格式：[图片: /path/to/image.png], [视频: /path/to/video.mp4], ![](url), <img src="url">
        最多发送5个媒体文件，超出的部分忽略。
        先发送文本内容，再逐个发送提取到的媒体文件，间隔 0.5 秒避免频率限制。
        """
        content = reply.content
        media_items = []  # [(url, type), ...]
        
        # 正则提取各种格式的媒体URL
        patterns = [
            (r'\[图片:\s*([^\]]+)\]', 'image'),   # [图片: /path/to/image.png]
            (r'\[视频:\s*([^\]]+)\]', 'video'),   # [视频: /path/to/video.mp4]
            (r'!\[.*?\]\(([^\)]+)\)', 'image'),   # ![alt](url) - 默认图片
            (r'<img[^>]+src=["\']([^"\']+)["\']', 'image'),  # <img src="url">
            (r'<video[^>]+src=["\']([^"\']+)["\']', 'video'),  # <video src="url">
            (r'https?://[^\s]+\.(?:jpg|jpeg|png|gif|webp)', 'image'),  # 直接的图片URL
            (r'https?://[^\s]+\.(?:mp4|avi|mov|wmv|flv)', 'video'),  # 直接的视频URL
        ]
        
        for pattern, media_type in patterns:
            matches = re.findall(pattern, content, re.IGNORECASE)
            for match in matches:
                media_items.append((match, media_type))
        
        # 去重（保持顺序）并限制最多5个
        seen = set()
        unique_items = []
        for url, mtype in media_items:
            if url not in seen:
                seen.add(url)
                unique_items.append((url, mtype))
        media_items = unique_items[:5]
        
        if media_items:
            logger.info(f"[chat_channel] Extracted {len(media_items)} media item(s) from reply")
            
            # 先发送文本（保持原文本不变）
            logger.info(f"[chat_channel] Sending text content before media: {reply.content[:100]}...")
            self._send(reply, context)
            logger.info(f"[chat_channel] Text sent, now sending {len(media_items)} media item(s)")
            
            # 然后逐个发送媒体文件
            for i, (url, media_type) in enumerate(media_items):
                try:
                    # 判断是本地文件还是URL
                    if url.startswith(('http://', 'https://')):
                        # 网络资源
                        if media_type == 'video':
                            # 视频使用 FILE 类型发送
                            media_reply = Reply(ReplyType.FILE, url)
                            media_reply.file_name = os.path.basename(url)
                        else:
                            # 图片使用 IMAGE_URL 类型
                            media_reply = Reply(ReplyType.IMAGE_URL, url)
                    elif os.path.exists(url):
                        # 本地文件
                        if media_type == 'video':
                            # 视频使用 FILE 类型，转换为 file:// URL
                            media_reply = Reply(ReplyType.FILE, f"file://{url}")
                            media_reply.file_name = os.path.basename(url)
                        else:
                            # 图片使用 IMAGE_URL 类型，转换为 file:// URL
                            media_reply = Reply(ReplyType.IMAGE_URL, f"file://{url}")
                    else:
                        logger.warning(f"[chat_channel] Media file not found or invalid URL: {url}")
                        continue
                    
                    # 发送媒体文件（添加小延迟避免频率限制）
                    if i > 0:
                        time.sleep(0.5)
                    self._send(media_reply, context)
                    logger.info(f"[chat_channel] Sent {media_type} {i+1}/{len(media_items)}: {url[:50]}...")
                    
                except Exception as e:
                    logger.error(f"[chat_channel] Failed to send {media_type} {url}: {e}")
        else:
            # 没有媒体文件，正常发送文本
                self._send(reply, context)

    def _send(self, reply: Reply, context: Context, retry_cnt=0):
        """
        实际发送回复消息，调用子类实现的 send() 方法。
        发送失败时自动重试，最多重试 2 次，重试间隔递增（3s + 3*retry_cnt）。
        NotImplementedError 不重试（子类未实现 send 方法时直接放弃）。
        """
        try:
            self.send(reply, context)
        except Exception as e:
            logger.error("[chat_channel] sendMsg error: {}".format(str(e)))
            if isinstance(e, NotImplementedError):
                return
            logger.exception(e)
            if retry_cnt < 2:
                time.sleep(3 + 3 * retry_cnt)
                self._send(reply, context, retry_cnt + 1)

    def _success_callback(self, session_id, **kwargs):  # 线程正常结束时的回调函数
        """消息处理成功的回调，仅记录调试日志"""
        logger.debug("Worker return success, session_id = {}".format(session_id))

    def _fail_callback(self, session_id, exception, **kwargs):  # 线程异常结束时的回调函数
        """消息处理异常的回调，记录异常堆栈"""
        logger.exception("Worker return exception: {}".format(exception))

    def _thread_pool_callback(self, session_id, **kwargs):
        """
        线程池任务完成后的回调工厂函数。
        根据任务执行结果调用成功/失败回调，并释放该 session 的并发信号量，
        允许同一会话的下一条消息被处理。
        """
        def func(worker: Future):
            try:
                worker_exception = worker.exception()
                if worker_exception:
                    self._fail_callback(session_id, exception=worker_exception, **kwargs)
                else:
                    self._success_callback(session_id, **kwargs)
            except CancelledError as e:
                logger.info("Worker cancelled, session_id = {}".format(session_id))
            except Exception as e:
                logger.exception("Worker raise exception: {}".format(e))
            with self.lock:
                self.sessions[session_id][1].release()  # 释放信号量，允许该会话处理下一条消息

        return func

    def produce(self, context: Context):
        """
        将消息上下文投入对应 session 的消息队列（生产者）。
        如果 session 不存在则自动创建（包含 Dequeue 和 BoundedSemaphore）。
        以 # 开头的文本消息（管理命令）会被插入队列头部优先处理。
        """
        session_id = context["session_id"]
        with self.lock:
            if session_id not in self.sessions:
                # 新会话：创建消息队列和并发控制信号量
                self.sessions[session_id] = [
                    Dequeue(),
                    threading.BoundedSemaphore(conf().get("concurrency_in_session", 1)),
                ]
            if context.type == ContextType.TEXT and context.content.startswith("#"):
                self.sessions[session_id][0].putleft(context)  # 优先处理管理命令（插队到队首）
            else:
                self.sessions[session_id][0].put(context)

    # 消费者函数，单独线程，用于从消息队列中取出消息并处理
    def consume(self):
        """
        消息消费循环，在守护线程中持续运行。
        每 200ms 遍历所有 session 的消息队列，尝试获取信号量后提交任务到线程池。
        当某 session 队列空且无正在执行的任务时，自动清理该 session 的资源。
        """
        while True:
            with self.lock:
                session_ids = list(self.sessions.keys())
            for session_id in session_ids:
                with self.lock:
                    context_queue, semaphore = self.sessions[session_id]
                if semaphore.acquire(blocking=False):  # 非阻塞获取信号量，获取不到说明该 session 有任务正在处理
                    if not context_queue.empty():
                        # 队列有消息且信号量获取成功，提交到线程池处理
                        context = context_queue.get()
                        logger.debug("[chat_channel] consume context: {}".format(context))
                        future: Future = handler_pool.submit(self._handle, context)
                        future.add_done_callback(self._thread_pool_callback(session_id, context=context))
                        with self.lock:
                            if session_id not in self.futures:
                                self.futures[session_id] = []
                            self.futures[session_id].append(future)
                    elif semaphore._initial_value == semaphore._value + 1:  # 除了当前，没有任务再申请到信号量，说明所有任务都处理完毕
                        # 会话队列已空且无正在执行的任务，清理该 session 的资源
                        with self.lock:
                            self.futures[session_id] = [t for t in self.futures[session_id] if not t.done()]
                            assert len(self.futures[session_id]) == 0, "thread pool error"
                            del self.sessions[session_id]
                    else:
                        # 队列为空但信号量已获取，释放信号量让其他消费者使用
                        semaphore.release()
            time.sleep(0.2)  # 轮询间隔 200ms，平衡响应速度和 CPU 占用

    # 取消session_id对应的所有任务，只能取消排队的消息和已提交线程池但未执行的任务
    def cancel_session(self, session_id):
        """
        取消指定会话的所有排队消息和未执行的任务。
        已开始执行的任务无法取消（线程池 Future.cancel() 仅对未开始的任务有效）。
        清空该 session 的消息队列。
        """
        with self.lock:
            if session_id in self.sessions:
                for future in self.futures[session_id]:
                    future.cancel()
                cnt = self.sessions[session_id][0].qsize()
                if cnt > 0:
                    logger.info("Cancel {} messages in session {}".format(cnt, session_id))
                self.sessions[session_id][0] = Dequeue()

    def cancel_all_session(self):
        """取消所有会话的排队消息和未执行任务"""
        with self.lock:
            for session_id in self.sessions:
                for future in self.futures[session_id]:
                    future.cancel()
                cnt = self.sessions[session_id][0].qsize()
                if cnt > 0:
                    logger.info("Cancel {} messages in session {}".format(cnt, session_id))
                self.sessions[session_id][0] = Dequeue()


def check_prefix(content, prefix_list):
    """
    检查消息内容是否以指定前缀列表中的任一项开头。
    返回匹配到的前缀字符串，未匹配则返回 None。
    """
    if not prefix_list:
        return None
    for prefix in prefix_list:
        if content.startswith(prefix):
            return prefix
    return None


def check_contain(content, keyword_list):
    """
    检查消息内容是否包含指定关键词列表中的任一项。
    包含则返回 True，不包含则返回 None。
    """
    if not keyword_list:
        return None
    for ky in keyword_list:
        if content.find(ky) != -1:
            return True
    return None
