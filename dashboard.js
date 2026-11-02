'use strict';

const $ = selector => document.querySelector(selector);
const tabs = [...document.querySelectorAll('[data-tab]')];
const settingForms = [...document.querySelectorAll('.settings-form')];
let state = null;
let selectedBot = '';
let selectionVersion = 0;
let pendingWrites = 0;
let switchingBot = false;
let editingBot = null;
let editingMemory = null;
let memorySequence = 0;
let searchTimer;
let expressionSequence = 0;
let expressionTimer;

function node(tag, text = '', className = '') {
  const element = document.createElement(tag);
  element.textContent = text;
  element.className = className;
  return element;
}

function notice(element, text, error = false) {
  element.textContent = text;
  element.dataset.kind = error ? 'error' : 'normal';
  element.hidden = !text;
}

async function api(path, method = 'GET', body, botId = selectedBot) {
  const writing = method !== 'GET';
  if (writing) pendingWrites++;
  try {
    let response;
    try {
      response = await fetch(`api/${path}`, {
        method, credentials: 'same-origin', cache: 'no-store',
        headers: { 'Content-Type': body instanceof Blob ? 'application/zip' : 'application/json', ...(botId ? { 'X-Bot-ID': botId } : {}) },
        body: body === undefined ? undefined : body instanceof Blob ? body : JSON.stringify(body),
      });
    } catch {
      throw new Error('Could not confirm the request. Check your connection; your edits are still here.');
    }
    const data = await response.json().catch(() => ({}));
    if (response.status === 401) showSignIn(data.error);
    if (!response.ok) throw new Error(data.error || `The dashboard returned HTTP ${response.status}.`);
    return data;
  } finally {
    if (writing) pendingWrites--;
  }
}

function showSignIn(message = 'This dashboard is for my owner.') {
  state = null;
  selectionVersion++;
  memorySequence++;
  $('#app').hidden = true;
  $('#sign-in').hidden = false;
  $('#sign-in-help').hidden = false;
  $('#sign-in-message').textContent = message;
}

function switchTab(name) {
  if (!tabs.some(tab => tab.dataset.tab === name)) name = 'overview';
  for (const tab of tabs) {
    const selected = tab.dataset.tab === name;
    tab.setAttribute('aria-selected', String(selected));
    tab.tabIndex = selected ? 0 : -1;
    $(`#panel-${tab.dataset.tab}`).hidden = !selected;
  }
  history.replaceState(null, '', `${location.pathname}#${name}`);
  if (name === 'memories' && state) loadMemories();
  document.dispatchEvent(new CustomEvent('hearthling:tab', { detail: name }));
}

for (const tab of tabs) tab.addEventListener('click', () => switchTab(tab.dataset.tab));
$('#tabs').addEventListener('keydown', event => {
  const current = tabs.indexOf(document.activeElement);
  if (current < 0) return;
  let index;
  if (event.key === 'ArrowDown' || event.key === 'ArrowRight') index = (current + 1) % tabs.length;
  if (event.key === 'ArrowUp' || event.key === 'ArrowLeft') index = (current + tabs.length - 1) % tabs.length;
  if (event.key === 'Home') index = 0;
  if (event.key === 'End') index = tabs.length - 1;
  if (index === undefined) return;
  event.preventDefault();
  switchTab(tabs[index].dataset.tab);
  tabs[index].focus();
});
const narrow = matchMedia('(max-width: 640px)');
function tabDirection() { $('#tabs').setAttribute('aria-orientation', narrow.matches ? 'horizontal' : 'vertical'); }
narrow.addEventListener('change', tabDirection);
tabDirection();

const fallbackEffortOptions = [
  ['inherit', 'Same as main model'], ['', 'Provider default'],
  ['none', 'none'], ['minimal', 'minimal'], ['low', 'low'], ['medium', 'medium'], ['high', 'high'], ['xhigh', 'xhigh'], ['max', 'max'],
];

function fallbackRows() { return [...$('#ai-fallback-list').querySelectorAll('.fallback-row')]; }

function updateFallbackRows() {
  const rows = fallbackRows();
  rows.forEach((row, index) => {
    row.querySelector('[data-fallback-model]').setAttribute('aria-label', `Fallback model ${index + 1}`);
    row.querySelector('[data-fallback-effort]').setAttribute('aria-label', `Reasoning effort for fallback ${index + 1}`);
    const up = row.querySelector('[data-fallback-up]');
    const down = row.querySelector('[data-fallback-down]');
    up.disabled = index === 0;
    down.disabled = index === rows.length - 1;
    up.setAttribute('aria-label', `Move fallback ${index + 1} up`);
    down.setAttribute('aria-label', `Move fallback ${index + 1} down`);
    row.querySelector('[data-fallback-remove]').setAttribute('aria-label', `Remove fallback ${index + 1}`);
  });
  $('#ai-fallback-empty').hidden = rows.length > 0;
  $('#add-ai-fallback').disabled = rows.length >= 10;
}

function changeFallbackRows() {
  updateFallbackRows();
  $('#ai-fallback-list').dispatchEvent(new Event('input', { bubbles: true }));
}

