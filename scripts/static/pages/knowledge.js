/* ================================================================
   Knowledge Page — Vue 3
   ================================================================ */

registerAdminPage('knowledge', async (container) => {
  container.innerHTML = `<div id="knowledge-app"></div>`;

  const app = Vue.createApp({
    data() {
      return {
        distillSource: '',
        distillSince: '',
        distillForce: false,
        embedIndex: '',
        embedFullRebuild: false,
        history: [],
        historyLoaded: false,
      };
    },
    methods: {
      async runDistill() {
        const params = {};
        if (this.distillSource) params.source = this.distillSource;
        if (this.distillSince) params.since = this.distillSince;
        params.force = this.distillForce;
        try {
          const res = await api('POST', '/admin/knowledge/distill', params);
          toast(`Distill started! Job: ${res.job_id}`, 'positive');
          this.loadHistory();
        } catch (e) {
          toast('Error: ' + e.message, 'negative');
        }
      },
      async runEmbed() {
        const params = {};
        if (this.embedIndex) params.index_name = this.embedIndex;
        params.full_rebuild = this.embedFullRebuild;
        try {
          const res = await api('POST', '/admin/knowledge/embed', params);
          toast(`Embed started! Job: ${res.job_id}`, 'positive');
          this.loadHistory();
        } catch (e) {
          toast('Error: ' + e.message, 'negative');
        }
      },
      async loadHistory() {
        try {
          const [data, data2] = await Promise.all([
            api('GET', '/admin/jobs?kind=distill&limit=10'),
            api('GET', '/admin/jobs?kind=embed&limit=10'),
          ]);
          const jobs = [...(data.jobs || []), ...(data2.jobs || [])]
            .sort((a, b) => (b.created_at || '').localeCompare(a.created_at || ''))
            .slice(0, 20);
          this.history = jobs;
          this.historyLoaded = true;
        } catch (_) { this.historyLoaded = true; }
      },
      statusIcon(job) {
        if (job.status === 'success') return '✅';
        if (job.status === 'error') return '❌';
        return '🔄';
      },
      kindIcon(job) {
        return job.kind === 'distill' ? '🧠' : '🔍';
      },
    },
    mounted() {
      this.loadHistory();
    },
    template: `
      <div class="max-w-4xl mx-auto">
        <h2 class="text-xl font-bold text-white mb-4">Knowledge Operations</h2>

        <div class="grid grid-cols-1 md:grid-cols-2 gap-4 mb-6">

          <!-- Distill Card -->
          <div class="admin-card">
            <div class="text-2xl mb-2">🧠</div>
            <h3 class="text-sm font-bold text-gray-200 mb-1">Distill Knowledge</h3>
            <p class="text-xs text-gray-500 mb-3">Extract knowledge from Box docs, minutes, or decisions</p>
            <div class="mb-3">
              <label class="text-xs text-gray-400 block mb-1">Source</label>
              <select v-model="distillSource"
                      class="bg-gray-700 border border-gray-600 rounded px-2 py-1.5 text-sm w-full text-gray-200">
                <option value="">All sources</option>
                <option value="box">Box documents</option>
                <option value="minutes">Meeting minutes</option>
                <option value="decisions">Decisions</option>
              </select>
            </div>
            <div class="mb-3">
              <label class="text-xs text-gray-400 block mb-1">Since (optional)</label>
              <input type="date" v-model="distillSince"
                     class="bg-gray-700 border border-gray-600 rounded px-2 py-1.5 text-sm w-full text-gray-200">
            </div>
            <div class="flex items-center gap-2 mb-3">
              <input type="checkbox" v-model="distillForce" id="df"
                     class="rounded bg-gray-700 border-gray-600">
              <label for="df" class="text-xs text-gray-400">Force re-distill (ignore cache)</label>
            </div>
            <button @click="runDistill"
                    class="w-full bg-purple-600 hover:bg-purple-700 text-white rounded px-3 py-2 text-sm font-medium">Run Distill</button>
          </div>

          <!-- Embed Card -->
          <div class="admin-card">
            <div class="text-2xl mb-2">🔍</div>
            <h3 class="text-sm font-bold text-gray-200 mb-1">Rebuild FTS5 Index</h3>
            <p class="text-xs text-gray-500 mb-3">Rebuild full-text search indexes for Argus QA</p>
            <div class="mb-3">
              <label class="text-xs text-gray-400 block mb-1">Index name (optional)</label>
              <input type="text" v-model="embedIndex" placeholder="Default index"
                     class="bg-gray-700 border border-gray-600 rounded px-2 py-1.5 text-sm w-full text-gray-200">
            </div>
            <div class="flex items-center gap-2 mb-3">
              <input type="checkbox" v-model="embedFullRebuild" id="efr"
                     class="rounded bg-gray-700 border-gray-600">
              <label for="efr" class="text-xs text-gray-400">Full rebuild (drop &amp; recreate)</label>
            </div>
            <button @click="runEmbed"
                    class="w-full bg-green-600 hover:bg-green-700 text-white rounded px-3 py-2 text-sm font-medium">Run Embed</button>
          </div>

        </div>

        <!-- Job History -->
        <div class="admin-card">
          <h3 class="text-sm font-bold text-gray-300 mb-2">Recent Jobs</h3>
          <p v-if="!historyLoaded" class="text-gray-500 italic text-sm">Loading...</p>
          <p v-else-if="history.length === 0" class="text-gray-500 italic text-sm">No jobs yet</p>
          <table v-else class="w-full text-xs">
            <thead>
              <tr class="text-gray-500 border-b border-gray-700">
                <th class="text-left py-1 pr-2">Job</th>
                <th class="text-left py-1 pr-2">Type</th>
                <th class="text-left py-1 pr-2">Status</th>
                <th class="text-left py-1 pr-2">Duration</th>
                <th class="text-left py-1">Summary</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="j in history" :key="j.id"
                  class="border-b border-gray-700/50 hover:bg-gray-700/30">
                <td class="py-1 pr-2 font-mono text-gray-400">{{ j.id }}</td>
                <td class="py-1 pr-2">{{ kindIcon(j) }} <span class="text-gray-300">{{ j.kind }}</span></td>
                <td class="py-1 pr-2">{{ statusIcon(j) }} {{ j.status }}</td>
                <td class="py-1 pr-2 text-gray-400">{{ fmtDuration(j.started_at, j.finished_at) }}</td>
                <td class="py-1 text-gray-400">{{ j.summary || '-' }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    `,
  });

  app.config.globalProperties.fmtDuration = window.fmtDuration || ((s, e) => s && e ? '...' : '');
  app.mount(container.querySelector('#knowledge-app'));
  return () => { app.unmount(); };
});
