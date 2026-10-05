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
抽取配置 config.json（mode：random/range/list/fixed/ep_range/tier；min/max、list、fixed；
ep_range 用 epMin/epMax 锁定 EP 区间，tier 用 tier 锁定等级，两者需要全量 EP 索引
ep_index.bin（首次自动用 exe --batch 扫描约 40 秒并缓存）；animation：false 时生成无抽奖动画的静态结果页）。
所有数值字段都接受 1e7 / 1.5e6 / 1_000_000 这类写法，落盘时统一写成整数。
config.json 属于本地运行时文件（已在 .gitignore 里，配置页每次保存都会重写它）：
缺失时用内置默认值（随机 0~999999），启动时会按默认值生成一份；仓库里的模板见 config.example.json。
启动：python rngdle_server.py  （默认端口 8765，Ctrl+C 退出）
"""
import os, random, subprocess, sys, time, json, threading, struct, bisect, re
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


# config.json 是纯本地运行时文件（已 gitignore），仓库里只有同样内容的模板 config.example.json。
# 文件缺失时一律用这份温和默认值：随机 0~999999；服务器启动时会据此生成 config.json 方便手工编辑。
DEFAULT_CONFIG = {"mode": "random", "min": 0, "max": 999999, "list": [], "fixed": None,
                  "animation": True, "epMin": 1000000, "epMax": 10000000, "tier": "mythic"}


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
    # 键顺序与仓库里的 config.json 保持一致：mode/min/max/list/fixed/animation [+ 新模式字段]
    return {"mode": mode, "min": lo, "max": hi, "list": lst, "fixed": fixed,
            "animation": bool(cfg.get("animation", True)),
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
    """让后台尽快开始建立索引（配置需要时调用，不阻塞）"""
    if _index_state in ("building", "ready"):
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
        return pick_by_ep_range(_cfg_int(cfg, "epMin", 0), _cfg_int(cfg, "epMax", 0))
    if mode == "tier":
        return pick_by_tier(str(cfg.get("tier", "mythic")))
    lo = _cfg_int(cfg, "min", 1000)
    hi = _cfg_int(cfg, "max", 999999)
    if hi < lo:
        lo, hi = hi, lo
    return random.randint(lo, hi)


def fetch_site():
    """抓取官网首页 HTML（5 分钟缓存）；失败返回 None（调用方降级到本地首页）"""
    if _site_cache["html"] and time.time() - _site_cache["t"] < 300:
        return _site_cache["html"]
    try:
        req = urllib.request.Request("https://www.rngdle.com/", headers={"User-Agent": UA})
        html = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "replace")
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
  .countdown { margin-top:12px; text-align:center; font-size:12px; letter-spacing:1px; color:#8b93a1;
               text-transform:uppercase; }
  .countdown b { color:#4ea3ff; font-size:14px; }
  iframe#res { position:fixed; inset:0; width:100vw; height:100vh; border:0; z-index:99; background:#fff; }
  #close { display:none; position:fixed; top:12px; right:12px; z-index:100; width:34px; height:34px;
           border-radius:50%; border:none; background:rgba(17,17,17,.85); color:#fff;
           font:bold 16px/1 Arial,sans-serif; box-shadow:0 2px 10px rgba(0,0,0,.4); }
  #skip { display:none; position:fixed; top:54px; right:12px; z-index:100; padding:7px 12px;
          border:none; border-radius:999px; background:rgba(17,17,17,.85); color:#fff;
          font:bold 12px/1 Arial,sans-serif; cursor:pointer; box-shadow:0 2px 10px rgba(0,0,0,.4); }
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
      <div class="actions">
        <button id="save" type="button">保存配置</button>
        <span id="status"></span>
      </div>
      <hr>
      <button id="gen" type="button">Generate</button>
      <div class="countdown">Next roll in <b id="cd">--:--:--</b></div>
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

  function save() {
    var btn = $("save");
    btn.disabled = true;
    setStatus("保存中…");
    var body = {
      mode: $("mode").value,
      min: $("min").value,
      max: $("max").value,
      list: $("list").value,
      fixed: $("fixed").value,
      animation: $("animation").checked,
      epMin: $("epMin").value,
      epMax: $("epMax").value,
      tier: $("tier").value
    };
    fetch("/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    })
      .then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (j) { return { ok: r.ok, j: j }; });
      })
      .then(function (res) {
        if (!res.ok || !res.j.ok) throw new Error(res.j.error || ("HTTP 保存失败"));
        fill(res.j.config);
        setStatus("已保存到 config.json", "ok");
      })
      .catch(function (e) { setStatus("保存失败：" + e.message, "err"); })
      .then(function () { btn.disabled = false; });
  }

  // 倒计时到次日 8:00（与官网一致）
  function tick() {
    var now = new Date();
    var nxt = new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1, 8, 0, 0);
    var d = Math.max(0, nxt - now);
    var h = Math.floor(d / 3600000), m = Math.floor(d % 3600000 / 60000), s = Math.floor(d % 60000 / 1000);
    function p(x) { return (x < 10 ? "0" : "") + x; }
    $("cd").textContent = p(h) + ":" + p(m) + ":" + p(s);
  }

  // GENERATE：按 config.json 抽一次，全屏展示本地结果页
  $("gen").addEventListener("click", function () {
    var btn = this;
    btn.disabled = true;
    btn.textContent = "Rolling…";
    fetch("/generate", { cache: "no-store" })
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
      })
      .catch(function (e) { alert("生成失败：" + e.message + "（请确认 rngdle_score.exe 与本服务器在同一目录）"); })
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
  bindHints();
  tick();
  setInterval(tick, 1000);
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
      f.srcdoc = saved;
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
                       " /?num=数字（指定）</p>", 404)

    def do_POST(self):
        path = self.path.split("?")[0]
        if path != "/config":
            self._json({"ok": False, "error": "404 未知端点：%s" % path}, 404)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > 65536:
            self._json({"ok": False, "error": "请求体为空或过大"}, 400)
            return
        raw = self.rfile.read(length)
        try:
            submitted = json.loads(raw.decode("utf-8"))
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
        print("[config] 已保存：mode=%s min=%s max=%s list=%s fixed=%s animation=%s epMin=%s epMax=%s tier=%s"
              % (cfg["mode"], cfg["min"], cfg["max"], cfg["list"], cfg["fixed"], cfg["animation"],
                 cfg["epMin"], cfg["epMax"], cfg["tier"]))
        if cfg["mode"] in ("ep_range", "tier"):
            # 新模式需要全量 EP 索引：保存后立刻在后台开始建立（再次保存或抽取时不会再重复扫）
            start_index_build()
        self._json({"ok": True, "config": cfg})

    def _serve_num(self, num):
        html = self._run_exe(num)
        if html is None:
            self._send("<h1>500</h1><p>rngdle_score.exe 执行失败，请确认其位于本目录。</p>", 500)
        else:
            self._send(html)

    def _run_exe(self, num):
        # 用 --no-open 调用：仅生成 rngdle_result.html，不弹浏览器；
        # CREATE_NO_WINDOW 让计分 exe 以无窗口方式运行，不额外弹出黑色控制台。
        # config.json 的 animation=false 时附加 --no-anim：生成无抽奖动画的静态结果页
        args = [EXE, "--no-open"]
        if not load_config().get("animation", True):
            args.append("--no-anim")
        try:
            subprocess.run(args, input=("%d\n" % num).encode("utf-8"),
                           cwd=BASE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=subprocess.CREATE_NO_WINDOW,
                           timeout=60)
            with open(RESULT, "r", encoding="utf-8") as f:
                return f.read()
        except Exception as e:
            print("[err]", e)
            return None

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


if __name__ == "__main__":
    print("RNGdle 本地服务器 http://127.0.0.1:%d  (Ctrl+C 退出)" % PORT, flush=True)
    # config.json 是本地文件（已 gitignore）：首次运行按默认值生成一份，方便手工编辑
    if not os.path.exists(CONFIG):
        try:
            write_config(dict(DEFAULT_CONFIG))
            print("[config] 未找到 config.json，已按默认值生成（随机 0~999999，可手工编辑）", flush=True)
        except Exception as e:
            print("[config] 生成 config.json 失败：%s" % e, flush=True)
    # 配置里用到 ep_range/tier 且索引缓存不存在时，启动就在后台扫描（不阻塞服务）
    try:
        if load_config().get("mode") in ("ep_range", "tier") and not os.path.exists(INDEX):
            print("[index] 当前配置需要 EP 索引，正在后台建立（仅首次，约 40 秒）…")
            start_index_build()
    except Exception as e:
        print("[index] 启动检查跳过：%s" % e)
    try:
        ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\n已退出")
