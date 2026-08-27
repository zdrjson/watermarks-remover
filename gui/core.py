#!/usr/bin/env python3
"""AI 水印清理工具 —— 纯逻辑层。

本模块只负责调用仓库上游的官方 CLI 脚本（``service/scripts/inspect_file.py``
与 ``service/scripts/clean_file.py``），把它们的 JSON 输出翻译成中文人话结果。

约束：
- 禁止 import tkinter，本模块必须可以脱离界面单独测试。
- 只通过 ``subprocess`` + ``sys.executable`` 调用上游 CLI，不直接 import 上游内部模块。
- 绝不改动原文件：清理一律写到新的 ``原名_已清理.扩展名``。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# 路径常量
# ---------------------------------------------------------------------------

#: gui/ 的父目录，也就是仓库根目录。
REPO_ROOT: Path = Path(__file__).resolve().parent.parent
#: 上游 CLI 脚本所在目录。
SCRIPTS_DIR: Path = REPO_ROOT / "service" / "scripts"

INSPECT_SCRIPT: Path = SCRIPTS_DIR / "inspect_file.py"
CLEAN_SCRIPT: Path = SCRIPTS_DIR / "clean_file.py"

#: 输出文件名后缀（原名 + 该后缀 + 原扩展名）。
CLEANED_SUFFIX = "_已清理"

#: 子进程超时（秒）。扫描比清理快，给的额度更小。
SCAN_TIMEOUT = 180
CLEAN_TIMEOUT = 900

#: Homebrew 等常见的第三方命令目录。
#:
#: Finder 双击启动时进程只继承系统默认 PATH（``/usr/bin:/bin:/usr/sbin:/sbin``），
#: 装在 Homebrew 里的 ``qpdf``／``exiftool`` 一律找不到——界面会误报「未安装」，
#: 上游脚本也会静默降级成浅层清理。这里显式补上这些目录。
EXTRA_TOOL_DIRS: tuple[str, ...] = ("/opt/homebrew/bin", "/usr/local/bin")


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass
class ScanResult:
    """一次扫描的结果。"""

    path: Path
    kind: str  # 上游给出的格式名，如 "text"/"png"/"markdown"/"pdf"/"unknown"
    findings: list[str]  # 中文人话的痕迹描述
    actionable: bool  # 是否有可清理的痕迹
    error: str | None = None  # 扫描失败时的中文说明；成功为 None


@dataclass
class CleanResult:
    """一次清理的结果。"""

    path: Path
    output: Path | None  # 成功时为清理后文件路径；失败／跳过为 None
    ok: bool
    message: str  # 中文结果说明


# ---------------------------------------------------------------------------
# 中文文案常量（保持稳定，界面与测试都依赖它们）
# ---------------------------------------------------------------------------

MSG_UNSUPPORTED = "跳过：无法识别的文件格式"
MSG_ALREADY_CLEAN = "文件本来就是干净的，无需清理"
MSG_NOT_FOUND = "跳过：文件不存在"
MSG_NOT_A_FILE = "跳过：这不是一个文件"
MSG_TOO_LARGE = "跳过：文件过大，超出工具的处理上限"

LABEL_INVISIBLE = "隐形字符"
LABEL_C2PA = "C2PA 内容凭证"
LABEL_METADATA = "EXIF/XMP 元数据"
LABEL_DOCPROPS = "文档属性"


# ---------------------------------------------------------------------------
# PATH 增广（内部实现细节，公开契约不变）
# ---------------------------------------------------------------------------


def _augmented_path(base: str | None = None) -> str:
    """把 ``EXTRA_TOOL_DIRS`` 前置到 PATH，去重后返回。"""
    current = os.environ.get("PATH", "") if base is None else base
    parts: list[str] = []
    for item in (*EXTRA_TOOL_DIRS, *current.split(os.pathsep)):
        if item and item not in parts:
            parts.append(item)
    return os.pathsep.join(parts)


def _augmented_env() -> dict[str, str]:
    """给子进程用的环境：PATH 前置 Homebrew 目录，输出强制 UTF-8。

    上游 ``clean_file.py`` 内部同样靠 PATH 找 ``qpdf``／``exiftool``，
    不补 PATH 的话 PDF 与残余元数据清理会静默降级。
    """
    env = dict(os.environ)
    env["PATH"] = _augmented_path(env.get("PATH", ""))
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _which(tool: str) -> str | None:
    """先按 PATH 找，再显式翻 ``EXTRA_TOOL_DIRS``；都没有返回 None。"""
    found = shutil.which(tool)
    if found:
        return found
    for directory in EXTRA_TOOL_DIRS:
        candidate = Path(directory) / tool
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


# ---------------------------------------------------------------------------
# 子进程调用
# ---------------------------------------------------------------------------


def _run(cmd: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    """跑一个子进程，永远以 UTF-8 文本模式拿回 stdout/stderr。"""
    env = _augmented_env()
    return subprocess.run(  # noqa: S603 - 命令由本模块拼装，不含用户输入的可执行文件
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        cwd=str(REPO_ROOT),
        env=env,
    )


def _parse_json(stdout: str) -> dict | None:
    """从 stdout 里抠出 JSON 对象；抠不出来返回 None。"""
    text = (stdout or "").strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            data = json.loads(text[start : end + 1])
        except (ValueError, TypeError):
            return None
    return data if isinstance(data, dict) else None


def _stderr_tail(stderr: str, limit: int = 200) -> str:
    """把 stderr 压成一行短摘要，供拼进中文错误说明。"""
    lines = [ln.strip() for ln in (stderr or "").splitlines() if ln.strip()]
    if not lines:
        return ""
    tail = lines[-1]
    return tail[:limit]


def _is_unsupported_stderr(stderr: str) -> bool:
    """上游用来表示「这堆字节我认不出来」的几种说法。"""
    low = (stderr or "").lower()
    return (
        "unrecognized format" in low
        or "looks like binary data" in low
        or "refusing to classify" in low
    )


# ---------------------------------------------------------------------------
# findings 翻译
# ---------------------------------------------------------------------------

# 纯提示性、不代表痕迹的 finding，直接丢掉。
_NOISE_PATTERNS = (
    re.compile(r"^\s*no\b", re.IGNORECASE),
    re.compile(r"has no metadata", re.IGNORECASE),
    re.compile(r"best-effort", re.IGNORECASE),
    re.compile(r"available for inspect", re.IGNORECASE),
    re.compile(r"already clean", re.IGNORECASE),
)

_RE_C2PA = re.compile(r"c2pa|jumbf|content.?credential|内容凭证", re.IGNORECASE)
_RE_DOCPROPS = re.compile(
    r"docprops|core\.xml|app\.xml|meta\.xml|coreproperties|frontmatter"
    r"|custom\s?xml|customxml|pdf-structured|document\s+propert|/info\b|dc:creator",
    re.IGNORECASE,
)
# PNG/JPEG 等容器里的元数据块名是大小写敏感的，单独列一条。
_RE_CHUNKS = re.compile(r"tEXt|iTXt|zTXt|eXIf")
_RE_METADATA = re.compile(
    r"exif|xmp|iptc|rdf|id3|udta|moov|list info|comment|metadata|generator"
    r"|marker:|\bai:|app\d|tiff tag|json-ld|data:image|trailing metadata|\bmeta\b",
    re.IGNORECASE,
)


def _translate_findings(payload: dict) -> list[str]:
    """把上游英文 findings 翻成中文人话，去重并保持顺序。"""
    out: list[str] = []

    def add(item: str) -> None:
        if item and item not in out:
            out.append(item)

    # 1) 隐形字符（Layer A）：文本走 hits，容器走 layer_a_hits。
    invisible = _invisible_count(payload)
    if invisible:
        add(f"{LABEL_INVISIBLE} ×{invisible}")

    # 2) 其余 findings 逐条归类。
    raw = payload.get("findings")
    findings = [str(f) for f in raw] if isinstance(raw, list) else []
    for finding in findings:
        text = finding.strip()
        if not text:
            continue
        if text.lower().startswith("layer-a:"):
            continue  # 已经并进「隐形字符 ×N」
        if any(p.search(text) for p in _NOISE_PATTERNS):
            continue
        if _RE_C2PA.search(text):
            add(LABEL_C2PA)
        elif _RE_DOCPROPS.search(text):
            add(LABEL_DOCPROPS)
        elif _RE_CHUNKS.search(text) or _RE_METADATA.search(text):
            add(LABEL_METADATA)
        else:
            add(f"其他痕迹：{text[:80]}")

    # 3) 上游只给了布尔标记、findings 为空时的兜底。
    if not out:
        if payload.get("has_c2pa"):
            add(LABEL_C2PA)
        if payload.get("has_ai_metadata"):
            add(LABEL_METADATA)
    return out


def _invisible_count(payload: dict) -> int:
    """统计隐形字符个数。"""
    total = payload.get("suspicious_total")
    if isinstance(total, int) and total > 0:
        return total
    count = 0
    for key in ("hits", "layer_a_hits"):
        hits = payload.get(key)
        if isinstance(hits, list):
            for hit in hits:
                if isinstance(hit, dict):
                    n = hit.get("count")
                    count += n if isinstance(n, int) else 1
    return count


def _kind_of(payload: dict) -> str:
    """取最具体的格式名：容器／图片／音视频用 format，文本与未知用 kind。"""
    kind = str(payload.get("kind") or "unknown")
    fmt = payload.get("format")
    if kind in ("image", "container", "av") and isinstance(fmt, str) and fmt:
        return fmt
    return kind


def _is_actionable(payload: dict) -> bool:
    """复刻上游 inspect_file.py 的「有痕迹」判定。"""
    kind = str(payload.get("kind") or "unknown")
    if kind == "unknown":
        return False
    if kind == "text":
        return bool(_invisible_count(payload))
    if kind in ("image", "av"):
        return bool(payload.get("has_c2pa") or payload.get("has_ai_metadata"))
    # container：元数据或正文里的隐形字符，任一命中都算。
    return bool(
        payload.get("has_c2pa")
        or payload.get("has_ai_metadata")
        or _invisible_count(payload)
    )


# ---------------------------------------------------------------------------
# 公开接口
# ---------------------------------------------------------------------------


def default_output_path(path: str | Path, out_dir: str | Path | None = None) -> Path:
    """算出清理结果该写到哪里。

    规则：``原名_已清理.扩展名``；若已存在则依次尝试 ``_2``、``_3``……
    ``out_dir`` 为 None 时输出到原文件所在目录。
    """
    src = Path(path).expanduser()
    parent = Path(out_dir).expanduser() if out_dir is not None else src.parent
    stem = src.stem
    suffix = src.suffix
    candidate = parent / f"{stem}{CLEANED_SUFFIX}{suffix}"
    index = 2
    while candidate.exists():
        candidate = parent / f"{stem}{CLEANED_SUFFIX}_{index}{suffix}"
        index += 1
    return candidate


def tool_hints() -> list[str]:
    """检查外部依赖，缺什么就给一条中文提示；都齐全返回空列表。

    除 PATH 外还显式探测 ``EXTRA_TOOL_DIRS``——Finder 双击启动的进程
    PATH 里没有 Homebrew 目录，只查 PATH 会把装好的工具误报成「未安装」。
    """
    hints: list[str] = []
    if _which("qpdf") is None:
        hints.append("未安装 qpdf，PDF 只能做浅层清理。终端执行：brew install qpdf")
    if _which("exiftool") is None:
        hints.append(
            "未安装 exiftool，图片与音视频的元数据可能清不干净。终端执行：brew install exiftool"
        )
    return hints


def scan_file(path: str | Path) -> ScanResult:
    """扫描一个文件，返回中文化的检测结果。任何异常都转成 error，不向上抛。"""
    src = Path(path).expanduser()
    try:
        if not src.exists():
            return ScanResult(src, "unknown", [], False, "文件不存在")
        if not src.is_file():
            return ScanResult(src, "unknown", [], False, "这不是一个文件")
        if not INSPECT_SCRIPT.is_file():
            return ScanResult(
                src, "unknown", [], False, f"找不到上游脚本：{INSPECT_SCRIPT}"
            )

        cmd = [sys.executable, str(INSPECT_SCRIPT), "--json", str(src)]
        try:
            proc = _run(cmd, SCAN_TIMEOUT)
        except subprocess.TimeoutExpired:
            return ScanResult(src, "unknown", [], False, "检测超时，文件可能太大")
        except OSError as exc:
            return ScanResult(src, "unknown", [], False, f"无法启动检测程序：{exc}")

        payload = _parse_json(proc.stdout)
        if payload is None:
            stderr = proc.stderr or ""
            if _is_unsupported_stderr(stderr):
                return ScanResult(src, "unknown", [], False, None)
            if "refusing input larger than" in stderr.lower():
                return ScanResult(src, "unknown", [], False, "文件过大，超出工具的处理上限")
            if "not a file" in stderr.lower():
                return ScanResult(src, "unknown", [], False, "文件不存在或不是普通文件")
            tail = _stderr_tail(stderr)
            detail = f"（{tail}）" if tail else ""
            return ScanResult(src, "unknown", [], False, f"检测失败{detail}")

        kind = _kind_of(payload)
        if str(payload.get("kind") or "") == "unknown":
            return ScanResult(src, "unknown", [], False, None)

        actionable = _is_actionable(payload)
        findings = _translate_findings(payload) if actionable else []
        return ScanResult(src, kind, findings, actionable, None)
    except Exception as exc:  # noqa: BLE001 - 契约要求：绝不向上抛 traceback
        return ScanResult(src, "unknown", [], False, f"检测出错：{exc}")


def clean_file(path: str | Path, out_dir: str | Path | None = None) -> CleanResult:
    """清理一个文件，结果另存为新文件；原文件永远不动。"""
    src = Path(path).expanduser()
    dest: Path | None = None
    try:
        if not src.exists():
            return CleanResult(src, None, False, MSG_NOT_FOUND)
        if not src.is_file():
            return CleanResult(src, None, False, MSG_NOT_A_FILE)
        if not CLEAN_SCRIPT.is_file():
            return CleanResult(src, None, False, f"失败：找不到上游脚本 {CLEAN_SCRIPT}")

        scan = scan_file(src)
        if scan.error:
            return CleanResult(src, None, False, f"失败：{scan.error}")
        if scan.kind == "unknown":
            return CleanResult(src, None, False, MSG_UNSUPPORTED)
        if not scan.actionable:
            # 契约第 7 条：干净文件不写输出，直接报「本来就干净」。
            return CleanResult(src, None, True, MSG_ALREADY_CLEAN)

        dest = default_output_path(src, out_dir)
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return CleanResult(src, None, False, f"失败：无法创建输出目录（{exc}）")

        cmd = [
            sys.executable,
            str(CLEAN_SCRIPT),
            "--json",
            "-o",
            str(dest),
            str(src),
        ]
        try:
            proc = _run(cmd, CLEAN_TIMEOUT)
        except subprocess.TimeoutExpired:
            _discard(dest)
            return CleanResult(src, None, False, "失败：清理超时，文件可能太大")
        except OSError as exc:
            _discard(dest)
            return CleanResult(src, None, False, f"失败：无法启动清理程序（{exc}）")

        stderr = proc.stderr or ""
        if proc.returncode == 2 or _is_unsupported_stderr(stderr):
            _discard(dest)
            if "refusing input larger than" in stderr.lower():
                return CleanResult(src, None, False, MSG_TOO_LARGE)
            return CleanResult(src, None, False, MSG_UNSUPPORTED)

        payload = _parse_json(proc.stdout)
        if not dest.exists():
            tail = _stderr_tail(stderr)
            detail = f"（{tail}）" if tail else ""
            return CleanResult(src, None, False, f"失败：清理没有生成输出文件{detail}")

        message = f"已清理，保存为 {dest.name}"
        if payload is not None and (
            payload.get("still_has_c2pa") or payload.get("still_has_ai_metadata")
        ):
            message += "（可能仍有残留信号）"
        return CleanResult(src, dest, True, message)
    except Exception as exc:  # noqa: BLE001 - 契约要求：绝不向上抛 traceback
        if dest is not None:
            _discard(dest)
        return CleanResult(src, None, False, f"失败：清理出错（{exc}）")


def _discard(dest: Path) -> None:
    """删掉半成品输出。dest 由 default_output_path 保证调用前不存在，删它是安全的。"""
    try:
        if dest.is_file():
            dest.unlink()
    except OSError:
        pass


__all__ = [
    "REPO_ROOT",
    "SCRIPTS_DIR",
    "ScanResult",
    "CleanResult",
    "scan_file",
    "clean_file",
    "default_output_path",
    "tool_hints",
]
