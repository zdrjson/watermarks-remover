#!/usr/bin/env python3
"""AI 水印清理工具 —— Tkinter 界面层。

本文件只做三件事：画界面、把活儿丢给后台线程、把结果显示出来。
所有判断逻辑都在 core.py 里，这里不做任何解析。
"""

from __future__ import annotations

import queue
import subprocess
import sys
import threading
import tkinter as tk
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import core  # type: ignore[no-redef]
else:  # pragma: no cover - 作为包导入时的分支
    from . import core

APP_TITLE = "AI 水印清理工具"
MIN_WIDTH = 760
MIN_HEIGHT = 500

#: 「添加文件夹」递归收集的扩展名。
SUPPORTED_EXTS = {
    ".md", ".txt", ".html", ".svg",
    ".png", ".jpg", ".jpeg", ".webp", ".gif", ".tiff", ".bmp",
    ".pdf", ".docx", ".xlsx", ".pptx", ".epub", ".odt",
    ".mp4", ".mov", ".m4a", ".wav", ".mp3", ".flac",
}

#: 一次最多从文件夹里收多少个文件，防止误选巨型目录。
MAX_FOLDER_FILES = 500

#: UI 轮询后台结果的间隔（毫秒）。
POLL_INTERVAL_MS = 100

FOOTER_NOTE = "清理结果另存为“原文件名_已清理”，不会改动原文件"

STATUS_SCANNING = "检测中…"
STATUS_PENDING = "等待中…"
STATUS_CLEANING = "清理中…"
STATUS_CLEAN_OK = "已清理 ✓"
STATUS_NO_NEED = "无需清理"
STATUS_FILE_CLEAN = "干净 ✓"


@dataclass
class Row:
    """列表里的一行。"""

    path: Path
    kind: str = "—"
    detail: str = ""
    status: str = STATUS_PENDING
    scan: object | None = None  # core.ScanResult 或 None
    output: Path | None = None
    cleaned: bool = False
    findings: list[str] = field(default_factory=list)


