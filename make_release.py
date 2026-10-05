# -*- coding: utf-8 -*-
"""打包 Windows 免安装发布包（release/RNGdle/ + zip）。

用法：
    python make_release.py                 # 用当前解释器的安装目录作为便携运行时来源
    set RNGDLE_PYSRC=D:\\Python312 && python make_release.py

产出（都在仓库根目录）：
    release/RNGdle/               免安装目录
    RNGdle_standalone_vX.Y.Z_win64.zip

打包内容：rngdle_score.exe（静态链接）、rngdle_server.py、便携 Python 运行时、
Chrome 扩展、配置模板、一键启动 .bat、使用说明、README。
目标机器无需安装 Python、编译器或任何运行库。
"""
import os
import shutil
import sys
import zipfile

BASE = os.path.dirname(os.path.abspath(__file__))
# 便携运行时来源：默认取运行本脚本的解释器所在安装目录（即 sys.base_prefix）
PYSRC = os.environ.get("RNGDLE_PYSRC") or sys.base_prefix
REL = os.path.join(BASE, "release", "RNGdle")
VERSION = "1.0.9"
ZIP = os.path.join(BASE, "RNGdle_standalone_v%s_win64.zip" % VERSION)

# Lib 里不需要的大目录（tkinter/tcl 与测试套件；服务器只用标准库的网络/JSON/线程部分）
SKIP_LIB = {"site-packages", "test", "tests", "tkinter", "idlelib", "lib2to3",
            "ensurepip", "distutils", "turtledemo", "__pycache__", "venv", "curses",
            "msilib", "multiprocessing", "asyncio", "xml", "xmlrpc", "sqlite3", "dbm"}
# DLLs 里不需要的扩展（tk/sqlite/测试），其余全留以免缺 C 模块
SKIP_DLL = {"_tkinter.pyd", "tcl86t.dll", "tk86t.dll", "_sqlite3.pyd", "sqlite3.dll",
            "_testcapi.pyd", "_testinternalcapi.pyd", "_testbuffer.pyd", "_testclinic.pyd",
            "_testconsole.pyd", "_testimportmultiple.pyd", "_testmultiphase.pyd",
            "_testsinglephase.pyd", "_ctypes_test.pyd", "_msi.pyd", "_wmi.pyd", "_zoneinfo.pyd"}

BAT = """@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem 用 pythonw 无窗口启动服务器：不弹控制台、不在任务栏占用按钮，
rem 进程驻留系统托盘通知区（默认收在右下角小箭头里，展开可见 RNGdle 图标），
rem 左键单击托盘图标可打开配置页，右键菜单可退出服务。
start "" "%~dp0runtime\\pythonw.exe" "%~dp0rngdle_server.py"
rem 延迟 2 秒后在默认浏览器打开配置页；打开动作放后台最小化 cmd 执行，本窗口立即退出。
start "" /min cmd /c "timeout /t 2 /nobreak >nul & start "" http://127.0.0.1:8765/"
exit /b
"""

README_TXT = """RNGdle 本地计分器 —— 免安装使用说明
================================================

本压缩包在 Windows 10/11 上开箱可用，不需要安装 Python、编译器或任何运行库。

一、只算一个数字
    双击 rngdle_score.exe，输入 0~1000000 的数字回车，
    会生成 rngdle_result.html 并用默认浏览器打开结果页。

二、本地服务器 + 配置页（推荐）
    双击 启动服务器.bat，浏览器会打开 http://127.0.0.1:8765/ 的配置页：
      · 选抽取模式（随机区间 / 候选列表 / 固定数字 / EP 区间 / 指定等级）
      · 保存配置 → 点 GENERATE 按配置抽一次
      · 结果页右上角 ✕ 返回配置页；「跳过动画」直接看最终结果
    启动后没有任何窗口弹出：服务器静默驻留系统托盘通知区
    （默认收在任务栏右下角的小箭头里，展开可见 RNGdle 图标），
    左键单击托盘图标打开配置页，右键菜单可退出服务。
    想在前台运行看日志，可手动启动：
        runtime\\python.exe rngdle_server.py
    说明：
      · EP 区间 / 指定等级两种模式首次使用会扫描全部 100 万个数字建立索引，
        约 40 秒（只做一次，缓存在 ep_index.bin，约 20MB，可随时删除重建）。
      · 数值可以写成 1e7、1.5e6 这种形式，界面会实时显示解析结果。
      · 想换端口：先执行 set RNGDLE_PORT=8770 再启动。

三、在官网抽取时用本地结果（Chrome 扩展）
    1) 先按上面方式启动本地服务器
    2) Chrome 打开 chrome://extensions → 右上角打开「开发者模式」
       →「加载已解压的扩展程序」→ 选择本目录下的 rngdle-intercept 文件夹
    3) 打开 https://www.rngdle.com/ 点 GENERATE：
       结果由本地程序生成并覆盖展示，右上角 ✕ 可关闭并再次抽取。

四、文件说明
    rngdle_score.exe      计分器（单文件）
    rngdle_server.py      本地服务器（用 runtime\\python.exe 运行）
    runtime\\              便携 Python 运行时（免安装）
    rngdle-intercept\\     Chrome 扩展（manifest.json / content.js / background.js）
    config.example.json   配置模板；首次运行会生成 config.json
    ep_index.bin          全量 EP 索引缓存（自动生成）
    rngdle_result.html    结果页（每次运行覆盖）

五、常见问题
    · 杀毒软件提示：静态链接的 exe 偶尔会被误报，加白名单即可。
    · 提示无法连接本地服务器：确认本地服务器窗口还开着（端口 8765）。
    · 官网改版后扩展可能失效：可先只用方式二在配置页抽取。
"""


