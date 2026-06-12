/* ================================================================
   PM Admin Dashboard — SPA Router + Page Loader + Job Poller
   ================================================================ */

// ----------------------------------------------------------------
// Page module registry
// ----------------------------------------------------------------
const adminPages = {};
const pageCleanups = {};

function registerAdminPage(name, loader) {
  adminPages[name] = loader;
}

// ----------------------------------------------------------------
// Hash-based router
// ----------------------------------------------------------------
let currentPage = 'dashboard';
let pageInitialized = {};

function navigateTo(page) {
  if (page === currentPage && pageInitialized[page]) return;

  // Run cleanup for previous page before switching
  if (currentPage && pageCleanups[currentPage]) {
    try { pageCleanups[currentPage](); } catch (_) {}
    delete pageCleanups[currentPage];
    pageInitialized[currentPage] = false;
  }

  currentPage = page;

  // Hide all admin pages
  document.querySelectorAll('.admin-page').forEach(el => el.classList.add('hidden'));
  // Hide all editor panels
  document.querySelectorAll('.tab-panel').forEach(el => el.classList.add('hidden'));

  // Update nav active states
  document.querySelectorAll('.admin-nav-btn').forEach(b => b.classList.remove('active'));
  document.querySelectorAll('.admin-nav-btn').forEach(b => {
    if (b.dataset.page === page) {
      b.classList.add('active');
      b.classList.remove('text-gray-400');
      b.classList.add('text-white');
    } else {
      b.classList.remove('text-white');
      b.classList.add('text-gray-400');
    }
  });

  const isEditorPage = page === 'ai' || page === 'dec' || page === 'files';

  if (isEditorPage) {
    const panel = document.getElementById('panel-' + page);
    panel.classList.remove('hidden');
    panel.classList.add('flex');
    // Trigger grid resize (delay for layout settle)
    setTimeout(() => {
      const grid = page === 'ai' ? window.aiGrid : (page === 'dec' ? window.decGrid : window.filesGrid);
      if (grid) grid.sizeColumnsToFit();
    }, 50);
  } else {
    document.getElementById('editor-nav').classList.add('hidden');
    // Show admin page
    const pageEl = document.getElementById('page-' + page);
    if (pageEl) {
      pageEl.classList.remove('hidden');
      // Initialize page (re-initialize if previously cleaned up)
      if (!pageInitialized[page] && adminPages[page]) {
        const result = adminPages[page](pageEl);
        pageInitialized[page] = true;
        if (result && typeof result.then === 'function') {
          // Async loader — don't mark initialized until complete
          pageInitialized[page] = false;
          result.then((cleanup) => {
            pageInitialized[page] = true;
            if (typeof cleanup === 'function') pageCleanups[page] = cleanup;
          }).catch(() => {
            pageInitialized[page] = true;
          });
        } else if (typeof result === 'function') {
          pageCleanups[page] = result;
        }
      }
    }
  }
}

function handleHashChange() {
  const hash = location.hash.replace('#', '') || 'dashboard';
  if (hash === 'editor' || hash === '') {
    navigateTo('dashboard');
  } else if (hash === 'ai' || hash === 'dec' || hash === 'files') {
    navigateTo(hash);
  } else if (adminPages[hash]) {
    navigateTo(hash);
  } else {
    navigateTo('dashboard');
  }
}

// ----------------------------------------------------------------
// Nav event binding
// ----------------------------------------------------------------
function initRouter() {
  // Admin nav
  document.querySelectorAll('.admin-nav-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      location.hash = btn.dataset.page;
    });
  });

  // Editor nav（タブは廃止: ナビゲーションボタンで直接遷移）

  // Listen for hash changes
  window.addEventListener('hashchange', handleHashChange);

  // Initial navigation
  if (!location.hash) location.hash = 'dashboard';
  handleHashChange();
}

// DOMContentLoaded 時に初期化（全 script タグの実行完了後に発火する）
document.addEventListener('DOMContentLoaded', initRouter);

// 念のため即時実行でも試みる（既に DOMContentLoaded が発火済みの場合）
if (document.readyState === 'complete' || document.readyState === 'interactive') {
  initRouter();
}

// ----------------------------------------------------------------
// Job Poller
// ----------------------------------------------------------------
class JobPoller {
  constructor() {
    this.activeJobs = new Set();
    this.interval = null;
    this.callbacks = {};
  }

  watch(jobId, callbacks) {
    this.activeJobs.add(jobId);
    this.callbacks[jobId] = callbacks || {};
    if (!this.interval) {
      this.interval = setInterval(() => this._poll(), 3000);
    }
  }

  unwatch(jobId) {
    this.activeJobs.delete(jobId);
    delete this.callbacks[jobId];
    if (this.activeJobs.size === 0 && this.interval) {
      clearInterval(this.interval);
      this.interval = null;
    }
  }

