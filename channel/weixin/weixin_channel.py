"""
微信通道实现。

使用HTTP长轮询（getUpdates）接收消息，通过sendMessage发送回复。
支持通过二维码扫码登录微信ilink机器人平台。

主要功能：
- 二维码登录：首次使用需扫码登录，凭证会保存到本地文件
- 会话保持：登录凭证持久化，重启后自动恢复登录状态
- 会话过期重登：检测到会话过期（errcode=-14）时自动重新扫码登录
- 消息收发：支持文本、图片、文件、视频等多类型消息
- 错误恢复：连续失败时自动退避重试，避免频繁请求
"""

import json
import os
import threading
import time
import uuid

import requests

from bridge.context import Context, ContextType
from bridge.reply import Reply, ReplyType
from channel.chat_channel import ChatChannel, check_prefix
from channel.weixin.weixin_api import (
    WeixinApi, upload_media_to_cdn,
    DEFAULT_BASE_URL, CDN_BASE_URL,
)
from channel.weixin.weixin_message import WeixinMessage
from common.expired_dict import ExpiredDict
from common.log import logger
from common.singleton import singleton
from config import conf

# 连续失败的最大次数，超过后进入退避等待
MAX_CONSECUTIVE_FAILURES = 3
# 退避等待时间（秒），连续失败达到上限后等待此时间再重试
BACKOFF_DELAY = 30
# 普通重试延迟（秒），未达连续失败上限时的重试间隔
RETRY_DELAY = 2
# 会话过期的错误码，需要重新登录
SESSION_EXPIRED_ERRCODE = -14
# 单条文本消息的长度上限，超过则分片发送
TEXT_CHUNK_LIMIT = 4000
# 二维码登录的超时时间（秒），8分钟内未扫码则超时
QR_LOGIN_TIMEOUT_S = 480
# 二维码最大刷新次数，超过后放弃登录
QR_MAX_REFRESHES = 10


def _load_credentials(cred_path: str) -> dict:
    """从JSON文件加载已保存的登录凭证。

    凭证文件中包含token、base_url、bot_id等信息，
    用于在重启后恢复登录状态，避免重复扫码。

    Args:
        cred_path: 凭证文件路径

    Returns:
        凭证字典，加载失败时返回空字典
    """
    try:
        if os.path.exists(cred_path):
            with open(cred_path, "r") as f:
                return json.load(f)
    except Exception as e:
        logger.warning(f"[Weixin] Failed to load credentials: {e}")
    return {}


def _save_credentials(cred_path: str, data: dict):
    """将登录凭证保存到JSON文件。

    保存后设置文件权限为600（仅所有者可读写），
    因为凭证中包含敏感的token信息。

    Args:
        cred_path: 凭证文件路径
        data: 要保存的凭证字典
    """
    os.makedirs(os.path.dirname(cred_path), exist_ok=True)
    with open(cred_path, "w") as f:
        json.dump(data, f, indent=2)
    # 设置文件权限为仅所有者可读写，保护敏感的token信息
    try:
        os.chmod(cred_path, 0o600)
    except Exception:
        pass