function addFallbackRow(model = '', effort = undefined) {
  const row = node('div', '', 'fallback-row');
  const modelInput = document.createElement('input');
  modelInput.type = 'text';
  modelInput.required = true;
  modelInput.maxLength = 150;
  modelInput.spellcheck = false;
  modelInput.placeholder = 'provider/model';
  modelInput.dataset.fallbackModel = '';
  modelInput.value = model;
  const effortInput = document.createElement('select');
  effortInput.dataset.fallbackEffort = '';
  for (const [value, label] of fallbackEffortOptions) {
    const option = node('option', label);
    option.value = value;
    effortInput.append(option);
  }
  effortInput.value = effort === undefined ? 'inherit' : effort ?? '';
  const actions = node('div', '', 'fallback-actions');
  const up = node('button', '↑', 'secondary small');
  const down = node('button', '↓', 'secondary small');
  const remove = node('button', 'Remove', 'text-button small');
  for (const button of [up, down, remove]) button.type = 'button';
  up.dataset.fallbackUp = '';
  down.dataset.fallbackDown = '';
  remove.dataset.fallbackRemove = '';
  up.addEventListener('click', () => {
    if (row.previousElementSibling) {
      row.parentNode.insertBefore(row, row.previousElementSibling);
      changeFallbackRows();
    }
  });
  down.addEventListener('click', () => {
    if (row.nextElementSibling) {
      row.parentNode.insertBefore(row, row.nextElementSibling.nextElementSibling);
      changeFallbackRows();
    }
  });
  remove.addEventListener('click', () => { row.remove(); changeFallbackRows(); });
  actions.append(up, down, remove);
  row.append(modelInput, effortInput, actions);
  $('#ai-fallback-list').append(row);
}

function renderFallbacks() {
  const efforts = state.settings.ai_fallback_reasoning_efforts || {};
  $('#ai-fallback-list').replaceChildren();
  for (const model of state.settings.ai_fallback_models || []) {
    addFallbackRow(model, Object.prototype.hasOwnProperty.call(efforts, model) ? efforts[model] : undefined);
  }
  updateFallbackRows();
}

function fallbackValues() {
  const models = [];
  const efforts = {};
  for (const row of fallbackRows()) {
    const model = row.querySelector('[data-fallback-model]').value.trim();
    const effort = row.querySelector('[data-fallback-effort]').value;
    if (!model) continue;
    if (models.includes(model)) throw new Error('List each fallback model only once.');
    models.push(model);
    if (effort !== 'inherit') efforts[model] = effort || null;
  }
  return { ai_fallback_models: models, ai_fallback_reasoning_efforts: efforts };
}

$('#add-ai-fallback').addEventListener('click', () => {
  if (fallbackRows().length >= 10) return;
  addFallbackRow();
  changeFallbackRows();
  const rows = fallbackRows();
  rows[rows.length - 1].querySelector('input').focus();
});

function fillSettings(form) {
  for (const input of form.querySelectorAll('[data-setting]')) {
    const saved = state.settings[input.dataset.setting];
    const value = input.dataset.subkey ? saved?.[input.dataset.subkey] || '' : saved;
    if (input.type === 'checkbox') input.checked = value;
    else if (input.multiple) for (const option of input.options) option.selected = (value || []).includes(option.value);
    else if (input.dataset.type === 'json') input.value = JSON.stringify(value, null, 2);
    else if (input.dataset.type === 'lines') input.value = (value || []).join('\n');
    else input.value = value ?? '';
  }
  if (form.id === 'connections-form') renderFallbacks();
}

function fieldValue(input) {
  if (input.dataset.optional && !input.value.trim()) return undefined;
  if (input.dataset.nullable && !input.value.trim()) return null;
  if (input.type === 'checkbox') return input.checked;
  if (input.multiple) return [...input.selectedOptions].map(option => option.value);
  if (input.type === 'number') {
    if (!Number.isFinite(input.valueAsNumber)) throw new Error(`Enter a number for ${input.labels?.[0]?.textContent || input.name}.`);
    return input.valueAsNumber;
  }
  if (input.dataset.type === 'lines') return [...new Set(input.value.split('\n').map(value => value.trim()).filter(Boolean))];
  if (input.dataset.type === 'json') {
    try { return JSON.parse(input.value.trim() || '{}'); }
    catch { throw new Error(`${input.labels?.[0]?.textContent || input.name || 'This field'} needs valid JSON. Your edits are still here.`); }
  }
  return input.value;
}

