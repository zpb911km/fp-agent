"""
极简日志接口 — 替代 display 在 core 层的终端耦合

用法:
  from fp_core.logger import get_logger
  get_logger().info("消息")

默认静默（空操作），调用方在初始化时注入具体实现:
  from fp_core.logger import set_logger
  set_logger(my_logger)
"""


class Logger:
    """极简日志接口（info / warning / error）"""

    def info(self, msg: str) -> None:
        """输出信息"""

    def warning(self, msg: str) -> None:
        """输出警告"""

    def error(self, msg: str) -> None:
        """输出错误"""


class _NullLogger(Logger):
    """静默实现 — 所有方法无操作"""

    def info(self, msg: str) -> None:
        pass

    def warning(self, msg: str) -> None:
        pass

    def error(self, msg: str) -> None:
        pass


_null_logger = _NullLogger()
_logger: Logger = _null_logger


def set_logger(logger: Logger) -> None:
    """设置全局 Logger（调用方在初始化时注入）"""
    global _logger
    _logger = logger


def get_logger() -> Logger:
    """获取当前 Logger（默认静默）"""
    return _logger
