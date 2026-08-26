# AI 水印清理工具 — GUI 设计规格 v1

目标用户：完全不懂命令行的普通人。产品形态：**macOS 原生桌面窗口（Tkinter）**，不是网页。
底层能力：复用本仓库 `service/scripts/` 里的官方 CLI 脚本（`inspect_file.py`、`clean_file.py`），
通过 `subprocess`（用 `sys.executable`）调用——CLI 是文档化的稳定契约，不直接 import 内部模块。

## 目录结构（全部放在仓库 `gui/` 下，不改动上游任何文件）

```
gui/
  core.py        # 纯逻辑层，禁止 import tkinter，可独立测试
  app.py         # Tkinter UI 层，只做界面与线程调度，逻辑全部调 core
  launch.command # 双击启动器（chmod +x）
  WatermarkCleaner.app/   # 最小 .app 包装（Info.plist + MacOS/WatermarkCleaner 脚本）
  tests/         # unittest 测试（测试 agent 负责）
    fixtures 由测试代码在 tmpdir 里动态生成，不提交二进制文件
```

## core.py 公开契约（实现与测试都以此为准，不得偏离）

```python
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT: Path       # gui/ 的父目录
SCRIPTS_DIR: Path     # REPO_ROOT / "service" / "scripts"

@dataclass
class ScanResult:
    path: Path
    kind: str                  # 上游 classify 的格式名，如 "text"/"png"/"pdf"/"unknown"…按上游实际输出
    findings: list[str]        # 人话（中文）描述发现的痕迹，如 "隐形字符 ×3"、"C2PA 内容凭证"、"EXIF/XMP 元数据"
    actionable: bool           # 是否有可清理的痕迹
    error: str | None = None   # 扫描失败时的中文错误说明；成功为 None

@dataclass
class CleanResult:
    path: Path
    output: Path | None        # 成功时为清理后文件路径；失败/跳过为 None
    ok: bool
    message: str               # 中文结果说明，如 "已清理，保存为 xxx_已清理.png"、"跳过：不支持的格式"

def scan_file(path: str | Path) -> ScanResult: ...
def clean_file(path: str | Path, out_dir: str | Path | None = None) -> CleanResult: ...
def default_output_path(path: str | Path, out_dir: str | Path | None = None) -> Path: ...
def tool_hints() -> list[str]: ...
```

规则：
1. **绝不改动原文件**。输出文件名 = `原名_已清理.扩展名`；若已存在则 `原名_已清理_2.扩展名`、`_3`…（`default_output_path` 负责，`clean_file` 使用它）。
2. `out_dir=None` 时输出到原文件所在目录。
3. 不支持/未知格式（上游 exit 2 或 kind=unknown）：`ok=False`，message 用人话说「跳过：无法识别的文件格式」，不写任何输出文件。
4. 扫描/清理内部所有异常都捕获，转成中文 error/message，绝不向上抛 traceback。
5. `tool_hints()`：检查 `qpdf`/`exiftool` 是否在 PATH，缺失时返回中文提示列表（如「未安装 qpdf，PDF 只能做浅层清理」）；齐全返回空列表。
6. findings 的分类翻译：隐形字符/零宽字符→「隐形字符 ×N」，C2PA→「C2PA 内容凭证」，EXIF/XMP/元数据→「EXIF/XMP 元数据」，文档属性→「文档属性」；无法归类的原样附上简短英文。实现前先实际运行上游 `inspect_file.py` 和 `clean_file.py` 摸清其 stdout 格式与 exit code，再写解析。
7. 干净文件：`actionable=False`，findings=[]；对干净文件调用 clean_file 允许成功（message 说明本来就干净或已输出副本，二选一，实现时定死并让测试匹配实现行为之一即可——推荐：不写输出文件，ok=True，message「文件本来就是干净的，无需清理」）。

## app.py UI 规格

- 窗口标题「AI 水印清理工具」，最小尺寸 760×500，中文界面。
- 顶部按钮行：「＋ 添加文件」「＋ 添加文件夹」「一键清理」「打开输出位置」「清空列表」。
- 中部 `ttk.Treeview` 四列：文件名 | 类型 | 检测结果 | 状态。
  - 添加文件后自动后台扫描：状态流转 检测中… → 「发现 N 处痕迹」或「干净 ✓」。
  - 清理后状态 → 「已清理 ✓」（并在检测结果列显示输出文件名）或「失败/跳过 + 原因」。
- 底部状态栏：当前进度 + 固定说明「清理结果另存为“原文件名_已清理”，不会改动原文件」。
  `tool_hints()` 非空时在状态栏上方显示黄色提示条。
- 「添加文件夹」递归收集常见支持格式（md txt html svg png jpg jpeg webp gif tiff bmp pdf docx xlsx pptx epub odt mp4 mov m4a wav mp3 flac），上限 500 个防误选巨型目录。
- 扫描与清理都在单条后台 worker 线程跑，结果经 `queue.Queue` + `root.after(100ms)` 回 UI 线程；UI 永不冻结。
- 「一键清理」只处理 actionable 的行，干净的行标「无需清理」。
- 所有报错都是人话，不出现英文 traceback。
- 供测试用：`app.py` 提供 `create_app()` 返回 `(root, app)`，`python3 gui/app.py` 正常启动主循环。

## 启动器

- `launch.command`：依次探测 `/opt/homebrew/bin/python3`、`/usr/local/bin/python3`、`/usr/bin/python3`、PATH 里的 `python3`，选第一个能 `import tkinter` 的来运行 `app.py`；一个都没有时用 `osascript -e 'display alert'` 弹中文提示（让用户装 `brew install python-tk`），不闪退。
- `WatermarkCleaner.app`：标准最小 bundle，`Contents/Info.plist`（CFBundleName=AI 水印清理工具，CFBundleIdentifier=local.watermarks.gui，CFBundleExecutable=WatermarkCleaner）+ `Contents/MacOS/WatermarkCleaner`（bash，逻辑同 launch.command，用相对自身的路径定位仓库）。两者都要 chmod +x。

## 测试规格（gui/tests/，标准库 unittest，禁止 pytest 依赖）

统一跑法：`cd ~/watermarks-remover && python3 -m unittest discover -s gui/tests -v`

必须覆盖（fixtures 全部在 tempdir 动态生成）：
1. 含零宽字符（U+200B、U+2060、U+FEFF 等混入正文）的 .md：scan 检出 actionable=True；clean 后输出文件不含这些码点、可见文本不变；原文件字节不变（前后 hash 对比）。
2. 干净 .txt：actionable=False；clean_file 行为符合契约第 7 条。
3. 带 tEXt 元数据块的合法小 PNG（手工构造字节：签名+IHDR+tEXt+IDAT+IEND，CRC 正确）：scan 检出；clean 后输出仍是合法 PNG（校验签名与 IHDR）且 tEXt 消失。
4. 无法识别格式（.xyz 随机字节）：scan kind=unknown 或 error；clean ok=False、无输出文件、message 是中文人话。
5. `default_output_path`：后缀规则、同名冲突递增 `_2`/`_3`、out_dir 指定与否。
6. 原文件保护：任何 clean 调用后原文件 hash 不变。
7. GUI 冒烟（tkinter 可用时才跑，否则 skipTest）：`create_app()` 能建窗、五个按钮与 Treeview 存在、`root.update()` 数次不抛异常、destroy 干净退出。
