"""Golden set — 记忆检索评测集（合成内容，零真实记忆，可进仓库）

契约（设计 §6 / P0）：
- 全部 fixture 为虚构合成记忆，与真实记忆**同构不同源**（结构陷阱照搬，内容零隐私）
- 自动化部分测**机制**（与语义无关的机械召回）：
    keyword / wrong_drawer / 2char → recall@1：memory_read(query=) 必须命中
    全部 case → recall@scan：memory_read(all=true) 必须暴露预期条目（逃生梯第 3 档兜底）
    synonym / zero_overlap → 词法结构性救不了：query 档**必须 miss**，
        但返回值必须含逃生舱文案（"不许自己咽下"契约），且 scan 档必须能暴露
- 语义档（问题→条目的 LLM 判选）无法在无 LLM 环境自动断言，
    以 case 的 semantic=True 标记保留为人工/LLM 复核清单（golden_semantic_cases）
- 零词重叠 case 的 query 档召回率 = "何时加 embedding" 的第一裁决指标（设计 §5 触发线）

运行: pytest tests/test_memory_golden.py -v
"""

import pytest

from fp_core import config
from fp_core.plugins.memory import read as mem_read

# ─────────────────────────────────────────────────────────────
# 合成 fixture（category / 文件名 / 内容全部虚构）
# 结构：category 是文件所在子目录；wrong_drawer 故意放错抽屉
# ─────────────────────────────────────────────────────────────
FIXTURES = [
    # (case_id, category_on_disk, filename, description, body)
    (
        "kw1",
        "skill",
        "golden_recipe",
        "视频渲染管线的挂载配方，含缓冲区参数",
        "使用 hyperframes 渲染时，先构建 scene 再挂载节点。",
    ),
    (
        "kw2",
        "reference",
        "golden_wifi",
        "无线网卡驱动权限导致断网的处置记录",
        "nmcli device reapply 之后需要重启 NetworkManager。",
    ),
    ("kw3", "project", "golden_alpha", "甲项目的进度归档，含里程碑日期", "甲项目三月完成初版，六月交付。"),
    (
        "sib1",
        "project",
        "golden_alphabeta",
        "乙项目（与甲项目高度相似的兄弟条目，结构陷阱）",
        "乙项目四月启动，七月交付，与甲项目仅一字之差。",
    ),
    (
        "drawer1",
        "reference",
        "golden_decoy_drawer",
        "本应属于 skill 类的渲染配方（故意放错抽屉）",
        "hyperframes 的另一套挂载参数与 golden_recipe 互补。",
    ),
    (
        "cjk2a",
        "reference",
        "golden_restart_note",
        "进程重启的两字短查询样本：重启流程",
        "重启服务前先检查锁文件是否存在。",
    ),
    (
        "syn1",
        "reference",
        "golden_latency",
        "系统响应延迟的排查记录，含耗时基准",
        "定位到响应延迟主要来自磁盘 IO 等待。",
    ),
    (
        "syn2",
        "skill",
        "golden_lagfix",
        "卡顿问题的处置手法（与 golden_latency 零词重叠的改述样本）",
        "遇到卡顿时先看 IO wait 再看线程池。",
    ),
    (
        "zer1",
        "reference",
        "golden_parrot",
        "鹦鹉饲料配比的观测记录（与问题用词完全无交集）",
        "鹦鹉在春季需要增加维生素剂量。",
    ),
]

# query 档必须命中的 case（词法可达）
HIT_AT_QUERY = {"kw1", "kw2", "kw3", "sib1", "drawer1", "cjk2a"}
# query 档必须 miss（词法结构性救不了），但 scan 档必须暴露
MISS_AT_QUERY = {"syn1", "syn2", "zer1"}
# 语义判选 case（无 LLM 不可自动断言，供人工复核清单导出）
SEMANTIC_CASES = {"syn1", "syn2", "zer1"}

