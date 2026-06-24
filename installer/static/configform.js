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
  return {
    settings: {
      FIREBASE_URL: v('firebase_url'),
      ELEVATOR_ID: v('elevator_id'),
      SECRET_KEY: v('secret_key'),
      SERIAL_PORT: v('serial_port') || '/dev/ttyUSB0',
      BAUDRATE: parseInt(v('baudrate') || '115200'),
    },
    tags: collectTags(),
  };
}

function populateForm(cfg) {
  const set = (id, val) => { const e = document.getElementById(id); if (e) e.value = val ?? ''; };
  const s = cfg.settings || {};
  set('firebase_url', s.FIREBASE_URL); set('elevator_id', s.ELEVATOR_ID);
  set('secret_key', s.SECRET_KEY); set('serial_port', s.SERIAL_PORT || '/dev/ttyUSB0');
  set('baudrate', s.BAUDRATE || 115200);
  renderTags(cfg.tags || {});
}

async function loadConfigForm() {
  try { populateForm(await api('/api/config')); } catch (e) { /* טופס ריק */ }
}