function formValues(form) {
  const settings = {};
  for (const input of form.querySelectorAll('[data-setting]')) {
    const key = input.dataset.setting;
    if (input.dataset.subkey) {
      settings[key] ||= { ...state.settings[key] };
      settings[key][input.dataset.subkey] = fieldValue(input);
    } else settings[key] = fieldValue(input);
  }
  if (form.id === 'connections-form') Object.assign(settings, fallbackValues());
  const keys = {};
  for (const input of form.querySelectorAll('[data-key]')) keys[input.dataset.key] = input.value.trim();
  const fallbacks = [...form.querySelectorAll('[data-key-item="image_fallback_keys"]')].map(input => input.value.trim()).filter(Boolean);
  const clearFallbacks = form.querySelector('#clear-image-fallbacks')?.checked;
  if (clearFallbacks && fallbacks.length) throw new Error('Choose replacement fallback keys or clear the saved list, not both.');
  if (fallbacks.length || clearFallbacks) keys.image_fallback_keys = clearFallbacks ? [] : fallbacks;
  return { settings, keys };
}

function connectionHints() {
  const preferred = state.characters?.find(item => item.name === state.settings.active_character)?.provider || state.settings.provider_order?.[0] || 'current';
  const provider = state.providers?.find(item => item.id === preferred);
  const fallbacks = (state.settings.ai_fallback_models || []).filter(model => model !== state.settings.ai_model).length;
  $('#chat-model').textContent = preferred === 'current' && fallbacks
    ? `${state.settings.ai_model} + ${fallbacks} fallback${fallbacks === 1 ? '' : 's'}` : provider?.model || state.settings.ai_model;
  const imageProvider = state.settings.image_provider;
  $('#image-provider-name').textContent = state.settings.image_providers?.find(item => item.id === imageProvider)?.name
    || 'Default image provider';
  $('#ai-key-help').textContent = state.connections.chat ? 'A key is saved. Leave blank to keep it.' : 'No key is saved. Add one to connect conversation.';
  $('#image-key-help').textContent = state.connections.image ? 'A key is saved. Leave blank to keep it.' : 'No image key is saved, so the conversation key is used.';
  $('#image-fallback-help').textContent = `${state.connections.image_keys || 0} distinct keys saved, including the primary. Blank fields keep the saved list; entering keys replaces the whole fallback list. Only a confirmed daily-quota rejection tries the next key.`;
  $('#brave-key-help').textContent = state.connections.brave ? 'A key is saved. Leave blank to keep it.' : 'No key is saved. Add your Brave API key here.';
}

for (const form of settingForms) {
  form.addEventListener('input', () => {
    form.classList.add('dirty');
    notice(form.querySelector('.form-status'), 'Unsaved changes');
  });
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (!form.reportValidity()) return;
    const button = form.querySelector('[type=submit]');
    const label = button.textContent;
    button.disabled = true;
    button.textContent = 'Saving…';
    try {
      const values = formValues(form);
      state = await api('settings', 'POST', values);
      let unchanged = false;
      try { unchanged = JSON.stringify(values) === JSON.stringify(formValues(form)); } catch { /* Preserve newer invalid edits too. */ }
      if (unchanged) {
        fillSettings(form);
        for (const input of form.querySelectorAll('[data-key], [data-key-item]')) input.value = '';
        if (form.querySelector('#clear-image-fallbacks')) form.querySelector('#clear-image-fallbacks').checked = false;
        form.classList.remove('dirty');
      }
      applyState(state);
      notice(form.querySelector('.form-status'), unchanged ? 'Saved. Applies to the next request.' : 'Saved earlier changes. Your newer edits are not saved yet.');
    } catch (error) {
      notice(form.querySelector('.form-status'), error.message, true);
    } finally {
      button.disabled = false;
      button.textContent = label;
    }
  });
}

function timeLabel(value) {
  return value ? new Date(value).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '';
}

const actionNames = {
  generate_image: 'Generate picture', host_status: 'Host health check',
  web_search: 'Brave web search', web_fetch: 'Read a web page', image_search: 'Find web images', post_image: 'Post a web image',
  lookup_members: 'Find member identity', ping_member: 'Ping a member', set_my_colour: 'Change own role colour',
  list_expressions: 'List server expressions', react: 'React to a message', send_sticker: 'Send a sticker',
  add_emoji: 'Add a server emoji', add_sticker: 'Add a server sticker',
  remember: 'Remember a fact', recall: 'Recall memories', forget: 'Forget a memory', dashboard: 'Dashboard sign-in',
};

