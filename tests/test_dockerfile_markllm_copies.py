"""Dockerfile.markllm must copy every local module its scripts import at load time.

The image copies the Layer B rewrite machinery into /app file by file, so a new
``from humanize_pass import ...`` in rewrite_text.py built fine and only failed
at run time with ModuleNotFoundError (and took bench_synthid_text.py with it).
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "service" / "scripts"
DOCKERFILE = ROOT / "service" / "Dockerfile.markllm"


def _copied_modules() -> dict[str, str]:
    """Map module name inside /app -> source file name under scripts/."""
    text = DOCKERFILE.read_text(encoding="utf-8")
    pairs = re.findall(r"^COPY\s+scripts/(\w+\.py)\s+/app/(\w+)\.py\s*$", text, re.M)
    return {dest: src for src, dest in pairs}


def _load_time_imports(tree: ast.Module) -> Iterator[str]:
    """Top-level module names imported when the module loads (skips function bodies)."""
    stack: list[ast.AST] = list(tree.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if isinstance(node, ast.Import):
            yield from (alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            yield node.module.split(".")[0]
        stack.extend(ast.iter_child_nodes(node))


def test_markllm_image_copies_every_local_import():
    copied = _copied_modules()
    assert "rewrite_text" in copied, "COPY regex no longer matches Dockerfile.markllm"
    local = {p.stem for p in SCRIPTS.glob("*.py")}
    missing = {}
    for module, src in copied.items():
        assert module == Path(src).stem, f"{src} is copied under another name"
        tree = ast.parse((SCRIPTS / src).read_text(encoding="utf-8"))
        absent = sorted((set(_load_time_imports(tree)) & local) - copied.keys())
        if absent:
            missing[src] = absent
    assert not missing, f"Dockerfile.markllm does not COPY imported modules: {missing}"
