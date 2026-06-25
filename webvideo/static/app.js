const $ = (selector) => document.querySelector(selector);
const state = { task: null, auth: {}, items: [], loginCaches: [], loaded: 100, events: null, refreshTimer: null, lastAuthStatus: "", toastTimer: null };

const statusNames = {
  parsing: "正在解析", browser: "浏览器解析", ready: "等待确认",
  waiting_browser: "等待登录确认",
  waiting_qr: "等待扫码登录",
  scheduled: "等待开始",
  processing: "正在转录", completed: "全部完成",
  completed_with_errors: "部分完成", cancelled: "已停止", failed: "失败",
  discovered: "已发现", queued: "等待下载", downloading: "正在下载",
  transcribing: "正在转录", unsupported: "无播放权限 / 不支持",
};

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || `请求失败 (${response.status})`);
  return data;
}

function escapeHTML(value) {
  return String(value ?? "").replace(/[&<>'"]/g, ch => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;"
  }[ch]));
}

function formatDuration(seconds) {
  const total = Number(seconds || 0);
  if (!total) return "时长未知";
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = Math.floor(total % 60);
  return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}

function itemHTML(item) {
  const completed = item.status === "completed" && item.output_path;
  const stateLabel = statusNames[item.status] || item.status;
  const deleteButton = item.has_artifacts
    ? `<button type="button" class="artifact-delete icon-button" data-id="${item.id}" title="删除 TXT 和媒体缓存" aria-label="删除该视频的 TXT 和媒体缓存" ${state.task?.running ? "disabled" : ""}>🗑</button>`
    : "";
  return `<article class="video-row" data-id="${item.id}">
    <input class="item-check" type="checkbox" ${item.selected ? "checked" : ""} aria-label="选择 ${escapeHTML(item.title)}">
    <div>
      <h3 class="video-title">${escapeHTML(item.title)}</h3>
      <div class="video-meta">
        <span>${escapeHTML(item.author || "作者未知")}</span>
        <span>${escapeHTML(item.duration_text || formatDuration(item.duration_seconds))}</span>
        <span>${escapeHTML(item.extractor)}</span>
        <a href="${escapeHTML(item.webpage_url)}" target="_blank" rel="noreferrer">来源</a>
      </div>
    </div>
    <div class="item-state">
      <div class="item-state-heading">
        <strong>${completed ? `<a class="transcript-link" href="/api/items/${item.id}/transcript" target="_blank">查看 TXT</a>` : escapeHTML(stateLabel)}</strong>
        ${deleteButton}
      </div>
      <div class="progress"><span style="width:${Number(item.progress || 0)}%"></span></div>
      ${item.last_error ? `<div class="item-error" title="${escapeHTML(item.last_error)}">${escapeHTML(item.last_error.slice(0, 90))}</div>` : ""}
    </div>
  </article>`;
}

function renderItems() {
  const list = $("#videoList");
  list.innerHTML = state.items.map(itemHTML).join("");
  $("#emptyState").classList.toggle("hidden", state.items.length > 0);
  list.classList.toggle("hidden", state.items.length === 0);
  document.querySelectorAll(".item-check").forEach(check => {
    check.addEventListener("change", async event => {
      const row = event.target.closest(".video-row");
      try {
        await api(`/api/tasks/${state.task.id}/items/${row.dataset.id}/selection`, {
          method: "PUT", body: JSON.stringify({ selected: event.target.checked })
        });
        await refreshTask();
      } catch (error) { showMessage(error.message, true); }
    });
  });
  document.querySelectorAll(".artifact-delete").forEach(button => {
    button.addEventListener("click", () => deleteItemArtifacts(button.dataset.id));
  });
}

async function deleteItemArtifacts(itemId) {
  if (!window.confirm("确定删除该视频的 TXT 和媒体缓存吗？")) return;
  try {
    await api(`/api/items/${itemId}/artifacts`, { method: "DELETE" });
    showToast("该视频的 TXT 和媒体缓存已删除");
    await refreshTask();
  } catch (error) {
    showMessage(error.message, true);
  }
}

async function loadItems() {
  if (!state.task) return;
  const data = await api(`/api/tasks/${state.task.id}/items?offset=0&limit=${state.loaded}`);
  state.items = data.items;
  renderItems();
}

