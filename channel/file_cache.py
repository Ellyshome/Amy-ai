"""
文件缓存管理器
用于缓存单独发送的文件消息（图片、视频、文档等），在用户提问时自动附加

File Cache Manager
Manages a session-based cache for standalone file messages (images, videos, documents, etc.).
When a user sends a file and then asks a question, the cached file is automatically
attached to the query context so the agent/model can process it.

设计背景：
在即时通讯场景中，用户经常先发送一张图片或文件，然后再发送文字消息进行提问。
例如：用户发送一张截图，然后问"这个错误怎么解决？"。为了让模型能够理解
用户的问题，需要将之前发送的文件自动附加到查询上下文中。

文件缓存管理器通过 session_id 将文件与用户的会话关联起来，并设置 TTL
（默认2分钟）来控制缓存的生命周期，避免无限增长。
"""
import time
import logging

logger = logging.getLogger(__name__)


class FileCache:
    """
    文件缓存管理器，按 session_id 缓存文件，TTL=2分钟
    File cache manager that caches files by session_id with a TTL of 2 minutes.

    缓存结构为嵌套字典：
    {
        session_id: {
            'files': [          # 文件列表，支持同一会话缓存多个文件
                {'path': '/tmp/img1.jpg', 'type': 'image'},
                {'path': '/tmp/doc1.pdf', 'type': 'file'},
            ],
            'timestamp': 1700000000.0   # 最后更新时间戳，用于 TTL 过期判断
        }
    }

    设计要点：
    1. 按 session_id 隔离：不同会话的文件互不干扰
    2. TTL 过期机制：默认2分钟，超时自动失效，防止缓存无限增长
    3. 文件去重：同一会话中重复添加相同文件不会重复缓存
    4. 线程安全注意事项：当前实现非线程安全，在多线程环境中
       需要外部加锁保护（通常由通道的消息处理串行化保证）
    """

    def __init__(self, ttl=120):
        """
        Args:
            ttl: 缓存过期时间（秒），默认2分钟
                 TTL 的设计考量：用户发送文件后通常会在短时间内提问，
                 2分钟足以覆盖绝大多数使用场景，同时避免长时间占用内存。
                 如果用户超过2分钟才提问，文件缓存已过期，模型将无法看到文件。
        """
        self.cache = {}
        # 缓存字典，键为 session_id，值为包含文件列表和时间戳的字典
        self.ttl = ttl
        # 缓存生存时间（秒），超过此时间的缓存条目将被视为过期

    def add(self, session_id: str, file_path: str, file_type: str = "image"):
        """
        添加文件到缓存
        Add a file to the cache for the given session.

        如果该 session_id 首次添加文件，会自动创建缓存条目。
        添加前会进行去重检查，避免同一文件被重复缓存。

        Args:
            session_id: 会话ID，通常由通道根据用户ID和群ID组合生成，
                        用于将文件与正确的会话上下文关联
            file_path: 文件本地路径，由通道在接收文件时保存到本地后传入
            file_type: 文件类型（image, video, file 等），默认为 "image"。
                       该类型信息用于告知模型或处理链路文件的格式，
                       以便选择正确的处理方式（如图片走视觉模型，文档走文本解析）
        """
        if session_id not in self.cache:
            # 首次为该会话添加文件，初始化缓存条目
            self.cache[session_id] = {
                'files': [],
                'timestamp': time.time()
            }

        # 添加文件（去重）
        # 构建文件信息字典，包含路径和类型
        file_info = {'path': file_path, 'type': file_type}
        # 去重检查：如果该文件已存在于缓存中，则跳过
        # 这避免了用户重复发送同一文件时产生冗余缓存
        if file_info not in self.cache[session_id]['files']:
            self.cache[session_id]['files'].append(file_info)
            logger.info(f"[FileCache] Added {file_type} to cache for session {session_id}: {file_path}")

    def get(self, session_id: str) -> list:
        """
        获取缓存的文件列表
        Get the cached file list for the given session.

        获取时会检查缓存是否过期，如果已过期则自动清除并返回空列表。
        注意：此方法不会刷新缓存的时间戳，只有 add() 方法会更新时间戳。
        这意味着如果用户只提问但不发送新文件，缓存仍会在 TTL 后过期。

        Args:
            session_id: 会话ID

        Returns:
            文件信息列表 [{'path': '...', 'type': 'image'}, ...]，
            如果没有缓存或已过期返回空列表。
            空列表的含义：该会话没有待处理的文件，模型应仅基于文本内容回复。
        """
        if session_id not in self.cache:
            # 会话不存在缓存，直接返回空列表
            return []

        item = self.cache[session_id]

        # 检查是否过期
        # 比较当前时间与缓存创建/更新时间的差值，超过 TTL 则视为过期
        if time.time() - item['timestamp'] > self.ttl:
            logger.info(f"[FileCache] Cache expired for session {session_id}, clearing...")
            # 过期缓存直接删除，释放内存
            del self.cache[session_id]
            return []

        # 缓存有效，返回文件列表
        return item['files']

    def clear(self, session_id: str):
        """
        清除指定会话的缓存
        Clear the cache for the specified session.

        通常在以下场景调用：
        1. 用户主动清除会话上下文时
        2. 文件已被消费（附加到查询中）后，清除以释放内存
        3. 会话结束时清理资源

        Args:
            session_id: 会话ID
        """
        if session_id in self.cache:
            logger.info(f"[FileCache] Cleared cache for session {session_id}")
            del self.cache[session_id]

    def cleanup_expired(self):
        """
        清理所有过期的缓存
        Clean up all expired cache entries.

        该方法遍历所有缓存条目，删除已过期的条目。
        通常由定时任务或低频钩子调用，作为被动过期检查（get方法中的过期删除）的补充。
        主动清理的必要性：如果某些会话的缓存过期后从未被 get() 访问，
        它们会一直占用内存。定期调用此方法可以回收这些"僵尸"缓存。

        注意：此方法使用"先收集后删除"的策略，避免在遍历字典时修改字典
        导致的 RuntimeError: dictionary changed size during iteration。
        """
        current_time = time.time()
        # 先收集所有过期的 session_id，避免遍历时修改字典
        expired_sessions = []

        for session_id, item in self.cache.items():
            # 检查每个缓存条目是否超过 TTL
            if current_time - item['timestamp'] > self.ttl:
                expired_sessions.append(session_id)

        # 批量删除过期的缓存条目
        for session_id in expired_sessions:
            del self.cache[session_id]
            logger.debug(f"[FileCache] Cleaned up expired cache for session {session_id}")

        if expired_sessions:
            logger.info(f"[FileCache] Cleaned up {len(expired_sessions)} expired cache(s)")


# 全局单例
# 文件缓存的全局单例实例，确保整个应用共享同一个缓存管理器。
# 使用模块级变量实现单例模式，相比类方法或装饰器方式更简单直接。
# 所有需要访问文件缓存的代码应通过 get_file_cache() 函数获取此实例。
_file_cache = FileCache()


def get_file_cache() -> FileCache:
    """
    获取全局文件缓存实例
    Get the global file cache instance.

    返回模块级单例实例，确保所有调用者操作的是同一个缓存管理器。
    这是访问文件缓存的推荐方式，避免直接导入 _file_cache 变量。

    Returns:
        FileCache: 全局文件缓存管理器实例
    """
    return _file_cache
