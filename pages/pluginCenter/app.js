const bridge = window.AstrBotPluginPage;

const state = {
  builtinTerms: [],
  customTerms: [],
  allowR18: false,
  loaded: false,
  pendingToggle: new Set(),
  pendingR18: false,
};

const $ = (id) => document.getElementById(id);
const els = {
  globalError: $("globalError"),
  globalErrorMessage: $("globalErrorMessage"),
  retryBtn: $("retryBtn"),
  builtinTerms: $("builtinTerms"),
  builtinCount: $("builtinCount"),
  builtinSearch: $("builtinSearch"),
  customTerms: $("customTerms"),
  customCount: $("customCount"),
  termForm: $("termForm"),
  termInput: $("termInput"),
  termError: $("termError"),
  r18Toggle: $("r18Toggle"),
  toast: $("toast"),
};

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function apiResult(result) {
  if (result?.success === false) throw new Error(result.error || "请求失败");
  return result || {};
}

async function apiGet(endpoint, params = {}) {
  return apiResult(await bridge.apiGet(endpoint, params));
}

async function apiPost(endpoint, body = {}) {
  return apiResult(await bridge.apiPost(endpoint, body));
}

function showToast(message, tone = "normal") {
  els.toast.textContent = message;
  els.toast.className = `toast show${tone === "error" ? " error" : ""}`;
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => { els.toast.className = "toast"; }, 3000);
}

function showGlobalError(message) {
  els.globalErrorMessage.textContent = message || "部分数据加载失败。";
  els.globalError.hidden = false;
}

function hideGlobalError() {
  els.globalError.hidden = true;
  els.globalErrorMessage.textContent = "";
}

function setButtonBusy(button, busy, busyLabel, idleLabel) {
  button.disabled = busy;
  button.textContent = busy ? busyLabel : idleLabel;
  button.setAttribute("aria-busy", String(busy));
}

function renderSafety() {
  const query = els.builtinSearch.value.trim().toLocaleLowerCase("zh-CN");
  const builtin = state.builtinTerms.filter((item) =>
    String(item.term).toLocaleLowerCase("zh-CN").includes(query)
  );
  const enabledCount = state.builtinTerms.filter((item) => item.enabled).length;
  els.builtinCount.textContent = `${state.builtinTerms.length} 项 · 启用 ${enabledCount}`;
  els.customCount.textContent = `${state.customTerms.length} 项`;

  renderR18Toggle();
  els.builtinTerms.innerHTML = builtin.length
    ? builtin.map((item) => `
        <button class="safety-toggle${item.enabled ? " on" : ""}"
          type="button"
          data-term="${escapeHtml(item.term)}"
          aria-pressed="${item.enabled ? "true" : "false"}">${escapeHtml(item.term)}</button>
      `).join("")
    : '<div class="empty">没有匹配的内置安全词</div>';

  els.builtinTerms.querySelectorAll("[data-term]").forEach((button) => {
    button.addEventListener("click", () => toggleBuiltinTerm(button));
  });

  els.customTerms.innerHTML = state.customTerms.length
    ? state.customTerms.map((item) => `
        <div class="term-item">
          <span><strong>${escapeHtml(item.term)}</strong><small>${escapeHtml(item.added_by || "web")} · ${escapeHtml(formatDate(item.added_at))}</small></span>
          <button type="button" data-remove-term="${escapeHtml(item.term)}">删除</button>
        </div>
      `).join("")
    : '<div class="empty">还没有自定义屏蔽词</div>';

  els.customTerms.querySelectorAll("[data-remove-term]").forEach((button) => {
    button.addEventListener("click", async () => {
      const term = button.dataset.removeTerm;
      button.disabled = true;
      try {
        await apiPost("content-safety/terms/remove", { term });
        state.customTerms = state.customTerms.filter((item) => item.term !== term);
        renderSafety();
        showToast("自定义屏蔽词已删除");
      } catch (error) {
        button.disabled = false;
        showToast(error.message, "error");
      }
    });
  });
}

