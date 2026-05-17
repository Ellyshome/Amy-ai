"""
企业微信自建应用API客户端模块 —— 封装企业微信API调用，支持access_token自动刷新。

本模块实现了WechatComAppClient类，继承自wechatpy的WeChatClient，
在父类基础上添加了线程安全的access_token获取机制和后台主动刷新功能。

企业微信的access_token有效期2小时，需要在过期前刷新。
wechatpy父类采用的是"用时刷新"策略（即调用API时发现过期才刷新），
但在多线程环境下可能导致多个线程同时刷新，造成不必要的API调用。
本模块通过以下方式解决：
1. 使用线程锁保证同一时刻只有一个线程执行刷新操作
2. 启动后台守护线程提前10分钟主动刷新，避免请求时才发现过期
"""
import threading
import time
from wechatpy.enterprise import WeChatClient


class WechatComAppClient(WeChatClient):
    """
    企业微信API客户端，支持线程安全的access_token管理和后台主动刷新。

    继承自wechatpy的WeChatClient，重写了fetch_access_token方法，
    添加线程锁保证在多线程环境下不会重复获取access_token。
    同时启动后台守护线程，在access_token过期前10分钟主动刷新，
    确保API调用时总能使用有效的access_token。

    线程安全设计：
    - fetch_access_token_lock: 互斥锁，保证同一时刻只有一个线程获取token
    - 双重检查模式：获取锁后再次检查是否仍需刷新，避免锁竞争导致的重复刷新
    - 后台线程使用双重检查，进一步减少不必要的刷新操作
    """

    def __init__(self, corp_id, secret, access_token=None, session=None, timeout=None, auto_retry=True):
        """
        初始化企业微信API客户端。

        调用父类构造函数后，创建access_token获取的线程锁，
        并启动后台主动刷新线程。

        Args:
            corp_id: 企业ID
            secret: 应用Secret
            access_token: 可选的初始access_token
            session: 可选的session对象，用于持久化access_token
            timeout: API请求超时时间
            auto_retry: API请求失败时是否自动重试
        """
        super(WechatComAppClient, self).__init__(corp_id, secret, access_token, session, timeout, auto_retry)
        # 线程锁，保证fetch_access_token在多线程环境下的安全性
        self.fetch_access_token_lock = threading.Lock()
        # 启动后台主动刷新线程
        self._active_refresh()

    def _active_refresh(self):
        """
        启动access_token后台主动刷新的守护线程。

        后台线程每隔60秒检查一次access_token的有效期，如果距离过期
        不足10分钟（600秒），则在获取锁后执行刷新操作。

        使用双重检查模式（Double-Check Locking）：
        1. 先不加锁检查是否需要刷新（快速路径）
        2. 需要刷新时获取锁，再次检查是否仍需刷新
        3. 确认需要刷新后才执行刷新操作

        这种设计可以减少锁竞争，提高性能。
        """
        """启动主动刷新的后台线程"""
        def refresh_loop():
            """
            后台刷新循环，定期检查并刷新access_token。

            每次循环间隔60秒，先快速判断是否接近过期，
            如果是则加锁执行双重检查后刷新。
            """
            while True:
                now = time.time()
                expires_at = self.session.get(f"{self.corp_id}_expires_at", 0)

                # 提前10分钟刷新(600秒)
                # 企业微信access_token有效期2小时，提前10分钟刷新可以避免
                # 在高并发场景下因token过期导致请求失败
                if expires_at - now < 600:
                    with self.fetch_access_token_lock:
                        # 双重检查避免重复刷新
                        # 获取锁后再次检查，因为可能其他线程已经完成了刷新
                        if self.session.get(f"{self.corp_id}_expires_at", 0) - time.time() < 600:
                            super(WechatComAppClient, self).fetch_access_token()
                # 每次检查间隔60秒
                # 间隔太短会浪费CPU资源，太长可能导致刷新不及时
                time.sleep(60)

        # 启动守护线程
        # 设置daemon=True确保主线程退出时后台线程也会终止
        refresh_thread = threading.Thread(
            target=refresh_loop,
            daemon=True,
            name="wechatcom_token_refresh_thread"
        )
        refresh_thread.start()

    def fetch_access_token(self):
        """
        获取有效的access_token，线程安全。

        重写父类方法，添加线程锁保证在多线程环境下不会重复获取。
        如果当前access_token仍然有效（距过期超过60秒），直接返回缓存的token；
        否则调用父类方法获取新的access_token。

        线程安全机制：
        1. 获取互斥锁，保证同一时刻只有一个线程执行获取操作
        2. 先检查缓存中是否有有效token，避免不必要的API调用
        3. 缓存失效时才调用父类方法获取新token

        Returns:
            有效的access_token字符串
        """
        with self.fetch_access_token_lock:
            access_token = self.session.get(self.access_token_key)
            expires_at = self.session.get(f"{self.corp_id}_expires_at", 0)

            # 如果token存在且距过期超过60秒，直接返回缓存的token
            # 60秒的缓冲时间可以避免在请求过程中token过期
            if access_token and expires_at > time.time() + 60:
                return access_token
            # 缓存失效，调用父类方法获取新token
            return super().fetch_access_token()
