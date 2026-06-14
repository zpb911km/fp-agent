# 常见问题 FAQ

---

## Q1: 启动提示 LLM_API_KEY not set，怎么办？

**A：** 系统启动时需要读取 LLM API 密钥，可以通过以下方式设置：

**方式一：修改 config.json**

```json
{
  "llm_api_key": "your-api-key-here"
}
```

**方式二：环境变量**

```bash
export LLM_API_KEY="your-api-key-here"
python3 cli.py
```

环境变量优先级高于配置文件。如果配置文件和环境变量都没有设置，系统会提示错误并退出。

---

## Q2: API 请求超时，可能是什么原因？

**A：** 可能的原因及解决方法：

1. **网络问题**：检查网络连接是否正常，能否访问 API 服务
2. **API 服务负载**：如果服务端负载过高，响应会变慢，可稍后重试
3. **URL 配置错误**：检查 `~/.config/fp/config.json` 中的 `llm_api_url` 是否正确
4. **超时时间过短**：在配置中增大超时时间

```json
{
  "llm_timeout": 120
}
```

默认超时时间为 60 秒，可根据实际网络状况调整。

---

## Q3: Agent 陷入死循环，一直调用工具怎么办？

**A：** 系统内置了 `MAX_ITERATIONS` 防护机制：

- 默认上限为 **50 次迭代**
- 超过后系统自动中断并提示错误
- 你也可以手动中断：按 **Ctrl+C** 终止当前操作

如果频繁触发迭代上限，说明任务可能需要更清晰的指令，或存在逻辑缺陷。尝试将复杂任务分解为多个小步骤执行。

---

## Q4: 上下文超长，Agent 忘记了早期内容怎么办？

**A：** 上下文超长会导致 LLM 注意力分散，忘记早期内容。有以下解决方案：

1. **使用 `/compact` 命令**：压缩当前上下文，保留关键信息，丢弃细节

```
> /compact
🔄 上下文已压缩（从 8500 tokens 减少到 3200 tokens）
```

2. **增大 MAX_CONTEXT_TOKENS**：在配置中调大上下文窗口

```json
{
  "max_context_tokens": 16384
}
```

3. **使用 `/fork` 分叉**：基于当前上下文创建新会话，让上下文从新起点开始

```
> /fork
🔄 会话已分叉，从干净上下文继续对话
```

---

## Q5: 按 Ctrl+C 中断后，程序没有响应怎么办？

**A：** 再次按一次 **Ctrl+C** 可强制退出。如果依旧无响应：

```bash
pkill -f cli.py
```

**注意**：会话在每次交互后已自动保存，强制退出不会丢失对话历史。重新启动后使用 `/resume latest` 即可恢复。

---

## Q6: 修改了技能文件，但没有生效？

**A：** 技能文件修改后需要热重载才能生效。执行以下命令：

```
> /reload_skills
🔄 技能已重新加载（13 个）
```

不需要重启程序。如果重载后仍然有问题，检查技能文件格式是否正确（YAML frontmatter 是否完整）。

---

## Q7: 会话文件损坏了，怎么修复？

**A：** 会话文件的格式是 JSONL（每行一个 JSON 对象）。修复步骤：

1. 检查文件结构

```bash
head -1 data/sessions/s_260610_153022123456.jsonl | python3 -m json.tool
```

2. 如果第 1 行（meta 行）JSON 格式错误，手动编辑修复
3. 如果后续行损坏，可删除损坏的行，保留完整的行
4. 修复后更新 meta 行中的 `message_count` 字段，使其与实际消息数一致

```bash
# 统计实际消息行数
wc -l data/sessions/s_260610_153022123456.jsonl
```

**最小修复示例**：如果只有 meta 行被损坏，可以直接用以下格式替换第 1 行：

```json
{"id": "s_260610_153022123456", "created": "2026-06-10T15:30:22", "updated": "2026-06-10T15:30:22", "summary": "手动修复", "message_count": 0}
```

---

## Q8: 提示文件不存在（FileNotFoundError）？

**A：** 使用**绝对路径**可以避免大部分文件不存在的问题。

```python
# 错误：相对路径可能导致找不到文件
read_file("config.json")

# 正确：使用绝对路径
read_file("/media/zpb/data/codes/AI/agent/config.json")
```

如果仍然提示不存在，先确认文件是否在预期位置：

```bash
ls -lh /media/zpb/data/codes/AI/agent/config.json
```

---

## Q9: Tab 补全没有反应？

**A：** 检查以下几点：

1. **命令前缀**：Tab 补全仅在输入以 `/` 开头的命令时触发。普通对话内容不会有补全
2. **依赖安装**：确认已安装 `prompt_toolkit`

```bash
pip install prompt_toolkit
```

3. **版本兼容**：要求 prompt_toolkit >= 3.0.0

```bash
pip install --upgrade prompt_toolkit
```

如果以上都正常，尝试重启程序。

---

## Q10: 如何迁移和备份数据？

**A：** 系统数据分布在以下三个位置，全部复制即可完成迁移：

| 数据 | 路径 | 说明 |
|------|------|------|
| 会话记录 | `~/.local/share/fp/sessions/` | 所有历史对话 |
| 长期记忆 | `~/.local/share/fp/memory/` | 跨会话持久化记忆 |
| 任务状态 | `~/.local/share/fp/tasks.json` | 任务管理系统状态 |

**一键备份**

```bash
backup_dir="backup_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$backup_dir/data"
cp -r data/sessions "$backup_dir/data/"
cp -r data/memory "$backup_dir/data/"
cp data/tasks.json "$backup_dir/data/"
echo "备份完成：$backup_dir"
```

**迁移到新环境**

将备份目录中的 `data/` 复制到新环境的根目录即可。
