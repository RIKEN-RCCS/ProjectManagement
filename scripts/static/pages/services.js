/* ================================================================
   Services Page — Vue 3
   ================================================================ */

registerAdminPage('services', async (container) => {
  container.innerHTML = `<div id="services-app"></div>`;

  const app = Vue.createApp({
    data() {
      return {
        services: [],
        loading: true,
        loadError: '',
        selectedLog: '',
        logContent: '',
        logLoading: false,
        logAutoRefresh: false,
        logTimer: null,
      };
    },
    methods: {
      async loadServices() {
        try {
          const data = await api('GET', '/admin/services');
          this.services = data.services || [];
          if (!this.selectedLog && this.services.length > 0) {
            this.selectedLog = this.services[0].name;
          }
        } catch (e) {
          this.loadError = e.message;
        } finally {
          this.loading = false;
        }
      },
      async serviceAction(name, action) {
        try {
          const res = await api('POST', `/admin/services/${name}/${action}`);
          toast(`${name}: ${action} ${res.success ? 'succeeded' : 'failed'}`, res.success ? 'positive' : 'negative', 4000);
          await this.loadServices();
          this.loadLog();
        } catch (e) {
          toast('Error: ' + e.message, 'negative');
        }
      },
      async loadLog() {
        if (!this.selectedLog) return;
        this.logLoading = true;
        try {
          const data = await api('GET', `/admin/services/${this.selectedLog}/logs?lines=100`);
          if (data.error) {
            this.logContent = data.error;
          } else {
            const lines = data.lines || [];
            this.logContent = lines.length === 0
              ? 'Empty log file'
              : `<div class="text-xs text-gray-400 mb-1">${esc(data.file)} &mdash; ${data.total_lines} total lines</div><pre>${lines.map(l => esc(l)).join('')}</pre>`;
          }
        } catch (e) {
          this.logContent = 'Failed to load log: ' + e.message;
        } finally {
          this.logLoading = false;
        }
      },
      toggleAutoRefresh() {
        if (this.logAutoRefresh) {
          this.logTimer = setInterval(() => this.loadLog(), 5000);
        } else {
          if (this.logTimer) { clearInterval(this.logTimer); this.logTimer = null; }
        }
      },
      dotClass(svc) {
        return svc.running ? 'status-running' : 'status-stopped';
      },
      statusText(svc) {
        return svc.running ? 'Running' : (svc.status === 'stale' ? 'Stale' : 'Stopped');
      },
      statusColor(svc) {
        return svc.running ? 'text-green-400' : (svc.status === 'stale' ? 'text-yellow-400' : 'text-red-400');
      },
    },
    watch: {
      selectedLog() { this.loadLog(); },
      logAutoRefresh() { this.toggleAutoRefresh(); },
    },
    unmounted() {
      if (this.logTimer) clearInterval(this.logTimer);
    },
    mounted() {
      this.loadServices();
    },
    template: `
      <div class="max-w-5xl mx-auto">
        <h2 class="text-xl font-bold text-white mb-4">Service Management</h2>

        <div v-if="loading" class="flex items-center justify-center py-12">
          <div class="spinner"></div>
          <span class="ml-3 text-gray-400">Loading service status...</span>
        </div>

        <div v-else-if="loadError" class="flex items-center justify-center py-16">
          <p class="text-gray-400">Failed to load services: {{ loadError }}</p>
        </div>

        <template v-else>
          <div class="grid grid-cols-1 md:grid-cols-2 gap-4 mb-6">
            <div v-for="svc in services" :key="svc.name" class="admin-card service-card">
              <div class="flex items-center gap-3 mb-3">
                <span class="status-dot" :class="dotClass(svc)"></span>
                <div>
                  <div class="text-sm font-bold text-gray-200">{{ svc.name.toUpperCase() }}</div>
                  <div class="text-xs" :class="statusColor(svc)">{{ statusText(svc) }}</div>
                </div>
              </div>
              <div class="text-xs text-gray-500 mb-3">
                PID: {{ svc.pid || '-' }}<br>
                Log: {{ svc.log_file || '-' }}
              </div>
              <div class="flex gap-2">
                <button v-if="!svc.running" @click="serviceAction(svc.name, 'start')"
                        class="flex-1 bg-green-600 hover:bg-green-700 text-white rounded px-3 py-1.5 text-xs font-medium">Start</button>
                <button v-if="svc.running" @click="serviceAction(svc.name, 'stop')"
                        class="flex-1 bg-red-600 hover:bg-red-700 text-white rounded px-3 py-1.5 text-xs font-medium">Stop</button>
                <button @click="selectedLog = svc.name"
                        class="flex-1 bg-gray-600 hover:bg-gray-500 text-white rounded px-3 py-1.5 text-xs font-medium">View Log</button>
              </div>
            </div>
          </div>

          <!-- Log Viewer -->
          <div class="admin-card">
            <div class="flex items-center justify-between mb-2">
              <h3 class="text-sm font-bold text-gray-300">Log Viewer</h3>
              <div class="flex items-center gap-3">
                <select v-model="selectedLog"
                        class="bg-gray-700 border border-gray-600 rounded px-2 py-1 text-xs text-gray-300">
                  <option v-for="svc in services" :key="svc.name" :value="svc.name">{{ svc.name.toUpperCase() }}</option>
                </select>
                <label class="text-xs text-gray-400 flex items-center gap-1">
                  <input type="checkbox" v-model="logAutoRefresh">
                  Auto-refresh (5s)
                </label>
                <button @click="loadLog"
                        class="bg-gray-700 hover:bg-gray-600 rounded px-2 py-1 text-xs text-gray-300">Refresh</button>
              </div>
            </div>
            <div class="log-viewer">
              <p v-if="logLoading" class="text-gray-500 italic text-xs">Loading log...</p>
              <div v-else-if="logContent" v-html="logContent" class="text-xs"></div>
              <p v-else class="text-gray-500 italic text-xs">Select a service to view logs</p>
            </div>
          </div>
        </template>
      </div>
    `,
  });

  app.config.globalProperties.esc = window.esc || (v => v || '');
  app.mount(container.querySelector('#services-app'));
  return () => { app.unmount(); };
});