async function refreshStatus() {
  if (!state || switchingBot) return;
  const version = selectionVersion;
  try {
    const status = await api('status');
    if (!state || version !== selectionVersion) return;
    state.bots = status.bots;
    state.bot = status.bots.find(bot => bot.id === selectedBot);
    $('#connection-status').textContent = botStatus(state.bot);
    $('#connection-dot').classList.toggle('connected', status.ready);
    $('#request-status').textContent = `${status.active_requests} active ${status.active_requests === 1 ? 'request' : 'requests'}${status.ready && status.latency_ms !== null ? ` · ${status.latency_ms} ms response time` : ''}`;
    $('#memory-count').textContent = status.memory_count;
    const labels = [['chat', '#chat-status'], ['generate_image', '#image-status'], ['web_search', '#search-status'], ['host_status', '#host-status']];
    for (const [name, selector] of labels) {
      const check = status.checks[name];
      let text = check ? `${check.ok ? 'Last request succeeded' : check.error || 'Last request failed'} · ${timeLabel(check.checked_at)}` : 'Not checked yet';
      if (name === 'generate_image' && state.settings.image_provider === 'default' && !state.connections.image && !state.connections.chat) text = 'No image or conversation key saved';
      if (name === 'chat' && !state.connections.chat && (!state.settings.provider_order?.length || state.settings.provider_order[0] === 'current')) text = 'No current-connection key saved';
      if (name === 'web_search' && !state.connections.brave) text = 'No Brave key saved';
      if (name !== 'chat' && state.settings.bot_mode === 'chatbot') text = 'Available to assistant bots';
      $(selector).textContent = text;
    }
    $('#activity-list').replaceChildren();
    $('#activity-empty').hidden = status.activity.length > 0;
    for (const entry of status.activity) {
      const item = node('li', '', 'activity-row');
      const outcome = entry.uncertain ? 'Unconfirmed' : entry.ok === true ? 'Succeeded' : entry.ok === false ? 'Failed' : 'In progress';
      item.dataset.state = entry.ok === true ? 'success' : entry.ok === false ? 'error' : 'pending';
      const detail = node('div');
      detail.append(node('p', actionNames[entry.action] || entry.action.replaceAll('_', ' '), 'activity-title'));
      detail.append(node('p', entry.error ? `${outcome}: ${entry.error}` : outcome, 'activity-detail'));
      if (entry.picture_available) {
        const link = node('a', 'Download picture', 'activity-detail');
        link.href = `api/pictures/${entry.request_id}?bot=${encodeURIComponent(selectedBot)}`;
        detail.append(link);
      }
      const when = node('time', timeLabel(entry.time));
      if (entry.time) { when.dateTime = entry.time; when.title = new Date(entry.time).toLocaleString(); }
      item.append(node('span', '', 'status-dot'), detail, when);
      $('#activity-list').append(item);
    }
    if (!pendingWrites) renderBots();
  } catch (error) { if (version === selectionVersion) notice($('#page-notice'), error.message, true); }
}

function checkResult(kind, data) {
  const container = $('#check-result');
  container.hidden = false;
  container.dataset.kind = data.ok ? 'normal' : 'error';
  container.replaceChildren();
  if (!data.ok) { container.append(node('p', data.error)); return; }
  const result = data.result;
  if (kind === 'chat') { container.append(node('p', `Conversation connected using ${result.model}.`)); return; }
  if (kind === 'web_search') {
    container.append(node('h3', 'Brave Search'));
    container.append(node('p', `Fetched ${new Date(result.fetched_at).toLocaleString()}${result.cached ? ' · cached result' : ''}`, 'field-help'));
    const list = node('ol');
    for (const source of result.results) {
      const item = node('li');
      const link = node('a', source.title);
      link.href = source.url; link.target = '_blank'; link.rel = 'noopener noreferrer';
      item.append(link, node('p', source.snippet, 'field-help')); list.append(item);
    }
    container.append(list);
    if (!result.results.length) container.append(node('p', 'No web results were returned for this search.'));
    return;
  }
  container.append(node('h3', 'Host machine health'));
  const facts = node('dl');
  const gb = bytes => `${(bytes / 1e9).toFixed(1)} GB`;
  const metrics = [
    ['Host', result.host], ['Running for', `${Math.floor(result.uptime_seconds / 86400)} days, ${Math.floor(result.uptime_seconds / 3600) % 24} hours`],
    ['Memory available', `${gb(result.memory_available_bytes)} of ${gb(result.memory_total_bytes)}`],
    ['Disk space free', `${gb(result.disk_free_bytes)} of ${gb(result.disk_total_bytes)}`],
    ['Network', result.interfaces.map(item => `${item.name}: ${item.state}`).join(', ') || 'No external interfaces found'],
  ];
  for (const [label, value] of metrics) facts.append(node('dt', label), node('dd', value));
  container.append(facts);
  const services = node('ul');
  for (const service of result.services) services.append(node('li', `${service.Id}: ${service.LoadState === 'not-found' ? 'not installed' : service.ActiveState === 'active' ? 'running' : service.ActiveState}`));
  if (result.note) container.append(node('p', result.note, 'field-help'));
  container.append(services, node('p', `Checked ${new Date(result.checked_at).toLocaleString()}. No changes made.`, 'field-help'));
}

for (const button of document.querySelectorAll('[data-check]')) button.addEventListener('click', async () => {
  const label = button.textContent;
  button.disabled = true; button.textContent = 'Checking…';
  try {
    checkResult(button.dataset.check, await api(`check/${button.dataset.check}`, 'POST', {}));
  }
  catch (error) { checkResult(button.dataset.check, { ok: false, error: error.message }); }
  finally { button.disabled = button.dataset.check !== 'chat' && state?.settings.bot_mode === 'chatbot'; button.textContent = label; refreshStatus(); }
});
$('#refresh-status').addEventListener('click', refreshStatus);

function options(select, values, emptyLabel) {
  const previous = select.value;
  select.replaceChildren(new Option(emptyLabel, ''));
  for (const value of values) select.add(new Option(value.name, value.id));
  select.value = previous;
}