  async _poll() {
    for (const jobId of this.activeJobs) {
      try {
        const data = await api('GET', '/admin/jobs/' + jobId);
        const cb = this.callbacks[jobId];
        if (cb.onUpdate) cb.onUpdate(data);
        if (data.status === 'success' || data.status === 'error') {
          this.unwatch(jobId);
          if (cb.onComplete) cb.onComplete(data);
        }
      } catch (e) {
        // Connection error — retry next cycle
      }
    }
  }
}

const jobPoller = new JobPoller();

// ----------------------------------------------------------------
// Admin API helper
// ----------------------------------------------------------------
async function adminApi(method, path, body) {
  return api(method, '/admin' + path, body);
}

// ----------------------------------------------------------------
// Confirmation dialog
// ----------------------------------------------------------------
function showConfirm(title, message) {
  return new Promise((resolve) => {
    const dlg = document.getElementById('dialog-confirm');
    document.getElementById('confirm-title').textContent = title;
    document.getElementById('confirm-message').textContent = message;
    const okBtn = document.getElementById('confirm-ok');
    const handler = () => {
      dlg.close();
      okBtn.removeEventListener('click', handler);
      resolve(true);
    };
    okBtn.addEventListener('click', handler);
    dlg.showModal();
  });
}

// ----------------------------------------------------------------
// Admin card component helper
// ----------------------------------------------------------------
function createAdminCard(title, content, className = '') {
  const card = document.createElement('div');
  card.className = `admin-card ${className}`;
  if (title) {
    const h3 = document.createElement('h3');
    h3.className = 'text-sm font-bold text-gray-300 mb-2';
    h3.textContent = title;
    card.appendChild(h3);
  }
  if (typeof content === 'string') {
    card.innerHTML += content;
  } else if (content instanceof HTMLElement) {
    card.appendChild(content);
  }
  return card;
}

// ----------------------------------------------------------------
// Stat card (used on dashboard)
// ----------------------------------------------------------------
function createStatCard(label, value, color = 'blue') {
  const colors = {
    blue: 'border-blue-500 text-blue-400',
    green: 'border-green-500 text-green-400',
    red: 'border-red-500 text-red-400',
    yellow: 'border-yellow-500 text-yellow-400',
    purple: 'border-purple-500 text-purple-400',
  };
  const c = colors[color] || colors.blue;
  return `
    <div class="admin-stat-card border-l-4 ${c}">
      <div class="text-3xl font-bold">${value}</div>
      <div class="text-xs text-gray-400 mt-1">${label}</div>
    </div>
  `;
}

// ----------------------------------------------------------------
// Status dot
// ----------------------------------------------------------------
function statusDot(running) {
  if (running === true) return '<span class="status-dot status-running" title="Running"></span>';
  if (running === false) return '<span class="status-dot status-stopped" title="Stopped"></span>';
  return '<span class="status-dot status-unknown" title="Unknown"></span>';
}

// ----------------------------------------------------------------
// Loading spinner
// ----------------------------------------------------------------
function showSpinner(container, message) {
  container.innerHTML = `
    <div class="flex items-center justify-center py-12">
      <div class="spinner"></div>
      <span class="ml-3 text-gray-400">${message || 'Loading...'}</span>
    </div>
  `;
}

function showEmpty(container, message) {
  container.innerHTML = `
    <div class="flex items-center justify-center py-12">
      <span class="text-gray-500 italic">${message || 'No data'}</span>
    </div>
  `;
}

// ----------------------------------------------------------------
// Formatting helpers
// ----------------------------------------------------------------
function fmtDateTime(iso) {
  if (!iso) return '-';
  try {
    const d = new Date(iso);
    return d.toLocaleString('ja-JP', { month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit' });
  } catch { return iso; }
}

function fmtDate(iso) {
  if (!iso) return '-';
  try {
    const d = new Date(iso);
    return d.toLocaleDateString('ja-JP', { year: 'numeric', month: '2-digit', day: '2-digit' });
  } catch { return iso; }
}

function fmtDuration(startIso, endIso) {
  if (!startIso || !endIso) return '-';
  try {
    const s = new Date(startIso).getTime();
    const e = new Date(endIso).getTime();
    const diff = Math.max(0, Math.round((e - s) / 1000));
    if (diff < 60) return `${diff}s`;
    if (diff < 3600) return `${Math.floor(diff / 60)}m ${diff % 60}s`;
    return `${Math.floor(diff / 3600)}h ${Math.floor((diff % 3600) / 60)}m`;
  } catch { return '-'; }
}

// ----------------------------------------------------------------
// Escape HTML
// ----------------------------------------------------------------
function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}