class App:
    """主界面。"""

    def __init__(self, root: tk.Misc) -> None:
        self.root = root
        self.rows: dict[str, Row] = {}
        self._paths: set[str] = set()
        self._results: queue.Queue = queue.Queue()
        self._tasks: queue.Queue = queue.Queue()
        self._pending = 0
        self._closing = False
        self._poll_id: str | None = None

        self._build_ui()
        self._start_worker()
        self._schedule_poll()

        try:
            self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        except (AttributeError, tk.TclError):  # pragma: no cover - Toplevel 之外的容器
            pass

    # ------------------------------------------------------------------
    # 界面
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = self.root
        try:
            root.title(APP_TITLE)
            root.minsize(MIN_WIDTH, MIN_HEIGHT)
            root.geometry(f"{MIN_WIDTH + 200}x{MIN_HEIGHT + 100}")
        except (AttributeError, tk.TclError):  # pragma: no cover
            pass

        outer = ttk.Frame(root, padding=(12, 10, 12, 8))
        outer.pack(fill="both", expand=True)
        self.outer = outer

        # --- 顶部按钮行 ---
        bar = ttk.Frame(outer)
        bar.pack(fill="x", pady=(0, 8))
        self.toolbar = bar

        self.btn_add_files = ttk.Button(bar, text="＋ 添加文件", command=self.add_files)
        self.btn_add_folder = ttk.Button(bar, text="＋ 添加文件夹", command=self.add_folder)
        self.btn_clean = ttk.Button(bar, text="一键清理", command=self.clean_all)
        self.btn_open_output = ttk.Button(bar, text="打开输出位置", command=self.open_output)
        self.btn_clear = ttk.Button(bar, text="清空列表", command=self.clear_list)
        for btn in (
            self.btn_add_files,
            self.btn_add_folder,
            self.btn_clean,
            self.btn_open_output,
            self.btn_clear,
        ):
            btn.pack(side="left", padx=(0, 8))
        self.buttons = {
            "add_files": self.btn_add_files,
            "add_folder": self.btn_add_folder,
            "clean": self.btn_clean,
            "open_output": self.btn_open_output,
            "clear": self.btn_clear,
        }

        # --- 中部列表 ---
        table = ttk.Frame(outer)
        table.pack(fill="both", expand=True)
        columns = ("name", "kind", "detail", "status")
        self.tree = ttk.Treeview(table, columns=columns, show="headings", selectmode="extended")
        headings = {
            "name": "文件名",
            "kind": "类型",
            "detail": "检测结果",
            "status": "状态",
        }
        widths = {"name": 300, "kind": 90, "detail": 330, "status": 140}
        for key in columns:
            self.tree.heading(key, text=headings[key])
            self.tree.column(key, width=widths[key], anchor="w", stretch=(key in ("name", "detail")))
        scroll = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        # --- 依赖缺失提示条（黄色） ---
        hints = []
        try:
            hints = core.tool_hints()
        except Exception:  # noqa: BLE001 - 提示条不该拖垮界面
            hints = []
        self.hint_label: tk.Label | None = None
        if hints:
            self.hint_label = tk.Label(
                outer,
                text="⚠️ " + "；".join(hints),
                bg="#FFF3CD",
                fg="#7A5B00",
                anchor="w",
                justify="left",
                padx=8,
                pady=5,
                wraplength=MIN_WIDTH + 120,
            )
            self.hint_label.pack(fill="x", pady=(8, 0))

        # --- 底部状态栏 ---
        status = ttk.Frame(outer)
        status.pack(fill="x", pady=(8, 0))
        self.status_var = tk.StringVar(value="就绪：请先添加要检查的文件")
        self.status_label = ttk.Label(status, textvariable=self.status_var, anchor="w")
        self.status_label.pack(side="left")
        self.footer_label = ttk.Label(status, text=FOOTER_NOTE, anchor="e", foreground="#666666")
        self.footer_label.pack(side="right")

    # ------------------------------------------------------------------
    # 后台线程
    # ------------------------------------------------------------------

    def _start_worker(self) -> None:
        self._worker = threading.Thread(target=self._worker_loop, name="wm-worker", daemon=True)
        self._worker.start()

    def _worker_loop(self) -> None:
        while True:
            task = self._tasks.get()
            if task is None:
                self._tasks.task_done()
                return
            action, item, path = task
            try:
                if action == "scan":
                    self._results.put(("scan", item, core.scan_file(path)))
                else:
                    self._results.put(("clean", item, core.clean_file(path)))
            except Exception as exc:  # noqa: BLE001 - 后台线程绝不能死
                self._results.put(("error", item, f"处理出错：{exc}"))
            finally:
                self._tasks.task_done()

    def _enqueue(self, action: str, item: str, path: Path) -> None:
        self._pending += 1
        self._tasks.put((action, item, path))

    # ------------------------------------------------------------------
    # 结果轮询（UI 线程）
    # ------------------------------------------------------------------

    def _schedule_poll(self) -> None:
        if self._closing:
            return
        try:
            self._poll_id = self.root.after(POLL_INTERVAL_MS, self._poll)
        except tk.TclError:  # pragma: no cover - 窗口已销毁
            self._poll_id = None

    def _poll(self) -> None:
        if self._closing:
            return
        drained = 0
        while drained < 50:
            try:
                kind, item, payload = self._results.get_nowait()
            except queue.Empty:
                break
            drained += 1
            self._pending = max(0, self._pending - 1)
            try:
                self._apply_result(kind, item, payload)
            except tk.TclError:  # pragma: no cover - 窗口销毁途中
                self._closing = True
                return
        self._refresh_status()
        self._schedule_poll()

    def _apply_result(self, kind: str, item: str, payload: object) -> None:
        row = self.rows.get(item)
        if row is None or not self.tree.exists(item):
            return
        if kind == "error":
            row.status = "失败"
            row.detail = str(payload)
        elif kind == "scan":
            row.scan = payload
            row.kind = getattr(payload, "kind", "—")
            error = getattr(payload, "error", None)
            findings = list(getattr(payload, "findings", []) or [])
            row.findings = findings
            if error:
                row.status = "失败"
                row.detail = error
            elif getattr(payload, "actionable", False):
                row.status = f"发现 {len(findings)} 处痕迹"
                row.detail = "、".join(findings)
            elif row.kind == "unknown":
                row.status = "跳过"
                row.detail = "无法识别的文件格式"
            else:
                row.status = STATUS_FILE_CLEAN
                row.detail = "未发现痕迹"
        else:  # clean
            ok = bool(getattr(payload, "ok", False))
            output = getattr(payload, "output", None)
            message = str(getattr(payload, "message", ""))
            if ok and output is not None:
                row.cleaned = True
                row.output = Path(output)
                row.status = STATUS_CLEAN_OK
                row.detail = row.output.name
            elif ok:
                row.status = STATUS_NO_NEED
                row.detail = message
            else:
                row.status = "失败" if message.startswith("失败") else "跳过"
                row.detail = message
        self._render_row(item)

    # ------------------------------------------------------------------
    # 列表操作
    # ------------------------------------------------------------------

    def _render_row(self, item: str) -> None:
        row = self.rows[item]
        self.tree.item(
            item,
            values=(row.path.name, row.kind, row.detail, row.status),
        )

    def add_paths(self, paths) -> int:
        """把一批路径加进列表并排队扫描，返回真正新增的条数。"""
        added = 0
        for raw in paths:
            path = Path(raw).expanduser()
            key = str(path.resolve()) if path.exists() else str(path)
            if key in self._paths:
                continue
            self._paths.add(key)
            item = self.tree.insert("", "end", values=(path.name, "—", "", STATUS_SCANNING))
            self.rows[item] = Row(path=path, status=STATUS_SCANNING)
            self._enqueue("scan", item, path)
            added += 1
        if added:
            self._refresh_status()
        return added

    def add_files(self) -> None:
        try:
            picked = filedialog.askopenfilenames(
                title="选择要检查的文件",
                parent=self.root,
            )
        except tk.TclError:  # pragma: no cover
            return
        if picked:
            self.add_paths(picked)

    def add_folder(self) -> None:
        try:
            folder = filedialog.askdirectory(title="选择要检查的文件夹", parent=self.root)
        except tk.TclError:  # pragma: no cover
            return
        if not folder:
            return
        files, truncated = collect_folder_files(folder)
        if not files:
            messagebox.showinfo(APP_TITLE, "这个文件夹里没有找到可以检查的文件。", parent=self.root)
            return
        self.add_paths(files)
        if truncated:
            messagebox.showinfo(
                APP_TITLE,
                f"文件夹里的文件太多，本次只加入前 {MAX_FOLDER_FILES} 个。",
                parent=self.root,
            )

    def clear_list(self) -> None:
        for item in list(self.rows):
            if self.tree.exists(item):
                self.tree.delete(item)
        self.rows.clear()
        self._paths.clear()
        self._refresh_status()

    def clean_all(self) -> None:
        """只清理有痕迹的行；干净的行标「无需清理」。"""
        queued = 0
        for item, row in self.rows.items():
            if row.cleaned:
                continue
            scan = row.scan
            if scan is None:
                # 还没扫完：照样排队，core.clean_file 会自己再判断一次。
                row.status = STATUS_CLEANING
                self._render_row(item)
                self._enqueue("clean", item, row.path)
                queued += 1
                continue
            if getattr(scan, "error", None):
                continue
            if getattr(scan, "actionable", False):
                row.status = STATUS_CLEANING
                self._render_row(item)
                self._enqueue("clean", item, row.path)
                queued += 1
            elif getattr(scan, "kind", "") == "unknown":
                row.status = "跳过"
                row.detail = "无法识别的文件格式"
                self._render_row(item)
            else:
                row.status = STATUS_NO_NEED
                row.detail = "未发现痕迹"
                self._render_row(item)
        if not self.rows:
            messagebox.showinfo(APP_TITLE, "列表是空的，请先添加文件。", parent=self.root)
        elif queued == 0:
            self.status_var.set("没有需要清理的文件。")
        self._refresh_status()

    def open_output(self) -> None:
        """在访达里打开输出位置。"""
        target: Path | None = None
        for item in self.tree.selection():
            row = self.rows.get(item)
            if row is not None and row.output is not None:
                target = row.output
                break
        if target is None:
            for row in self.rows.values():
                if row.output is not None:
                    target = row.output
                    break
        if target is None:
            for item in self.tree.selection():
                row = self.rows.get(item)
                if row is not None:
                    target = row.path
                    break
        if target is None and self.rows:
            target = next(iter(self.rows.values())).path
        if target is None:
            messagebox.showinfo(APP_TITLE, "还没有可以打开的位置，请先添加并清理文件。", parent=self.root)
            return
        reveal(target)

    # ------------------------------------------------------------------
    # 状态栏
    # ------------------------------------------------------------------

    def _refresh_status(self) -> None:
        try:
            if self._pending > 0:
                self.status_var.set(f"处理中…还剩 {self._pending} 个文件")
            elif not self.rows:
                self.status_var.set("就绪：请先添加要检查的文件")
            else:
                cleaned = sum(1 for r in self.rows.values() if r.cleaned)
                dirty = sum(
                    1 for r in self.rows.values() if getattr(r.scan, "actionable", False) and not r.cleaned
                )
                self.status_var.set(
                    f"共 {len(self.rows)} 个文件｜待清理 {dirty} 个｜已清理 {cleaned} 个"
                )
        except tk.TclError:  # pragma: no cover
            self._closing = True

    # ------------------------------------------------------------------
    # 关闭
    # ------------------------------------------------------------------

    def on_close(self) -> None:
        self.shutdown()
        try:
            self.root.destroy()
        except tk.TclError:  # pragma: no cover
            pass

    def shutdown(self) -> None:
        """停掉轮询与后台线程；重复调用安全。"""
        if self._closing:
            return
        self._closing = True
        if self._poll_id is not None:
            try:
                self.root.after_cancel(self._poll_id)
            except (tk.TclError, ValueError):  # pragma: no cover
                pass
            self._poll_id = None
        self._tasks.put(None)


