# encoding:utf-8
"""
应用入口，负责配置加载、信号注册与频道管理器生命周期控制。
ChannelManager 统一调度多个消息频道的并发启停与动态增删，实现多通道并行运行。
核心类: ChannelManager; 核心函数: run(), sigterm_handler_wrap()
"""

import os
import signal
import sys
import time

from channel import channel_factory
from common import const
from common.log import logger
from config import load_config, conf
from plugins import *
import threading

# 全局频道管理器实例
_channel_mgr = None


def get_channel_manager():
    """获取全局频道管理器实例"""
    return _channel_mgr


def _parse_channel_type(raw) -> list:
    """
    将 channel_type 配置值解析为频道名称列表。
    支持以下格式：
      - 单个字符串: "feishu"
      - 逗号分隔字符串: "feishu, dingtalk"
      - 列表: ["feishu", "dingtalk"]
    """
    if isinstance(raw, list):
        return [ch.strip() for ch in raw if ch.strip()]
    if isinstance(raw, str):
        return [ch.strip() for ch in raw.split(",") if ch.strip()]
    return []


class ChannelManager:
    """
    频道管理器，管理多个消息频道的生命周期，使它们并发运行。
    每个频道的 startup() 方法在独立的守护线程中执行。
    Web 频道默认作为控制台启动，除非显式禁用。
    """

    def __init__(self):
        self._channels = {}        # 频道名称 -> 频道实例
        self._threads = {}         # 频道名称 -> 守护线程
        self._primary_channel = None  # 主频道（首个非 web 频道）
        self._lock = threading.Lock()  # 线程安全锁
        self.cloud_mode = False    # 云端客户端激活时设为 True

    @property
    def channel(self):
        """返回主频道（首个非 web 频道），用于向后兼容"""
        return self._primary_channel

    def get_channel(self, channel_name: str):
        """根据频道名称获取频道实例"""
        return self._channels.get(channel_name)

    def start(self, channel_names: list, first_start: bool = False):
        """
        创建并在子线程中启动一个或多个频道。
        参数：
            channel_names: 要启动的频道名称列表
            first_start: 是否为首次启动，为 True 时会初始化插件和 LinkAI 客户端
        """
        with self._lock:
            channels = []
            for name in channel_names:
                ch = channel_factory.create_channel(name)
                ch.cloud_mode = self.cloud_mode
                self._channels[name] = ch
                channels.append((name, ch))
                if self._primary_channel is None and name != "web":
                    self._primary_channel = ch

            if self._primary_channel is None and channels:
                self._primary_channel = channels[0][1]

            if first_start:
                PluginManager().load_plugins()

                # 云端客户端为可选项。仅当 use_linkai=True 且
                # cloud_deployment_id 已设置时才会启动。
                # 默认两者均未配置，因此应用完全在本地运行，
                # 不会建立任何远程连接。
                if conf().get("use_linkai") and (
                    os.environ.get("CLOUD_DEPLOYMENT_ID") or conf().get("cloud_deployment_id")
                ):
                    try:
                        from common import cloud_client
                        threading.Thread(
                            target=cloud_client.start,
                            args=(self._primary_channel, self),
                            daemon=True,
                        ).start()
                    except Exception:
                        pass

            # 优先启动 Web 控制台，使其日志输出整洁，
            # 其余频道稍作间隔后依次启动。
            web_entry = None
            other_entries = []
            for entry in channels:
                if entry[0] == "web":
                    web_entry = entry
                else:
                    other_entries.append(entry)

            ordered = ([web_entry] if web_entry else []) + other_entries
            for i, (name, ch) in enumerate(ordered):
                if i > 0 and name != "web":
                    time.sleep(0.1)
                t = threading.Thread(target=self._run_channel, args=(name, ch), daemon=True)
                self._threads[name] = t
                t.start()
                logger.debug(f"[ChannelManager] Channel '{name}' started in sub-thread")

    def _run_channel(self, name: str, channel):
        """在当前线程中启动频道，捕获并记录启动异常"""
        try:
            channel.startup()
        except Exception as e:
            logger.error(f"[ChannelManager] Channel '{name}' startup error: {e}")
            logger.exception(e)

    def stop(self, channel_name: str = None):
        """
        停止频道。若指定 channel_name 则仅停止该频道，否则停止所有频道。
        先在锁内移除引用，再在锁外执行停止操作以避免死锁。
        """
        # 在锁内弹出引用，锁外停止以避免死锁
        with self._lock:
            names = [channel_name] if channel_name else list(self._channels.keys())
            to_stop = []
            for name in names:
                ch = self._channels.pop(name, None)
                th = self._threads.pop(name, None)
                to_stop.append((name, ch, th))
            if channel_name and self._primary_channel is self._channels.get(channel_name):
                self._primary_channel = None

        for name, ch, th in to_stop:
            if ch is None:
                logger.warning(f"[ChannelManager] Channel '{name}' not found in managed channels")
                if th and th.is_alive():
                    self._interrupt_thread(th, name)
                continue
            logger.info(f"[ChannelManager] Stopping channel '{name}'...")
            graceful = False
            if hasattr(ch, 'stop'):
                try:
                    ch.stop()
                    graceful = True
                except Exception as e:
                    logger.warning(f"[ChannelManager] Error during channel '{name}' stop: {e}")
            if th and th.is_alive():
                th.join(timeout=5)
                if th.is_alive():
                    if graceful:
                        logger.info(f"[ChannelManager] Channel '{name}' thread still alive after stop(), "
                                    "leaving daemon thread to finish on its own")
                    else:
                        logger.warning(f"[ChannelManager] Channel '{name}' thread did not exit in 5s, forcing interrupt")
                        self._interrupt_thread(th, name)

    @staticmethod
    def _interrupt_thread(th: threading.Thread, name: str):
        """向目标线程注入 SystemExit 异常，打断 start_forever 等阻塞循环"""
        import ctypes
        try:
            tid = th.ident
            if tid is None:
                return
            res = ctypes.pythonapi.PyThreadState_SetAsyncExc(
                ctypes.c_ulong(tid), ctypes.py_object(SystemExit)
            )
            if res == 1:
                logger.info(f"[ChannelManager] Interrupted thread for channel '{name}'")
            elif res > 1:
                ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_ulong(tid), None)
                logger.warning(f"[ChannelManager] Failed to interrupt thread for channel '{name}'")
        except Exception as e:
            logger.warning(f"[ChannelManager] Thread interrupt error for '{name}': {e}")

    def restart(self, new_channel_name: str):
        """
        以新的频道类型重启单个频道。
        可从任意线程调用（如 LinkAI 配置回调）。
        """
        logger.info(f"[ChannelManager] Restarting channel to '{new_channel_name}'...")
        self.stop(new_channel_name)
        _clear_singleton_cache(new_channel_name)
        time.sleep(1)
        self.start([new_channel_name], first_start=False)
        logger.info(f"[ChannelManager] Channel restarted to '{new_channel_name}' successfully")

    def add_channel(self, channel_name: str):
        """
        动态添加并启动新频道。
        若该频道已在运行，则改为重启。
        """
        with self._lock:
            if channel_name in self._channels:
                logger.info(f"[ChannelManager] Channel '{channel_name}' already exists, restarting")
        if self._channels.get(channel_name):
            self.restart(channel_name)
            return
        logger.info(f"[ChannelManager] Adding channel '{channel_name}'...")
        _clear_singleton_cache(channel_name)
        self.start([channel_name], first_start=False)
        logger.info(f"[ChannelManager] Channel '{channel_name}' added successfully")

    def remove_channel(self, channel_name: str):
        """
        动态停止并移除一个正在运行的频道。
        """
        with self._lock:
            if channel_name not in self._channels:
                logger.warning(f"[ChannelManager] Channel '{channel_name}' not found, nothing to remove")
                return
        logger.info(f"[ChannelManager] Removing channel '{channel_name}'...")
        self.stop(channel_name)
        logger.info(f"[ChannelManager] Channel '{channel_name}' removed successfully")


