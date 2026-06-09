"""
技能加载器
从 skills/ 目录加载技能文件
"""

import os
import re
from typing import Dict, List, Optional
from dataclasses import dataclass, field

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILLS_DIR = os.path.join(PROJECT_ROOT, "skills")

# 模板变量：agent 主入口文件路径（用于自修改等技能）
AGENT_ENTRY = os.path.join(PROJECT_ROOT, "cli.py")


@dataclass
class Skill:
    """技能定义"""
    name: str
    title: str
    description: str
    content: str = ""
    category: str = "general"
    version: str = "1.0"
    priority: int = 5
    metadata: Dict = field(default_factory=dict)


class SkillLoader:
    """技能加载器"""
    
    def __init__(self):
        self.skills: Dict[str, Skill] = {}
    
    def load_all(self) -> Dict[str, Skill]:
        """加载所有技能"""
        self.skills.clear()
        
        if not os.path.isdir(SKILLS_DIR):
            return self.skills
        
        for filename in os.listdir(SKILLS_DIR):
            if not filename.endswith('.md'):
                continue
            
            skill = self._load_skill_file(os.path.join(SKILLS_DIR, filename))
            if skill:
                self.skills[skill.name] = skill
        
        return self.skills
    
    def _load_skill_file(self, filepath: str) -> Optional[Skill]:
        """加载单个技能文件"""
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                content = f.read()
            
            # 模板变量替换
            content = self._substitute_templates(content)
            
            # 解析 YAML frontmatter
            skill = self._parse_skill(content, filepath)
            if skill:
                skill.metadata["file"] = os.path.basename(filepath)
            
            return skill
        except Exception as e:
            print(f"[SkillLoader] Failed to load {filepath}: {e}")
            return None
    
    @staticmethod
    def _substitute_templates(text: str) -> str:
        """替换技能内容中的模板变量"""
        replacements = {
            "{{path_to_agent_file}}": AGENT_ENTRY,
        }
        for placeholder, value in replacements.items():
            text = text.replace(placeholder, value)
        return text
    
    def _parse_skill(self, content: str, filepath: str = "") -> Optional[Skill]:
        """解析技能内容（支持 YAML frontmatter）"""
        # 检查是否有 YAML frontmatter
        match = re.match(r'^---\n(.*?)\n---\n(.*)$', content, re.DOTALL)
        
        if match:
            yaml_content = match.group(1)
            body_content = match.group(2).strip()
            
            # 简单解析 YAML
            metadata = self._parse_yaml(yaml_content)
            
            return Skill(
                name=metadata.get("name", ""),
                title=metadata.get("title", ""),
                description=metadata.get("description", ""),
                content=body_content,
                category=metadata.get("category", "general"),
                version=metadata.get("version", "1.0"),
                priority=int(metadata.get("priority", 5)),
                metadata=metadata
            )
        else:
            # 无 frontmatter，整个内容作为描述
            name = os.path.basename(filepath).replace('.md', '')
            return Skill(
                name=name,
                title=name.replace('_', ' ').title(),
                description=content[:200],
                content=content
            )
    
    def _parse_yaml(self, yaml_text: str) -> Dict:
        """简单的 YAML 解析器"""
        result = {}
        current_key = None
        current_value = []
        in_list = False
        list_items = []
        
        for line in yaml_text.split('\n'):
            # 检查是否是键值对
            key_match = re.match(r'^(\w+):\s*(.*)$', line)
            
            if key_match:
                # 保存前一个键值
                if current_key:
                    if in_list:
                        result[current_key] = list_items
                        in_list = False
                        list_items = []
                    else:
                        val = '\n'.join(current_value).strip()
                        if val:
                            # 去除 YAML 值的外层引号
                            if len(val) >= 2 and val[0] in '"\'' and val[-1] == val[0]:
                                val = val[1:-1]
                            result[current_key] = val
                
                current_key = key_match.group(1)
                rest = key_match.group(2).strip()
                
                if rest.startswith('[') and rest.endswith(']'):
                    # 单行列表
                    items = rest[1:-1].split(',')
                    result[current_key] = [i.strip() for i in items]
                elif rest.startswith('-'):
                    # 列表开始
                    in_list = True
                    list_items = [rest[1:].strip()]
                    current_value = []
                elif rest:
                    current_value = [rest]
                else:
                    current_value = []
            elif in_list and line.strip().startswith('-'):
                list_items.append(line.strip()[1:].strip())
            elif current_value is not None:
                current_value.append(line)
        
        # 保存最后一个
        if current_key:
            if in_list:
                result[current_key] = list_items
            else:
                val = '\n'.join(current_value).strip()
                if val:
                    result[current_key] = val
        
        return result
    
    def get_skill(self, name: str) -> Optional[Skill]:
        """获取指定技能"""
        return self.skills.get(name)
    
    def get_all_prompt_text(self) -> str:
        """获取所有技能的提示词文本"""
        if not self.skills:
            return ""
        
        parts = ["\n\n## 可用技能\n"]
        for skill in sorted(self.skills.values(), key=lambda s: -s.priority):
            parts.append(f"### {skill.title} ({skill.name})\n")
            parts.append(f"{skill.content}\n\n")
        
        return '\n'.join(parts)
    
    def reload(self):
        """热重载所有技能"""
        self.load_all()


# 全局单例
skill_loader = SkillLoader()