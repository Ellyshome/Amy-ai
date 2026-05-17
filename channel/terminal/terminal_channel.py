import sys

from bridge.context import *
from bridge.reply import Reply, ReplyType
from channel.chat_channel import ChatChannel, check_prefix
from channel.chat_message import ChatMessage
from common.log import logger
from config import conf


class TerminalMessage(ChatMessage):
    """
    终端频道消息对象。
    将终端中用户输入的文本封装为统一的消息格式，使其能被 ChatChannel 的
    消息处理流水线（compose_context → produce → consume → handle）所识别。
    由于终端没有真实的用户体系，from_user_id 默认为 "User"，
    to_user_id / other_user_id 默认为 "Chatgpt"。
    """

    def __init__(
        self,
        msg_id,
        content,
        ctype=ContextType.TEXT,
        from_user_id="User",
        to_user_id="Chatgpt",
        other_user_id="Chatgpt",
    ):
        self.msg_id = msg_id          # 消息自增 ID，每次输入递增
        self.ctype = ctype            # 消息类型，终端模式下固定为文本
        self.content = content        # 用户输入的原始文本内容
        self.from_user_id = from_user_id    # 发送者标识（终端用户）
        self.to_user_id = to_user_id        # 接收者标识（机器人）
        self.other_user_id = other_user_id  # 对端标识（与 to_user_id 相同）


class TerminalChannel(ChatChannel):
    """
    终端频道实现，通过命令行标准输入/输出与用户交互。
    继承自 ChatChannel，复用了消息生产-消费、会话管理、限流等核心能力，
    仅需实现 send() 和 startup() 即可适配终端场景。

    不支持的回复类型：语音（终端无法播放音频）
    启动方式：python app.py --cmd
    """

    # 终端不支持语音回复类型
    NOT_SUPPORT_REPLYTYPE = [ReplyType.VOICE]

    def send(self, reply: Reply, context: Context):
        """
        将机器人回复输出到终端。
        根据回复类型采用不同的展示方式：
          - 图片：用 PIL 打开并调用系统图片查看器展示
          - 图片URL：先从网络下载图片数据，再用系统查看器展示
          - 其他（文本/错误等）：直接打印到终端
        输出完毕后打印 "User:" 提示符，等待用户下一次输入。
        """
        print("\nBot:")
        if reply.type == ReplyType.IMAGE:
            # 本地生成的图片（如 DALL-E），content 是 BytesIO 对象
            from PIL import Image

            image_storage = reply.content
            image_storage.seek(0)       # 将读取指针重置到开头
            img = Image.open(image_storage)
            print("<IMAGE>")
            img.show()                  # 调用操作系统默认图片查看器打开
        elif reply.type == ReplyType.IMAGE_URL:  # 从网络下载图片
            import io

            import requests
            from PIL import Image

            img_url = reply.content
            # 以流式方式下载图片，避免大图占用过多内存
            pic_res = requests.get(img_url, stream=True)
            image_storage = io.BytesIO()
            for block in pic_res.iter_content(1024):  # 每次读取 1KB
                image_storage.write(block)
            image_storage.seek(0)       # 下载完成，指针重置到开头
            img = Image.open(image_storage)
            print(img_url)
            img.show()                  # 调用操作系统默认图片查看器打开
        else:
            # 文本、错误、信息等类型直接打印内容
            print(reply.content)
        # 输出用户提示符，end="" 使光标停留在同一行，等待输入
        print("\nUser:", end="")
        sys.stdout.flush()  # 强制刷新输出缓冲区，确保提示符立即显示
        return

    def startup(self):
        """
        终端频道的启动入口，由 ChannelManager 在守护线程中调用。
        进入一个无限循环，不断读取用户输入并交给消息处理流水线处理。

        流程：
        1. 降低日志级别为 WARN，避免终端输出被日志刷屏
        2. 循环读取用户输入（Ctrl+C 退出程序）
        3. 检查输入是否匹配触发前缀，不匹配则自动补上（终端模式下始终触发）
        4. 调用 _compose_context() 构建消息上下文
        5. 调用 produce() 将上下文投入消息队列，由消费者线程异步处理
        """
        context = Context()
        logger.setLevel("WARN")  # 终端模式降低日志级别，避免干扰交互输出
        print("\nPlease input your question:\nUser:", end="")
        sys.stdout.flush()
        msg_id = 0
        while True:
            try:
                prompt = self.get_input()  # 阻塞等待用户输入
            except KeyboardInterrupt:
                # 用户按 Ctrl+C，优雅退出程序
                print("\nExiting...")
                sys.exit()
            msg_id += 1
            # 获取配置的触发前缀（如 ["bot", "@bot"]），终端模式下默认为 [""] 即无需前缀
            trigger_prefixs = conf().get("single_chat_prefix", [""])
            # 在群聊/私聊场景（飞书、钉钉、微信），用户消息里可能混着跟人聊天的内容，
            # 只有带 @bot 或 bot 前缀的消息才应该让机器人回复，避免每条消息都触发
            if check_prefix(prompt, trigger_prefixs) is None:
                # 用户输入未匹配任何触发前缀，自动补上第一个前缀使其触发机器人回复
                prompt = trigger_prefixs[0] + prompt

            # 将用户输入构建为消息上下文，包含消息类型、内容、消息对象等
            context = self._compose_context(ContextType.TEXT, prompt, msg=TerminalMessage(msg_id, prompt))
            '''
            _compose_context() 是 ChatChannel 基类的方法，它内部会完成：
            设置 session_id(区分不同用户的会话)
            匹配触发前缀，提取实际查询内容
            检查限流(token bucket)
            触发 ON_HANDLE_CONTEXT 插件钩子，让插件有机会修改或拦截消息
            返回构建好的 Context 对象，如果消息不应处理则返回 None
            '''
            context["isgroup"] = False  # 终端模式为私聊，非群聊
            if context:
                # 将上下文投入消息队列，由 ChatChannel 的消费线程异步处理
                self.produce(context)
            else:
                raise Exception("context is None")

    def get_input(self):
        """
        从终端读取用户输入。
        当前为单行输入模式（一次 input() 调用），
        可扩展为多行输入（如检测空行结束）。
        """
        sys.stdout.flush()
        line = input()
        return line
