"""
临时目录管理，提供统一的临时文件存放路径(./tmp/)。
自动创建目录，被语音转换、文件缓存等模块用于存放运行时临时文件。
核心类：TmpDir，单例式使用，通过path()获取临时目录路径。
"""
import os
import pathlib

from config import conf


class TmpDir(object):
    """A temporary directory that is deleted when the object is destroyed."""

    tmpFilePath = pathlib.Path("./tmp/")

    def __init__(self):
        pathExists = os.path.exists(self.tmpFilePath)
        if not pathExists:
            os.makedirs(self.tmpFilePath)

    def path(self):
        return str(self.tmpFilePath) + "/"
