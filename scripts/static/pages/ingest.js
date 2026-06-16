/* ================================================================
   Ingest Page — Vue 3
   ================================================================ */

registerAdminPage('ingest', async (container) => {
  container.innerHTML = `<div id="ingest-app"></div>`;

  const app = Vue.createApp({
    data() {
      return {
        channels: {},
        meetingKinds: [],
        slackChannel: '',
        slackSince: '',
        minutesKind: '',
        minutesSince: '',
        embedIndex: '',
        embedFullRebuild: false,
        loading: true,
        loadError: '',
        history: [],
        historyLoaded: false,
      };
    },
    computed: {
      channelEntries() {
        return Object.entries(this.channels);
      },
    },
    methods: {
      async loadSources() {
        try {
          const data = await api('GET', '/admin/ingest/sources');
          this.channels = data.channel_names || {};
          this.meetingKinds = data.meeting_kinds || [];
        } catch (e) {
          this.loadError = e.message;
        } finally {
          this.loading = false;
        }
      },
      async loadHistory() {
        try {
          const [ingestData, embedData] = await Promise.all([
            api('GET', '/admin/jobs?kind=ingest&limit=15'),
            api('GET', '/admin/jobs?kind=embed&limit=10'),
          ]);
          const jobs = [...(ingestData.jobs || []), ...(embedData.jobs || [])]
            .sort((a, b) => (b.created_at || '').localeCompare(a.created_at || ''))
            .slice(0, 20);
          this.history = jobs;
          this.historyLoaded = true;
        } catch (_) {
          this.historyLoaded = true;
        }
      },
      async runIngest(source) {
        if (source === 'slack' && !this.slackChannel) {
          const ok = await showConfirm('Run Slack Ingest', 'Process ALL Slack channels? This may take a while.');
          if (!ok) return;
        }

        const params = { source };
        if (source === 'slack') {
          if (this.slackChannel) params.slack_channel = this.slackChannel;
          if (this.slackSince) params.since = this.slackSince;
        }
        if (source === 'minutes') {
          if (this.minutesSince) params.since = this.minutesSince;
        }

        try {
          const res = await api('POST', '/admin/ingest/run', params);
          toast(`Ingest ${source} started! Job: ${res.job_id}`, 'positive');
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
      jobSource(job) {
        if (job.kind === 'embed') return 'embed';
        try { return JSON.parse(job.params_json || '{}').source || '-'; }
        catch (_) { return '-'; }
      },
      statusIcon(job) {
        if (job.status === 'success') return '✅';
        if (job.status === 'error') return '❌';
        return '🔄';
      },
    },
    mounted() {
      this.loadSources();
      this.loadHistory();
      this._pollTimer = setInterval(() => this.loadHistory(), 10000);
    },
    unmounted() {
      if (this._pollTimer) clearInterval(this._pollTimer);
    },
    template: `
      <div class="max-w-5xl mx-auto">
        <h2 class="text-xl font-bold text-white mb-4">Data Ingestion &amp; Indexing</h2>

        <div v-if="loading" class="flex items-center justify-center py-12">
          <div class="spinner"></div>
          <span class="ml-3 text-gray-400">Loading ingest sources...</span>
        </div>

        <div v-else-if="loadError" class="flex items-center justify-center py-16">
          <p class="text-gray-400">Failed to load: {{ loadError }}</p>
        </div>

        <template v-else>
          <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4 mb-6">

            <!-- Slack Card -->
            <div class="admin-card">
              <div class="text-2xl mb-2">💬</div>
              <h3 class="text-sm font-bold text-gray-200 mb-1">Slack Ingest</h3>
              <p class="text-xs text-gray-500 mb-3">Extract decisions &amp; action items from Slack</p>
              <div class="mb-3">
                <label class="text-xs text-gray-400 block mb-1">Channel</label>
                <select v-model="slackChannel"
                        class="bg-gray-700 border border-gray-600 rounded px-2 py-1.5 text-sm w-full text-gray-200">
                  <option value="">All channels</option>
                  <option v-for="[id, name] in channelEntries" :key="id" :value="id">{{ name }}</option>
                </select>
              </div>
              <div class="mb-3">
                <label class="text-xs text-gray-400 block mb-1">Since (optional)</label>
                <input type="date" v-model="slackSince"
                       class="bg-gray-700 border border-gray-600 rounded px-2 py-1.5 text-sm w-full text-gray-200">
              </div>
              <button @click="runIngest('slack')"
                      class="w-full bg-blue-600 hover:bg-blue-700 text-white rounded px-3 py-2 text-sm font-medium">Run Ingest</button>
            </div>

            <!-- Minutes Card -->
            <div class="admin-card">
              <div class="text-2xl mb-2">📝</div>
              <h3 class="text-sm font-bold text-gray-200 mb-1">Minutes Ingest</h3>
              <p class="text-xs text-gray-500 mb-3">Sync minutes DB to pm.db</p>
              <div class="mb-3">
                <label class="text-xs text-gray-400 block mb-1">Meeting kind (optional)</label>
                <select v-model="minutesKind"
                        class="bg-gray-700 border border-gray-600 rounded px-2 py-1.5 text-sm w-full text-gray-200">
                  <option value="">All meetings</option>
                  <option v-for="mk in meetingKinds" :key="mk.name || mk" :value="mk.name || mk">{{ mk.name || mk }}</option>
                </select>
              </div>
              <div class="mb-3">
                <label class="text-xs text-gray-400 block mb-1">Since (optional)</label>
                <input type="date" v-model="minutesSince"
                       class="bg-gray-700 border border-gray-600 rounded px-2 py-1.5 text-sm w-full text-gray-200">
              </div>
              <button @click="runIngest('minutes')"
                      class="w-full bg-blue-600 hover:bg-blue-700 text-white rounded px-3 py-2 text-sm font-medium">Run Ingest</button>
            </div>

            <!-- Goals Card -->
            <div class="admin-card">
              <div class="text-2xl mb-2">🎯</div>
              <h3 class="text-sm font-bold text-gray-200 mb-1">Goals Ingest</h3>
              <p class="text-xs text-gray-500 mb-3">Sync goals.yaml to pm.db</p>
              <div class="mb-3 text-xs text-gray-400">
                Synchronizes goals and milestones from the YAML configuration. No additional parameters needed.
              </div>
              <button @click="runIngest('goals')"
                      class="w-full bg-green-600 hover:bg-green-700 text-white rounded px-3 py-2 text-sm font-medium">Run Ingest</button>
            </div>

            <!-- Embed Card -->
            <div class="admin-card">
              <div class="text-2xl mb-2">🔍</div>
              <h3 class="text-sm font-bold text-gray-200 mb-1">Rebuild Search Index</h3>
              <p class="text-xs text-gray-500 mb-3">Rebuild FTS5 + bge-m3 embedding indexes for Argus QA</p>
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
                      class="w-full bg-emerald-600 hover:bg-emerald-700 text-white rounded px-3 py-2 text-sm font-medium">Run Embed</button>
            </div>

          </div>

          <!-- Job History -->
          <div class="admin-card">
            <h3 class="text-sm font-bold text-gray-300 mb-2">Recent Ingest &amp; Embed Jobs</h3>
            <p v-if="!historyLoaded" class="text-gray-500 italic text-sm">Loading...</p>
            <p v-else-if="history.length === 0" class="text-gray-500 italic text-sm">No history</p>
            <table v-else class="w-full text-xs">
              <thead>
                <tr class="text-gray-500 border-b border-gray-700">
                  <th class="text-left py-1 pr-2">Job</th>
                  <th class="text-left py-1 pr-2">Kind</th>
                  <th class="text-left py-1 pr-2">Source</th>
                  <th class="text-left py-1 pr-2">Status</th>
                  <th class="text-left py-1 pr-2">Started</th>
                  <th class="text-left py-1">Duration</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="j in history" :key="j.id"
                    class="border-b border-gray-700/50 hover:bg-gray-700/30">
                  <td class="py-1 pr-2 font-mono text-gray-400">{{ j.id }}</td>
                  <td class="py-1 pr-2 text-gray-300">{{ j.kind }}</td>
                  <td class="py-1 pr-2 text-gray-200">{{ jobSource(j) }}</td>
                  <td class="py-1 pr-2">{{ statusIcon(j) }} {{ j.status }}</td>
                  <td class="py-1 pr-2 text-gray-400">{{ fmtDateTime(j.started_at) }}</td>
                  <td class="py-1 text-gray-400">{{ fmtDuration(j.started_at, j.finished_at) }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </template>
      </div>
    `,
  });

  app.config.globalProperties.fmtDateTime = window.fmtDateTime || (v => v || '');
  app.config.globalProperties.fmtDuration = window.fmtDuration || ((s, e) => s && e ? '...' : '');
  app.mount(container.querySelector('#ingest-app'));
  return () => { app.unmount(); };
});
