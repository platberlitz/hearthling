'use strict';

// Selected-bot editors share the existing session, draft checks and settings saves.
const extraForms = [...document.querySelectorAll('.extra-form')];
const extraReads = {};
let personalZones = {};
let validatedImport = null;

function editorValues(form) {
  const values = {};
  for (const field of form.querySelectorAll('[name]')) {
    const value = fieldValue(field);
    if (value !== undefined) values[field.name] = value;
  }
  return values;
}

function resetEditor(form, hide = false) {
  form.reset(); form.classList.remove('dirty'); delete form.dataset.identity;
  for (const field of form.querySelectorAll('[readonly]')) field.readOnly = false;
  for (const field of form.querySelectorAll('[type=password], [name=headers]')) field.value = '';
  notice(form.querySelector('.form-status'), '');
  if (hide) form.hidden = true;
}

function edit(id, values = {}) {
  const form = document.getElementById(id);
  if (form.classList.contains('dirty') || pendingWrites) {
    notice($('#page-notice'), 'Save or cancel the current edit, and let pending saves finish first.'); return false;
  }
  resetEditor(form);
  for (const field of form.querySelectorAll('[name]')) {
    const value = values[field.name];
    if (field.type === 'checkbox') field.checked = !!value;
    else if (field.dataset.type === 'json') field.value = value === undefined && field.dataset.optional ? '' : JSON.stringify(value ?? {}, null, 2);
    else field.value = value ?? '';
  }
  form.hidden = false;
  for (let parent = form.parentElement; parent; parent = parent.parentElement) if (parent.tagName === 'DETAILS') parent.open = true;
  form.querySelector('input:not([type=hidden]), select, textarea')?.focus();
  return true;
}

for (const form of extraForms) {
  form.addEventListener('input', () => {
    form.classList.add('dirty'); notice(form.querySelector('.form-status'), 'Unsaved changes');
  });
}
for (const button of document.querySelectorAll('[data-cancel], [data-reset]')) button.addEventListener('click', () => {
  resetEditor(document.getElementById(button.dataset.cancel || button.dataset.reset), !!button.dataset.cancel);
});

function bindForm(id, handler) {
  const form = document.getElementById(id);
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (!form.reportValidity()) return;
    const controls = [...form.querySelectorAll('input, select, textarea, button')].map(field => [field, field.disabled]);
    form.classList.add('dirty');
    try {
      const values = editorValues(form);
      for (const [field] of controls) field.disabled = true;
      notice(form.querySelector('.form-status'), 'Working…');
      const message = await handler(form, values);
      notice(form.querySelector('.form-status'), message || 'Saved.');
      if (form.hidden) notice($('#page-notice'), message || 'Saved.');
    } catch (error) { notice(form.querySelector('.form-status'), error.message, true); }
    finally { for (const [field, disabled] of controls) field.disabled = disabled; }
  });
}

function actionButton(label, handler, { confirm = false, description = label } = {}) {
  const button = node('button', label, 'text-button');
  button.type = 'button'; button.setAttribute('aria-label', description);
  button.addEventListener('click', async () => {
    if (confirm && !button.dataset.confirmed) {
      button.dataset.confirmed = 'true'; button.textContent = `Confirm ${label.toLowerCase()}`;
      button.setAttribute('aria-label', `Confirm ${description.toLowerCase()}`);
      const cancel = node('button', 'Cancel', 'text-button'); cancel.type = 'button';
      cancel.addEventListener('click', () => {
        delete button.dataset.confirmed; button.textContent = label; button.setAttribute('aria-label', description); cancel.remove();
      });
      button.after(cancel); return;
    }
    button.disabled = true;
    try {
      const message = await handler();
      if (message) notice($('#page-notice'), message);
    } catch (error) { notice($('#page-notice'), error.message, true); }
    finally { button.disabled = false; }
  });
  return button;
}

function entity(list, title, detail, actions = []) {
  const row = node('li', '', 'entity-row'); const content = node('div');
  content.append(node('strong', title), node('p', detail, 'muted'));
  const buttons = node('div', '', 'entity-actions'); buttons.append(...actions);
  row.append(content, buttons); list.append(row);
}

async function refreshExtraState() { applyState(await api('state')); }

async function readExtra(path) {
  const serial = (extraReads[path] || 0) + 1; extraReads[path] = serial;
  const version = selectionVersion;
  const data = await api(path);
  return state && version === selectionVersion && extraReads[path] === serial ? data : null;
}