def size_of(path):
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            total += os.path.getsize(os.path.join(root, f))
    return total


def copy_py_runtime(dst):
    os.makedirs(dst, exist_ok=True)
    # python 主 DLL 不写死版本号：动态拷贝 python*.dll（python3.dll / python312.dll / python314.dll 等），
    # 避免便携源是 3.14 却只拷 python312.dll 导致 runtime 缺 DLL、python.exe 无法启动。
    for f in ("python.exe", "pythonw.exe", "python3.dll",
              "vcruntime140.dll", "vcruntime140_1.dll", "LICENSE.txt"):
        s = os.path.join(PYSRC, f)
        if os.path.exists(s):
            shutil.copy2(s, dst)
    import glob
    for s in glob.glob(os.path.join(PYSRC, "python*.dll")):
        shutil.copy2(s, dst)
    dllsrc, dlldst = os.path.join(PYSRC, "DLLs"), os.path.join(dst, "DLLs")
    os.makedirs(dlldst, exist_ok=True)
    for f in os.listdir(dllsrc):
        s = os.path.join(dllsrc, f)
        if f in SKIP_DLL or not os.path.isfile(s):
            continue
        shutil.copy2(s, os.path.join(dlldst, f))
    libsrc, libdst = os.path.join(PYSRC, "Lib"), os.path.join(dst, "Lib")
    os.makedirs(libdst, exist_ok=True)
    for name in os.listdir(libsrc):
        s = os.path.join(libsrc, name)
        if name in SKIP_LIB:
            continue
        d = os.path.join(libdst, name)
        if os.path.isdir(s):
            shutil.copytree(s, d, dirs_exist_ok=True)
        elif os.path.isfile(s):
            shutil.copy2(s, d)


def zip_dir(src_dir, zip_path):
    """用 zipfile 打包（条目分隔符为 /，非 ASCII 文件名按 UTF-8 标记，符合 ZIP 规范）"""
    root_name = os.path.basename(src_dir)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for root, _, files in os.walk(src_dir):
            for f in files:
                full = os.path.join(root, f)
                arc = root_name + "/" + os.path.relpath(full, src_dir).replace(os.sep, "/")
                z.write(full, arc)


def main():
    if not os.path.isdir(PYSRC):
        sys.exit("找不到 Python 运行时目录：%s（可用 RNGDLE_PYSRC 指定）" % PYSRC)
    if os.path.exists(REL):
        shutil.rmtree(REL)
    os.makedirs(REL)

    print("便携运行时来源：%s" % PYSRC)
    rt = os.path.join(REL, "runtime")
    copy_py_runtime(rt)
    print("  runtime %.1f MB" % (size_of(rt) / 1048576.0))

    for f in ("rngdle_score.exe", "rngdle_server.py", "config.example.json", "README.md"):
        shutil.copy2(os.path.join(BASE, f), os.path.join(REL, f))
    shutil.copytree(os.path.join(BASE, "rngdle-intercept"), os.path.join(REL, "rngdle-intercept"))

    with open(os.path.join(REL, "启动服务器.bat"), "w", encoding="utf-8", newline="\r\n") as f:
        f.write(BAT)
    with open(os.path.join(REL, "使用说明.txt"), "w", encoding="utf-8-sig", newline="\r\n") as f:
        f.write(README_TXT)

    zip_dir(REL, ZIP)
    print("目录：%s（%.1f MB）" % (REL, size_of(REL) / 1048576.0))
    print("压缩包：%s（%.1f MB）" % (ZIP, os.path.getsize(ZIP) / 1048576.0))


if __name__ == "__main__":
    main()
