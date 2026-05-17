"""
企业微信AI机器人消息解析模块。

解析企业微信WebSocket长连接模式下收到的消息体，
将其转换为统一的ChatMessage格式，支持以下消息类型：
- 文本消息（text）
- 语音消息（voice，转为文本）
- 图片消息（image，下载并解密）
- 混合消息（mixed，包含文本和图片的组合消息）
- 文件消息（file，下载并解密）
- 视频消息（video，下载并解密）

企业微信的媒体文件使用AES-256-CBC加密传输，
需要使用消息体中提供的aeskey进行解密。
"""

import os
import re
import base64
import requests

from bridge.context import ContextType
from channel.chat_message import ChatMessage
from common.log import logger
from common.utils import expand_path
from config import conf
from Crypto.Cipher import AES


# 文件魔数（Magic Bytes）签名表，用于通过文件头部字节判断文件类型
# 每个元组格式为：(魔数字节, 对应扩展名)
MAGIC_SIGNATURES = [
    (b"%PDF", ".pdf"),
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"\xff\xd8\xff", ".jpg"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
    (b"RIFF", ".webp"),  # RIFF....WEBP, further checked below
    (b"PK\x03\x04", ".zip"),  # zip / docx / xlsx / pptx
    (b"\x1f\x8b", ".gz"),
    (b"Rar!\x1a\x07", ".rar"),
    (b"7z\xbc\xaf\x27\x1c", ".7z"),
    (b"\x00\x00\x00", ".mp4"),  # ftyp box, further checked below
    (b"#!AMR", ".amr"),
]

# Office文件在ZIP包内的特征标记
# 当ZIP文件内部包含这些路径时，可以判断为对应的Office文档格式
OFFICE_ZIP_MARKERS = {
    b"word/": ".docx",
    b"xl/": ".xlsx",
    b"ppt/": ".pptx",
}


def _guess_ext_from_bytes(data: bytes) -> str:
    """通过文件内容的魔数字节猜测文件扩展名。

    读取文件头部的字节特征，与预定义的签名表进行匹配。
    对于某些格式需要额外的验证：
    - WebP: 需要确认偏移8-12位为"WEBP"
    - MP4: 需要确认偏移4-12位包含"ftyp"
    - ZIP: 需要检查内部是否包含Office文件特征

    Args:
        data: 文件的二进制内容，至少需要8字节

    Returns:
        猜测的文件扩展名（如".jpg"），无法识别时返回空字符串
    """
    if not data or len(data) < 8:
        return ""
    for sig, ext in MAGIC_SIGNATURES:
        if data[:len(sig)] == sig:
            # WebP需要进一步验证：偏移8-12位必须是"WEBP"
            if ext == ".webp" and data[8:12] != b"WEBP":
                continue
            # MP4需要进一步验证：偏移4-12位必须包含"ftyp"（文件类型框）
            if ext == ".mp4":
                if b"ftyp" not in data[4:12]:
                    continue
            # ZIP文件可能是Office文档，检查内部特征
            if ext == ".zip":
                for marker, office_ext in OFFICE_ZIP_MARKERS.items():
                    if marker in data[:2000]:
                        return office_ext
                return ".zip"
            return ext
    return ""


def _decrypt_media(url: str, aeskey: str) -> bytes:
    """下载并解密企业微信的AES-256-CBC加密媒体文件。

    企业微信的图片、文件等媒体内容通过AES-256-CBC加密传输，
    加密使用的密钥和初始向量(IV)都来自消息体中的aeskey字段：
    - 密钥: base64解码aeskey得到32字节（256位）密钥
    - IV: 取密钥的前16字节作为初始向量
    - 填充: PKCS7填充

    Args:
        url: 加密媒体文件的下载URL
        aeskey: base64编码的AES密钥（可能需要补齐padding字符）

    Returns:
        解密后的原始文件字节数据

    Raises:
        ValueError: AES密钥长度不正确或PKCS7填充无效时抛出
    """
    # 下载加密的媒体数据
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    encrypted = resp.content

    # 解码AES密钥：base64解码，可能需要补齐padding字符
    key = base64.b64decode(aeskey + "=" * (-len(aeskey) % 4))
    # 校验密钥长度：AES-256需要32字节密钥
    if len(key) != 32:
        raise ValueError(f"Invalid AES key length: {len(key)}, expected 32")

    # IV取密钥的前16字节（企业微信的约定）
    iv = key[:16]
    # 使用AES-256-CBC模式解密
    cipher = AES.new(key, AES.MODE_CBC, iv)
    decrypted = cipher.decrypt(encrypted)

    # 去除PKCS7填充
    pad_len = decrypted[-1]
    # 填充长度不应超过块大小（32字节），否则说明数据可能损坏
    if pad_len > 32:
        raise ValueError(f"Invalid PKCS7 padding length: {pad_len}")
    return decrypted[:-pad_len]


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


