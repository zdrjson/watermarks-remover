#!/usr/bin/env python3
"""AI 水印清理工具 —— 原生 .app 的 Python 引导程序（纯标准库）。

由 ``gui/native/stub.c`` 编译出的存根 dlopen libpython 后直接调用
``Py_BytesMain`` 运行本文件。**本文件绝对不能再 exec／subprocess 另起一个
python 来跑 app.py**——那样 GUI 进程又会归属 Homebrew 的 Python.app bundle，
菜单栏应用名会退回「Python」。app.py 必须跑在当前这个进程里。

职责与仓库里 bash 版 ``WatermarkCleaner.app`` 的「独立模式」一致：
读系统代理 → 查 GitHub 上 main 的最新提交 → 有更新就下载 tarball 原子替换
本地缓存 → 离线时用已缓存的版本 → 一次都没下载成功就弹中文提示退出。
"""

from __future__ import annotations

import io
import os
import runpy
import shutil
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path

REPO = "zdrjson/watermarks-remover"
BASE = Path.home() / "Library" / "Application Support" / "AI水印清理工具"
CURRENT = BASE / "current"
SHA_FILE = BASE / "sha"

API_TIMEOUT = 8
DOWNLOAD_TIMEOUT = 180

EXTRA_TOOL_DIRS = ("/opt/homebrew/bin", "/usr/local/bin")


# ---------------------------------------------------------------------------
# 中文提示
# ---------------------------------------------------------------------------


def alert(message: str) -> None:
    """弹一个中文 alert；osascript 挂了就退回 stderr，绝不静默闪退。"""
    script = (
        'display alert "AI 水印清理工具" message "{msg}" as critical '
        'buttons {{"知道了"}} default button 1'
    ).format(msg=message.replace("\\", " ").replace('"', " "))
    try:
        subprocess.run(  # noqa: S603 - 文案是本文件里的常量
            ["/usr/bin/osascript", "-e", script],
            capture_output=True,
            timeout=120,
            check=False,
        )
    except Exception:  # noqa: BLE001 - 提示失败也不能再抛
        print(f"AI 水印清理工具：{message}", file=sys.stderr)


# ---------------------------------------------------------------------------
# 环境准备
# ---------------------------------------------------------------------------


