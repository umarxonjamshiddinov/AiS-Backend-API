(() => {
"use strict";
const $ = id => {
  const element = document.getElementById(id);
  if (!element) throw new Error("Интерфейс не загрузился полностью. Обновите страницу.");
  return element;
};
let adminToken = "", state = null, editingKey = null;
const number = value => Number(value || 0).toLocaleString();
const limit = value => value == null ? "∞" : number(value);
const money = value => "$" + Number(value || 0).toFixed(8);
const quotaFields = {
  "key-requests-day": "requests_per_day", "key-requests-month": "requests_per_month",
  "key-tokens-day": "tokens_per_day", "key-tokens-month": "tokens_per_month",
  "key-concurrency": "max_concurrent_requests"
};
const capabilityLabels = {
  text_input: "Текст: вход", text_output: "Текст: выход", streaming: "Streaming",
  system_prompt: "System / developer", tools: "Function tools", parallel_tool_calls: "Управление parallel calls",
  strict_tools: "Строгие function schemas", json_mode: "JSON mode", json_schema: "JSON schema",
  image_input: "Vision / image input", reasoning: "Reasoning effort", temperature: "Temperature"
};
function notice(message) {
  $("notice").textContent = message;
  for (const target of document.querySelectorAll(".dialog-notice")) {
    target.textContent = target.closest("dialog").open ? message : "";
  }
}
async function api(path, method = "GET", body) {
  const response = await fetch("/admin/api" + path, {
    method, cache: "no-store", mode: "same-origin", credentials: "same-origin", redirect: "error",
    headers: {Authorization: "Bearer " + adminToken, "Content-Type": "application/json"},
    body: body ? JSON.stringify(body) : undefined
  });
  let data;
  try { data = await response.json(); } catch {
    throw new Error("API временно недоступен (HTTP " + response.status + "). Повторите попытку.");
  }
  if (response.status === 401) {
    throw new Error("Неверный ADMIN_TOKEN или доступ запрещён.");
  }
  if (!response.ok) {
    throw new Error((data.error?.message || "request_failed") +
      (data.error?.param ? " (" + data.error.param + ")" : ""));
  }
  return data;
}
function cell(row, text, small) {
  const td = document.createElement("td"); td.textContent = text;
  if (small) { const s = document.createElement("small"); s.textContent = small; td.append(s); }
  row.append(td); return td;
}
function action(td, text, fn) {
  const b = document.createElement("button"); b.className = "secondary"; b.textContent = text;
  b.onclick = () => run(fn); td.append(b);
}
async function run(fn) { try { await fn(); } catch (e) { notice(e.message); } }
function status(row, enabled, text, detail) {
  const td = cell(row, ""); const p = document.createElement("span");
  p.className = "pill" + (enabled ? "" : " off"); p.textContent = text; td.append(p);
  if (detail) { const small = document.createElement("small"); small.textContent = detail; td.append(small); }
}
function keyState(k) {
  if (k.revoked_at) return "Отозван";
  if (!k.active) return "Отключён";
  if (k.expires_at && Date.parse(k.expires_at) <= Date.now()) return "Истёк";
  if (!state.users.some(user => user.id === k.user_id && user.active)) return "Владелец отключён";
  return "Активен";
}
async function refresh() {
  state = await api("/overview");
  $("base-url").textContent = state.public_api_base_url;
  $("issued-url").textContent = state.public_api_base_url;
  $("local-base-url").textContent = state.deployment_api_base_url;
  $("local-api").classList.toggle("hidden",
    state.public_api_base_url === state.deployment_api_base_url);
  $("stat-keys").textContent = number(state.keys.filter(k => keyState(k) === "Активен").length);
  $("stat-requests").textContent = number(state.stats.requests_today);
  $("stat-errors").textContent = "Ошибок: " + number(state.stats.errors_today);
  $("stat-tokens").textContent = number(state.stats.tokens_today);
  $("stat-active").textContent = number(state.stats.active_requests);
  $("provider-status").textContent = Object.entries(state.provider_details).map(([name, value]) =>
    name + ": " + (value.configured ? "credential настроен" : "не настроен") + " · " + value.transport).join(" | ");
  $("cost-summary").textContent = "Учтено: " + money(state.keys.reduce((a, k) => a + Number(k.spent_usd), 0)) +
    " · Активный резерв: " + money(state.keys.reduce((a, k) => a + Number(k.reserved_usd), 0));
  $("keys").replaceChildren(); $("archived-keys").replaceChildren();
  $("models").replaceChildren(); $("usage").replaceChildren();
  $("users").replaceChildren();
  for (const user of state.users) {
    const tr = document.createElement("tr"); cell(tr, user.username, user.id);
    status(tr, user.active, user.active ? "Активен" : "Отключён");
    action(cell(tr, ""), user.active ? "Отключить" : "Включить", async () => {
      await api("/users/" + user.id, "PUT", {active: !user.active}); await refresh();
    });
    $("users").append(tr);
  }
  $("new-key").disabled = !state.users.some(user => user.active);
  const userNames = Object.fromEntries(state.users.map(user => [user.id, user.username]));
  for (const k of state.keys) {
    const archived = keyState(k) !== "Активен";
    const tr = document.createElement("tr");
    cell(tr, k.name, (userNames[k.user_id] || k.user_id) + " · " + k.prefix + "…");
    cell(tr, k.all_public_models ? "Все публичные модели каталога" : k.models.join(", ") || "Нет доступа");
    cell(tr, (k.rpm == null ? "Без лимита" : number(k.rpm)) + " / " + (k.tpm == null ? "Без лимита" : number(k.tpm)),
      "Concurrency: " + number(k.active_requests) + " / " + limit(k.max_concurrent_requests));
    cell(tr, "Запросы: " + limit(k.requests_per_day) + " / " + limit(k.requests_per_month),
      "Токены: " + limit(k.tokens_per_day) + " / " + limit(k.tokens_per_month));
    status(tr, keyState(k) === "Активен", keyState(k),
      k.expires_at ? "До " + new Date(k.expires_at).toLocaleString() : "");
    if (archived) cell(tr, "Создан: " + new Date(k.created_at).toLocaleString(),
      "Отозван: " + (k.revoked_at ? new Date(k.revoked_at).toLocaleString() : "—"));
    const td = cell(tr, ""); action(td, "Изменить", () => keyDialog(k));
    if (!k.revoked_at) action(td, "Отозвать", async () => {
      await api("/keys/" + k.id + "/revoke", "POST"); await refresh();
    });
    $(archived ? "archived-keys" : "keys").append(tr);
  }
  for (const m of state.models) {
    const tr = document.createElement("tr"); cell(tr, m.display_name || m.alias, m.alias);
    cell(tr, m.manufacturer || m.provider, [m.region, m.api_route, m.upstream_model].filter(Boolean).join(" · "));
    const features = Object.entries(capabilityLabels).filter(([name]) => m.capabilities[name])
      .map(([, label]) => label).join(", ");
    cell(tr, (features || "—") + (m.vision_behavior === "vision_fallback" ? ", Vision через Qwen" : ""), "Output: " + (m.max_output_tokens == null ? "не указан" : number(m.max_output_tokens)) + " · Context: " +
      (m.max_context_tokens == null ? "не задан" : number(m.max_context_tokens)));
    status(tr, m.enabled, m.enabled ? "Включён" : "Отключён",
      m.is_public && m.external_api_allowed ? "Внешний API" : "Только внутренний");
    action(cell(tr, ""), "Изменить", () => modelDialog(m)); $("models").append(tr);
  }
  const names = Object.fromEntries(state.keys.map(k => [k.id, k.name]));
  for (const u of state.usage) {
    const tr = document.createElement("tr");
    cell(tr, new Date(u.created_at).toLocaleString(), names[u.key_id] || u.key_id);
    cell(tr, u.alias + " · " + u.provider, u.upstream_model);
    cell(tr, number(u.input_tokens) + " / " + number(u.output_tokens),
      (u.usage_estimated ? "Оценка: " : "Учтено: ") + number(u.accounted_tokens));
    status(tr, u.status === "completed", u.status, u.error_code || (number(u.latency_ms) + " ms"));
    const td = cell(tr, u.status === "reserved" ? "Резерв: " + number(u.reserved_tokens) : "Освобождён",
      u.lease_expires_at ? "До " + new Date(u.lease_expires_at).toLocaleTimeString() : "");
    if (u.usage_estimated && u.status !== "released" && u.status !== "reserved") {
      action(td, "Снять оценку", async () => {
        if (!confirm("Проверьте фактический расход у провайдера. Обнулить эту оценку?")) return;
        await api("/usage/" + u.id + "/release", "POST"); await refresh();
      });
    }
    $("usage").append(tr);
  }
}
$("login-form").onsubmit = async event => {
  event.preventDefault();
  const button = $("login-submit");
  if (button.disabled) return;
  button.disabled = true; button.textContent = "Входим…"; $("login-status").textContent = "";
  try {
    adminToken = $("admin-token").value.trim();
    await refresh();
    $("admin-token").value = "";
    $("login").classList.add("hidden"); $("app").classList.remove("hidden");
    $("logout").classList.remove("hidden"); notice("");
  } catch (error) {
    adminToken = ""; state = null;
    $("login-status").textContent = error.message;
  } finally { button.disabled = false; button.textContent = "Войти"; }
};
$("logout").onclick = () => { adminToken = ""; state = null; location.reload(); };
$("user-form").onsubmit = event => {
  event.preventDefault(); run(async () => {
    await api("/users", "POST", {username: $("user-name").value.trim()});
    $("user-name").value = ""; await refresh(); notice("Пользователь создан");
  });
};
$("credentials-form").onsubmit = event => {
  event.preventDefault(); run(async () => {
    await api("/credentials", "PUT", {provider: $("provider").value, api_key: $("provider-key").value});
    $("provider-key").value = ""; await refresh(); notice("Credential сохранён на сервере.");
  });
};
function keyDialog(k) {
  editingKey = k?.id || null; $("key-title").textContent = k ? "Изменить доступ" : "Новый ключ";
  $("key-name").value = k?.name || ""; $("key-budget").value = k?.budget_usd ?? "";
  $("key-user").replaceChildren();
  for (const user of state.users) {
    const option = document.createElement("option"); option.value = user.id;
    option.textContent = user.username + (user.active ? "" : " · отключён");
    option.disabled = !user.active && user.id !== k?.user_id;
    $("key-user").append(option);
  }
  $("key-user").value = k?.user_id || state.users.find(user => user.active)?.id || "";
  $("key-user").disabled = Boolean(k);
  $("key-all-public").checked = k?.all_public_models ?? true;
  $("permissions").classList.toggle("hidden", $("key-all-public").checked);
  $("key-rpm").value = k ? (k.rpm ?? "") : 30;
  $("key-tpm").value = k ? (k.tpm ?? "") : 2000000;
  for (const [id, field] of Object.entries(quotaFields)) {
    $(id).value = k ? (k[field] ?? "") : (field === "max_concurrent_requests" ? 5 : "");
  }
  $("key-active").checked = k?.active ?? true; $("key-active").disabled = Boolean(k?.revoked_at);
  $("key-revoked-note").classList.toggle("hidden", !k?.revoked_at);
  $("key-expiry").value = k?.expires_at
    ? new Date(Date.parse(k.expires_at) - new Date(k.expires_at).getTimezoneOffset() * 60000).toISOString().slice(0, 16) : "";
  $("permissions").replaceChildren();
  for (const m of state.models) {
    if (!m.is_public) continue;
    const label = document.createElement("label"); label.className = "check";
    const input = document.createElement("input"); input.type = "checkbox"; input.value = m.alias;
    input.checked = k?.models.includes(m.alias) || false;
    label.append(input, document.createTextNode(m.alias + (m.external_api_allowed ? "" : " · внутренний")));
    $("permissions").append(label);
  }
  $("key-dialog").showModal();
  notice("");
}
$("new-key").onclick = () => keyDialog();
$("show-archived-keys").onclick = () => $("archived-keys-dialog").showModal();
$("key-all-public").onchange = () => $("permissions").classList.toggle("hidden", $("key-all-public").checked);
$("key-form").onsubmit = event => {
  event.preventDefault(); run(async () => {
    const body = {
      name: $("key-name").value, models: [...$("permissions").querySelectorAll("input:checked")].map(c => c.value),
      user_id: $("key-user").value, all_public_models: $("key-all-public").checked,
      budget_usd: $("key-budget").value || null,
      rpm: $("key-rpm").value === "" ? null : Number($("key-rpm").value),
      tpm: $("key-tpm").value === "" ? null : Number($("key-tpm").value),
      active: $("key-active").checked, expires_at: $("key-expiry").value ? new Date($("key-expiry").value).toISOString() : null
    };
    for (const [id, field] of Object.entries(quotaFields)) body[field] = $(id).value ? Number($(id).value) : null;
    const result = await api(editingKey ? "/keys/" + editingKey : "/keys", editingKey ? "PUT" : "POST", body);
    $("key-dialog").close();
    if (result.key) { $("issued-key").value = result.key; $("issued-dialog").showModal(); }
    await refresh(); notice("Доступ сохранён");
  });
};
function checkbox(parent, name, label, checked, enabled) {
  const wrapper = document.createElement("label"); wrapper.className = "check";
  const input = document.createElement("input"); input.type = "checkbox"; input.value = name;
  input.checked = checked && enabled; input.disabled = !enabled;
  if (!enabled) wrapper.title = "Этот адаптер не поддерживает параметр";
  wrapper.append(input, document.createTextNode(label)); $(parent).append(wrapper); return input;
}
function capabilityEnabled(name) {
  return $("capabilities").querySelector('input[value="' + name + '"]').checked;
}
function updateCapabilityDependencies() {
  const ceiling = state.provider_details[$("model-provider").value].supported_capabilities;
  const tools = capabilityEnabled("tools"), reasoning = capabilityEnabled("reasoning");
  for (const input of $("tool-modes").querySelectorAll("input")) {
    input.disabled = !tools || !ceiling.tool_choice_modes.includes(input.value);
    if (input.disabled) input.checked = false;
  }
  for (const input of $("reasoning-modes").querySelectorAll("input")) {
    input.disabled = !reasoning || !ceiling.reasoning_efforts.includes(input.value);
    if (input.disabled) input.checked = false;
  }
  for (const name of ["parallel_tool_calls", "strict_tools"]) {
    const input = $("capabilities").querySelector('input[value="' + name + '"]');
    input.disabled = !tools || !ceiling[name]; if (input.disabled) input.checked = false;
  }
}
function renderCapabilities(caps) {
  const info = state.provider_details[$("model-provider").value], ceiling = info.supported_capabilities;
  $("model-transport").value = info.transport;
  $("model-mantle-fields").classList.toggle("hidden", $("model-provider").value !== "amazon_bedrock");
  $("capabilities").replaceChildren(); $("tool-modes").replaceChildren(); $("reasoning-modes").replaceChildren();
  for (const [name, label] of Object.entries(capabilityLabels)) {
    const input = checkbox("capabilities", name, label, caps[name], ceiling[name]);
    input.onchange = updateCapabilityDependencies;
  }
  for (const mode of ["auto", "none", "required", "function"]) {
    checkbox("tool-modes", mode, mode, caps.tool_choice_modes.includes(mode), ceiling.tool_choice_modes.includes(mode));
  }
  for (const effort of ["none", "minimal", "low", "medium", "high", "xhigh"]) {
    checkbox("reasoning-modes", effort, effort, caps.reasoning_efforts.includes(effort), ceiling.reasoning_efforts.includes(effort));
  }
  updateCapabilityDependencies();
}
function modelDialog(m) {
  $("model-alias").value = m?.alias || ""; $("model-alias").readOnly = Boolean(m);
  $("model-provider").value = m?.provider || "amazon_bedrock"; $("model-upstream").value = m?.upstream_model || "";
  $("model-provider").disabled = Boolean(m);
  $("model-key").value = m?.model_key || ""; $("model-key").readOnly = Boolean(m?.model_key);
  $("model-name").value = m?.display_name || "";
  $("model-manufacturer").value = m?.manufacturer || "";
  $("model-region").value = m?.region || "us-east-1";
  $("model-route").value = m?.api_route || "mantle_default";
  $("model-vision").value = m?.vision_behavior || "none";
  $("model-input").value = m?.input_price ?? 0; $("model-output").value = m?.output_price ?? 0;
  $("model-max").value = m ? (m.max_output_tokens ?? "") : 4096; $("model-context").value = m?.max_context_tokens ?? "";
  $("model-enabled").checked = m?.enabled ?? true; $("model-external").checked = m?.external_api_allowed ?? true;
  $("model-external").disabled = Boolean(m && !m.is_public);
  renderCapabilities(m?.capabilities || state.capability_defaults);
  $("model-dialog").showModal();
  notice("");
}
$("new-model").onclick = () => modelDialog();
$("model-provider").onchange = () => renderCapabilities(state.capability_defaults);
$("model-form").onsubmit = event => {
  event.preventDefault(); run(async () => {
    const caps = {...state.capability_defaults};
    for (const input of $("capabilities").querySelectorAll("input")) caps[input.value] = input.checked && !input.disabled;
    caps.tool_choice_modes = [...$("tool-modes").querySelectorAll("input:checked")].map(input => input.value);
    caps.tool_choice = caps.tool_choice_modes.length > 0;
    caps.reasoning_efforts = [...$("reasoning-modes").querySelectorAll("input:checked")].map(input => input.value);
    if (caps.reasoning && !caps.reasoning_efforts.length) throw new Error("Выберите доступные reasoning efforts");
    caps.structured_output = caps.json_schema; caps.vision = caps.image_input;
    const body = {
      alias: $("model-alias").value, provider: $("model-provider").value, upstream_model: $("model-upstream").value,
      input_price: $("model-input").value || "0", output_price: $("model-output").value || "0",
      max_output_tokens: $("model-max").value ? Number($("model-max").value) : null, max_context_tokens: $("model-context").value ? Number($("model-context").value) : null,
      enabled: $("model-enabled").checked, external_api_allowed: $("model-external").checked,
      provider_transport: $("model-transport").value, capabilities: caps
    };
    if (body.provider === "amazon_bedrock") Object.assign(body, {
      model_key: $("model-key").value || null, display_name: $("model-name").value || null,
      manufacturer: $("model-manufacturer").value || null,
      region: $("model-region").value, api_route: $("model-route").value,
      supports_vision: caps.image_input,
      vision_behavior: caps.image_input ? "native_vision" : $("model-vision").value
    });
    await api("/models", "PUT", body);
    $("model-dialog").close(); await refresh(); notice("Маршрут сохранён");
  });
};
$("refresh").onclick = () => run(refresh);
for (const button of document.querySelectorAll(".close")) button.onclick = () => button.closest("dialog").close();
$("issued-dialog").addEventListener("close", () => { $("issued-key").value = ""; });
$("copy-key").onclick = () => run(async () => { await navigator.clipboard.writeText($("issued-key").value); notice("Ключ скопирован"); });
})();
