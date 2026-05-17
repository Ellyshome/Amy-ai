import os
import re

import requests
from dingtalk_stream import ChatbotMessage

from bridge.context import ContextType
from channel.chat_message import ChatMessage
# -*- coding=utf-8 -*-
from common.log import logger
from common.tmp_dir import TmpDir
from common.utils import expand_path
from config import conf


class DingTalkMessage(ChatMessage):
    """
    钉钉消息封装类，将钉钉SDK的ChatbotMessage转换为系统统一的ChatMessage格式。

    该类负责将钉钉平台特有的消息格式（文本、语音、图片、富文本等）解析为
    系统内部统一的消息结构，使上层处理逻辑无需关心具体的平台差异。

    消息类型映射：
    - text -> ContextType.TEXT：普通文本消息
    - audio -> ContextType.TEXT：语音消息（钉钉已内置语音转文字，直接提取识别结果）
    - picture -> ContextType.IMAGE：单张图片消息
    - richText -> ContextType.TEXT：富文本消息（可能包含图片和文字的混合内容）

    为什么语音消息映射为TEXT：钉钉SDK在推送语音消息时，已经通过钉钉服务端
    完成了语音识别，识别结果存储在event.extensions['content']['recognition']中，
    因此无需再做客户端语音识别，直接当文本处理即可。

    图片下载机制：
    钉钉图片不能直接通过URL访问，需要通过downloadCode和robot_code调用
    钉钉新版API获取下载链接。因此图片下载逻辑封装在download_image_file函数中，
    该函数支持两种下载方式：钉钉协议URL和普通HTTP URL。
    """

    def __init__(self, event: ChatbotMessage, image_download_handler):
        """
        初始化钉钉消息对象。

        Args:
            event: 钉钉SDK的ChatbotMessage事件对象，包含消息的所有原始信息
            image_download_handler: 图片下载处理器，通常是DingTalkChanel实例，
                提供get_image_download_url方法用于获取图片下载地址
        """
        super().__init__(event)
        self.image_download_handler = image_download_handler
        # 消息基础属性
        self.msg_id = event.message_id          # 消息唯一ID，用于去重
        self.message_type = event.message_type  # 消息类型（text/audio/picture/richText）
        self.incoming_message = event           # 保存原始消息对象，用于后续回复
        self.sender_staff_id = event.sender_staff_id  # 发送者的员工ID，单聊回复时需要
        self.other_user_id = event.conversation_id    # 会话ID，群聊时为群ID
        self.create_time = event.create_at           # 消息创建时间戳（毫秒），用于过期判断
        self.image_content = event.image_content     # 图片内容对象
        self.rich_text_content = event.rich_text_content  # 富文本内容对象
        self.robot_code = event.robot_code  # 机器人编码，用于主动发送消息时标识机器人身份

        # 判断会话类型：conversation_type="1"表示单聊，其他表示群聊
        # 钉钉的会话类型用字符串"1"表示单聊，而不是布尔值或数字
        if event.conversation_type == "1":
            self.is_group = False
        else:
            self.is_group = True

        # 根据消息类型进行不同的解析处理
        if self.message_type == "text":
            # 文本消息：直接提取文本内容
            self.ctype = ContextType.TEXT
            self.content = event.text.content.strip()

        elif self.message_type == "audio":
            # 钉钉支持直接识别语音，所以此处将直接提取文字，当文字处理
            # 钉钉服务端已将语音转为文字，存储在extensions字段中
            self.content = event.extensions['content']['recognition'].strip()
            self.ctype = ContextType.TEXT

        elif (self.message_type == 'picture') or (self.message_type == 'richText'):
            # 钉钉图片类型或富文本类型消息处理
            # 两种类型都可能包含图片，需要通过get_image_list获取图片列表
            image_list = event.get_image_list()

            if self.message_type == 'picture' and len(image_list) > 0:
                # 单张图片消息：下载到工作空间，用于文件缓存
                self.ctype = ContextType.IMAGE
                download_code = image_list[0]
                # 通过下载处理器获取图片下载URL
                # 返回的可能是特殊协议URL（dingtalk://download/...）或普通HTTP URL
                download_url = image_download_handler.get_image_download_url(download_code)

                # 下载到工作空间 tmp 目录
                workspace_root = expand_path(conf().get("agent_workspace", "~/cow"))
                tmp_dir = os.path.join(workspace_root, "tmp")
                os.makedirs(tmp_dir, exist_ok=True)

                # 调用通用下载函数，支持多种URL格式
                image_path = download_image_file(download_url, tmp_dir)
                if image_path:
                    self.content = image_path
                    self.image_path = image_path  # 保存图片路径用于缓存
                    logger.info(f"[DingTalk] Downloaded single image to {image_path}")
                else:
                    # 下载失败时设置占位文本，避免后续处理出错
                    self.content = "[图片下载失败]"
                    self.image_path = None

            elif self.message_type == 'richText' and len(image_list) > 0:
                # 富文本消息：下载所有图片并附加到文本中
                # 富文本统一作为TEXT类型处理，因为包含文本和图片的混合内容
                self.ctype = ContextType.TEXT

                # 下载到工作空间 tmp 目录
                workspace_root = expand_path(conf().get("agent_workspace", "~/cow"))
                tmp_dir = os.path.join(workspace_root, "tmp")
                os.makedirs(tmp_dir, exist_ok=True)

                # 提取富文本中的文本内容
                text_content = ""
                if self.rich_text_content:
                    # rich_text_content 是一个 RichTextContent 对象，需要从中提取文本
                    text_list = event.get_text_list()
                    if text_list:
                        text_content = "".join(text_list).strip()

                # 下载所有图片 —— 逐张下载，任何一张失败不影响其他图片
                image_paths = []
                for download_code in image_list:
                    download_url = image_download_handler.get_image_download_url(download_code)
                    image_path = download_image_file(download_url, tmp_dir)
                    if image_path:
                        image_paths.append(image_path)

                # 构建消息内容：文本 + 图片路径
                # 图片以[图片: path]格式嵌入文本中，上层处理逻辑可据此识别图片
                content_parts = []
                if text_content:
                    content_parts.append(text_content)
                for img_path in image_paths:
                    content_parts.append(f"[图片: {img_path}]")

                self.content = "\n".join(content_parts) if content_parts else "[富文本消息]"
                logger.info(f"[DingTalk] Received richText with {len(image_paths)} image(s): {self.content}")
            else:
                # 图片列表为空的情况：消息类型是picture或richText但没有实际图片
                self.ctype = ContextType.IMAGE
                self.content = "[未找到图片]"
                logger.debug(f"[DingTalk] messageType: {self.message_type}, imageList isEmpty")

        # 设置用户ID信息，群聊和单聊的ID映射不同
        if self.is_group:
            # 群聊：from_user_id为群ID，actual_user_id为实际发送者ID
            self.from_user_id = event.conversation_id
            self.actual_user_id = event.sender_id
            self.is_at = True  # 群聊消息默认标记为@，因为只有@机器人的消息才会推送
        else:
            # 单聊：发送者和接收者都是用户
            self.from_user_id = event.sender_id
            self.actual_user_id = event.sender_id
        self.to_user_id = event.chatbot_user_id  # 机器人自身的用户ID
        self.other_user_nickname = event.conversation_title  # 会话标题（群名或对方昵称）


