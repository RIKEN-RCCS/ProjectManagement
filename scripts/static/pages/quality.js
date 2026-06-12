/* ================================================================
   Quality Page — Vue 3
   ================================================================ */

registerAdminPage('quality', async (container) => {
  container.innerHTML = `<div id="quality-app"></div>`;

  const app = Vue.createApp({
    data() {
      return {
        screenIncludeDec: false,
        history: [],
        historyLoaded: false,
      };
    },
    methods: {
      async runScreen() {
        const params = { include_decisions: this.screenIncludeDec, export: true };
        try {
          const res = await api('POST', '/admin/quality/screen', params);
          toast(`Screen started! Job: ${res.job_id}`, 'positive');
          this.loadHistory();
        } catch (e) {
          toast('Error: ' + e.message, 'negative');
        }
      },
      async exportRelinkCSV() {
        try {
          const res = await fetch('/api/admin/quality/relink-export');
          if (!res.ok) {
            const data = await res.json();
            toast(data.error || 'Export failed', 'negative');
            return;
          }
          const blob = await res.blob();
          const url = URL.createObjectURL(blob);
          const a = document.createElement('a');
          a.href = url;
          a.download = `pm_relink_export_${new Date().toISOString().slice(0, 10)}.csv`;
          a.click();
          URL.revokeObjectURL(url);
          toast('CSV exported', 'positive');
        } catch (e) {
          toast('Export error: ' + e.message, 'negative');
        }
      },
      async importRelinkCSV(dryRun) {
        const fileInput = this.$refs.csvFile;
        if (!fileInput || !fileInput.files[0]) {
          toast('Please select a CSV file', 'negative');
          return;
        }
        const text = await fileInput.files[0].text();
        if (!text.trim()) {
          toast('CSV file is empty', 'negative');
          return;
        }
        try {
          const res = await api('POST', '/admin/quality/relink-import', {
            csv_content: text,
            dry_run: dryRun,
          });
          toast(`Import ${dryRun ? '(dry run) ' : ''}started! Job: ${res.job_id}`, 'positive');
          this.loadHistory();
        } catch (e) {
          toast('Import error: ' + e.message, 'negative');
        }
      },
      async loadHistory() {
        try {
          const data = await api('GET', '/admin/jobs?kind=screen&limit=10');
          this.history = data.jobs || [];
          this.historyLoaded = true;
        } catch (_) { this.historyLoaded = true; }
      },
      statusIcon(job) {
        if (job.status === 'success') return '✅';
        if (job.status === 'error') return '❌';
        return '🔄';
      },
    },
    mounted() {
      this.loadHistory();
    },
    template: `
      <div class="max-w-4xl mx-auto">
        <h2 class="text-xl font-bold text-white mb-4">Data Quality</h2>

        <div class="grid grid-cols-1 md:grid-cols-2 gap-4 mb-6">

          <!-- Screen Card -->
          <div class="admin-card">
            <div class="text-2xl mb-2">🔍</div>
            <h3 class="text-sm font-bold text-gray-200 mb-1">Screen Duplicates</h3>
            <p class="text-xs text-gray-500 mb-3">Detect exact/near duplicates in pm.db</p>
            <div class="flex items-center gap-2 mb-3">
              <input type="checkbox" v-model="screenIncludeDec" id="sid"
                     class="rounded bg-gray-700 border-gray-600">
              <label for="sid" class="text-xs text-gray-400">Include decisions in screen</label>
            </div>
            <button @click="runScreen"
                    class="w-full bg-yellow-600 hover:bg-yellow-700 text-white rounded px-3 py-2 text-sm font-medium mb-2">Run Screen</button>
            <p class="text-xs text-gray-500 italic">Results appear as CSV export below</p>
          </div>

          <!-- Relink Card -->
          <div class="admin-card">
            <div class="text-2xl mb-2">🔗</div>
            <h3 class="text-sm font-bold text-gray-200 mb-1">Relink (Batch Edit)</h3>
            <p class="text-xs text-gray-500 mb-3">Export/import CSV for batch editing items</p>
            <button @click="exportRelinkCSV"
                    class="w-full bg-blue-600 hover:bg-blue-700 text-white rounded px-3 py-2 text-sm font-medium mb-2">Export CSV</button>
            <div class="border-t border-gray-700 pt-3 mt-3">
              <h4 class="text-xs font-bold text-gray-400 mb-2">Import Edited CSV</h4>
              <input ref="csvFile" type="file" accept=".csv"
                     class="text-xs text-gray-400 mb-2 file:mr-2 file:bg-gray-700 file:text-gray-300 file:border-0 file:rounded file:px-2 file:py-1">
              <div class="flex gap-2">
                <button @click="importRelinkCSV(false)"
                        class="flex-1 bg-green-600 hover:bg-green-700 text-white rounded px-3 py-1.5 text-sm">Import</button>
                <button @click="importRelinkCSV(true)"
                        class="flex-1 bg-gray-600 hover:bg-gray-700 text-white rounded px-3 py-1.5 text-sm">Dry Run</button>
              </div>
            </div>
          </div>

        </div>

        <!-- Job History -->
        <div class="admin-card">
          <h3 class="text-sm font-bold text-gray-300 mb-2">Recent Quality Jobs</h3>
          <p v-if="!historyLoaded" class="text-gray-500 italic text-sm">Loading...</p>
          <p v-else-if="history.length === 0" class="text-gray-500 italic text-sm">No quality jobs yet</p>
          <table v-else class="w-full text-xs">
            <thead>
              <tr class="text-gray-500 border-b border-gray-700">
                <th class="text-left py-1 pr-2">Job</th>
                <th class="text-left py-1 pr-2">Type</th>
                <th class="text-left py-1 pr-2">Status</th>
                <th class="text-left py-1 pr-2">Summary</th>
                <th class="text-left py-1">Duration</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="j in history" :key="j.id"
                  class="border-b border-gray-700/50 hover:bg-gray-700/30">
                <td class="py-1 pr-2 font-mono text-gray-400">{{ j.id }}</td>
                <td class="py-1 pr-2 text-gray-200">{{ j.kind }}</td>
                <td class="py-1 pr-2">{{ statusIcon(j) }} {{ j.status }}</td>
                <td class="py-1 pr-2 text-gray-400">{{ j.summary || '-' }}</td>
                <td class="py-1 text-gray-400">{{ fmtDuration(j.started_at, j.finished_at) }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    `,
  });

  app.config.globalProperties.fmtDuration = window.fmtDuration || ((s, e) => s && e ? '...' : '');
  app.mount(container.querySelector('#quality-app'));
  return () => { app.unmount(); };
});
