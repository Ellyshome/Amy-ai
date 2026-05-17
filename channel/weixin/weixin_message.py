"""
微信ChatMessage实现模块。

将从getUpdates API获取的微信消息解析为统一的ChatMessage格式，
支持以下消息项类型：
- 文本消息（ITEM_TEXT）
- 图片消息（ITEM_IMAGE）
- 语音消息（ITEM_VOICE）
- 文件消息（ITEM_FILE）
- 视频消息（ITEM_VIDEO）

同时支持引用消息的解析，当用户引用一条消息发送文本时，
会解析引用的原始消息内容并附加到文本中。
"""

import os
import uuid

from bridge.context import ContextType
from channel.chat_message import ChatMessage
from channel.weixin.weixin_api import download_media_from_cdn, CDN_BASE_URL
from common.log import logger
from common.utils import expand_path
from config import conf


# 微信协议中的消息项类型常量
# MessageItemType constants from the Weixin protocol
ITEM_TEXT = 1    # 文本消息项
ITEM_IMAGE = 2   # 图片消息项
ITEM_VOICE = 3   # 语音消息项
ITEM_FILE = 4    # 文件消息项
ITEM_VIDEO = 5   # 视频消息项


def _get_tmp_dir() -> str:
    """获取临时文件目录的绝对路径，如果不存在则创建。

    使用配置项agent_workspace指定的目录作为工作空间根目录，
    在其下创建tmp子目录用于存放下载的媒体文件。

    Returns:
        临时文件目录的绝对路径
    """
    ws_root = expand_path(conf().get("agent_workspace", "~/cow"))
    tmp_dir = os.path.join(ws_root, "tmp")
    os.makedirs(tmp_dir, exist_ok=True)
    return tmp_dir


