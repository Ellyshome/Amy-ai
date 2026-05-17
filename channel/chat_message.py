"""
Unified chat message class for different channel implementations.
统一聊天消息类，为不同通道实现提供标准化的消息数据结构。

该类定义了跨通道的统一消息格式，使得上层处理逻辑（如 ChatChannel、插件系统）
无需关心底层通道的消息格式差异。各通道在接收到原始消息后，将其转换为 ChatMessage
对象，即可无缝接入 ChatChannel 的处理链路。

填好必填项(群聊6个，非群聊8个)，即可接入ChatChannel，并支持插件，参考TerminalChannel

ChatMessage
msg_id: 消息id (必填)
create_time: 消息创建时间

ctype: 消息类型 : ContextType (必填)
content: 消息内容, 如果是声音/图片，这里是文件路径 (必填)

from_user_id: 发送者id (必填)
from_user_nickname: 发送者昵称
to_user_id: 接收者id (必填)
to_user_nickname: 接收者昵称

other_user_id: 对方的id，如果你是发送者，那这个就是接收者id，如果你是接收者，那这个就是发送者id，如果是群消息，那这一直是群id (必填)
other_user_nickname: 同上

is_group: 是否是群消息 (群聊必填)
is_at: 是否被at

- (群消息时，一般会存在实际发送者，是群内某个成员的id和昵称，下列项仅在群消息时存在)
actual_user_id: 实际发送者id (群聊必填)
actual_user_nickname：实际发送者昵称
self_display_name: 自身的展示名，设置群昵称时，该字段表示群昵称

_prepare_fn: 准备函数，用于准备消息的内容，比如下载图片等,
_prepared: 是否已经调用过准备函数
_rawmsg: 原始消息对象

设计说明：
- 必填字段的设计遵循最小可用原则：非群聊场景需要8个字段，
  群聊场景需要额外2个字段（is_group 和 actual_user_id），共6个独有必填项
- other_user_id 是一个智能字段：无论你是发送者还是接收者，
  它始终指向"对话的另一方"，简化了消息流向的判断逻辑
- 群消息中的 actual_user_id 区别于 from_user_id：
  from_user_id 是群本身，actual_user_id 才是群内实际发言的成员
"""