function choices(field, entries) {
  if (field.closest('form')?.classList.contains('dirty')) return;
  if (!('emptyLabel' in field.dataset)) field.dataset.emptyLabel = [...field.options].find(option => option.value === '')?.text || '';
  const saved = field.dataset.setting ? state.settings[field.dataset.setting] : field.multiple ? [...field.selectedOptions].map(option => option.value) : field.value;
  const wanted = field.multiple ? saved || [] : [saved || ''];
  const items = entries.map(item => new Option(item.name, item.id));
  if (!field.multiple && (field.dataset.emptyLabel || (field.required && !field.dataset.setting))) {
    items.unshift(new Option(field.dataset.emptyLabel || 'Choose one', ''));
  }
  for (const value of wanted.filter(Boolean)) if (!items.some(option => option.value === value)) items.push(new Option(`Saved selection: ${value}`, value));
  field.replaceChildren(...items);
  if (field.multiple) for (const option of field.options) option.selected = wanted.includes(option.value);
  else if (wanted[0] || items.some(option => !option.value)) field.value = wanted[0];
}

function populateChoices() {
  const groups = {
    people: state.people, conversations: state.channels,
    channels: state.channels.filter(channel => channel.server_text || channel.name.startsWith('#')),
    providers: (state.providers || []).map(item => ({ id: item.id, name: `${item.name} · ${item.model}` })),
    images: [{ id: 'default', name: 'Default image provider' }, ...(state.settings.image_providers || []).map(item => ({ id: item.id, name: item.name }))],
  };
  for (const field of document.querySelectorAll('[data-choices]')) choices(field, groups[field.dataset.choices] || []);
}

function renderCharacters() {
  const list = $('#character-list'); list.replaceChildren();
  const active = state.settings.active_character;
  $('#character-active').textContent = active ? `Active preset: ${active}.` : 'Using the default character instructions.';
  if (active) entity(list, 'Default character', 'Use the main personality field again.', [actionButton('Use default', async () => {
    applyState(await api('characters/activate', 'POST', { name: '' })); return 'Default character selected.';
  })]);
  for (const character of state.characters || []) entity(list, character.name,
    `${active === character.name ? 'Active' : 'Saved'}${character.provider ? ` · Provider: ${character.provider}` : ''}`,
    [actionButton('Edit', () => {
      if (edit('character-editor', character)) $('#character-name').readOnly = true;
    }, { description: `Edit character: ${character.name}` }),
    actionButton('Activate', async () => {
      applyState(await api('characters/activate', 'POST', { name: character.name })); return `${character.name} selected.`;
    }, { description: `Activate character: ${character.name}` }),
    actionButton('Delete', async () => {
      await api(`characters/${encodeURIComponent(character.name)}`, 'DELETE'); await refreshExtraState(); return 'Character deleted.';
    }, { confirm: true, description: `Delete character: ${character.name}` })]);
  if (!list.children.length) entity(list, 'No saved presets yet', 'Add a character, or keep using the default instructions.');
}

$('#new-character').addEventListener('click', () => {
  if (edit('character-editor', { markdown: '## System Persona\n\n\n## Example Dialogue\n\n\n## User Context\n', provider: '' })) $('#character-preview').hidden = true;
});
bindForm('character-editor', async (form, values) => {
  await api('characters', 'POST', values); resetEditor(form, true); await refreshExtraState(); return 'Character saved.';
});
$('#preview-character').addEventListener('click', async () => {
  const button = $('#preview-character'); button.disabled = true;
  try {
    const data = await api('characters/preview', 'POST', { markdown: $('#character-markdown').value, username: $('#character-preview-user').value });
    $('#character-preview').textContent = JSON.stringify(data, null, 2); $('#character-preview').hidden = false;
  } catch (error) { notice($('#character-editor .form-status'), error.message, true); }
  finally { button.disabled = false; }
});

const providerDefaults = { family: 'openai_chat', timeout: 90, max_tokens: 4096, thinking_budget: 0,
  vision: false, local_keyless: false, append_version: false, temperature: null, reasoning_effort: null, prefill: '', extra_body: {} };