@singleton
class WeixinChannel(ChatChannel):
    """微信通道类，基于ilink机器人协议实现。

    通过HTTP长轮询方式接收微信消息，通过API发送回复。
    使用 @singleton 装饰器确保全局只有一个通道实例。

    支持的功能：
    - 二维码扫码登录，凭证持久化
    - 会话过期自动重新登录
    - 文本消息分片发送（超过4000字符自动分片）
    - 图片/文件/视频消息通过CDN上传发送
    - 连续失败自动退避重试
    """

    # 登录状态常量
    LOGIN_STATUS_IDLE = "idle"              # 空闲，未登录
    LOGIN_STATUS_WAITING = "waiting_scan"   # 等待扫码
    LOGIN_STATUS_SCANNED = "scanned"        # 已扫码，等待确认
    LOGIN_STATUS_OK = "logged_in"           # 已登录

    def __init__(self):
        """初始化微信通道。

        设置API客户端、消息去重缓存、上下文令牌缓存等。
        配置单聊前缀为空字符串，表示任何消息都会触发回复。
        """
        super().__init__()
        # 微信API客户端实例，登录后初始化
        self.api = None
        # 停止事件，用于优雅地关闭轮询线程
        self._stop_event = threading.Event()
        # 长轮询线程
        self._poll_thread = None
        # 上下文令牌缓存：user_id -> context_token
        # context_token是微信协议中关联请求和响应的必要参数
        self._context_tokens = {}  # user_id -> context_token
        # 已接收消息的去重缓存，过期时间7.1小时
        self._received_msgs = ExpiredDict(60 * 60 * 7.1)
        # getUpdates的同步游标，确保消息不丢失不重复
        self._get_updates_buf = ""
        # 凭证文件路径
        self._credentials_path = ""
        # 当前登录状态
        self.login_status = self.LOGIN_STATUS_IDLE
        # 当前二维码URL（用于云模式展示）
        self._current_qr_url = ""

        # 配置单聊前缀为空，任何消息都触发回复
        conf()["single_chat_prefix"] = [""]

    # ── Lifecycle ──────────────────────────────────────────────────────

    def startup(self):
        """启动微信通道。

        启动流程：
        1. 从配置中读取API地址和token
        2. 如果没有token，尝试从凭证文件加载
        3. 如果仍然没有token，启动二维码登录流程
        4. 初始化API客户端并开始长轮询

        如果登录失败则不启动轮询。
        """
        # 清除停止事件标志
        self._stop_event.clear()

        # 读取配置
        base_url = conf().get("weixin_base_url", DEFAULT_BASE_URL)
        cdn_base_url = conf().get("weixin_cdn_base_url", CDN_BASE_URL)
        token = conf().get("weixin_token", "")

        # 凭证文件路径，默认保存在用户主目录下
        self._credentials_path = os.path.expanduser(
            conf().get("weixin_credentials_path", "~/.weixin_cow_credentials.json")
        )

        # 如果配置中没有token，尝试从凭证文件加载
        if not token:
            creds = _load_credentials(self._credentials_path)
            token = creds.get("token", "")
            # 凭证文件中可能包含不同的base_url（登录时服务端指定的）
            if creds.get("base_url"):
                base_url = creds["base_url"]

        # 仍然没有token，启动二维码登录
        if not token:
            token, base_url = self._login_with_retry(base_url)
            if not token:
                # 登录失败或被取消，不启动轮询
                return

        # 初始化API客户端
        self.api = WeixinApi(base_url=base_url, token=token, cdn_base_url=cdn_base_url)
        self.login_status = self.LOGIN_STATUS_OK

        logger.info(f"[Weixin] 微信通道已启动，凭证保存在 {self._credentials_path}，"
                     f"如需重新扫码登录请删除该文件后重启")
        self.report_startup_success()

        # 进入长轮询主循环
        self._poll_loop()

    def _login_with_retry(self, base_url: str) -> tuple:
        """尝试二维码登录，如果失败则等待停止信号。

        登录超时后不会立即退出，而是等待用户通过控制台重新接入。
        这是因为在Docker等容器环境中，进程退出会导致容器重启。

        Args:
            base_url: API基础URL

        Returns:
            (token, base_url) 元组，登录失败或被停止时返回 ("", "")
        """
        logger.info("[Weixin] No token found, starting QR login...")
        self.login_status = self.LOGIN_STATUS_WAITING
        login_result = self._qr_login(base_url)
        if login_result:
            return login_result["token"], login_result.get("base_url", base_url)

        # 登录超时或失败，等待用户操作
        self.login_status = self.LOGIN_STATUS_IDLE
        if not self._stop_event.is_set():
            logger.info("[Weixin] QR login timed out, waiting for stop or reconnect...")
            print("  二维码登录超时，请通过控制台重新接入\n")
            # 阻塞等待，直到收到停止信号
            self._stop_event.wait()

        logger.info("[Weixin] Login cancelled by stop event")
        return "", ""

    def stop(self):
        """停止微信通道。

        设置停止事件，长轮询线程会在下一次循环检查时退出。
        """
        logger.info("[Weixin] stop() called")
        self._stop_event.set()

    def _relogin(self) -> bool:
        """会话过期后重新登录。

        删除旧凭证文件，重新启动二维码登录流程。
        登录成功后更新API客户端并清除上下文令牌缓存。

        Returns:
            登录成功返回True，失败返回False
        """
        base_url = self.api.base_url if self.api else DEFAULT_BASE_URL
        # 删除旧凭证文件，确保使用新的登录结果
        if os.path.exists(self._credentials_path):
            try:
                os.remove(self._credentials_path)
            except Exception:
                pass
        self.login_status = self.LOGIN_STATUS_WAITING
        result = self._qr_login(base_url)
        if not result:
            self.login_status = self.LOGIN_STATUS_IDLE
            return False
        # 创建新的API客户端
        self.api = WeixinApi(
            base_url=result.get("base_url", base_url),
            token=result["token"],
            cdn_base_url=self.api.cdn_base_url if self.api else CDN_BASE_URL,
        )
        self.login_status = self.LOGIN_STATUS_OK
        # 清除旧的上下文令牌缓存，因为新会话的token已失效
        self._context_tokens.clear()
        return True

    # ── QR Login ───────────────────────────────────────────────────────

    @staticmethod
    def _print_qr(qrcode_url: str):
        """在终端打印二维码供用户扫码。

        优先使用qrcode库在终端直接渲染ASCII二维码，
        如果未安装则退而显示URL链接。

        Args:
            qrcode_url: 二维码对应的URL
        """
        print("\n" + "=" * 60)
        print("  请使用微信扫描二维码登录 (二维码约2分钟后过期)")
        print("=" * 60)
        try:
            import qrcode as qr_lib
            # 使用低纠错等级和小方框尺寸，使二维码适合终端显示
            qr = qr_lib.QRCode(error_correction=qr_lib.constants.ERROR_CORRECT_L, box_size=1, border=1)
            qr.add_data(qrcode_url)
            qr.make(fit=True)
            qr.print_ascii(invert=True)
        except ImportError:
            # 未安装qrcode库，显示URL链接
            print(f"\n  二维码链接: {qrcode_url}")
            print("  (安装 'qrcode' 包可在终端显示二维码)\n")

    def _notify_cloud_qrcode(self, qrcode_url: str):
        """在云模式下将二维码URL通知到云控制台。

        当运行在云环境中时（如Docker部署），
        终端二维码可能不可见，需要将URL发送到云控制台展示。

        Args:
            qrcode_url: 二维码对应的URL
        """
        if not self.cloud_mode:
            return
        try:
            from common import cloud_client
            client = getattr(cloud_client, "chat_client", None)
            if client and getattr(client, "client_id", None):
                client.send_channel_qrcode("weixin", qrcode_url)
        except Exception as e:
            logger.warning(f"[Weixin] Failed to notify cloud QR code: {e}")

    def _notify_cloud_connected(self):
        """在云模式下通知登录成功状态。

        登录成功后通知云控制台，以便更新通道状态显示。
        """
        if not self.cloud_mode:
            return
        try:
            from common import cloud_client
            client = getattr(cloud_client, "chat_client", None)
            if client and getattr(client, "client_id", None):
                client.send_channel_status("weixin", "connected")
        except Exception as e:
            logger.warning(f"[Weixin] Failed to notify cloud connected: {e}")

    def _qr_login(self, base_url: str) -> dict:
        """执行交互式二维码登录流程。

        完整的登录流程：
        1. 请求服务端生成二维码
        2. 在终端和云控制台展示二维码
        3. 定期轮询扫码状态
        4. 处理各种状态：等待扫码、已扫码、已过期、已确认
        5. 二维码过期时自动刷新（最多刷新QR_MAX_REFRESHES次）
        6. 登录确认后保存凭证到本地文件

        Args:
            base_url: API基础URL

        Returns:
            包含token和base_url的字典，失败返回空字典
        """
        api = WeixinApi(base_url=base_url)
        # 获取二维码
        try:
            qr_resp = api.fetch_qr_code()
        except Exception as e:
            logger.error(f"[Weixin] Failed to fetch QR code: {e}")
            return {}

        qrcode = qr_resp.get("qrcode", "")
        qrcode_url = qr_resp.get("qrcode_img_content", "")

        if not qrcode:
            logger.error("[Weixin] No QR code returned from server")
            return {}

        self._current_qr_url = qrcode_url
        logger.info(f"[Weixin] QR code URL: {qrcode_url}")
        # 展示二维码
        self._print_qr(qrcode_url)
        self._notify_cloud_qrcode(qrcode_url)
        print("  等待扫码...\n")

        # 轮询状态
        scanned_printed = False  # 是否已打印"已扫码"提示
        refresh_count = 0  # 二维码刷新次数
        deadline = time.time() + QR_LOGIN_TIMEOUT_S  # 总超时截止时间

        while not self._stop_event.is_set():
            # 检查总超时
            if time.time() >= deadline:
                logger.warning(f"[Weixin] QR login timed out after {QR_LOGIN_TIMEOUT_S}s")
                print(f"\n  二维码登录超时（{QR_LOGIN_TIMEOUT_S}s），请重启后重试")
                break

            # 轮询扫码状态
            try:
                status_resp = api.poll_qr_status(qrcode)
            except Exception as e:
                logger.error(f"[Weixin] QR status poll error: {e}")
                return {}

            status = status_resp.get("status", "wait")

            if status == "wait":
                # 等待扫码，继续轮询
                pass
            elif status == "scaned":
                # 已扫码，更新登录状态并提示用户确认
                self.login_status = self.LOGIN_STATUS_SCANNED
                if not scanned_printed:
                    print("  已扫码，请在手机上确认...")
                    scanned_printed = True
            elif status == "expired":
                # 二维码已过期，尝试刷新
                refresh_count += 1
                if refresh_count >= QR_MAX_REFRESHES:
                    logger.warning(f"[Weixin] QR code refreshed {QR_MAX_REFRESHES} times, giving up")
                    print(f"\n  二维码已刷新 {QR_MAX_REFRESHES} 次仍未扫码，请重启后重试")
                    break
                print(f"  二维码已过期，正在刷新（{refresh_count}/{QR_MAX_REFRESHES}）...")
                try:
                    # 获取新的二维码
                    qr_resp = api.fetch_qr_code()
                    qrcode = qr_resp.get("qrcode", "")
                    qrcode_url = qr_resp.get("qrcode_img_content", "")
                    scanned_printed = False
                    self._current_qr_url = qrcode_url
                    logger.info(f"[Weixin] New QR code ({refresh_count}/{QR_MAX_REFRESHES}): {qrcode_url}")
                    self._print_qr(qrcode_url)
                    self._notify_cloud_qrcode(qrcode_url)
                except Exception as e:
                    logger.error(f"[Weixin] QR refresh failed: {e}")
                    return {}
            elif status == "confirmed":
                # 登录已确认，提取凭证信息
                bot_token = status_resp.get("bot_token", "")
                bot_id = status_resp.get("ilink_bot_id", "")
                result_base_url = status_resp.get("baseurl", base_url)
                user_id = status_resp.get("ilink_user_id", "")

                if not bot_token or not bot_id:
                    logger.error("[Weixin] Login confirmed but missing token/bot_id")
                    return {}

                self._current_qr_url = ""
                print(f"\n  ✅ 微信登录成功！bot_id={bot_id}")
                logger.info(f"[Weixin] Login confirmed: bot_id={bot_id}")
                self._notify_cloud_connected()

                # 保存凭证到本地文件，下次启动时自动登录
                creds = {
                    "token": bot_token,
                    "base_url": result_base_url,
                    "bot_id": bot_id,
                    "user_id": user_id,
                }
                _save_credentials(self._credentials_path, creds)
                logger.info(f"[Weixin] Credentials saved to {self._credentials_path}")

                return {"token": bot_token, "base_url": result_base_url}

            # 每秒轮询一次，同时检查stop_event
            self._stop_event.wait(1)

        self._current_qr_url = ""
        if self._stop_event.is_set():
            logger.info("[Weixin] QR login cancelled by stop event")
        return {}

    # ── Long-poll loop ─────────────────────────────────────────────────

    def _poll_loop(self):
        """长轮询主循环：getUpdates -> 解析消息 -> 投递到处理队列。

        循环逻辑：
        1. 调用getUpdates获取新消息（HTTP长轮询，服务端有消息时才返回）
        2. 检查响应是否为错误：
           - 会话过期（errcode=-14）：自动重新登录
           - 其他错误：累计连续失败次数，达到上限后退避等待
        3. 更新同步游标，确保消息不丢失
        4. 逐条处理新消息

        错误恢复策略：
        - 连续失败次数 < MAX_CONSECUTIVE_FAILURES：等待RETRY_DELAY后重试
        - 连续失败次数 >= MAX_CONSECUTIVE_FAILURES：等待BACKOFF_DELAY后重试
        - 会话过期：重新登录后继续轮询
        """
        logger.info("[Weixin] Starting long-poll loop")
        consecutive_failures = 0

        while not self._stop_event.is_set():
            try:
                # 调用getUpdates长轮询接口
                resp = self.api.get_updates(self._get_updates_buf)

                ret = resp.get("ret", 0)
                errcode = resp.get("errcode", 0)

                is_error = (ret != 0) or (errcode != 0)
                if is_error:
                    # 检查是否为会话过期错误
                    if errcode == SESSION_EXPIRED_ERRCODE or ret == SESSION_EXPIRED_ERRCODE:
                        logger.error("[Weixin] Session expired (errcode -14), starting re-login...")
                        if self._relogin():
                            # 重新登录成功，重置游标和失败计数，继续轮询
                            logger.info("[Weixin] Re-login successful, resuming long-poll")
                            self._get_updates_buf = ""
                            consecutive_failures = 0
                            continue
                        else:
                            # 重新登录失败，等待5分钟后重试
                            logger.error("[Weixin] Re-login failed, will retry in 5 minutes")
                            self._stop_event.wait(300)
                            continue

                    # 非会话过期错误，累计失败次数
                    consecutive_failures += 1
                    errmsg = resp.get("errmsg", "")
                    logger.error(f"[Weixin] getUpdates error: ret={ret} errcode={errcode} "
                                 f"errmsg={errmsg} ({consecutive_failures}/{MAX_CONSECUTIVE_FAILURES})")
                    if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                        # 达到连续失败上限，重置计数并进入退避等待
                        consecutive_failures = 0
                        self._stop_event.wait(BACKOFF_DELAY)
                    else:
                        self._stop_event.wait(RETRY_DELAY)
                    continue

                # 请求成功，重置失败计数
                consecutive_failures = 0

                # Update sync cursor
                # 更新同步游标，确保下次轮询从正确位置开始
                new_buf = resp.get("get_updates_buf", "")
                if new_buf:
                    self._get_updates_buf = new_buf

                # Process messages
                # 逐条处理新消息
                msgs = resp.get("msgs", [])
                for raw_msg in msgs:
                    try:
                        self._process_message(raw_msg)
                    except Exception as e:
                        logger.error(f"[Weixin] Failed to process message: {e}", exc_info=True)

            except Exception as e:
                # 网络异常等非API错误
                if self._stop_event.is_set():
                    break
                consecutive_failures += 1
                logger.error(f"[Weixin] getUpdates exception: {e} "
                             f"({consecutive_failures}/{MAX_CONSECUTIVE_FAILURES})")
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    consecutive_failures = 0
                    self._stop_event.wait(BACKOFF_DELAY)
                else:
                    self._stop_event.wait(RETRY_DELAY)

        logger.info("[Weixin] Long-poll loop ended")

    def _process_message(self, raw_msg: dict):
        """解析单条入站消息并投递到处理队列。

        处理流程：
        1. 过滤非用户消息（只处理message_type=1的消息）
        2. 消息去重：通过message_id过滤已处理的消息
        3. 缓存context_token：用于后续回复时关联会话
        4. 解析消息内容：创建WeixinMessage对象
        5. 文件缓存：图片和文件类型消息先缓存路径
        6. 文本消息：附加缓存文件引用
        7. 组装上下文并投递

        Args:
            raw_msg: 从getUpdates获取的原始消息字典
        """
        msg_type = raw_msg.get("message_type", 0)
        if msg_type != 1:  # Only process USER messages (type=1)
            # 仅处理用户消息，其他类型（如系统消息）跳过
            return

        # 消息去重
        msg_id = str(raw_msg.get("message_id", raw_msg.get("seq", "")))
        if self._received_msgs.get(msg_id):
            return
        self._received_msgs[msg_id] = True

        from_user = raw_msg.get("from_user_id", "")
        context_token = raw_msg.get("context_token", "")

        # 缓存context_token，后续发送回复时必须使用同一个token
        if context_token and from_user:
            self._context_tokens[from_user] = context_token

        cdn_base_url = self.api.cdn_base_url if self.api else CDN_BASE_URL
        # 解析消息内容
        try:
            wx_msg = WeixinMessage(raw_msg, cdn_base_url=cdn_base_url)
        except Exception as e:
            logger.error(f"[Weixin] Failed to parse WeixinMessage: {e}", exc_info=True)
            return

        logger.info(f"[Weixin] Received: from={from_user} ctype={wx_msg.ctype} "
                     f"content={str(wx_msg.content)[:50]}")

        # File cache logic
        # 文件缓存逻辑：图片和文件先缓存，等后续文本消息到来时一并处理
        from channel.file_cache import get_file_cache
        file_cache = get_file_cache()
        session_id = from_user

        # 图片消息：下载后缓存路径，不立即处理
        if wx_msg.ctype == ContextType.IMAGE:
            if hasattr(wx_msg, "image_path") and wx_msg.image_path:
                file_cache.add(session_id, wx_msg.image_path, file_type="image")
                logger.info(f"[Weixin] Image cached for session {session_id}")
            return

        # 文件消息：先下载（prepare），然后缓存路径
        if wx_msg.ctype == ContextType.FILE:
            wx_msg.prepare()
            file_cache.add(session_id, wx_msg.content, file_type="file")
            logger.info(f"[Weixin] File cached for session {session_id}: {wx_msg.content}")
            return

        # 文本消息：检查是否有缓存的文件需要附加引用
        if wx_msg.ctype == ContextType.TEXT:
            cached_files = file_cache.get(session_id)
            if cached_files:
                refs = []
                for fi in cached_files:
                    ftype, fpath = fi["type"], fi["path"]
                    if ftype == "image":
                        refs.append(f"[图片: {fpath}]")
                    elif ftype == "video":
                        refs.append(f"[视频: {fpath}]")
                    else:
                        refs.append(f"[文件: {fpath}]")
                # 将文件引用追加到文本内容中
                wx_msg.content = wx_msg.content + "\n" + "\n".join(refs)
                # 清除已使用的缓存
                file_cache.clear(session_id)

        # 组装上下文并投递到消息处理队列
        context = self._compose_context(
            wx_msg.ctype,
            wx_msg.content,
            isgroup=False,
            msg=wx_msg,
            no_need_at=True,
        )
        if context:
            self.produce(context)

    # ── _compose_context ───────────────────────────────────────────────

    def _compose_context(self, ctype: ContextType, content, **kwargs):
        """组装消息上下文Context对象。

        将微信消息统一封装为Context对象，包含：
        - 消息类型和内容
        - 会话ID（微信仅支持单聊，使用from_user_id）
        - 接收者ID
        - 是否为图片创作请求的判断

        Args:
            ctype: 消息类型（TEXT, IMAGE, FILE等）
            content: 消息内容
            **kwargs: 额外参数

        Returns:
            组装完成的Context对象
        """
        context = Context(ctype, content)
        context.kwargs = kwargs
        # 设置通道类型
        if "channel_type" not in context:
            context["channel_type"] = self.channel_type
        # 记录原始消息类型
        if "origin_ctype" not in context:
            context["origin_ctype"] = ctype

        cmsg = context["msg"]
        # 微信仅支持单聊，session_id直接使用发送者ID
        context["session_id"] = cmsg.from_user_id
        context["receiver"] = cmsg.other_user_id

        # 处理文本消息：检查是否为图片创作请求
        if ctype == ContextType.TEXT:
            img_match_prefix = check_prefix(content, conf().get("image_create_prefix"))
            if img_match_prefix:
                # 匹配到图片创作前缀，修改消息类型
                content = content.replace(img_match_prefix, "", 1)
                context.type = ContextType.IMAGE_CREATE
            else:
                context.type = ContextType.TEXT
            context.content = content.strip()

        return context

    # ── Send reply ─────────────────────────────────────────────────────

    def send(self, reply: Reply, context: Context):
        """发送回复消息。

        根据回复类型分发到不同的发送方法：
        - TEXT: 发送文本消息（超过4000字符自动分片）
        - IMAGE/IMAGE_URL: 上传到CDN后发送图片
        - FILE: 上传到CDN后发送文件
        - VIDEO/VIDEO_URL: 上传到CDN后发送视频
        - 其他类型: 降级为文本发送

        所有发送都需要context_token，没有token则无法发送。

        Args:
            reply: 回复对象，包含类型和内容
            context: 上下文对象，包含接收者信息
        """
        receiver = context.get("receiver", "")
        msg = context.get("msg")
        context_token = self._get_context_token(receiver, msg)

        if not context_token:
            logger.error(f"[Weixin] No context_token for receiver={receiver}, cannot send")
            return

        if reply.type == ReplyType.TEXT:
            self._send_text(reply.content, receiver, context_token)
        elif reply.type in (ReplyType.IMAGE_URL, ReplyType.IMAGE):
            self._send_image(reply.content, receiver, context_token)
        elif reply.type == ReplyType.FILE:
            self._send_file(reply.content, receiver, context_token)
        elif reply.type in (ReplyType.VIDEO, ReplyType.VIDEO_URL):
            self._send_video(reply.content, receiver, context_token)
        else:
            # 不支持的回复类型，降级为文本
            logger.warning(f"[Weixin] Unsupported reply type: {reply.type}, fallback to text")
            self._send_text(str(reply.content), receiver, context_token)

    def _get_context_token(self, receiver: str, msg=None) -> str:
        """获取指定接收者的context_token。

        context_token是微信协议中关联请求和响应的必要参数，
        每条用户消息都携带一个context_token，回复时必须使用同一个token。

        优先从消息对象中获取，如果没有则从缓存中查找。

        Args:
            receiver: 接收者用户ID
            msg: 消息对象，可能包含context_token

        Returns:
            context_token字符串，未找到时返回空字符串
        """
        if msg and hasattr(msg, "context_token") and msg.context_token:
            return msg.context_token
        return self._context_tokens.get(receiver, "")

    def _send_text(self, text: str, receiver: str, context_token: str):
        """发送文本消息。

        如果文本长度超过TEXT_CHUNK_LIMIT（4000字符），
        会自动分片发送，优先在段落和行边界处切分。
        分片之间间隔0.5秒，避免消息顺序混乱。

        Args:
            text: 文本内容
            receiver: 接收者用户ID
            context_token: 上下文令牌
        """
        if len(text) <= TEXT_CHUNK_LIMIT:
            # 短文本直接发送
            try:
                self.api.send_text(receiver, text, context_token)
                logger.debug(f"[Weixin] Text sent to {receiver}, len={len(text)}")
            except Exception as e:
                logger.error(f"[Weixin] Failed to send text: {e}")
            return

        # 长文本分片发送
        chunks = self._split_text(text, TEXT_CHUNK_LIMIT)
        for i, chunk in enumerate(chunks):
            try:
                self.api.send_text(receiver, chunk, context_token)
                logger.debug(f"[Weixin] Text chunk {i+1}/{len(chunks)} sent to {receiver}, len={len(chunk)}")
            except Exception as e:
                logger.error(f"[Weixin] Failed to send text chunk {i+1}/{len(chunks)}: {e}")
                break
            # 分片之间短暂延迟，确保消息顺序正确
            if i < len(chunks) - 1:
                time.sleep(0.5)

    @staticmethod
    def _split_text(text: str, limit: int) -> list:
        """将长文本分片，优先在段落或行边界处切分。

        切分优先级：
        1. 双换行（段落边界）
        2. 单换行（行边界）
        3. 强制在limit处切分

        切分后去除每段开头的换行符，避免产生空行。

        Args:
            text: 待切分的文本
            limit: 每片的最大字符数

        Returns:
            文本片段列表
        """
        if len(text) <= limit:
            return [text]
        chunks = []
        while text:
            if len(text) <= limit:
                chunks.append(text)
                break
            # 优先在段落边界切分
            cut = text.rfind("\n\n", 0, limit)
            if cut <= 0:
                # 没有段落边界，尝试行边界
                cut = text.rfind("\n", 0, limit)
            if cut <= 0:
                # 没有合适的换行边界，强制切分
                cut = limit
            chunks.append(text[:cut])
            # 去除切分点后的换行符
            text = text[cut:].lstrip("\n")
        return chunks

    def _send_image(self, img_path_or_url: str, receiver: str, context_token: str):
        """发送图片消息。

        流程：
        1. 解析图片路径（支持本地路径、file://和http(s)://）
        2. 上传图片到CDN（使用AES-128-ECB加密）
        3. 发送包含CDN参数的图片消息

        Args:
            img_path_or_url: 图片的本地路径或URL
            receiver: 接收者用户ID
            context_token: 上下文令牌
        """
        local_path = self._resolve_media_path(img_path_or_url)
        if not local_path:
            self._send_text("[Image send failed: file not found]", receiver, context_token)
            return
        try:
            # 上传到CDN，media_type=1表示图片
            result = upload_media_to_cdn(self.api, local_path, receiver, media_type=1)
            self.api.send_image_item(
                to=receiver,
                context_token=context_token,
                encrypt_query_param=result["encrypt_query_param"],
                aes_key_b64=result["aes_key_b64"],
                ciphertext_size=result["ciphertext_size"],
            )
            logger.info(f"[Weixin] Image sent to {receiver}")
        except Exception as e:
            logger.error(f"[Weixin] Image send failed: {e}")
            self._send_text("[Image send failed]", receiver, context_token)

    def _send_file(self, file_path_or_url: str, receiver: str, context_token: str):
        """发送文件消息。

        流程：
        1. 解析文件路径
        2. 上传文件到CDN
        3. 发送包含CDN参数的文件消息

        Args:
            file_path_or_url: 文件的本地路径或URL
            receiver: 接收者用户ID
            context_token: 上下文令牌
        """
        local_path = self._resolve_media_path(file_path_or_url)
        if not local_path:
            self._send_text("[File send failed: file not found]", receiver, context_token)
            return
        try:
            # 上传到CDN，media_type=3表示文件
            result = upload_media_to_cdn(self.api, local_path, receiver, media_type=3)
            self.api.send_file_item(
                to=receiver,
                context_token=context_token,
                encrypt_query_param=result["encrypt_query_param"],
                aes_key_b64=result["aes_key_b64"],
                file_name=os.path.basename(local_path),
                file_size=result["raw_size"],
            )
            logger.info(f"[Weixin] File sent to {receiver}")
        except Exception as e:
            logger.error(f"[Weixin] File send failed: {e}")
            self._send_text("[File send failed]", receiver, context_token)

    def _send_video(self, video_path_or_url: str, receiver: str, context_token: str):
        """发送视频消息。

        流程：
        1. 解析视频路径
        2. 上传视频到CDN
        3. 发送包含CDN参数的视频消息

        Args:
            video_path_or_url: 视频的本地路径或URL
            receiver: 接收者用户ID
            context_token: 上下文令牌
        """
        local_path = self._resolve_media_path(video_path_or_url)
        if not local_path:
            self._send_text("[Video send failed: file not found]", receiver, context_token)
            return
        try:
            # 上传到CDN，media_type=2表示视频
            result = upload_media_to_cdn(self.api, local_path, receiver, media_type=2)
            self.api.send_video_item(
                to=receiver,
                context_token=context_token,
                encrypt_query_param=result["encrypt_query_param"],
                aes_key_b64=result["aes_key_b64"],
                ciphertext_size=result["ciphertext_size"],
            )
            logger.info(f"[Weixin] Video sent to {receiver}")
        except Exception as e:
            logger.error(f"[Weixin] Video send failed: {e}")
            self._send_text("[Video send failed]", receiver, context_token)

    @staticmethod
    def _resolve_media_path(path_or_url: str) -> str:
        """将文件路径或URL解析为本地文件路径，必要时下载到本地。

        支持的输入格式：
        - 本地绝对/相对路径
        - file:// 协议路径
        - http:// 或 https:// URL（自动下载到临时文件）

        根据Content-Type头猜测文件扩展名。

        Args:
            path_or_url: 文件路径或URL

        Returns:
            本地文件路径，文件不存在或下载失败时返回空字符串
        """
        if not path_or_url:
            return ""

        local_path = path_or_url
        # 处理file://协议
        if local_path.startswith("file://"):
            local_path = local_path[7:]

        # 处理网络URL，下载到本地临时文件
        if local_path.startswith(("http://", "https://")):
            try:
                resp = requests.get(local_path, timeout=60)
                resp.raise_for_status()
                # 根据Content-Type确定文件扩展名
                ct = resp.headers.get("Content-Type", "")
                ext = ".bin"
                if "jpeg" in ct or "jpg" in ct:
                    ext = ".jpg"
                elif "png" in ct:
                    ext = ".png"
                elif "gif" in ct:
                    ext = ".gif"
                elif "webp" in ct:
                    ext = ".webp"
                elif "mp4" in ct:
                    ext = ".mp4"
                elif "pdf" in ct:
                    ext = ".pdf"

                tmp_path = f"/tmp/wx_media_{uuid.uuid4().hex[:8]}{ext}"
                with open(tmp_path, "wb") as f:
                    f.write(resp.content)
                return tmp_path
            except Exception as e:
                logger.error(f"[Weixin] Failed to download media: {e}")
                return ""

        # 检查本地文件是否存在
        if os.path.exists(local_path):
            return local_path

        logger.warning(f"[Weixin] Media file not found: {local_path}")
        return ""
