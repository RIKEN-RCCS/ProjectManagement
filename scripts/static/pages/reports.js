/* ================================================================
   Reports Page — Vue 3
   ================================================================ */

registerAdminPage('reports', async (container) => {
  container.innerHTML = `<div id="reports-app"></div>`;

  const app = Vue.createApp({
    data() {
      return {
        reportSince: '',
        reportSkipCanvas: false,
        insightSince: '',
        insightSkipCanvas: false,
        xlsxSince: '',
        xlsxSkipCanvas: false,
        history: [],
        historyLoaded: false,
      };
    },
    methods: {
      async runReport(type) {
        const sinceKey = { report: 'reportSince', insight: 'insightSince', xlsx_report: 'xlsxSince' }[type];
        const skipKey = { report: 'reportSkipCanvas', insight: 'insightSkipCanvas', xlsx_report: 'xlsxSkipCanvas' }[type];
        const params = {
          report_type: type,
          since: this[sinceKey] || null,
          skip_canvas: this[skipKey],
        };
        try {
          const res = await api('POST', '/admin/reports/generate', params);
          toast(`Report ${type} started! Job: ${res.job_id}`, 'positive');
          this.loadHistory();
        } catch (e) {
          toast('Error: ' + e.message, 'negative');
        }
      },
      async loadHistory() {
        try {
          const data = await api('GET', '/admin/jobs?kind=report&limit=15');
          this.history = data.jobs || [];
          this.historyLoaded = true;
        } catch (_) { this.historyLoaded = true; }
      },
      jobType(job) {
        try { return JSON.parse(job.params_json || '{}').report_type || '-'; }
        catch (_) { return '-'; }
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
        <h2 class="text-xl font-bold text-white mb-4">Report Generation</h2>

        <div class="grid grid-cols-1 md:grid-cols-3 gap-4 mb-6">

          <!-- Weekly Report -->
          <div class="admin-card">
            <div class="text-3xl mb-2">📋</div>
            <h3 class="text-sm font-bold text-gray-200 mb-1">Weekly Report</h3>
            <p class="text-xs text-gray-500 mb-3">Generate progress report &rarr; Canvas</p>
            <div class="mb-3">
              <label class="text-xs text-gray-400 block mb-1">Since (optional)</label>
              <input type="date" v-model="reportSince"
                     class="bg-gray-700 border border-gray-600 rounded px-2 py-1.5 text-sm w-full text-gray-200">
            </div>
            <div class="flex items-center gap-2 mb-3">
              <input type="checkbox" v-model="reportSkipCanvas" id="rsc"
                     class="rounded bg-gray-700 border-gray-600">
              <label for="rsc" class="text-xs text-gray-400">Skip Canvas post</label>
            </div>
            <button @click="runReport('report')"
                    class="w-full bg-blue-600 hover:bg-blue-700 text-white rounded px-3 py-2 text-sm font-medium">Generate Report</button>
          </div>

          <!-- Insight -->
          <div class="admin-card">
            <div class="text-3xl mb-2">💡</div>
            <h3 class="text-sm font-bold text-gray-200 mb-1">Project Insight</h3>
            <p class="text-xs text-gray-500 mb-3">Health assessment with LLM analysis</p>
            <div class="mb-3">
              <label class="text-xs text-gray-400 block mb-1">Since (optional)</label>
              <input type="date" v-model="insightSince"
                     class="bg-gray-700 border border-gray-600 rounded px-2 py-1.5 text-sm w-full text-gray-200">
            </div>
            <div class="flex items-center gap-2 mb-3">
              <input type="checkbox" v-model="insightSkipCanvas" id="isc"
                     class="rounded bg-gray-700 border-gray-600">
              <label for="isc" class="text-xs text-gray-400">Skip Canvas post</label>
            </div>
            <button @click="runReport('insight')"
                    class="w-full bg-purple-600 hover:bg-purple-700 text-white rounded px-3 py-2 text-sm font-medium">Generate Insight</button>
          </div>

          <!-- XLSX Report -->
          <div class="admin-card">
            <div class="text-3xl mb-2">📊</div>
            <h3 class="text-sm font-bold text-gray-200 mb-1">XLSX Report</h3>
            <p class="text-xs text-gray-500 mb-3">Excel workbook &rarr; Box + Canvas</p>
            <div class="mb-3">
              <label class="text-xs text-gray-400 block mb-1">Since (optional)</label>
              <input type="date" v-model="xlsxSince"
                     class="bg-gray-700 border border-gray-600 rounded px-2 py-1.5 text-sm w-full text-gray-200">
            </div>
            <div class="flex items-center gap-2 mb-3">
              <input type="checkbox" v-model="xlsxSkipCanvas" id="xsc"
                     class="rounded bg-gray-700 border-gray-600">
              <label for="xsc" class="text-xs text-gray-400">Skip Canvas post</label>
            </div>
            <button @click="runReport('xlsx_report')"
                    class="w-full bg-green-600 hover:bg-green-700 text-white rounded px-3 py-2 text-sm font-medium">Generate XLSX</button>
          </div>

        </div>

        <!-- Job History -->
        <div class="admin-card">
          <h3 class="text-sm font-bold text-gray-300 mb-2">Recent Report Jobs</h3>
          <p v-if="!historyLoaded" class="text-gray-500 italic text-sm">Loading...</p>
          <p v-else-if="history.length === 0" class="text-gray-500 italic text-sm">No reports generated yet</p>
          <table v-else class="w-full text-xs">
            <thead>
              <tr class="text-gray-500 border-b border-gray-700">
                <th class="text-left py-1 pr-2">Job</th>
                <th class="text-left py-1 pr-2">Type</th>
                <th class="text-left py-1 pr-2">Status</th>
                <th class="text-left py-1 pr-2">Started</th>
                <th class="text-left py-1">Duration</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="j in history" :key="j.id"
                  class="border-b border-gray-700/50 hover:bg-gray-700/30">
                <td class="py-1 pr-2 font-mono text-gray-400">{{ j.id }}</td>
                <td class="py-1 pr-2 text-gray-200">{{ jobType(j) }}</td>
                <td class="py-1 pr-2">{{ statusIcon(j) }} {{ j.status }}</td>
                <td class="py-1 pr-2 text-gray-400">{{ fmtDateTime(j.started_at) }}</td>
                <td class="py-1 text-gray-400">{{ fmtDuration(j.started_at, j.finished_at) }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    `,
  });

  app.config.globalProperties.fmtDateTime = window.fmtDateTime || (v => v || '');
  app.config.globalProperties.fmtDuration = window.fmtDuration || ((s, e) => s && e ? '...' : '');
  app.mount(container.querySelector('#reports-app'));
  return () => { app.unmount(); };
});