function renderTask() {
  const task = state.task;
  if (!task) return;
  $("#taskCard").classList.remove("hidden");
  $("#taskStatus").textContent = statusNames[task.status] || task.status;
  $("#taskTitle").textContent = ["browser", "waiting_browser"].includes(task.status)
    ? "浏览器窗口正在协助解析"
    : task.status === "waiting_qr" ? "扫码登录后自动继续解析" : "解析与转录任务";
  $("#sourceLink").href = task.input_url;
  $("#sourceLink").textContent = task.input_url;
  $("#totalCount").textContent = task.item_count;
  $("#selectedCount").textContent = task.selected_count;
  $("#completedCount").textContent = task.counts.completed || 0;
  $("#failedCount").textContent = (task.counts.failed || 0) + (task.counts.unsupported || 0);
  $("#selectAll").checked = task.item_count > 0 && task.selected_count === task.item_count;
  const parseRunning = Boolean(task.running && task.phase === "parsing");
  const transcriptionRunning = Boolean(task.running && task.phase === "transcription");
  const parseButton = $("#parseButton");
  const confirmButton = $("#confirmButton");
  parseButton.textContent = parseRunning ? "停止解析" : "开始解析";
  parseButton.disabled = transcriptionRunning || (parseRunning && Boolean(task.stop_requested));
  $("#browserActions").classList.toggle("hidden", task.status !== "waiting_browser");
  const auth = state.auth || {};
  const showQr = task.status === "waiting_qr";
  $("#qrLogin").classList.toggle("hidden", !showQr);
  $("#loginQrCode").classList.toggle("hidden", !auth.qrcode);
  if (auth.qrcode) $("#loginQrCode").src = auth.qrcode;
  $("#qrPlatform").textContent = auth.platform ? `${auth.platform} 扫码登录` : "扫码登录";
  $("#qrMessage").textContent = auth.message || "正在生成二维码…";
  $("#retryButton").classList.toggle("hidden", task.status !== "completed_with_errors" || task.running);
  const canStartTranscription = Boolean(task.can_start_transcription);
  confirmButton.textContent = transcriptionRunning ? "停止转录" : "开始转录";
  confirmButton.disabled = parseRunning
    || (transcriptionRunning && Boolean(task.stop_requested))
    || (!transcriptionRunning && (!canStartTranscription || task.selected_count === 0));
  $("#listHint").textContent = ["parsing", "browser", "waiting_browser", "waiting_qr"].includes(task.status)
    ? "结果正在持续加入列表"
    : `${task.selected_count} / ${task.item_count} 个视频已选择`;
  if (task.status === "cancelled") {
    showMessage(task.phase === "transcription" ? "转录已停止" : "解析已停止");
  }
  else if (task.error) showMessage(task.error, task.status === "failed");
  if (auth.status === "success" && state.lastAuthStatus !== "success") {
    showToast(auth.username ? `登录成功：${auth.username}` : "登录成功，正在继续解析");
  }
  state.lastAuthStatus = auth.status || "";
}

async function refreshTask() {
  if (!state.task) return;
  const data = await api(`/api/tasks/${state.task.id}`);
  state.task = data.task;
  state.auth = data.auth || {};
  renderTask();
  await loadItems();
}

function connectEvents(taskId) {
  state.events?.close();
  state.events = new EventSource(`/api/tasks/${taskId}/events`);
  state.events.onmessage = event => {
    const payload = JSON.parse(event.data);
    state.task = payload.task;
    state.auth = payload.auth || {};
    renderTask();
    clearTimeout(state.refreshTimer);
    state.refreshTimer = setTimeout(() => loadItems().catch(error => showMessage(error.message, true)), 120);
  };
  state.events.onerror = () => $("#serviceState").classList.add("error");
}

function showMessage(text, error = false) {
  const message = $("#message");
  message.textContent = text || "";
  message.classList.toggle("error", error);
}

function showToast(text) {
  const toast = $("#toast");
  toast.textContent = text;
  toast.classList.remove("hidden");
  clearTimeout(state.toastTimer);
  state.toastTimer = setTimeout(() => toast.classList.add("hidden"), 4200);
}

function renderLoginCaches(busy = false) {
  const list = $("#loginCacheList");
  if (!state.loginCaches.length) {
    list.innerHTML = '<div class="login-cache-empty">没有已保存的登录缓存</div>';
    return;
  }
  list.innerHTML = state.loginCaches.map(entry => `
    <article class="login-cache-row">
      <div class="login-cache-info">
        <strong>${escapeHTML(entry.platform)}</strong>
        <span>${escapeHTML(entry.domain)}</span>
        <span>${entry.username ? `当前账号：${escapeHTML(entry.username)}` : "账号未知"}</span>
      </div>
      <button type="button" class="danger clear-login-cache" data-key="${escapeHTML(entry.key)}" ${busy ? "disabled" : ""}>
        清除登录缓存
      </button>
    </article>`).join("");
  document.querySelectorAll(".clear-login-cache").forEach(button => {
    button.addEventListener("click", () => clearLoginCache(button.dataset.key));
  });
}

async function loadLoginCaches() {
  $("#loginCacheList").innerHTML = '<div class="login-cache-empty">正在读取登录状态…</div>';
  const data = await api("/api/auth/cache");
  state.loginCaches = data.entries || [];
  $("#clearAllLoginCacheButton").disabled = Boolean(data.busy);
  renderLoginCaches(Boolean(data.busy));
}

async function clearLoginCache(key) {
  const entry = state.loginCaches.find(item => item.key === key);
  if (!entry) return;
  if (!window.confirm(`确定清除 ${entry.platform}（${entry.domain}）的登录缓存吗？`)) return;
  try {
    await api(`/api/auth/cache/${encodeURIComponent(key)}`, { method: "DELETE" });
    showToast(`${entry.platform} 的登录缓存已清除`);
    await loadLoginCaches();
  } catch (error) {
    showMessage(error.message, true);
    showToast(error.message);
  }
}

