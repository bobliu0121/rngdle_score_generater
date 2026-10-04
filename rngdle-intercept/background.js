// RNGdle 本地结果服务器的取数通道（MV3 service worker）。
//
// 为什么不让 content script 直接 fetch：
//   1) Chrome 142+ 的本地网络访问（LNA）权限：公网页面请求 127.0.0.1 需要用户逐个站点授权，
//      未授权时直接被拦（net::ERR_FAILED / "Local Network Access"）。手动声明
//      targetAddressSpace:'local' 更糟——Chrome 会校验声明与真实地址空间，
//      127.0.0.1 属于 loopback，与声明的 local 不符，于是必然失败。
//   2) 内容脚本的 fetch 还受网页 origin 的 CORS 约束。
// 由扩展后台发起请求则不受上述两者限制，目标地址已在 manifest 的 host_permissions 里声明。
const SERVER = "http://127.0.0.1:8765";

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!msg || msg.type !== "rngdle-generate") return;
  fetch(SERVER + "/generate", { cache: "no-store" })
    .then((r) => {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.text();
    })
    .then((html) => sendResponse({ ok: true, html: html }))
    .catch((e) => sendResponse({ ok: false, error: e && e.message ? e.message : String(e) }));
  return true; // 保持消息通道，等待异步 sendResponse
});