function scopeLabel(values, identity) { return values.find(value => value.id === identity)?.name || identity; }

async function loadMemories(more = false) {
  if (!state || switchingBot) return;
  const sequence = ++memorySequence;
  const query = new URLSearchParams({ q: $('#memory-search').value, author: $('#filter-person').value, channel: $('#filter-channel').value, limit: 50 });
  if (more) query.set('offset', $('#memory-list').children.length);
  try {
    const data = await api(`memories?${query}`);
    if (sequence !== memorySequence || !state) return;
    if (!more) $('#memory-list').replaceChildren();
    $('#memory-empty').textContent = query.get('q') || query.get('author') || query.get('channel') ? 'No memories match these filters.' : `No memories yet. Ask ${state.bot.name} to remember a preference in Discord, or add one here.`;
    for (const memory of data.memories) {
      const item = node('li', '', 'memory-row');
      const content = node('div');
      content.append(node('p', memory.fact, 'memory-fact'));
      if (memory.keywords) content.append(node('p', `Keywords: ${memory.keywords}`, 'memory-meta'));
      content.append(node('p', `${scopeLabel(state.people, memory.author_id)} · ${memory.channel_id ? `Across channels · learned in ${scopeLabel(state.channels, memory.channel_id)}` : 'Server-wide'}`, 'memory-meta'));
      const actions = node('div', '', 'memory-actions');
      const edit = node('button', 'Edit', 'text-button');
      edit.type = 'button'; edit.setAttribute('aria-label', `Edit memory: ${memory.fact}`);
      edit.addEventListener('click', () => openMemory(memory));
      const forget = node('button', 'Forget', 'text-button');
      forget.type = 'button'; forget.setAttribute('aria-label', `Forget memory: ${memory.fact}`);
      forget.addEventListener('click', async () => {
        if (!forget.dataset.confirm) {
          forget.dataset.confirm = 'true'; forget.textContent = 'Confirm forget'; forget.classList.add('danger');
          forget.setAttribute('aria-label', `Confirm forgetting: ${memory.fact}`);
          const cancel = node('button', 'Cancel', 'text-button');
          cancel.type = 'button';
          cancel.addEventListener('click', () => { delete forget.dataset.confirm; forget.textContent = 'Forget'; forget.setAttribute('aria-label', `Forget memory: ${memory.fact}`); forget.classList.remove('danger'); cancel.remove(); });
          actions.append(cancel);
          return;
        }
        forget.disabled = true;
        try {
          await api(`memories/${memory.id}`, 'DELETE', {});
          notice($('#memory-notice'), 'Memory forgotten. This person’s recent context was cleared across channels and bots.');
          await loadMemories(); refreshStatus();
        } catch (error) { notice($('#memory-notice'), error.message, true); forget.disabled = false; }
      });
      actions.append(edit, forget); item.append(content, actions); $('#memory-list').append(item);
    }
    const shown = $('#memory-list').children.length;
    $('#memory-empty').hidden = shown > 0;
    $('#memory-more').hidden = shown >= data.total;
  } catch (error) { if (sequence === memorySequence) notice($('#memory-notice'), error.message, true); }
}

function memoryScope() {
  const shared = $('#memory-shared').checked;
  $('#memory-channel').required = !shared;
  $('#memory-channel').disabled = !!editingMemory || shared;
  $('#memory-shared').disabled = !!editingMemory;
}

function openMemory(memory = null) {
  if ($('#memory-editor').classList.contains('dirty')) {
    notice($('#memory-notice'), 'Save or cancel the current memory edit first.'); return;
  }
  editingMemory = memory?.id || null;
  $('#memory-editor-title').textContent = memory ? 'Edit memory' : 'Add a memory';
  $('#memory-person').value = memory?.author_id || $('#filter-person').value || state.owner_id;
  $('#memory-channel').value = memory?.channel_id || $('#filter-channel').value;
  $('#memory-shared').checked = memory?.channel_id === '';
  memoryScope();
  $('#memory-person').disabled = !!memory;
  $('#memory-fact').value = memory?.fact || '';
  $('#memory-keywords').value = memory?.keywords || '';
  $('#memory-editor').hidden = false;
  notice($('#memory-editor .form-status'), '');
  $('#new-memory').disabled = true;
  $('#memory-fact').focus();
}

function closeMemory() {
  $('#memory-editor').hidden = true;
  $('#memory-editor').classList.remove('dirty');
  $('#new-memory').disabled = false;
  editingMemory = null;
  $('#memory-shared').checked = false;
  $('#memory-keywords').value = '';
  memoryScope();
}

