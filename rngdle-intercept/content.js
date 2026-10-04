// RNGdle 官网结果拦截：点 GENERATE 后改用本地 rngdle_score.exe 生成的网页展示。
// 依赖：本地运行 rngdle_server.py（默认 127.0.0.1:8765）。
(() => {
  "use strict";

  const SERVER = "http://127.0.0.1:8765";
  let busy = false;

  // 只有点中 GENERATE 按钮（按钮自身或其内部元素）才算数。
  // 注意：绝不能把「closest 取不到按钮时的被点元素」当按钮来判——点页面空白处时目标是
  // <body>/大容器，它们的 textContent 里含按钮文字 "GENERATE"，会导致整页任意位置都被拦截。
  function isGenerateButton(el) {
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

  // 全屏 iframe 覆盖官网，展示本地结果页（srcdoc 隔离，动画不受官网脚本干扰）；
  // 右上角提供关闭按钮：关闭后回到官网，可再次点击 GENERATE 无限抽取
  function showLocalResult(html) {
    const old = document.getElementById("rngdle-local-frame");
    if (old) old.remove();
    const oldClose = document.getElementById("rngdle-local-close");
    if (oldClose) oldClose.remove();
    const f = document.createElement("iframe");
    f.id = "rngdle-local-frame";
    f.style.cssText =
      "position:fixed;inset:0;width:100vw;height:100vh;border:0;z-index:2147483647;" +
      "background:#fff;";
    document.documentElement.appendChild(f);
    f.srcdoc = html;
    const c = document.createElement("button");
    c.id = "rngdle-local-close";
    c.textContent = "✕";
    c.title = "关闭结果，返回官网（可再次抽取）";
    c.style.cssText =
      "position:fixed;top:12px;right:12px;z-index:2147483647;width:34px;height:34px;" +
      "border-radius:50%;border:none;background:rgba(17,17,17,.85);color:#fff;" +
      "font:bold 16px/1 Arial,sans-serif;cursor:pointer;box-shadow:0 2px 10px rgba(0,0,0,.4);";
    c.onclick = () => {
      f.remove();
      c.remove();
    };
    document.documentElement.appendChild(c);
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