async function clearAllLoginCaches() {
  if (!window.confirm("确定清除所有平台的登录缓存吗？任务记录、下载缓存和转录结果不会被删除。")) return;
  try {
    await api("/api/auth/cache", { method: "DELETE" });
    state.loginCaches = [];
    renderLoginCaches(false);
    showToast("所有平台的登录缓存已清除");
  } catch (error) {
    showMessage(error.message, true);
    showToast(error.message);
  }
}

function closeLoginCacheModal() {
  $("#loginCacheModal").classList.add("hidden");
}

$("#loginCacheButton").addEventListener("click", async () => {
  $("#loginCacheModal").classList.remove("hidden");
  try { await loadLoginCaches(); }
  catch (error) { $("#loginCacheList").innerHTML = `<div class="login-cache-empty">${escapeHTML(error.message)}</div>`; }
});
$("#closeLoginCacheButton").addEventListener("click", closeLoginCacheModal);
$("#clearAllLoginCacheButton").addEventListener("click", clearAllLoginCaches);
$("#loginCacheModal").addEventListener("click", event => {
  if (event.target === event.currentTarget) closeLoginCacheModal();
});
document.addEventListener("keydown", event => {
  if (event.key === "Escape") closeLoginCacheModal();
});

$("#urlForm").addEventListener("submit", async event => {
  event.preventDefault();
  const button = $("#parseButton");
  button.disabled = true;
  const parseRunning = state.task?.running && state.task.phase === "parsing";
  if (parseRunning) {
    try {
      await api(`/api/tasks/${state.task.id}/stop-parsing`, { method: "POST" });
      await refreshTask();
    } catch (error) {
      showMessage(error.message, true);
      renderTask();
    }
    return;
  }
  showMessage("正在创建解析任务…");
  try {
    const data = await api("/api/tasks", { method: "POST", body: JSON.stringify({ url: $("#urlInput").value }) });
    state.task = data.task;
    state.auth = data.auth || {};
    state.lastAuthStatus = "";
    state.items = [];
    state.loaded = 100;
    renderTask();
    renderItems();
    connectEvents(state.task.id);
    showMessage("解析已开始。需要登录时将显示二维码或打开 Chrome。", false);
  } catch (error) { showMessage(error.message, true); }
  finally {
    if (state.task) renderTask();
    else button.disabled = false;
  }
});

$("#selectAll").addEventListener("change", async event => {
  if (!state.task) return;
  try {
    await api(`/api/tasks/${state.task.id}/selection`, { method: "PUT", body: JSON.stringify({ selected: event.target.checked }) });
    await refreshTask();
  } catch (error) { showMessage(error.message, true); }
});

async function sendBrowserAction(action) {
  try {
    await api(`/api/tasks/${state.task.id}/browser-action`, {
      method: "POST", body: JSON.stringify({ action })
    });
    showMessage(action === "continue" ? "登录状态已确认，继续解析…" : "无需登录，继续解析…");
  } catch (error) { showMessage(error.message, true); }
}

$("#loginCompleteButton").addEventListener("click", () => sendBrowserAction("continue"));
$("#skipLoginButton").addEventListener("click", () => sendBrowserAction("skip"));

$("#confirmButton").addEventListener("click", async () => {
  const button = $("#confirmButton");
  button.disabled = true;
  try {
    if (state.task?.running && state.task.phase === "transcription") {
      await api(`/api/tasks/${state.task.id}/stop-transcription`, { method: "POST" });
      await refreshTask();
    } else {
      await api(`/api/tasks/${state.task.id}/confirm`, { method: "POST" });
      showMessage("已开始下载和转录。");
      await refreshTask();
    }
  } catch (error) {
    showMessage(error.message, true);
    renderTask();
  }
});

$("#retryButton").addEventListener("click", async () => {
  try { const data = await api(`/api/tasks/${state.task.id}/retry`, { method: "POST" }); showMessage(`已重新加入 ${data.reset} 个项目。`); }
  catch (error) { showMessage(error.message, true); }
});

$("#videoList").addEventListener("scroll", async event => {
  const el = event.currentTarget;
  if (el.scrollTop + el.clientHeight < el.scrollHeight - 80) return;
  if (!state.task || state.items.length >= state.task.item_count) return;
  state.loaded = Math.min(state.loaded + 100, state.task.item_count);
  await loadItems();
});

(async function restoreLatest() {
  try {
    const data = await api("/api/tasks/latest");
    if (!data.task) return;
    state.task = data.task;
    state.auth = data.auth || {};
    state.lastAuthStatus = state.auth.status || "";
    $("#urlInput").value = state.task.input_url;
    renderTask();
    await loadItems();
    connectEvents(state.task.id);
  } catch (error) { showMessage(error.message, true); }
})();
