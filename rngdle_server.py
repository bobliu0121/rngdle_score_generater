# -*- coding: utf-8 -*-
"""RNGdle 本地结果服务器

作用：调用 rngdle_score.exe 生成结果页（含官网同款抽奖动画），供浏览器取用。
端点：
  GET /              -> 本地配置页（编辑 config.json 的抽取模式/区间/列表/固定值/动画开关，
                         保留 GENERATE 按钮与倒计时；设环境变量 RNGDLE_PROXY=1 时改为
                         代理官网真实页面并注入拦截脚本）
  GET /config        -> 返回当前 config.json 原文（供配置页回填）
  POST /config       -> 校验并写回 config.json（JSON body；失败返回 400 与原因）
  GET /generate      -> 按 config.json 设定抽取数字，调 exe 生成结果页并返回完整 HTML
  GET /?num=<数字>   -> 指定数字生成结果页
  GET /sounds/*      -> 音效文件夹 sounds/ 下的静态音频文件（抽取完成音效）
  GET /sound_dirs    -> 音效子文件夹权重与音效数（配置页编辑权重用）
  POST /sound_dirs   -> 写回 sounds/权重.txt（JSON body：{"weights": {"文件夹": 权重}}）
抽取配置 config.json（mode：random/range/list/fixed/ep_range/tier；min/max、list、fixed；
ep_range 用 epMin/epMax 锁定 EP 区间，tier 用 tier 锁定等级，两者需要全量 EP 索引
ep_index.bin（首次自动用 exe --batch 扫描约 40 秒并缓存）；animation：false 时生成无抽奖动画的静态结果页；
playSound：true 时在抽取动画播完（或点击跳过动画）随机播放 sounds/ 目录中的音效，刷新恢复不播放）。
所有数值字段都接受 1e7 / 1.5e6 / 1_000_000 这类写法，落盘时统一写成整数。
config.json 属于本地运行时文件（已在 .gitignore 里，配置页每次保存都会重写它）：
缺失时用内置默认值（随机 0~999999），启动时会按默认值生成一份；仓库里的模板见 config.example.json。
启动：python rngdle_server.py  （默认端口 8765，Ctrl+C 退出）
"""
import os, random, subprocess, sys, time, json, threading, struct, bisect, re, tempfile, shutil
from array import array
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

# 打包成单文件 exe（PyInstaller）后 __file__ 指向临时解包目录，必须改用 exe 自身所在目录；
# 这样 config.json / ep_index.bin / rngdle_result.html 与 rngdle_score.exe 都在 exe 旁边
if getattr(sys, "frozen", False):
    BASE = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE = os.path.dirname(os.path.abspath(__file__))
EXE = os.path.join(BASE, "rngdle_score.exe")
RESULT = os.path.join(BASE, "rngdle_result.html")
CONFIG = os.path.join(BASE, "config.json")
INDEX = os.path.join(BASE, "ep_index.bin")   # 全量 EP 索引缓存（exe 变化后自动重建）
PORT = int(os.environ.get("RNGDLE_PORT", "8765") or "8765")   # 可用环境变量 RNGDLE_PORT 改端口
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
_site_cache = {"t": 0.0, "html": None}
_site_cache_lock = threading.Lock()   # 保护 _site_cache 的并发读/写（ThreadingHTTPServer 多线程）

# 等级（与 rngdle_score.cpp 的 cardTier 一致，按 EP 百分位分档）
TIERS = ("trash", "common", "uncommon", "rare", "epic", "anomaly", "mythic")
TIER_CODE = dict((t, i) for i, t in enumerate(TIERS))
INDEX_N = 1000001          # 扫描 0..1000000
INDEX_MAGIC = b"RNGDLEIDX1"

# 注入到官网首页的拦截脚本：点 GENERATE 后改请求本地 /generate，全屏展示本地结果页（含抽奖动画）
# 用 r""" 原始字符串：脚本里有 JS 正则 \s，普通字符串会触发 Python 的无效转义警告
INJECT_SCRIPT = r"""<script>
(() => {
  "use strict";
  // 官网 Next.js 会把 URL 规范化为 https://www.rngdle.com/ 并 history.replaceState，
  // 在 localhost 下跨 origin 会抛 SecurityError 导致整页报错；这里把官网 URL 改写为当前 origin。
  (function () {
    var orig = "https://www.rngdle.com/";
    var cur = location.origin + "/";
    var rep = history.replaceState.bind(history);
    var pus = history.pushState.bind(history);
    function fix(u) {
      // Next.js 可能传 URL 对象而非字符串：统一 String() 后再判定
      var s = (u === null || u === undefined) ? u : String(u);
      if (typeof s === "string") {
        if (s.indexOf(orig) === 0) return cur + s.slice(orig.length);
        if (s.indexOf(orig.slice(0, -1)) === 0) return cur + s.slice(orig.length - 1);
        if (s === "https://www.rngdle.com") return location.origin + "/";
      }
      return u;
    }
    history.replaceState = function (s, t, u) { return rep(s, t, fix(u)); };
    history.pushState = function (s, t, u) { return pus(s, t, fix(u)); };
    // ServiceWorker 脚本来自官网域名，跨源注册必然失败并抛 unhandled rejection，
    // 会触发 Next.js 全局错误边界；短路为静默成功。
    if (navigator.serviceWorker && navigator.serviceWorker.register) {
      navigator.serviceWorker.register = function () {
        return Promise.resolve({ active: null, installing: null, waiting: null });
      };
    }
  })();
  let busy = false;
  // 只有点中 GENERATE 按钮（按钮自身或其内部元素）才算数。
  // 注意：绝不能把「closest 取不到按钮时的被点元素」当按钮来判——点页面空白处时目标是
  // <body>/大容器，它们的 textContent 里含按钮文字 "GENERATE"，会导致整页任意位置都被拦截。
  function isGen(el) {
    if (!el || el.nodeType !== 1 || typeof el.closest !== "function") return false;
    const b = el.closest('button, [role="button"], a[href]');
    if (!b) return false; // 不在按钮/链接里，一律不拦截
    const norm = (s) => (s || "").replace(/\s+/g, " ").trim().toLowerCase();
    // 只认按钮自身的文字，且要求很短（真按钮的标签就几个字），避免容器的整段文本命中
    const t = norm(b.textContent);
    const textHit = t.length > 0 && t.length <= 20 && t.indexOf("generate") >= 0;
    const ariaHit = norm(b.getAttribute && b.getAttribute("aria-label")).indexOf("generate") >= 0;
    const dtHit = norm(b.getAttribute && b.getAttribute("data-testid")).indexOf("generate") >= 0;
    // class/id 只在真按钮上按词匹配，避免 <a id="generate-xxx"> 这类链接容器误命中
    const isBtnLike = b.tagName === "BUTTON" || (b.getAttribute && b.getAttribute("role") === "button");
    // 按词匹配，避免 "generated-xxx" 这类误命中
    const word = /(^|[^a-z])generate([^a-z]|$)/;
    const cls = typeof b.className === "string" ? b.className.toLowerCase() : "";
    const id = (b.id || "").toLowerCase();
    return textHit || ariaHit || dtHit || (isBtnLike && (word.test(cls) || word.test(id)));
  }
  function show(html) {
    const old = document.getElementById("rngdle-local-frame");
    if (old) old.remove();
    const f = document.createElement("iframe");
    f.id = "rngdle-local-frame";
    f.style.cssText = "position:fixed;inset:0;width:100vw;height:100vh;border:0;z-index:2147483647;background:#fff;";
    document.documentElement.appendChild(f);
    f.srcdoc = html;
  }
  function notice(m) {
    const d = document.createElement("div");
    d.textContent = m;
    d.style.cssText = "position:fixed;bottom:24px;left:50%;transform:translateX(-50%);z-index:2147483647;background:#111;color:#fff;padding:12px 20px;border-radius:10px;font:14px Arial,sans-serif;box-shadow:0 4px 16px rgba(0,0,0,.3);";
    document.body.appendChild(d);
    setTimeout(() => d.remove(), 7000);
  }
  document.addEventListener("click", async (e) => {
    if (busy || !isGen(e.target)) return;
    busy = true;
    e.preventDefault();
    e.stopPropagation();
    e.stopImmediatePropagation();
    try {
      const r = await fetch(location.origin + "/generate", { cache: "no-store" });
      if (!r.ok) throw new Error("HTTP " + r.status);
      show(await r.text());
    } catch (err) {
      notice("无法连接本地服务器（" + err.message + "），请确认 rngdle_server.py 已运行。");
    } finally {
      busy = false;
    }
  }, true);
})();
</script>"""

# ---------------------------------------------------------------------------
# 抽取完成音效：sounds/ 目录（与 rngdle_score.exe 同目录）里放任意数量的音频文件
# （mp3/wav/ogg/m4a/flac/aac，可放子目录），config 里 playSound=true 时，
# 抽取动画播完（或点击跳过动画）随机播放一个；刷新恢复终态页面时不播放。
# ---------------------------------------------------------------------------
SND_DIR = os.path.join(BASE, "sounds")
SOUND_EXTS = (".mp3", ".wav", ".ogg", ".m4a", ".flac", ".aac")
WEIGHTS_FILE = os.path.join(SND_DIR, "权重.txt")


