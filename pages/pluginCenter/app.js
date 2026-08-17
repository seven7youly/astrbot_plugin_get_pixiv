const bridge = window.AstrBotPluginPage;

const state = {
  builtinTerms: [],
  customTerms: [],
  allowR18: false,
  loaded: false,
  pendingToggle: new Set(),
  pendingR18: false,
  config: {},
  configSchema: {},
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
  configForm: $("configForm"),
  configSaveBtn: $("configSaveBtn"),
  llmConfigForm: $("llmConfigForm"),
  llmConfigSaveBtn: $("llmConfigSaveBtn"),
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

function switchView(name) {
  document.querySelectorAll(".workspace-nav [data-view]").forEach((button) => {
    const active = button.dataset.view === name;
    button.classList.toggle("active", active);
    button.setAttribute("aria-current", active ? "page" : "false");
  });
  document.querySelectorAll(".workspace").forEach((view) => {
    view.classList.toggle("active", view.id === `${name}View`);
  });
}

function toggleConfigBool(button) {
  const key = button.dataset.key;
  state.config[key] = !state.config[key];
  button.classList.toggle("on", state.config[key]);
  button.setAttribute("aria-pressed", String(state.config[key]));
  console.log(`[config-toggle] ${key} -> ${state.config[key] ? "ON" : "OFF"}`, {
    key,
    enabled: state.config[key],
  });
}

function renderConfigField(key, meta) {
  const value = state.config[key];
  const label = meta.label || key;
  const hint = meta.hint ? `<p class="config-hint">${escapeHtml(meta.hint)}</p>` : "";
  const full = meta.type === "text" ? " full" : "";
  let control = "";

  if (meta.type === "bool") {
    control = `
      <button class="toggle-switch${value ? " on" : ""}" type="button"
        data-key="${escapeHtml(key)}" aria-pressed="${value ? "true" : "false"}">
        ${value ? "开启" : "关闭"}
      </button>`;
  } else if (meta.type === "select") {
    control = `
      <select data-key="${escapeHtml(key)}">
        ${(meta.options || []).map((opt) =>
          `<option value="${escapeHtml(opt)}"${String(opt) === String(value) ? " selected" : ""}>${escapeHtml(opt)}</option>`
        ).join("")}
      </select>`;
  } else if (meta.type === "int" || meta.type === "float") {
    const step = meta.type === "float" ? "0.5" : "1";
    control = `
      <input type="number" data-key="${escapeHtml(key)}"
        value="${escapeHtml(value)}"
        min="${escapeHtml(meta.min ?? "")}" max="${escapeHtml(meta.max ?? "")}"
        step="${step}" />`;
  } else if (meta.type === "text") {
    control = `
      <textarea data-key="${escapeHtml(key)}" rows="3">${escapeHtml(value)}</textarea>`;
  } else {
    control = `
      <input type="text" data-key="${escapeHtml(key)}" value="${escapeHtml(value)}" />`;
  }

  return `
    <div class="config-field${full}">
      <label>${escapeHtml(label)}</label>
      ${control}
      ${hint}
    </div>`;
}

function renderConfigGroup(container, group) {
  const schema = state.configSchema || {};
  const keys = Object.keys(schema).filter((key) => (schema[key]?.group || "basic") === group);
  if (!keys.length) {
    container.innerHTML = '<div class="empty">暂无配置项</div>';
    return;
  }
  container.innerHTML = `
    <div class="config-group full">
      <div class="config-group-grid">
        ${keys.map((key) => renderConfigField(key, schema[key] || {})).join("")}
      </div>
    </div>
  `;

  // 布尔开关
  container.querySelectorAll(".toggle-switch").forEach((button) => {
    button.addEventListener("click", () => toggleConfigBool(button));
  });
  // 数值/文本输入
  container.querySelectorAll("input[data-key], select[data-key], textarea[data-key]").forEach((input) => {
    const key = input.dataset.key;
    const meta = schema[key] || {};
    const sync = () => {
      if (meta.type === "int" || meta.type === "float") {
        state.config[key] = input.value === "" ? "" : (meta.type === "float" ? Number(input.value) : parseInt(input.value, 10));
      } else {
        state.config[key] = input.value;
      }
    };
    input.addEventListener("input", sync);
    input.addEventListener("change", sync);
  });
}

function renderConfigForm() {
  const schema = state.configSchema || {};
  if (!Object.keys(schema).length) {
    els.configForm.innerHTML = '<div class="empty">配置加载失败，请点击上方“重新加载”。</div>';
    els.llmConfigForm.innerHTML = '<div class="empty">配置加载失败，请点击上方“重新加载”。</div>';
    return;
  }
  renderConfigGroup(els.configForm, "basic");
  renderConfigGroup(els.llmConfigForm, "llm");
}

async function loadConfig() {
  try {
    const result = await apiGet("config");
    state.config = result.config || {};
    state.configSchema = result.schema || {};
    hideGlobalError();
    renderConfigForm();
  } catch (error) {
    showGlobalError(error.message || "插件配置读取失败");
    showToast(error.message || "插件配置读取失败", "error");
    throw error;
  }
}

async function saveConfig(button) {
  const btn = button || els.configSaveBtn;
  setButtonBusy(btn, true, "保存中…", "保存配置");
  try {
    const result = await apiPost("config", { config: state.config });
    showToast("插件配置已保存");
    if (result.persisted === false) {
      showToast("配置已应用，但写入文件失败，请检查日志", "error");
    }
  } catch (error) {
    showToast(error.message || "保存失败", "error");
  } finally {
    setButtonBusy(btn, false, "保存中…", "保存配置");
  }
}

function bindEvents() {
  document.querySelectorAll(".workspace-nav [data-view]").forEach((button) => {
    button.addEventListener("click", () => switchView(button.dataset.view));
  });
  els.builtinSearch.addEventListener("input", renderSafety);
  els.r18Toggle.addEventListener("click", toggleR18);
  els.configSaveBtn.addEventListener("click", () => saveConfig(els.configSaveBtn));
  els.llmConfigSaveBtn.addEventListener("click", () => saveConfig(els.llmConfigSaveBtn));
  els.retryBtn.addEventListener("click", async () => {
    try {
      await Promise.allSettled([loadSafety(), loadConfig()]);
    } catch { /* handled inside loaders */ }
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
  const hash = location.hash.slice(1);
  switchView(["safety", "config", "llm"].includes(hash) ? hash : "safety");
  try {
    await Promise.allSettled([loadSafety(), loadConfig()]);
  } catch { /* handled inside loaders */ }
}

start();