function formatDate(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  const pad = (n) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function renderR18Toggle() {
  els.r18Toggle.classList.toggle("on", state.allowR18);
  els.r18Toggle.setAttribute("aria-pressed", String(state.allowR18));
}

async function toggleR18() {
  if (state.pendingR18) return;
  state.pendingR18 = true;
  const nextEnabled = !state.allowR18;

  // 立即切换 class 并输出当前状态
  state.allowR18 = nextEnabled;
  renderR18Toggle();
  console.log(`[safety-toggle] R18 -> ${nextEnabled ? "ON" : "OFF"}`, {
    allowR18: nextEnabled,
  });

  try {
    await apiPost("content-safety/r18-toggle", { enabled: nextEnabled });
    showToast(`R18 内容${nextEnabled ? "已允许" : "已限制"}`);
  } catch (error) {
    state.allowR18 = !nextEnabled;
    renderR18Toggle();
    showToast(error.message, "error");
  } finally {
    state.pendingR18 = false;
  }
}

async function toggleBuiltinTerm(button) {
  const term = button.dataset.term;
  const item = state.builtinTerms.find((entry) => entry.term === term);
  if (!item || state.pendingToggle.has(term)) return;
  state.pendingToggle.add(term);
  const nextEnabled = !item.enabled;

  // 立即切换 class 并输出当前状态
  button.classList.toggle("on", nextEnabled);
  button.setAttribute("aria-pressed", String(nextEnabled));
  console.log(`[safety-toggle] ${term} -> ${nextEnabled ? "ON" : "OFF"}`, {
    term,
    enabled: nextEnabled,
  });
  item.enabled = nextEnabled;

  try {
    await apiPost("content-safety/terms/toggle", { term, enabled: nextEnabled });
    renderSafety();
    showToast(`安全词「${term}」已${nextEnabled ? "启用" : "停用"}`);
  } catch (error) {
    item.enabled = !nextEnabled;
    renderSafety();
    showToast(error.message, "error");
  } finally {
    state.pendingToggle.delete(term);
  }
}

async function loadSafety() {
  try {
    const result = await apiGet("content-safety");
    state.builtinTerms = Array.isArray(result.builtin_terms) ? result.builtin_terms : [];
    state.customTerms = Array.isArray(result.custom_terms) ? result.custom_terms : [];
    state.allowR18 = Boolean(result.allow_r18);
    state.loaded = true;
    hideGlobalError();
    renderSafety();
  } catch (error) {
    showGlobalError(error.message || "内容安全数据读取失败");
    showToast(error.message || "内容安全数据读取失败", "error");
    throw error;
  }
}

function bindEvents() {
  els.builtinSearch.addEventListener("input", renderSafety);
  els.r18Toggle.addEventListener("click", toggleR18);
  els.retryBtn.addEventListener("click", async () => {
    try {
      await loadSafety();
    } catch { /* handled inside loadSafety */ }
  });

  els.termForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const submitButton = event.currentTarget.querySelector('button[type="submit"]');
    const term = els.termInput.value.trim();
    if (!term) return;
    els.termError.textContent = "";
    setButtonBusy(submitButton, true, "添加中…", "添加屏蔽词");
    try {
      await apiPost("content-safety/terms/add", { term });
      els.termInput.value = "";
      showToast("自定义屏蔽词已添加");
      await loadSafety();
    } catch (error) {
      els.termError.textContent = error.message || "添加失败，请检查内容后重试。";
      els.termInput.focus();
      showToast(els.termError.textContent, "error");
    } finally {
      setButtonBusy(submitButton, false, "添加中…", "添加屏蔽词");
    }
  });
}

async function start() {
  if (!bridge) {
    showToast("AstrBot 页面桥接不可用，请在 AstrBot 内置环境中打开", "error");
    return;
  }
  await bridge.ready();
  bindEvents();
  try {
    await loadSafety();
  } catch { /* handled inside loadSafety */ }
}

start();