def load_weights():
    """解析 sounds/权重.txt 的子文件夹权重：每行 `文件夹名=权重`（权重为数字），
    # 开头为注释，空行忽略，解析失败的行跳过。返回 {文件夹名: 权重}；
    文件不存在或没有任何有效行时返回空 dict（全部目录等权）。"""
    w = {}
    try:
        with open(WEIGHTS_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                name, sep, vs = line.rpartition("=")
                name = name.strip()
                vs = vs.strip()
                if not sep or not name:
                    continue
                try:
                    v = float(vs)
                except ValueError:
                    continue
                w[name] = v
    except FileNotFoundError:
        pass
    return w


def list_sounds():
    """递归扫描 sounds/ 下音效文件并按子文件夹分组，返回播放池结构：
    [{"name": 子文件夹名, "weight": 权重, "urls": ["/sounds/...", ...]}, ...]
    - name 为顶层子文件夹名（如"网络流行梗类"）；sounds/ 根目录下的文件归入 name=""。
    - weight 取 sounds/权重.txt 中配置值；未配置的文件夹默认 1；权重 <= 0 的文件夹整组排除。
    - 每组 urls 按路径排序。"""
    groups = {}
    if os.path.isdir(SND_DIR):
        for root, _dirs, files in os.walk(SND_DIR):
            for fn in sorted(files):
                if fn.lower().endswith(SOUND_EXTS):
                    rel = os.path.relpath(os.path.join(root, fn), SND_DIR).replace("\\", "/")
                    parts = rel.split("/")
                    name = parts[0] if len(parts) > 1 else ""
                    groups.setdefault(name, []).append("/sounds/" + rel)
    weights = load_weights()
    pool = []
    for name in sorted(groups):
        w = weights.get(name, 1)
        if w <= 0:
            continue  # 权重 <= 0：整组不参与抽取
        pool.append({"name": name, "weight": w, "urls": groups[name]})
    return pool


# 注入到结果页 <body> 末尾的音效脚本。SOUNDS 为播放池结构（含子文件夹名、权重、URL 列表），
# ENABLED 为布尔，由服务器在返回结果页时填好（%s 占位）。三种路径统一以 DOM 属性
# data-rngdle-anim 是否变成 done 作为触发点：
#   · 动画自然播完 → 脚本 after(markDone, t+150) 置 done；
#   · 点击跳过动画 → finish() 内部同样调用 markDone() 置 done；
#   · 刷新/恢复终态页面 → 加载时 data-rngdle-anim 已是 done，视为已播放过、不再播。
# 播放时在页面左上角浮层显示音效文件名（去掉扩展名），点击浮层可重新播放该音效；
# 浮层 5 秒后自动淡出，点击重播会重置计时。
SOUND_INJECT = """<script>
(function () {
  "use strict";
  var SOUNDS = __RNGDLE_SOUNDS__;
  var ENABLED = __RNGDLE_ENABLED__;
  if (!ENABLED || !SOUNDS || !SOUNDS.length) return;
  if (typeof Audio === "undefined") return;
  var played = false;   // 是否已自动播放过
  var replayReady = false; // 恢复场景是否已就绪一个可点击重播的音效
  var de = document.documentElement;
  // 恢复模式：父页面刷新恢复上次结果时注入 data-rngdle-recover=1。
  // 该场景不自动播放，但左上角浮层常驻显示音效名，点击后仍可播放。
  var recoverMode = !!(de && de.getAttribute("data-rngdle-recover") === "1");
  // 已自动播放过判定：仅当「加载时动画已是终态 done」且非恢复模式时视为已播过
  // （静态终态页直接打开：不自动播、也不显示浮层）。
  if (!recoverMode && de && de.getAttribute("data-rngdle-anim") === "done") played = true;
  // 静音预热：Chrome 自动播放策略下静音播放始终允许，让音频引擎先就绪；
  // 用户点击过页面（域名已解锁）后，动画播完时的正式播放基本不会被拒。
  try {
    var warmUrl = SOUNDS[0] && SOUNDS[0].urls && SOUNDS[0].urls[0];
    if (warmUrl) {
      var warm = new Audio(warmUrl);
      warm.muted = true;
      warm.volume = 0;
      var wp = warm.play();
      if (wp && wp.catch) wp.catch(function () {});
    }
  } catch (e) {}
  var curAudio = null;
  // 浮层随系统主题变色：深色系统黑底白字，浅色系统白底黑字
  var darkMode = !!(window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches);
  var banner = document.createElement("div");
  banner.style.cssText = "position:fixed;top:64px;left:12px;z-index:99999;display:flex;" +
    "align-items:center;gap:7px;max-width:calc(100vw - 24px);padding:7px 13px;" +
    "border-radius:999px;cursor:pointer;user-select:none;" +
    "font:500 14px/1.4 system-ui,-apple-system,'Segoe UI',sans-serif;" +
    (darkMode ? "color:#fff;background:rgba(15,23,42,.85);" :
                "color:#111;background:rgba(255,255,255,.95);border:1px solid #d1d5db;") +
    "box-shadow:0 3px 12px rgba(0,0,0,.3);" +
    "opacity:0;pointer-events:none;transform:translateY(-8px);" +
    "transition:opacity .25s ease,transform .25s ease;";
  banner.title = "点击重新播放";
  var icon = document.createElement("span");
  icon.style.cssText = "display:inline-block;width:0;height:0;flex:none;" +
    "border-left:9px solid " + (darkMode ? "#fff" : "#111") +
    ";border-top:5px solid transparent;border-bottom:5px solid transparent;";
  var nameEl = document.createElement("span");
  nameEl.style.cssText = "white-space:nowrap;overflow:hidden;text-overflow:ellipsis;";
  banner.appendChild(icon);
  banner.appendChild(nameEl);
  document.body.appendChild(banner);
  // 浮层常驻：动画播放完后始终显示音效名（此时"跳过动画"按钮已隐藏，浮层即常驻入口），
  // 点击浮层重新播放；刷新恢复场景同样常驻显示。
  function showBanner(name) {
    nameEl.textContent = name;
    banner.style.opacity = "1";
    banner.style.pointerEvents = "auto";
    banner.style.transform = "translateY(0)";
  }
  banner.addEventListener("click", function () {
    if (curAudio) {
      try { curAudio.currentTime = 0; curAudio.play(); } catch (e) {}
      showBanner(nameEl.textContent);
    }
  });
  // 加权随机：先按权重选子文件夹，再从该文件夹内随机选一个音效
  function pickUrl() {
    var total = 0, i;
    for (i = 0; i < SOUNDS.length; i++) total += SOUNDS[i].weight;
    var r = Math.random() * total;
    for (i = 0; i < SOUNDS.length; i++) {
      r -= SOUNDS[i].weight;
      if (r < 0) {
        var us = SOUNDS[i].urls;
        return us[Math.floor(Math.random() * us.length)];
      }
    }
    var fallback = SOUNDS[0] && SOUNDS[0].urls;
    return fallback && fallback[0];
  }
  // 记录"本次抽取实际播放的音效"，供刷新恢复场景沿用同一个（sessionStorage 同源共享，
  // 与父页面的 rngdle_local_roll 同生命周期，点 × 关闭后自然作废）。
  function rememberSound(url) {
    try { sessionStorage.setItem("rngdle_last_sound", url); } catch (e) {}
  }
  function playOne() {
    if (played) return;
    played = true;
    var url = pickUrl();
    if (!url) return;
    rememberSound(url);
    // 立即显示浮层（不依赖播放成功）：动画播完后音效标签始终显示
    showBanner(url.split("/").pop().replace(/\\.[^.]+$/, ""));
    try {
      var a = new Audio(url);
      curAudio = a;
      // Chrome 自动播放策略：非静音 play() 在无用户手势时会被拒（NotAllowedError）。
      // 静音自动播放始终允许——先以静音启动，播放真正开始后再解除静音出声，
      // 从而绕开手势限制，动画播完 15 秒后仍能正常播放。
      a.muted = true;
      var p = a.play();
      if (p && p.then) {
        p.then(function () { a.muted = false; a.volume = 0.8; }).catch(function () {
          // 极少数策略下连静音启动也被拒：等用户下一次点击页面任意处时补播
          document.addEventListener("click", function once() {
            a.muted = false;
            a.volume = 0.8;
            try { a.play(); } catch (e2) {}
          }, { once: true, passive: true });
        });
      }
    } catch (e) {}
  }
  // 恢复场景：沿用刷新前那次抽取播放的音效（sessionStorage 记录），没有记录时才随机选；
  // 浮层常驻显示音效名、不自动播放，点击浮层时播放。
  function prepareReplay() {
    if (replayReady) return;
    replayReady = true;
    var url = null;
    try { url = sessionStorage.getItem("rngdle_last_sound"); } catch (e) {}
    if (!url) url = pickUrl();
    if (!url) return;
    try {
      curAudio = new Audio(url);
    } catch (e) { return; }
    showBanner(url.split("/").pop().replace(/\\.[^.]+$/, ""));
  }
  try {
    var mo = new MutationObserver(function () {
      var d2 = document.documentElement;
      if (d2 && d2.getAttribute("data-rngdle-anim") === "done") {
        if (recoverMode) prepareReplay();
        else playOne();
      }
    });
    mo.observe(document.documentElement, { attributes: true, attributeFilter: ["data-rngdle-anim"] });
    // 边界：恢复场景若加载时已是 done（跳过发生在监听器注册前），立即显示浮层
    if (recoverMode) {
      var d2 = document.documentElement;
      if (d2 && d2.getAttribute("data-rngdle-anim") === "done") prepareReplay();
    }
    // 静态结果页（配置里 animation=false，无动画脚本）：没有 data-rngdle-skip 标记、
    // 也不会有 data-rngdle-anim 的 done 事件可等——加载后直接播放音效并显示浮层
    if (!recoverMode && !played && de && de.getAttribute("data-rngdle-skip") !== "1") {
      playOne();
    }
  } catch (e) {}
})();
</script>"""


# config.json 是纯本地运行时文件（已 gitignore），仓库里只有同样内容的模板 config.example.json。
# 文件缺失时一律用这份温和默认值：随机 0~999999；服务器启动时会据此生成 config.json 方便手工编辑。
DEFAULT_CONFIG = {"mode": "random", "min": 0, "max": 999999, "list": [], "fixed": None,
                  "animation": True, "playSound": False, "epMin": 1000000, "epMax": 10000000, "tier": "mythic"}


def read_config():
    """读取 config.json 原文（不校验，便于配置页提示无效值）；返回 (cfg, 错误信息)。

    文件不存在时返回默认配置（错误信息为 None），这样首次运行的配置页也能正常回填；
    文件存在但内容坏了才报错，交由页面提示用户修。
    """
    try:
        with open(CONFIG, encoding="utf-8") as f:
            return json.load(f), None
    except FileNotFoundError:
        return dict(DEFAULT_CONFIG), None
    except Exception as e:
        return None, str(e)


def load_config():
    """读取 config.json；文件缺失或内容坏了都回退到 DEFAULT_CONFIG（随机 0~999999）"""
    cfg, _ = read_config()
    if isinstance(cfg, dict):
        return cfg
    return dict(DEFAULT_CONFIG)


MODES = ("random", "range", "list", "fixed", "ep_range", "tier")
# range 与 random 等价（代码里都走 min~max 随机）
# ep_range：EP 永远落在 [epMin, epMax]；tier：永远抽到指定等级（两者都需要全量 EP 索引）


def parse_int_text(value):
    """把 1e7 / 1.5e6 / 1_000_000 / 20000000 这类写法解析成整数；解析不了返回 None。

    只接受整数值（1.5e6=1500000 可以，1.5e0 不行），指数限制 ±18 防止溢出；
    纯数字串走 int() 精确解析，科学计数法走 float（1e7/1e9 这类短写法都不丢精度）。
    """
    if value is None:
        return None
    s = str(value).strip().replace("_", "").replace(",", "").replace(" ", "")
    if s == "":
        return None
    m = re.match(r"^([+-]?\d+(?:\.\d+)?)[eE]([+-]?\d+)$", s)
    if m:
        exp = int(m.group(2))
        if abs(exp) > 18:
            return None
        try:
            f = float(m.group(1)) * (10.0 ** exp)
        except (ValueError, OverflowError):
            return None
        if f != int(f) or abs(f) > 10 ** 15:
            return None
        return int(f)
    try:
        return int(s)
    except ValueError:
        return None


def _cfg_int(cfg, key, default):
    """从（可能被手工编辑过的）配置里取整数，允许 1e7 这类字符串写法"""
    v = parse_int_text(cfg.get(key, default))
    return default if v is None else v


def normalize_config(cfg):
    """校验并规范化配置页提交的 JSON；返回 (规范化后的配置, None) 或 (None, 错误信息)"""
    if not isinstance(cfg, dict):
        return None, "请求体需为 JSON 对象"
    mode = str(cfg.get("mode", "random")).strip().lower()
    if mode not in MODES:
        return None, "mode 只能是 %s" % "/".join(MODES)
    if mode == "range":
        mode = "random"
    try:
        lo = parse_int_text(cfg.get("min", 0))
        hi = parse_int_text(cfg.get("max", 999999))
    except Exception:
        lo = hi = None
    if lo is None or hi is None:
        return None, "min/max 需为整数，可写 1e6 这种形式"
    if not (0 <= lo <= 1000000 and 0 <= hi <= 1000000):
        return None, "min/max 需在 0~1000000 之间（最大可写 1e6）"
    if hi < lo:
        lo, hi = hi, lo
    raw = cfg.get("list", [])
    if raw is None:
        raw = []
    if isinstance(raw, str):
        raw = raw.replace("，", ",").replace(" ", ",").split(",")
    if not isinstance(raw, (list, tuple)):
        return None, "list 需为数组或逗号分隔的字符串"
    lst = []
    for item in raw:
        s = str(item).strip()
        if s == "":
            continue
        v = parse_int_text(s)
        if v is None:
            return None, "list 里出现解析不了的数值：%s（可写 1e3 这种形式）" % s
        if not (0 <= v <= 1000000):
            return None, "list 元素需在 0~1000000 之间：%s" % s
        lst.append(v)
    if mode == "list" and not lst:
        return None, "list 模式至少要填一个数字"
    fx = cfg.get("fixed", None)
    if fx is None or str(fx).strip() == "":
        fixed = None
    else:
        fixed = parse_int_text(fx)
        if fixed is None:
            return None, "fixed 需为整数，可写 1e6 这种形式"
        if not (0 <= fixed <= 1000000):
            return None, "fixed 需在 0~1000000 之间"
    if mode == "fixed" and fixed is None:
        return None, "fixed 模式必须填一个固定数字"
    # EP 区间模式：只接受非负整数，反了自动交换
    try:
        eplo = parse_int_text(cfg.get("epMin", 1000000))
        ephi = parse_int_text(cfg.get("epMax", 10000000))
    except Exception:
        eplo = ephi = None
    if eplo is None or ephi is None:
        return None, "EP 上下限需为整数，可写 1e7 / 1e9 这种形式"
    if eplo < 0 or ephi < 0 or ephi > 10 ** 12:
        return None, "EP 上下限需在 0~1e12 之间"
    if ephi < eplo:
        eplo, ephi = ephi, eplo
    # 等级模式
    tier = str(cfg.get("tier", "mythic")).strip().lower()
    if tier not in TIER_CODE:
        return None, "tier 只能是 %s" % "/".join(TIERS)
    # 键顺序与仓库里的 config.json 保持一致：mode/min/max/list/fixed/animation/playSound [+ 新模式字段]
    return {"mode": mode, "min": lo, "max": hi, "list": lst, "fixed": fixed,
            "animation": bool(cfg.get("animation", True)),
            "playSound": bool(cfg.get("playSound", False)),
            "epMin": eplo, "epMax": ephi, "tier": tier}, None


def write_config(cfg):
    """写回 config.json（先写临时文件再替换，避免写坏）"""
    tmp = CONFIG + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, CONFIG)


