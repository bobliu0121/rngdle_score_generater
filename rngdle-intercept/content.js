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
  // 右上角：✕ 关闭回官网可再抽；✕ 下方「跳过动画」直接跳到最终结果。
  //
  // 两个必须防御的点：
  //   1) 官网是 Next.js App Router，<html>/<body> 也在 React 的渲染树里，我们 append 到
  //      documentElement 的节点属于「多出来的子节点」，官网任何一次重渲染都可能把它清掉
  //      ——所以下面有一个保活看护：节点被移除就补回（move 节点不会让 iframe 重新加载）。
  //   2) iframe 的 load 可能早于结果页脚本就绪（about:blank 的 load），不能一次探测不到就删按钮。
  function showLocalResult(html) {
    const old = document.getElementById("rngdle-local-frame");
    if (old) old.remove();
    const oldClose = document.getElementById("rngdle-local-close");
    if (oldClose) oldClose.remove();
    const oldSkip = document.getElementById("rngdle-local-skip");
    if (oldSkip) oldSkip.remove();

    const f = document.createElement("iframe");
    f.id = "rngdle-local-frame";
    f.style.cssText =
      "position:fixed;inset:0;width:100vw;height:100vh;border:0;z-index:2147483647;" +
      "background:#fff;";
    const c = document.createElement("button");
    c.id = "rngdle-local-close";
    c.textContent = "✕";
    c.title = "关闭结果，返回官网（可再次抽取）";
    c.style.cssText =
      "position:fixed;top:12px;right:12px;z-index:2147483647;width:34px;height:34px;" +
      "border-radius:50%;border:none;background:rgba(17,17,17,.85);color:#fff;" +
      "font:bold 16px/1 Arial,sans-serif;cursor:pointer;box-shadow:0 2px 10px rgba(0,0,0,.4);";
    const s = document.createElement("button");
    s.id = "rngdle-local-skip";
    s.textContent = "跳过动画";
    s.title = "立即显示最终结果";
    s.style.cssText =
      "position:fixed;top:54px;right:12px;z-index:2147483647;padding:7px 12px;" +
      "border:none;border-radius:999px;background:rgba(17,17,17,.85);color:#fff;" +
      "font:bold 12px/1 Arial,sans-serif;cursor:pointer;box-shadow:0 2px 10px rgba(0,0,0,.4);";

    // 注意：content script 跑在隔离世界，读不到结果页里的 JS 全局变量 window.rngdleSkipAnim，
    // 所以一律用 DOM 通信——结果页会设 data-rngdle-skip=1 / data-rngdle-anim=running|done，
    // 并监听 'rngdle-skip' 事件来执行跳过。
    const docOf = () => {
      try {
        return f.contentDocument;
      } catch (err) {
        return null;
      }
    };
    const hasSkip = () => {
      const d = docOf();
      return !!(d && d.documentElement && d.documentElement.getAttribute("data-rngdle-skip") === "1");
    };
    const animDone = () => {
      const d = docOf();
      return !!(d && d.documentElement && d.documentElement.getAttribute("data-rngdle-anim") === "done");
    };
    const requestSkip = () => {
      const d = docOf();
      if (!d) return false;
      try {
        // 用结果页自身所属世界的构造函数造事件，再派发进它的 document（事件跨世界共享）
        const W = f.contentWindow;
        const Ctor = (W && (W.CustomEvent || W.Event)) || window.Event;
        d.dispatchEvent(new Ctor("rngdle-skip"));
        return true;
      } catch (err) {
        return false;
      }
    };

    let closed = false;   // 用户关掉结果：停止一切看护
    let skipped = false;  // 已点过跳过：不再补回按钮
    let miss = 0;         // 连续探测不到跳过标记的次数（判定静态结果页）

    c.onclick = () => {
      closed = true;
      f.remove();
      c.remove();
      s.remove();
    };
    s.onclick = () => {
      skipped = true;
      requestSkip();
      s.remove();
    };

    document.documentElement.appendChild(f);
    document.documentElement.appendChild(c);
    document.documentElement.appendChild(s);
    f.srcdoc = html;

    // 保活看护：官网重渲染清掉节点就补回；跳过按钮要等确认结果页没有跳过能力才隐藏
    const keep = setInterval(() => {
      if (closed) {
        clearInterval(keep);
        return;
      }
      if (!f.isConnected) document.documentElement.appendChild(f);
      if (!c.isConnected) document.documentElement.appendChild(c);
      if (skipped) return;
      if (hasSkip() && !animDone()) {
        miss = 0;
        if (!s.isConnected) document.documentElement.appendChild(s);
      } else if (++miss >= 6 && s.isConnected) {
        s.remove(); // ~3 秒仍没有跳过能力（或动画已结束）→ 隐藏按钮
      }
    }, 500);
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

  // 取本地结果页：
  //   1) 优先请扩展后台（background.js service worker）代取——不受网页「本地网络访问(LNA)」
  //      权限和页面 CORS 限制。切勿在网页上下文声明 targetAddressSpace:'local'：Chrome 会校验
  //      声明与真实地址空间，127.0.0.1 属于 loopback，与 local 不符会导致请求必然失败。
  //   2) 后台不可用时回退为内容脚本直接 fetch（此时需已允许 rngdle.com 访问本地网络）。
  function fetchResult() {
    return new Promise((resolve) => {
      const bg = (typeof chrome !== "undefined") && chrome.runtime && chrome.runtime.sendMessage;
      if (!bg) { resolve(null); return; }
      try {
        chrome.runtime.sendMessage({ type: "rngdle-generate" }, (res) => {
          if (chrome.runtime.lastError) resolve(null);
          else resolve(res || null);
        });
      } catch (err) {
        resolve(null);
      }
    }).then((res) => {
      if (res && res.ok && typeof res.html === "string") return res.html;
      if (res && !res.ok && res.error) throw new Error(res.error);
      // 回退：内容脚本直接取
      return fetch(SERVER + "/generate", { cache: "no-store" }).then((r) => {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.text();
      });
    });
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
        showLocalResult(await fetchResult());
      } catch (err) {
        showNotice("无法连接本地服务器（" + err.message + "）。请先运行 rngdle_server.py（端口 8765）；" +
                   "若控制台提示本地网络访问被拦截，请在地址栏允许 rngdle.com 访问本地网络后重试。");
      } finally {
        busy = false;
      }
    },
    true
  );
})();
