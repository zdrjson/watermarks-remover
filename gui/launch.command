#!/bin/bash
# AI 水印清理工具 —— 双击启动器
# 依次探测几个常见的 python3，挑第一个自带 tkinter 的来跑 app.py。
# 一个都没有时弹中文提示，绝不闪退。

# Finder 双击启动的进程只有系统默认 PATH，Homebrew 装的 qpdf／exiftool 一律找不到。
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

GUI_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_PY="$GUI_DIR/app.py"

alert() {
    /usr/bin/osascript -e "display alert \"AI 水印清理工具\" message \"$1\" as critical buttons {\"知道了\"} default button 1" >/dev/null 2>&1
}

if [ ! -f "$APP_PY" ]; then
    alert "找不到程序文件 app.py，请确认 gui 文件夹是完整的。"
    exit 1
fi

CANDIDATES=(
    "/opt/homebrew/bin/python3"
    "/usr/local/bin/python3"
    "/usr/bin/python3"
)

# PATH 里的 python3 放最后兜底（可能和上面重复，重复也无所谓）。
PATH_PY="$(command -v python3 2>/dev/null)"
if [ -n "$PATH_PY" ]; then
    CANDIDATES+=("$PATH_PY")
fi

PY=""
for candidate in "${CANDIDATES[@]}"; do
    if [ -x "$candidate" ] && "$candidate" -c "import tkinter" >/dev/null 2>&1; then
        PY="$candidate"
        break
    fi
done

if [ -z "$PY" ]; then
    alert "这台电脑上的 Python 缺少图形界面组件（tkinter），程序无法启动。

请打开「终端」执行下面这行命令后重试：

brew install python-tk

如果还没装 Homebrew，请先访问 brew.sh 按说明安装。"
    exit 1
fi

cd "$GUI_DIR" || exit 1
exec "$PY" "$APP_PY"