# ---------------------------------------------------------------------------
# 全量 EP 索引：ep_range（EP 永远落在给定区间）与 tier（永远抽到某个等级）两种模式
# 需要先知道 0..1000000 每个数字的 EP，所以用 exe 的 --batch 全量扫一遍（约 36 秒），
# 结果缓存到 ep_index.bin；exe 变化（大小/mtime）后指纹不符会自动重建。
# 结构：eps[数字]=EP(int64) | tiers[数字]=等级码(uint8) | order=按 EP 升序的数字(int32) | eps_sorted=对应 EP
# ---------------------------------------------------------------------------
_index_lock = threading.Lock()
_index_state = "none"        # none / building / ready / error
_index_error = None
_index_progress = 0
_index_data = None           # (eps, tiers, order, eps_sorted)


def _exe_fingerprint():
    st = os.stat(EXE)
    return "%d-%d-%d" % (st.st_size, int(st.st_mtime), INDEX_N)


def _try_load_cache():
    """尝试从 ep_index.bin 载入（需在 _index_lock 内调用）；成功返回 True 并置 ready"""
    global _index_state, _index_data, _index_error
    try:
        with open(INDEX, "rb") as f:
            if f.read(len(INDEX_MAGIC)) != INDEX_MAGIC:
                raise ValueError("缓存格式不符")
            (nlen,) = struct.unpack("<H", f.read(2))
            fp = f.read(nlen).decode("ascii")
            (n,) = struct.unpack("<I", f.read(4))
            if n != INDEX_N or fp != _exe_fingerprint():
                raise ValueError("缓存已过期（exe 或扫描范围变化）")
            eps = array("q")
            eps.frombytes(f.read(8 * n))
            tiers = bytearray(f.read(n))
            order = array("i")
            order.frombytes(f.read(4 * n))
            eps_sorted = array("q")
            eps_sorted.frombytes(f.read(8 * n))
        _index_data = (eps, tiers, order, eps_sorted)
        _index_state = "ready"
        _index_error = None
        return True
    except Exception:
        return False


def _build_worker():
    """后台线程：调 exe --batch 全量扫描 → 解析 → 落盘缓存 → 置 ready"""
    global _index_state, _index_error, _index_data, _index_progress
    csv_path = INDEX + ".csv.tmp"
    try:
        print("[index] 正在扫描 0~%d 的 EP（首次约 40 秒）…" % (INDEX_N - 1))
        _index_progress = 5
        with open(csv_path, "wb") as f:
            subprocess.run([EXE, "--batch", "0", str(INDEX_N - 1)], cwd=BASE, stdout=f,
                           stderr=subprocess.DEVNULL,
                           creationflags=subprocess.CREATE_NO_WINDOW, timeout=3600)
        _index_progress = 70
        eps = array("q", bytes(8 * INDEX_N))
        tiers = bytearray(INDEX_N)
        with open(csv_path, "rb") as f:
            while True:
                line = f.readline()
                if not line:
                    break
                p = line.split(b",")
                if len(p) < 4:
                    continue
                try:
                    n = int(p[0])
                except ValueError:
                    continue
                if 0 <= n < INDEX_N:
                    eps[n] = int(p[1])
                    tiers[n] = TIER_CODE.get(p[3].strip().decode("ascii", "replace"), 255)
        try:
            os.remove(csv_path)
        except OSError:
            pass
        _index_progress = 85
        order = array("i", sorted(range(INDEX_N), key=eps.__getitem__))
        eps_sorted = array("q", (eps[k] for k in order))
        _index_progress = 92
        tmp = INDEX + ".tmp"
        with open(tmp, "wb") as f:
            fp = _exe_fingerprint().encode("ascii")
            f.write(INDEX_MAGIC)
            f.write(struct.pack("<H", len(fp)))
            f.write(fp)
            f.write(struct.pack("<I", INDEX_N))
            f.write(eps.tobytes())
            f.write(bytes(tiers))
            f.write(order.tobytes())
            f.write(eps_sorted.tobytes())
        os.replace(tmp, INDEX)
        with _index_lock:
            _index_data = (eps, tiers, order, eps_sorted)
            _index_state = "ready"
            _index_error = None
            _index_progress = 100
        tp = eps_sorted[-1]
        counts = {}
        for c in tiers:
            counts[c] = counts.get(c, 0) + 1
        dist = "  ".join("%s=%d" % (TIERS[c], counts.get(c, 0))
                         for c in range(len(TIERS)))
        print("[index] 完成：%d 个数字，EP 范围 %d~%d，等级分布 %s"
              % (INDEX_N, eps_sorted[0], tp, dist))
    except Exception as e:
        with _index_lock:
            _index_state = "error"
            _index_error = str(e)
        print("[index] 建立失败：%s" % e)


