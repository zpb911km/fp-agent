"""fp 顶层入口的 -m 双义归一（_normalize_mode_argv）。

背景：顶层 `--mode` 注册了短选项 -m，而 fp_cli 的 -m 是 --message。
不归一则 `fp -m "你好"` 会被 --mode 的 choices 拦成 invalid choice，
文档承诺的单次消息用法根本跑不通。归一规则：
  -m <模式名>        → --mode   （兼容旧用法 fp -m webui）
  -m <其他值>        → --message（透传 fp_cli）
  -m=xxx / -mxxx     → 同上（等号 / 粘连形式）
  -m 裸露或后随选项   → 原样保留（交给 argparse 报缺值，不吞选项）
"""

from fp.main import _normalize_mode_argv


class TestNormalizeModeArgv:
    def test_message_space_form(self):
        assert _normalize_mode_argv(["-m", "hi"]) == ["--message", "hi"]

    def test_mode_space_form(self):
        assert _normalize_mode_argv(["-m", "webui"]) == ["--mode", "webui"]

    def test_mode_equals_form(self):
        assert _normalize_mode_argv(["-m=webui"]) == ["--mode=webui"]
        assert _normalize_mode_argv(["-m=cli"]) == ["--mode=cli"]

    def test_message_equals_form(self):
        assert _normalize_mode_argv(["-m=hi"]) == ["--message=hi"]

    def test_mode_joined_form(self):
        assert _normalize_mode_argv(["-mwebui"]) == ["--mode=webui"]

    def test_message_joined_form(self):
        assert _normalize_mode_argv(["-m你好"]) == ["--message=你好"]

    def test_bare_m_untouched(self):
        # 裸 -m 不能被改写成 --message=（值不存在），留给 argparse 报缺值
        assert _normalize_mode_argv(["-m"]) == ["-m"]
        assert _normalize_mode_argv(["-m", "-r"]) == ["-m", "-r"]

    def test_long_forms_untouched(self):
        assert _normalize_mode_argv(["--mode", "acp"]) == ["--mode", "acp"]
        assert _normalize_mode_argv(["--mode=acp"]) == ["--mode=acp"]
        assert _normalize_mode_argv(["--message", "x"]) == ["--message", "x"]

    def test_mixed_sequence(self):
        assert _normalize_mode_argv(["-m", "acp", "-m", "go"]) == ["--mode", "acp", "--message", "go"]

    def test_passthrough_args_untouched(self):
        argv = ["-r", "s1", "--headless", "--port", "9999", "--model", "p/m"]
        assert _normalize_mode_argv(argv) == argv

    def test_empty_argv(self):
        assert _normalize_mode_argv([]) == []
