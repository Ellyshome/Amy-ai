"""
Agent Event Handler - 处理 Agent 事件和思考过程输出
"""

from common.log import logger


class AgentEventHandler:
    """
    【功能】处理 Agent 事件并可选地向通道发送中间消息
    
    【职责】
    1. 接收并处理 Agent 执行过程中产生的各种事件
    2. 跟踪 Agent 的思考过程
    3. 向通道发送中间消息（如思考内容）
    4. 链式调用原始回调函数
    """
    
    def __init__(self, context=None, original_callback=None):
        """
        【功能】初始化事件处理器
        
        【参数】
        - context: COW 上下文（用于访问通道）
        - original_callback: 要链式调用的原始事件回调
        """
        # 【变量】COW 上下文，包含通道信息等
        self.context = context
        # 【变量】原始事件回调函数，用于链式调用
        self.original_callback = original_callback
        
        # 【变量】通道对象，用于发送中间消息
        self.channel = None
        if context:
            self.channel = context.kwargs.get("channel") if hasattr(context, "kwargs") else None
        
        # 【变量】当前思考内容，用于通道输出
        self.current_thinking = ""
        # 【变量】当前轮次编号
        self.turn_number = 0
    
    def handle_event(self, event):
        """
        【功能】主事件处理器
        
        【参数】
        - event: 包含类型和数据的事件字典
        """
        """
        Main event handler
        
        Args:
            event: Event dict with type and data
        """

        event_type = event.get("type")
        data = event.get("data", {})
        
        # Dispatch to specific handlers
        if event_type == "turn_start":
            self._handle_turn_start(data)
        elif event_type == "message_update":
            self._handle_message_update(data)
        elif event_type == "message_end":
            self._handle_message_end(data)
        elif event_type == "tool_execution_start":
            self._handle_tool_execution_start(data)
        elif event_type == "tool_execution_end":
            self._handle_tool_execution_end(data)
        
        # Call original callback if provided
        if self.original_callback:
            self.original_callback(event)
    
    def _handle_turn_start(self, data):
        """
        【功能】处理轮次开始事件
        
        【参数】
        - data: 事件数据，包含轮次信息
        """
        """
        Handle turn start event
        """
        # 更新当前轮次编号
        self.turn_number = data.get("turn", 0)
        # 标记本轮次是否有工具调用
        self.has_tool_calls_in_turn = False
        # 清空当前思考内容
        self.current_thinking = ""
    
    def _handle_message_update(self, data):
        """
        【功能】处理消息更新事件（流式文本）
        
        【参数】
        - data: 事件数据，包含文本增量
        """
        """
        Handle message update event (streaming text)
        """
        # 获取文本增量
        delta = data.get("delta", "")
        # 累积到当前思考内容
        self.current_thinking += delta
    
    def _handle_message_end(self, data):
        """
        【功能】处理消息结束事件
        
        【参数】
        - data: 事件数据，包含工具调用信息
        """
        """
        Handle message end event
        """
        # 获取工具调用列表
        tool_calls = data.get("tool_calls", [])
        
        # 只有在工具调用之前才发送思考过程
        if tool_calls:
            if self.current_thinking.strip():
                # 记录思考过程到日志
                logger.info(f"💭 {self.current_thinking.strip()[:200]}{'...' if len(self.current_thinking) > 200 else ''}")
                # 向通道发送思考过程
                self._send_to_channel(f"{self.current_thinking.strip()}")
        else:
            # 没有工具调用 = 最终响应（在 agent_stream 级别记录）
            if self.current_thinking.strip():
                logger.debug(f"💬 {self.current_thinking.strip()[:200]}{'...' if len(self.current_thinking) > 200 else ''}")
        
        # 清空当前思考内容
        self.current_thinking = ""
    
    def _handle_tool_execution_start(self, data):
        """
        【功能】处理工具执行开始事件 - 由 agent_stream.py 记录
        
        【参数】
        - data: 事件数据
        Handle tool execution start event - logged by agent_stream.py
        """
        pass
    
    def _handle_tool_execution_end(self, data):
        """
        【功能】处理工具执行结束事件 - 由 agent_stream.py 记录
        
        【参数】
        - data: 事件数据
        """
        """
        Handle tool execution end event - logged by agent_stream.py
        """
        pass
    
    def _send_to_channel(self, message):
        """
        【功能】尝试向通道发送中间消息
        
        【参数】
        - message: 要发送的消息
        
        【说明】
        在 SSE 模式下会跳过，因为思考文本已经通过 on_event 流式传输
        """
        """
        Try to send intermediate message to channel.
        Skipped in SSE mode because thinking text is already streamed via on_event.
        """
        # SSE 模式下跳过，因为思考文本已经通过 on_event 流式传输
        if self.context and self.context.get("on_event"):
            return

        # 如果有通道对象，则尝试发送消息
        if self.channel:
            try:
                from bridge.reply import Reply, ReplyType
                # 创建回复对象
                reply = Reply(ReplyType.TEXT, message)
                # 发送回复到通道
                self.channel._send(reply, self.context)
            except Exception as e:
                # 记录发送失败的日志
                logger.debug(f"[AgentEventHandler] Failed to send to channel: {e}")
    
    def log_summary(self):
        """
        【功能】记录执行摘要 - 简化版
        
        【说明】
        根据用户请求移除了摘要，执行过程中的实时日志已经足够
        """
        """
        Log execution summary - simplified
        """
        # 根据用户请求移除了摘要
        # 执行过程中的实时日志已经足够
        pass