def apply_system_proxy() -> None:
    """把系统代理（Clash 等只设系统代理）翻成 https_proxy／http_proxy 环境变量。

    Finder 启动的进程默认没有代理环境变量，不做这一步在墙内基本连不上 GitHub。
    """
    if os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY"):
        return
    try:
        proc = subprocess.run(  # noqa: S603 - 固定命令
            ["/usr/sbin/scutil", "--proxy"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except Exception:  # noqa: BLE001
        return

    fields: dict[str, str] = {}
    for line in (proc.stdout or "").splitlines():
        parts = line.strip().split(":", 1)
        if len(parts) == 2:
            fields[parts[0].strip()] = parts[1].strip()

    if fields.get("HTTPSEnable") != "1":
        return
    host = fields.get("HTTPSProxy", "")
    port = fields.get("HTTPSPort", "")
    if not host or not port:
        return
    url = f"http://{host}:{port}"
    os.environ["https_proxy"] = url
    os.environ["http_proxy"] = url


def fix_path() -> None:
    """PATH 前置 Homebrew 目录，保证 qpdf／exiftool 找得到。"""
    parts: list[str] = []
    for item in (*EXTRA_TOOL_DIRS, *os.environ.get("PATH", "").split(os.pathsep)):
        if item and item not in parts:
            parts.append(item)
    os.environ["PATH"] = os.pathsep.join(parts)


def fix_executable() -> None:
    """把 ``sys.executable`` 指回真正的 python 解释器。

    存根是用 ``Py_BytesMain`` 内嵌启动的，``sys.executable`` 会等于存根自己；
    而 ``gui/core.py`` 靠 ``sys.executable`` 起子进程跑上游 CLI——不修的话
    每次扫描都会再拉起一个 GUI。
    """
    home = os.environ.get("PYTHONHOME", "")
    tag = f"{sys.version_info.major}.{sys.version_info.minor}"
    candidates = []
    if home:
        candidates += [f"{home}/bin/python{tag}", f"{home}/bin/python3"]
    candidates += [
        f"/opt/homebrew/bin/python{tag}",
        "/opt/homebrew/bin/python3",
        f"/usr/local/bin/python{tag}",
        "/usr/local/bin/python3",
    ]
    for candidate in candidates:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            sys.executable = candidate
            sys._base_executable = candidate  # noqa: SLF001 - 官方内嵌场景的既定用法
            return


# ---------------------------------------------------------------------------
# 代码自更新（复刻 bash 壳的独立模式）
# ---------------------------------------------------------------------------


def remote_sha() -> str | None:
    """查 GitHub 上 main 的最新提交 sha；查不到返回 None（离线／被墙）。"""
    url = f"https://api.github.com/repos/{REPO}/commits/main"
    request = urllib.request.Request(  # noqa: S310 - 固定的 https 常量 URL
        url,
        headers={
            "Accept": "application/vnd.github.sha",
            "User-Agent": "watermarks-remover-gui",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=API_TIMEOUT) as resp:  # noqa: S310
            body = resp.read(4096).decode("utf-8", "replace").strip()
    except Exception:  # noqa: BLE001 - 离线属于正常情况，静默走缓存
        return None
    # Accept: application/vnd.github.sha 直接回纯 sha；万一回的是 JSON 再抠一次。
    if len(body) == 40 and all(c in "0123456789abcdef" for c in body.lower()):
        return body.lower()
    import json

    try:
        data = json.loads(body)
    except ValueError:
        return None
    sha = data.get("sha") if isinstance(data, dict) else None
    return sha if isinstance(sha, str) and sha else None


def local_sha() -> str | None:
    try:
        return SHA_FILE.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def download(sha: str) -> bool:
    """下载 tarball 并原子替换 ``current/``；成功返回 True。"""
    url = f"https://github.com/{REPO}/archive/{sha}.tar.gz"
    tmp = BASE / f"download.{os.getpid()}"
    shutil.rmtree(tmp, ignore_errors=True)
    try:
        tmp.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT) as resp:  # noqa: S310
            blob = resp.read()
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as archive:
            try:
                archive.extractall(tmp, filter="data")  # noqa: S202 - 已用 data 过滤器
            except TypeError:  # Python < 3.12 没有 filter 参数
                archive.extractall(tmp)  # noqa: S202

        # GitHub tarball 顶层只有一个 <repo>-<sha> 目录，等价于 --strip-components 1。
        tops = [p for p in tmp.iterdir() if p.is_dir()]
        root = tops[0] if len(tops) == 1 else tmp
        if not (root / "gui" / "app.py").is_file():
            return False

        staged = BASE / f"staged.{os.getpid()}"
        shutil.rmtree(staged, ignore_errors=True)
        root.rename(staged)
        shutil.rmtree(CURRENT, ignore_errors=True)
        staged.rename(CURRENT)
        SHA_FILE.write_text(sha, encoding="utf-8")
        return True
    except Exception:  # noqa: BLE001 - 下载失败就退回缓存
        return False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def ensure_code() -> Path | None:
    """保证 ``current/gui`` 存在且尽量是最新的；拿不到代码返回 None。"""
    try:
        BASE.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None

    cached = (CURRENT / "gui" / "app.py").is_file()
    sha = remote_sha()
    if sha and (not cached or sha != local_sha()):
        download(sha)

    gui_dir = CURRENT / "gui"
    return gui_dir if (gui_dir / "app.py").is_file() else None


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def main() -> int:
    fix_path()
    fix_executable()
    apply_system_proxy()

    gui_dir = ensure_code()
    if gui_dir is None:
        alert(
            f"首次使用需要联网从 GitHub 下载程序（github.com/{REPO}），刚才没有下载成功。"
            "请确认网络（或代理）可以访问 GitHub 后重试。"
        )
        return 1

    os.chdir(gui_dir)
    sys.path.insert(0, str(gui_dir))
    # app.py 里读 sys.argv 时不该看到 bootstrap 的痕迹。
    sys.argv = ["app.py"]
    runpy.run_path(str(gui_dir / "app.py"), run_name="__main__")
    return 0


if __name__ == "__main__":
    sys.exit(main())
