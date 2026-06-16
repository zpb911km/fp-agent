# 贡献指南

感谢你考虑为 Five Pebbles Agent 贡献代码！这份指南帮助你快速了解如何参与项目。

## 📋 目录

- [行为准则](#行为准则)
- [如何贡献](#如何贡献)
- [开发环境搭建](#开发环境搭建)
- [代码风格](#代码风格)
- [提交规范](#提交规范)
- [PR 流程](#pr-流程)
- [Issue 指南](#issue-指南)
- [测试](#测试)

## 行为准则

本项目采用 [贡献者契约](https://www.contributor-covenant.org/) 行为准则。参与者应：

- 使用友好包容的语言
- 尊重不同的观点和经验
- 优雅地接受建设性批评
- 以社区利益为重

## 如何贡献

### 报告 Bug

1. **先搜索** 是否已有相关 Issue
2. 使用 Bug 报告模板创建 Issue
3. 包含：
   - 清晰的标题和描述
   - 复现步骤（最小化示例）
   - 预期行为和实际行为
   - 环境信息（OS、Python 版本、依赖版本）
   - 日志或截图（如有）

### 提交新功能

1. 先开 Issue 讨论，避免做无用功
2. 说明功能动机和预期用法
3. 维护者确认后再开始编码

### 改进文档

文档永远有改进空间！修正错别字、补充示例、翻译都是很好的贡献。

## 开发环境搭建

```bash
# 1. 克隆仓库
git clone https://github.com/zpb/agent.git
cd agent

# 2. 创建虚拟环境（推荐）
python3 -m venv .venv
source .venv/bin/activate

# 3. 安装依赖
pip install -e ".[dev]"

# 4. 配置 API Key
cp config.json config.json.bak
# 编辑 config.json，将 LLM_API_KEY 替换为你的密钥

# 5. 验证安装
python -c "import agent; print(agent.__version__)"
```

## 代码风格

### Python

- **缩进**: 4 空格（禁止 Tab）
- **行宽**: 100 字符
- **引号**: 双引号
- **类型注解**: 所有函数参数和返回值必须标注类型
- **命名约定**:
  - 类名: `PascalCase`
  - 函数/变量: `snake_case`
  - 常量: `UPPER_SNAKE_CASE`
  - 私有成员: 前缀下划线 `_private_method`

### 文件组织

```
一个新功能/插件至少包含：
├── 功能代码       — 实现逻辑
├── 类型注解       — 完整的类型签名
├── 文档字符串     — 模块/类/函数的三引号文档
└── 测试（可选）   — 单元测试或集成测试
```

### Commit 规范

使用 [Conventional Commits](https://www.conventionalcommits.org/) 格式：

```
<类型>(<作用域>): <描述>

[可选的正文]
[可选的脚注]
```

**类型说明**:

| 类型 | 用途 | 示例 |
|------|------|------|
| `feat` | 新功能 | `feat(tools): 添加文件搜索工具` |
| `fix` | 修复 Bug | `fix(agent): 修复空消息导致的空指针` |
| `refactor` | 重构 | `refactor(core): 提取公共方法` |
| `docs` | 文档 | `docs: 更新 README 安装步骤` |
| `style` | 格式 | `style: 移除多余空格` |
| `test` | 测试 | `test: 添加中断处理单元测试` |
| `chore` | 杂项 | `chore: 更新依赖版本` |

**示例**:

```
feat(plugins): 添加天气查询插件

- 支持城市名自动补全
- 支持摄氏/华氏度切换
- 通过 lifecycle 钩子注入

Closes #42
```

## PR 流程

```
1. Fork 仓库
2. 创建功能分支: git checkout -b feat/xxx
3. 开发并本地测试
4. 提交代码（遵循 commit 规范）
5. 推送到你的 Fork
6. 创建 Pull Request
```

### PR 检查清单

提交 PR 前请确认：

- [ ] 代码通过了语法检查（`python3 -c "import ast; ast.parse(open('xxx.py').read())"`）
- [ ] 代码风格符合项目规范
- [ ] 添加了必要的文档字符串
- [ ] 已更新相关文档（如有 API 变更）
- [ ] 所有现有测试通过
- [ ] Commit 信息遵循规范

### PR 审查

- 至少需要 1 位维护者批准
- 审查者可能要求修改，请保持耐心
- CI 检查必须全部通过

## Issue 指南

### Bug 报告

```markdown
**描述 Bug**
清晰的 Bug 描述

**复现步骤**
1. 运行 `fp`
2. 输入 `/help`
3. 回车后崩溃

**期望行为**
应该显示帮助信息

**环境信息**
- OS: Ubuntu 22.04
- Python: 3.12.0
- Agent 版本: 0.1.5
```

### 功能请求

```markdown
**动机**
为什么这个功能有用？

**方案描述**
你期望的 API 或行为

**备选方案**
其他可能的实现方式
```

## 测试

### 语法检查

```bash
# 检查单个文件
python3 -c "import ast; ast.parse(open('core/agent.py').read()); print('OK')"

# 检查所有 Python 文件
find . -name "*.py" -type f -exec python3 -c "
import ast, sys
for f in sys.argv[1:]:
    try:
        ast.parse(open(f).read())
        print(f'✅ {f}')
    except SyntaxError as e:
        print(f'❌ {f}: {e}')
" {} +
```

### 运行集成测试

```bash
python3 test_interrupt.py
```

### 手动测试

修改代码后，用会话注入快速验证：

```bash
echo "你好" | fp                    # 测试基本对话
echo "运行 ls -la" | fp             # 测试工具调用
echo "你是谁" | fp                   # 测试系统提示词
```

---

**再次感谢你的贡献！🎉**
