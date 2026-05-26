# display/ — TUI 显示层

将 agent.py 中所有 print() / ANSI 输出逻辑集中于此，
实现逻辑层与显示层的完全解耦。

## 架构

```
agent.py (逻辑层)
    │  通过 self.display.xxx() 调用
    ▼
Display (抽象接口)  ←── interfaces.py
    │
    ├── ConsoleDisplay  ←── console.py (当前实现)
    │
    └── (未来: CursesDisplay, RichDisplay 等)
```

## 接口设计原则 (interfaces.py)

1. **方法即事件** — 每个方法名以 `on_` 开头表示"发生了什么事"
2. **参数最小化** — 只传必要参数，富数据用 dict 封装
3. **所有输出都抽象化** — agent.py 中不再有 print()，全部委托给 display
4. **32 个方法覆盖所有输出场景**

## 目录结构

```
display/
├── __init__.py         # 公开 API: get_display(), Display, ConsoleDisplay
├── interfaces.py       # Display 抽象基类（32 个方法）
├── console.py          # ConsoleDisplay — ANSI print 实现
├── tokens.py           # 样式常量 (Fg, Bg, Style, Icons, Border)
├── widgets.py          # 可复用 UI 组件 (panel, stats_table, progress_bar)
├── events.py           # 可选事件总线（解耦通道）
├── README.md           # 本文件
└── components/         # 旧版组件 (保留供参考)
    ├── __init__.py
    ├── box_panel.py    # BoxPanel 类
    └── streaming.py    # StreamManager 状态机
```

## 迁移计划

### Phase 1 ✅ (已完成)
- 创建 `display/` 目录结构
- 定义 Display 抽象接口 (32 个方法)
- 实现 ConsoleDisplay 覆盖全部接口
- 提取样式常量到 tokens.py
- 提供可复用组件 (widgets.py)

### Phase 2 🔜 (进行中)
- agent.py 中所有 print() 替换为 `self.display.xxx()` 调用
- 依赖注入：`Agent(display=ConsoleDisplay())`
- 移除 agent.py 中的 `_render_markdown()`、ANSI 常量

### Phase 3 (可选)
- EventBus 接入日志/诊断管道
- CursesDisplay 全屏 TUI 实现
- RichDisplay 库实现 (Panel, Table, Markdown)

## 测试

```bash
# 验证模块导入
python3 -c "from display import get_display; d = get_display(); print(type(d).__name__)"

# 验证接口完整性
python3 -c "
from display.interfaces import Display
from display.console import ConsoleDisplay
import inspect
abstracts = [n for n, m in inspect.getmembers(Display) if getattr(m, '__isabstractmethod__', False)]
console = ConsoleDisplay()
for a in abstracts:
    assert hasattr(console, a), f'Missing: {a}'
print(f'✅ ConsoleDisplay 实现了全部 {len(abstracts)} 个抽象方法')
"
```
