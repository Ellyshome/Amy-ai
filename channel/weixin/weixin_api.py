"""
微信HTTP JSON API客户端。

实现了微信ilink机器人协议，提供以下功能：
  - getUpdates (长轮询获取新消息)
  - sendMessage (发送文本/图片/文件/视频消息)
  - getUploadUrl (获取CDN上传地址)
  - getConfig (获取配置)
  - sendTyping (发送输入中状态)
  - QR login (二维码登录：get_bot_qrcode / get_qrcode_status)

CDN媒体上传使用AES-128-ECB加密，确保传输安全。
"""

import base64
import hashlib
import os
import random
import struct
import time
import uuid

import requests

from common.log import logger

# 微信ilink机器人API的默认基础URL
DEFAULT_BASE_URL = "https://ilinkai.weixin.qq.com"
# CDN（内容分发网络）基础URL，用于媒体文件上传和下载
CDN_BASE_URL = "https://novac2c.cdn.weixin.qq.com/c2c"
# getUpdates长轮询的默认超时时间（秒）
DEFAULT_LONG_POLL_TIMEOUT = 35
# 普通API调用的默认超时时间（秒）
DEFAULT_API_TIMEOUT = 15
# 二维码状态轮询超时时间（秒）
QR_POLL_TIMEOUT = 35
# 机器人类型标识，3表示ilink机器人
BOT_TYPE = "3"


def _random_wechat_uin() -> str:
    """生成随机的微信UIN标识。

    用于请求头中的X-WECHAT-UIN字段，
    模拟微信客户端的身份标识。
    生成一个随机整数后进行base64编码。

    Returns:
        base64编码的随机UIN字符串
    """
    val = random.randint(0, 0xFFFFFFFF)
    return base64.b64encode(str(val).encode("utf-8")).decode("utf-8")


def _build_headers(token: str = "") -> dict:
    """构建API请求的公共请求头。

    包含以下必要字段：
    - Content-Type: 指定为JSON格式
    - AuthorizationType: 使用ilink_bot_token认证方式
    - X-WECHAT-UIN: 随机生成的微信UIN
    - Authorization: 如果提供了token，则添加Bearer认证头

    Args:
        token: 认证令牌，为空时不添加Authorization头

    Returns:
        请求头字典
    """
    headers = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "X-WECHAT-UIN": _random_wechat_uin(),
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _ensure_trailing_slash(url: str) -> str:
    """确保URL以斜杠结尾，用于拼接endpoint路径。

    Args:
        url: 原始URL

    Returns:
        以斜杠结尾的URL
    """
    return url if url.endswith("/") else url + "/"