function openProvider(profile = null) {
  if (!edit('provider-editor', { ...providerDefaults, ...profile, api_key: '', headers: undefined })) return;
  $('#provider-id').readOnly = !!profile;
  $('#provider-key-note').textContent = profile?.key_saved ? 'A key is saved. Leave blank to keep it.' : 'No key is saved for this profile.';
  $('#provider-headers-note').textContent = `Saved header names: ${profile?.header_names?.join(', ') || 'none'}. Values stay private; blank keeps them and {} clears them.`;
}
function renderProviders() {
  const list = $('#provider-list'); list.replaceChildren();
  for (const provider of state.providers || []) {
    const actions = [];
    if (provider.id !== 'current') actions.push(actionButton('Edit', () => openProvider(provider), { description: `Edit provider: ${provider.name}` }));
    actions.push(actionButton('Test', async () => {
      const result = await api(`providers/${provider.id}/check`, 'POST', {});
      if (!result.ok) throw new Error(result.error);
      return `Connection verified: ${provider.name}, ${result.model}.`;
    }, { description: `Test provider: ${provider.name}` }));
    if (provider.id !== 'current') actions.push(actionButton('Delete', async () => {
      await api(`providers/${provider.id}`, 'DELETE'); await refreshExtraState(); return 'Provider deleted.';
    }, { confirm: true, description: `Delete provider: ${provider.name}` }));
    entity(list, provider.name, `${provider.id} · ${provider.family} · ${provider.model} · ${provider.vision ? 'Vision on' : 'Text only'}`, actions);
  }
  renderProviderOrder();
}
$('#new-provider').addEventListener('click', () => openProvider());
bindForm('provider-editor', async (form, values) => {
  await api('providers', 'POST', values); resetEditor(form, true); await refreshExtraState(); return 'Provider saved.';
});

function orderValues() { return JSON.parse($('#provider-order').value || '[]'); }
function changeOrder(values) {
  $('#provider-order').value = JSON.stringify(values);
  $('#provider-order').dispatchEvent(new Event('input', { bubbles: true })); renderProviderOrder();
}
function mainConnectionDetail() {
  const fallbacks = (state.settings.ai_fallback_models || []).filter(model => model !== state.settings.ai_model);
  return `${state.settings.ai_model}${fallbacks.length ? `, then ${fallbacks.join(', then ')}` : ', with no fallback models'}.`;
}
function renderProviderOrder() {
  const list = $('#provider-order-list'); list.replaceChildren();
  const order = orderValues();
  if (!order.length) { entity(list, '1. Main connection', mainConnectionDetail()); return; }
  order.forEach((identity, index) => {
    const provider = state.providers.find(item => item.id === identity);
    const detail = identity === 'current' ? mainConnectionDetail() : provider ? `${provider.model} at ${provider.base_url}.` : 'This provider is no longer saved.';
    const actions = [];
    if (index > 0) actions.push(actionButton('Move up', () => {
      const values = orderValues(); [values[index - 1], values[index]] = [values[index], values[index - 1]]; changeOrder(values);
    }, { description: `Move provider up: ${identity}` }));
    if (index < order.length - 1) actions.push(actionButton('Move down', () => {
      const values = orderValues(); [values[index + 1], values[index]] = [values[index], values[index + 1]]; changeOrder(values);
    }, { description: `Move provider down: ${identity}` }));
    actions.push(actionButton('Remove', () => changeOrder(orderValues().filter(value => value !== identity)), { description: `Remove provider from order: ${identity}` }));
    entity(list, `${index + 1}. ${provider?.name || identity}`, `${detail} ${index ? 'Tried if everything above fails.' : 'Tried first, unless a character prefers another provider.'}`, actions);
  });
}
$('#add-provider-order').addEventListener('click', () => {
  const value = $('#provider-order-add').value; const order = orderValues();
  if (value && !order.includes(value)) changeOrder([...order, value]);
});

const imageDefaults = { family: 'openai_images', model: 'gpt-image-1', coordinator_model: 'gpt-5-mini', size: '1024x1024',
  quality: 'high', style: '', output_format: 'png', background: '', moderation: '', response_format: '', timeout: 360,
  local_keyless: false, extra_body: {} };