class ChatMessage(object):
    """
    统一聊天消息类，为所有通道实现提供标准化的消息数据结构。

    该类将不同平台的消息格式抽象为统一的字段集合，使得 ChatChannel、
    插件系统和 Agent 层可以一致地处理来自不同通道的消息，无需关心
    底层平台的消息格式差异。

    字段分为三类：
    1. 基础消息字段：消息ID、时间、类型、内容
    2. 用户身份字段：发送者、接收者、对方用户
    3. 群聊专用字段：群标识、实际发送者、AT列表
    4. 内部处理字段：准备函数、原始消息对象
    """

    # ---- 基础消息字段 ----

    msg_id = None
    # 消息唯一标识符，由消息平台分配。用于消息去重、回复关联和日志追踪。
    # 必填字段，缺少此字段可能导致消息无法被正确处理。

    create_time = None
    # 消息创建时间戳。用于消息排序、超时判断和历史记录查询。

    ctype = None
    # 消息类型，对应 ContextType 枚举值（如 TEXT、IMAGE、VOICE 等）。
    # 必填字段，决定了消息的处理方式（如图片需要先下载再识别）。

    content = None
    # 消息内容。文本消息时为文字内容，图片/语音消息时为本地文件路径。
    # 必填字段，是消息处理的核心输入。

    # ---- 用户身份字段 ----

    from_user_id = None
    # 发送者ID。私聊中为对方用户ID，群聊中为群ID。
    # 必填字段，用于识别消息来源。

    from_user_nickname = None
    # 发送者昵称。用于日志输出和用户展示，非必填。

    to_user_id = None
    # 接收者ID。通常为机器人自身的ID。
    # 必填字段，用于识别消息目标。

    to_user_nickname = None
    # 接收者昵称。用于日志输出和展示，非必填。

    other_user_id = None
    # 对方用户ID。这是一个智能字段：
    # - 如果你是接收者，此字段为发送者ID
    # - 如果你是发送者，此字段为接收者ID
    # - 如果是群消息，此字段始终为群ID
    # 必填字段，简化了消息流向的判断逻辑，使得插件和处理链路
    # 无需区分消息方向即可定位"对话的另一方"。

    other_user_nickname = None
    # 对方用户昵称，含义同 other_user_id 的昵称版本。

    my_msg = False
    # 是否为自身发送的消息。用于在群聊中过滤自己发出的消息，
    # 避免机器人对自身消息产生循环回复。默认为 False。

    self_display_name = None
    # 自身展示名称。在群聊中设置群昵称时，此字段表示群昵称。
    # 用于在 AT 匹配和消息过滤时识别机器人自身。

    # ---- 群聊专用字段 ----

    is_group = False
    # 是否为群聊消息。群聊必填字段。
    # 影响消息处理逻辑：群消息需要考虑 AT 触发、实际发送者提取等。
    # 默认为 False，即非群聊消息。

    is_at = False
    # 是否被 @AT。用于判断是否需要响应此消息——
    # 在群聊中，通常只有被 AT 的消息才需要处理，避免对群内所有消息都回复。

    actual_user_id = None
    # 群消息的实际发送者ID。在群聊中，from_user_id 是群ID，
    # 而 actual_user_id 才是群内实际发言的成员ID。
    # 群聊必填字段，用于用户身份识别、权限判断和会话管理。

    actual_user_nickname = None
    # 群消息实际发送者的昵称。

    at_list = None
    # 被 AT 的用户ID列表。用于识别消息中 AT 了哪些用户，
    # 支持多 AT 场景下的触发判断。

    # ---- 内部处理字段 ----

    _prepare_fn = None
    # 准备函数，用于在消息处理前执行异步准备工作。
    # 典型场景：图片消息需要先下载到本地、语音消息需要先转换格式等。
    # 通过延迟执行准备操作，可以避免在消息接收阶段就执行耗时的IO操作，
    # 只在实际需要处理该消息时才执行准备工作。

    _prepared = False
    # 是否已调用过准备函数。防止重复执行准备工作，确保 _prepare_fn 只执行一次。

    _rawmsg = None
    # 原始消息对象，保存通道特定的原始消息数据。
    # 在需要访问通道特有信息（如微信的消息XML、飞书的事件体）时使用。
    # 上层通用逻辑不应直接访问此字段。

    def __init__(self, _rawmsg):
        """
        初始化聊天消息对象。

        Args:
            _rawmsg: 原始消息对象，由各通道实现提供。
                     保存为 _rawmsg 属性，供后续需要访问通道特有信息时使用。
                     各通道负责在创建 ChatMessage 后设置其他必填字段。
        """
        self._rawmsg = _rawmsg

    def prepare(self):
        """
        执行消息的准备工作。

        如果设置了准备函数（_prepare_fn）且尚未执行过（_prepared 为 False），
        则调用准备函数并标记为已准备。

        准备函数的典型用途：
        - 下载图片/文件到本地，以便后续处理（如 OCR、图片识别）
        - 转换语音格式，以适配 ASR 服务的要求
        - 预加载消息附件，避免在处理链路中产生延迟

        通过 _prepared 标志确保准备工作只执行一次，即使 prepare() 被多次调用。
        """
        if self._prepare_fn and not self._prepared:
            # 标记为已准备，防止重复调用准备函数
            self._prepared = True
            # 执行准备函数，完成消息内容的异步准备工作
            self._prepare_fn()

    def __str__(self):
        """
        返回消息的字符串表示，用于日志输出和调试。

        包含所有主要字段的信息，便于在日志中快速定位消息相关的问题。
        注意：不包含 _rawmsg 和 _prepare_fn 等内部字段，因为它们
        可能体积较大或不适合序列化输出。
        """
        return "ChatMessage: id={}, create_time={}, ctype={}, content={}, from_user_id={}, from_user_nickname={}, to_user_id={}, to_user_nickname={}, other_user_id={}, other_user_nickname={}, is_group={}, is_at={}, actual_user_id={}, actual_user_nickname={}, at_list={}".format(
            self.msg_id,
            self.create_time,
            self.ctype,
            self.content,
            self.from_user_id,
            self.from_user_nickname,
            self.to_user_id,
            self.to_user_nickname,
            self.other_user_id,
            self.other_user_nickname,
            self.is_group,
            self.is_at,
            self.actual_user_id,
            self.actual_user_nickname,
            self.at_list
        )