def _clear_singleton_cache(channel_name: str):
    """
    清除频道类的单例缓存，以便使用更新后的配置创建新实例。
    """
    # 频道名称到对应模块类路径的映射表
    cls_map = {
        "web": "channel.web.web_channel.WebChannel",
        "wechatmp": "channel.wechatmp.wechatmp_channel.WechatMPChannel",
        "wechatmp_service": "channel.wechatmp.wechatmp_channel.WechatMPChannel",
        "wechatcom_app": "channel.wechatcom.wechatcomapp_channel.WechatComAppChannel",
        const.FEISHU: "channel.feishu.feishu_channel.FeiShuChanel",
        const.DINGTALK: "channel.dingtalk.dingtalk_channel.DingTalkChanel",
        const.WECOM_BOT: "channel.wecom_bot.wecom_bot_channel.WecomBotChannel",
        const.QQ: "channel.qq.qq_channel.QQChannel",
        const.WEIXIN: "channel.weixin.weixin_channel.WeixinChannel",
        "wx": "channel.weixin.weixin_channel.WeixinChannel",
    }
    module_path = cls_map.get(channel_name)
    if not module_path:
        return  # 未找到对应的类路径，直接返回
    try:
        # 将模块路径拆分为模块名和类名
        parts = module_path.rsplit(".", 1)
        module_name, class_name = parts[0], parts[1]
        import importlib
        # 动态导入模块并获取类引用
        module = importlib.import_module(module_name)
        wrapper = getattr(module, class_name, None)
        # 遍历闭包变量，找到并清除单例缓存字典
        if wrapper and hasattr(wrapper, '__closure__') and wrapper.__closure__:
            for cell in wrapper.__closure__:
                try:
                    cell_contents = cell.cell_contents
                    if isinstance(cell_contents, dict):
                        cell_contents.clear()  # 清空单例缓存，使下次访问时重新创建实例
                        logger.debug(f"[ChannelManager] Cleared singleton cache for {class_name}")
                        break
                except ValueError:
                    pass  # cell_contents 可能未初始化，跳过
    except Exception as e:
        logger.warning(f"[ChannelManager] Failed to clear singleton cache: {e}")


