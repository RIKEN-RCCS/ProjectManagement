/* ================================================================
   PM DB Editor — Client-side logic
   ================================================================ */

let aiGrid = null;
let decGrid = null;
let achGrid = null;
let filesGrid = null;
let _filesLoaded = false;
let milestones = {};
let bulkState = { ai: { done: false, deleted: false } };

// ----------------------------------------------------------------
// API helper
// ----------------------------------------------------------------
async function api(method, path, body) {
  const opts = { method, headers: { 'Content-Type': 'application/json' } };
  if (body !== undefined) opts.body = JSON.stringify(body);
  const res = await fetch('/api' + path, opts);
  const data = await res.json();
  if (!res.ok) {
    toast(data.error || `API error ${res.status}`, 'negative');
    throw new Error(data.error || res.statusText);
  }
  return data;
}

// ----------------------------------------------------------------
// Toast notifications
// ----------------------------------------------------------------
function toast(msg, type, duration) {
  const el = document.createElement('div');
  el.className = `toast toast-${type || 'info'}`;
  el.textContent = msg;
  el.onclick = () => el.remove();
  document.getElementById('toast-container').appendChild(el);
  const ms = duration !== undefined ? duration : (type === 'warning' ? 0 : 3000);
  if (ms > 0) setTimeout(() => el.remove(), ms);
}

// ----------------------------------------------------------------
// Tabs (for DB Editor — linked to URL hash navigation)
// ----------------------------------------------------------------
// Editor tab clicks change the hash, handled by admin.js
document.querySelectorAll('.editor-nav-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    location.hash = btn.dataset.tab;
  });
});

// ----------------------------------------------------------------
// Dialog helper
// ----------------------------------------------------------------
function openDialog(id) { document.getElementById(id).showModal(); }

// ----------------------------------------------------------------
// Databases
// ----------------------------------------------------------------
async function loadDatabases() {
  const data = await api('GET', '/databases');
  const sel = document.getElementById('sel-db');
  sel.innerHTML = '';
  data.databases.forEach(db => {
    const opt = document.createElement('option');
    opt.value = db.path;
    opt.textContent = db.name;
    if (db.path === data.current) opt.selected = true;
    sel.appendChild(opt);
  });
}

document.getElementById('sel-db').addEventListener('change', async function() {
  try {
    const res = await api('POST', '/databases/switch', { path: this.value });
    toast(`DB を切り替えました: ${res.name}`, 'positive');
    await loadMilestones();
    await loadActionItems();
    await loadDecisions();
  } catch (e) { /* toast already shown */ }
});

// ----------------------------------------------------------------
// Milestones
// ----------------------------------------------------------------
async function loadMilestones() {
  const data = await api('GET', '/milestones');
  milestones = data.milestones || {};

  // Update filter dropdown
  const sel = document.getElementById('f-ai-ms');
  const cur = sel.value;
  sel.innerHTML = '<option value="すべて">すべて</option>';
  Object.keys(milestones).forEach(k => {
    const opt = document.createElement('option');
    opt.value = k; opt.textContent = k;
    sel.appendChild(opt);
  });
  sel.value = cur;

  // Update grid editor + new-AI dialog
  const vals = ['', ...Object.keys(milestones)];
  if (aiGrid) {
    const col = aiGrid.getColumn('milestone_id');
    if (col) col.getColDef().cellEditorParams = { values: vals };
  }
  const msSel = document.querySelector('#form-ai-new select[name="milestone_id"]');
  if (msSel) {
    msSel.innerHTML = '<option value="">マイルストーン</option>';
    Object.keys(milestones).forEach(k => {
      const opt = document.createElement('option');
      opt.value = k; opt.textContent = milestones[k];
      msSel.appendChild(opt);
    });
  }
}

// ----------------------------------------------------------------
// AG Grid: source column renderer
// ----------------------------------------------------------------
function sourceRenderer(params) {
  const src = params.value || '';
  const ref = (params.data || {}).source_ref || '';
  const mid = (params.data || {}).meeting_id || '';
  const s = 'cursor:pointer;color:#1565c0;text-decoration:underline';
  if (src === 'slack' && ref) return `<span style="${s}">Slack</span>`;
  if (src === 'meeting') return `<span style="${s}">minutes</span>`;
  return src;
}

