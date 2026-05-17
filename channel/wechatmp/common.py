"""
微信公众号公共工具模块 —— 提供消息验证和异常定义等公共功能。

本模块包含微信公众号渠道所需的公共组件：
- verify_server: 微信公众号URL验证函数，用于验证回调URL和消息签名
- WeChatAPIException: 微信API异常类
- MAX_UTF8_LEN: 微信公众号消息的最大UTF-8字节长度常量

微信公众号的回调URL验证机制：
当配置微信公众号的服务器URL时，微信服务器会发送GET请求进行验证，
需要使用Token校验签名，验证通过后URL才生效。
后续的消息推送也会携带签名参数，需要验证签名确保消息来自微信服务器。
"""
import web
from wechatpy.crypto import WeChatCrypto
from wechatpy.exceptions import InvalidSignatureException
from wechatpy.utils import check_signature

from config import conf

# 微信公众号单条消息的最大UTF-8字节数限制
# 微信公众号被动回复接口对消息内容有长度限制，超过此长度需要分条发送
MAX_UTF8_LEN = 2048


class WeChatAPIException(Exception):
    """
    微信API异常类。

    用于封装微信API调用过程中的异常情况，
    如API限流、参数错误等。继承自Python内置Exception类，
    目前为简单封装，可根据需要扩展更多异常信息。
    """
    pass


def verify_server(data):
    """
    验证微信公众号的服务器签名。

    微信公众号配置回调URL时，微信服务器会发送GET请求进行验证，
    请求中携带signature、timestamp、nonce和echostr参数。
    通过Token、timestamp、nonce三个参数按照微信的签名算法
    计算签名，与signature参数比对，验证请求是否来自微信服务器。

    验证通过后返回echostr参数值，微信服务器据此确认URL有效。
    后续消息推送时也会携带签名参数，同样需要验证签名。

    Args:
        data: web.input()获取的请求参数对象，包含signature、timestamp、nonce、echostr

    Returns:
        echostr参数值（URL验证时）或None（消息验证时）

    Raises:
        web.Forbidden: 签名验证失败时返回HTTP 403错误
    """
    try:
        signature = data.signature
        timestamp = data.timestamp
        nonce = data.nonce
        echostr = data.get("echostr", None)
        token = conf().get("wechatmp_token")  # 请按照公众平台官网\基本配置中信息填写
        # 使用wechatpy的check_signature验证签名
        # 签名算法：将token、timestamp、nonce三个参数排序后拼接，计算SHA1哈希
        check_signature(token, signature, timestamp, nonce)
        return echostr
    except InvalidSignatureException:
        # 签名验证失败，可能是非法请求或参数错误
        raise web.Forbidden("Invalid signature")
    except Exception as e:
        # 其他异常（如缺少必要参数）
        raise web.Forbidden(str(e))