def sigterm_handler_wrap(_signo):
    """注册信号处理函数，在收到终止信号时保存用户数据后优雅退出"""
    old_handler = signal.getsignal(_signo)

    def func(_signo, _stack_frame):
        """信号处理回调：保存用户数据后退出进程"""
        logger.info("signal {} received, exiting...".format(_signo))
        conf().save_user_datas()
        if callable(old_handler):  # check old_handler / 检查是否存在旧处理器
            return old_handler(_signo, _stack_frame)
        sys.exit(0)

    signal.signal(_signo, func)


def run():
    """
    应用主入口函数。
    加载配置、注册信号处理、解析频道类型，创建并启动频道管理器，
    然后进入主循环保持进程运行。
    """
    global _channel_mgr
    try:
        # 加载配置
        load_config()
        # 处理 Ctrl+C 信号
        sigterm_handler_wrap(signal.SIGINT)
        # 处理 kill 信号
        sigterm_handler_wrap(signal.SIGTERM)

        # 将 channel_type 配置解析为频道列表
        raw_channel = conf().get("channel_type", "web")

        if "--cmd" in sys.argv:
            channel_names = ["terminal"]  # 命令行模式，使用终端频道
        else:
            channel_names = _parse_channel_type(raw_channel)
            if not channel_names:
                channel_names = ["web"]  # 默认使用 Web 频道

        # 除非显式禁用，否则自动启动 Web 控制台
        web_console_enabled = conf().get("web_console", True)
        if web_console_enabled and "web" not in channel_names:
            channel_names.append("web")

        logger.info(f"[App] Starting channels: {channel_names}")

        _channel_mgr = ChannelManager()
        _channel_mgr.start(channel_names, first_start=True)  # 创建频道管理器并启动所有频道

        while True:
            time.sleep(1)  # 主循环，保持进程运行
    except Exception as e:
        logger.error("App startup failed!")  # 应用启动失败
        logger.exception(e)


if __name__ == "__main__":
    run()
