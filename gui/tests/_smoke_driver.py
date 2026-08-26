"""Standalone driver for the GUI smoke test -- NOT a unittest module itself.

test_app_smoke.py runs this file with ``sys.executable`` in a *subprocess*
instead of instantiating ``tkinter.Tk()`` inside the unittest process. See
the module docstring in test_app_smoke.py for why: on this machine
(Homebrew Python 3.14.7 + Tk 9.0), repeated ``root.update()`` calls against
a real Tk root intermittently segfault inside libtcl9tk9.0's window-mapping
code (``Tk_MacOSXGetTkWindow`` / ``showRootWindow``), which is a known
Tk 9.0 + macOS 15 race. A crash there takes down the whole process it runs
in -- so this driver isolates that risk to a disposable child process and
reports back over stdout as one line of JSON.

Linear flow (deliberately no branching / no loops around the risky calls):
  1. import app (adds gui/ to sys.path first)
  2. root, app = create_app()
  3. root.withdraw() immediately -- a withdrawn window is never mapped by
     the window server, which sidesteps the showRootWindow race entirely
     (that code path only runs when a window transitions to visible).
  4. collect the assertion material the unittest side needs: window title,
     minsize, the five toolbar buttons' text, and the Treeview's columns.
  5. root.update_idletasks() a few times -- idle-only refresh (geometry /
     pending callbacks), never the platform event pump that update() uses,
     so it does not touch native window-manager code either.
  6. root.destroy()
  7. print the collected data as one line of JSON on stdout, exit 0.

Special-cased outcomes (still exit 0, so a real crash is unambiguous -- a
crash is a nonzero/negative return code with NO trailing JSON line):
  - tkinter not importable at all               -> {"skip": true, "reason": ...}
  - tkinter importable but no usable Tcl/display -> {"skip": true, "reason": ...}
  - any other failure while building the payload -> {"skip": false, "error": ...}
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

GUI_DIR = Path(__file__).resolve().parent.parent
if str(GUI_DIR) not in sys.path:
    sys.path.insert(0, str(GUI_DIR))


def _walk_widgets(root):
    """Yield root and every descendant widget, depth-first."""
    stack = [root]
    while stack:
        widget = stack.pop()
        yield widget
        try:
            stack.extend(widget.winfo_children())
        except Exception:
            pass


def main() -> int:
    try:
        import tkinter as tk
    except ImportError as exc:
        print(json.dumps({"skip": True, "reason": f"tkinter is not importable: {exc}"}))
        return 0

    try:
        from app import create_app
    except Exception as exc:  # noqa: BLE001 - report, don't traceback
        print(json.dumps({"skip": False, "error": f"import app failed: {exc!r}"}))
        return 1

    try:
        root, app = create_app()
    except tk.TclError as exc:
        # Importable but no usable display/Tcl interpreter behind it --
        # common on minimal/headless Python installs. Not the native crash
        # we're guarding against; treat as an environment skip.
        print(json.dumps({"skip": True, "reason": f"tkinter has no usable display/Tcl: {exc}"}))
        return 0

    error: str | None = None
    payload: dict | None = None
    try:
        root.withdraw()

        title = root.title()
        minsize = list(root.minsize())

        buttons: list[str] = []
        tree_columns: list[list[str]] = []
        for widget in _walk_widgets(root):
            try:
                wclass = widget.winfo_class()
            except Exception:
                continue
            if wclass in ("Button", "TButton"):
                try:
                    buttons.append(str(widget["text"]))
                except Exception:
                    pass
            elif wclass == "Treeview":
                try:
                    tree_columns.append(list(widget["columns"]))
                except Exception:
                    tree_columns.append([])

        for _ in range(3):
            root.update_idletasks()

        payload = {
            "skip": False,
            "title": title,
            "minsize": minsize,
            "buttons": buttons,
            "tree_columns": tree_columns,
        }
    except Exception as exc:  # noqa: BLE001 - report, don't traceback
        error = repr(exc)
    finally:
        try:
            root.destroy()
        except Exception:
            pass

    if error is not None:
        print(json.dumps({"skip": False, "error": error}))
        return 1

    print(json.dumps(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