function openImageProfile(profile = null) {
  if (!edit('image-profile-editor', { ...imageDefaults, ...profile, api_key: '' })) return;
  $('#image-profile-id').readOnly = !!profile;
  $('#image-profile-key-note').textContent = profile && state.image_connections[profile.id] ? 'A key is saved. Leave blank to keep it.' : 'No key is saved for this profile.';
}
function renderImages() {
  const list = $('#image-profile-list'); list.replaceChildren();
  for (const profile of state.settings.image_providers || []) entity(list, profile.name, `${profile.id} · ${profile.model} · ${profile.size}`, [
    actionButton('Edit', () => openImageProfile(profile), { description: `Edit image provider: ${profile.name}` }),
    actionButton('Delete', async () => {
      await api(`image-providers/${profile.id}`, 'DELETE'); await refreshExtraState(); return 'Image provider deleted.';
    }, { confirm: true, description: `Delete image provider: ${profile.name}` }),
  ]);
  if (!list.children.length) entity(list, 'Using the default image provider', 'Add another provider only when you need it.');
}
$('#new-image-profile').addEventListener('click', () => openImageProfile());
bindForm('image-profile-editor', async (form, values) => {
  const { api_key, ...profile } = values;
  await api('image-providers', 'POST', { profile, api_key }); resetEditor(form, true); await refreshExtraState(); return 'Image provider saved.';
});

function channelName(identity) { return state.channels.find(channel => channel.id === identity)?.name || identity; }
function personName(identity) { return state.people.find(person => person.id === identity)?.name || identity; }
function renderAutomation() {
  const list = $('#channel-rule-list'); list.replaceChildren();
  for (const [channel, rule] of Object.entries(state.settings.channel_rules)) entity(list, channelName(channel),
    `${rule.enabled ? `${rule.chance}% reply chance · ${rule.cooldown}s cooldown` : 'Chance-based replies off'} · Names ${rule.nickname_trigger ? 'on' : 'off'}`,
    [actionButton('Edit', () => edit('channel-rule-editor', { channel, ...rule }), { description: `Edit channel rule: ${channelName(channel)}` }),
    actionButton('Remove', async () => {
      const rules = { ...state.settings.channel_rules }; delete rules[channel];
      applyState(await api('settings', 'POST', { settings: { channel_rules: rules } })); return 'Channel override removed.';
    }, { confirm: true, description: `Remove channel rule: ${channelName(channel)}` })]);
  if (!list.children.length) entity(list, 'No channel overrides', 'Mentions, names and direct replies still use the normal access settings.');
  const stopped = state.bot_interactions_stopped;
  $('#bot-talk-status').textContent = stopped ? 'Conversations between bots are stopped programme-wide.' : 'Managed bots can converse when their own settings and channel rules allow it.';
  $('#toggle-bot-talk').textContent = stopped ? 'Resume bot conversations' : 'Stop all bot conversations';
  const schedule = $('#schedule-list'); schedule.replaceChildren();
  state.settings.unavailable_windows.forEach((window, index) => entity(schedule,
    `${window.days.map(day => ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'][day]).join(', ')} · ${window.start} to ${window.end}`,
    `${state.settings.timezone}${window.end <= window.start ? ' · Continues into the next day' : ''}`,
    [actionButton('Remove', async () => {
      applyState(await api('settings', 'POST', { settings: { unavailable_windows: state.settings.unavailable_windows.filter((_, i) => i !== index) } }));
      return 'Quiet period removed.';
    }, { description: `Remove quiet period: ${index + 1}` })]));
  if (!schedule.children.length) entity(schedule, 'No quiet periods', 'Other access controls still apply.');
}
bindForm('channel-rule-editor', async (form, values) => {
  const { channel, ...rule } = values;
  const data = await api('settings', 'POST', { settings: { channel_rules: { ...state.settings.channel_rules, [channel]: rule } } });
  resetEditor(form); applyState(data); return 'Channel rule saved.';
});
bindForm('schedule-editor', async form => {
  const days = [...form.querySelectorAll('[name=days]:checked')].map(field => Number(field.value));
  if (!days.length) throw new Error('Choose at least one weekday.');
  const entry = { days, start: $('#schedule-start').value, end: $('#schedule-end').value };
  const data = await api('settings', 'POST', { settings: { unavailable_windows: [...state.settings.unavailable_windows, entry] } });
  resetEditor(form); applyState(data); return 'Quiet period saved.';
});
$('#toggle-bot-talk').addEventListener('click', async () => {
  const button = $('#toggle-bot-talk'); button.disabled = true;
  try { await api('social/stop', 'POST', { stopped: !state.bot_interactions_stopped }); await refreshExtraState(); }
  catch (error) { notice($('#page-notice'), error.message, true); }
  finally { button.disabled = false; }
});

