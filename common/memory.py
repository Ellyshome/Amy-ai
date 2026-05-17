"""
进程内共享内存缓存，存放跨模块需临时共享的运行时数据。
目前定义USER_IMAGE_CACHE(图片缓存，TTL 3分钟)，供Agent视觉工具读取用户发送的图片。
缓存均基于ExpiredDict实现自动过期，避免内存泄漏。
"""
from common.expired_dict import ExpiredDict

USER_IMAGE_CACHE = ExpiredDict(60 * 3)