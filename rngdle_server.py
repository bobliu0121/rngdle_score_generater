# -*- coding: utf-8 -*-
"""RNGdle 本地结果服务器

作用：调用 rngdle_score.exe 生成结果页（含官网同款抽奖动画），供浏览器取用。
端点：
  GET /              -> 本地复刻官网首页（标题/GENERATE/倒计时/主题自适应，视觉与官网一致；
                         点 GENERATE 走本地 /generate。设环境变量 RNGDLE_PROXY=1 时改为
                         代理官网真实页面并注入拦截脚本）
  GET /generate      -> 按 config.json 设定抽取数字，调 exe 生成结果页并返回完整 HTML
  GET /?num=<数字>   -> 指定数字生成结果页
抽取配置 config.json（mode：random/list/fixed/range；min/max/list/fixed）。
启动：python rngdle_server.py  （默认端口 8765，Ctrl+C 退出）
"""
import os, random, subprocess, sys, time, json
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

BASE = os.path.dirname(os.path.abspath(__file__))
EXE = os.path.join(BASE, "rngdle_score.exe")
RESULT = os.path.join(BASE, "rngdle_result.html")
CONFIG = os.path.join(BASE, "config.json")
PORT = 8765
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
_site_cache = {"t": 0.0, "html": None}

# 注入到官网首页的拦截脚本：点 GENERATE 后改请求本地 /generate，全屏展示本地结果页（含抽奖动画）
INJECT_SCRIPT = """<script>
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
  function isGen(el) {
    if (!el || el.nodeType !== 1) return false;
    const b = el.closest("button, [role=button]") || el;
    const t = (b.textContent || "").toLowerCase();
    const aria = (b.getAttribute && (b.getAttribute("aria-label") || "")) || "";
    const cls = b.className && typeof b.className === "string" ? b.className.toLowerCase() : "";
    return /generate/.test(t) || /generate/.test(aria) || /generate/.test(cls);
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


def load_config():
    """读取 config.json；出错时回退默认（纯随机 4~6 位）"""
    try:
        with open(CONFIG, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"mode": "random", "min": 1000, "max": 999999}


def pick_number(cfg):
    """按配置抽取数字：fixed / list / random(range)"""
    mode = cfg.get("mode", "random")
    if mode == "fixed" and cfg.get("fixed") is not None:
        return int(cfg["fixed"])
    if mode == "list" and cfg.get("list"):
        return random.choice([int(x) for x in cfg["list"]])
    lo = int(cfg.get("min", 1000))
    hi = int(cfg.get("max", 999999))
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

HOME = """<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>RNGdle</title>
<style>
  :root { color-scheme: light dark; }
  * { box-sizing: border-box; }
  body { margin:0; min-height:100vh; display:flex; flex-direction:column; font-family:'Arial Black',Arial,sans-serif;
         background:#f6f7f9; color:#111; }
  .dark body { background:#141517; color:#f5f5f5; }
  #topbar { display:flex; align-items:center; justify-content:space-between; padding:18px 26px; }
  .logo { font-size:30px; letter-spacing:1px; margin:0; text-transform:uppercase; }
  .logo .dle { color:#4ea3ff; }
  .nav { display:flex; gap:20px; align-items:center; }
  .nav-item { font-size:12px; font-weight:700; letter-spacing:.5px; color:#fff; background:#3a3f47;
              padding:7px 13px; border-radius:8px; cursor:pointer; text-transform:uppercase;
              display:flex; align-items:center; gap:6px; }
  main { flex:1; display:flex; flex-direction:column; align-items:center; justify-content:center; gap:30px; padding:0 20px; }
  #gen { font-size:22px; font-weight:700; padding:16px 54px; border:none; border-radius:999px;
         cursor:pointer; background:#4ea3ff; color:#fff; text-transform:uppercase;
         box-shadow:0 8px 24px rgba(78,163,255,.35); transition:transform .15s,box-shadow .15s; }
  #gen:hover { transform:translateY(-2px); box-shadow:0 12px 30px rgba(78,163,255,.5); }
  #gen:active { transform:translateY(0); }
  #gen:disabled { opacity:.6; cursor:wait; }
  .countdown { font-size:14px; letter-spacing:1px; color:#8b93a1; text-transform:uppercase;
               font-family:Arial,sans-serif; }
  .countdown b { color:#4ea3ff; font-size:16px; }
  iframe#res { position:fixed; inset:0; width:100vw; height:100vh; border:0; z-index:99; background:#fff; }
</style>
</head>
<body>
  <div id="topbar">
    <h1 class="logo">RNG<span class="dle">dle</span></h1>
    <div class="nav"><span class="nav-item">LEADERBOARD</span></div>
  </div>
  <main>
    <button id="gen">GENERATE</button>
    <div class="countdown">NEXT ROLL IN <b id="cd">--:--:--</b></div>
    <div class="countdown" style="font-size:12px;opacity:.55">本地模式 · 结果由 rngdle_score.exe 生成 · 抽取受 config.json 控制</div>
  </main>
<script>
  // 倒计时到次日 8:00（本地时间）
  function tick() {
    var now = new Date();
    var nxt = new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1, 8, 0, 0);
    var d = Math.max(0, nxt - now);
    var h = Math.floor(d / 3600000), m = Math.floor(d % 3600000 / 60000), s = Math.floor(d % 60000 / 1000);
    function p(x) { return (x < 10 ? "0" : "") + x; }
    document.getElementById("cd").textContent = p(h) + ":" + p(m) + ":" + p(s);
  }
  tick(); setInterval(tick, 1000);
  // GENERATE：请求本地 /generate（抽取按 config.json 设定），全屏展示结果页（含抽奖动画）
  document.getElementById("gen").addEventListener("click", async function () {
    var btn = this;
    btn.disabled = true;
    btn.textContent = "ROLLING…";
    try {
      var r = await fetch("/generate", { cache: "no-store" });
      if (!r.ok) throw new Error("HTTP " + r.status);
      var html = await r.text();
      var old = document.getElementById("res");
      if (old) old.remove();
      var f = document.createElement("iframe");
      f.id = "res";
      document.body.appendChild(f);
      f.srcdoc = html;
      btn.textContent = "GENERATE";
    } catch (err) {
      btn.textContent = "GENERATE";
      alert("生成失败：" + err.message + "（请确认 rngdle_server.py 已运行）");
    }
    btn.disabled = false;
  });
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

    def do_GET(self):
        path = self.path.split("?")[0]
        q = self.path.split("?", 1)[1] if "?" in self.path else ""
        if path == "/generate":
            num = pick_number(load_config())  # 抽取结果受 config.json 设定
            print("[generate] 抽取数字:", num)
            self._serve_num(num)
        elif path == "/" and q:
            try:
                self._serve_num(int(parse_qs(q).get("num", [""])[0]))
            except ValueError:
                self._send("<h1>参数需为数字：/?num=123456</h1>", 400)
        elif path in ("/", "/index.html"):
            # 默认本地复刻官网首页（视觉一致 + 拦截 GENERATE）；设置环境变量
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
            self._send("<h1>404</h1><p>端点：/generate（随机） 或 /?num=数字（指定）</p>", 404)

    def _serve_num(self, num):
        html = self._run_exe(num)
        if html is None:
            self._send("<h1>500</h1><p>rngdle_score.exe 执行失败，请确认其位于本目录。</p>", 500)
        else:
            self._send(html)

    def _run_exe(self, num):
        # 用 --no-open 调用：仅生成 rngdle_result.html，不弹浏览器；
        # CREATE_NO_WINDOW 让计分 exe 以无窗口方式运行，不额外弹出黑色控制台
        try:
            subprocess.run([EXE, "--no-open"], input=("%d\n" % num).encode("utf-8"),
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
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, fmt, *args):
        print("[%s] %s" % (self.address_string(), fmt % args))


if __name__ == "__main__":
    print("RNGdle 本地服务器 http://127.0.0.1:%d  (Ctrl+C 退出)" % PORT)
    try:
        ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\n已退出")
