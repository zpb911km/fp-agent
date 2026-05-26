import os
import glob
from datetime import datetime
from config import MEMORY_DIR


class Memory:
    """文件型持久化记忆系统，每个记忆存储为一个 markdown 文件（带 YAML frontmatter）。"""

    def __init__(self):
        os.makedirs(MEMORY_DIR, exist_ok=True)

    def save(self, name: str, type_: str, description: str, content: str) -> str:
        """保存一条记忆。"""
        safe_name = name.replace(" ", "_").replace("/", "_")
        path = os.path.join(MEMORY_DIR, f"{safe_name}.md")
        date = datetime.now().strftime("%Y-%m-%d %H:%M")

        with open(path, "w", encoding="utf-8") as f:
            f.write(f"---\nname: {safe_name}\ndescription: {description}\ntype: {type_}\ncreated: {date}\n---\n\n{content}\n")

        return f"记忆已保存: {safe_name} ({type_})"

    def list_memories(self) -> list[dict]:
        """列出所有记忆的元信息。"""
        memories = []
        for path in sorted(glob.glob(os.path.join(MEMORY_DIR, "*.md"))):
            name = os.path.splitext(os.path.basename(path))[0]
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
            description = ""
            type_ = "unknown"
            for line in content.split("\n"):
                if line.startswith("description:"):
                    description = line.split(":", 1)[1].strip()
                elif line.startswith("type:"):
                    type_ = line.split(":", 1)[1].strip()
            memories.append({"name": name, "type": type_, "description": description, "path": path})
        return memories

    def search(self, keyword: str = "") -> list[dict]:
        """搜索记忆（匹配名称和描述）。多个关键词用空格分隔，需全部匹配。"""
        all_mem = self.list_memories()
        if not keyword:
            return all_mem
        keywords = [kw for kw in keyword.lower().split()]
        return [
            m for m in all_mem
            if all(kw in m["name"].lower() or kw in m["description"].lower() for kw in keywords)
        ]

    def load_context(self) -> str:
        """将所有记忆格式化为上下文文本。"""
        memories = self.list_memories()
        if not memories:
            return ""
        parts = []
        for m in memories:
            with open(m["path"], "r", encoding="utf-8") as f:
                # 跳过 frontmatter，只取正文
                lines = f.read().split("\n")
                body_start = 0
                if lines and lines[0].strip() == "---":
                    for i in range(1, len(lines)):
                        if lines[i].strip() == "---":
                            body_start = i + 1
                            break
                body = "\n".join(lines[body_start:]).strip()
                parts.append(f"[{m['name']}] ({m['type']})\n{body}")

        return "\n\n".join(parts)
