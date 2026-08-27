/*
 * AI 水印清理工具 —— 原生启动存根（macOS）
 *
 * 为什么需要它：bash 壳脚本用 `exec python3` 起 Tk，GUI 进程于是归属
 * Homebrew 的 Python.app bundle，菜单栏应用名被显示成「Python」。
 * Tk 取的是主 bundle 的 CFBundleName，所以只要主进程本身就是我们
 * bundle 里的可执行文件，菜单名就是「AI 水印清理工具」。
 *
 * 做法：本存根不链接、也不包含任何 Python 头文件——它只
 *   1) setenv 好 PYTHONHOME 与 PATH；
 *   2) dlopen Homebrew 的 libpython3.14.dylib；
 *   3) dlsym 拿 Py_BytesMain，以 argv {argv[0], "<Resources>/bootstrap.py"} 调用。
 * 全程不 exec/fork 任何 python 进程，主进程始终是自己，菜单名才生效。
 *
 * 编译（见 gui/build-app.sh）：
 *   clang -O2 -arch arm64 -DPY_HOME='"..."' -DPY_DYLIB='"..."' -o WatermarkCleaner stub.c
 */

#include <dlfcn.h>
#include <libgen.h>
#include <limits.h>
#include <mach-o/dyld.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

/* 由 build-app.sh 用 brew --prefix python@3.14 的实测值覆盖；这里给出默认值。 */
#ifndef PY_HOME
#define PY_HOME "/opt/homebrew/opt/python@3.14/Frameworks/Python.framework/Versions/3.14"
#endif

#ifndef PY_DYLIB
#define PY_DYLIB PY_HOME "/lib/libpython3.14.dylib"
#endif

#ifndef EXTRA_PATH
#define EXTRA_PATH "/opt/homebrew/bin:/usr/local/bin"
#endif

#define ALERT_TITLE "AI 水印清理工具"

/*
 * 把要塞进 AppleScript 字符串字面量里的文本洗一遍：引号、反斜杠、换行
 * 统统换成空格。文案本身是本文件里的常量，但 dlerror() 的内容不可控。
 */
static void sanitize(const char *src, char *dst, size_t cap) {
    size_t i = 0;
    if (cap == 0) {
        return;
    }
    for (; src && *src && i + 1 < cap; src++) {
        char c = *src;
        if (c == '"' || c == '\'' || c == '\\' || c == '\n' || c == '\r' || c == '`' ||
            c == '$') {
            c = ' ';
        }
        dst[i++] = c;
    }
    dst[i] = '\0';
}

/* 弹一个中文 alert。所有失败路径都走这里，绝不静默闪退。 */
static void alert(const char *message) {
    char safe[2048];
    char cmd[4096];
    sanitize(message, safe, sizeof(safe));
    snprintf(cmd, sizeof(cmd),
             "/usr/bin/osascript -e 'display alert \"%s\" message \"%s\" as critical "
             "buttons {\"知道了\"} default button 1' >/dev/null 2>&1",
             ALERT_TITLE, safe);
    if (system(cmd) != 0) {
        fprintf(stderr, "%s: %s\n", ALERT_TITLE, safe);
    }
}

/* PATH 前置 Homebrew 目录，保证上游脚本能找到 qpdf／exiftool。 */
static void fix_path(void) {
    const char *old = getenv("PATH");
    char merged[8192];
    if (old != NULL && *old != '\0') {
        snprintf(merged, sizeof(merged), "%s:%s", EXTRA_PATH, old);
    } else {
        snprintf(merged, sizeof(merged), "%s:/usr/bin:/bin:/usr/sbin:/sbin", EXTRA_PATH);
    }
    setenv("PATH", merged, 1);
}

/* 取本可执行文件的真实路径：先 argv[0]，失败再问 dyld。 */
static int resolve_self(const char *argv0, char *out, size_t cap) {
    char buf[PATH_MAX];
    uint32_t size = (uint32_t)sizeof(buf);

    if (argv0 != NULL && *argv0 != '\0' && realpath(argv0, out) != NULL) {
        return 1;
    }
    if (_NSGetExecutablePath(buf, &size) == 0 && realpath(buf, out) != NULL) {
        return 1;
    }
    (void)cap;
    return 0;
}

int main(int argc, char *argv[]) {
    char self[PATH_MAX];
    char self_copy[PATH_MAX];
    char raw[PATH_MAX];
    char bootstrap[PATH_MAX];
    char detail[2048];
    void *handle = NULL;
    int (*py_bytes_main)(int, char **) = NULL;
    char *py_argv[3];

    (void)argc;

    if (!resolve_self(argv[0], self, sizeof(self))) {
        alert("无法定位程序自身的位置，应用可能已损坏。请重新安装「AI 水印清理工具」。");
        return 1;
    }

    /* dirname 可能就地改写入参，先拷一份。 */
    snprintf(self_copy, sizeof(self_copy), "%s", self);
    snprintf(raw, sizeof(raw), "%s/../Resources/bootstrap.py", dirname(self_copy));
    if (realpath(raw, bootstrap) == NULL) {
        alert("应用内部文件 bootstrap.py 缺失，安装包可能不完整。请重新运行 "
              "gui/build-app.sh 安装。");
        return 1;
    }

    setenv("PYTHONHOME", PY_HOME, 1);
    setenv("PYTHONIOENCODING", "utf-8", 1);
    fix_path();

    handle = dlopen(PY_DYLIB, RTLD_NOW | RTLD_GLOBAL);
    if (handle == NULL) {
        snprintf(detail, sizeof(detail),
                 "找不到或无法加载 Python 运行库：%s。请在「终端」执行 "
                 "brew install python@3.14 python-tk 后重试。（%s）",
                 PY_DYLIB, dlerror());
        alert(detail);
        return 1;
    }

    *(void **)(&py_bytes_main) = dlsym(handle, "Py_BytesMain");
    if (py_bytes_main == NULL) {
        snprintf(detail, sizeof(detail),
                 "Python 运行库版本不兼容（缺少 Py_BytesMain）：%s。请在「终端」执行 "
                 "brew reinstall python@3.14 后重试。",
                 PY_DYLIB);
        alert(detail);
        return 1;
    }

    py_argv[0] = argv[0];
    py_argv[1] = bootstrap;
    py_argv[2] = NULL;
    return py_bytes_main(2, py_argv);
}