$('#new-memory').addEventListener('click', () => openMemory());
$('#cancel-memory').addEventListener('click', closeMemory);
$('#memory-editor').addEventListener('input', () => $('#memory-editor').classList.add('dirty'));
$('#memory-editor').addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.currentTarget;
  if (!form.reportValidity()) return;
  const values = { author_id: $('#memory-person').value, channel_id: $('#memory-shared').checked ? '' : $('#memory-channel').value, fact: $('#memory-fact').value, keywords: $('#memory-keywords').value, memory_id: editingMemory };
  const controls = [...form.querySelectorAll('input, select, textarea, button')].map(control => [control, control.disabled]);
  for (const [control] of controls) control.disabled = true;
  try {
    await api('memories', 'POST', values);
    closeMemory(); notice($('#memory-notice'), 'Memory saved.'); await loadMemories(); refreshStatus();
  } catch (error) { notice(form.querySelector('.form-status'), error.message, true); }
  finally { for (const [control, disabled] of controls) control.disabled = disabled; }
});
$('#memory-search').addEventListener('input', () => { clearTimeout(searchTimer); searchTimer = setTimeout(loadMemories, 200); });
for (const selector of ['#filter-person', '#filter-channel']) $(selector).addEventListener('change', loadMemories);
$('#memory-more').addEventListener('click', () => loadMemories(true));
$('#memory-shared').addEventListener('change', memoryScope);

function botStatus(bot) {
  if (!bot.enabled) return 'Paused';
  if (bot.error) return bot.error;
  if (bot.ready) return bot.in_server ? 'Connected to Discord' : 'Connected to Discord; invite this bot to its server';
  return bot.running ? 'Connecting to Discord…' : 'Discord is disconnected';
}

function accountChangeBlocked() {
  if (pendingWrites || switchingBot) {
    notice($('#page-notice'), 'Wait for the current save or connection check to finish.'); return true;
  }
  if (document.querySelector('form.dirty')) {
    notice($('#page-notice'), 'Save or cancel your current edits before changing bot accounts.'); return true;
  }
  return false;
}

function renderBots() {
  const focusId = $('#bot-list').contains(document.activeElement) ? document.activeElement.id : '';
  const groups = [['assistant', 'Assistant bots'], ['chatbot', 'Chatbots']].map(([mode, label]) => ({ mode, label, bots: state.bots.filter(bot => (bot.bot_mode || 'assistant') === mode) }));
  const signature = JSON.stringify(groups.map(group => [group.mode, group.bots.map(bot => [bot.id, bot.name])]));
  if ($('#bot-select').dataset.options !== signature) {
    $('#bot-select').replaceChildren();
    for (const group of groups) if (group.bots.length) {
      const options = document.createElement('optgroup'); options.label = group.label;
      options.append(...group.bots.map(bot => new Option(bot.name, bot.id))); $('#bot-select').append(options);
    }
    $('#bot-select').dataset.options = signature;
  }
  $('#bot-select').value = selectedBot;
  $('#add-bot').disabled = !state.manage_bots;
  $('#bot-list').replaceChildren();
  for (const group of groups) {
    const section = node('section', '', 'bot-group');
    const title = node('h2', group.label); title.id = `group-${group.mode}`;
    section.setAttribute('aria-labelledby', title.id);
    const list = node('ul', '', 'bot-list');
    section.append(title, node('p', group.mode === 'assistant' ? 'Conversation, memories and practical tools, with owner-only moderation.' : 'Conversation, memories and existing emojis, reactions and stickers.', 'field-help'), list);
    if (!group.bots.length) section.append(node('p', `No ${group.mode === 'assistant' ? 'assistant bots' : 'chatbots'} added yet. Choose the type when adding a bot.`, 'field-help'));
    $('#bot-list').append(section);
    for (const bot of group.bots) {
      const item = node('li', '', 'bot-row');
      const detail = node('div');
      detail.append(node('h3', bot.name), node('p', botStatus(bot), 'muted'));
      detail.append(node('p', `Account ${bot.id}${bot.account_name ? ` · ${bot.account_name}` : ''}`, 'muted'));
      detail.append(node('p', `Server ${bot.server_id}`, 'muted'));
      const links = node('div', '', 'bot-links');
      for (const [label, url] of [['Invite to server', bot.invite_url], ['Developer portal', bot.portal_url]]) {
        if (!url) continue;
        const link = node('a', label);
        link.href = url; link.target = '_blank'; link.rel = 'noopener noreferrer'; links.append(link);
      }
      detail.append(links);
      const actions = node('div', '', 'bot-actions');
      const edit = node('button', 'Edit', 'text-button');
      edit.id = `edit-bot-${bot.id}`;
      edit.type = 'button'; edit.disabled = !state.manage_bots; edit.setAttribute('aria-label', `Edit bot: ${bot.name}`);
      edit.addEventListener('click', () => openBot(bot));
      const toggle = node('button', bot.running ? 'Pause' : bot.enabled ? 'Reconnect' : 'Start', 'secondary small');
      toggle.id = `toggle-bot-${bot.id}`;
      toggle.type = 'button'; toggle.disabled = !state.manage_bots;
      toggle.setAttribute('aria-label', `${toggle.textContent} bot: ${bot.name}`);
      toggle.addEventListener('click', async () => {
        if (accountChangeBlocked()) return;
        toggle.disabled = true;
        try {
          await api(`bots/${bot.id}`, 'POST', { name: bot.name, server_id: bot.server_id, enabled: !bot.running });
          notice($('#bot-notice'), bot.running ? `${bot.name} paused. Settings and memories are kept.` : `${bot.name} is connecting. Check its status below.`);
          await refreshStatus();
        } catch (error) { notice($('#bot-notice'), error.message, true); }
        finally { toggle.disabled = false; }
      });
      actions.append(edit, toggle); item.append(detail, actions); list.append(item);
    }
  }
  if (focusId) document.getElementById(focusId)?.focus({ preventScroll: true });
}

