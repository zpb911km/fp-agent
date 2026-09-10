"""taskmap.present 呈现层测试

覆盖: Mermaid 形状(随 kind)/状态类/转义/确定性/空图;
异常高亮(目标不可达/陈旧锁/环); 终端树(含重访标记); md 围栏。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from fp_core.taskmap.models import Edge, Node, NodeKind, NodeStatus, TaskMap
from fp_core.taskmap.present import to_markdown, to_mermaid, to_tree


def _map() -> TaskMap:
    """n0 -> n2 -> {n3 -> n4 -> n1, n1}"""
    m = TaskMap.create(1, "修扬声器", "扬声器正常出声")
    m.nodes["n2"] = Node(id="n2", desc="病因已定位", kind=NodeKind.STEP, status=NodeStatus.DONE)
    m.nodes["n3"] = Node(
        id="n3", desc="驱动异常", kind=NodeKind.STEP, status=NodeStatus.ACTIVE, owner="w1", lease_until=2000.0
    )
    m.nodes["n4"] = Node(id="n4", desc="修蓝牙", kind=NodeKind.SIDE_EFFECT, status=NodeStatus.PENDING)
    m.edges = [
        Edge("n0", "n2", "requires_decompose"),
        Edge("n2", "n3", "complete"),
        Edge("n2", "n1", "complete"),
        Edge("n3", "n4", "side_effect"),
        Edge("n4", "n1", "complete"),
    ]
    m.next_nid = 5
    return m


class TestMermaid(unittest.TestCase):
    def test_shapes_by_kind(self):
        s = to_mermaid(_map())
        self.assertIn('m0(("n0 (起点)"))', s)  # START 圆
        self.assertIn('m1(["n1 扬声器正常出声"])', s)  # GOAL 胶囊
        self.assertIn('m2["n2 病因已定位"]', s)  # STEP 方框
        self.assertIn('{{"n4 修蓝牙"}}', s)  # SIDE_EFFECT 六边形

    def test_status_classes(self):
        s = to_mermaid(_map())
        self.assertIn("class m2 done", s)
        self.assertIn("class m3 active", s)
        self.assertIn("class m4 pending", s)

    def test_edge_semantics(self):
        self.assertIn("m3 -->|side_effect| m4", to_mermaid(_map()))

    def test_deterministic(self):
        m = _map()
        self.assertEqual(to_mermaid(m), to_mermaid(m))

    def test_escaping(self):
        m = TaskMap.create(1, "t", "g")
        m.nodes["n0"].desc = 'a"b#c|d'
        s = to_mermaid(m)
        self.assertIn("&quot;", s)
        self.assertIn("&num;", s)
        self.assertIn("&#124;", s)

    def test_empty_map(self):
        self.assertIn("empty", to_mermaid(TaskMap(id=9, title="空")))


class TestAnomalies(unittest.TestCase):
    def test_goal_unreachable(self):
        m = TaskMap.create(1, "t", "g")
        m.nodes["n2"] = Node(id="n2", desc="x")
        m.edges = [Edge("n0", "n2", "requires_decompose")]  # n1 无入边
        self.assertIn("class m1 anomaly", to_mermaid(m))

    def test_stale_lock(self):
        m = TaskMap.create(1, "t", "g")
        m.nodes["n2"] = Node(id="n2", desc="x", status=NodeStatus.ACTIVE, owner="w", lease_until=100.0)
        m.edges = [Edge("n0", "n2", "requires_decompose"), Edge("n2", "n1", "complete")]
        self.assertIn("class m2 anomaly", to_mermaid(m, now=999.0))  # 已过期
        self.assertNotIn("class m2 anomaly", to_mermaid(m, now=50.0))  # 仍有效

    def test_cycle(self):
        m = TaskMap.create(1, "t", "g")
        m.nodes["n2"] = Node(id="n2", desc="a")
        m.nodes["n3"] = Node(id="n3", desc="b")
        m.edges = [
            Edge("n0", "n2", "x"),
            Edge("n2", "n3", "x"),
            Edge("n3", "n2", "x"),
            Edge("n3", "n1", "x"),
        ]
        s = to_mermaid(m)
        self.assertIn("class m2 anomaly", s)
        self.assertIn("class m3 anomaly", s)

    def test_markdown_reports_anomaly(self):
        m = TaskMap.create(1, "t", "g")
        m.edges = []  # 目标不可达
        self.assertIn("异常", to_markdown(m))


class TestTree(unittest.TestCase):
    def test_contains_nodes_and_semantics(self):
        s = to_tree(_map())
        self.assertIn("n0", s)
        self.assertIn("n2", s)
        self.assertIn("[side_effect]", s)

    def test_marks_revisit(self):
        # n2 有第二条出边直达 n1, 而 n1 已沿 n3->n4->n1 展开过
        self.assertIn("↩", to_tree(_map()))

    def test_long_desc_truncated(self):
        m = TaskMap.create(1, "t", "很长的目标" * 30)
        self.assertIn("…", to_tree(m))
        self.assertIn("…", to_mermaid(m))

    def test_unconnected_node_listed(self):
        m = TaskMap.create(1, "t", "g")
        m.nodes["n9"] = Node(id="n9", desc="孤岛")
        self.assertIn("孤岛", to_tree(m))


class TestMarkdown(unittest.TestCase):
    def test_has_fence_and_title(self):
        s = to_markdown(_map())
        self.assertIn("```mermaid", s)
        self.assertIn("### 任务 #1", s)


if __name__ == "__main__":
    unittest.main()
