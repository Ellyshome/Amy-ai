"""
Agent Stream Execution Module - Multi-turn reasoning based on tool-call

Provides streaming output, event system, and complete tool-call loop
"""
import json
import time
from typing import List, Dict, Any, Optional, Callable, Tuple

from agent.protocol.models import LLMRequest, LLMModel
from agent.protocol.message_utils import sanitize_claude_messages, compress_turn_to_text_only
from agent.tools.base_tool import BaseTool, ToolResult
from common.log import logger


class AgentStreamExecutor:
    """
    Agent Stream Executor
    
    基于工具调用的多轮推理循环处理器：
    1. LLM生成响应（可能包含工具调用）
    2. 执行工具
    3. 将结果返回给LLM
    4. 重复直到没有更多工具调用
    """

    def __init__(
            self,
            agent,  # Agent实例
            model: LLMModel,
            system_prompt: str,
            tools: List[BaseTool],
            max_turns: int = 50,
            on_event: Optional[Callable] = None,
            messages: Optional[List[Dict]] = None,
            max_context_turns: int = 30
    ):
        """
        初始化流执行器
        
        参数:
            agent: Agent实例（用于访问上下文）
            model: LLM模型
            system_prompt: 系统提示
            tools: 可用工具列表
            max_turns: 最大轮次数量
            on_event: 事件回调函数
            messages: 可选的现有消息历史（用于持久化对话）
            max_context_turns: 要保存在上下文中的最大对话轮次数量
        """
        self.agent = agent
        self.model = model
        self.system_prompt = system_prompt
        # 将工具列表转换为字典
        self.tools = {tool.name: tool for tool in tools} if isinstance(tools, list) else tools
        self.max_turns = max_turns
        self.on_event = on_event
        self.max_context_turns = max_context_turns

        # 消息历史 - 使用提供的消息或创建新列表
        self.messages = messages if messages is not None else []
        
        # 工具失败跟踪，用于重试保护
        self.tool_failure_history = []  # (tool_name, args_hash, success) 元组列表
        
        # 跟踪要发送的文件（由read工具填充）
        self.files_to_send = []  # 文件元数据字典列表

    def _emit_event(self, event_type: str, data: dict = None):
        """发出事件"""
        if self.on_event:
            try:
                self.on_event({
                    "type": event_type,
                    "timestamp": time.time(),
                    "data": data or {}
                })
            except Exception as e:
                logger.error(f"事件回调错误: {e}")
    
    def _filter_think_tags(self, text: str) -> str:
        """
        移除<think>和</think>标签但保留内部内容。
        一些LLM提供商（例如MiniMax）可能会返回用<think>标签包裹的思考过程。
        我们只移除标签本身，保留实际的思考内容。
        """
        if not text:
            return text
        import re
        # 仅移除 </think> 和 </think> 标签，保留内容
        text = re.sub(r'<think>', '', text)
        text = re.sub(r'</think>', '', text)
        return text

    def _hash_args(self, args: dict) -> str:
        """为工具参数生成简单哈希值"""
        import hashlib
        # 对键进行排序以保持一致的哈希
        args_str = json.dumps(args, sort_keys=True, ensure_ascii=False)
        return hashlib.md5(args_str.encode()).hexdigest()[:8]
    
    def _check_consecutive_failures(self, tool_name: str, args: dict) -> Tuple[bool, str, bool]:
        """
        检查工具是否连续失败次数过多或使用相同参数重复调用
        
        返回值:
            (should_stop, reason, is_critical)
            - should_stop: 是否停止工具执行
            - reason: 停止的原因
            - is_critical: 是否终止整个对话（8次以上失败时为True）
        """
        args_hash = self._hash_args(args)
        
        # 统计相同工具+参数的连续调用次数（包括成功和失败）
        # 这可以捕获工具成功但LLM持续调用的无限循环
        same_args_calls = 0
        for name, ahash, success in reversed(self.tool_failure_history):
            if name == tool_name and ahash == args_hash:
                same_args_calls += 1
            else:
                break  # 不同的工具或参数，停止计数
        
        # 相同参数连续调用5次时停止（无论成功或失败）
        if same_args_calls >= 5:
            return True, f"工具 '{tool_name}' 使用相同参数已被调用 {same_args_calls} 次，停止执行以防止无限循环。如果需要查看配置，结果已在之前的调用中返回。", False
        
        # 统计相同工具+参数的连续失败次数
        same_args_failures = 0
        for name, ahash, success in reversed(self.tool_failure_history):
            if name == tool_name and ahash == args_hash:
                if not success:
                    same_args_failures += 1
                else:
                    break  # 遇到第一次成功时停止
            else:
                break  # 不同的工具或参数，停止计数
        
        if same_args_failures >= 3:
            return True, f"工具 '{tool_name}' 使用相同参数连续失败 {same_args_failures} 次，停止执行以防止无限循环", False
        
        # 统计相同工具的连续失败次数（任意参数）
        same_tool_failures = 0
        for name, ahash, success in reversed(self.tool_failure_history):
            if name == tool_name:
                if not success:
                    same_tool_failures += 1
                else:
                    break  # 遇到第一次成功时停止
            else:
                break  # 不同的工具，停止计数
        
        # 8次失败时强制停止 - 用关键消息中止
        if same_tool_failures >= 8:
            return True, f"抱歉，我没能完成这个任务。可能是我理解有误或者当前方法不太合适。\n\n建议你：\n• 换个方式描述需求试试\n• 把任务拆分成更小的步骤\n• 或者换个思路来解决", True
        
        # 6次失败时警告
        if same_tool_failures >= 6:
            return True, f"工具 '{tool_name}' 连续失败 {same_tool_failures} 次（使用不同参数），停止执行以防止无限循环", False
        
        return False, "", False
    
    def _record_tool_result(self, tool_name: str, args: dict, success: bool):
        """记录工具执行结果用于失败跟踪"""
        args_hash = self._hash_args(args)
        self.tool_failure_history.append((tool_name, args_hash, success))
        # 只保留最近50条记录以避免内存膨胀
        if len(self.tool_failure_history) > 50:
            self.tool_failure_history = self.tool_failure_history[-50:]

    def run_stream(self, user_message: str) -> str:
        """
        执行流式推理循环
        
        参数:
            user_message: 用户消息
            
        返回值:
            最终响应文本
        """
        # 记录用户消息和模型信息
        logger.info(f"🤖 {self.model.model} | 👤 {user_message}")
        
        # 添加用户消息（Claude格式 - 使用内容块保持一致性）
        self.messages.append({
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": user_message
                }
            ]
        })

        # 在agent循环开始前只修剪一次上下文，而不是在工具步骤期间。
        # 这确保当前运行期间创建的tool_use/tool_result链
        # 永远不会在执行中途被剥离（这会导致LLM循环）。
        self._trim_messages()

        # 修剪后验证：修剪可能会在边界处留下孤立的tool_use
        # （例如，最后保留的轮次以assistant tool_use结束，但其
        # tool_result在丢弃的轮次中）。
        self._validate_and_fix_messages()

        self._emit_event("agent_start")

        final_response = ""
        turn = 0

        try:
            while turn < self.max_turns:
                turn += 1
                logger.info(f"[Agent] 第 {turn} 轮")
                self._emit_event("turn_start", {"turn": turn})

                # 调用LLM（启用retry_on_empty以提高可靠性）
                assistant_msg, tool_calls = self._call_llm_stream(retry_on_empty=True)
                final_response = assistant_msg

                # 没有工具调用，结束循环
                if not tool_calls:
                    # 检查是否返回了空响应
                    if not assistant_msg:
                        logger.warning(f"[Agent] LLM returned empty response after retry (no content and no tool calls)")
                        logger.info(f"[Agent] This usually happens when LLM thinks the task is complete after tool execution")
                        
                        # 如果之前有工具调用，强制要求 LLM 生成文本回复
                        if turn > 1:
                            logger.info(f"[Agent] Requesting explicit response from LLM...")
                            
                            # 添加一条消息，明确要求回复用户
                            self.messages.append({
                                "role": "user",
                                "content": [{
                                    "type": "text",
                                    "text": "请向用户说明刚才工具执行的结果或回答用户的问题。"
                                }]
                            })
                            
                            # 再调用一次 LLM
                            assistant_msg, tool_calls = self._call_llm_stream(retry_on_empty=False)
                            final_response = assistant_msg
                            
                            # 如果还是空，才使用 fallback
                            if not assistant_msg and not tool_calls:
                                logger.warning(f"[Agent] Still empty after explicit request")
                                final_response = (
                                    "抱歉，我暂时无法生成回复。请尝试换一种方式描述你的需求，或稍后再试。"
                                )
                                logger.info(f"Generated fallback response for empty LLM output")
                        else:
                            # 第一轮就空回复，直接 fallback
                            final_response = (
                                "抱歉，我暂时无法生成回复。请尝试换一种方式描述你的需求，或稍后再试。"
                            )
                            logger.info(f"Generated fallback response for empty LLM output")
                    else:
                        logger.info(f"💭 {assistant_msg[:150]}{'...' if len(assistant_msg) > 150 else ''}")
                    
                    logger.debug(f"✅ 完成 (无工具调用)")
                    self._emit_event("turn_end", {
                        "turn": turn,
                        "has_tool_calls": False
                    })
                    break

                # 记录工具调用和参数
                tool_calls_str = []
                for tc in tool_calls:
                    # 安全处理None或缺失的参数
                    args = tc.get('arguments') or {}
                    if isinstance(args, dict):
                        args_str = ', '.join([f"{k}={v}" for k, v in args.items()])
                        if args_str:
                            tool_calls_str.append(f"{tc['name']}({args_str})")
                        else:
                            tool_calls_str.append(tc['name'])
                    else:
                        tool_calls_str.append(tc['name'])
                logger.info(f"🔧 {', '.join(tool_calls_str)}")

                # 执行工具
                tool_results = []
                tool_result_blocks = []

                try:
                    for tool_call in tool_calls:
                        result = self._execute_tool(tool_call)
                        tool_results.append(result)
                        
                        # 调试：检查工具是否使用相同参数重复调用
                        if turn > 2:
                            # 检查最近N次工具调用是否有重复
                            repeat_count = sum(
                                1 for name, ahash, _ in self.tool_failure_history[-10:]
                                if name == tool_call["name"] and ahash == self._hash_args(tool_call["arguments"])
                            )
                            if repeat_count >= 3:
                                logger.warning(
                                    f"⚠️  Tool '{tool_call['name']}' has been called {repeat_count} times "
                                    f"with same arguments. This may indicate a loop."
                                )
                        
                        # 检查这是否是要发送的文件（来自read工具）
                        if result.get("status") == "success" and isinstance(result.get("result"), dict):
                            result_data = result.get("result")
                            if result_data.get("type") == "file_to_send":
                                # 存储文件元数据以供后续发送
                                self.files_to_send.append(result_data)
                                logger.info(f"📎 检测到待发送文件: {result_data.get('file_name', result_data.get('path'))}")
                        
                        # 检查严重错误 - 中止整个对话
                        if result.get("status") == "critical_error":
                            logger.error(f"💥 检测到严重错误，终止对话")
                            final_response = result.get('result', '任务执行失败')
                            return final_response
                        
                        # 以紧凑格式记录工具结果
                        status_emoji = "✅" if result.get("status") == "success" else "❌"
                        result_data = result.get('result', '')
                        # 格式化结果字符串，正确支持中文字符
                        if isinstance(result_data, (dict, list)):
                            result_str = json.dumps(result_data, ensure_ascii=False)
                        else:
                            result_str = str(result_data)
                        logger.info(f"  {status_emoji} {tool_call['name']} ({result.get('execution_time', 0):.2f}s): {result_str[:200]}{'...' if len(result_str) > 200 else ''}")

                        # 构建工具结果块（Claude格式）
                        # 以LLM易于理解的方式格式化内容
                        is_error = result.get("status") == "error"

                        if is_error:
                            # 对于错误，提供清晰的错误消息
                            result_content = f"Error: {result.get('result', 'Unknown error')}"
                        elif isinstance(result.get('result'), dict):
                            # 对于字典结果，使用JSON格式
                            result_content = json.dumps(result.get('result'), ensure_ascii=False)
                        elif isinstance(result.get('result'), str):
                            # 对于字符串结果，直接使用
                            result_content = result.get('result')
                        else:
                            # 回退到完整JSON
                            result_content = json.dumps(result, ensure_ascii=False)

                        # 截断当前轮次过大的工具结果
                        # 历史轮次将在_trim_messages()中进一步截断
                        MAX_CURRENT_TURN_RESULT_CHARS = 50000
                        if len(result_content) > MAX_CURRENT_TURN_RESULT_CHARS:
                            truncated_len = len(result_content)
                            result_content = result_content[:MAX_CURRENT_TURN_RESULT_CHARS] + \
                                f"\n\n[Output truncated: {truncated_len} chars total, showing first {MAX_CURRENT_TURN_RESULT_CHARS} chars]"
                            logger.info(f"📎 Truncated tool result for '{tool_call['name']}': {truncated_len} -> {MAX_CURRENT_TURN_RESULT_CHARS} chars")

                        tool_result_block = {
                            "type": "tool_result",
                            "tool_use_id": tool_call["id"],
                            "content": result_content
                        }
                        
                        # 为Claude API添加is_error字段（帮助模型理解失败）
                        if is_error:
                            tool_result_block["is_error"] = True
                        
                        tool_result_blocks.append(tool_result_block)
                
                finally:
                    # 关键：始终添加tool_result以维护消息历史完整性
                    # 即使工具执行失败，我们也必须添加错误结果以匹配tool_use
                    if tool_result_blocks:
                        # 将工具结果作为用户消息添加到消息历史（Claude格式）
                        self.messages.append({
                            "role": "user",
                            "content": tool_result_blocks
                        })
                        
                        # 检测潜在的无限循环：相同工具多次成功调用
                        # 如果检测到，向LLM添加提示以停止调用工具并提供响应
                        if turn >= 3 and len(tool_calls) > 0:
                            tool_name = tool_calls[0]["name"]
                            args_hash = self._hash_args(tool_calls[0]["arguments"])
                            
                            # 统计最近相同工具+参数的成功调用次数
                            recent_success_count = 0
                            for name, ahash, success in reversed(self.tool_failure_history[-10:]):
                                if name == tool_name and ahash == args_hash and success:
                                    recent_success_count += 1
                            
                            # 如果工具使用相同参数成功调用3次以上，添加提示以停止循环
                            if recent_success_count >= 3:
                                logger.warning(
                                    f"⚠️  Detected potential loop: '{tool_name}' called {recent_success_count} times "
                                    f"with same args. Adding hint to LLM to provide final response."
                                )
                                # 添加温和的提示消息以引导LLM响应
                                self.messages.append({
                                    "role": "user",
                                    "content": [{
                                        "type": "text",
                                        "text": "工具已成功执行并返回结果。请基于这些信息向用户做出回复，不要重复调用相同的工具。"
                                    }]
                                })
                    elif tool_calls:
                        # 如果我们有tool_calls但没有tool_result_blocks（意外错误），
                        # 为所有工具调用创建错误结果以维护消息完整性
                        logger.warning("⚠️ Tool execution interrupted, adding error results to maintain message history")
                        emergency_blocks = []
                        for tool_call in tool_calls:
                            emergency_blocks.append({
                                "type": "tool_result",
                                "tool_use_id": tool_call["id"],
                                "content": "Error: Tool execution was interrupted",
                                "is_error": True
                            })
                        self.messages.append({
                            "role": "user",
                            "content": emergency_blocks
                        })

                self._emit_event("turn_end", {
                    "turn": turn,
                    "has_tool_calls": True,
                    "tool_count": len(tool_calls)
                })

            if turn >= self.max_turns:
                logger.warning(f"⚠️  已达到最大决策步数限制: {self.max_turns}")
                
                # 强制模型在不调用工具的情况下进行总结
                logger.info(f"[Agent] Requesting summary from LLM after reaching max steps...")
                
                # 记住注入提示前的位置，以便稍后移除
                prompt_insert_idx = len(self.messages)
                
                # 添加临时提示以强制总结
                self.messages.append({
                    "role": "user",
                    "content": [{
                        "type": "text",
                        "text": f"你已经执行了{turn}个决策步骤，达到了单次运行的最大步数限制。请总结一下你目前的执行过程和结果，告诉用户当前的进展情况。不要再调用工具，直接用文字回复。"
                    }]
                })
                
                # 再次调用LLM以获取总结（不重试以避免循环）
                try:
                    summary_response, summary_tools = self._call_llm_stream(retry_on_empty=False)
                    if summary_response:
                        final_response = summary_response
                        logger.info(f"💭 Summary: {summary_response[:150]}{'...' if len(summary_response) > 150 else ''}")
                    else:
                        # 如果模型仍然没有响应的回退方案
                        final_response = (
                            f"我已经执行了{turn}个决策步骤，达到了单次运行的步数上限。"
                            "任务可能还未完全完成，建议你将任务拆分成更小的步骤，或者换一种方式描述需求。"
                        )
                except Exception as e:
                    logger.warning(f"Failed to get summary from LLM: {e}")
                    final_response = (
                        f"我已经执行了{turn}个决策步骤，达到了单次运行的步数上限。"
                        "任务可能还未完全完成，建议你将任务拆分成更小的步骤，或者换一种方式描述需求。"
                    )
                finally:
                    # 从历史记录中移除注入的用户提示以避免污染
                    # 持久化的对话记录。助手总结（如果有）
                    # 已经由_call_llm_stream追加并保留。
                    if (prompt_insert_idx < len(self.messages)
                            and self.messages[prompt_insert_idx].get("role") == "user"):
                        self.messages.pop(prompt_insert_idx)
                        logger.debug("[Agent] Removed injected max-steps prompt from message history")

        except Exception as e:
            logger.error(f"❌ Agent执行错误: {e}")
            self._emit_event("error", {"error": str(e)})
            raise

        finally:
            logger.info(f"[Agent] 🏁 完成 ({turn}轮)")
            self._emit_event("agent_end", {"final_response": final_response})

        return final_response

    def _call_llm_stream(self, retry_on_empty=True, retry_count=0, max_retries=3,
                         _overflow_retry: bool = False) -> Tuple[str, List[Dict]]:
        """
        使用流式调用LLM并在错误时自动重试
        
        参数:
            retry_on_empty: 如果收到空响应是否重试一次
            retry_count: 当前重试次数（内部使用）
            max_retries: API错误的最大重试次数
            _overflow_retry: 内部标志，表示这是上下文溢出后的重试
        
        返回值:
            (response_text, tool_calls) - (响应文本, 工具调用列表)
        """
        # 验证和修复消息历史（例如孤立的tool_result块）。
        # 上下文修剪在run_stream()中循环开始前只进行一次，
        # 不在这里 — 执行中途修剪会剥离当前运行的
        # tool_use/tool_result链并导致LLM循环。
        self._validate_and_fix_messages()

        # 准备消息
        messages = self._prepare_messages()
        turns = self._identify_complete_turns()
        logger.info(f"Sending {len(messages)} messages ({len(turns)} turns) to LLM")

        # 准备工具定义（OpenAI/Claude格式）
        tools_schema = None
        if self.tools:
            tools_schema = []
            for tool in self.tools.values():
                tools_schema.append({
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": tool.params  # Claude使用input_schema
                })

        # 创建请求
        request = LLMRequest(
            messages=messages,
            temperature=0,
            stream=True,
            tools=tools_schema,
            system=self.system_prompt  # 为Claude API单独传递系统提示
        )

        self._emit_event("message_start", {"role": "assistant"})

        # 流式响应
        full_content = ""
        tool_calls_buffer = {}  # {index: {id, name, arguments}}
        gemini_raw_parts = None  # 保留Gemini thoughtSignature用于往返
        stop_reason = None  # 跟踪流停止的原因

        try:
            stream = self.model.call_stream(request)

            for chunk in stream:
                # 检查错误
                if isinstance(chunk, dict) and chunk.get("error"):
                    # Extract error message from nested structure / 从嵌套结构中提取错误消息
                    error_data = chunk.get("error", {})
                    if isinstance(error_data, dict):
                        error_msg = error_data.get("message", chunk.get("message", "Unknown error"))
                        error_code = error_data.get("code", "")
                        error_type = error_data.get("type", "")
                    else:
                        error_msg = chunk.get("message", str(error_data))
                        error_code = ""
                        error_type = ""
                    
                    status_code = chunk.get("status_code", "N/A")
                    
                    # Log error with all available information / 记录所有可用信息的错误
                    logger.error(f"🔴 Stream API Error:")
                    logger.error(f"   Message: {error_msg}")
                    logger.error(f"   Status Code: {status_code}")
                    logger.error(f"   Error Code: {error_code}")
                    logger.error(f"   Error Type: {error_type}")
                    logger.error(f"   Full chunk: {chunk}")
                    
                    # Check if this is a context overflow error (keyword-based, works for all models) / 检查是否为上下文溢出错误（基于关键词，适用于所有模型）
                    # Don't rely on specific status codes as different providers use different codes / 不要依赖特定的状态码，因为不同提供商使用不同的代码
                    error_msg_lower = error_msg.lower()
                    is_overflow = any(keyword in error_msg_lower for keyword in [
                        'context length exceeded', 'maximum context length', 'prompt is too long',
                        'context overflow', 'context window', 'too large', 'exceeds model context',
                        'request_too_large', 'request exceeds the maximum size', 'tokens exceed'
                    ])
                    
                    if is_overflow:
                        # Mark as context overflow for special handling / 标记为上下文溢出以进行特殊处理
                        raise Exception(f"[CONTEXT_OVERFLOW] {error_msg} (Status: {status_code})")
                    else:
                        # Raise exception with full error message for retry logic / 抛出包含完整错误消息的异常以进行重试逻辑
                        raise Exception(f"{error_msg} (Status: {status_code}, Code: {error_code}, Type: {error_type})")

                # Parse chunk / 解析块
                if isinstance(chunk, dict) and chunk.get("choices"):
                    choice = chunk["choices"][0]
                    delta = choice.get("delta", {})
                    
                    # Capture finish_reason if present / 如果存在则捕获finish_reason
                    finish_reason = choice.get("finish_reason")
                    if finish_reason:
                        stop_reason = finish_reason

                    # Skip reasoning_content (internal thinking from models like GLM-5) / 跳过reasoning_content（来自GLM-5等模型的内部思考）
                    reasoning_delta = delta.get("reasoning_content") or ""
                    # if reasoning_delta:
                    #     logger.debug(f"🧠 [thinking] {reasoning_delta[:100]}...")

                    # Handle text content / 处理文本内容
                    content_delta = delta.get("content") or ""
                    if content_delta:
                        # Filter out <think> tags from content / 从内容中过滤掉<think>标签
                        filtered_delta = self._filter_think_tags(content_delta)
                        full_content += filtered_delta
                        if filtered_delta:  # Only emit if there's content after filtering
                            self._emit_event("message_update", {"delta": filtered_delta})

                    # Handle tool calls / 处理工具调用
                    if "tool_calls" in delta and delta["tool_calls"]:
                        for tc_delta in delta["tool_calls"]:
                            index = tc_delta.get("index", 0)

                            if index not in tool_calls_buffer:
                                tool_calls_buffer[index] = {
                                    "id": "",
                                    "name": "",
                                    "arguments": ""
                                }

                            if tc_delta.get("id"):
                                tool_calls_buffer[index]["id"] = tc_delta["id"]

                            if "function" in tc_delta:
                                func = tc_delta["function"]
                                if func.get("name"):
                                    tool_calls_buffer[index]["name"] = func["name"]
                                if func.get("arguments"):
                                    tool_calls_buffer[index]["arguments"] += func["arguments"]

                    # Preserve _gemini_raw_parts for Gemini thoughtSignature round-trip / 保留_gemini_raw_parts用于Gemini thoughtSignature往返
                    if "_gemini_raw_parts" in delta:
                        gemini_raw_parts = delta["_gemini_raw_parts"]

        except Exception as e:
            error_str = str(e)
            error_str_lower = error_str.lower()
            
            # Check if error is context overflow (non-retryable, needs session reset) / 检查错误是否为上下文溢出（不可重试，需要会话重置）
            # Method 1: Check for special marker (set in stream error handling above) / 方法1：检查特殊标记（在上面流错误处理中设置）
            is_context_overflow = '[context_overflow]' in error_str_lower
            
            # Method 2: Fallback to keyword matching for non-stream errors / 方法2：对非流错误回退到关键词匹配
            if not is_context_overflow:
                is_context_overflow = any(keyword in error_str_lower for keyword in [
                    'context length exceeded', 'maximum context length', 'prompt is too long',
                    'context overflow', 'context window', 'too large', 'exceeds model context',
                    'request_too_large', 'request exceeds the maximum size'
                ])
            
            # Check if error is message format error (incomplete tool_use/tool_result pairs) / 检查错误是否为消息格式错误（不完整的tool_use/tool_result对）
            # This happens when previous conversation had tool failures or context trimming / 当之前的对话有工具失败或上下文修剪时会发生
            # broke tool_use/tool_result pairs.
            # Note: MiniMax returns error 2013 "tool result's tool id(...) not found" for / 注意：MiniMax返回错误2013"tool result's tool id(...) not found"
            # tool_call_id mismatches — the keywords below are intentionally broad to catch / tool_call_id不匹配 — 下面的关键词故意宽泛以捕获
            # both standard (Claude/OpenAI) and provider-specific (MiniMax) variants. / 标准（Claude/OpenAI）和提供商特定（MiniMax）变体。
            is_message_format_error = any(keyword in error_str_lower for keyword in [
                'tool_use', 'tool_result', 'tool result', 'without', 'immediately after',
                'corresponding', 'must have', 'each',
                'tool_call_id', 'tool id', 'is not found', 'not found', 'tool_calls',
                'must be a response to a preceeding message',
                '2013',  # MiniMax error code for tool_call_id mismatch
            ]) and ('400' in error_str_lower or 'status: 400' in error_str_lower
                     or 'invalid_request' in error_str_lower
                     or 'invalidparameter' in error_str_lower)
            
            if is_context_overflow or is_message_format_error:
                error_type = "context overflow" if is_context_overflow else "message format error"
                logger.error(f"💥 {error_type} detected: {e}")

                # Flush memory before trimming to preserve context that will be lost / 在修剪前刷新内存以保留将要丢失的上下文
                if is_context_overflow and self.agent.memory_manager:
                    user_id = getattr(self.agent, '_current_user_id', None)
                    self.agent.memory_manager.flush_memory(
                        messages=self.messages, user_id=user_id,
                        reason="overflow", max_messages=0
                    )

                # Strategy: try aggressive trimming first, only clear as last resort / 策略：先尝试激进修剪，仅作为最后手段才清除
                if is_context_overflow and not _overflow_retry:
                    trimmed = self._aggressive_trim_for_overflow()
                    if trimmed:
                        logger.warning("🔄 Aggressively trimmed context, retrying...")
                        return self._call_llm_stream(
                            retry_on_empty=retry_on_empty,
                            retry_count=retry_count,
                            max_retries=max_retries,
                            _overflow_retry=True
                        )

                # Aggressive trim didn't help or this is a message format error / 激进修剪没有帮助或这是消息格式错误
                # -> clear everything and also purge DB to prevent reload of dirty data / -> 清除所有内容并清除数据库以防止重新加载脏数据
                logger.warning("🔄 Clearing conversation history to recover")
                self.messages.clear()
                self._clear_session_db()
                if is_context_overflow:
                    raise Exception(
                        "抱歉，对话历史过长导致上下文溢出。我已清空历史记录，请重新描述你的需求。"
                    )
                else:
                    raise Exception(
                        "抱歉，之前的对话出现了问题。我已清空历史记录，请重新发送你的消息。"
                    )
            
            # Check if error is rate limit (429) / 检查错误是否为速率限制（429）
            is_rate_limit = '429' in error_str_lower or 'rate limit' in error_str_lower
            
            # Check if error is retryable (timeout, connection, server busy, etc.) / 检查错误是否可重试（超时、连接、服务器繁忙等）
            is_retryable = any(keyword in error_str_lower for keyword in [
                'timeout', 'timed out', 'connection', 'network', 
                'rate limit', 'overloaded', 'unavailable', 'busy', 'retry',
                '429', '500', '502', '503', '504', '512'
            ])
            
            if is_retryable and retry_count < max_retries:
                # Rate limit needs longer wait time / 速率限制需要更长的等待时间
                if is_rate_limit:
                    wait_time = 30 + (retry_count * 15)  # 30s, 45s, 60s for rate limit
                else:
                    wait_time = (retry_count + 1) * 2  # 2s, 4s, 6s for other errors
                
                logger.warning(f"⚠️ LLM API error (attempt {retry_count + 1}/{max_retries}): {e}")
                logger.info(f"Retrying in {wait_time}s...")
                time.sleep(wait_time)
                return self._call_llm_stream(
                    retry_on_empty=retry_on_empty, 
                    retry_count=retry_count + 1,
                    max_retries=max_retries
                )
            else:
                if retry_count >= max_retries:
                    logger.error(f"❌ LLM API error after {max_retries} retries: {e}", exc_info=True)
                else:
                    logger.error(f"❌ LLM call error (non-retryable): {e}", exc_info=True)
                raise

        # Parse tool calls / 解析工具调用
        tool_calls = []
        for idx in sorted(tool_calls_buffer.keys()):
            tc = tool_calls_buffer[idx]

            # Ensure tool call has a valid ID (some providers return empty/None IDs) / 确保工具调用有有效ID（某些提供商返回空/None ID）
            tool_id = tc.get("id") or ""
            if not tool_id:
                import uuid
                tool_id = f"call_{uuid.uuid4().hex[:24]}"

            try:
                # Safely get arguments, handle None case / 安全获取参数，处理None情况
                args_str = tc.get("arguments") or ""
                arguments = json.loads(args_str) if args_str else {}
            except json.JSONDecodeError as e:
                # Handle None or invalid arguments safely / 安全处理None或无效参数
                args_str = tc.get('arguments') or ""
                args_preview = args_str[:200] if len(args_str) > 200 else args_str
                logger.error(f"Failed to parse tool arguments for {tc['name']}")
                logger.error(f"Arguments length: {len(args_str)} chars")
                logger.error(f"Arguments preview: {args_preview}...")
                logger.error(f"JSON decode error: {e}")

                # Return a clear error message to the LLM instead of empty dict / 向LLM返回清晰的错误消息而不是空字典
                # This helps the LLM understand what went wrong / 这帮助LLM理解出了什么问题
                tool_calls.append({
                    "id": tool_id,
                    "name": tc["name"],
                    "arguments": {},
                    "_parse_error": f"Invalid JSON in tool arguments: {args_preview}... Error: {str(e)}. Tip: For large content, consider splitting into smaller chunks or using a different approach."
                })
                continue

            tool_calls.append({
                "id": tool_id,
                "name": tc["name"],
                "arguments": arguments
            })

        # Check for empty response and retry once if enabled / 检查空响应并在启用时重试一次
        if retry_on_empty and not full_content and not tool_calls:
            logger.warning(f"⚠️  LLM returned empty response (stop_reason: {stop_reason}), retrying once...")
            self._emit_event("message_end", {
                "content": "",
                "tool_calls": [],
                "empty_retry": True,
                "stop_reason": stop_reason
            })
            # Retry without retry flag to avoid infinite loop / 不带重试标志重试以避免无限循环
            return self._call_llm_stream(
                retry_on_empty=False, 
                retry_count=retry_count,
                max_retries=max_retries
            )

        # Filter full_content one more time (in case tags were split across chunks) / 再次过滤full_content（以防标签被分割到多个块中）
        full_content = self._filter_think_tags(full_content)
        
        # Add assistant message to history (Claude format uses content blocks) / 将助手消息添加到历史（Claude格式使用内容块）
        assistant_msg = {"role": "assistant", "content": []}

        # Add text content block if present / 如果存在则添加文本内容块
        if full_content:
            assistant_msg["content"].append({
                "type": "text",
                "text": full_content
            })

        # Add tool_use blocks if present / 如果存在则添加tool_use块
        if tool_calls:
            for tc in tool_calls:
                assistant_msg["content"].append({
                    "type": "tool_use",
                    "id": tc.get("id", ""),
                    "name": tc.get("name", ""),
                    "input": tc.get("arguments", {})
                })
        
        if gemini_raw_parts:
            assistant_msg["_gemini_raw_parts"] = gemini_raw_parts

        # Only append if content is not empty / 仅在内容不为空时追加
        if assistant_msg["content"]:
            self.messages.append(assistant_msg)

        self._emit_event("message_end", {
            "content": full_content,
            "tool_calls": tool_calls
        })

        return full_content, tool_calls

    def _execute_tool(self, tool_call: Dict) -> Dict[str, Any]:
        """
        执行工具
        
        参数:
            tool_call: {"id": str, "name": str, "arguments": dict} - 工具调用信息
            
        返回值:
            工具执行结果
        """
        tool_name = tool_call["name"]
        tool_id = tool_call["id"]
        arguments = tool_call["arguments"]

        # Check if there was a JSON parse error / 检查是否有JSON解析错误
        if "_parse_error" in tool_call:
            parse_error = tool_call["_parse_error"]
            logger.error(f"Skipping tool execution due to parse error: {parse_error}")
            result = {
                "status": "error",
                "result": f"Failed to parse tool arguments. {parse_error}. Please ensure your tool call uses valid JSON format with all required parameters.",
                "execution_time": 0
            }
            self._record_tool_result(tool_name, arguments, False)
            return result

        # Check for consecutive failures (retry protection) / 检查连续失败（重试保护）
        should_stop, stop_reason, is_critical = self._check_consecutive_failures(tool_name, arguments)
        if should_stop:
            logger.error(f"🛑 {stop_reason}")
            self._record_tool_result(tool_name, arguments, False)
            
            if is_critical:
                # Critical failure - abort entire conversation / 严重失败 - 中止整个对话
                result = {
                    "status": "critical_error",
                    "result": stop_reason,
                    "execution_time": 0
                }
            else:
                # Normal failure - let LLM try different approach / 正常失败 - 让LLM尝试不同方法
                result = {
                    "status": "error",
                    "result": f"{stop_reason}\n\n当前方法行不通，请尝试完全不同的方法或向用户询问更多信息。",
                    "execution_time": 0
                }
            return result

        self._emit_event("tool_execution_start", {
            "tool_call_id": tool_id,
            "tool_name": tool_name,
            "arguments": arguments
        })

        try:
            tool = self.tools.get(tool_name)
            if not tool:
                raise ValueError(self._build_tool_not_found_message(tool_name))

            # Set tool context / 设置工具上下文
            tool.model = self.model
            tool.context = self.agent

            # Execute tool / 执行工具
            start_time = time.time()
            result: ToolResult = tool.execute_tool(arguments)
            execution_time = time.time() - start_time

            result_dict = {
                "status": result.status,
                "result": result.result,
                "execution_time": execution_time
            }

            # Record tool result for failure tracking / 记录工具结果用于失败跟踪
            success = result.status == "success"
            self._record_tool_result(tool_name, arguments, success)

            # Auto-refresh skills after skill creation / 技能创建后自动刷新技能
            if tool_name == "bash" and result.status == "success":
                command = arguments.get("command", "")
                if "init_skill.py" in command and self.agent.skill_manager:
                    logger.info("Detected skill creation, refreshing skills...")
                    self.agent.refresh_skills()
                    logger.info(f"Skills refreshed! Now have {len(self.agent.skill_manager.skills)} skills")

            self._emit_event("tool_execution_end", {
                "tool_call_id": tool_id,
                "tool_name": tool_name,
                **result_dict
            })

            return result_dict

        except Exception as e:
            logger.error(f"Tool execution error: {e}")
            error_result = {
                "status": "error",
                "result": str(e),
                "execution_time": 0
            }
            # Record failure / 记录失败
            self._record_tool_result(tool_name, arguments, False)
            
            self._emit_event("tool_execution_end", {
                "tool_call_id": tool_id,
                "tool_name": tool_name,
                **error_result
            })
            return error_result

    def _build_tool_not_found_message(self, tool_name: str) -> str:
        """当找不到工具时构建有帮助的错误消息。

        如果skill_manager中存在同名的技能，会读取其SKILL.md并包含内容，以便LLM知道如何使用它。
        """
        available_tools = list(self.tools.keys())
        base_msg = f"Tool '{tool_name}' not found. Available tools: {available_tools}"

        skill_manager = getattr(self.agent, 'skill_manager', None)
        if not skill_manager:
            return base_msg

        skill_entry = skill_manager.get_skill(tool_name)
        if not skill_entry:
            return base_msg

        skill = skill_entry.skill
        skill_md_path = skill.file_path
        skill_content = ""
        try:
            with open(skill_md_path, 'r', encoding='utf-8') as f:
                skill_content = f.read()
        except Exception:
            skill_content = skill.description

        logger.info(
            f"[Agent] Tool '{tool_name}' not found, but matched skill '{skill.name}'. "
            f"Guiding LLM to use the skill instead."
        )

        return (
            f"Tool '{tool_name}' is not a built-in tool, but a matching skill "
            f"'{skill.name}' is available. You should use existing tools (e.g. bash with curl) "
            f"to accomplish this task following the skill instructions below:\n\n"
            f"--- SKILL: {skill.name} (path: {skill_md_path}) ---\n"
            f"{skill_content}\n"
            f"--- END SKILL ---\n\n"
            f"Available tools: {available_tools}"
        )

    def _validate_and_fix_messages(self):
        """Delegate to the shared sanitizer (see message_sanitizer.py)."""
        sanitize_claude_messages(self.messages)

    def _identify_complete_turns(self) -> List[Dict]:
        """
        识别完整的对话轮次
        
        一个完整轮次包括：
        1. 用户消息（text）
        2. AI 回复（可能包含 tool_use）
        3. 工具结果（tool_result，如果有）
        4. 后续 AI 回复（如果有）
        
        Returns:
            List of turns, each turn is a dict with 'messages' list
        """
        turns = []
        current_turn = {'messages': []}
        
        for msg in self.messages:
            role = msg.get('role')
            content = msg.get('content', [])
            
            if role == 'user':
                # Determine if this is a real user query (not a tool_result injection / 确定这是否是真正的用户查询（不是tool_result注入
                # or an internal hint message injected by the agent loop).
                is_user_query = False
                has_tool_result = False
                if isinstance(content, list):
                    has_text = any(
                        isinstance(block, dict) and block.get('type') == 'text'
                        for block in content
                    )
                    has_tool_result = any(
                        isinstance(block, dict) and block.get('type') == 'tool_result'
                        for block in content
                    )
                    # A message with tool_result is always internal, even if it / 带有tool_result的消息始终是内部的，即使它
                    # also contains text blocks (shouldn't happen, but be safe).
                    is_user_query = has_text and not has_tool_result
                elif isinstance(content, str):
                    is_user_query = True
                
                if is_user_query:
                    if current_turn['messages']:
                        turns.append(current_turn)
                    current_turn = {'messages': [msg]}
                else:
                    current_turn['messages'].append(msg)
            else:
                # AI 回复，属于当前轮次
                current_turn['messages'].append(msg)
        
        # 添加最后一个轮次
        if current_turn['messages']:
            turns.append(current_turn)
        
        return turns
    
    def _estimate_turn_tokens(self, turn: Dict) -> int:
        """估算一个轮次的 tokens"""
        return sum(
            self.agent._estimate_message_tokens(msg) 
            for msg in turn['messages']
        )

    def _truncate_historical_tool_results(self):
        """
        截断历史消息中的tool_result内容以减少上下文大小。

        当前轮次结果保持在30K字符（在创建时截断）。
        历史轮次结果在这里进一步截断为20K字符。
        这在基于token的截断之前运行，以便我们首先缩小过大的结果，
        可能避免需要丢弃整个轮次。
        """
        MAX_HISTORY_RESULT_CHARS = 20000

        if len(self.messages) < 2:
            return

        # Find where the last user text message starts (= current turn boundary) / 找到最后一条用户文本消息开始的位置（=当前轮次边界）
        # We skip the current turn's messages to preserve their full content / 我们跳过当前轮次的消息以保留其完整内容
        current_turn_start = len(self.messages)
        for i in range(len(self.messages) - 1, -1, -1):
            msg = self.messages[i]
            if msg.get("role") == "user":
                content = msg.get("content", [])
                if isinstance(content, list) and any(
                    isinstance(b, dict) and b.get("type") == "text" for b in content
                ):
                    current_turn_start = i
                    break
                elif isinstance(content, str):
                    current_turn_start = i
                    break

        truncated_count = 0
        for i in range(current_turn_start):
            msg = self.messages[i]
            if msg.get("role") != "user":
                continue
            content = msg.get("content", [])
            if not isinstance(content, list):
                continue

            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                result_str = block.get("content", "")
                if isinstance(result_str, str) and len(result_str) > MAX_HISTORY_RESULT_CHARS:
                    original_len = len(result_str)
                    block["content"] = result_str[:MAX_HISTORY_RESULT_CHARS] + \
                        f"\n\n[Historical output truncated: {original_len} -> {MAX_HISTORY_RESULT_CHARS} chars]"
                    truncated_count += 1

        if truncated_count > 0:
            logger.info(f"📎 Truncated {truncated_count} historical tool result(s) to {MAX_HISTORY_RESULT_CHARS} chars")

    def _aggressive_trim_for_overflow(self) -> bool:
        """
        当API返回实际溢出错误时，积极地修剪上下文。

        此方法超出了正常的_trim_messages功能：
        1. 将所有工具结果（包括当前轮次）截断到较小的限制
        2. 只保留最后5个完整的对话轮次
        3. 截断过长的用户消息

        返回值:
            如果消息被修剪（值得重试）则返回True，如果没有可修剪的内容则返回False
        """
        if not self.messages:
            return False

        original_count = len(self.messages)

        # Step 1: Aggressively truncate ALL tool results to 5K chars / 步骤1：将所有工具结果激进地截断到5K字符
        AGGRESSIVE_LIMIT = 10000
        truncated = 0
        for msg in self.messages:
            content = msg.get("content", [])
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                # Truncate tool_result blocks / 截断tool_result块
                if block.get("type") == "tool_result":
                    result_str = block.get("content", "")
                    if isinstance(result_str, str) and len(result_str) > AGGRESSIVE_LIMIT:
                        block["content"] = (
                            result_str[:AGGRESSIVE_LIMIT]
                            + f"\n\n[Truncated for context recovery: "
                            f"{len(result_str)} -> {AGGRESSIVE_LIMIT} chars]"
                        )
                        truncated += 1
                # Truncate tool_use input blocks (e.g. large write content) / 截断tool_use输入块（例如大写入内容）
                if block.get("type") == "tool_use" and isinstance(block.get("input"), dict):
                    input_str = json.dumps(block["input"], ensure_ascii=False)
                    if len(input_str) > AGGRESSIVE_LIMIT:
                        # Keep only a summary of the input / 仅保留输入的摘要
                        for key, val in block["input"].items():
                            if isinstance(val, str) and len(val) > 1000:
                                block["input"][key] = (
                                    val[:1000]
                                    + f"... [truncated {len(val)} chars]"
                                )
                        truncated += 1

        # Step 2: Truncate overly long user text messages (e.g. pasted content) / 步骤2：截断过长的用户文本消息（例如粘贴的内容）
        USER_MSG_LIMIT = 10000
        for msg in self.messages:
            if msg.get("role") != "user":
                continue
            content = msg.get("content", [])
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text = block.get("text", "")
                        if len(text) > USER_MSG_LIMIT:
                            block["text"] = (
                                text[:USER_MSG_LIMIT]
                                + f"\n\n[Message truncated for context recovery: "
                                f"{len(text)} -> {USER_MSG_LIMIT} chars]"
                            )
                            truncated += 1
            elif isinstance(content, str) and len(content) > USER_MSG_LIMIT:
                msg["content"] = (
                    content[:USER_MSG_LIMIT]
                    + f"\n\n[Message truncated for context recovery: "
                    f"{len(content)} -> {USER_MSG_LIMIT} chars]"
                )
                truncated += 1

        # Step 3: Keep only the last 5 complete turns / 步骤3：仅保留最后5个完整轮次
        turns = self._identify_complete_turns()
        if len(turns) > 5:
            kept_turns = turns[-5:]
            new_messages = []
            for turn in kept_turns:
                new_messages.extend(turn["messages"])
            removed = len(turns) - 5
            self.messages[:] = new_messages
            logger.info(
                f"🔧 Aggressive trim: removed {removed} old turns, "
                f"truncated {truncated} large blocks, "
                f"{original_count} -> {len(self.messages)} messages"
            )
            return True

        if truncated > 0:
            logger.info(
                f"🔧 Aggressive trim: truncated {truncated} large blocks "
                f"(no turns removed, only {len(turns)} turn(s) left)"
            )
            return True

        # Nothing left to trim / 没有可修剪的内容了
        logger.warning("🔧 Aggressive trim: nothing to trim, will clear history")
        return False

    def _trim_messages(self):
        """
        智能清理消息历史，保持对话完整性

        使用完整轮次作为清理单位，确保：
        1. 不会在对话中间截断
        2. 工具调用链（tool_use + tool_result）保持完整
        3. 每轮对话都是完整的（用户消息 + AI回复 + 工具调用）
        """
        if not self.messages or not self.agent:
            return

        # Step 0: Truncate large tool results in historical turns (30K -> 10K) / 步骤0：截断历史轮次中的大工具结果（30K -> 10K）
        self._truncate_historical_tool_results()

        # Step 1: 识别完整轮次
        turns = self._identify_complete_turns()
        
        if not turns:
            return
        
        # Step 2: 轮次限制 - 超出时移除前一半，保留后一半
        if len(turns) > self.max_context_turns:
            removed_count = len(turns) // 2
            keep_count = len(turns) - removed_count
            
            # Flush discarded turns to daily memory / 将丢弃的轮次刷新到每日记忆
            if self.agent.memory_manager:
                discarded_messages = []
                for turn in turns[:removed_count]:
                    discarded_messages.extend(turn["messages"])
                if discarded_messages:
                    user_id = getattr(self.agent, '_current_user_id', None)
                    self.agent.memory_manager.flush_memory(
                        messages=discarded_messages, user_id=user_id,
                        reason="trim", max_messages=0
                    )
            
            turns = turns[-keep_count:]
            
            logger.info(
                f"💾 上下文轮次超限: {keep_count + removed_count} > {self.max_context_turns}，"
                f"裁剪至 {keep_count} 轮（移除 {removed_count} 轮）"
            )

        # Step 3: Token 限制 - 保留完整轮次
        # Get context window from agent (based on model) / 从agent获取上下文窗口（基于模型）
        context_window = self.agent._get_model_context_window()

        # Use configured max_context_tokens if available / 如果可用则使用配置的max_context_tokens
        if hasattr(self.agent, 'max_context_tokens') and self.agent.max_context_tokens:
            max_tokens = self.agent.max_context_tokens
        else:
            # Reserve 10% for response generation / 为响应生成保留10%
            reserve_tokens = int(context_window * 0.1)
            max_tokens = context_window - reserve_tokens

        # Estimate system prompt tokens / 估算系统提示tokens
        system_tokens = self.agent._estimate_message_tokens({"role": "system", "content": self.system_prompt})
        available_tokens = max_tokens - system_tokens

        # Calculate current tokens / 计算当前tokens
        current_tokens = sum(self._estimate_turn_tokens(turn) for turn in turns)
        
        # If under limit, reconstruct messages and return / 如果在限制内，重建消息并返回
        if current_tokens + system_tokens <= max_tokens:
            # Reconstruct message list from turns / 从轮次重建消息列表
            new_messages = []
            for turn in turns:
                new_messages.extend(turn['messages'])
            
            old_count = len(self.messages)
            self.messages = new_messages
            
            # Log if we removed messages due to turn limit / 如果由于轮次限制移除消息则记录日志
            if old_count > len(self.messages):
                logger.info(f"   重建消息列表: {old_count} -> {len(self.messages)} 条消息")
            return

        # Token limit exceeded — tiered strategy based on turn count: / Token限制超出 — 基于轮次数的分层策略：
        #
        #   Few turns (<5):  Compress ALL turns to text-only (strip tool chains, / 轮次较少（<5）：将所有轮次压缩为纯文本（剥离工具链，
        #                    keep user query + final reply).  Never discard turns / 保留用户查询+最终回复）。绝不丢弃轮次
        #                    — losing even one is too painful when context is thin. / — 当上下文稀疏时，丢失任何一个都太痛苦了。
        #
        #   Many turns (>=5): Directly discard the first half of turns. / 轮次较多（>=5）：直接丢弃前半部分轮次。
        #                     With enough turns the oldest ones are less / 轮次足够多时，最早的轮次不那么
        #                     critical, and keeping the recent half intact / 关键，保持最近一半完整
        #                     (with full tool chains) is more useful. / （带完整工具链）更有用。

        COMPRESS_THRESHOLD = 5

        if len(turns) < COMPRESS_THRESHOLD:
            # --- Few turns: compress ALL turns to text-only, never discard --- / --- 轮次较少：将所有轮次压缩为纯文本，绝不丢弃 ---
            compressed_turns = []
            for t in turns:
                compressed = compress_turn_to_text_only(t)
                if compressed["messages"]:
                    compressed_turns.append(compressed)

            new_messages = []
            for turn in compressed_turns:
                new_messages.extend(turn["messages"])

            new_tokens = sum(self._estimate_turn_tokens(t) for t in compressed_turns)
            old_count = len(self.messages)
            self.messages = new_messages

            logger.info(
                f"📦 上下文tokens超限(轮次<{COMPRESS_THRESHOLD}): "
                f"~{current_tokens + system_tokens} > {max_tokens}，"
                f"压缩全部 {len(turns)} 轮为纯文本 "
                f"({old_count} -> {len(self.messages)} 条消息，"
                f"~{current_tokens + system_tokens} -> ~{new_tokens + system_tokens} tokens)"
            )
            return

        # --- Many turns (>=5): discard the older half, keep the newer half ---
        removed_count = len(turns) // 2
        keep_count = len(turns) - removed_count
        kept_turns = turns[-keep_count:]
        kept_tokens = sum(self._estimate_turn_tokens(t) for t in kept_turns)

        logger.info(
            f"🔄 上下文tokens超限: ~{current_tokens + system_tokens} > {max_tokens}，"
            f"裁剪至 {keep_count} 轮（移除 {removed_count} 轮）"
        )

        if self.agent.memory_manager:
            discarded_messages = []
            for turn in turns[:removed_count]:
                discarded_messages.extend(turn["messages"])
            if discarded_messages:
                user_id = getattr(self.agent, '_current_user_id', None)
                self.agent.memory_manager.flush_memory(
                    messages=discarded_messages, user_id=user_id,
                    reason="trim", max_messages=0
                )

        new_messages = []
        for turn in kept_turns:
            new_messages.extend(turn['messages'])

        old_count = len(self.messages)
        self.messages = new_messages

        logger.info(
            f"   移除了 {removed_count} 轮对话 "
            f"({old_count} -> {len(self.messages)} 条消息，"
            f"~{current_tokens + system_tokens} -> ~{kept_tokens + system_tokens} tokens)"
        )

    def _clear_session_db(self):
        """
        从SQLite DB中清除当前会话的持久化消息。

        这可以防止脏数据（损坏的tool_use/tool_result对）在下次请求或重启后被重新加载。
        """
        try:
            session_id = getattr(self.agent, '_current_session_id', None)
            if not session_id:
                return
            from agent.memory import get_conversation_store
            store = get_conversation_store()
            store.clear_session(session_id)
            logger.info(f"🗑️ Cleared dirty session data from DB: {session_id}")
        except Exception as e:
            logger.warning(f"Failed to clear session DB: {e}")

    def _prepare_messages(self) -> List[Dict[str, Any]]:
        """
        准备要发送给LLM的消息
        
        注意：对于Claude API，系统提示应该通过system参数单独传递，
        而不是作为消息的一部分。AgentLLMModel将处理这个问题。
        """
        # Don't add system message here - it will be handled separately by the LLM adapter / 不要在这里添加系统消息 - 它将由LLM适配器单独处理
        return self.messages