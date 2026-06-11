<#
╔════════════════════════════════════════════════════════╗
║  Five Pebbles Agent — Windows 安装脚本                ║
║  用法: powershell -ExecutionPolicy Bypass .\install.ps1
╚════════════════════════════════════════════════════════╝
#>

$ErrorActionPreference = "Stop"
$Host.UI.RawUI.WindowTitle = " Five Pebbles Agent — Installer"

function Write-Info  { Write-Host "ℹ️  " -ForegroundColor Cyan -NoNewline; Write-Host $args }
function Write-Ok    { Write-Host "✅ " -ForegroundColor Green -NoNewline; Write-Host $args }
function Write-Warn  { Write-Host "⚠️  " -ForegroundColor Yellow -NoNewline; Write-Host $args }
function Write-Err   { Write-Host "❌ " -ForegroundColor Red -NoNewline; Write-Host $args }

# ─── 1. 定位项目根目录 ──────────────────────────────
$ScriptDir = (Get-Item $PSScriptRoot).FullName
Set-Location $ScriptDir

Write-Host "╭──────────────────────────────────────╮" -ForegroundColor Cyan
Write-Host "│    Five Pebbles Agent 安装程序      │" -ForegroundColor Cyan
Write-Host "╰──────────────────────────────────────╯" -ForegroundColor Cyan
Write-Info "项目目录: $ScriptDir"

# ─── 2. 检查 Python 版本 ───────────────────────────
Write-Info "检查 Python 版本..."
$Python = "python"

try {
    $PyVer = & $Python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
    $PyMajor = & $Python -c "import sys; print(sys.version_info.major)"
    $PyMinor = & $Python -c "import sys; print(sys.version_info.minor)"
} catch {
    Write-Err "未找到 Python。请先安装 Python ≥ 3.11"
    Write-Info "下载地址: https://www.python.org/downloads/"
    Write-Info "安装时请勾选 'Add Python to PATH'"
    exit 1
}

if ($PyMajor -lt 3 -or ($PyMajor -eq 3 -and $PyMinor -lt 11)) {
    Write-Err "需要 Python ≥ 3.11，当前为 $PyVer"
    exit 1
}
Write-Ok "Python $PyVer"

# ─── 3. 安装依赖 + 注册 fp 命令 ──────────────────
Write-Info "安装 Python 依赖..."
& $Python -m pip install -e . -q
if ($LASTEXITCODE -ne 0) {
    Write-Err "依赖安装失败"
    exit 1
}
Write-Ok "依赖安装完成，fp 命令已注册"

# 验证 fp
$FpPath = & $Python -c "import shutil; print(shutil.which('fp'))" 2>$null
if ($FpPath) {
    Write-Ok "fp 命令可用: $FpPath"
} else {
    Write-Warn "fp 可能不在 PATH 中，请尝试重新打开终端"
    Write-Warn "或直接使用: python cli.py"
}

# ─── 4. 创建数据目录结构 ─────────────────────────
Write-Info "创建数据目录..."
$null = New-Item -ItemType Directory -Force -Path "data\sessions"
$null = New-Item -ItemType Directory -Force -Path "data\memory"
$null = New-Item -ItemType Directory -Force -Path "data\tasks"
Write-Ok "数据目录已就绪"

# ─── 5. 保存"代码位置"记忆 ───────────────────────
Write-Info "记录代码安装位置..."
$DateStr = Get-Date -Format "yyyy-MM-dd HH:mm"
$MemoContent = @"
---
name: agent_path
description: 我的代码安装位置
type: reference
created: $DateStr
---

$ScriptDir
"@

$MemoFile = "data\memory\agent_path.md"
Set-Content -Path $MemoFile -Value $MemoContent -Encoding UTF8
Write-Ok "代码位置已记录: $ScriptDir"

# ─── 6. 检查 config.json ────────────────────────
$ConfigPath = "config.json"
if (Test-Path $ConfigPath) {
    $ConfigContent = Get-Content $ConfigPath -Raw -Encoding UTF8
    if ($ConfigContent -match "YOUR_API_KEY") {
        Write-Warn "config.json 中的 LLM_API_KEY 仍为占位符"
        Write-Warn "请编辑 config.json 填入你的 API Key"

        $EditChoice = Read-Host "是否现在用记事本编辑？[y/N]"
        if ($EditChoice -eq "y" -or $EditChoice -eq "Y") {
            notepad.exe $ConfigPath
        }
    } else {
        Write-Ok "config.json 已配置"
    }
}

# ─── 7. 完成 ──────────────────────────────────────
Write-Host ""
Write-Host "╔══════════════════════════════════════════════╗" -ForegroundColor Green
Write-Host "║           安装完成！                       ║" -ForegroundColor Green
Write-Host "╠══════════════════════════════════════════════╣" -ForegroundColor Green
Write-Host "║  运行 fp     启动交互式 CLI                  ║" -ForegroundColor Green
Write-Host "║  如果 fp 不可用，用 python cli.py 代替       ║" -ForegroundColor Green
Write-Host "╚══════════════════════════════════════════════╝" -ForegroundColor Green
