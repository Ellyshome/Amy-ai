"""
翻译服务工厂模块，负责根据配置创建具体的翻译器实例
将翻译器创建逻辑与业务代码解耦，便于扩展新的翻译提供商
核心函数：create_translator 按类型返回对应翻译器（当前支持baidu）
"""

def create_translator(voice_type):
    if voice_type == "baidu":
        from translate.baidu.baidu_translate import BaiduTranslator

        return BaiduTranslator()
    raise RuntimeError