function openBot(bot = null) {
  if (accountChangeBlocked()) return;
  editingBot = bot?.id || null;
  $('#bot-editor-title').textContent = bot ? `Edit ${bot.name}` : 'Add a Discord bot';
  $('#bot-editor-help').textContent = bot ? 'Account changes reconnect this bot if it is running. Its settings and memories are kept.' : 'Starts with a copy of the main bot\'s provider keys and tool settings, and a separate character. Edit it after adding.';
  $('#bot-name').value = bot?.name || '';
  $('#bot-mode').value = bot ? bot.bot_mode || 'assistant' : 'chatbot';
  $('#bot-server').value = bot?.server_id || state.bot.server_id;
  $('#bot-enabled').checked = bot?.enabled || false;
  $('#bot-token').value = ''; $('#bot-token').required = !bot;
  $('#bot-token-help').textContent = bot ? 'Leave blank to keep the saved token. Replacements must belong to this same account.' : 'Verified privately with Discord. Saved tokens are never shown.';
  notice($('#bot-editor .form-status'), '');
  $('#bot-editor').hidden = false;
  $('#bot-name').focus();
}

function closeBot() {
  $('#bot-editor').hidden = true;
  $('#bot-editor').classList.remove('dirty');
  $('#bot-token').value = '';
  editingBot = null;
}

function applyState(data) {
  const changedBot = selectedBot && selectedBot !== data.bot.id;
  state = data;
  selectedBot = state.bot.id;
  for (const form of settingForms) if (!form.classList.contains('dirty')) fillSettings(form);
  connectionHints();
  const assistant = state.settings.bot_mode !== 'chatbot';
  $('#selected-bot').textContent = `Editing ${state.bot.name} · ${assistant ? 'Assistant' : 'Chatbot'} · ${state.bot.id}`;
  $('#bot-mode-note').hidden = assistant;
  $('#reminder-mode-note').hidden = assistant;
  for (const element of document.querySelectorAll('[data-assistant-only]')) {
    element.hidden = !assistant;
    if (element.tagName === 'FIELDSET') element.disabled = !assistant;
  }
  for (const button of document.querySelectorAll('[data-check]')) if (button.dataset.check !== 'chat') button.disabled = !assistant;
  expressionSequence++;
  if (changedBot) $('#expression-search').value = '';
  renderExpressions(state.expressions || {});
  $('#character-heading').textContent = `${state.effective_name || state.bot.name}'s character.`;
  $('#name-trigger-label').textContent = `Answer to ‘${state.effective_name || state.bot.name}’`;
  $('#system-prompt').textContent = state.system_prompt;
  document.title = `${state.bot.name} · Hearthling settings`;
  options($('#filter-person'), state.people, 'All people'); options($('#filter-channel'), state.channels, 'All conversations');
  options($('#memory-person'), state.people, 'Choose a person'); options($('#memory-channel'), state.channels, 'Choose a conversation');
  renderBots();
  document.dispatchEvent(new CustomEvent('hearthling:state', { detail: { changedBot } }));
}

$('#bot-select').addEventListener('change', async event => {
  const identity = event.target.value;
  if (identity === selectedBot) return;
  if (accountChangeBlocked()) { event.target.value = selectedBot; return; }
  switchingBot = true; selectionVersion++; memorySequence++;
  $('#bot-select').disabled = true; $('#main').inert = true;
  $('#main').setAttribute('aria-busy', 'true');
  try {
    const data = await api('state', 'GET', undefined, identity);
    closeMemory(); closeBot();
    $('#memory-search').value = ''; $('#filter-person').value = ''; $('#filter-channel').value = '';
    $('#check-result').hidden = true;
    for (const input of document.querySelectorAll('[data-key]')) input.value = '';
    for (const status of document.querySelectorAll('.form-status')) notice(status, '');
    notice($('#page-notice'), ''); applyState(data);
    $('#memory-list').replaceChildren(); $('#memory-more').hidden = true; $('#activity-list').replaceChildren();
  } catch (error) {
    $('#bot-select').value = selectedBot;
    notice($('#page-notice'), error.message, true);
  } finally {
    switchingBot = false; $('#bot-select').disabled = false; $('#main').inert = false;
    $('#main').removeAttribute('aria-busy');
    await refreshStatus();
    if ($('#tab-memories').getAttribute('aria-selected') === 'true') loadMemories();
  }
});

