#!/usr/bin/env bash
# ════════════════════════════════════════════════════════
# Five Pebbles Agent — Linux/macOS 安装脚本
# 用法: bash install.sh
# ════════════════════════════════════════════════════════
set -e

# ─── 0. 颜色 ────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

info()  { echo -e "${CYAN}ℹ️${NC}  $1"; }
ok()    { echo -e "${GREEN}✅${NC}  $1"; }
warn()  { echo -e "${YELLOW}⚠️${NC}  $1"; }
err()   { echo -e "${RED}❌${NC}  $1"; }

# ─── 1. 定位项目根目录 ────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo -e "${CYAN}"
echo "  ╭──────────────────────────────────────╮"
echo "  │  🪨  Five Pebbles Agent 安装程序      │"
echo "  ╰──────────────────────────────────────╯"
echo -e "${NC}"
info "项目目录: $SCRIPT_DIR"

# ─── 2. 检查 Python 版本 ─────────────────────────────
info "检查 Python 版本..."
PYTHON=$(command -v python3 || command -v python)
if [ -z "$PYTHON" ]; then
    err "未找到 Python，请先安装 Python ≥ 3.11"
    exit 1
fi

PY_VER=$($PYTHON -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PY_MAJOR=$($PYTHON -c "import sys; print(sys.version_info.major)")
PY_MINOR=$($PYTHON -c "import sys; print(sys.version_info.minor)")

if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 11 ]; }; then
    err "需要 Python ≥ 3.11，当前为 $PY_VER"
    exit 1
fi
ok "Python $PY_VER"

# ─── 3. 创建虚拟环境（可选，仅当不在 venv 中时提示） ──
if [ -z "$VIRTUAL_ENV" ]; then
    warn "当前未激活虚拟环境。建议创建并激活："
    echo "    $PYTHON -m venv .venv"
    echo "    source .venv/bin/activate"
    echo ""
    read -rp "是否自动创建并激活虚拟环境？[Y/n] " YN
    YN="${YN:-Y}"
    if [[ "$YN" =~ ^[Yy] ]]; then
        info "创建虚拟环境..."
        $PYTHON -m venv .venv
        source .venv/bin/activate
        ok "虚拟环境已激活: $(which python)"
    fi
fi

# ─── 4. 安装依赖 + 注册 fp 命令 ─────────────────────
info "安装 Python 依赖..."
$PYTHON -m pip install -e . -q
ok "依赖安装完成，fp 命令已注册"

# 验证 fp 可用
if command -v fp &>/dev/null; then
    ok "fp 命令可用: $(which fp)"
else
    warn "fp 命令未在 PATH 中找到。可能需要重启终端或重新加载 shell"
    warn "或者直接使用: $PYTHON cli.py"
fi

# ─── 5. 创建数据目录结构 ─────────────────────────────
info "创建数据目录..."
mkdir -p data/sessions
mkdir -p data/memory
mkdir -p data/tasks
ok "数据目录已就绪"

# ─── 6. 保存"代码位置"记忆 ───────────────────────────
info "记录代码安装位置..."
MEMO_FILE="data/memory/agent_path.md"
cat > "$MEMO_FILE" <<EOF
---
name: agent_path
description: 我的代码安装位置
type: reference
created: $(date "+%Y-%m-%d %H:%M")
---

$SCRIPT_DIR
EOF
ok "代码位置已记录: $SCRIPT_DIR"

# ─── 7. 检查 config.json ────────────────────────────
if [ -f "config.json" ]; then
    if grep -q "YOUR_API_KEY" config.json 2>/dev/null; then
        warn "config.json 中的 LLM_API_KEY 仍为占位符"
        warn "请编辑 config.json 填入你的 API Key"
        echo ""
        read -rp "现在编辑？[y/N] " EDIT_YN
        if [[ "$EDIT_YN" =~ ^[Yy] ]]; then
            ${EDITOR:-vi} config.json
        fi
    else
        ok "config.json 已配置"
    fi
fi

# ─── 8. 完成 ─────────────────────────────────────────
echo ""
echo -e "${GREEN}╔══════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║         🪨  安装完成！                       ║${NC}"
echo -e "${GREEN}╠══════════════════════════════════════════════╣${NC}"
echo -e "${GREEN}║  运行 fp     启动交互式 CLI                  ║${NC}"
echo -e "${GREEN}║  运行 fp -c  一次性问答                       ║${NC}"
echo -e "${GREEN}║  运行 fp -h  查看帮助                        ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════════╝${NC}"
