import os
import glob
import yaml
from pathlib import Path
from typing import Dict, List, Optional

import config


class SkillLoader:
    """技能加载器 - 支持热重载"""
    
    def __init__(self, skills_dir: str = None):
        # 使用 config.SKILLS_DIR 作为默认值，允许外部传入覆盖
        self.skills_dir = Path(skills_dir if skills_dir else config.SKILLS_DIR)
        self.skills: Dict[str, dict] = {}
    
    def load_all(self) -> List[dict]:
        """加载所有技能文件"""
        self.skills.clear()
        
        if not self.skills_dir.exists():
            print(f"⚠️ 技能目录不存在：{self.skills_dir}")
            return []
        
        for md_file in sorted(self.skills_dir.glob("*.md")):
            try:
                skill = self._load_single(md_file)
                if skill:
                    self.skills[skill['name']] = skill
                    print(f"✅ 已加载技能：{skill['title']}")
            except Exception as e:
                print(f"❌ 加载技能 {md_file.name} 失败：{e}")
        
        return list(self.skills.values())
    
    def _load_single(self, file_path: Path) -> Optional[dict]:
        """加载单个技能文件"""
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # 分离 YAML frontmatter 和正文
        parts = content.split('---', 2)
        if len(parts) < 3:
            raise ValueError(f"缺少 YAML frontmatter: {file_path}")
        
        metadata = yaml.safe_load(parts[1])
        body = parts[2].strip()
        
        # 验证必要字段
        required = ['name', 'title', 'description']
        for field in required:
            if field not in metadata:
                raise ValueError(f"缺少必要字段 {field}: {file_path}")
        
        # 补充默认值
        metadata.setdefault('category', 'general')
        metadata.setdefault('version', '1.0')
        metadata.setdefault('priority', 5)
        
        # 保存原始内容（用于提示词注入）
        metadata['raw_content'] = content
        
        return metadata
    
    def get_prompt_fragment(self, skill_name: str) -> str:
        """获取单个技能的提示词片段"""
        skill = self.skills.get(skill_name)
        if not skill:
            return ""
        
        # 只保留正文部分（去掉 YAML frontmatter）
        parts = skill['raw_content'].split('---', 2)
        return parts[2].strip() if len(parts) > 2 else ""
    
    def get_all_prompt_text(self) -> str:
        """获取所有技能的提示词文本（按优先级排序）"""
        sorted_skills = sorted(
            self.skills.values(), 
            key=lambda s: s.get('priority', 5), 
            reverse=True
        )
        
        fragments = []
        for skill in sorted_skills:
            fragment = self.get_prompt_fragment(skill['name'])
            if fragment:
                fragments.append(fragment)
        
        return "\n\n".join(fragments)
    
    def reload(self) -> List[dict]:
        """热重载所有技能"""
        print("🔄 正在重新加载技能...")
        return self.load_all()


# 全局实例
skill_loader = SkillLoader()
