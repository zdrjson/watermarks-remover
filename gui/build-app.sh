#!/bin/bash
# AI 水印清理工具 —— 构建并安装「原生版」.app
#
# 和仓库里那份 bash 版 WatermarkCleaner.app 的唯一区别：主进程是本仓库
# gui/native/stub.c 编译出来的存根，而不是 Homebrew 的 Python.app，
# 所以菜单栏显示的是「AI 水印清理工具」而不是「Python」。
#
# 用法：bash gui/build-app.sh
# 产物：/Applications/AI 水印清理工具.app（构建中间产物全在 mktemp 目录里，不留在仓库）

set -euo pipefail

APP_NAME="AI 水印清理工具"
BUNDLE_ID="local.watermarks.gui"
EXEC_NAME="WatermarkCleaner"
TARGET="/Applications/${APP_NAME}.app"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STUB_SRC="$HERE/native/stub.c"
BOOTSTRAP_SRC="$HERE/native/bootstrap.py"

die() {
    echo "构建失败：$1" >&2
    exit 1
}

# --- 1. 依赖检查 -----------------------------------------------------------

[ -f "$STUB_SRC" ] || die "找不到 $STUB_SRC"
[ -f "$BOOTSTRAP_SRC" ] || die "找不到 $BOOTSTRAP_SRC"

command -v clang >/dev/null 2>&1 || die \
    "这台电脑上没有 clang，无法编译原生启动存根。请在「终端」执行 xcode-select --install 装好命令行工具后重试。"

command -v brew >/dev/null 2>&1 || die \
    "没有找到 Homebrew。请先访问 brew.sh 安装 Homebrew，再执行 brew install python@3.14 python-tk。"

PY_PREFIX="$(brew --prefix python@3.14 2>/dev/null || true)"
[ -n "$PY_PREFIX" ] || die "没有找到 Homebrew 的 python@3.14。请在「终端」执行 brew install python@3.14 python-tk 后重试。"

PY_HOME="$PY_PREFIX/Frameworks/Python.framework/Versions/3.14"
PY_DYLIB="$PY_HOME/lib/libpython3.14.dylib"
[ -d "$PY_HOME" ] || die "Python framework 目录不存在：$PY_HOME"
[ -f "$PY_DYLIB" ] || die "找不到 Python 运行库：$PY_DYLIB"

"$PY_HOME/bin/python3.14" -c "import tkinter" >/dev/null 2>&1 || die \
    "Homebrew 的 python@3.14 缺少图形界面组件（tkinter）。请在「终端」执行 brew install python-tk 后重试。"

ARCH="$(uname -m)"

# --- 2. 在临时目录里组装 bundle --------------------------------------------

WORK="$(mktemp -d "${TMPDIR:-/tmp}/watermark-gui-build.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

STAGE="$WORK/${APP_NAME}.app"
mkdir -p "$STAGE/Contents/MacOS" "$STAGE/Contents/Resources"

echo "编译启动存根（arch=$ARCH）…"
clang -O2 -Wall -arch "$ARCH" \
    -DPY_HOME="\"$PY_HOME\"" \
    -DPY_DYLIB="\"$PY_DYLIB\"" \
    -o "$STAGE/Contents/MacOS/$EXEC_NAME" \
    "$STUB_SRC" || die "clang 编译 stub.c 失败。"

chmod +x "$STAGE/Contents/MacOS/$EXEC_NAME"
cp "$BOOTSTRAP_SRC" "$STAGE/Contents/Resources/bootstrap.py"

cat > "$STAGE/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>CFBundleName</key>
	<string>${APP_NAME}</string>
	<key>CFBundleDisplayName</key>
	<string>${APP_NAME}</string>
	<key>CFBundleIdentifier</key>
	<string>${BUNDLE_ID}</string>
	<key>CFBundleExecutable</key>
	<string>${EXEC_NAME}</string>
	<key>CFBundlePackageType</key>
	<string>APPL</string>
	<key>CFBundleInfoDictionaryVersion</key>
	<string>6.0</string>
	<key>CFBundleShortVersionString</key>
	<string>1.0</string>
	<key>CFBundleVersion</key>
	<string>1</string>
	<key>CFBundleDevelopmentRegion</key>
	<string>zh_CN</string>
	<key>LSMinimumSystemVersion</key>
	<string>12.0</string>
	<key>NSHighResolutionCapable</key>
	<true/>
	<key>LSApplicationCategoryType</key>
	<string>public.app-category.utilities</string>
</dict>
</plist>
PLIST

# --- 3. 安装到 /Applications ----------------------------------------------

echo "安装到 $TARGET …"
rm -rf "$TARGET"
mv "$STAGE" "$TARGET"
# 让 LaunchServices 立刻看见新 bundle（Info.plist 换了名字时尤其需要）。
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister \
    -f "$TARGET" >/dev/null 2>&1 || true
touch "$TARGET"

echo "完成：$TARGET"
echo "双击启动即可；菜单栏应用名为「${APP_NAME}」。"
