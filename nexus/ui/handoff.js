/* Synthetic-only capability probe. No secrets, telemetry, or persistent storage. */
(() => {
  "use strict";
  const MAX = 128 * 1024;
  const el = id => document.getElementById(id);
  let verified = null, fileId = null, blobUrl = null, busy = false, generation = 0;
  let sequence = 1, bridgeReady = false, acceptedSignature = null;
  const pending = new Map();
  const rejectedIds = new Set();
  let diagnostics = {download: "not_tested", origin: null, http_status: null, widget_state: "not_tested"};
  const state = {receiver_widget_verified: false, host_file_registered: false,
    host_download_verified: false, sandbox_original_verified: false};
  const show = text => { el("status").textContent = text; };
  const details = () => { el("diagnostics").textContent = JSON.stringify(diagnostics, null, 2); };
  const validId = id => typeof id === "string" && id.length > 0 && id.length <= 512 && !/[\s\x00-\x1f\x7f]/.test(id);
  function restore() {
    if (!verified || busy || fileId) return;
    const saved = window.openai?.widgetState;
    const p = saved?.privateContent;
    if (p?.sha256 !== verified.manifest.sha256 || p?.revision !== 1 || p?.artifact_id !== "small-png" ||
        !validId(p.fileId) || rejectedIds.has(p.fileId) || saved.imageIds?.length !== 1 || saved.imageIds[0] !== p.fileId) return;
    fileId = p.fileId;
    state.host_file_registered = true;
    diagnostics.widget_state = "restored_from_host";
    // Persisted success is historical evidence; freshly verify host bytes again.
    diagnostics.download = "not_tested_after_reload";
    details(); controls();
  }
  const digest = async bytes => Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", bytes)),
    x => x.toString(16).padStart(2, "0")).join("");
  function notify(method, params) { window.parent.postMessage({jsonrpc: "2.0", method, params}, "*"); }
  function request(method, params) {
    const id = sequence++;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => { pending.delete(id); reject(new Error("host_timeout")); }, 15000);
      pending.set(id, {resolve, reject, timer});
      window.parent.postMessage({jsonrpc: "2.0", id, method, params}, "*");
    });
  }
  function capabilities() {
    const api = window.openai;
    return api && typeof api.uploadFile === "function" && typeof api.setWidgetState === "function";
  }
  function controls() {
    el("upload").disabled = busy || !verified || !capabilities();
    el("local").disabled = busy || !verified || !capabilities();
    el("inspect").disabled = busy || !fileId || !bridgeReady;
    el("recheck").disabled = busy || !fileId;
  }
  async function accept(result) {
    if (!result || !result._meta?.original_base64) return;
    const signature = JSON.stringify([result.structuredContent, result._meta.original_base64]);
    if (verified && signature === acceptedSignature) return;
    const ticket = ++generation;
    verified = null; fileId = null;
    Object.keys(state).forEach(k => state[k] = false);
    controls();
    el("preview").hidden = true; el("download").hidden = true;
    if (blobUrl) { URL.revokeObjectURL(blobUrl); blobUrl = null; }
    try {
      const m = result.structuredContent;
      const b64 = result._meta.original_base64;
      if (result.isError || m?.artifact_id !== "small-png" || m.revision !== 1 ||
          m.project !== "synthetic-probe" || m.mime_type !== "image/png" ||
          !Number.isInteger(m.size) || m.size < 1 || m.size > MAX ||
          !/^[a-f0-9]{64}$/.test(m.sha256) || typeof b64 !== "string" ||
          b64.length > Math.ceil(MAX / 3) * 4 || !/^[A-Za-z0-9+/]*={0,2}$/.test(b64))
        throw new Error("invalid_original");
      const bytes = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
      if (bytes.length !== m.size || await digest(bytes) !== m.sha256) throw new Error("hash_mismatch");
      if (ticket !== generation) return;
      verified = {bytes, manifest: {...m}};
      acceptedSignature = signature;
      state.receiver_widget_verified = true;
      blobUrl = URL.createObjectURL(new Blob([bytes], {type: "image/png"}));
      el("preview").src = blobUrl; el("preview").hidden = false;
      el("download").href = blobUrl; el("download").hidden = false;
      show(`UI受信検証：一致（${m.size} bytes）\nSHA-256: ${m.sha256}\nChatGPT内原本：未確認\n` +
        (capabilities() ? "ファイル受渡し機能を検出しました。" : "この環境では公式ファイル受渡し機能を検出できません。通常添付を使用してください。"));
    } catch (_) {
      if (ticket === generation) show("原本の受信検証に失敗しました。アップロードは停止しました。");
    }
    restore(); details(); controls();
  }
  // Optional round trip, restricted to the documented OpenAI file host.
  async function checkHostFile(api, id, expected) {
    diagnostics.download = "requesting_url"; diagnostics.origin = null; diagnostics.http_status = null; details();
    if (typeof api.getFileDownloadUrl !== "function") { diagnostics.download = "helper_unavailable"; return false; }
    let value;
    try { value = await api.getFileDownloadUrl({fileId: id}); }
    catch (_) { diagnostics.download = "url_api_failed"; return false; }
    if (typeof value?.downloadUrl !== "string") { diagnostics.download = "invalid_api_response"; return false; }
    let url;
    try { url = new URL(value.downloadUrl); } catch (_) { diagnostics.download = "invalid_url"; return false; }
    // Only origin is visible; never retain a signed path/query or raw exception.
    diagnostics.origin = url.origin;
    // Exact additional origin observed from the official helper in this probe.
    if (url.protocol !== "https:" || !["files.oaiusercontent.com", "oaisdmntprcentralus.blob.core.windows.net"].includes(url.hostname) ||
        url.port || url.username || url.password) { diagnostics.download = "origin_not_allowlisted"; return false; }
    diagnostics.download = "fetching";
    let response;
    try { response = await fetch(url.href, {credentials: "omit", redirect: "error", signal: AbortSignal.timeout(10000)}); }
    catch (_) { diagnostics.download = "fetch_blocked_or_network_error"; return false; }
    diagnostics.http_status = response.status;
    if (!response.ok || !response.body) { diagnostics.download = "http_or_body_unavailable"; return false; }
    const reader = response.body.getReader();
    const chunks = []; let length = 0;
    try {
      while (true) {
        const {done, value: chunk} = await reader.read();
        if (done) break;
        length += chunk.length;
        if (length > MAX) { diagnostics.download = "file_limit"; throw new Error("file_limit"); }
        chunks.push(chunk);
      }
    } finally { await reader.cancel(); }
    const bytes = new Uint8Array(length); let offset = 0;
    for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.length; }
    if (length !== expected.size || await digest(bytes) !== expected.sha256) {
      diagnostics.download = "hash_mismatch"; throw new Error("host_hash_mismatch");
    }
    diagnostics.download = "verified";
    return true;
  }
  async function upload(file, mode) {
    if (!verified || busy || !capabilities()) return;
    busy = true; controls();
    const ticket = generation, expected = verified.manifest;
    try {
      if (file.size > MAX || file.size !== expected.size || await digest(await file.arrayBuffer()) !== expected.sha256)
        throw new Error("selected_file_mismatch");
      show("原本一致を確認。ChatGPTへファイルを渡しています。");
      const api = window.openai;
      const result = await api.uploadFile(file);
      if (ticket !== generation) return;
      // Host file IDs are opaque (including sediment://file_* in live ChatGPT).
      // They are passed only to official host APIs, never used as a fetch URL.
      if (!validId(result?.fileId)) throw new Error("missing_file_id");
      fileId = result.fileId; state.host_file_registered = true;
      try { state.host_download_verified = await checkHostFile(api, fileId, expected); }
      catch (error) {
        if (error.message === "host_hash_mismatch" || error.message === "file_limit") throw error;
        // A network/CSP/CORS failure is unverified, never a pass or a reason to relax security.
      }
      if (ticket !== generation) return;
      api.setWidgetState({modelContent: {artifact_id: expected.artifact_id, revision: 1,
        transfer_mode: mode, ...state}, privateContent: {fileId, artifact_id: expected.artifact_id,
          revision: 1, sha256: expected.sha256}, imageIds: [fileId]});
      const saved = api.widgetState;
      diagnostics.widget_state = saved?.imageIds?.includes(fileId) ? "readback_matched" : "submitted_readback_unconfirmed";
      show(`UI受信検証：一致\nChatGPTファイル登録：成功（${mode}）\nホスト再取得検証：${state.host_download_verified ? "一致" : "未確認"}\nChatGPT内原本：未確認\n「渡した原本を検査する」で実ファイルを確認します。`);
    } catch (_) {
      if (fileId) rejectedIds.add(fileId);
      if (diagnostics.download === "hash_mismatch" || diagnostics.download === "file_limit")
        window.openai.setWidgetState({modelContent: {status: "integrity_failed"}, privateContent: {}, imageIds: []});
      fileId = null; state.host_file_registered = false; state.host_download_verified = false;
      show("ファイル受渡し試験は未達です。原本の不一致またはホスト機能の制限により停止しました。通常添付を利用できます。");
    } finally { busy = false; details(); controls(); }
  }
  el("recheck").onclick = async () => {
    if (!fileId || busy || !verified) return;
    busy = true; controls();
    try { state.host_download_verified = await checkHostFile(window.openai, fileId, verified.manifest); }
    catch (_) {
      state.host_download_verified = false;
      if (diagnostics.download === "hash_mismatch" || diagnostics.download === "file_limit") {
        rejectedIds.add(fileId); fileId = null; state.host_file_registered = false;
        window.openai.setWidgetState({modelContent: {status: "integrity_failed"}, privateContent: {}, imageIds: []});
      }
    }
    finally { busy = false; details(); controls(); }
  };
  el("upload").onclick = () => {
    if (verified) void upload(new File([verified.bytes], "nexus-small-png-r1.png", {type: "image/png"}), "nexus-original");
  };
  el("local").onchange = event => {
    const file = event.target.files?.[0];
    if (file) void upload(file, "user-selected");
    event.target.value = "";
  };
  el("inspect").onclick = async () => {
    if (!fileId || busy || !bridgeReady) return;
    busy = true; controls();
    try {
      await request("ui/message", {role: "user", content: [{type: "text", text:
        "Nexusの原本byte検査です。登録した画像の正規ファイル参照を、利用可能な公式resolver/実行ツールで開いてください。画像を再作成せず、既存実ファイルのbyte数・SHA-256・strict PNGの寸法を実行コードで確認。ツールの正しい引数形式を使い、終了状態とエラーも確認してください。/mnt/dataだけを根拠に利用不能と断定しないこと。画像の内容説明は別試験です。実ファイルへ到達できなければinsufficient_contextで停止。Base64転記・生成・破損許容は禁止。UI結果はsandbox原本検査の代わりになりません。"}]});
      show("原本検査を依頼しました。検査結果は会話側で確認してください。UIは検査完了を自動認定しません。");
    } catch (_) { show("会話への検査依頼は未確認です。通常の入力欄から依頼してください。"); }
    finally { busy = false; controls(); }
  };
  window.addEventListener("message", event => {
    if (event.source !== window.parent || event.data?.jsonrpc !== "2.0") return;
    const msg = event.data;
    if (pending.has(msg.id)) {
      const p = pending.get(msg.id); pending.delete(msg.id); clearTimeout(p.timer);
      msg.error ? p.reject(new Error("host_error")) : p.resolve(msg.result);
    } else if (msg.method === "ui/notifications/tool-result") void accept(msg.params);
  });
  function legacyResult() {
    const meta = window.openai?.toolResponseMetadata;
    // Current host envelopes preserve the MCP result. Do not read browser internals.
    const result = meta?.mcp_tool_result || meta?.call_tool_result;
    if (result?._meta?.original_base64) void accept(result);
    else if (meta?.original_base64) void accept({structuredContent: window.openai.toolOutput, _meta: meta});
    restore(); controls();
  }
  window.addEventListener("openai:set_globals", legacyResult);
  request("ui/initialize", {appInfo: {name: "nexus-original-handoff", version: "0.1.0"},
    appCapabilities: {}, protocolVersion: "2026-01-26"}).then(() => {
      bridgeReady = true; notify("ui/notifications/initialized", {}); controls(); legacyResult();
  }).catch(() => { legacyResult(); });
  legacyResult();
})();
