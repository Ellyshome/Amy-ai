"""
单例装饰器，确保被装饰的类全局只创建一个实例。
通过闭包字典缓存实例，首次调用时构造，后续直接返回，适用于Bridge/Config等需全局唯一的对象。
"""
def singleton(cls):
    instances = {}

    def get_instance(*args, **kwargs):
        if cls not in instances:
            instances[cls] = cls(*args, **kwargs)
        return instances[cls]

    return get_instance
