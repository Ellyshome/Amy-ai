import json
import os
import requests
from bridge.context import ContextType
from channel.chat_message import ChatMessage
from common.log import logger
from common.tmp_dir import TmpDir
from common import utils
from common.utils import expand_path
from config import conf


class FeishuMessage(ChatMessage):
    """
    飞书消息封装类，将飞书平台的事件数据转换为系统统一的ChatMessage格式。

    该类负责解析飞书多种消息类型（文本、图片、富文本、文件），
    将其转换为系统内部统一的ContextType和content格式。

    消息类型映射：
    - text -> ContextType.TEXT：纯文本消息
    - image -> ContextType.IMAGE：单张图片消息，下载到本地并缓存路径
    - post -> ContextType.TEXT：富文本消息，可能包含文本和图片的混合内容
    - file -> ContextType.FILE：文件消息，通过_prepare_fn延迟下载

    图片处理策略：
    - 单张图片(image类型)：下载到本地，设置image_path属性，用于文件缓存机制
    - 富文本中的图片(post类型)：立即下载（因为TEXT类型不会调用prepare()方法），
      将图片路径以[图片: path]格式嵌入文本中

    为什么富文本中的图片要立即下载：
    系统的文件下载机制是通过_prepare_fn回调实现的，但_prepare_fn只在
    ContextType.IMAGE和ContextType.FILE类型的消息中被调用。
    富文本消息被映射为TEXT类型，因此_prepare_fn不会被调用，
    图片必须在构造函数中立即下载。

    @处理逻辑：
    飞书在群聊消息中使用@_user_1占位符表示@机器人，需要从content中移除。
    """

    def __init__(self, event: dict, is_group=False, access_token=None):
        """
        初始化飞书消息对象。

        Args:
            event: 飞书事件字典，包含message和sender等字段。
                   message字段包含消息类型、内容、消息ID等信息。
                   sender字段包含发送者ID信息。
            is_group: 是否为群聊消息，影响用户ID的映射方式
            access_token: 飞书API访问令牌，用于下载图片和文件
        """
        super().__init__(event)
        msg = event.get("message")       # 消息主体信息
        sender = event.get("sender")     # 发送者信息
        self.access_token = access_token  # 缓存token，供后续下载资源使用
        self.msg_id = msg.get("message_id")          # 消息唯一ID，用于去重和回复
        self.create_time = msg.get("create_time")    # 消息创建时间戳（毫秒）
        self.is_group = is_group                      # 是否群聊
        msg_type = msg.get("message_type")            # 消息类型标识

        if msg_type == "text":
            # 文本消息：从content JSON中提取文本
            self.ctype = ContextType.TEXT
            content = json.loads(msg.get('content'))
            self.content = content.get("text").strip()

        elif msg_type == "image":
            # 单张图片消息：下载并缓存，等待用户提问时一起发送
            # 图片下载后保存到本地工作空间，路径缓存到image_path属性
            self.ctype = ContextType.IMAGE
            content = json.loads(msg.get("content"))
            image_key = content.get("image_key")  # 飞书图片的唯一标识

            # 下载图片到工作空间临时目录
            workspace_root = expand_path(conf().get("agent_workspace", "~/cow"))
            tmp_dir = os.path.join(workspace_root, "tmp")
            os.makedirs(tmp_dir, exist_ok=True)
            # 使用image_key作为文件名，确保同一图片不会重复下载
            image_path = os.path.join(tmp_dir, f"{image_key}.png")

            # 下载图片 —— 通过飞书消息资源API获取图片二进制数据
            # API文档: https://open.feishu.cn/document/server-docs/im-v1/message-resources
            url = f"https://open.feishu.cn/open-apis/im/v1/messages/{msg.get('message_id')}/resources/{image_key}"
            headers = {"Authorization": "Bearer " + access_token}
            params = {"type": "image"}
            response = requests.get(url=url, headers=headers, params=params)

            if response.status_code == 200:
                with open(image_path, "wb") as f:
                    f.write(response.content)
                logger.info(f"[FeiShu] Downloaded single image, key={image_key}, path={image_path}")
                self.content = image_path
                self.image_path = image_path  # 保存图片路径，用于文件缓存机制
            else:
                logger.error(f"[FeiShu] Failed to download single image, key={image_key}, status={response.status_code}")
                # 下载失败时设置占位文本，避免后续处理出错
                self.content = f"[图片下载失败: {image_key}]"
                self.image_path = None

        elif msg_type == "post":
            # 富文本消息，可能包含图片、文本等多种元素
            # 飞书富文本结构：content包含title和content数组，content数组中
            # 每个元素是一个段落，段落包含多个文本和图片元素
            content = json.loads(msg.get("content"))

            # 飞书富文本消息结构：content 直接包含 title 和 content 数组
            # 不是嵌套在 post 字段下
            title = content.get("title", "")
            content_list = content.get("content", [])

            logger.info(f"[FeiShu] Post message - title: '{title}', content_list length: {len(content_list)}")

            # 收集所有图片和文本 —— 分别提取后组合
            image_keys = []
            text_parts = []

            if title:
                text_parts.append(title)

            for block in content_list:
                logger.debug(f"[FeiShu] Processing block: {block}")
                # block 本身就是元素列表 —— 飞书富文本的content是二维数组
                if not isinstance(block, list):
                    continue

                for element in block:
                    element_tag = element.get("tag")
                    logger.debug(f"[FeiShu] Element tag: {element_tag}, element: {element}")
                    if element_tag == "img":
                        # 找到图片元素 —— 收集image_key用于后续下载
                        image_key = element.get("image_key")
                        if image_key:
                            image_keys.append(image_key)
                    elif element_tag == "text":
                        # 文本元素 —— 直接提取文本内容
                        text_content = element.get("text", "")
                        if text_content:
                            text_parts.append(text_content)

            logger.info(f"[FeiShu] Parsed - images: {len(image_keys)}, text_parts: {text_parts}")

            # 富文本消息统一作为文本消息处理
            # 原因：系统只支持TEXT和IMAGE两种主要类型，富文本包含多种元素，
            # 统一作为TEXT处理可以将所有内容（文本+图片路径）放在一起
            self.ctype = ContextType.TEXT

            if image_keys:
                # 如果包含图片，下载并在文本中引用本地路径
                workspace_root = expand_path(conf().get("agent_workspace", "~/cow"))
                tmp_dir = os.path.join(workspace_root, "tmp")
                os.makedirs(tmp_dir, exist_ok=True)

                # 保存图片路径映射 —— image_key到本地路径的映射
                self.image_paths = {}
                for image_key in image_keys:
                    image_path = os.path.join(tmp_dir, f"{image_key}.png")
                    self.image_paths[image_key] = image_path

                def _download_images():
                    """
                    下载富文本中的所有图片。

                    这是内部函数，直接在构造时调用。
                    使用闭包访问self.image_paths和access_token。
                    """
                    for image_key, image_path in self.image_paths.items():
                        url = f"https://open.feishu.cn/open-apis/im/v1/messages/{self.msg_id}/resources/{image_key}"
                        headers = {"Authorization": "Bearer " + access_token}
                        params = {"type": "image"}
                        response = requests.get(url=url, headers=headers, params=params)
                        if response.status_code == 200:
                            with open(image_path, "wb") as f:
                                f.write(response.content)
                            logger.info(f"[FeiShu] Image downloaded from post message, key={image_key}, path={image_path}")
                        else:
                            logger.error(f"[FeiShu] Failed to download image from post, key={image_key}, status={response.status_code}")

                # 立即下载图片，不使用延迟下载
                # 因为 TEXT 类型消息不会调用 prepare()
                # 这与单张图片消息不同：单张图片是IMAGE类型，会通过_prepare_fn延迟下载
                _download_images()

                # 构建消息内容：文本 + 图片路径
                # 图片以[图片: path]格式嵌入，上层逻辑可以据此识别图片文件
                content_parts = []
                if text_parts:
                    content_parts.append("\n".join(text_parts).strip())
                for image_key, image_path in self.image_paths.items():
                    content_parts.append(f"[图片: {image_path}]")

                self.content = "\n".join(content_parts)
                logger.info(f"[FeiShu] Received post message with {len(image_keys)} image(s) and text: {self.content}")
            else:
                # 纯文本富文本消息 —— 没有图片，直接拼接所有文本段落
                self.content = "\n".join(text_parts).strip() if text_parts else "[富文本消息]"
                logger.info(f"[FeiShu] Received post message (text only): {self.content}")

        elif msg_type == "file":
            # 文件消息：设置延迟下载回调
            self.ctype = ContextType.FILE
            content = json.loads(msg.get("content"))
            file_key = content.get("file_key")     # 飞书文件的唯一标识
            file_name = content.get("file_name")   # 原始文件名

            # 构建本地文件保存路径：临时目录 + file_key + 文件扩展名
            self.content = TmpDir().path() + file_key + "." + utils.get_path_suffix(file_name)

            def _download_file():
                """
                延迟下载文件回调。

                该函数不会立即执行，而是赋值给self._prepare_fn，
                在消息处理流程的后续阶段（prepare()方法中）被调用。
                延迟下载的原因：不是所有文件都需要立即下载，
                如果消息被过滤或跳过，则无需浪费带宽。
                """
                # 如果响应状态码是200，则将响应内容写入本地文件
                url = f"https://open.feishu.cn/open-apis/im/v1/messages/{self.msg_id}/resources/{file_key}"
                headers = {
                    "Authorization": "Bearer " + access_token,
                }
                params = {
                    "type": "file"
                }
                response = requests.get(url=url, headers=headers, params=params)
                if response.status_code == 200:
                    with open(self.content, "wb") as f:
                        f.write(response.content)
                else:
                    logger.info(f"[FeiShu] Failed to download file, key={file_key}, res={response.text}")
            # 将下载函数赋值给_prepare_fn，在ChatChannel.prepare()中调用
            self._prepare_fn = _download_file
        else:
            # 不支持的消息类型：如音频、视频、位置等，抛出异常
            raise NotImplementedError("Unsupported message type: Type:{} ".format(msg_type))

        # 设置用户ID信息
        # from_user_id: 消息发送者的open_id
        # to_user_id: 应用ID（即机器人自身）
        self.from_user_id = sender.get("sender_id").get("open_id")
        self.to_user_id = event.get("app_id")
        if is_group:
            # 群聊
            self.other_user_id = msg.get("chat_id")   # 群聊的chat_id作为other_user_id
            self.actual_user_id = self.from_user_id     # 实际发送者ID
            # 移除飞书群聊中的@占位符
            # 飞书在群聊@消息中使用 @_user_1 作为占位符表示@机器人，
            # 需要从文本内容中移除，避免影响AI理解
            self.content = self.content.replace("@_user_1", "").strip()
            self.actual_user_nickname = ""
        else:
            # 私聊
            self.other_user_id = self.from_user_id    # 私聊中对方就是发送者
            self.actual_user_id = self.from_user_id
