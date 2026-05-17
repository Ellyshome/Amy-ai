"""
插件系统入口，统一导出事件、插件基类与插件管理器
通过模块级单例暴露register等快捷API，简化插件注册流程
"""

from .event import *
from .plugin import *
from .plugin_manager import PluginManager

instance = PluginManager()

register = instance.register
# load_plugins                = instance.load_plugins
# emit_event                  = instance.emit_event
