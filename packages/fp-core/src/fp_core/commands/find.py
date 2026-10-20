"""find 命令 — 按**正文内容**搜索历史会话

与 ``/resume list <词>`` 的分工：
- ``/resume list <词>``：只看**元数据**（标题/关键词/sid/cwd），零 IO，秒出
- ``/find <词>``：搜**正文**，能找到关键词没覆盖的任意词，代价是扫全库（约 300ms）

序号与 ``/resume list`` **共用同一个排序池**，所以搜到的序号可直接
``/resume <序号>`` 切换——这是「搜索 → 跳转」一步到位的关键。
"""

from fp_core.core.session_view import render_search
from fp_core.core.state import State

name = "find"
aliases = ["search"]
description = "按正文内容搜索历史会话。多词之间为 AND。用法: /find <词...>，结果可用 /resume <序号> 直接切换"


def execute(state: State, arg: str) -> tuple[bool, str]:
    return (True, render_search(state, arg.split()))