def ensure_index(timeout=1800):
    """确保索引可用（必要时建立并等待）；返回 (ok, 错误信息)"""
    global _index_state
    with _index_lock:
        if _index_state == "ready":
            return True, None
        if _index_state == "error":
            return False, _index_error
        if _index_state == "none":
            if _try_load_cache():
                return True, None
            _index_state = "building"
            threading.Thread(target=_build_worker, daemon=True).start()
    deadline = time.time() + timeout
    while time.time() < deadline:
        with _index_lock:
            if _index_state == "ready":
                return True, None
            if _index_state == "error":
                return False, _index_error
        time.sleep(0.2)
    return False, "建立 EP 索引超时（%d 秒）" % timeout


def start_index_build():
    """让后台尽快开始建立索引（服务器启动时调用，不阻塞）。
    锁内判断 _index_state 并优先复用缓存，避免多线程下重复启动 worker /
    读到过期状态；ensure_index 内部同样持锁，二者不会并发建两份索引。"""
    with _index_lock:
        if _index_state in ("building", "ready"):
            return
        if _index_state == "none" and _try_load_cache():
            return
    threading.Thread(target=ensure_index, daemon=True).start()


def pick_by_ep_range(lo, hi):
    """在 EP 落在 [lo, hi] 的数字里均匀随机取一个（区间内没有数字时报错）"""
    ok, err = ensure_index()
    if not ok:
        raise RuntimeError(err)
    eps, tiers, order, eps_sorted = _index_data
    a = bisect.bisect_left(eps_sorted, lo)
    b = bisect.bisect_right(eps_sorted, hi)
    if b <= a:
        raise RuntimeError("EP 区间 [%d, %d] 里没有数字，请放宽区间" % (lo, hi))
    return int(order[random.randrange(a, b)])


def pick_by_tier(tier):
    """在指定等级的数字里均匀随机取一个（按索引拒绝采样，等级不会出现时报错）"""
    ok, err = ensure_index()
    if not ok:
        raise RuntimeError(err)
    eps, tiers, order, eps_sorted = _index_data
    code = TIER_CODE.get(tier)
    if code is None:
        raise RuntimeError("未知等级：%s" % tier)
    for _ in range(5000000):
        n = random.randrange(INDEX_N)
        if tiers[n] == code:
            return n
    raise RuntimeError("等级 %s 在 0~%d 里没有数字" % (tier, INDEX_N - 1))


def pick_number(cfg):
    """按配置抽取数字：fixed / list / ep_range / tier / random(range)

    数值一律用 _cfg_int 读，允许 config.json 里手写成 "1e7" 这类字符串。
    """
    mode = cfg.get("mode", "random")
    fixed = parse_int_text(cfg.get("fixed"))
    if mode == "fixed" and fixed is not None:
        return fixed
    if mode == "list" and cfg.get("list"):
        vals = [parse_int_text(x) for x in cfg["list"]]
        vals = [v for v in vals if v is not None and 0 <= v <= 1000000]
        if vals:
            return random.choice(vals)
    if mode == "ep_range":
        # 默认值与 DEFAULT_CONFIG / normalize_config 一致（epMin=1000000, epMax=10000000）；
        # 用户手工编辑 config.json 漏掉这两个字段时也不会退化成 [0,0] 导致报错
        return pick_by_ep_range(_cfg_int(cfg, "epMin", 1000000), _cfg_int(cfg, "epMax", 10000000))
    if mode == "tier":
        return pick_by_tier(str(cfg.get("tier", "mythic")))
    lo = _cfg_int(cfg, "min", 1000)
    hi = _cfg_int(cfg, "max", 999999)
    if hi < lo:
        lo, hi = hi, lo
    return random.randint(lo, hi)


def fetch_site():
    """抓取官网首页 HTML（5 分钟缓存）；失败返回 None（调用方降级到本地首页）"""
    with _site_cache_lock:
        if _site_cache["html"] and time.time() - _site_cache["t"] < 300:
            return _site_cache["html"]
    try:
        req = urllib.request.Request("https://www.rngdle.com/", headers={"User-Agent": UA})
        html = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "replace")
        with _site_cache_lock:
            _site_cache.update(t=time.time(), html=html)
        return html
    except Exception:
        return None