function reminderZoneNote() {
  $('#reminder-zone-note').textContent = `This time uses ${personalZones[$('#reminder-person').value] || state.settings.timezone}, the selected person's effective timezone.`;
}
async function loadReminders() {
  try {
    const data = await readExtra('reminders'); if (!data) return;
    personalZones = data.timezones; reminderZoneNote();
    const list = $('#reminder-list'); list.replaceChildren();
    for (const reminder of data.reminders) entity(list, reminder.message,
      `${personName(reminder.user_id)} · ${reminder.channel_id === 'dm' ? 'Owner DM' : channelName(reminder.channel_id)} · ${new Date(reminder.due_at * 1000).toLocaleString()} · ${reminder.state}`,
      reminder.state === 'pending' ? [actionButton('Cancel reminder', async () => {
        await api(`reminders/${reminder.id}`, 'DELETE'); await loadReminders(); return 'Reminder cancelled.';
      }, { description: `Cancel reminder: ${reminder.message}` })] : []);
    if (!list.children.length) entity(list, 'No reminders yet', 'Add one here or ask in Discord.');
    const zones = $('#timezone-list'); zones.replaceChildren();
    for (const [author, zone] of Object.entries(data.timezones)) entity(zones, personName(author), zone);
    if (!zones.children.length) entity(zones, 'No personal overrides', `Using ${state.settings.timezone} for this bot.`);
    notice($('#reminder-notice'), '');
  } catch (error) { notice($('#reminder-notice'), error.message, true); }
}
$('#refresh-reminders').addEventListener('click', loadReminders);
$('#reminder-person').addEventListener('change', reminderZoneNote);
bindForm('reminder-editor', async (form, values) => {
  await api('reminders', 'POST', values); resetEditor(form); await loadReminders(); return 'Reminder saved.';
});
bindForm('timezone-editor', async (form, values) => {
  await api('timezones', 'POST', values); resetEditor(form); await loadReminders(); return 'Personal timezone saved.';
});

async function loadLore() {
  try {
    const data = await readExtra('lore'); if (!data) return;
    const list = $('#lore-list'); list.replaceChildren();
    for (const lore of data.lore) entity(list, lore.text,
      `${lore.scope === 'global' ? 'Every bot, everywhere' : lore.scope === 'user' ? personName(lore.target) : lore.scope === 'server' ? 'This server' : lore.target ? 'All conversations with this bot' : 'Every account, every conversation'}${lore.channel_id ? ` · ${channelName(lore.channel_id)}` : ''}`,
      [actionButton('Edit', () => {
        if (edit('lore-editor', lore)) { $('#lore-editor').dataset.identity = lore.id; $('#lore-person').value = lore.target; loreScope(); }
      }, { description: `Edit lore: ${lore.id}` }),
      actionButton('Delete', async () => { await api(`lore/${lore.id}`, 'DELETE'); await loadLore(); return 'Lore deleted.'; },
        { confirm: true, description: `Delete lore: ${lore.id}` })]);
    if (!list.children.length) entity(list, 'No manual lore', 'Add background facts with an explicit audience.');
  } catch (error) { notice($('#memory-notice'), error.message, true); }
}
function loreScope() { $('#lore-person').disabled = $('#lore-scope').value !== 'user'; $('#lore-person').required = !$('#lore-person').disabled; }
$('#lore-scope').addEventListener('change', loreScope);
$('#new-lore').addEventListener('click', () => { if (edit('lore-editor', { scope: 'server' })) loreScope(); });
bindForm('lore-editor', async (form, values) => {
  values.target = values.scope === 'global' ? '' : values.scope === 'user' ? $('#lore-person').value : values.scope === 'server' ? state.bot.server_id : state.bot.id;
  values.id = form.dataset.identity ? Number(form.dataset.identity) : null;
  await api('lore', 'POST', values); resetEditor(form, true); await loadLore(); return 'Lore saved.';
});
bindForm('memory-scope-editor', async (form, values) => {
  const result = await api('memories/clean', 'POST', values); resetEditor(form); await loadMemories();
  return `${result.removed} duplicate facts removed; ${result.retained} retained.`;
});
$('#clear-scope-history').addEventListener('click', async () => {
  const form = $('#memory-scope-editor');
  if (!form.reportValidity()) return;
  try {
    await api('history/clear', 'POST', editorValues(form)); resetEditor(form);
    notice(form.querySelector('.form-status'), 'Recent context cleared. Lasting facts and action receipts are kept.');
  } catch (error) { notice(form.querySelector('.form-status'), error.message, true); }
});

