"""
微信公众号API客户端模块 —— 封装微信公众号API调用，支持access_token安全刷新和API限流处理。

本模块实现了WechatMPClient类，继承自wechatpy的WeChatClient，
在父类基础上添加了两个关键功能：

1. 线程安全的access_token获取：
   - 使用互斥锁保证同一时刻只有一个线程执行token刷新
   - 避免多线程并发刷新导致的重复API调用和token冲突

2. API限流自动恢复：
   - 捕获APILimitedException异常（API配额用尽）
   - 自动调用clear_quota_v2清除配额
   - 使用双重检查锁定避免重复清除
   - 60秒内最多清除一次配额，防止频繁清除被微信封禁

微信公众号API的限流机制：
- 每个公众号每月有固定的API调用配额
- 配额用尽后需要手动清除（调用clear_quota接口）
- 清除配额有频率限制，过于频繁可能被封禁
"""
import threading
import time

from wechatpy.client import WeChatClient
from wechatpy.exceptions import APILimitedException

from channel.wechatmp.common import *
from common.log import logger


class WechatMPClient(WeChatClient):
    """
    微信公众号API客户端，支持线程安全和API限流自动恢复。

    继承自wechatpy的WeChatClient，重写了fetch_access_token和_request方法：

    - fetch_access_token: 添加线程锁，保证多线程环境下不会重复获取token
    - _request: 捕获API限流异常，自动清除配额后重试

    线程安全设计：
    - fetch_access_token_lock: 互斥锁，保护token获取操作
    - clear_quota_lock: 互斥锁，保护配额清除操作
    - last_clear_quota_time: 上次清除配额的时间戳，用于限频

    配额清除策略：
    - 遇到APILimitedException时自动清除配额
    - 60秒内最多清除一次，防止频繁操作
    - 使用双重检查锁定减少锁竞争
    - 优先使用clear_quota_v2接口（更安全，不需要access_token）
    """

    def __init__(self, appid, secret, access_token=None, session=None, timeout=None, auto_retry=True):
        """
        初始化微信公众号API客户端。

        调用父类构造函数后，创建线程锁和配额清除时间记录。

        Args:
            appid: 公众号AppID
            secret: 公众号AppSecret
            access_token: 可选的初始access_token
            session: 可选的session对象，用于持久化access_token
            timeout: API请求超时时间
            auto_retry: API请求失败时是否自动重试
        """
        super(WechatMPClient, self).__init__(appid, secret, access_token, session, timeout, auto_retry)
        # access_token获取的互斥锁，保证线程安全
        self.fetch_access_token_lock = threading.Lock()
        # 配额清除的互斥锁，防止多线程同时清除配额
        self.clear_quota_lock = threading.Lock()
        # 上次清除配额的时间戳，初始化为-1表示尚未清除过
        # 用于实现60秒内最多清除一次的限频策略
        self.last_clear_quota_time = -1

    def clear_quota(self):
        """
        清除微信公众号API调用配额（旧版接口）。

        调用微信API的clear_quota接口，需要有效的access_token。

        Returns:
            API响应数据
        """
        return self.post("clear_quota", data={"appid": self.appid})

    def clear_quota_v2(self):
        """
        清除微信公众号API调用配额（V2版接口，推荐使用）。

        调用微信API的clear_quota/v2接口，使用appid和appsecret直接认证，
        不依赖access_token，避免因token问题导致清除失败。

        Returns:
            API响应数据
        """
        return self.post("clear_quota/v2", params={"appid": self.appid, "appsecret": self.secret})

    def fetch_access_token(self):  # 重载父类方法，加锁避免多线程重复获取access_token
        """
        获取有效的access_token，线程安全。

        重写父类方法，添加线程锁保证在多线程环境下不会重复获取。
        获取锁后先检查缓存的token是否仍然有效（距过期超过60秒），
        有效则直接返回，避免不必要的API调用。

        60秒的缓冲时间可以确保：
        1. 获取token后至少有60秒的使用时间
        2. 不会因为token即将过期而导致API调用失败

        Returns:
            有效的access_token字符串
        """
        with self.fetch_access_token_lock:
            access_token = self.session.get(self.access_token_key)
            if access_token:
                if not self.expires_at:
                    return access_token
                timestamp = time.time()
                # 如果token距过期超过60秒，仍然有效
                if self.expires_at - timestamp > 60:
                    return access_token
            # token不存在或已过期，调用父类方法获取新token
            return super().fetch_access_token()

    def _request(self, method, url_or_endpoint, **kwargs):  # 重载父类方法，遇到API限流时，清除quota后重试
        """
        发送API请求，支持API限流自动恢复。

        重写父类方法，在API请求遇到限流异常（APILimitedException）时，
        自动清除配额后重试请求。使用双重检查锁定和60秒限频策略
        防止过多清除操作。

        处理流程：
        1. 尝试发送API请求
        2. 如果遇到APILimitedException：
           a. 检查距上次清除是否超过60秒
           b. 如果超过60秒，获取锁后再次检查（双重检查）
           c. 确认需要清除后调用clear_quota_v2
           d. 重试原始请求
        3. 如果60秒内已清除过，直接抛出异常（避免频繁清除被微信封禁）

        Args:
            method: HTTP方法（GET/POST等）
            url_or_endpoint: API端点URL
            **kwargs: 其他请求参数

        Returns:
            API响应数据

        Raises:
            APILimitedException: API限流且无法清除配额时抛出
        """
        try:
            return super()._request(method, url_or_endpoint, **kwargs)
        except APILimitedException as e:
            # API配额用尽，尝试自动清除
            logger.error("[wechatmp] API quata has been used up. {}".format(e))
            # 检查距上次清除是否超过60秒（限频策略）
            if self.last_clear_quota_time == -1 or time.time() - self.last_clear_quota_time > 60:
                with self.clear_quota_lock:
                    # 双重检查：获取锁后再次验证时间条件
                    # 因为在等待锁的过程中，其他线程可能已经完成了清除
                    if self.last_clear_quota_time == -1 or time.time() - self.last_clear_quota_time > 60:
                        self.last_clear_quota_time = time.time()
                        # 使用v2版接口清除配额（更安全）
                        response = self.clear_quota_v2()
                        logger.debug("[wechatmp] API quata has been cleard, {}".format(response))
                # 清除配额后重试原始请求
                return super()._request(method, url_or_endpoint, **kwargs)
            else:
                # 60秒内已清除过配额，不再重复清除
                # 频繁清除配额可能导致被微信封禁
                logger.error("[wechatmp] last clear quota time is {}, less than 60s, skip clear quota")
                raise e
