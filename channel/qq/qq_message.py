import os
import requests

from bridge.context import ContextType
from channel.chat_message import ChatMessage
from common.log import logger
from common.utils import expand_path
from config import conf


def _get_tmp_dir() -> str:
    """
    获取工作空间的临时目录路径（绝对路径），如果不存在则自动创建。

    该函数为QQ消息处理提供统一的临时文件存储目录。
    所有下载的图片等临时文件都存放在此目录下。

    为什么使用独立的工作空间tmp目录而非系统临时目录：
    1. 便于统一管理和清理临时文件
    2. 避免系统临时目录权限问题
    3. 与项目的agent_workspace配置保持一致，方便用户自定义存储位置

    Returns:
        str: 临时目录的绝对路径
    """
    ws_root = expand_path(conf().get("agent_workspace", "~/cow"))
    tmp_dir = os.path.join(ws_root, "tmp")
    os.makedirs(tmp_dir, exist_ok=True)
    return tmp_dir


class QQMessage(ChatMessage):
    """
    QQ消息封装类，将QQ Bot的事件数据转换为系统统一的ChatMessage格式。

    该类负责解析QQ Bot的四种消息事件类型：
    1. GROUP_AT_MESSAGE_CREATE: 群聊@机器人消息
    2. C2C_MESSAGE_CREATE: C2C私聊消息
    3. AT_MESSAGE_CREATE: 频道@机器人消息
    4. DIRECT_MESSAGE_CREATE: 频道私信消息

    消息类型映射：
    - 纯图片附件 -> ContextType.IMAGE：缓存图片等待后续文本消息
    - 图文混合 -> ContextType.TEXT：图片下载后以[图片: path]格式嵌入文本
    - 纯文本 -> ContextType.TEXT：直接处理

    图片处理策略：
    与钉钉和飞书通道一致，采用"图片缓存+文本关联"的多模态交互模式：
    - 单张图片不直接处理，缓存路径等待用户后续的文本提问
    - 图文混合消息则立即将图片路径嵌入文本中一起处理
    """

    def __init__(self, event_data: dict, event_type: str):
        """
        初始化QQ消息对象。

        Args:
            event_data: QQ事件数据字典，包含消息ID、内容、附件、发送者等信息
            event_type: 事件类型字符串，决定消息的解析方式和用户ID的映射策略
        """
        super().__init__(event_data)
        # 消息基础属性
        self.msg_id = event_data.get("id", "")            # 消息唯一ID，用于去重和回复
        self.create_time = event_data.get("timestamp", "") # 消息创建时间
        # 群聊@消息类型为GROUP_AT_MESSAGE_CREATE
        self.is_group = event_type in ("GROUP_AT_MESSAGE_CREATE",)
        self.event_type = event_type  # 保存事件类型，用于后续发送回复时确定API端点

        # 提取发送者信息
        author = event_data.get("author", {})
        # member_openid用于群聊场景，id用于频道场景
        from_user_id = author.get("member_openid", "") or author.get("id", "")
        group_openid = event_data.get("group_openid", "")  # 群聊的群ID

        # 提取消息文本内容
        content = event_data.get("content", "").strip()

        # 检查是否包含图片附件
        attachments = event_data.get("attachments", [])
        # 判断附件中是否有图片类型（content_type以"image/"开头）
        has_image = any(
            a.get("content_type", "").startswith("image/") for a in attachments
        ) if attachments else False

        if has_image and not content:
            # 纯图片消息（无文本）：下载图片并缓存，等待用户后续文本消息
            # 这是多模态交互的第一步：用户先发图片，后续再发文字提问
            self.ctype = ContextType.IMAGE
            # 取第一个图片附件
            img_attachment = next(
                a for a in attachments if a.get("content_type", "").startswith("image/")
            )
            img_url = img_attachment.get("url", "")
            # 确保URL以http开头，QQ返回的URL可能缺少协议前缀
            if img_url and not img_url.startswith("http"):
                img_url = "https://" + img_url
            tmp_dir = _get_tmp_dir()
            # 使用msg_id作为文件名的一部分，确保唯一性
            image_path = os.path.join(tmp_dir, f"qq_{self.msg_id}.png")
            try:
                resp = requests.get(img_url, timeout=30)
                resp.raise_for_status()
                with open(image_path, "wb") as f:
                    f.write(resp.content)
                self.content = image_path
                self.image_path = image_path  # 保存图片路径，用于文件缓存机制
                logger.info(f"[QQ] Image downloaded: {image_path}")
            except Exception as e:
                logger.error(f"[QQ] Failed to download image: {e}")
                # 下载失败时设置占位文本
                self.content = "[Image download failed]"
                self.image_path = None
        elif has_image and content:
            # 图文混合消息：下载所有图片，将图片路径嵌入文本中
            # 这种情况下图片和文字一起到达，无需缓存等待
            self.ctype = ContextType.TEXT
            image_paths = []
            tmp_dir = _get_tmp_dir()
            for idx, att in enumerate(attachments):
                if not att.get("content_type", "").startswith("image/"):
                    continue  # 跳过非图片附件
                img_url = att.get("url", "")
                if img_url and not img_url.startswith("http"):
                    img_url = "https://" + img_url
                # 使用msg_id和附件索引作为文件名，避免文件名冲突
                img_path = os.path.join(tmp_dir, f"qq_{self.msg_id}_{idx}.png")
                try:
                    resp = requests.get(img_url, timeout=30)
                    resp.raise_for_status()
                    with open(img_path, "wb") as f:
                        f.write(resp.content)
                    image_paths.append(img_path)
                except Exception as e:
                    logger.error(f"[QQ] Failed to download mixed image: {e}")
            # 构建消息内容：文本 + 图片路径引用
            content_parts = [content]
            for p in image_paths:
                content_parts.append(f"[图片: {p}]")
            self.content = "\n".join(content_parts)
        else:
            # 纯文本消息：无附件或无图片附件
            self.ctype = ContextType.TEXT
            self.content = content

        # 根据事件类型设置用户ID映射
        # 不同场景下的用户ID字段不同，需要分别处理
        if event_type == "GROUP_AT_MESSAGE_CREATE":
            # 群聊@消息
            self.from_user_id = from_user_id       # 发送者ID
            self.to_user_id = ""                    # QQ Bot不需要to_user_id
            self.other_user_id = group_openid       # 群ID，用于回复和session
            self.actual_user_id = from_user_id      # 实际发送者
            self.actual_user_nickname = from_user_id  # QQ群聊中没有昵称字段，使用ID代替

        elif event_type == "C2C_MESSAGE_CREATE":
            # C2C私聊消息
            user_openid = author.get("user_openid", "") or from_user_id
            self.from_user_id = user_openid         # 发送者openid
            self.to_user_id = ""
            self.other_user_id = user_openid         # 私聊中对方就是other_user
            self.actual_user_id = user_openid

        elif event_type == "AT_MESSAGE_CREATE":
            # 频道@消息
            self.from_user_id = from_user_id
            self.to_user_id = ""
            channel_id = event_data.get("channel_id", "")  # 频道ID
            self.other_user_id = channel_id                  # 频道ID用于回复
            self.actual_user_id = from_user_id
            self.actual_user_nickname = author.get("username", from_user_id)

        elif event_type == "DIRECT_MESSAGE_CREATE":
            # 频道私信消息
            self.from_user_id = from_user_id
            self.to_user_id = ""
            guild_id = event_data.get("guild_id", "")  # 频道服务器ID
            # 频道私信的other_user_id使用组合ID，包含guild_id和user_id
            # 这样可以区分同一用户在不同频道的私信会话
            self.other_user_id = f"dm_{guild_id}_{from_user_id}"
            self.actual_user_id = from_user_id
            self.actual_user_nickname = author.get("username", from_user_id)

        else:
            # 不支持的事件类型，抛出异常
            raise NotImplementedError(f"Unsupported QQ event type: {event_type}")

        logger.debug(f"[QQ] Message parsed: type={event_type}, ctype={self.ctype}, "
                     f"from={self.from_user_id}, content_len={len(self.content)}")
