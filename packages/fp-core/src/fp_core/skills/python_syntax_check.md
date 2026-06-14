---
name: "python_syntax_check"
title: "Python 语法检查"
description: "使用 Bash 调用 Python 的 ast 模块检查 Python 文件的语法正确性。"
category: "development"
version: "1.0"
priority: 3
---

[技能] Python 语法检查
使用 Bash 调用 Python 的 ast 模块检查 Python 文件的语法正确性。
命令示例：
- python3 -c "import ast; ast.parse(open('xxx.py').read()); print('OK')" xxx.py  # 检查语法
- python3 -m py_compile xxx.py                   # 编译检查（生成 .pyc）
- python3 -c "import ast, sys; [print(f'{l}:{c} {t.msg}') for l,t in enumerate(ast.walk(ast.parse(open(sys.argv[1]).read()))), c in range(1)]" xxx.py  # 详细错误定位