function sourceRendererDec(params) {
  const src = params.value || '';
  const ref = (params.data || {}).source_ref || '';
  const mid = (params.data || {}).meeting_id || '';
  const s = 'cursor:pointer;color:#1565c0;text-decoration:underline';
  if (src === 'slack' && ref) return `<span style="${s}">Slack</span>`;
  if (src === 'meeting') return `<span style="${s}">minutes</span>`;
  return src;
}

function openMinutes(meetingId, kind) {
  const params = new URLSearchParams({ id: meetingId, kind: kind || '' });
  window.open(`/minutes.html?${params}`, '_blank',
    'width=960,height=780,scrollbars=yes,resizable=yes');
}

function escapeHtml(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function renderEnrichSection(label, value) {
  if (!value) return '';
  const text = escapeHtml(value).replace(/\n/g, '<br>');
  return `<div>
    <div class="text-xs font-bold text-gray-400 mb-1">${label}</div>
    <div class="bg-gray-700 border rounded p-2 whitespace-pre-wrap text-gray-200">${text}</div>
  </div>`;
}

function renderRelatedIds(related) {
  if (!related) return '';
  let ids = [];
  try {
    const parsed = typeof related === 'string' ? JSON.parse(related) : related;
    if (Array.isArray(parsed)) ids = parsed;
  } catch (e) {
    // fall through - raw string fallback
  }
  if (ids.length === 0 && typeof related === 'string') {
    ids = [related];
  }
  const items = ids.map(id => `<code class="bg-blue-900 text-blue-200 px-1 rounded text-xs">${escapeHtml(id)}</code>`).join(' ');
  return `<div>
    <div class="text-xs font-bold text-gray-400 mb-1">関連ID</div>
    <div>${items}</div>
  </div>`;
}

function openEnrichDialog(kind, data) {
  const dlg = document.getElementById('dialog-enrich');
  const title = document.getElementById('enrich-title');
  const body = document.getElementById('enrich-body');
  const actor = kind === 'AI' ? (data.requested_by || '') : (data.decided_by || '');
  const actorLabel = kind === 'AI' ? '依頼者' : '判断者';
  const conf = kind === 'AI' ? (data.requested_by_confidence || '') : (data.decided_by_confidence || '');
  title.textContent = `${kind} #${data.id || '-'}`;
  const contentSection = `<div>
    <div class="text-xs font-bold text-gray-400 mb-1">内容</div>
    <div class="bg-gray-700 border rounded p-2 whitespace-pre-wrap text-gray-200">${escapeHtml(data.content || '')}</div>
  </div>`;
  const actorSection = actor ? `<div>
    <div class="text-xs font-bold text-gray-400 mb-1">${actorLabel}${conf ? ` <span class="font-normal text-gray-500">(${escapeHtml(conf)})</span>` : ''}</div>
    <div>${escapeHtml(actor)}</div>
  </div>` : '';
  const parts = [
    contentSection,
    actorSection,
    renderEnrichSection('根拠 (rationale)', data.rationale),
    renderEnrichSection('背景 (source_context)', data.source_context),
    renderRelatedIds(data.related_ids),
  ].filter(Boolean);
  if (parts.length <= 1) {
    parts.push('<div class="text-gray-400 italic">エンリッチメント情報はまだありません。scripts/enrich/enrich_items.py を実行してください。</div>');
  }
  body.innerHTML = parts.join('');
  dlg.showModal();
}

// ----------------------------------------------------------------
// Action Items
// ----------------------------------------------------------------
// 根拠/背景セル: 要約を表示、クリックで詳細モーダル
function enrichRenderer(params) {
  const data = params.data || {};
  const rat = data.rationale || '';
  const ctx = data.source_context || '';
  const rel = data.related_ids || '';
  const has = rat || ctx || rel;
  if (!has) return '<span style="color:#9ca3af;font-size:11px">—</span>';
  const preview = (rat || ctx).replace(/[\r\n]+/g, ' ').slice(0, 60);
  const marks = [];
  if (rat) marks.push('根拠');
  if (ctx) marks.push('背景');
  if (rel) marks.push('関連');
  const tag = marks.join('/');
  const s = 'cursor:pointer;color:#1565c0;text-decoration:underline';
  return `<span style="${s}" title="クリックで詳細表示">[${tag}] ${preview}${preview.length >= 60 ? '…' : ''}</span>`;
}

const aiColumnDefs = [
  { field: 'deleted', headerName: '削除', width: 50, pinned: 'left',
    cellRenderer: 'agCheckboxCellRenderer',
    cellEditor: 'agCheckboxCellEditor',
    cellRendererParams: { disabled: false } },
  { field: 'id', headerName: 'ID', editable: false, width: 50, pinned: 'left' },
  { field: 'content', headerName: '内容', width: 360 },
  { field: 'assignee', headerName: '担当者', width: 110 },
  { field: 'requested_by', headerName: '依頼者', width: 110 },
  { field: 'due_date', headerName: '期限', width: 105 },
  { field: 'milestone_id', headerName: 'MS', width: 60,
    cellEditor: 'agSelectCellEditor',
    cellEditorParams: { values: [''] } },
  { field: 'done', headerName: '完了', width: 70,
    cellRenderer: 'agCheckboxCellRenderer',
    cellEditor: 'agCheckboxCellEditor',
    cellRendererParams: { disabled: false } },
  { field: 'note', headerName: '対応状況', width: 260 },
  { headerName: '根拠/背景', width: 280, editable: false,
    colId: 'enrich',
    valueGetter: (p) => (p.data && p.data.rationale) || '',
    cellRenderer: enrichRenderer },
  { field: 'extracted_at', headerName: '発生日', editable: false, width: 110 },
  { field: 'source', headerName: '出典', editable: false, width: 110,
    cellRenderer: sourceRenderer },
  { field: 'source_ref', hide: true },
  { field: 'meeting_id', hide: true },
  { field: 'meeting_kind', hide: true },
  { field: 'rationale', hide: true },
  { field: 'source_context', hide: true },
  { field: 'related_ids', hide: true },
  { field: 'requested_by_confidence', hide: true },
];

function initAiGrid() {
  const el = document.getElementById('grid-ai');
  const msCol = aiColumnDefs.find(c => c.field === 'milestone_id');
  if (msCol) msCol.cellEditorParams = { values: ['', ...Object.keys(milestones)] };
  aiGrid = agGrid.createGrid(el, {
    columnDefs: aiColumnDefs,
    defaultColDef: {
      editable: true, resizable: true, sortable: true, filter: true,
      wrapText: true, autoHeight: true,
    },
    domLayout: 'autoHeight',
    rowData: [],
    stopEditingWhenCellsLoseFocus: true,
    singleClickEdit: true,
    onCellClicked: (event) => {
      if (event.colDef.colId === 'enrich') {
        openEnrichDialog('AI', event.data || {});
        return;
      }
      if (event.colDef.field !== 'source') return;
      const data = event.data || {};
      if (data.source === 'slack' && data.source_ref) {
        window.open(data.source_ref, '_blank');
      } else if (data.source === 'meeting' && data.meeting_id) {
        openMinutes(data.meeting_id, data.meeting_kind || '');
      }
    },
  });
}

async function loadActionItems() {
  const status = document.getElementById('f-ai-status').value;
  const ms = document.getElementById('f-ai-ms').value;
  const del = document.getElementById('f-ai-del').value;
  const since = document.getElementById('f-ai-since').value;
  const qs = new URLSearchParams({ status, milestone: ms, deleted: del, since });
  const sf = sourceFilter.ai;
  (sf.channels || []).forEach(c => qs.append('channels', c));
  (sf.meeting_kinds || []).forEach(k => qs.append('meeting_kinds', k));
  const data = await api('GET', '/action-items?' + qs);
  aiGrid.setGridOption('rowData', data.rows);
  bulkState.ai = { done: false, deleted: false };
  document.getElementById('btn-ai-done').classList.remove('bg-blue-500', 'text-white');
  document.getElementById('btn-ai-del').classList.remove('bg-red-500', 'text-white');
}

async function saveActionItems() {
  aiGrid.stopEditing();
  const rows = [];
  aiGrid.forEachNode(node => rows.push(node.data));
  const res = await api('POST', '/action-items/save', { rows });
  if (res.updated > 0) toast(`${res.updated} フィールドを更新しました`, 'positive');
  if (res.conflicts && res.conflicts.length > 0) {
    const lines = res.conflicts.map(c =>
      `ID:${c.id} [${c.field}] あなた: ${JSON.stringify(c.yours)} / DB現在値: ${JSON.stringify(c.db)}`
    ).join('\n');
    toast('競合のため保存できなかった変更があります:\n' + lines, 'warning');
  }
  if (res.updated === 0 && (!res.conflicts || res.conflicts.length === 0)) {
    toast('変更はありませんでした', 'info');
  }
  await loadActionItems();
}

function bulkToggle(panel, field) {
  if (panel !== 'ai') return;
  bulkState.ai[field] = !bulkState.ai[field];
  const val = bulkState.ai[field];
  const rows = [];
  aiGrid.forEachNode(node => { node.data[field] = val; rows.push(node.data); });
  aiGrid.setGridOption('rowData', rows);

  if (field === 'done') {
    const btn = document.getElementById('btn-ai-done');
    btn.classList.toggle('bg-blue-500', val);
    btn.classList.toggle('text-white', val);
  } else {
    const btn = document.getElementById('btn-ai-del');
    btn.classList.toggle('bg-red-500', val);
    btn.classList.toggle('text-white', val);
  }
  toast(val ? `全件を${field === 'done' ? '完了' : '削除'}にしました（保存で確定）` :
             `全件の${field === 'done' ? '完了' : '削除'}を解除しました（保存で確定）`, 'info');
}

// New action item
document.getElementById('form-ai-new').addEventListener('submit', async (e) => {
  e.preventDefault();
  const fd = new FormData(e.target);
  const body = {};
  for (const [k, v] of fd.entries()) body[k] = v || null;
  if (!body.content || !body.content.trim()) { toast('内容は必須です', 'negative'); return; }
  await api('POST', '/action-items/new', body);
  toast('追加しました', 'positive');
  e.target.reset();
  document.getElementById('dialog-ai-new').close();
  await loadActionItems();
});

// ----------------------------------------------------------------
// Decisions
// ----------------------------------------------------------------
const decColumnDefs = [
  { field: 'deleted', headerName: '削除', width: 50, pinned: 'left',
    cellRenderer: 'agCheckboxCellRenderer',
    cellEditor: 'agCheckboxCellEditor',
    cellRendererParams: { disabled: false } },
  { field: 'id', headerName: 'ID', editable: false, width: 50, pinned: 'left' },
  { field: 'content', headerName: '内容', width: 440 },
  { field: 'decided_by', headerName: '判断者', width: 110 },
  { field: 'decided_at', headerName: '決定日', width: 110 },
  { headerName: '根拠/背景', width: 280, editable: false,
    colId: 'enrich',
    valueGetter: (p) => (p.data && p.data.rationale) || '',
    cellRenderer: enrichRenderer },
  { field: 'extracted_at', headerName: '発生日', editable: false, width: 110 },
  { field: 'source', headerName: '出典', editable: false, width: 110,
    cellRenderer: sourceRendererDec },
  { field: 'source_ref', hide: true },
  { field: 'rationale', hide: true },
  { field: 'source_context', hide: true },
  { field: 'related_ids', hide: true },
  { field: 'decided_by_confidence', hide: true },
];

function initDecGrid() {
  const el = document.getElementById('grid-dec');
  decGrid = agGrid.createGrid(el, {
    columnDefs: decColumnDefs,
    defaultColDef: {
      editable: true, resizable: true, sortable: true, filter: true,
      wrapText: true, autoHeight: true,
    },
    domLayout: 'autoHeight',
    rowData: [],
    stopEditingWhenCellsLoseFocus: true,
    singleClickEdit: true,
    onCellClicked: (event) => {
      if (event.colDef.colId === 'enrich') {
        openEnrichDialog('Decision', event.data || {});
        return;
      }
      if (event.colDef.field !== 'source') return;
      const data = event.data || {};
      if (data.source === 'slack' && data.source_ref) {
        window.open(data.source_ref, '_blank');
      } else if (data.source === 'meeting' && data.meeting_id) {
        openMinutes(data.meeting_id, data.meeting_kind || '');
      }
    },
  });
}

async function loadDecisions() {
  const del = document.getElementById('f-dec-del').value;
  const since = document.getElementById('f-dec-since').value;
  const qs = new URLSearchParams({ acknowledged: 'すべて', deleted: del, since });
  const sf = sourceFilter.dec;
  (sf.channels || []).forEach(c => qs.append('channels', c));
  (sf.meeting_kinds || []).forEach(k => qs.append('meeting_kinds', k));
  const data = await api('GET', '/decisions?' + qs);
  decGrid.setGridOption('rowData', data.rows);
}

async function saveDecisions() {
  decGrid.stopEditing();
  const rows = [];
  decGrid.forEachNode(node => rows.push(node.data));
  const res = await api('POST', '/decisions/save', { rows });
  if (res.updated > 0) toast(`${res.updated} フィールドを更新しました`, 'positive');
  if (res.conflicts && res.conflicts.length > 0) {
    const lines = res.conflicts.map(c =>
      `ID:${c.id} [${c.field}] あなた: ${JSON.stringify(c.yours)} / DB現在値: ${JSON.stringify(c.db)}`
    ).join('\n');
    toast('競合のため保存できなかった変更があります:\n' + lines, 'warning');
  }
  if (res.updated === 0 && (!res.conflicts || res.conflicts.length === 0)) {
    toast('変更はありませんでした', 'info');
  }
  await loadDecisions();
}

async function ackAll() {
  const res = await api('POST', '/decisions/ack-all');
  toast(`${res.count} 件を確認済みにしました`, 'positive');
  await loadDecisions();
}

// New decision
document.getElementById('form-dec-new').addEventListener('submit', async (e) => {
  e.preventDefault();
  const fd = new FormData(e.target);
  const body = {};
  for (const [k, v] of fd.entries()) body[k] = v || null;
  if (!body.content || !body.content.trim()) { toast('内容は必須です', 'negative'); return; }
  await api('POST', '/decisions/new', body);
  toast('追加しました', 'positive');
  e.target.reset();
  document.getElementById('dialog-dec-new').close();
  await loadDecisions();
});

// ----------------------------------------------------------------
// Achievements
// ----------------------------------------------------------------
const achColumnDefs = [
  { field: 'id', headerName: 'ID', editable: false, width: 50, pinned: 'left' },
  { field: 'app', headerName: 'アプリ', editable: false, width: 130 },
  { field: 'title', headerName: '実績', width: 380 },
  { field: 'category', headerName: 'カテゴリ', width: 120 },
  { field: 'achieved_on', headerName: '達成日', width: 110 },
  { field: 'confidence', headerName: '確信度', editable: false, width: 90 },
  { field: 'status', headerName: 'ステータス', width: 110,
    cellEditor: 'agSelectCellEditor',
    cellEditorParams: { values: ['proposed', 'confirmed', 'rejected'] } },
  { field: 'evidence_ref', headerName: '根拠リンク', width: 200 },
  { field: 'evidence_quote', headerName: '根拠引用', width: 280 },
  { field: 'source', hide: true },
  { field: 'deleted', hide: true },
];

function initAchGrid() {
  const el = document.getElementById('grid-ach');
  achGrid = agGrid.createGrid(el, {
    columnDefs: achColumnDefs,
    defaultColDef: {
      editable: true, resizable: true, sortable: true, filter: true,
      wrapText: true, autoHeight: true,
    },
    domLayout: 'autoHeight',
    rowData: [],
    stopEditingWhenCellsLoseFocus: true,
    singleClickEdit: true,
  });
}

async function loadAchievements() {
  const status = document.getElementById('f-ach-status').value;
  const app = document.getElementById('f-ach-app').value;
  const deleted = document.getElementById('f-ach-deleted').checked;
  const qs = new URLSearchParams({ status, app, deleted });
  const data = await api('GET', '/achievements?' + qs);
  achGrid.setGridOption('rowData', data.rows);
}

async function saveAchievements() {
  achGrid.stopEditing();
  const rows = [];
  achGrid.forEachNode(node => rows.push(node.data));
  const res = await api('POST', '/achievements/save', { rows });
  if (res.updated > 0) toast(`${res.updated} フィールドを更新しました`, 'positive');
  if (res.conflicts && res.conflicts.length > 0) {
    const lines = res.conflicts.map(c =>
      `ID:${c.id} [${c.field}] あなた: ${JSON.stringify(c.yours)} / DB現在値: ${JSON.stringify(c.db)}`
    ).join('\n');
    toast('競合のため保存できなかった変更があります:\n' + lines, 'warning');
  }
  if (res.updated === 0 && (!res.conflicts || res.conflicts.length === 0)) {
    toast('変更はありませんでした', 'info');
  }
  await loadAchievements();
}

// New achievement
document.getElementById('form-ach-new').addEventListener('submit', async (e) => {
  e.preventDefault();
  const fd = new FormData(e.target);
  const body = {};
  for (const [k, v] of fd.entries()) body[k] = v || null;
  if (!body.app || !body.app.trim()) { toast('アプリ名は必須です', 'negative'); return; }
  if (!body.title || !body.title.trim()) { toast('実績タイトルは必須です', 'negative'); return; }
  await api('POST', '/achievements/new', body);
  toast('追加しました', 'positive');
  e.target.reset();
  document.getElementById('dialog-ach-new').close();
  await loadAchievements();
});

// ----------------------------------------------------------------
// Files (AG Grid)
// ----------------------------------------------------------------
// チャンネル名は /api/filter-presets が返す channel_names を使う
// （argus_config.yaml の channel_names: が一次定義）。
function initFilesChannelFilter() {
  const sel = document.getElementById('f-files-ch');
  sel.innerHTML = '<option value="">すべて</option>';
  Object.entries(filterPresets.channel_names || {}).forEach(([id, name]) => {
    const opt = document.createElement('option');
    opt.value = id;
    opt.textContent = name;
    sel.appendChild(opt);
  });
}

const filesColumnDefs = [
  { field: 'date',         headerName: '日付',     width: 110, editable: false },
  { field: 'label',        headerName: 'ファイル名', width: 280, editable: false,
    cellRenderer: (params) => {
      const url = (params.data || {}).url || '';
      const label = params.value || '';
      const s = 'cursor:pointer;color:#1565c0;text-decoration:underline';
      const sf = 'cursor:pointer;color:#9ca3af;font-style:italic';
      if (label) return `<a href="${url}" target="_blank" rel="noopener noreferrer" style="${s}">${label}</a>`;
      return `<a href="${url}" target="_blank" rel="noopener noreferrer" style="${sf}">(リンク)</a>`;
    }},
  { field: 'context',      headerName: '投稿内容',  width: 320, editable: false },
  { field: 'channel_name', headerName: 'チャンネル', width: 200, editable: false },
  { field: 'permalink',    headerName: '投稿',      width: 70,  editable: false,
    cellRenderer: (params) => {
      const permalink = params.value || '';
      if (!permalink) return '';
      return `<a href="${permalink}" target="_blank" rel="noopener noreferrer" style="cursor:pointer;color:#1565c0;text-decoration:underline">Slack</a>`;
    }},
  { field: 'url', hide: true },
];

function initFilesGrid() {
  const el = document.getElementById('grid-files');
  filesGrid = agGrid.createGrid(el, {
    columnDefs: filesColumnDefs,
    defaultColDef: {
      editable: false,
      resizable: true,
      sortable: true,
      filter: true,
      wrapText: true,
      autoHeight: true,
    },
    rowData: [],
  });
}

async function loadFiles() {
  const ch = document.getElementById('f-files-ch').value;
  const since = document.getElementById('f-files-since').value;
  const qs = new URLSearchParams({ channel: ch, since });
  const data = await api('GET', '/files?' + qs);
  const rows = data.files || [];
  filesGrid.setGridOption('rowData', rows);
  document.getElementById('files-count').textContent = `${rows.length} 件`;
  // パネルが表示されている場合のみ列幅を調整
  if (!document.getElementById('panel-files').classList.contains('hidden')) {
    setTimeout(() => filesGrid.sizeColumnsToFit(), 0);
  }
}

// ----------------------------------------------------------------
// Source filter (channel / meeting_kind multi-select)
// ----------------------------------------------------------------
let filterPresets = { channels: [], meeting_kinds: [], channel_names: {} };
let sourceFilter = {
  ai:  { channels: [], meeting_kinds: [] },
  dec: { channels: [], meeting_kinds: [] },
};
let _sourceFilterTarget = 'ai';

function _saveSourceFilter() {
  try { localStorage.setItem('pm_source_filter', JSON.stringify(sourceFilter)); } catch (_) {}
}
function _loadSourceFilter() {
  try {
    const raw = localStorage.getItem('pm_source_filter');
    if (raw) {
      const obj = JSON.parse(raw);
      if (obj && typeof obj === 'object') {
        for (const k of ['ai', 'dec']) {
          if (obj[k]) sourceFilter[k] = {
            channels: Array.isArray(obj[k].channels) ? obj[k].channels : [],
            meeting_kinds: Array.isArray(obj[k].meeting_kinds) ? obj[k].meeting_kinds : [],
          };
        }
      }
    }
  } catch (_) {}
}

async function loadFilterPresets() {
  try {
    const data = await api('GET', '/filter-presets');
    filterPresets.channels = data.channels || [];
    filterPresets.meeting_kinds = data.meeting_kinds || [];
    filterPresets.channel_names = data.channel_names || {};
  } catch (_) {}
}

function _channelLabel(id) {
  return filterPresets.channel_names[id] ? `${filterPresets.channel_names[id]} (${id})` : id;
}

function _updateSourceFilterButtonLabel(target) {
  const sf = sourceFilter[target];
  const n = (sf.channels?.length || 0) + (sf.meeting_kinds?.length || 0);
  const btn = document.getElementById(`btn-${target}-source`);
  if (!btn) return;
  if (n === 0) {
    btn.textContent = 'すべて';
    btn.classList.remove('bg-blue-900', 'border-blue-400', 'text-blue-200');
  } else {
    const ch = sf.channels?.length || 0;
    const mk = sf.meeting_kinds?.length || 0;
    btn.textContent = `絞り込み中 (ch:${ch}, 会議:${mk})`;
    btn.classList.add('bg-blue-900', 'border-blue-400', 'text-blue-200');
  }
}

function openSourceFilter(target) {
  _sourceFilterTarget = target;
  document.getElementById('source-filter-target').textContent =
    target === 'ai' ? '— アクションアイテム' : '— 決定事項';

  // Presets
  const presetEl = document.getElementById('source-presets');
  presetEl.innerHTML = '';
  const allPresets = [
    ...filterPresets.channels.map(p => ({ ...p, kind: 'channels' })),
    ...filterPresets.meeting_kinds.map(p => ({ ...p, kind: 'meeting_kinds' })),
  ];
  if (allPresets.length === 0) {
    presetEl.innerHTML = '<span class="text-xs text-gray-400">プリセットなし（argus_config.yaml の filter_presets を確認）</span>';
  }
  allPresets.forEach(p => {
    const btn = document.createElement('button');
    btn.type = 'button';
    const label = p.kind === 'meeting_kinds' ? `会議: ${p.name}` : p.name;
    btn.textContent = label;
    btn.className = 'border rounded-full px-3 py-0.5 text-xs bg-gray-700 text-gray-200 hover:bg-blue-900 hover:text-blue-200 hover:border-blue-400';
    btn.onclick = () => {
      const sf = sourceFilter[_sourceFilterTarget];
      const arr = sf[p.kind];
      // Toggle: 全て含まれていれば外す、そうでなければ追加
      const allIncluded = p.values.every(v => arr.includes(v));
      if (allIncluded) {
        sf[p.kind] = arr.filter(v => !p.values.includes(v));
      } else {
        const merged = new Set([...arr, ...p.values]);
        sf[p.kind] = [...merged];
      }
      renderSourceFilterDialog();
    };
    presetEl.appendChild(btn);
  });

  // Channel checklist (sourced from presets values + channel_names keys)
  const chSet = new Set();
  filterPresets.channels.forEach(p => p.values.forEach(v => chSet.add(v)));
  Object.keys(filterPresets.channel_names).forEach(v => chSet.add(v));
  const chList = [...chSet].sort();
  const chEl = document.getElementById('source-channels');
  chEl.innerHTML = '';
  chList.forEach(id => {
    const lbl = document.createElement('label');
    lbl.className = 'flex items-center gap-2 py-0.5 cursor-pointer hover:bg-gray-700 px-1 rounded';
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.value = id;
    cb.dataset.kind = 'channels';
    cb.checked = sourceFilter[_sourceFilterTarget].channels.includes(id);
    cb.onchange = () => {
      const sf = sourceFilter[_sourceFilterTarget];
      if (cb.checked) {
        if (!sf.channels.includes(id)) sf.channels.push(id);
      } else {
        sf.channels = sf.channels.filter(x => x !== id);
      }
      renderSourceFilterChips();
    };
    lbl.appendChild(cb);
    const span = document.createElement('span');
    span.textContent = _channelLabel(id);
    span.className = 'truncate';
    lbl.appendChild(span);
    chEl.appendChild(lbl);
  });

  // Meeting kinds checklist (from presets values)
  const mkSet = new Set();
  filterPresets.meeting_kinds.forEach(p => p.values.forEach(v => mkSet.add(v)));
  const mkList = [...mkSet].sort();
  const mkEl = document.getElementById('source-meetings');
  mkEl.innerHTML = '';
  mkList.forEach(name => {
    const lbl = document.createElement('label');
    lbl.className = 'flex items-center gap-2 py-0.5 cursor-pointer hover:bg-gray-700 px-1 rounded';
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.value = name;
    cb.dataset.kind = 'meeting_kinds';
    cb.checked = sourceFilter[_sourceFilterTarget].meeting_kinds.includes(name);
    cb.onchange = () => {
      const sf = sourceFilter[_sourceFilterTarget];
      if (cb.checked) {
        if (!sf.meeting_kinds.includes(name)) sf.meeting_kinds.push(name);
      } else {
        sf.meeting_kinds = sf.meeting_kinds.filter(x => x !== name);
      }
      renderSourceFilterChips();
    };
    lbl.appendChild(cb);
    const span = document.createElement('span');
    span.textContent = name;
    lbl.appendChild(span);
    mkEl.appendChild(lbl);
  });

  renderSourceFilterChips();
  document.getElementById('dialog-source-filter').showModal();
}

function renderSourceFilterChips() {
  const sf = sourceFilter[_sourceFilterTarget];
  const el = document.getElementById('source-chips');
  el.innerHTML = '';
  const items = [
    ...sf.channels.map(c => ({ kind: 'channels', value: c, label: _channelLabel(c) })),
    ...sf.meeting_kinds.map(k => ({ kind: 'meeting_kinds', value: k, label: `会議: ${k}` })),
  ];
  document.getElementById('source-selected-count').textContent =
    items.length > 0 ? `(${items.length} 件)` : '';
  if (items.length === 0) {
    el.innerHTML = '<span class="text-xs text-gray-400">未選択（全件対象）</span>';
    return;
  }
  items.forEach(it => {
    const chip = document.createElement('span');
    chip.className = 'inline-flex items-center gap-1 bg-blue-100 text-blue-800 rounded-full px-2 py-0.5 text-xs';
    chip.textContent = it.label;
    const x = document.createElement('button');
    x.type = 'button';
    x.textContent = '×';
    x.className = 'ml-1 text-blue-600 hover:text-blue-900 font-bold';
    x.onclick = () => {
      sf[it.kind] = sf[it.kind].filter(v => v !== it.value);
      renderSourceFilterDialog();
    };
    chip.appendChild(x);
    el.appendChild(chip);
  });
}

function renderSourceFilterDialog() {
  // Re-render the dialog body for the same target without closing it
  openSourceFilter(_sourceFilterTarget);
}

function clearSourceFilter() {
  sourceFilter[_sourceFilterTarget] = { channels: [], meeting_kinds: [] };
  renderSourceFilterDialog();
}

function applySourceFilter() {
  _saveSourceFilter();
  _updateSourceFilterButtonLabel(_sourceFilterTarget);
  document.getElementById('dialog-source-filter').close();
  if (_sourceFilterTarget === 'ai') loadActionItems();
  else loadDecisions();
}

// ----------------------------------------------------------------
// Initialization
// ----------------------------------------------------------------
document.addEventListener('DOMContentLoaded', async () => {
  _loadSourceFilter();
  await loadDatabases();
  await loadFilterPresets();
  await loadMilestones();
  initAiGrid();
  initDecGrid();
  initAchGrid();
  initFilesGrid();
  initFilesChannelFilter();
  _updateSourceFilterButtonLabel('ai');
  _updateSourceFilterButtonLabel('dec');
  // 初期化後に admin.js のルーターが起動。editor ページの場合はここでデータ読み込み
  const initHash = location.hash.replace('#', '') || 'dashboard';
  if (initHash === 'ai' || initHash === 'dec' || initHash === 'ach' || initHash === 'files') {
    await loadActionItems();
    await loadDecisions();
    await loadAchievements();
    _filesLoaded = true;
    loadFiles();
  }
  // admin.js のルーター初期化を再トリガー（app.js の DOMContentLoaded が admin.js より後に完了する場合への対処）
  if (typeof handleHashChange === 'function') {
    handleHashChange();
  }
});
