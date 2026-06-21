// configform.js — לוגיקה משותפת לטופס ההגדרה (משמש את אשף ההתקנה ואת מסך ההגדרה).

function tagRow(tag, floor) {
  const div = document.createElement('div');
  div.className = 'tag-row';
  div.innerHTML = `<code class="tag-id">${tag}</code>
    <span class="muted">→ קומה</span>
    <input type="text" class="tag-floor" value="${floor || ''}">
    <button type="button" class="danger" onclick="this.parentElement.remove()">✕</button>`;
  return div;
}

function addTagRow(tag, floor) {
  document.getElementById('tags').appendChild(tagRow(tag || '', floor || ''));
}

function renderTags(tags) {
  const c = document.getElementById('tags');
  c.innerHTML = '';
  Object.entries(tags || {}).forEach(([t, f]) => c.appendChild(tagRow(t, f)));
}

function collectTags() {
  const out = {};
  document.querySelectorAll('#tags .tag-row').forEach(row => {
    const id = row.querySelector('.tag-id').textContent.trim();
    const fl = row.querySelector('.tag-floor').value.trim();
    if (id && fl) out[id] = fl;
  });
  return out;
}

async function scanTag(btn) {
  btn.disabled = true; const old = btn.textContent;
  btn.textContent = 'סורק… קרב תג לקורא';
  try {
    const r = await api('/api/scan-tag', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
    if (r.tag) { addTagRow(r.tag, ''); toast('תג נקרא: ' + r.tag); }
    else toast('לא נקרא תג. נסה שוב.');
  } catch (e) { toast('שגיאת סריקה: ' + e); }
  btn.disabled = false; btn.textContent = old;
}

function readForm() {
  const v = id => (document.getElementById(id) || {}).value || '';
  const ck = id => (document.getElementById(id) || {}).checked || false;
  return {
    settings: {
      FIREBASE_URL: v('firebase_url'),
      ELEVATOR_ID: v('elevator_id'),
      SECRET_KEY: v('secret_key'),
      SERIAL_PORT: v('serial_port') || '/dev/ttyUSB0',
      BAUDRATE: parseInt(v('baudrate') || '115200'),
    },
    tags: collectTags(),
    notifications: {
      enabled: ck('notif_enabled'),
      events: { shabbat_enter: ck('ev_enter'), shabbat_exit: ck('ev_exit'), no_movement: ck('ev_nomove') },
      no_movement: {
        threshold_hours: parseFloat(v('nm_threshold') || '10'),
        night_start: v('nm_night_start') || '23:00',
        night_end: v('nm_night_end') || '06:00',
      },
      channels: {
        telegram: { enabled: ck('tg_enabled'), bot_token: v('tg_token'), chat_id: v('tg_chat') },
        email: {
          enabled: ck('em_enabled'), smtp_host: v('em_host'), smtp_port: parseInt(v('em_port') || '587'),
          username: v('em_user'), password: v('em_pass'), from: v('em_from'),
          to: v('em_to').split(',').map(s => s.trim()).filter(Boolean),
        },
        whatsapp: { enabled: false },
      },
    },
  };
}

function populateForm(cfg) {
  const set = (id, val) => { const e = document.getElementById(id); if (e) e.value = val ?? ''; };
  const chk = (id, val) => { const e = document.getElementById(id); if (e) e.checked = !!val; };
  const s = cfg.settings || {}, n = cfg.notifications || {};
  set('firebase_url', s.FIREBASE_URL); set('elevator_id', s.ELEVATOR_ID);
  set('secret_key', s.SECRET_KEY); set('serial_port', s.SERIAL_PORT || '/dev/ttyUSB0');
  set('baudrate', s.BAUDRATE || 115200);
  renderTags(cfg.tags || {});
  chk('notif_enabled', n.enabled);
  const ev = n.events || {}; chk('ev_enter', ev.shabbat_enter !== false);
  chk('ev_exit', ev.shabbat_exit !== false); chk('ev_nomove', ev.no_movement !== false);
  const nm = n.no_movement || {}; set('nm_threshold', nm.threshold_hours ?? 10);
  set('nm_night_start', nm.night_start || '23:00'); set('nm_night_end', nm.night_end || '06:00');
  const ch = n.channels || {}, tg = ch.telegram || {}, em = ch.email || {};
  chk('tg_enabled', tg.enabled); set('tg_token', tg.bot_token); set('tg_chat', tg.chat_id);
  chk('em_enabled', em.enabled); set('em_host', em.smtp_host || 'smtp.gmail.com');
  set('em_port', em.smtp_port || 587); set('em_user', em.username); set('em_pass', em.password);
  set('em_from', em.from); set('em_to', (em.to || []).join(', '));
}

async function loadConfigForm() {
  try { populateForm(await api('/api/config')); } catch (e) { /* טופס ריק */ }
}