# ----------------------------------------------------------------------
# 辅助函数
# ----------------------------------------------------------------------


def collect_folder_files(folder: str | Path, limit: int = MAX_FOLDER_FILES):
    """递归收集文件夹里的可处理文件，返回 (文件列表, 是否被截断)。"""
    root = Path(folder).expanduser()
    found: list[Path] = []
    truncated = False
    try:
        for path in sorted(root.rglob("*")):
            if len(found) >= limit:
                truncated = True
                break
            if path.name.startswith("."):
                continue
            if core.CLEANED_SUFFIX in path.stem:
                continue  # 别把上次的清理结果再收一遍
            try:
                if not path.is_file():
                    continue
            except OSError:
                continue
            if path.suffix.lower() in SUPPORTED_EXTS:
                found.append(path)
    except OSError:
        pass
    return found, truncated


def reveal(target: str | Path) -> None:
    """在访达里定位文件／打开目录；失败时静默，不弹 traceback。"""
    path = Path(target)
    try:
        if path.is_dir():
            subprocess.Popen(["open", str(path)])  # noqa: S603,S607
        elif path.exists():
            subprocess.Popen(["open", "-R", str(path)])  # noqa: S603,S607
        else:
            subprocess.Popen(["open", str(path.parent)])  # noqa: S603,S607
    except OSError:
        pass


def create_app():
    """建窗并返回 ``(root, app)``，供 main() 与测试共用。"""
    root = tk.Tk()
    app = App(root)
    return root, app


def main() -> int:
    root, app = create_app()
    try:
        root.mainloop()
    finally:
        app.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
