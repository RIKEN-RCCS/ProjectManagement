/* ================================================================
   PM DB Editor — Client-side logic
   ================================================================ */

let aiGrid = null;
let decGrid = null;
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
// Tabs
// ----------------------------------------------------------------
document.querySelectorAll('.tab-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    document.querySelectorAll('.tab-panel').forEach(p => p.classList.add('hidden'));
    document.getElementById('panel-' + btn.dataset.tab).classList.remove('hidden');
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

// Source for decisions (no meeting_id column; derive from source_ref)
function sourceRendererDec(params) {
  const src = params.value || '';
  const ref = (params.data || {}).source_ref || '';
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

// ----------------------------------------------------------------
// Action Items
// ----------------------------------------------------------------
const aiColumnDefs = [
  { field: 'deleted', headerName: '削除', width: 50, pinned: 'left',
    cellRenderer: 'agCheckboxCellRenderer',
    cellEditor: 'agCheckboxCellEditor',
    cellRendererParams: { disabled: false } },
  { field: 'id', headerName: 'ID', editable: false, width: 50, pinned: 'left' },
  { field: 'content', headerName: '内容', width: 380 },
  { field: 'assignee', headerName: '担当者', width: 120 },
  { field: 'due_date', headerName: '期限', width: 110 },
  { field: 'milestone_id', headerName: 'MS', width: 60,
    cellEditor: 'agSelectCellEditor',
    cellEditorParams: { values: [''] } },
  { field: 'done', headerName: '完了', width: 80,
    cellRenderer: 'agCheckboxCellRenderer',
    cellEditor: 'agCheckboxCellEditor',
    cellRendererParams: { disabled: false } },
  { field: 'note', headerName: '対応状況', width: 280 },
  { field: 'extracted_at', headerName: '発生日', editable: false, width: 110 },
  { field: 'source', headerName: '出典', editable: false, width: 110,
    cellRenderer: sourceRenderer },
  { field: 'source_ref', hide: true },
  { field: 'meeting_id', hide: true },
  { field: 'meeting_kind', hide: true },
];

function initAiGrid() {
  const el = document.getElementById('grid-ai');
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
  { field: 'content', headerName: '内容', width: 500 },
  { field: 'extracted_at', headerName: '発生日', editable: false, width: 110 },
  { field: 'source', headerName: '出典', editable: false, width: 110,
    cellRenderer: sourceRendererDec },
  { field: 'source_ref', hide: true },
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
      if (event.colDef.field !== 'source') return;
      const data = event.data || {};
      if (data.source === 'slack' && data.source_ref) {
        window.open(data.source_ref, '_blank');
      } else if (data.source === 'meeting' && data.source_ref) {
        // Derive meeting_id from source_ref path
        const filename = data.source_ref.split('/').pop().replace(/\.md$/, '');
        const kind = filename.length > 11 ? filename.substring(11) : '';
        openMinutes(filename, kind);
      }
    },
  });
}

async function loadDecisions() {
  const del = document.getElementById('f-dec-del').value;
  const since = document.getElementById('f-dec-since').value;
  const qs = new URLSearchParams({ acknowledged: 'すべて', deleted: del, since });
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
// Initialization
// ----------------------------------------------------------------
document.addEventListener('DOMContentLoaded', async () => {
  await loadDatabases();
  await loadMilestones();
  initAiGrid();
  initDecGrid();
  await loadActionItems();
  await loadDecisions();
});
