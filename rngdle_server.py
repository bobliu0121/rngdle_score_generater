# -*- coding: utf-8 -*-
"""RNGdle 本地结果服务器

作用：调用 rngdle_score.exe 生成结果页（含官网同款抽奖动画），供浏览器取用。
端点：
  GET /              -> 镜像首页（复刻官网：标题 + GENERATE 按钮 + 主题自适应）
  GET /generate      -> 随机 4~6 位数字，调 exe 生成结果页并返回完整 HTML
  GET /?num=<数字>   -> 指定数字生成结果页
启动：python rngdle_server.py  （默认端口 8765，Ctrl+C 退出）
"""
import os, random, subprocess, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

BASE = os.path.dirname(os.path.abspath(__file__))
EXE = os.path.join(BASE, "rngdle_score.exe")
RESULT = os.path.join(BASE, "rngdle_result.html")
PORT = 8765

HOME = """<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>RNGdle (本地)</title>
<style>
  :root { color-scheme: light dark; }
  body { margin:0; min-height:100vh; display:flex; flex-direction:column; align-items:center;
         justify-content:center; gap:28px; font-family:'Arial Black',Arial,sans-serif;
         background:#f4f4f4; color:#111; transition:background .3s,color .3s; }
  @media (prefers-color-scheme: dark) { body { background:#1a1a1a; color:#f5f5f5; } }
  h1 { font-size:44px; letter-spacing:2px; margin:0; text-transform:uppercase; }
  h1 .dle { color:#4ea3ff; }
  #gen { font-size:20px; font-weight:700; padding:14px 42px; border:none; border-radius:999px;
         cursor:pointer; background:#4ea3ff; color:#fff; text-transform:uppercase;
         box-shadow:0 6px 18px rgba(78,163,255,.35); transition:transform .15s,box-shadow .15s; }
  #gen:hover { transform:translateY(-2px); box-shadow:0 10px 26px rgba(78,163,255,.5); }
  #gen:active { transform:translateY(0); }
  .hint { font-size:13px; opacity:.55; font-family:Arial,sans-serif; }
  iframe#res { position:fixed; inset:0; width:100vw; height:100vh; border:0; z-index:99;
               background:#fff; }
</style>
</head>
<body>
  <h1>RNG<span class="dle">dle</span></h1>
  <button id="gen">GENERATE</button>
  <div class="hint">本地模式 · 结果由 rngdle_score.exe 生成</div>
<script>
  document.getElementById('gen').addEventListener('click', async function () {
    var btn = this;
    btn.disabled = true;
    btn.textContent = 'ROLLING…';
    try {
      var r = await fetch('/generate');
      if (!r.ok) throw new Error('HTTP ' + r.status);
      var html = await r.text();
      var old = document.getElementById('res');
      if (old) old.remove();
      var f = document.createElement('iframe');
      f.id = 'res';
      document.body.appendChild(f);
      f.srcdoc = html;
      btn.textContent = 'GENERATE';
    } catch (err) {
      btn.textContent = 'GENERATE';
      alert('生成失败：' + err.message);
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
            num = random.randint(1000, 999999)  # 官网随机 4~6 位
            self._serve_num(num)
        elif path == "/" and q:
            try:
                self._serve_num(int(parse_qs(q).get("num", [""])[0]))
            except ValueError:
                self._send("<h1>参数需为数字：/?num=123456</h1>", 400)
        elif path in ("/", "/index.html"):
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

    def log_message(self, fmt, *args):
        print("[%s] %s" % (self.address_string(), fmt % args))


if __name__ == "__main__":
    print("RNGdle 本地服务器 http://127.0.0.1:%d  (Ctrl+C 退出)" % PORT)
    try:
        ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\n已退出")