# 本地配置页（改 config.json + GENERATE）。用 r""" 原始字符串：页面 JS 里有 /[\s,_]/ 这类正则
HOME = r"""<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>RNGdle 本地配置</title>
<script>(function(){try{var t=localStorage.getItem('rngdle_theme');var d=window.matchMedia('(prefers-color-scheme: dark)').matches;if(t==='dark'||(t!=='light'&&d))document.documentElement.classList.add('dark')}catch(e){}})();</script>
<style>
  :root { color-scheme: light dark; }
  * { box-sizing: border-box; }
  body { margin:0; min-height:100vh; display:flex; flex-direction:column; font-family:'Arial Black',Arial,sans-serif;
         background:#f6f7f9; color:#111; }
  .dark body { background:#141517; color:#f5f5f5; }
  #topbar { display:flex; align-items:center; justify-content:space-between; padding:18px 26px; }
  .logo { font-size:30px; letter-spacing:1px; margin:0; text-transform:uppercase; }
  .logo .dle { color:#4ea3ff; }
  .tag { font-size:12px; font-weight:700; letter-spacing:.5px; color:#fff; background:#3a3f47;
         padding:7px 13px; border-radius:8px; text-transform:uppercase; }
  main { flex:1; display:flex; align-items:flex-start; justify-content:center; padding:8px 20px 40px; }
  .card { width:100%; max-width:560px; background:#fff; border:1px solid #e5e7eb; border-radius:14px;
          padding:22px 24px 24px; box-shadow:0 10px 30px rgba(0,0,0,.06); font-family:Arial,sans-serif; }
  .dark .card { background:#1c1e22; border-color:#33383f; box-shadow:none; }
  .card h2 { margin:0 0 4px; font-size:18px; font-family:'Arial Black',Arial,sans-serif; }
  .hint { margin:0 0 18px; font-size:12px; line-height:1.5; color:#8b93a1; }
  .hint code { font-family:Consolas,monospace; font-size:12px; }
  .warn { display:none; margin:-6px 0 14px; padding:9px 11px; border-radius:8px; font-size:12px; line-height:1.5;
          background:#fff7ed; border:1px solid #fdba74; color:#9a3412; }
  .dark .warn { background:rgba(124,45,18,.25); border-color:rgba(154,52,18,.6); color:#fdba74; }
  .field { margin-bottom:14px; }
  .field > label { display:block; font-size:12px; font-weight:700; letter-spacing:.4px; text-transform:uppercase;
                   color:#6b7280; margin-bottom:6px; }
  .dark .field > label { color:#9aa3ad; }
  input[type=text], input[type=number], select, textarea {
      width:100%; padding:9px 11px; font:14px/1.4 Arial,sans-serif; color:inherit;
      background:#f9fafb; border:1px solid #d1d5db; border-radius:8px; }
  .dark input[type=text], .dark input[type=number], .dark select, .dark textarea {
      background:#24272c; border-color:#3f454d; color:#f5f5f5; }
  textarea { min-height:74px; resize:vertical; font-family:Consolas,monospace; }
  .row2 { display:flex; gap:10px; }
  .row2 > * { flex:1; min-width:0; }
  .check { display:flex; align-items:center; gap:8px; font-size:14px; color:inherit;
           text-transform:none; letter-spacing:normal; font-weight:400; }
  .check input { width:16px; height:16px; flex:none; }
  .actions { display:flex; align-items:center; gap:12px; margin-top:18px; flex-wrap:wrap; }
  button { font-family:Arial,sans-serif; cursor:pointer; }
  #save { padding:10px 20px; font-size:14px; font-weight:700; border:none; border-radius:9px;
          background:#111; color:#fff; text-transform:uppercase; letter-spacing:.5px; }
  .dark #save { background:#f5f5f5; color:#111; }
  #save:disabled { opacity:.55; cursor:wait; }
  #status { font-size:13px; color:#6b7280; }
  .numhint { margin-top:6px; font-size:12px; line-height:1.4; color:#8b93a1; min-height:16px; }
  .numhint.bad { color:#dc2626; }
  .dark .numhint { color:#9aa3ad; }
  .dark .numhint.bad { color:#f87171; }
  .idxline { font-size:13px; line-height:1.5; color:#6b7280; }
  .dark .idxline { color:#9aa3ad; }
  .sndline { display:flex; flex-direction:column; gap:6px; margin-top:4px; max-height:220px; overflow-y:auto; }
  .sndbox { margin-top:16px; border:1px solid #e5e7eb; border-radius:10px; background:#fafafa;
            padding:12px 14px; }
  .dark .sndbox { border-color:#33383f; background:#1d2024; }
  .sndbox-head { font-size:13px; font-weight:700; color:#111; letter-spacing:.3px;
                 text-transform:none; }
  .dark .sndbox-head { color:#f5f5f5; }
  .sndbox-body { margin-top:8px; }
  .sndrow { display:flex; align-items:center; gap:10px; }
  .sndname { flex:1; min-width:0; font-size:13px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .sndcnt { font-size:12px; color:#8b93a1; flex:none; }
  .dark .sndcnt { color:#9aa3ad; }
  .sndrow input[type=number] { width:92px; flex:none; padding:5px 8px; font-size:13px; }
  .sndhint { margin-top:6px; font-size:12px; line-height:1.4; color:#8b93a1; }
  .dark .sndhint { color:#9aa3ad; }
  .sndactions { display:flex; align-items:center; gap:10px; margin-top:8px; flex-wrap:wrap; }
  #saveWeights { padding:7px 14px; font-size:13px; font-weight:600; border:none; border-radius:8px;
                 background:#111; color:#fff; }
  .dark #saveWeights { background:#f5f5f5; color:#111; }
  #wstatus { font-size:12px; color:#6b7280; }
  #wstatus.ok { color:#059669; }
  #wstatus.err { color:#dc2626; }
  #status.ok { color:#059669; }
  #status.err { color:#dc2626; }
  hr { border:none; border-top:1px solid #e5e7eb; margin:20px 0 18px; }
  .dark hr { border-top-color:#33383f; }
  #gen { width:100%; font-size:20px; font-weight:700; padding:14px 46px; border:none; border-radius:999px;
         background:#4ea3ff; color:#fff; text-transform:uppercase; letter-spacing:1px;
         box-shadow:0 8px 24px rgba(78,163,255,.35); transition:transform .15s,box-shadow .15s;
         font-family:'Arial Black',Arial,sans-serif; }
  #gen:hover { transform:translateY(-2px); box-shadow:0 12px 30px rgba(78,163,255,.5); }
  #gen:disabled { opacity:.6; cursor:wait; }
  iframe#res { position:fixed; inset:0; width:100vw; height:100vh; border:0; z-index:99; background:#fff; }
  #close { display:none; position:fixed; top:12px; right:12px; z-index:100; width:34px; height:34px;
           border-radius:50%; border:none; background:rgba(17,17,17,.85); color:#fff;
           font:bold 16px/1 Arial,sans-serif; box-shadow:0 2px 10px rgba(0,0,0,.4); }
  #skip { display:none; position:fixed; top:54px; right:12px; z-index:100; padding:7px 12px;
          border:none; border-radius:999px; background:rgba(17,17,17,.85); color:#fff;
          font:bold 12px/1 Arial,sans-serif; cursor:pointer; box-shadow:0 2px 10px rgba(0,0,0,.4); }
  /* 结果页上的 × 与跳过动画按钮随系统主题变色：深色系统黑底白字，浅色系统白底黑字 */
  @media (prefers-color-scheme: light) {
    #close, #skip { background:rgba(255,255,255,.95); color:#111; border:1px solid #d1d5db;
                    box-shadow:0 2px 10px rgba(0,0,0,.18); }
  }
</style>
</head>
<body>
  <div id="topbar">
    <h1 class="logo">RNG<span class="dle">dle</span></h1>
    <span class="tag">Local Config</span>
  </div>
  <main>
    <div class="card">
      <h2>抽取配置</h2>
      <p class="hint">改完点「保存配置」写入 <code>config.json</code>；点 GENERATE 按当前配置抽一次（结果页右上角 ✕ 可返回本页继续改）。</p>
      <div class="warn" id="warn"></div>
      <div class="field">
        <label for="mode">模式</label>
        <select id="mode">
          <option value="random">随机区间（min~max 内均匀随机）</option>
          <option value="list">候选列表（在列表里随机取一个）</option>
          <option value="fixed">固定数字（每次都抽这一个）</option>
          <option value="ep_range">EP 区间（抽到的 EP 永远在给定范围内）</option>
          <option value="tier">指定等级（永远抽到该等级）</option>
        </select>
      </div>
      <div class="field" id="f-random">
        <label>区间 min / max（可写 1e6）</label>
        <div class="row2">
          <input type="text" inputmode="numeric" autocomplete="off" id="min" placeholder="如 0">
          <input type="text" inputmode="numeric" autocomplete="off" id="max" placeholder="如 1e6">
        </div>
        <div class="numhint" id="h-random"></div>
      </div>
      <div class="field" id="f-list">
        <label for="list">候选列表（逗号或空格分隔，可写 1e3）</label>
        <textarea id="list" placeholder="6, 69, 420, 12345"></textarea>
      </div>
      <div class="field" id="f-fixed">
        <label for="fixed">固定数字（可写 1e6）</label>
        <input type="text" inputmode="numeric" autocomplete="off" id="fixed" placeholder="例如 123456">
        <div class="numhint" id="h-fixed"></div>
      </div>
      <div class="field" id="f-ep">
        <label>目标 EP 区间 min / max（可写 1e7 / 1e9）</label>
        <div class="row2">
          <input type="text" inputmode="numeric" autocomplete="off" id="epMin" placeholder="如 1e7">
          <input type="text" inputmode="numeric" autocomplete="off" id="epMax" placeholder="如 1e9">
        </div>
        <div class="numhint" id="h-ep"></div>
      </div>
      <div class="field" id="f-tier">
        <label for="tier">目标等级</label>
        <select id="tier">
          <option value="trash">trash</option>
          <option value="common">common</option>
          <option value="uncommon">uncommon</option>
          <option value="rare">rare</option>
          <option value="epic">epic</option>
          <option value="anomaly">anomaly</option>
          <option value="mythic">mythic</option>
        </select>
      </div>
      <div class="field" id="f-idx">
        <label>EP 索引</label>
        <div class="idxline" id="idx">—</div>
      </div>
      <div class="field">
        <label class="check"><input type="checkbox" id="animation"> 播放抽奖动画（取消勾选则生成静态结果页）</label>
      </div>
      <div class="field">
        <label class="check"><input type="checkbox" id="playSound"> 抽取完成音效（动画播完或点击跳过时随机播放 sounds/ 目录中的音效，刷新恢复不播放）</label>
      </div>
      <div class="actions">
        <button id="save" type="button">保存配置</button>
        <span id="status"></span>
      </div>
      <hr>
      <button id="gen" type="button">Generate</button>
      <div class="sndbox">
        <div class="sndbox-head">音效文件夹权重</div>
        <div class="sndbox-body">
          <div class="sndline" id="soundDirs">加载中…</div>
          <div class="sndhint">数字为对应子文件夹被抽中的权重，支持小数（如 0.5；未列出为 1，≤0 不参与）；保存后写入 sounds/权重.txt。根目录音效权重固定 1。</div>
          <div class="sndactions">
            <button id="saveWeights" type="button">保存音效权重</button>
            <span id="wstatus"></span>
          </div>
        </div>
      </div>
    </div>
  </main>
  <button id="close" type="button" title="关闭结果，返回配置">&times;</button>
  <button id="skip" type="button" title="立即显示最终结果">跳过动画</button>
<script>
(function () {
  "use strict";
  function $(id) { return document.getElementById(id); }
  var MODES = ["random", "list", "fixed", "ep_range", "tier"];
  var TIERS = ["trash", "common", "uncommon", "rare", "epic", "anomaly", "mythic"];
  var IDX_TXT = {
    none: "未建立 · 保存配置或首次抽取时自动建立（约 40 秒，只做一次）",
    building: "建立中",
    ready: "已就绪 · 抽取即时完成",
    error: "建立失败"
  };

  // 与后端 parse_int_text 一致：支持 1e7 / 1.5e6 / 1_000_000 / 带逗号或空格
  function parseNum(v) {
    var s = String(v == null ? "" : v).replace(/[\s,_]/g, "");
    if (s === "") return null;
    var m = /^([+-]?\d+(?:\.\d+)?)[eE]([+-]?\d+)$/.exec(s);
    if (m) {
      var exp = parseInt(m[2], 10);
      if (isNaN(exp) || Math.abs(exp) > 18) return null;
      var f = parseFloat(m[1]) * Math.pow(10, exp);
      if (!isFinite(f) || f !== Math.floor(f)) return null;
      return f;
    }
    return /^[+-]?\d+$/.test(s) ? parseInt(s, 10) : null;
  }

  function setHint(id, text, bad) {
    var el = $(id);
    el.textContent = text;
    el.className = bad ? "numhint bad" : "numhint";
  }

  function hintRange(id, aId, bId, unit) {
    var a = $(aId).value.trim(), b = $(bId).value.trim();
    if (a === "" && b === "") { setHint(id, ""); return; }
    var va = parseNum(a), vb = parseNum(b);
    if (va === null || vb === null) {
      setHint(id, "解析不了，示例：1e7 / 1500000", true);
      return;
    }
    setHint(id, "= " + va.toLocaleString("en-US") + " ~ " + vb.toLocaleString("en-US") + (unit || ""));
  }

  function refreshHints() {
    hintRange("h-random", "min", "max");
    hintRange("h-ep", "epMin", "epMax", " EP");
    var v = $("fixed").value.trim();
    if (v === "") { setHint("h-fixed", ""); return; }
    var n = parseNum(v);
    if (n === null) setHint("h-fixed", "解析不了，示例：1e6 / 123456", true);
    else setHint("h-fixed", "= " + n.toLocaleString("en-US"));
  }

  function bindHints() {
    ["min", "max", "epMin", "epMax", "fixed"].forEach(function (id) {
      $(id).addEventListener("input", refreshHints);
    });
  }

  function applyMode() {
    var m = $("mode").value;
    var needsIdx = (m === "ep_range") || (m === "tier");
    $("f-random").style.display = (m === "random") ? "" : "none";
    $("f-list").style.display = (m === "list") ? "" : "none";
    $("f-fixed").style.display = (m === "fixed") ? "" : "none";
    $("f-ep").style.display = (m === "ep_range") ? "" : "none";
    $("f-tier").style.display = (m === "tier") ? "" : "none";
    $("f-idx").style.display = needsIdx ? "" : "none";
    if (needsIdx) indexStatus();
  }

  function indexStatus() {
    fetch("/index", { cache: "no-store" })
      .then(function (r) { return r.json(); })
      .then(function (j) {
        var t = IDX_TXT[j.state] || String(j.state);
        if (j.state === "building") t += " " + j.progress + "%…";
        if (j.state === "error") t += "：" + (j.error || "未知原因");
        $("idx").textContent = t;
      })
      .catch(function () { $("idx").textContent = "状态未知（服务器未响应）"; });
  }

  function setStatus(msg, cls) {
    var s = $("status");
    s.textContent = msg || "";
    s.className = cls || "";
  }

  function fill(cfg) {
    var raw = String(cfg && cfg.mode ? cfg.mode : "random").toLowerCase();
    if (raw === "range") raw = "random";
    var mode = (MODES.indexOf(raw) >= 0) ? raw : "random";
    $("mode").value = mode;
    var w = $("warn");
    if (raw !== mode) {
      w.style.display = "block";
      w.textContent = "config.json 里的 mode 是「" + cfg.mode + "」，不是有效值，这里按 random 显示；保存后会写入有效值。";
    } else {
      w.style.display = "none";
    }
    $("min").value = (cfg && cfg.min != null) ? cfg.min : 0;
    $("max").value = (cfg && cfg.max != null) ? cfg.max : 999999;
    $("list").value = (cfg && cfg.list && cfg.list.length) ? cfg.list.join(", ") : "";
    $("fixed").value = (cfg && cfg.fixed != null) ? cfg.fixed : "";
    $("epMin").value = (cfg && cfg.epMin != null) ? cfg.epMin : 1000000;
    $("epMax").value = (cfg && cfg.epMax != null) ? cfg.epMax : 10000000;
    $("tier").value = (cfg && TIERS.indexOf(cfg.tier) >= 0) ? cfg.tier : "mythic";
    $("animation").checked = !(cfg && cfg.animation === false);
    $("playSound").checked = !!(cfg && cfg.playSound === true);   // 音效默认关闭：仅显式 true 才勾选
    loadWeights();
    applyMode();
    refreshHints();
  }

  function load() {
    setStatus("读取中…");
    fetch("/config", { cache: "no-store" })
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(function (cfg) { fill(cfg); setStatus(""); })
      .catch(function (e) { setStatus("读取配置失败：" + e.message, "err"); });
  }

  // 保存 config（Promise 版）：供「保存配置」按钮与 GENERATE 自动保存共用
  function saveConfig() {
    var body = {
      mode: $("mode").value,
      min: $("min").value,
      max: $("max").value,
      list: $("list").value,
      fixed: $("fixed").value,
      animation: $("animation").checked,
      playSound: $("playSound").checked,
      epMin: $("epMin").value,
      epMax: $("epMax").value,
      tier: $("tier").value
    };
    return fetch("/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    })
      .then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (j) { return { ok: r.ok, j: j }; });
      })
      .then(function (res) {
        if (!res.ok || !res.j.ok) throw new Error(res.j.error || "HTTP 保存失败");
        fill(res.j.config);
        return true;
      });
  }

  function save() {
    var btn = $("save");
    btn.disabled = true;
    setStatus("保存中…");
    saveConfig()
      .then(function () { setStatus("已保存到 config.json", "ok"); })
      .catch(function (e) { setStatus("保存失败：" + e.message, "err"); })
      .then(function () { btn.disabled = false; });
  }

  // GENERATE：先自动保存当前配置与音效权重（未点保存按钮也会存），再按最新 config 抽一次，
  // 全屏展示本地结果页
  function doGenerate() {
    var btn = $("gen");
    return fetch("/generate", { cache: "no-store" })
      .then(function (r) {
        if (!r.ok) {
          // 抽取失败时服务器返回 JSON（如「EP 区间里没有数字」）
          return r.text().then(function (t) {
            var msg = "HTTP " + r.status;
            try { var j = JSON.parse(t); if (j && j.error) msg = j.error; } catch (e) {}
            throw new Error(msg);
          });
        }
        return r.text();
      })
      .then(function (html) {
        // 保存本次结果：刷新（reload）后据此恢复，且刷新即直接落到终态（不重播动画）
        try { sessionStorage.setItem("rngdle_local_roll", html); } catch (e) {}
        var old = $("res");
        if (old) old.remove();
        var f = document.createElement("iframe");
        f.id = "res";
        document.body.appendChild(f);
        f.srcdoc = html;
        $("close").style.display = "block";
        $("skip").style.display = "block";
        // 静态结果页（config 里 animation=false）没有 rngdleSkipAnim，此时才隐藏跳过按钮；
        // 连续多次探测不到才判定（iframe 的首次 load 可能早于结果页脚本就绪，一次误判会把按钮删掉）
        var miss = 0;
        var guard = setInterval(function () {
          if (!f.isConnected) { clearInterval(guard); return; }
          var has = false;
          try { has = typeof f.contentWindow.rngdleSkipAnim === "function"; } catch (e) { has = false; }
          if (has) { miss = 0; return; }
          if (++miss >= 6) { clearInterval(guard); $("skip").style.display = "none"; }
        }, 500);
        // 动画播放完成后自动隐藏"跳过动画"按钮（与官网一致：动画结束不再保留跳过入口）
        var animWatch = setInterval(function () {
          if (!f.isConnected) { clearInterval(animWatch); return; }
          var done = false;
          try {
            var de = f.contentDocument && f.contentDocument.documentElement;
            done = !!(de && de.getAttribute("data-rngdle-anim") === "done");
          } catch (e) { done = false; }
          if (done) { clearInterval(animWatch); $("skip").style.display = "none"; }
        }, 300);
      })
      .catch(function (e) { alert("生成失败：" + e.message + "（请确认 rngdle_score.exe 与本服务器在同一目录）"); });
  }
  $("gen").addEventListener("click", function () {
    var btn = this;
    btn.disabled = true;
    btn.textContent = "Saving…";
    // 点击 GENERATE 时自动保存未保存的配置与音效权重（含权重框），保存成功后再抽取
    Promise.all([saveConfig(), saveWeights()])
      .then(function () { btn.textContent = "Rolling…"; return doGenerate(); })
      .catch(function (e) { alert("保存配置失败：" + e.message + "，未执行抽取"); })
      .then(function () {
        btn.disabled = false;
        btn.textContent = "Generate";
      });
  });

  $("skip").addEventListener("click", function () {
    var f = $("res");
    try {
      if (f && f.contentWindow && typeof f.contentWindow.rngdleSkipAnim === "function") {
        f.contentWindow.rngdleSkipAnim();
      }
    } catch (e) { /* 结果页未就绪：忽略 */ }
    this.style.display = "none";
  });

  $("close").addEventListener("click", function () {
    // 只有点击 × 才清除保存的结果并回到配置页：动画播完后的刷新不会清掉它
    try { sessionStorage.removeItem("rngdle_local_roll"); } catch (e) {}
    var f = $("res");
    if (f) f.remove();
    this.style.display = "none";
    $("skip").style.display = "none";
  });

  $("mode").addEventListener("change", applyMode);
  $("save").addEventListener("click", save);

  // —— 音效文件夹权重编辑 ——
  function loadWeights() {
    fetch("/sound_dirs", { cache: "no-store" })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        var box = $("soundDirs");
        if (!d || !d.dirs || !d.dirs.length) {
          box.textContent = "未发现音效子文件夹（sounds/ 下还没有文件夹，可先放入音效）";
          return;
        }
        box.innerHTML = "";
        d.dirs.forEach(function (g) {
          var row = document.createElement("div");
          row.className = "sndrow";
          var nm = document.createElement("span");
          nm.className = "sndname";
          nm.textContent = g.name;
          nm.title = g.name;
          var ct = document.createElement("span");
          ct.className = "sndcnt";
          ct.textContent = g.count + " 个";
          var inp = document.createElement("input");
          inp.type = "number";
          inp.step = "any";
          inp.min = 0;
          inp.value = g.weight;
          inp.setAttribute("data-name", g.name);
          row.appendChild(nm);
          row.appendChild(ct);
          row.appendChild(inp);
          box.appendChild(row);
        });
      })
      .catch(function () { $("soundDirs").textContent = "加载失败"; });
  }
  // 保存音效权重（Promise 版）：供「保存音效权重」按钮与 GENERATE 自动保存共用
  function saveWeights() {
    var ws = {};
    var bad = false;
    document.querySelectorAll("#soundDirs input[data-name]").forEach(function (inp) {
      var v = inp.value.trim();
      if (v === "") { ws[inp.getAttribute("data-name")] = 1; return; }
      var n = Number(v);
      if (isNaN(n) || !isFinite(n)) { bad = true; return; }
      ws[inp.getAttribute("data-name")] = n;
    });
    if (bad) return Promise.reject(new Error("音效权重存在无效数字"));
    var wbtn = $("saveWeights");
    wbtn.disabled = true;
    return fetch("/sound_dirs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ weights: ws })
    })
      .then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (j) { return { ok: r.ok, j: j }; });
      })
      .then(function (res) {
        if (!res.ok || !res.j.ok) throw new Error(res.j.error || "HTTP 保存失败");
        return true;
      })
      .then(function (v) { wbtn.disabled = false; return v; },
            function (e) { wbtn.disabled = false; throw e; });
  }
  function saveWeightsBtn() {
    saveWeights()
      .then(function () { $("wstatus").textContent = "已保存，下次抽取生效"; $("wstatus").className = "ok"; })
      .catch(function (e) { $("wstatus").textContent = "保存失败：" + e.message; $("wstatus").className = "err"; });
  }
  $("saveWeights").addEventListener("click", saveWeightsBtn);
  bindHints();
  // 配置页停在 ep_range/tier 时轮询索引状态（建立中显示百分比）
  setInterval(function () {
    if ($("f-idx").style.display !== "none") indexStatus();
  }, 3000);
  load();
  // 刷新（reload）后恢复上一次抽取的结果：
  //   动画尚未播完 → 结果页重载后立即跳过动画，直接展示终态；
  //   动画已经播完 → 同样直接展示终态（不再重播）。
  // 只有点击右上角 × 才清除 sessionStorage，回到配置页进行下一次抽取。
  try {
    var saved = sessionStorage.getItem("rngdle_local_roll");
    if (saved) {
      var old = $("res");
      if (old) old.remove();
      var f = document.createElement("iframe");
      f.id = "res";
      document.body.appendChild(f);
      // 恢复的是动画已播完的终态 HTML，meta（等级标签/百分位）此时已 anim-in 正常显示：
      // 注入前把 meta 拉回隐藏态（anim-wait），iframe 加载瞬间即为隐藏，之后 rngdleSkipAnim
      // 的 150ms 延迟再触发放大动画——避免"先闪现正常标签、随后被动画覆盖"。
      // 同时注入 data-rngdle-recover=1：刷新恢复场景不重播抽取完成音效（音效脚本认此标记）。
      f.srcdoc = saved
        .replace(/<html/i, '<html data-rngdle-recover="1"')
        .replace(/class="meta[^"]*anim-in[^"]*"/, 'class="meta anim-wait"');
      $("close").style.display = "block";
      $("skip").style.display = "none";   // 恢复场景不重播动画，无需跳过按钮
      var t0 = Date.now();
      var poll = setInterval(function () {
        try {
          if (typeof f.contentWindow.rngdleSkipAnim === "function") {
            f.contentWindow.rngdleSkipAnim();
            clearInterval(poll);
            return;
          }
        } catch (e) {}
        if (Date.now() - t0 > 8000) clearInterval(poll);
      }, 200);
    }
  } catch (e) {}
})();
</script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, html, code=200):
        body = html.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        # Chrome 新版对「公网 https 页面 → 回环地址」的请求强制 Private Network Access
        # 预检；缺此头会直接 Failed to fetch（net::ERR_BLOCKED_BY_CLIENT / PNA 拦截）。
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def do_GET(self):
        path = self.path.split("?")[0]
        q = self.path.split("?", 1)[1] if "?" in self.path else ""
        if path == "/generate":
            try:
                num = pick_number(load_config())  # 抽取结果受 config.json 设定
            except Exception as e:
                # ep_range/tier 模式可能因区间里没有数字、索引建立失败等原因抽不出来
                print("[generate] 抽取失败：%s" % e)
                self._json({"ok": False, "error": str(e)}, 500)
                return
            print("[generate] 抽取数字:", num)
            self._serve_num(num)
        elif path == "/config":
            # 配置页回填用：返回 config.json 原文（不校验，无效值由页面提示）
            cfg, err = read_config()
            if err is None:
                self._json(cfg)
            else:
                self._json({"error": err}, 500)
        elif path == "/sound_dirs":
            # 音效子文件夹权重（配置页编辑）：返回各文件夹当前权重与音效数
            pool = list_sounds()
            dirs = []
            for g in pool:
                if g["name"]:  # 根目录（name==""）权重固定 1，不参与权重配置
                    dirs.append({"name": g["name"], "weight": g["weight"], "count": len(g["urls"])})
            self._json({"dirs": dirs})
        elif path.startswith("/sounds/"):
            # 抽取完成音效的静态文件服务（sounds/ 目录内，带路径穿越防护）
            self._serve_sound(path[len("/sounds/"):])
        elif path == "/index":
            # EP 索引状态（供配置页显示进度；只报告，不触发建立）
            with _index_lock:
                self._json({"state": _index_state, "progress": _index_progress,
                            "error": _index_error, "count": INDEX_N, "tiers": list(TIERS),
                            "cached": os.path.exists(INDEX)})
        elif path == "/" and q:
            try:
                self._serve_num(int(parse_qs(q).get("num", [""])[0]))
            except ValueError:
                self._send("<h1>参数需为数字：/?num=123456</h1>", 400)
        elif path in ("/", "/index.html"):
            # 默认本地配置页（改 config.json + GENERATE）；设置环境变量
            # RNGDLE_PROXY=1 时改为代理官网真实页面（官网 Next.js 在 localhost 下兼容有限）
            if os.environ.get("RNGDLE_PROXY") == "1":
                site = fetch_site()
                if site is None:
                    self._send(HOME)
                else:
                    base = '<base href="https://www.rngdle.com/">'
                    if "<head>" in site.lower():
                        site = site.replace("<head>", "<head>" + base + INJECT_SCRIPT, 1)
                    else:
                        site = site.replace("<html", "<html><head>" + base + INJECT_SCRIPT + "</head>", 1)
                    self._send(site)
            else:
                self._send(HOME)
        else:
            self._send("<h1>404</h1><p>端点：/（配置页） /config（读写配置） /generate（按配置抽取）"
                       " /?num=数字（指定） /sounds/音效文件（抽取完成音效） /sound_dirs（音效权重）</p>", 404)

    def do_POST(self):
        path = self.path.split("?")[0]
        if path not in ("/config", "/sound_dirs"):
            self._json({"ok": False, "error": "404 未知端点：%s" % path}, 404)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > 65536:
            self._json({"ok": False, "error": "请求体为空或过大"}, 400)
            return
        body = self.rfile.read(length).decode("utf-8", "replace")
        if path == "/sound_dirs":
            self._save_sound_weights(body)
            return
        try:
            submitted = json.loads(body)
        except Exception as e:
            self._json({"ok": False, "error": "JSON 解析失败：%s" % e}, 400)
            return
        cfg, err = normalize_config(submitted)
        if err is not None:
            self._json({"ok": False, "error": err}, 400)
            return
        try:
            write_config(cfg)
        except Exception as e:
            self._json({"ok": False, "error": "写入 config.json 失败：%s" % e}, 500)
            return
        print("[config] 已保存：mode=%s min=%s max=%s list=%s fixed=%s animation=%s playSound=%s epMin=%s epMax=%s tier=%s"
              % (cfg["mode"], cfg["min"], cfg["max"], cfg["list"], cfg["fixed"], cfg["animation"],
                 cfg["playSound"], cfg["epMin"], cfg["epMax"], cfg["tier"]))
        if cfg["mode"] in ("ep_range", "tier"):
            # 新模式需要全量 EP 索引：保存后立刻在后台开始建立（再次保存或抽取时不会再重复扫）
            start_index_build()
        self._json({"ok": True, "config": cfg})

    def _serve_sound(self, rel):
        # self.path 是百分号编码的 URL（如 /sounds/网络流行梗类/xx.wav 会编码成 %E7%BD%91…），
        # 先 unquote 解码；只允许 sounds/ 目录内的相对路径，用 posix 分割过滤再拼回，杜绝 ../ 穿越
        rel = urllib.parse.unquote(rel).replace("\\", "/")
        parts = [p for p in rel.split("/") if p not in ("", ".", "..")]
        if not parts:
            self._send("<h1>403</h1>", 403)
            return
        fp = os.path.normpath(os.path.join(SND_DIR, *parts))
        if not os.path.isfile(fp):
            self._send("<h1>404</h1>", 404)
            return
        ext = os.path.splitext(fp)[1].lower()
        ctype = {".mp3": "audio/mpeg", ".wav": "audio/wav", ".ogg": "audio/ogg",
                 ".m4a": "audio/mp4", ".flac": "audio/flac",
                 ".aac": "audio/aac"}.get(ext, "application/octet-stream")
        try:
            with open(fp, "rb") as f:
                data = f.read()
        except OSError:
            self._send("<h1>500</h1>", 500)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(data)
        except BrokenPipeError:
            pass

    def _save_sound_weights(self, body):
        """POST /sound_dirs：把 {weights: {文件夹名: 权重}} 写回 sounds/权重.txt。
        权重必须是有限数字（可为小数）；≤0 表示该文件夹不参与抽取。"""
        try:
            data = json.loads(body)
        except Exception:
            self._json({"ok": False, "error": "JSON 解析失败"}, 400)
            return
        ws = data.get("weights")
        if not isinstance(ws, dict):
            self._json({"ok": False, "error": "缺少 weights 对象"}, 400)
            return
        lines = [
            "# 音效子文件夹权重：每行 `文件夹名=权重数字`，# 开头为注释，空行忽略。",
            "# 未列出的文件夹权重为 1；权重 <= 0 的文件夹不参与抽取。",
            "# 本文件由配置页「保存音效权重」生成，也可手工编辑。",
        ]
        import math as _math
        for name in sorted(ws):
            v = ws[name]
            if not isinstance(v, (int, float)) or isinstance(v, bool) or not _math.isfinite(v):
                self._json({"ok": False, "error": "权重值必须是有限数字：" + str(name)}, 400)
                return
            lines.append("%s=%s" % (name, v))
        os.makedirs(SND_DIR, exist_ok=True)
        with open(WEIGHTS_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        self._json({"ok": True, "dirs": [g["name"] for g in list_sounds() if g["name"]]})

    def _serve_num(self, num):
        html = self._run_exe(num)
        if html is None:
            self._send("<h1>500</h1><p>rngdle_score.exe 执行失败，请确认其位于本目录。</p>", 500)
        else:
            self._send(self._inject_sound(html))

    def _inject_sound(self, html):
        """把抽取完成音效脚本注入结果页 </body> 前（动画脚本已先于该位置执行）。"""
        if not html or "</body>" not in html:
            return html
        cfg = load_config()
        pool = list_sounds()
        # 音效 URL 必须用绝对地址：浏览器插件是在官网页面用 srcdoc iframe 展示结果页，
        # 相对路径 /sounds/... 在 iframe 里会按官网域名解析成 rngdle.com/sounds/... → 404，
        # 导致无声、浮层不显示。按请求 Host 拼成 http://host/sounds/... 后任何页面都能正确加载。
        host = (self.headers.get("Host") or "127.0.0.1:8765").strip()
        base = "http://" + host
        pool = [dict(g, urls=[base + u for u in g["urls"]]) for g in pool]
        # 用占位符字符串替换而非 % 格式化：sounds/ 下若存在含 % 的文件名/子文件夹名
        # （如 "100%.wav"），json.dumps 会把 % 带进替换串，% 格式化会抛 ValueError
        # 导致整个结果页注入失败（无声、无浮层）
        script = (SOUND_INJECT
                  .replace("__RNGDLE_SOUNDS__", json.dumps(pool))
                  .replace("__RNGDLE_ENABLED__", "true" if cfg.get("playSound", False) else "false"))
        return html.replace("</body>", script + "</body>", 1)

    def _run_exe(self, num):
        # 用 --no-open 调用：仅生成 rngdle_result.html，不弹浏览器；
        # CREATE_NO_WINDOW 让计分 exe 以无窗口方式运行，不额外弹出黑色控制台。
        # config.json 的 animation=false 时附加 --no-anim：生成无抽奖动画的静态结果页
        # 并发安全：ThreadingHTTPServer 多线程下若共用 BASE/rngdle_result.html，
        # 请求 A 的 exe 未写完、请求 B 就读取会串台。这里每个请求用独立的临时目录
        # 让 exe 写自己的 rngdle_result.html，读完立即删除，互不干扰。
        args = [EXE, "--no-open"]
        if not load_config().get("animation", True):
            args.append("--no-anim")
        tmpdir = None
        try:
            tmpdir = tempfile.mkdtemp(prefix="rngdle_gen_", dir=BASE)
            subprocess.run(args, input=("%d\n" % num).encode("utf-8"),
                           cwd=tmpdir, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=subprocess.CREATE_NO_WINDOW,
                           timeout=60)
            with open(os.path.join(tmpdir, "rngdle_result.html"), "r", encoding="utf-8") as f:
                return f.read()
        except Exception as e:
            print("[err]", e)
            return None
        finally:
            if tmpdir:
                shutil.rmtree(tmpdir, ignore_errors=True)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, fmt, *args):
        print("[%s] %s" % (self.address_string(), fmt % args))


# ---------------------------------------------------------------------------
# 托盘图标（pythonw 无控制台模式）：服务器静默驻留系统托盘通知区
# （默认收在任务栏右下角小箭头里），左键单击打开配置页，右键菜单可退出服务。
# 纯 ctypes 实现，不依赖第三方库；python.exe 前台模式不启用。
# ---------------------------------------------------------------------------
def _has_console():
    try:
        import ctypes
        return bool(ctypes.windll.kernel32.GetConsoleWindow())
    except Exception:
        return False


def _start_tray():
    """创建托盘图标并运行消息循环（守护线程）。仅 pythonw 启动时调用。"""
    import ctypes
    import webbrowser
    from ctypes import wintypes

    WM_APP = 0x8000
    CB_MSG = WM_APP + 1                      # 回调消息必须落在 WM_APP..0xBFFF
    NIM_ADD, NIM_DELETE, NIM_SETVERSION = 0, 2, 4
    NIF_MESSAGE, NIF_ICON, NIF_TIP = 1, 2, 4
    WM_LBUTTONUP, WM_LBUTTONDBLCLK = 0x0202, 0x0203
    WM_RBUTTONUP, WM_COMMAND, WM_DESTROY = 0x0205, 0x0111, 0x0002
    ID_OPEN, ID_EXIT = 1, 2
    URL = "http://127.0.0.1:%d/" % PORT

    user32 = ctypes.windll.user32
    shell32 = ctypes.windll.shell32
    kernel32 = ctypes.windll.kernel32

    # 回调返回 LRESULT（64 位指针值），不能用 32 位 c_long，否则返回值截断
    WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
                                 wintypes.WPARAM, wintypes.LPARAM)

    class WNDCLASS(ctypes.Structure):
        _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
                    ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                    ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                    ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HANDLE),
                    ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]

    class NOTIFYICONDATAW(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND),
                    ("uID", wintypes.UINT), ("uFlags", wintypes.UINT),
                    ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HICON),
                    ("szTip", wintypes.WCHAR * 128), ("dwState", wintypes.DWORD),
                    ("dwStateMask", wintypes.DWORD), ("szInfo", wintypes.WCHAR * 256),
                    ("uVersion", wintypes.UINT), ("szInfoTitle", wintypes.WCHAR * 64),
                    ("dwInfoFlags", wintypes.DWORD), ("guidItem", ctypes.c_byte * 16),
                    ("hBalloonIcon", wintypes.HICON)]

    # 逐个声明参数/返回类型，避免 64 位指针在 ctypes 默认 32 位转换下溢出
    user32.LoadIconW.restype = wintypes.HICON
    user32.LoadIconW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR]
    user32.DefWindowProcW.restype = ctypes.c_ssize_t  # LRESULT（64 位）
    user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                      wintypes.WPARAM, wintypes.LPARAM]
    user32.RegisterClassW.restype = wintypes.ATOM
    user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASS)]
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                       wintypes.DWORD, ctypes.c_int, ctypes.c_int,
                                       ctypes.c_int, ctypes.c_int, wintypes.HWND,
                                       wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
    user32.GetCursorPos.restype = wintypes.BOOL
    user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
    user32.CreatePopupMenu.restype = wintypes.HMENU
    user32.AppendMenuW.restype = wintypes.BOOL
    user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t,
                                   wintypes.LPCWSTR]  # UINT_PTR = c_size_t
    user32.TrackPopupMenu.restype = wintypes.BOOL
    user32.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_int,
                                      ctypes.c_int, ctypes.c_int, wintypes.HWND,
                                      ctypes.c_void_p]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.PostMessageW.restype = wintypes.BOOL
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                    wintypes.WPARAM, wintypes.LPARAM]
    user32.GetMessageW.restype = wintypes.BOOL
    user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                                   wintypes.UINT, wintypes.UINT]
    user32.TranslateMessage.restype = wintypes.BOOL
    user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.DispatchMessageW.restype = ctypes.c_ssize_t  # LRESULT（64 位）
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    shell32.Shell_NotifyIconW.restype = wintypes.BOOL
    shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]

    def open_page():
        try:
            webbrowser.open(URL)
        except Exception:
            pass

    def quit_server():
        try:
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
        except Exception:
            pass
        os._exit(0)

    def show_menu():
        hmenu = user32.CreatePopupMenu()
        user32.AppendMenuW(hmenu, 0, ID_OPEN, "打开配置页")
        user32.AppendMenuW(hmenu, 0, ID_EXIT, "退出服务")
        pt = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        user32.SetForegroundWindow(hwnd)
        user32.TrackPopupMenu(hmenu, 0x0002, pt.x, pt.y, 0, hwnd, None)
        user32.PostMessageW(hwnd, WM_APP, 0, 0)  # 触发菜单消失消息

    @WNDPROC
    def wndproc(hw, msg, wp, lp):
        if msg == CB_MSG:
            ev = wp & 0xFFFF
            if ev in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                open_page()
                return 0
            if ev == WM_RBUTTONUP:
                show_menu()
                return 0
        elif msg == WM_COMMAND:
            if wp == ID_OPEN:
                open_page()
            elif wp == ID_EXIT:
                quit_server()
            return 0
        elif msg == WM_DESTROY:
            return 0
        return user32.DefWindowProcW(hw, msg, wp, lp)

    hinst = kernel32.GetModuleHandleW(None)
    clsname = "RNGdleTrayWnd"
    wc = WNDCLASS()
    wc.lpfnWndProc = wndproc
    wc.hInstance = hinst
    wc.lpszClassName = clsname
    user32.RegisterClassW(ctypes.byref(wc))
    # 隐藏窗口（style=0 且不 ShowWindow）：无任务栏按钮，仅用于接收托盘回调
    hwnd = user32.CreateWindowExW(0, clsname, "RNGdle", 0, 0, 0, 0, 0,
                                  None, None, hinst, None)
    icon = user32.LoadIconW(None, ctypes.cast(32512, wintypes.LPCWSTR))  # IDI_APPLICATION
    nid = NOTIFYICONDATAW()
    nid.cbSize = ctypes.sizeof(nid)
    nid.hWnd = hwnd
    nid.uID = 1
    nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
    nid.uCallbackMessage = CB_MSG
    nid.hIcon = icon
    nid.szTip = "RNGdle 本地服务器（左键打开配置页，右键菜单退出）"
    if not shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)):
        print("[tray] Shell_NotifyIconW 添加失败", flush=True)
        return
    print("[tray] 托盘图标已添加（NIM_ADD ok，hwnd=%d）" % hwnd, flush=True)
    # 版本协商：让右键/悬停事件可靠（Windows 7+）
    nid.uVersion = 3
    shell32.Shell_NotifyIconW(NIM_SETVERSION, ctypes.byref(nid))

    msg = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))
    try:
        shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
    except Exception:
        pass


if __name__ == "__main__":
    # pythonw 无控制台运行：sys.stdout/stderr 为 None，任何 print 都会抛异常，
    # 重定向到空设备；同时启用托盘图标（后台静默模式）。
    if sys.stdout is None or sys.stderr is None:
        sys.stdout = sys.stderr = open(os.devnull, "w", encoding="utf-8", errors="replace")
    print("RNGdle 本地服务器 http://127.0.0.1:%d  (Ctrl+C 退出)" % PORT, flush=True)
    # config.json 是本地文件（已 gitignore）：首次运行按默认值生成一份，方便手工编辑
    if not os.path.exists(CONFIG):
        try:
            write_config(dict(DEFAULT_CONFIG))
            print("[config] 未找到 config.json，已按默认值生成（随机 0~999999，可手工编辑）", flush=True)
        except Exception as e:
            print("[config] 生成 config.json 失败：%s" % e, flush=True)
    # EP 索引在服务器启动后立刻后台生成：已有 ep_index.bin 缓存则秒载入就绪，
    # 没有则全量扫描 0~1000000（约 40 秒，不阻塞服务）；exe 变化后自动重建
    try:
        print("[index] 启动后台建立 EP 索引（首次约 40 秒，不阻塞服务）…")
        start_index_build()
    except Exception as e:
        print("[index] 启动检查跳过：%s" % e)
    # 托盘模式：pythonw 无控制台启动时，进程驻留系统托盘通知区（小箭头里），
    # 左键打开配置页、右键菜单退出；python.exe 前台运行（有控制台）时不启用，
    # 但可用环境变量 RNGDLE_TRAY=1 强制启用（便于前台排障观察日志）。
    try:
        if os.environ.get("RNGDLE_TRAY") == "1" or not _has_console():
            threading.Thread(target=_start_tray, daemon=True).start()
    except Exception as e:
        print("[tray] 托盘启动失败（不影响服务）：%s" % e)
    try:
        ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\n已退出")