$('#add-bot').addEventListener('click', () => openBot());
$('#cancel-bot').addEventListener('click', closeBot);
$('#bot-editor').addEventListener('input', () => {
  $('#bot-editor').classList.add('dirty'); notice($('#bot-editor .form-status'), 'Unsaved changes');
});
$('#bot-editor').addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.currentTarget;
  if (!form.reportValidity()) return;
  const values = { name: $('#bot-name').value, bot_mode: $('#bot-mode').value, server_id: $('#bot-server').value, token: $('#bot-token').value.trim(), enabled: $('#bot-enabled').checked };
  form.classList.add('dirty');
  const controls = [...form.querySelectorAll('input, select, button')];
  for (const control of controls) control.disabled = true;
  notice(form.querySelector('.form-status'), values.token ? 'Verifying the account with Discord…' : 'Saving account…');
  try {
    await api(editingBot ? `bots/${editingBot}` : 'bots', 'POST', values);
    const data = await api('state');
    closeBot();
    applyState(data);
    notice($('#bot-notice'), `${values.name} saved as ${values.bot_mode === 'assistant' ? 'an assistant' : 'a chatbot'}. Choose it in ‘Editing bot’ to customise its character.`);
    await refreshStatus();
  } catch (error) { notice(form.querySelector('.form-status'), error.message, true); }
  finally { for (const control of controls) control.disabled = false; }
});

function renderExpressions(data) {
  const list = $('#expression-list'); list.replaceChildren();
  const query = $('#expression-search').value.trim().toLocaleLowerCase();
  const items = [...(data.emojis || []).map(item => ({ ...item, kind: 'Emoji' })), ...(data.stickers || []).map(item => ({ ...item, kind: 'Sticker' }))].filter(item => item.name.toLocaleLowerCase().includes(query));
  for (const item of items) {
    const row = node('li', '', 'expression-row');
    if (/^https:\/\/(?:cdn\.discordapp\.com|media\.discordapp\.net)\/(?:emojis|stickers)\/[^?]+\.(?:png|gif|webp)(?:\?|$)/.test(item.url || '')) {
      const image = document.createElement('img'); image.src = item.url; image.alt = ''; image.width = image.height = 48; image.loading = 'lazy'; image.decoding = 'async'; row.append(image);
    }
    const detail = node('div');
    detail.append(node('strong', item.name), node('p', `${item.kind} · ${item.id}`, 'field-help'));
    if (item.description) detail.append(node('p', item.description, 'field-help'));
    if (item.markup) detail.append(node('code', item.markup));
    row.append(detail); list.append(row);
  }
  notice($('#expression-status'), data.connected === false ? 'Connect this bot to load its server expressions.' : items.length ? `Showing ${items.filter(item => item.kind === 'Emoji').length} emojis and ${items.filter(item => item.kind === 'Sticker').length} stickers available to this bot.` : 'No available server expressions match. Assistants can add one from an image supplied in Discord.');
}

async function loadExpressions() {
  if (!state || switchingBot) return;
  const sequence = ++expressionSequence, version = selectionVersion;
  try {
    const data = await api(`expressions?q=${encodeURIComponent($('#expression-search').value)}`);
    if (state && sequence === expressionSequence && version === selectionVersion) renderExpressions(data);
  } catch (error) { if (sequence === expressionSequence && version === selectionVersion) notice($('#expression-status'), error.message, true); }
}
$('#expression-search').addEventListener('input', () => { clearTimeout(expressionTimer); expressionTimer = setTimeout(loadExpressions, 200); });
$('#refresh-expressions').addEventListener('click', loadExpressions);

async function initialise() {
  try {
    const fragment = new URLSearchParams(location.hash.slice(1));
    if (fragment.has('login')) {
      const token = fragment.get('login');
      history.replaceState(null, '', location.pathname);
      await api('login', 'POST', { token });
    }
    applyState(await api('state'));
    $('#sign-in').hidden = true; $('#app').hidden = false;
    switchTab(location.hash.slice(1)); await refreshStatus();
  } catch (error) { showSignIn(error.message); }
}

$('#retry-sign-in').addEventListener('click', initialise);
$('#send-link').addEventListener('click', async () => {
  const button = $('#send-link');
  const message = $('#sign-in-message');
  button.disabled = true;
  message.textContent = 'Asking Discord to send you a link…';
  try {
    await api('request-link', 'POST', {});
    message.textContent = 'Sent. Open the private link in your Discord DMs; it expires in ten minutes.';
  } catch (error) { message.textContent = error.message; }
  finally { button.disabled = false; }
});
$('#sign-out').addEventListener('click', async () => {
  if (document.querySelector('form.dirty') || pendingWrites || switchingBot) {
    notice($('#page-notice'), 'Save your changes before signing out.'); return;
  }
  try { await api('logout', 'POST', {}); location.replace(location.pathname); }
  catch (error) { notice($('#page-notice'), error.message, true); }
});
window.addEventListener('beforeunload', event => {
  if (document.querySelector('form.dirty')) { event.preventDefault(); event.returnValue = ''; }
});
setInterval(() => { if (!document.hidden && state) refreshStatus(); }, 20000);
document.addEventListener('visibilitychange', () => { if (!document.hidden && state) refreshStatus(); });
initialise();
