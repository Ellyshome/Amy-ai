"""
channel factory
通道工厂模块，负责根据通道类型字符串创建对应的通道实例。

工厂模式的核心思想是将对象的创建逻辑集中管理，使得：
1. 上层调用者（如 ChannelManager）无需了解具体通道类的导入路径
2. 通道类型与实现类之间的映射关系在此统一维护
3. 新增通道类型时只需在此处添加映射，无需修改调用方代码
4. 延迟导入（lazy import）避免了循环依赖和不必要的模块加载
"""
from common import const
from .channel import Channel


def create_channel(channel_type) -> Channel:
    """
    create a channel instance
    根据通道类型代码创建对应的通道实例。

    该函数是整个通道创建的统一入口，由 ChannelManager 在启动时调用。
    采用延迟导入（lazy import）策略，即只在需要时才导入具体的通道模块，
    这样做的好处是：
    - 避免启动时加载所有通道模块，减少内存占用和启动时间
    - 避免因某个通道的依赖缺失而影响其他通道的正常运行
    - 避免循环导入问题（通道模块之间可能存在交叉引用）

    创建完成后，会将 channel_type 赋值给通道实例的 channel_type 属性，
    以便在后续处理中识别消息来源的通道类型。

    Args:
        channel_type: 通道类型代码，支持的值包括：
            - "terminal": 终端命令行通道
            - "web": Web 网页通道
            - "wechatmp": 微信公众号通道（被动回复模式）
            - "wechatmp_service": 微信公众号通道（主动回复模式）
            - "wechatcom_app": 微信企业号应用通道
            - const.FEISHU: 飞书通道
            - const.DINGTALK: 钉钉通道
            - const.WECOM_BOT: 企微机器人通道
            - const.QQ: QQ 通道
            - const.WEIXIN / "wx": 微信个人号通道

    Returns:
        Channel: 创建好的通道实例，类型为对应的具体通道子类

    Raises:
        RuntimeError: 当传入不支持的通道类型时抛出
    """
    # 先创建一个默认的 Channel 基类实例，后续根据类型替换为具体子类实例
    ch = Channel()
    if channel_type == "terminal":
        # 终端通道：命令行交互模式，主要用于开发调试
        from channel.terminal.terminal_channel import TerminalChannel
        ch = TerminalChannel()
    elif channel_type == 'web':
        # Web 通道：基于 HTTP/SSE 的网页聊天界面，支持流式输出
        from channel.web.web_channel import WebChannel
        ch = WebChannel()
    elif channel_type == "wechatmp":
        # 微信公众号通道（被动回复模式）：
        # 使用微信公众号的被动回复机制，在5秒内必须返回响应
        # 适合消息量较小的场景
        from channel.wechatmp.wechatmp_channel import WechatMPChannel
        ch = WechatMPChannel(passive_reply=True)
    elif channel_type == "wechatmp_service":
        # 微信公众号通道（主动回复/客服消息模式）：
        # 使用微信客服消息接口主动推送回复，不受5秒超时限制
        # 适合需要较长处理时间的场景
        from channel.wechatmp.wechatmp_channel import WechatMPChannel
        ch = WechatMPChannel(passive_reply=False)
    elif channel_type == "wechatcom_app":
        # 微信企业号应用通道：对接企业微信的自建应用
        from channel.wechatcom.wechatcomapp_channel import WechatComAppChannel
        ch = WechatComAppChannel()
    elif channel_type == const.FEISHU:
        # 飞书通道：对接飞书机器人，支持群聊和私聊
        from channel.feishu.feishu_channel import FeiShuChanel
        ch = FeiShuChanel()
    elif channel_type == const.DINGTALK:
        # 钉钉通道：对接钉钉机器人，支持群聊和私聊
        from channel.dingtalk.dingtalk_channel import DingTalkChanel
        ch = DingTalkChanel()
    elif channel_type == const.WECOM_BOT:
        # 企微机器人通道：对接企业微信群聊机器人
        from channel.wecom_bot.wecom_bot_channel import WecomBotChannel
        ch = WecomBotChannel()
    elif channel_type == const.QQ:
        # QQ 通道：对接 QQ 机器人
        from channel.qq.qq_channel import QQChannel
        ch = QQChannel()
    elif channel_type in (const.WEIXIN, "wx"):
        # 微信个人号通道：通过 itchat 等库登录微信个人账号
        # 同时支持 "wx" 作为兼容性别名
        from channel.weixin.weixin_channel import WeixinChannel
        ch = WeixinChannel()
        # 统一通道类型为标准常量，确保后续逻辑中类型判断的一致性
        channel_type = const.WEIXIN
    else:
        # 不支持的通道类型，抛出运行时异常
        raise RuntimeError
    # 将通道类型标识赋值给实例，以便在消息处理链路中识别通道来源
    ch.channel_type = channel_type
    return ch
