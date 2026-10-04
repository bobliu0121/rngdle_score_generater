// RNGdle 官网结果拦截：点 GENERATE 后改用本地 rngdle_score.exe 生成的网页展示。
// 依赖：本地运行 rngdle_server.py（默认 127.0.0.1:8765）。
(() => {
  "use strict";

  const SERVER = "http://127.0.0.1:8765";
  let busy = false;

  // 官网的 GENERATE 按钮（button / [role=button] / 带 generate 文本的元素）
  function isGenerateButton(el) {
    if (!el || el.nodeType !== 1) return false;
    const b = el.closest("button, [role=button]") || el;
    const t = (b.textContent || "").toLowerCase();
    const aria = (b.getAttribute && (b.getAttribute("aria-label") || "")) || "";
    const cls = b.className && typeof b.className === "string" ? b.className.toLowerCase() : "";
    const id = (b.id || "").toLowerCase();
    return /generate/.test(t) || /generate/.test(aria) || /generate/.test(cls) || /generate/.test(id);
  }

  // 全屏 iframe 覆盖官网，展示本地结果页（srcdoc 隔离，动画不受官网脚本干扰）
  function showLocalResult(html) {
    const old = document.getElementById("rngdle-local-frame");
    if (old) old.remove();
    const f = document.createElement("iframe");
    f.id = "rngdle-local-frame";
    f.style.cssText =
      "position:fixed;inset:0;width:100vw;height:100vh;border:0;z-index:2147483647;" +
      "background:#fff;";
    document.documentElement.appendChild(f);
    f.srcdoc = html;
  }

  function showNotice(msg) {
    const d = document.createElement("div");
    d.textContent = msg;
    d.style.cssText =
      "position:fixed;bottom:24px;left:50%;transform:translateX(-50%);z-index:2147483647;" +
      "background:#111;color:#fff;padding:12px 20px;border-radius:10px;" +
      "font:14px/1.4 Arial,sans-serif;box-shadow:0 4px 16px rgba(0,0,0,.3);";
    document.body.appendChild(d);
    setTimeout(() => d.remove(), 7000);
  }

  // capture 阶段拦截：先于官网自己的 click 处理执行，阻止官网 roll 与结果展示
  document.addEventListener(
    "click",
    async (e) => {
      if (busy || !isGenerateButton(e.target)) return;
      busy = true;
      e.preventDefault();
      e.stopPropagation();
      e.stopImmediatePropagation();
      try {
        const r = await fetch(SERVER + "/generate", { cache: "no-store" });
        if (!r.ok) throw new Error("HTTP " + r.status);
        showLocalResult(await r.text());
      } catch (err) {
        showNotice("无法连接本地服务器（" + err.message + "）。请先运行 rngdle_server.py（端口 8765）。");
      } finally {
        busy = false;
      }
    },
    true
  );
})();
