"""ToolPlane + ToolExecutor.definition_filter 行为测试

对应 #175：插件改写工具 schema 的正规落点。
- 过滤链：注册顺序应用、remove 幂等、get_definitions 出口统一生效
- ToolPlane：api 包面导出 + 未开实例时 InstanceNotOpenError 判据
"""

from __future__ import annotations

import asyncio


def _mk_def(name: str) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": f"工具 {name}",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    }


class _FakeRegistry:
    def __init__(self, names: list[str]) -> None:
        self._defs = [_mk_def(n) for n in names]

    def get_all_definitions(self) -> list[dict]:
        return list(self._defs)

    async def execute(self, tool_name: str, params: dict) -> str:  # pragma: no cover
        return f"ok:{tool_name}"


def test_filter_chain_applied_in_order() -> None:
    from fp_core.core.tool_executor import ToolExecutor

    ex = ToolExecutor(registry=_FakeRegistry(["alpha", "beta", "gamma"]))  # type: ignore[arg-type]

    def drop_alpha(defs: list[dict]) -> list[dict]:
        return [d for d in defs if d["function"]["name"] != "alpha"]

    def rename_beta(defs: list[dict]) -> list[dict]:
        out = []
        for d in defs:
            if d["function"]["name"] == "beta":
                d = dict(d)
                d["function"] = dict(d["function"], name="beta_renamed")
            out.append(d)
        return out

    ex.add_definition_filter(drop_alpha)
    ex.add_definition_filter(rename_beta)
    names = [d["function"]["name"] for d in ex.get_definitions()]
    assert names == ["beta_renamed", "gamma"]

    # remove 幂等：drop_alpha 移除后 alpha 回归；rename_beta 仍在生效
    # （过滤器只作用于当前调用，移除不回滚其他过滤器的改写）
    ex.remove_definition_filter(drop_alpha)
    ex.remove_definition_filter(drop_alpha)  # 二次移除不炸
    names = [d["function"]["name"] for d in ex.get_definitions()]
    assert names == ["alpha", "beta_renamed", "gamma"]


def test_filter_does_not_mutate_registry_source() -> None:
    from fp_core.core.tool_executor import ToolExecutor

    reg = _FakeRegistry(["alpha", "beta"])
    ex = ToolExecutor(registry=reg)  # type: ignore[arg-type]
    ex.add_definition_filter(lambda defs: defs[:1])
    assert len(ex.get_definitions()) == 1
    # 底层注册表未被裁剪
    assert len(reg.get_all_definitions()) == 2


def test_toolplane_exported_and_requires_open() -> None:
    import fp_core.api as api

    assert "ToolPlane" in api.__all__
    assert hasattr(api.portal, "tools")

    from fp_core.api import InstanceNotOpenError, Portal, PortalError

    p = Portal()
    try:
        asyncio.run(p.tools.definitions())
        raise AssertionError("未开实例应抛 InstanceNotOpenError")
    except InstanceNotOpenError:
        pass
    except PortalError:
        pass
