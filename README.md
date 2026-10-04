# RNGdle 本地计分器

把 [rngdle.com](https://www.rngdle.com/) 的计分规则（徽章判定 / 家族覆盖 / EP 求和 / 百分位与等级）
离线复刻成单文件程序，并提供本地结果页、配置页与浏览器插件。

**Windows 10/11 免安装**：发布包内所有组件都已打包好，不需要安装 Python、MinGW/g++ 或任何运行库。

## 功能

| 组件 | 说明 |
|---|---|
| `rngdle_score.exe` | 单文件计分器（静态链接，只依赖系统自带的 UCRT）。输入 0~1000000，输出 EP / 百分位 / 等级 / 徽章列表，并生成与官网同构的抽奖动画结果页 |
| 结果页 `rngdle_result.html` | 复刻官网视觉与动画：数字滚动逐位定格、徽章自下而上逐个弹出（每枚弹出时播放一次高亮数字展示）、EP 累加、等级与百分位揭示、七档稀有度配色、暗色主题、分享文案复制 |
| `rngdle_server.py` | 本地服务器（默认 `http://127.0.0.1:8765/`）：**配置页**可改抽取模式并写回 `config.json`，`GENERATE` 按配置抽一次；结果页右上角有 ✕ 与「跳过动画」 |
| `rngdle-intercept/` | Chrome 扩展（MV3）：在官网点 `GENERATE` 时改为展示本地结果页，可无限抽取、可跳过动画 |

## 抽取模式（配置页 / `config.json`）

| mode | 含义 | 相关字段 |
|---|---|---|
| `random` | 在 `min`~`max` 内均匀随机 | `min`、`max` |
| `list` | 在候选列表里随机取一个 | `list` |
| `fixed` | 每次都抽这一个数字 | `fixed` |
| `ep_range` | **抽到的 EP 永远落在给定区间内** | `epMin`、`epMax` |
| `tier` | **永远抽到指定等级**（trash/common/uncommon/rare/epic/anomaly/mythic） | `tier` |

- 所有数值字段都接受 `1e7`、`1.5e6`、`1_000_000` 这类写法，落盘时统一写成整数；配置页会实时回显解析结果。
- `ep_range` / `tier` 需要先知道 0~1000000 每个数字的 EP，所以**首次使用会自动建立全量索引**
  （调用 `rngdle_score.exe --batch` 扫描一遍，约 40 秒，结果缓存在 `ep_index.bin` ≈ 20MB）。
  索引与 exe 指纹绑定，重新编译 exe 后会自动重建；平时抽取是 O(1) 的。
- `animation: false` 时生成无抽奖动画的静态结果页。

## 快速开始

### 方式一：只要一个数（最简单）

双击 `rngdle_score.exe`，输入数字回车，浏览器会打开结果页。

### 方式二：本地服务器 + 配置页（推荐）

双击 **`启动服务器.bat`**（它用自带的便携运行时启动服务器；也可手动执行 `runtime\python.exe rngdle_server.py`），
浏览器会打开 `http://127.0.0.1:8765/` 的配置页：选模式 → 填参数 → 保存配置 → 点 `GENERATE`。

- 端口可用环境变量改：`set RNGDLE_PORT=8770`
- 可用端点：`GET /`（配置页）、`GET/POST /config`（读写配置）、`GET /index`（EP 索引状态）、
  `GET /generate`（按配置抽一次）、`GET /?num=123456`（指定数字）
- 另有一种可选模式：设 `RNGDLE_PROXY=1` 时改为代理官网真实页面并注入拦截脚本

### 方式三：在官网上用插件抽

1. Chrome 打开 `chrome://extensions` → 打开右上角「开发者模式」→「加载已解压的扩展程序」→ 选择 `rngdle-intercept` 文件夹
2. 先启动本地服务器（方式二）
3. 打开 <https://www.rngdle.com/>，点 `GENERATE`：结果由本地程序生成并覆盖展示，右上角 ✕ 返回官网可再抽，「跳过动画」直接看最终结果

> 取数由扩展的 service worker 发起，因此不受 Chrome 本地网络访问（LNA）权限与页面 CORS 限制。

## 文件说明

```
rngdle_score.exe        计分器（单文件，免安装）
rngdle_server.py        本地服务器（用 runtime\python.exe 运行）
runtime\                便携 Python 运行时（服务器需要；只跑计分器可以不装）
rngdle-intercept\       Chrome 扩展：manifest.json / content.js / background.js
config.example.json     配置模板；首次运行会按默认值生成 config.json（本地文件，不必提交）
ep_index.bin            全量 EP 索引缓存（自动生成，可随时删除）
rngdle_result.html      结果页（每次运行覆盖）
```

源码仓库里的 `rngdle_score.cpp` 是计分器全部逻辑（含内联的 60392 组百分位数据、210 枚徽章定义、
结果页 HTML/CSS/JS 生成器）；`rngdle_server.py` 是本地服务器与配置页。

## 免安装发布包

`python make_release.py` 会组装出 `release/RNGdle/` 并生成 `RNGdle_standalone_v<版本>_win64.zip`：
内含上面的文件、便携 CPython 运行时与一键启动脚本，**目标机器无需安装 Python、编译器或任何运行库**
（`rngdle_score.exe` 静态链接，只依赖 Windows 自带的 UCRT）。

## 从源码构建

计分器（MinGW-w64 g++，静态链接以便免安装分发）：

```bash
g++ -static -O2 -std=c++11 rngdle_score.cpp -o rngdle_score.exe -lwinmm -lpthread -lshell32
```

本地服务器需要 Python 3.8+（只用标准库，无需第三方包）：`python rngdle_server.py`。

## 已知限制

- 仅支持 Windows：计分器是 Windows 程序，服务器也要调用它（且用了 Windows 专有的 `CREATE_NO_WINDOW`）。
- 结果页的计分规则、徽章文案、配色与动画时序按 2026-10 抓取的官网前端源码复刻；官网改版后可能不一致。
- 插件的点击判定依赖官网 `GENERATE` 按钮的语义（`button`/`[role=button]`/`a[href]`，自身短文本或 `aria-label` 含 generate）；官网改版后可能需要更新选择器。
- `ep_range` / `tier` 的首次索引建立约 40 秒（一次性）。
- 静态链接的 exe 在部分杀软下可能被误报，必要时加白名单。

## 许可与致谢

计分规则、徽章名称与文案、视觉配色均来自 [rngdle.com](https://www.rngdle.com/) 的前端实现，本仓库为离线复刻与本地化用途。