def download_image_file(image_url, temp_dir):
    """
    通用图片下载函数，支持两种URL格式。

    该函数封装了钉钉图片下载的复杂逻辑，使调用方无需关心具体的下载协议差异。

    支持两种方式：
    1. 普通 HTTP(S) URL：直接通过HTTP请求下载
    2. 钉钉 downloadCode: dingtalk://download/{robot_code}:{download_code}
       这种格式需要先获取access_token，再调用钉钉新版API获取实际下载链接，
       最后从下载链接获取图片数据。这是因为钉钉的图片安全策略不允许直接
       通过downloadCode访问图片，必须经过鉴权。

    Args:
        image_url: 图片URL，支持HTTP(S)和钉钉特殊协议格式
        temp_dir: 临时目录路径，下载的图片将保存到此目录

    Returns:
        str: 下载成功的图片本地路径；失败返回None
    """
    # 检查临时目录是否存在，如果不存在则创建
    if not os.path.exists(temp_dir):
        os.makedirs(temp_dir)

    # 处理钉钉 downloadCode —— 钉钉特殊的图片下载协议
    if image_url.startswith("dingtalk://download/"):
        download_code = image_url.replace("dingtalk://download/", "")
        logger.info(f"[DingTalk] Downloading image with downloadCode: {download_code[:20]}...")

        # 需要从外部传入 access_token，这里先用一个临时方案
        # 从 config 获取 dingtalk_client_id 和 dingtalk_client_secret
        # 注意：这里直接从配置读取凭证，而不是从通道实例传入，因为该函数是模块级函数
        from config import conf
        client_id = conf().get("dingtalk_client_id")
        client_secret = conf().get("dingtalk_client_secret")

        if not client_id or not client_secret:
            logger.error("[DingTalk] Missing dingtalk_client_id or dingtalk_client_secret")
            return None

        # 解析 robot_code 和 download_code
        # URL格式为 dingtalk://download/{robot_code}:{download_code}
        parts = download_code.split(":", 1)
        if len(parts) != 2:
            logger.error(f"[DingTalk] Invalid download_code format (expected robot_code:download_code): {download_code[:50]}")
            return None

        robot_code, actual_download_code = parts

        # 第一步：获取 access_token（使用新版 API）
        # 必须先获取token，因为后续的图片下载API需要鉴权
        token_url = "https://api.dingtalk.com/v1.0/oauth2/accessToken"
        token_headers = {
            "Content-Type": "application/json"
        }
        token_body = {
            "appKey": client_id,
            "appSecret": client_secret
        }

        try:
            token_response = requests.post(token_url, json=token_body, headers=token_headers, timeout=10)

            if token_response.status_code == 200:
                token_data = token_response.json()
                access_token = token_data.get("accessToken")

                if not access_token:
                    logger.error(f"[DingTalk] Failed to get access token: {token_data}")
                    return None

                # 第二步：获取下载 URL（使用新版 API）
                # 钉钉的图片下载需要先通过API换取临时下载链接，链接有时效限制
                download_api_url = "https://api.dingtalk.com/v1.0/robot/messageFiles/download"
                download_headers = {
                    "x-acs-dingtalk-access-token": access_token,
                    "Content-Type": "application/json"
                }
                download_body = {
                    "downloadCode": actual_download_code,
                    "robotCode": robot_code
                }

                download_response = requests.post(download_api_url, json=download_body, headers=download_headers, timeout=10)

                if download_response.status_code == 200:
                    download_data = download_response.json()
                    download_url = download_data.get("downloadUrl")

                    if not download_url:
                        logger.error(f"[DingTalk] No downloadUrl in response: {download_data}")
                        return None

                    # 第三步：从 downloadUrl 下载实际图片
                    # 获取到的downloadUrl是临时的CDN链接，可直接通过HTTP下载
                    image_response = requests.get(download_url, stream=True, timeout=60)

                    if image_response.status_code == 200:
                        # 生成文件名（使用 download_code 的 hash，避免特殊字符）
                        # 使用hash而不是原始download_code作为文件名，因为download_code
                        # 可能包含特殊字符（如冒号）不适合作为文件名
                        import hashlib
                        file_hash = hashlib.md5(actual_download_code.encode()).hexdigest()[:16]
                        file_name = f"{file_hash}.png"
                        file_path = os.path.join(temp_dir, file_name)

                        with open(file_path, 'wb') as file:
                            file.write(image_response.content)

                        logger.info(f"[DingTalk] Image downloaded successfully: {file_path}")
                        return file_path
                    else:
                        logger.error(f"[DingTalk] Failed to download image from URL: {image_response.status_code}")
                        return None
                else:
                    logger.error(f"[DingTalk] Failed to get download URL: {download_response.status_code}, {download_response.text}")
                    return None
            else:
                logger.error(f"[DingTalk] Failed to get access token: {token_response.status_code}, {token_response.text}")
                return None
        except Exception as e:
            logger.error(f"[DingTalk] Exception downloading image: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return None

    # 普通 HTTP(S) URL —— 直接下载
    else:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/112.0.0.0 Safari/537.36'
        }

        try:
            response = requests.get(image_url, headers=headers, stream=True, timeout=60 * 5)
            if response.status_code == 200:
                # 生成文件名 —— 从URL中提取，去除查询参数
                file_name = image_url.split("/")[-1].split("?")[0]

                # 将文件保存到临时目录
                file_path = os.path.join(temp_dir, file_name)
                with open(file_path, 'wb') as file:
                    file.write(response.content)
                return file_path
            else:
                logger.info(f"[Dingtalk] Failed to download image file, {response.content}")
                return None
        except Exception as e:
            logger.error(f"[Dingtalk] Exception downloading image: {e}")
            return None
