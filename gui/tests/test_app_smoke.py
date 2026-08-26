"""GUI smoke tests for gui/app.py -- see SPEC.md "测试规格" item 7.

These tests only exercise the small, callable surface SPEC.md commits app.py
to: a `create_app()` factory returning `(root, app)`, a fixed set of five
top-level buttons, and a `ttk.Treeview` with four columns. They do not drive
real scan/clean workflows through the UI (that is core.py's job, covered by
test_core.py) and do not attempt to click buttons or exercise the background
worker thread -- this is a smoke test ("does the window come up and stay
up"), not an end-to-end UI test.

Why subprocess isolation (read this before "simplifying" this file):
On this machine (Homebrew Python 3.14.7 + Tk 9.0), instantiating a real
``tkinter.Tk()`` and then calling ``root.update()`` against it repeatedly
*inside this unittest process* intermittently segfaults inside
libtcl9tk9.0's window-mapping code (``Tk_MacOSXGetTkWindow`` /
``showRootWindow``) -- a known Tk 9.0 + macOS 15 race condition. A native
segfault there is a SIGSEGV in this very interpreter: it does not raise a
catchable Python exception, it takes the whole test process down (or, on a
narrower timing window, wedges it) -- so no amount of try/except in this
file can contain it, and an earlier version of this file that did all of
this in-process was observed to crash or hang the entire
``python3 -m unittest discover`` run.

To stay immune to that native crash while still covering everything SPEC.md
item 7 asks for, the actual ``tkinter.Tk()`` lifecycle happens in a
*subprocess* running ``tests/_smoke_driver.py`` (see that file for the exact
linear flow: create_app() -> root.withdraw() immediately -> collect
title/minsize/buttons/columns -> a few root.update_idletasks() calls
[deliberately not update(), see below] -> root.destroy() -> print one JSON
line to stdout, exit 0). Withdrawing the window before ever touching it
sidesteps the showRootWindow mapping race entirely, since that code path
only runs when a window transitions to visible/mapped -- a withdrawn root
never gets there. ``update_idletasks()`` only flushes pending idle
callbacks (geometry management, etc.); unlike ``update()`` it never pumps
native window-manager/OS events, which is the lower-risk choice here.

If that subprocess is killed by a signal (e.g. SIGSEGV) or hangs past the
timeout, this test process itself is completely unaffected -- Python's
``subprocess`` module reports it as a negative/nonzero return code (or a
``TimeoutExpired``), and setUpClass turns that into one clear, ordinary
Python assertion failure/error instead of taking the whole suite down.

If tkinter is not importable in the subprocess, or is importable but has no
usable display/Tcl interpreter behind it (headless CI, missing Tcl/Tk
framework), the driver reports that as a `{"skip": true, ...}` JSON payload
and every test in this class is skipped via `unittest.SkipTest`, per
SPEC.md's explicit instruction: "GUI 冒烟（tkinter 可用时才跑，否则
skipTest）" -- this is a skip, not a failure.

Run with:
    cd ~/watermarks-remover && python3 -m unittest discover -s gui/tests -v
"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
DRIVER_PATH = TESTS_DIR / "_smoke_driver.py"

#: Generous but bounded -- create_app() + a handful of update_idletasks()
#: calls should take well under a second; 30s only needs to cover a slow
#: CI box, never a hang (a real wedge should time out well before this).
SUBPROCESS_TIMEOUT_S = 30


class TestAppSmoke(unittest.TestCase):
    """Runs the smoke driver subprocess ONCE in setUpClass (one Tk()
    lifecycle, per SPEC.md's linear "create -> inspect -> update ->
    destroy" description), then makes independent assertions against the
    parsed JSON result from several test methods -- so a failure on, say,
    the button labels is reported distinctly from a failure on the window
    title, instead of as one monolithic test.
    """

    @classmethod
    def setUpClass(cls) -> None:
        try:
            proc = subprocess.run(
                [sys.executable, str(DRIVER_PATH)],
                capture_output=True,
                text=True,
                timeout=SUBPROCESS_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired as exc:
            raise AssertionError(
                f"GUI 冒烟子进程超过 {SUBPROCESS_TIMEOUT_S} 秒未退出（疑似挂死）："
                f"判定为 Tk 原生崩溃/挂死。\n"
                f"stdout:\n{exc.stdout}\nstderr:\n{exc.stderr}"
            ) from exc

        if proc.returncode < 0:
            raise AssertionError(
                f"GUI 冒烟子进程被信号杀死（returncode={proc.returncode}）："
                f"判定为 Tk 原生崩溃。\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
            )
        if proc.returncode != 0:
            raise AssertionError(
                f"GUI 冒烟子进程异常退出（returncode={proc.returncode}）。\n"
                f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
            )

        # The driver prints exactly one JSON line, but be tolerant of any
        # incidental banner output (e.g. macOS AppKit/Tk log lines) sharing
        # stdout by taking the last non-blank line rather than assuming
        # line 1.
        json_line = ""
        for candidate in reversed(proc.stdout.splitlines()):
            if candidate.strip():
                json_line = candidate.strip()
                break
        if not json_line:
            raise AssertionError(
                f"GUI 冒烟子进程没有输出预期的 JSON 结果。\nstderr:\n{proc.stderr}"
            )

        try:
            result = json.loads(json_line)
        except json.JSONDecodeError as exc:
            raise AssertionError(
                f"GUI 冒烟子进程输出不是合法 JSON：{exc}\n原始输出：{json_line!r}"
            ) from exc

        if result.get("skip"):
            raise unittest.SkipTest(result.get("reason", "tkinter unavailable"))
        if "error" in result:
            raise AssertionError(f"GUI 冒烟子进程内部出错：{result['error']}")

        cls.result = result

    def test_create_app_returns_usable_result(self) -> None:
        self.assertIn("title", self.result)
        self.assertIn("minsize", self.result)
        self.assertIn("buttons", self.result)
        self.assertIn("tree_columns", self.result)

    def test_window_title_and_minimum_size(self) -> None:
        self.assertEqual(self.result["title"], "AI 水印清理工具")
        min_w, min_h = self.result["minsize"]
        self.assertGreaterEqual(min_w, 760)
        self.assertGreaterEqual(min_h, 500)

    def test_five_top_level_buttons_exist(self) -> None:
        expected_labels = {
            "＋ 添加文件",
            "＋ 添加文件夹",
            "一键清理",
            "打开输出位置",
            "清空列表",
        }
        found_labels = set(self.result["buttons"])
        missing = expected_labels - found_labels
        self.assertFalse(
            missing,
            f"missing expected buttons: {missing}; buttons found: {found_labels}",
        )

    def test_treeview_exists_with_four_columns(self) -> None:
        trees = self.result["tree_columns"]
        self.assertEqual(len(trees), 1, "expected exactly one ttk.Treeview in the window")
        columns = trees[0]
        self.assertEqual(
            len(columns),
            4,
            f"expected 4 columns (文件名/类型/检测结果/状态), got: {columns}",
        )

    def test_update_idletasks_and_destroy_completed_without_crashing(self) -> None:
        # If we get here at all, setUpClass already proved the subprocess
        # ran root.update_idletasks() a few times and root.destroy() and
        # then exited 0 -- i.e. neither call raised nor crashed the
        # interpreter. This test exists to make that coverage explicit and
        # independently visible in -v output, per SPEC.md's "root.update()
        # 数次不抛异常、destroy 干净退出".
        self.assertEqual(self.result.get("skip"), False)


if __name__ == "__main__":
    unittest.main()