function facts(element, values) {
  element.replaceChildren();
  for (const [label, value] of values) element.append(node('dt', label), node('dd', String(value ?? 'Not available')));
}
async function loadDiagnostics() {
  try {
    const [data, release] = await Promise.all([readExtra('diagnostics'), readExtra('maintenance')]);
    if (!data || !release) return;
    const stats = data.statistics;
    facts($('#diagnostic-stats'), [['Database check', data.database], ['Recorded requests', stats.requests], ['Errors', stats.errors],
      ['Input / output tokens', `${stats.input_tokens} / ${stats.output_tokens}`], ['Average provider time', `${Number(stats.average_seconds).toFixed(2)} seconds`]]);
    const rows = $('#diagnostic-requests'); rows.replaceChildren();
    for (const request of data.requests) {
      const row = node('tr');
      for (const value of [new Date(request.at * 1000).toLocaleString(), `${request.provider} · ${request.model}`, Number(request.seconds).toFixed(2),
        `${request.input_tokens} / ${request.output_tokens}`, request.error || 'Succeeded']) row.append(node('td', String(value)));
      rows.append(row);
    }
    $('#diagnostic-log').textContent = data.logs || 'No saved log. Logging is off unless you enable it above.';
    $('#diagnostic-context').textContent = data.last_context ? JSON.stringify(data.last_context, null, 2) : 'No context has been saved.';
    $('#command-inventory').textContent = `Registered locally: ${data.commands.map(name => `/${name}`).join(', ')}.`;
    facts($('#release-status'), [['Running release', release.current], ['Checked release', release.staged?.id || 'None'],
      ['Previous release', release.active?.previous || 'None'], ['Last update', release.last_update ? JSON.stringify(release.last_update) : 'No update recorded']]);
    $('#restart-programme').disabled = !release.can_restart;
    $('#apply-release').disabled = !release.can_update || !release.staged;
    $('#rollback-release').disabled = !release.can_update || !release.active?.previous;
  } catch (error) { notice($('#maintenance-notice'), error.message, true); }
}
$('#refresh-diagnostics').addEventListener('click', loadDiagnostics);
bindForm('context-editor', async (form, values) => {
  const data = await api('context', 'POST', values);
  $('#context-output').textContent = JSON.stringify(data, null, 2); $('#context-output').hidden = false;
  form.classList.remove('dirty'); return 'Preview built. Nothing was sent to a provider.';
});

function otherDrafts(form) {
  if ([...document.querySelectorAll('form.dirty')].some(other => other !== form)) throw new Error('Save or cancel other edits before replacing this bot’s settings.');
}
bindForm('advanced-editor', async form => {
  otherDrafts(form);
  let settings;
  try { settings = JSON.parse($('#advanced-settings').value); }
  catch { throw new Error('The settings need valid JSON. Your edits are still here.'); }
  const data = await api('settings', 'POST', { settings }); form.classList.remove('dirty'); applyState(data); return 'Advanced settings saved.';
});
$('#reset-advanced').addEventListener('click', () => {
  $('#advanced-settings').value = JSON.stringify(state.settings, null, 2); $('#advanced-editor').classList.remove('dirty');
  notice($('#advanced-editor .form-status'), 'Edits discarded.');
});
$('#import-file').addEventListener('change', () => { validatedImport = null; $('#apply-import').disabled = true; });
bindForm('import-editor', async form => {
  const file = $('#import-file').files[0];
  if (!file || file.size > 2 * 1024 * 1024) throw new Error('Choose a profile JSON file smaller than 2 MiB.');
  let profile;
  try { profile = JSON.parse(await file.text()); } catch { throw new Error('This file does not contain valid JSON.'); }
  const result = await api('import', 'POST', { profile, apply: false });
  validatedImport = { bot: selectedBot, profile };
  $('#import-result').textContent = JSON.stringify(result, null, 2); $('#import-result').hidden = false;
  // Enable after bindForm restores the original control states.
  setTimeout(() => { $('#apply-import').disabled = !validatedImport; }, 0);
  return 'Valid profile. Review the counts, then apply when ready.';
});
$('#apply-import').addEventListener('click', async () => {
  const form = $('#import-editor'); const button = $('#apply-import');
  try {
    otherDrafts(form);
    if (!validatedImport || validatedImport.bot !== selectedBot) throw new Error('Validate this profile for the selected bot first.');
    button.disabled = true;
    const result = await api('import', 'POST', { profile: validatedImport.profile, apply: true });
    validatedImport = null; resetEditor(form); $('#import-result').hidden = true;
    await refreshExtraState(); notice(form.querySelector('.form-status'), `Profile imported. Backup: ${result.backup}.`);
  } catch (error) { notice(form.querySelector('.form-status'), error.message, true); button.disabled = !validatedImport; }
});

