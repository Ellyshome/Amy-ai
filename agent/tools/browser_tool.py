"""
浏览器工具的实例拷贝方法，支持跨Agent共享浏览器实例。
通过浅拷贝浏览器对象避免重复创建昂贵的浏览器会话，
同时保持每个Agent拥有独立的model与上下文引用。
核心方法：copy — 共享浏览器实例的拷贝策略
"""

def copy(self):
    """
    Special copy method for browser tool to avoid recreating browser instance.
    
    :return: A new instance with shared browser reference but unique model
    """
    new_tool = self.__class__()
    
    # Copy essential attributes
    new_tool.model = self.model
    new_tool.context = getattr(self, 'context', None)
    new_tool.config = getattr(self, 'config', None)
    
    # Share the browser instance instead of creating a new one
    if hasattr(self, 'browser'):
        new_tool.browser = self.browser
    
    return new_tool 