class WeixinMessage(ChatMessage):
    """微信消息封装类。

    将微信ilink协议的消息格式解析为统一的ChatMessage格式，
    处理微信特有的消息结构：
    - item_list: 一条消息可能包含多个消息项（文本+图片的组合）
    - ref_msg: 引用消息，用户可能引用一条消息发送回复
    - CDN加密媒体: 图片/文件/视频通过CDN加密传输

    对于媒体类型消息，采用延迟下载策略：
    不在构造时立即下载，而是将下载函数赋值给_prepare_fn，
    在调用prepare()方法时才执行下载，避免不必要的网络请求。
    """

    def __init__(self, msg: dict, cdn_base_url: str = CDN_BASE_URL):
        """初始化微信消息。

        解析微信消息的各项字段，确定消息类型和内容：
        1. 提取基本字段：消息ID、时间戳、context_token等
        2. 设置发送者和接收者信息
        3. 解析消息项列表（item_list），确定文本内容和媒体类型
        4. 处理引用消息（ref_msg）
        5. 根据解析结果设置ctype和content

        微信消息的特殊之处：
        - 一条消息可能同时包含文本和媒体（如图片+说明文字）
        - 引用消息中的媒体也需要下载
        - 语音消息可能包含语音转文字结果

        Args:
            msg: 从getUpdates获取的原始消息字典
            cdn_base_url: CDN基础URL，用于下载加密媒体文件
        """
        super().__init__(msg)

        # 消息ID：优先使用message_id，备选seq，都没有则生成随机ID
        self.msg_id = str(msg.get("message_id", msg.get("seq", uuid.uuid4().hex[:8])))
        # 消息创建时间（毫秒时间戳）
        self.create_time = msg.get("message_create_time_ms", 0)
        # 上下文令牌，回复消息时必须携带
        self.context_token = msg.get("context_token", "")
        # 微信ilink插件仅支持单聊，不支持群聊
        self.is_group = False  # Weixin plugin only supports direct chat
        self.is_at = False

        # 提取发送者和接收者ID
        from_user_id = msg.get("from_user_id", "")
        to_user_id = msg.get("to_user_id", "")

        # 设置各种用户ID映射
        self.from_user_id = from_user_id
        self.from_user_nickname = from_user_id
        self.to_user_id = to_user_id
        self.to_user_nickname = to_user_id
        # other_user_id设为发送者（因为我们是接收方，回复时发送者就是对方）
        self.other_user_id = from_user_id
        self.other_user_nickname = from_user_id
        self.actual_user_id = from_user_id
        self.actual_user_nickname = from_user_id

        # 解析消息项列表
        item_list = msg.get("item_list", [])

        # Parse items: find text and media
        # 遍历消息项，提取文本和媒体内容
        text_body = ""       # 文本内容
        media_item = None    # 媒体消息项（图片/文件/视频/语音）
        media_type = None    # 媒体类型
        ref_text = ""        # 引用消息的文本

        for item in item_list:
            itype = item.get("type", 0)

            if itype == ITEM_TEXT:
                # 文本消息项
                text_item = item.get("text_item", {})
                text_body = text_item.get("text", "")

                # 处理引用消息
                ref = item.get("ref_msg")
                if ref:
                    # 提取引用消息的标题和内容
                    ref_title = ref.get("title", "")
                    ref_mi = ref.get("message_item", {})
                    ref_body = ""
                    if ref_mi.get("type") == ITEM_TEXT:
                        ref_body = ref_mi.get("text_item", {}).get("text", "")
                    if ref_title or ref_body:
                        parts = [p for p in [ref_title, ref_body] if p]
                        ref_text = f"[引用: {' | '.join(parts)}]\n"
                    # If ref is a media item, treat it as the media to download
                    # 如果引用的是媒体消息（图片/视频/文件），也需要下载该媒体
                    if ref_mi.get("type") in (ITEM_IMAGE, ITEM_VIDEO, ITEM_FILE):
                        media_item = ref_mi
                        media_type = ref_mi.get("type")

            elif itype == ITEM_VOICE:
                # 语音消息项：优先使用语音转文字结果
                voice_item = item.get("voice_item", {})
                voice_text = voice_item.get("text", "")
                if voice_text:
                    # 语音已转为文字，直接使用
                    text_body = voice_text
                else:
                    # Voice without transcription - download the audio
                    # 语音未转文字，需要下载音频文件
                    media_item = item
                    media_type = ITEM_VOICE

            elif itype in (ITEM_IMAGE, ITEM_VIDEO, ITEM_FILE):
                # 媒体消息项：只取第一个媒体（一条消息通常只有一个媒体项）
                if not media_item:
                    media_item = item
                    media_type = itype

        # Determine ctype and content
        # 根据文本和媒体的存在情况确定消息类型和内容
        if media_item and not text_body:
            # 仅有媒体，无文本：设置为对应的媒体类型
            self._setup_media(media_item, media_type, cdn_base_url)
        elif media_item and text_body:
            # Text + media: download media, attach as file ref in text
            # 同时有文本和媒体：下载媒体后将路径引用附加到文本中
            self.ctype = ContextType.TEXT
            media_path = self._download_media(media_item, media_type, cdn_base_url)
            if media_path:
                if media_type == ITEM_IMAGE:
                    text_body += f"\n[图片: {media_path}]"
                elif media_type == ITEM_VIDEO:
                    text_body += f"\n[视频: {media_path}]"
                else:
                    text_body += f"\n[文件: {media_path}]"
            # 将引用文本和正文组合
            self.content = ref_text + text_body
        else:
            # 仅文本，无媒体
            self.ctype = ContextType.TEXT
            self.content = ref_text + text_body

    def _setup_media(self, item: dict, media_type: int, cdn_base_url: str):
        """设置消息为媒体类型，使用延迟下载策略。

        不同媒体类型的处理方式：
        - 图片(ITEM_IMAGE): 立即下载（需要路径用于文件缓存），失败时降级为文本
        - 视频(ITEM_VIDEO): 延迟下载，设置_prepare_fn
        - 文件(ITEM_FILE): 延迟下载，保留原始文件名
        - 语音(ITEM_VOICE): 延迟下载，保存为silk格式

        延迟下载的好处：
        - 如果消息被过滤或不需要处理，可以避免不必要的网络请求
        - 对于大文件，延迟到真正需要时才下载

        Args:
            item: 媒体消息项字典
            media_type: 媒体类型常量（ITEM_IMAGE/ITEM_VIDEO/ITEM_FILE/ITEM_VOICE）
            cdn_base_url: CDN基础URL
        """
        if media_type == ITEM_IMAGE:
            # 图片消息：立即下载，因为需要图片路径用于后续的文件缓存机制
            self.ctype = ContextType.IMAGE
            image_path = self._download_media(item, ITEM_IMAGE, cdn_base_url)
            if image_path:
                self.content = image_path
                self.image_path = image_path
            else:
                # 图片下载失败，降级为文本消息
                self.ctype = ContextType.TEXT
                self.content = "[Image download failed]"

        elif media_type == ITEM_VIDEO:
            # 视频消息：延迟下载，使用_prepare_fn
            self.ctype = ContextType.FILE
            save_path = os.path.join(_get_tmp_dir(), f"wx_{self.msg_id}.mp4")
            self.content = save_path

            def _download():
                """延迟下载视频的闭包函数，更新content为实际下载路径。"""
                path = self._download_media(item, ITEM_VIDEO, cdn_base_url)
                if path:
                    self.content = path
            self._prepare_fn = _download

        elif media_type == ITEM_FILE:
            # 文件消息：延迟下载，保留原始文件名
            self.ctype = ContextType.FILE
            # 使用消息中的原始文件名，方便用户识别
            file_name = item.get("file_item", {}).get("file_name", f"wx_{self.msg_id}")
            save_path = os.path.join(_get_tmp_dir(), file_name)
            self.content = save_path

            def _download():
                """延迟下载文件的闭包函数，更新content为实际下载路径。"""
                path = self._download_media(item, ITEM_FILE, cdn_base_url)
                if path:
                    self.content = path
            self._prepare_fn = _download

        elif media_type == ITEM_VOICE:
            # 语音消息：延迟下载，保存为silk格式（微信语音编码格式）
            self.ctype = ContextType.VOICE
            save_path = os.path.join(_get_tmp_dir(), f"wx_{self.msg_id}.silk")
            self.content = save_path

            def _download():
                """延迟下载语音的闭包函数，更新content为实际下载路径。"""
                path = self._download_media(item, ITEM_VOICE, cdn_base_url)
                if path:
                    self.content = path
            self._prepare_fn = _download

    def _download_media(self, item: dict, media_type: int, cdn_base_url: str) -> str:
        """从CDN下载并解密媒体文件。

        从消息项中提取CDN下载所需的加密参数和AES密钥，
        调用download_media_from_cdn完成下载和解密。

        AES密钥可能存在于两个位置：
        - 部分消息格式中，密钥在image_item.aeskey（十六进制格式）
        - 标准格式中，密钥在media.aes_key（base64格式）

        Args:
            item: 媒体消息项字典
            media_type: 媒体类型常量
            cdn_base_url: CDN基础URL

        Returns:
            下载后的本地文件路径，失败时返回空字符串
        """
        # 各媒体类型对应的JSON键名映射
        type_key_map = {
            ITEM_IMAGE: "image_item",
            ITEM_VIDEO: "video_item",
            ITEM_FILE: "file_item",
            ITEM_VOICE: "voice_item",
        }
        key = type_key_map.get(media_type, "")
        info = item.get(key, {})
        media = info.get("media", {})

        # 提取CDN加密参数
        encrypt_param = media.get("encrypt_query_param", "")
        # aes_key can be in image_item.aeskey (hex) or media.aes_key (b64)
        # AES密钥可能存在于两个位置，优先使用info中的aeskey，备选media中的aes_key
        aes_key = info.get("aeskey", "") or media.get("aes_key", "")

        if not encrypt_param or not aes_key:
            logger.warning(f"[Weixin] Missing CDN params for media download (type={media_type})")
            return ""

        # 确定保存路径
        if media_type == ITEM_FILE:
            # 文件类型使用原始文件名
            original_name = info.get("file_name", "")
            if original_name:
                save_path = os.path.join(_get_tmp_dir(), original_name)
            else:
                save_path = os.path.join(_get_tmp_dir(), f"wx_{self.msg_id}.bin")
        else:
            # 其他类型使用消息ID加对应扩展名
            ext_map = {ITEM_IMAGE: ".jpg", ITEM_VIDEO: ".mp4", ITEM_VOICE: ".silk"}
            ext = ext_map.get(media_type, "")
            save_path = os.path.join(_get_tmp_dir(), f"wx_{self.msg_id}{ext}")

        try:
            # 调用CDN下载函数，自动处理加密参数解析和解密
            download_media_from_cdn(cdn_base_url, encrypt_param, aes_key, save_path)
            logger.info(f"[Weixin] Media downloaded: {save_path}")
            return save_path
        except Exception as e:
            logger.error(f"[Weixin] Media download failed: {e}")
            return ""