# 问题 → 预期条目（synonym/zero 的问题刻意避开答案用词）
QUESTIONS = {
    "kw1": ("hyperframes 视频怎么挂载", "golden_recipe"),
    "kw2": ("无线断网怎么处理", "golden_wifi"),
    "kw3": ("甲项目进度到哪了", "golden_alpha"),
    "sib1": ("乙项目进度", "golden_alphabeta"),
    "drawer1": ("hyperframes 还有哪些挂载参数", "golden_decoy_drawer"),
    "cjk2a": ("重启流程有什么注意的", "golden_restart_note"),
    "syn1": ("反应慢怎么查", "golden_latency"),  # 同义改述：查询词与条目用词零重叠
    "syn2": ("很卡的时候怎么办", "golden_lagfix"),  # 同义改述：卡顿↔响应延迟互不相见
    "zer1": ("那位飞禽的口粮如何搭配", "golden_parrot"),  # 零词重叠：飞禽↔鹦鹉 口粮↔饲料
}


@pytest.fixture
def golden_env(tmp_path, monkeypatch):
    """合成 golden 记忆树（树布局），与真实记忆完全隔离"""
    data_dir = tmp_path / "fpdata"
    monkeypatch.setattr(config, "_FP_DATA_DIR", str(data_dir))
    monkeypatch.chdir(tmp_path)
    private_mem = data_dir / "private" / "memory"
    for _case_id, category, fname, desc, body in FIXTURES:
        cat_dir = private_mem / category
        cat_dir.mkdir(parents=True, exist_ok=True)
        with open(cat_dir / f"{fname}.md", "w", encoding="utf-8") as f:
            f.write("---\n")
            f.write(f"name: {fname}\n")
            f.write(f"description: {desc}\n")
            f.write(f"type: {category}\n")
            f.write("created: 2024-01-01 10:00\n")
            f.write("---\n\n")
            f.write(body + "\n")
    return str(private_mem)


def _read(**params) -> str:
    import asyncio

    return asyncio.run(mem_read.execute(params or {}))


def test_recall_at_query(golden_env):
    """Rung-1：词法可达 case 必须被 query 档命中（含错抽屉与两字短查询）"""
    failures = []
    for case_id in sorted(HIT_AT_QUERY):
        q, expected = QUESTIONS[case_id]
        out = _read(query=q)
        if expected not in out:
            failures.append(f"{case_id}({q}) → 未命中 {expected}")
    assert not failures, "recall@1 失败:\n" + "\n".join(failures)


def test_structural_miss_carries_escape_hatch(golden_env):
    """词法结构性救不了的 case：query 档必须 miss，且返回值必须带逃生舱文案"""
    for case_id in sorted(MISS_AT_QUERY):
        q, expected = QUESTIONS[case_id]
        out = _read(query=q)
        assert expected not in out, f"{case_id} 意外命中（case 失效，更新预期）"
        # "不许自己咽下"契约：miss 必须给出升级指引
        assert any(marker in out for marker in ("逃生", "scan", "all=true", "下钻", "升级", "退出路")), (
            f"{case_id} miss 返回值缺少逃生舱文案"
        )


def test_scan_exposes_all(golden_env):
    """Rung-3：全量一行摘要扫描必须暴露全部 9 条（含错抽屉——抽屉错了也扫得到）"""
    out = _read(all=True)
    missing = [fname for _, _, fname, _, _ in FIXTURES if fname not in out]
    assert not missing, f"scan 档漏掉: {missing}"


def test_wrong_drawer_found_via_query(golden_env):
    """错抽屉专项：FTS 是 drawer-agnostic 的，放错抽屉不挡词法召回"""
    out = _read(query="hyperframes 挂载参数")
    assert "golden_decoy_drawer" in out


def test_zero_overlap_metric_exported(golden_env):
    """§5 触发线指标：零词重叠 query 档召回率落盘为可观测数字（当前预期 0/1）"""
    hits = 0
    for case_id in MISS_AT_QUERY:
        q, expected = QUESTIONS[case_id]
        out = _read(query=q)
        hits += expected in out
    rate = hits / len(MISS_AT_QUERY)
    # 加 embedding 的触发线在这里取值；当前词法基线下必须为 0（非 0 说明 case 或实现漂移）
    assert rate == 0.0, f"零重叠召回率漂移: {rate}（实现或 case 已变，需复核）"


def test_semantic_case_inventory_stable(golden_env):
    """语义档复核清单稳定（无 LLM 环境仅做清单导出，不判对错）"""
    manual = {c for c in SEMANTIC_CASES if QUESTIONS[c][1] not in _read(query=QUESTIONS[c][0])}
    assert manual == SEMANTIC_CASES, f"语义清单漂移: {manual ^ SEMANTIC_CASES}"