for (const [id, path] of [['reload-settings', 'maintenance/reload'], ['sync-commands', 'commands/sync']]) {
  document.getElementById(id).addEventListener('click', async event => {
    if (accountChangeBlocked()) return;
    const button = event.currentTarget; button.disabled = true;
    try {
      const result = await api(path, 'POST', {});
      if (!result.ok) throw new Error(result.error);
      await refreshExtraState(); await loadDiagnostics(); notice($('#maintenance-notice'), result.message || `${result.count} slash commands synced.`);
    } catch (error) { notice($('#maintenance-notice'), error.message, true); }
    finally { button.disabled = false; }
  });
}

async function restartProgramme(action) {
  if (accountChangeBlocked()) return;
  for (const id of ['restart-programme', 'apply-release', 'rollback-release']) document.getElementById(id).disabled = true;
  try {
    const before = await fetch('health', { cache: 'no-store' }).then(response => response.json());
    const result = await api(`maintenance/${action}`, 'POST', {});
    notice($('#maintenance-notice'), `${result.message} Reconnecting…`);
    for (let attempt = 0; attempt < 45; attempt++) {
      await new Promise(resolve => setTimeout(resolve, 2000));
      try {
        const health = await fetch('health', { cache: 'no-store' }).then(response => response.json());
        if (health.launch_id && health.launch_id !== before.launch_id) { location.reload(); return; }
      } catch { /* The old listener closes while the replacement starts. */ }
    }
    throw new Error('The restart has not been confirmed yet. Refresh this page to check the current state.');
  } catch (error) { notice($('#maintenance-notice'), error.message, true); await loadDiagnostics(); }
}
$('#restart-programme').addEventListener('click', () => restartProgramme('restart'));
$('#apply-release').addEventListener('click', () => restartProgramme('apply'));
$('#rollback-release').addEventListener('click', () => restartProgramme('rollback'));
bindForm('release-editor', async form => {
  const file = $('#release-file').files[0];
  if (!file || file.size > 25 * 1024 * 1024) throw new Error('Choose a Hearthling release ZIP smaller than 25 MiB.');
  notice(form.querySelector('.form-status'), 'Validating files and running the release checks…');
  const result = await api('release', 'POST', file);
  resetEditor(form); await loadDiagnostics(); return `Release ${result.id} checked. ${result.checks_passed.length} runnable checks passed; nothing has been restarted.`;
});

function extraState(changedBot = false) {
  if (!state) return;
  if (changedBot) {
    for (const form of extraForms) resetEditor(form, ['character-editor', 'provider-editor', 'image-profile-editor', 'lore-editor'].includes(form.id));
    for (const id of ['character-preview', 'context-output', 'import-result']) document.getElementById(id).hidden = true;
    for (const id of ['lore-list', 'reminder-list', 'timezone-list', 'diagnostic-requests']) document.getElementById(id).replaceChildren();
    validatedImport = null; personalZones = {}; $('#apply-import').disabled = true;
  }
  populateChoices(); renderCharacters(); renderProviders(); renderImages(); renderAutomation();
  if (!$('#advanced-editor').classList.contains('dirty')) $('#advanced-settings').value = JSON.stringify(state.settings, null, 2);
  $('#export-profile').href = `api/export?bot=${encodeURIComponent(selectedBot)}`;
  $('#export-release').href = `api/release?bot=${encodeURIComponent(selectedBot)}`;
  reminderZoneNote();
  if (changedBot) loadExtraTab(tabs.find(tab => tab.getAttribute('aria-selected') === 'true')?.dataset.tab);
}
function loadExtraTab(tab) {
  if (!state) return;
  if (tab === 'reminders') loadReminders();
  if (tab === 'memories') loadLore();
  if (tab === 'maintenance') loadDiagnostics();
}
document.addEventListener('hearthling:state', event => extraState(event.detail.changedBot));
document.addEventListener('hearthling:tab', event => loadExtraTab(event.detail));
if (state) extraState();