class WecomBotMessage(ChatMessage):
    """企业微信机器人消息封装类。

    将企业微信WebSocket消息体解析为统一的ChatMessage格式，
    处理不同消息类型的解析逻辑：
    - text: 纯文本消息，群聊时去除@提及
    - voice: 语音转文本消息
    - image: 图片消息，下载解密后保存到本地
    - mixed: 混合消息，包含文本和图片的组合
    - file: 文件消息，使用延迟下载（prepare方法触发）
    - video: 视频消息，使用延迟下载（prepare方法触发）

    对于文件和视频消息，采用延迟下载策略：
    不在构造时立即下载，而是将下载函数赋值给_prepare_fn，
    在需要时调用prepare()方法触发下载，避免不必要的网络请求。
    """

    def __init__(self, msg_body: dict, is_group: bool = False):
        """初始化企业微信机器人消息。

        根据消息类型(msgtype)解析不同的字段和内容：
        - 提取发送者ID、会话ID、机器人ID等基本信息
        - 根据消息类型设置ctype（上下文类型）和content（内容）
        - 对于媒体类型消息，下载或准备下载函数

        Args:
            msg_body: 企业微信WebSocket消息体字典
            is_group: 是否为群聊消息，默认False（单聊）

        Raises:
            NotImplementedError: 遇到不支持的消息类型时抛出
        """
        super().__init__(msg_body)
        # 消息唯一标识，用于去重
        self.msg_id = msg_body.get("msgid")
        # 消息创建时间
        self.create_time = msg_body.get("create_time")
        # 是否为群聊消息
        self.is_group = is_group

        # 提取消息类型和发送者信息
        msg_type = msg_body.get("msgtype")
        from_userid = msg_body.get("from", {}).get("userid", "")
        chat_id = msg_body.get("chatid", "")
        bot_id = msg_body.get("aibotid", "")

        if msg_type == "text":
            # 文本消息：直接提取文本内容
            self.ctype = ContextType.TEXT
            content = msg_body.get("text", {}).get("content", "")
            # 群聊消息需要去除@提及，避免@机器人名称干扰后续处理
            if is_group:
                content = re.sub(r"@\S+\s*", "", content).strip()
            self.content = content

        elif msg_type == "voice":
            # 语音消息：企业微信已做语音转文字，直接使用转换结果
            self.ctype = ContextType.TEXT
            self.content = msg_body.get("voice", {}).get("content", "")

        elif msg_type == "image":
            # 图片消息：下载并解密图片文件
            self.ctype = ContextType.IMAGE
            image_info = msg_body.get("image", {})
            image_url = image_info.get("url", "")
            aeskey = image_info.get("aeskey", "")
            tmp_dir = _get_tmp_dir()
            # 使用消息ID作为文件名，避免重复
            image_path = os.path.join(tmp_dir, f"wecom_{self.msg_id}.png")

            try:
                # 下载并解密图片数据，企业微信图片使用AES-256-CBC加密
                data = _decrypt_media(image_url, aeskey)
                with open(image_path, "wb") as f:
                    f.write(data)
                self.content = image_path
                self.image_path = image_path
                logger.info(f"[WecomBot] Image downloaded: {image_path}")
            except Exception as e:
                logger.error(f"[WecomBot] Failed to download image: {e}")
                self.content = "[Image download failed]"
                self.image_path = None

        elif msg_type == "mixed":
            # 混合消息：可能同时包含文本和图片，需要逐项解析
            self.ctype = ContextType.TEXT
            text_parts = []  # 文本部分列表
            image_paths = []  # 图片路径列表
            mixed_items = msg_body.get("mixed", {}).get("msg_item", [])
            tmp_dir = _get_tmp_dir()

            for idx, item in enumerate(mixed_items):
                item_type = item.get("msgtype")
                if item_type == "text":
                    # 提取文本内容，群聊时去除@提及
                    txt = item.get("text", {}).get("content", "")
                    if is_group:
                        txt = re.sub(r"@\S+\s*", "", txt).strip()
                    if txt:
                        text_parts.append(txt)
                elif item_type == "image":
                    # 下载并解密图片
                    img_info = item.get("image", {})
                    img_url = img_info.get("url", "")
                    img_aeskey = img_info.get("aeskey", "")
                    # 使用消息ID+索引作为文件名，避免多图冲突
                    img_path = os.path.join(tmp_dir, f"wecom_{self.msg_id}_{idx}.png")
                    try:
                        img_data = _decrypt_media(img_url, img_aeskey)
                        with open(img_path, "wb") as f:
                            f.write(img_data)
                        image_paths.append(img_path)
                    except Exception as e:
                        logger.error(f"[WecomBot] Failed to download mixed image: {e}")

            # 将文本和图片引用组合为最终内容
            content_parts = text_parts[:]
            for p in image_paths:
                content_parts.append(f"[图片: {p}]")
            self.content = "\n".join(content_parts) if content_parts else "[Mixed message]"

        elif msg_type == "file":
            # 文件消息：设置延迟下载，不在构造时立即下载
            # 原因是文件可能较大，且可能不需要下载（如消息被过滤时）
            self.ctype = ContextType.FILE
            file_info = msg_body.get("file", {})
            file_url = file_info.get("url", "")
            aeskey = file_info.get("aeskey", "")
            tmp_dir = _get_tmp_dir()
            # 先使用不含扩展名的路径作为占位，下载后根据内容确定扩展名
            base_path = os.path.join(tmp_dir, f"wecom_{self.msg_id}")
            self.content = base_path

            def _download_file():
                """延迟下载文件的闭包函数。

                下载并解密文件，通过魔数字节猜测文件类型并添加扩展名。
                """
                try:
                    data = _decrypt_media(file_url, aeskey)
                    # 通过文件内容魔数猜测正确的扩展名
                    ext = _guess_ext_from_bytes(data)
                    final_path = base_path + ext
                    with open(final_path, "wb") as f:
                        f.write(data)
                    # 更新content为最终的文件路径（含扩展名）
                    self.content = final_path
                    logger.info(f"[WecomBot] File downloaded: {final_path}")
                except Exception as e:
                    logger.error(f"[WecomBot] Failed to download file: {e}")
            # 将下载函数赋值给_prepare_fn，调用prepare()时执行
            self._prepare_fn = _download_file

        elif msg_type == "video":
            # 视频消息：同样使用延迟下载策略
            self.ctype = ContextType.FILE
            video_info = msg_body.get("video", {})
            video_url = video_info.get("url", "")
            aeskey = video_info.get("aeskey", "")
            tmp_dir = _get_tmp_dir()
            self.content = os.path.join(tmp_dir, f"wecom_{self.msg_id}.mp4")

            def _download_video():
                """延迟下载视频的闭包函数。"""
                try:
                    data = _decrypt_media(video_url, aeskey)
                    with open(self.content, "wb") as f:
                        f.write(data)
                    logger.info(f"[WecomBot] Video downloaded: {self.content}")
                except Exception as e:
                    logger.error(f"[WecomBot] Failed to download video: {e}")
            self._prepare_fn = _download_video

        else:
            # 不支持的消息类型，抛出NotImplementedError让调用方处理
            raise NotImplementedError(f"Unsupported message type: {msg_type}")

        # 设置消息的发送者和接收者信息
        self.from_user_id = from_userid
        self.to_user_id = bot_id
        # 根据是否为群聊设置不同的用户ID映射
        if is_group:
            # 群聊：other_user_id为群ID，actual_user_id为实际发送者
            self.other_user_id = chat_id
            self.actual_user_id = from_userid
            self.actual_user_nickname = from_userid
        else:
            # 单聊：other_user_id为对方（发送者），actual_user_id也是发送者
            self.other_user_id = from_userid
            self.actual_user_id = from_userid