class WeixinApi:
    """微信ilink机器人API的无状态HTTP客户端。

    封装了与微信ilink机器人服务端的所有HTTP交互，
    包括消息收发、媒体上传、二维码登录等功能。
    该类是无状态的，所有认证信息通过token参数传递。
    """

    def __init__(self, base_url: str = DEFAULT_BASE_URL, token: str = "",
                 cdn_base_url: str = CDN_BASE_URL):
        """初始化微信API客户端。

        Args:
            base_url: API服务基础URL
            token: Bearer认证令牌，登录后获取
            cdn_base_url: CDN服务基础URL，用于媒体文件上传下载
        """
        self.base_url = base_url
        self.token = token
        self.cdn_base_url = cdn_base_url

    def _post(self, endpoint: str, body: dict, timeout: int = DEFAULT_API_TIMEOUT) -> dict:
        """发送POST请求到指定API端点。

        统一的HTTP请求方法，处理认证、超时和错误：
        - 超时时返回空响应（getUpdates长轮询场景下超时是正常的）
        - 其他异常向上抛出由调用方处理

        Args:
            endpoint: API端点路径（相对于base_url）
            body: 请求体字典
            timeout: 请求超时时间（秒）

        Returns:
            API响应的JSON字典，超时时返回 {"ret": 0, "msgs": []}

        Raises:
            Exception: 非超时的HTTP错误
        """
        url = _ensure_trailing_slash(self.base_url) + endpoint
        headers = _build_headers(self.token)
        try:
            resp = requests.post(url, json=body, headers=headers, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.Timeout:
            # 超时不视为错误，返回空响应以保持长轮询的连续性
            logger.debug(f"[Weixin] API timeout: {endpoint}")
            return {"ret": 0, "msgs": []}
        except Exception as e:
            logger.error(f"[Weixin] API error {endpoint}: {e}")
            raise

    # ── getUpdates (long-poll) ─────────────────────────────────────────

    def get_updates(self, get_updates_buf: str = "", timeout: int = DEFAULT_LONG_POLL_TIMEOUT) -> dict:
        """长轮询获取新消息。

        使用HTTP长轮询机制接收新消息：
        - 客户端发送请求后，服务端在有新消息或超时时才返回
        - get_updates_buf是同步游标，确保消息不会丢失或重复
        - 每次返回后，使用新的get_updates_buf继续下一次轮询

        Args:
            get_updates_buf: 上次返回的同步游标，为空表示从头开始
            timeout: 长轮询超时时间（秒），实际请求超时会多5秒

        Returns:
            包含新消息列表和同步游标的响应字典
        """
        return self._post("ilink/bot/getupdates", {
            "get_updates_buf": get_updates_buf,
        }, timeout=timeout + 5)

    # ── sendMessage ────────────────────────────────────────────────────

    def send_text(self, to: str, text: str, context_token: str) -> dict:
        """发送文本消息。

        Args:
            to: 接收者的用户ID
            text: 文本内容
            context_token: 上下文令牌，关联到原始消息的会话

        Returns:
            API响应字典
        """
        return self._post("ilink/bot/sendmessage", {
            "msg": {
                "from_user_id": "",
                "to_user_id": to,
                "client_id": uuid.uuid4().hex[:16],  # 客户端生成的消息唯一标识
                "message_type": 2,  # BOT: 表示这是机器人发送的消息
                "message_state": 2,  # FINISH: 表示消息已完整生成
                "item_list": [{"type": 1, "text_item": {"text": text}}],  # type=1表示文本项
                "context_token": context_token,
            }
        })

    def send_image_item(self, to: str, context_token: str,
                        encrypt_query_param: str, aes_key_b64: str,
                        ciphertext_size: int, text: str = "") -> dict:
        """发送图片消息。

        图片需要先通过CDN上传，然后在消息中引用上传结果。
        支持同时发送文本和图片。

        Args:
            to: 接收者的用户ID
            context_token: 上下文令牌
            encrypt_query_param: CDN上传后返回的加密查询参数（用于下载定位）
            aes_key_b64: AES密钥的base64编码（用于接收方解密）
            ciphertext_size: 加密后的文件大小（用于前端显示）
            text: 附加的文本内容，可选

        Returns:
            API响应字典
        """
        items = []
        # 可选的文本项，附加在图片之前
        if text:
            items.append({"type": 1, "text_item": {"text": text}})
        # 图片项：包含CDN加密参数和密钥
        items.append({
            "type": 2,  # type=2表示图片项
            "image_item": {
                "media": {
                    "encrypt_query_param": encrypt_query_param,
                    "aes_key": aes_key_b64,
                    "encrypt_type": 1,  # 加密类型：1=AES-128-ECB
                },
                "mid_size": ciphertext_size,
            }
        })
        return self._send_items(to, context_token, items)

    def send_file_item(self, to: str, context_token: str,
                       encrypt_query_param: str, aes_key_b64: str,
                       file_name: str, file_size: int, text: str = "") -> dict:
        """发送文件消息。

        文件需要先通过CDN上传，然后在消息中引用上传结果。
        支持同时发送文本和文件。

        Args:
            to: 接收者的用户ID
            context_token: 上下文令牌
            encrypt_query_param: CDN上传后返回的加密查询参数
            aes_key_b64: AES密钥的base64编码
            file_name: 文件名（含扩展名）
            file_size: 原始文件大小
            text: 附加的文本内容，可选

        Returns:
            API响应字典
        """
        items = []
        if text:
            items.append({"type": 1, "text_item": {"text": text}})
        items.append({
            "type": 4,  # type=4表示文件项
            "file_item": {
                "media": {
                    "encrypt_query_param": encrypt_query_param,
                    "aes_key": aes_key_b64,
                    "encrypt_type": 1,
                },
                "file_name": file_name,
                "len": str(file_size),
            }
        })
        return self._send_items(to, context_token, items)

    def send_video_item(self, to: str, context_token: str,
                        encrypt_query_param: str, aes_key_b64: str,
                        ciphertext_size: int, text: str = "") -> dict:
        """发送视频消息。

        视频需要先通过CDN上传，然后在消息中引用上传结果。
        支持同时发送文本和视频。

        Args:
            to: 接收者的用户ID
            context_token: 上下文令牌
            encrypt_query_param: CDN上传后返回的加密查询参数
            aes_key_b64: AES密钥的base64编码
            ciphertext_size: 加密后的文件大小
            text: 附加的文本内容，可选

        Returns:
            API响应字典
        """
        items = []
        if text:
            items.append({"type": 1, "text_item": {"text": text}})
        items.append({
            "type": 5,  # type=5表示视频项
            "video_item": {
                "media": {
                    "encrypt_query_param": encrypt_query_param,
                    "aes_key": aes_key_b64,
                    "encrypt_type": 1,
                },
                "video_size": ciphertext_size,
            }
        })
        return self._send_items(to, context_token, items)

    def _send_items(self, to: str, context_token: str, items: list) -> dict:
        """发送包含多个消息项的消息。

        这是所有send_*_item方法的底层实现，
        将消息项列表组装为完整的消息格式后发送。

        Args:
            to: 接收者的用户ID
            context_token: 上下文令牌
            items: 消息项列表，每项包含type和对应的内容

        Returns:
            API响应字典
        """
        return self._post("ilink/bot/sendmessage", {
            "msg": {
                "from_user_id": "",
                "to_user_id": to,
                "client_id": uuid.uuid4().hex[:16],
                "message_type": 2,
                "message_state": 2,
                "item_list": items,
                "context_token": context_token,
            }
        })

    # ── getUploadUrl ───────────────────────────────────────────────────

    def get_upload_url(self, filekey: str, media_type: int, to_user_id: str,
                       rawsize: int, rawfilemd5: str, filesize: int,
                       aeskey: str) -> dict:
        """获取CDN上传地址和参数。

        在上传媒体文件到CDN之前，需要先调用此接口获取上传凭证。
        服务端会返回upload_param，用于构造CDN上传URL。

        Args:
            filekey: 文件唯一标识（UUID格式）
            media_type: 媒体类型（1=图片, 2=视频, 3=文件）
            to_user_id: 目标用户ID
            rawsize: 原始文件大小（字节）
            rawfilemd5: 原始文件的MD5值（用于完整性校验）
            filesize: 加密后的文件大小（字节，PKCS7填充后的大小）
            aeskey: AES加密密钥的十六进制字符串

        Returns:
            包含upload_param等上传参数的响应字典
        """
        return self._post("ilink/bot/getuploadurl", {
            "filekey": filekey,
            "media_type": media_type,
            "to_user_id": to_user_id,
            "rawsize": rawsize,
            "rawfilemd5": rawfilemd5,
            "filesize": filesize,
            "aeskey": aeskey,
            "no_need_thumb": True,  # 不需要服务端生成缩略图
        })

    # ── getConfig / sendTyping ─────────────────────────────────────────

    def get_config(self, user_id: str, context_token: str = "") -> dict:
        """获取用户配置信息。

        Args:
            user_id: 用户ID
            context_token: 上下文令牌，可选

        Returns:
            包含用户配置的响应字典
        """
        return self._post("ilink/bot/getconfig", {
            "ilink_user_id": user_id,
            "context_token": context_token,
        }, timeout=10)

    def send_typing(self, user_id: str, typing_ticket: str, status: int = 1) -> dict:
        """发送"正在输入"状态指示。

        在处理用户消息时，发送此状态让用户看到"对方正在输入..."的提示，
        提升交互体验。

        Args:
            user_id: 用户ID
            typing_ticket: 输入状态的票据（从消息中获取）
            status: 状态值，1=开始输入，0=停止输入

        Returns:
            API响应字典
        """
        return self._post("ilink/bot/sendtyping", {
            "ilink_user_id": user_id,
            "typing_ticket": typing_ticket,
            "status": status,
        }, timeout=10)

    # ── QR Login ───────────────────────────────────────────────────────

    def fetch_qr_code(self) -> dict:
        """获取登录二维码。

        请求服务端生成一个新的二维码，用户使用微信扫码后可完成登录。
        二维码有效期约2分钟，过期需要重新获取。

        Returns:
            包含qrcode（二维码标识）和qrcode_img_content（二维码URL）的响应字典
        """
        url = _ensure_trailing_slash(self.base_url) + f"ilink/bot/get_bot_qrcode?bot_type={BOT_TYPE}"
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        return resp.json()

    def poll_qr_status(self, qrcode: str, timeout: int = QR_POLL_TIMEOUT) -> dict:
        """轮询二维码登录状态。

        获取二维码后，定期调用此接口检查扫码状态。
        可能的状态值：
        - "wait": 等待扫码
        - "scaned": 已扫码，等待手机确认
        - "expired": 二维码已过期
        - "confirmed": 已确认登录，返回bot_token等信息

        Args:
            qrcode: 二维码标识（从fetch_qr_code获取）
            timeout: 轮询超时时间（秒）

        Returns:
            包含登录状态的响应字典，超时时返回 {"status": "wait"}
        """
        url = (_ensure_trailing_slash(self.base_url) +
               f"ilink/bot/get_qrcode_status?qrcode={requests.utils.quote(qrcode)}")
        headers = {"iLink-App-ClientVersion": "1"}
        try:
            resp = requests.get(url, headers=headers, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.Timeout:
            # 超时视为仍在等待扫码
            return {"status": "wait"}


# ── AES-128-ECB helpers ─────────────────────────────────────────────

def _aes_ecb_encrypt(data: bytes, key: bytes) -> bytes:
    """使用AES-128-ECB模式加密数据。

    微信CDN上传协议要求使用AES-128-ECB加密媒体文件。
    ECB模式不使用IV（初始向量），每个16字节块独立加密。
    使用PKCS7填充将数据对齐到16字节的块大小。

    Args:
        data: 待加密的原始数据
        key: 16字节（128位）的AES密钥

    Returns:
        加密后的字节数据
    """
    from Crypto.Cipher import AES
    # PKCS7填充：将数据填充到16字节的整数倍
    pad_len = 16 - (len(data) % 16)
    padded = data + bytes([pad_len] * pad_len)
    cipher = AES.new(key, AES.MODE_ECB)
    return cipher.encrypt(padded)


def _aes_ecb_decrypt(data: bytes, key: bytes) -> bytes:
    """使用AES-128-ECB模式解密数据。

    解密微信CDN下载的媒体文件。
    解密后去除PKCS7填充恢复原始数据。

    Args:
        data: 加密的字节数据
        key: 16字节（128位）的AES密钥

    Returns:
        解密后的原始字节数据
    """
    from Crypto.Cipher import AES
    cipher = AES.new(key, AES.MODE_ECB)
    decrypted = cipher.decrypt(data)
    # 去除PKCS7填充
    pad_len = decrypted[-1]
    if pad_len > 16:
        # 填充长度超过块大小，说明数据可能未加密或已损坏
        return decrypted
    return decrypted[:-pad_len]


def _file_md5(file_path: str) -> str:
    """计算文件的MD5哈希值。

    用于上传文件时进行完整性校验，
    服务端会验证文件MD5以确保传输正确。

    Args:
        file_path: 文件路径

    Returns:
        MD5哈希的十六进制字符串
    """
    h = hashlib.md5()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _md5_bytes(data: bytes) -> str:
    """计算字节数据的MD5哈希值。

    Args:
        data: 字节数据

    Returns:
        MD5哈希的十六进制字符串
    """
    return hashlib.md5(data).hexdigest()


def _aes_ecb_padded_size(plaintext_size: int) -> int:
    """计算AES-128-ECB加密并PKCS7填充后的数据大小。

    在上传文件前需要告知服务端加密后的文件大小，
    此函数根据原始数据大小计算填充后的字节数。
    PKCS7填充规则：至少填充1字节，最多填充16字节，
    使总长度为16的整数倍。

    Args:
        plaintext_size: 原始数据的字节大小

    Returns:
        PKCS7填充后的字节大小（16的整数倍）
    """
    return ((plaintext_size + 1 + 15) // 16) * 16


# CDN上传最大重试次数
UPLOAD_MAX_RETRIES = 3


def upload_media_to_cdn(api: WeixinApi, file_path: str, to_user_id: str,
                        media_type: int) -> dict:
    """将本地文件上传到微信CDN（遵循ilink机器人协议）。

    完整的上传流程：
    1. 生成随机的AES-128密钥和文件唯一标识
    2. 使用AES-128-ECB加密文件内容
    3. 调用getUploadUrl获取上传凭证（upload_param）
    4. 将加密后的数据POST到CDN
    5. 从CDN响应头中获取下载参数（x-encrypted-param）

    CDN上传可能因网络问题失败，支持最多3次重试。
    客户端错误（4xx）不重试，因为重试也不会成功。

    Args:
        api: WeixinApi实例，用于调用getUploadUrl
        file_path: 本地文件路径
        to_user_id: 目标用户ID
        media_type: 媒体类型，1=图片, 2=视频, 3=文件

    Returns:
        包含以下键的字典：
        - encrypt_query_param: 下载时使用的加密查询参数
        - aes_key_b64: AES密钥的base64编码（发送给接收方用于解密）
        - ciphertext_size: 加密后的数据大小
        - raw_size: 原始数据大小

    Raises:
        RuntimeError: getUploadUrl失败或CDN上传失败时抛出
    """
    # 生成16字节随机AES密钥
    aes_key = os.urandom(16)
    aes_key_hex = aes_key.hex()
    # 生成文件唯一标识
    filekey = uuid.uuid4().hex

    # 读取原始文件数据
    with open(file_path, "rb") as f:
        raw_data = f.read()

    raw_size = len(raw_data)
    # 计算原始文件的MD5值
    raw_md5 = _md5_bytes(raw_data)
    # 计算加密后的文件大小（含PKCS7填充）
    cipher_size = _aes_ecb_padded_size(raw_size)

    # 步骤1：获取上传凭证
    resp = api.get_upload_url(
        filekey=filekey,
        media_type=media_type,
        to_user_id=to_user_id,
        rawsize=raw_size,
        rawfilemd5=raw_md5,
        filesize=cipher_size,
        aeskey=aes_key_hex,
    )

    upload_param = resp.get("upload_param", "")
    if not upload_param:
        raise RuntimeError(f"[Weixin] getUploadUrl returned no upload_param: {resp}")

    # 步骤2：使用AES-128-ECB加密文件内容
    encrypted = _aes_ecb_encrypt(raw_data, aes_key)

    # 步骤3：构造CDN上传URL
    from urllib.parse import quote
    cdn_url = (f"{api.cdn_base_url}/upload"
               f"?encrypted_query_param={quote(upload_param)}"
               f"&filekey={quote(filekey)}")

    # 步骤4：上传加密数据到CDN，支持重试
    download_param = None
    last_error = None
    for attempt in range(1, UPLOAD_MAX_RETRIES + 1):
        try:
            cdn_resp = requests.post(cdn_url, data=encrypted, headers={
                "Content-Type": "application/octet-stream",
            }, timeout=120)
            # 客户端错误（4xx）不重试，说明请求本身有问题
            if 400 <= cdn_resp.status_code < 500:
                err_msg = cdn_resp.headers.get("x-error-message", cdn_resp.text[:200])
                raise RuntimeError(f"CDN client error {cdn_resp.status_code}: {err_msg}")
            cdn_resp.raise_for_status()
            # 从响应头中获取下载参数，接收方使用此参数下载文件
            download_param = cdn_resp.headers.get("x-encrypted-param", "")
            if not download_param:
                raise RuntimeError("CDN response missing x-encrypted-param header")
            logger.debug(f"[Weixin] CDN upload success attempt={attempt} filekey={filekey}")
            break
        except Exception as e:
            last_error = e
            # 客户端错误直接抛出，不重试
            if "client error" in str(e):
                raise
            if attempt < UPLOAD_MAX_RETRIES:
                logger.warning(f"[Weixin] CDN upload attempt {attempt} failed, retrying: {e}")
            else:
                logger.error(f"[Weixin] CDN upload failed after {UPLOAD_MAX_RETRIES} attempts: {e}")

    if not download_param:
        raise last_error or RuntimeError("CDN upload failed")

    # 将AES密钥进行base64编码，用于在发送消息时传递给接收方
    aes_key_b64 = base64.b64encode(aes_key_hex.encode("utf-8")).decode("utf-8")

    return {
        "encrypt_query_param": download_param,
        "aes_key_b64": aes_key_b64,
        "ciphertext_size": cipher_size,
        "raw_size": raw_size,
    }


def download_media_from_cdn(cdn_base_url: str, encrypt_query_param: str,
                            aes_key: str, save_path: str) -> str:
    """从微信CDN下载并解密媒体文件。

    下载流程：
    1. 使用encrypt_query_param构造CDN下载URL
    2. GET请求下载加密的文件数据
    3. 解析AES密钥（支持多种格式）
    4. 使用AES-128-ECB解密文件
    5. 保存到指定路径

    AES密钥格式兼容性处理：
    1) 32字符十六进制字符串 → 直接转为16字节密钥
    2) base64编码 → 解码后如果32字节，视为十六进制编码 → 转16字节密钥
    3) base64编码 → 解码后16字节，直接作为密钥

    这种兼容处理是因为不同版本的微信协议可能使用不同的密钥编码方式。

    Args:
        cdn_base_url: CDN基础URL
        encrypt_query_param: 加密的查询参数（从消息中获取）
        aes_key: AES密钥（十六进制或base64编码）
        save_path: 解密后文件的保存路径

    Returns:
        保存路径（成功时等于save_path）

    Raises:
        ValueError: AES密钥格式无效时抛出
    """
    from urllib.parse import quote
    # 构造CDN下载URL
    url = f"{cdn_base_url}/download?encrypted_query_param={quote(encrypt_query_param)}"
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()

    # Determine key format:
    # 解析AES密钥，兼容多种编码格式
    # 1) 32-char hex string → 16 raw bytes
    # 2) base64 string → decode → if 32 bytes, treat as hex-encoded → 16 raw bytes
    # 3) base64 string → decode → 16 raw bytes directly
    try:
        # 尝试直接解析为十六进制字符串
        key_bytes = bytes.fromhex(aes_key)
        if len(key_bytes) != 16:
            raise ValueError()
    except (ValueError, TypeError):
        # 十六进制解析失败，尝试base64解码
        decoded = base64.b64decode(aes_key)
        if len(decoded) == 32:
            # 32字节的base64解码结果，视为十六进制编码的16字节密钥
            try:
                key_bytes = bytes.fromhex(decoded.decode("ascii"))
            except (ValueError, UnicodeDecodeError):
                raise ValueError(f"Invalid AES key: 32 bytes but not valid hex")
        elif len(decoded) == 16:
            # 16字节直接作为密钥
            key_bytes = decoded
        else:
            raise ValueError(f"Invalid AES key length after base64 decode: {len(decoded)}")

    # 使用AES-128-ECB解密
    decrypted = _aes_ecb_decrypt(resp.content, key_bytes)

    # 确保保存目录存在并写入文件
    os.makedirs(os.path.dirname(save_path), exist_ok=True)
    with open(save_path, "wb") as f:
        f.write(decrypted)
    return save_path
