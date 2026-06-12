/* ================================================================
   Dashboard Page — Vue 3
   ================================================================ */

registerAdminPage('dashboard', async (container) => {
  container.innerHTML = `<div id="dashboard-app"></div>`;

  const app = Vue.createApp({
    data() {
      return {
        loading: true,
        loadError: '',
        stats: {},
        services: [],
        minutes: [],
        errors: [],
      };
    },
    computed: {
      statCards() {
        const s = this.stats;
        return [
          { label: 'Open Action Items', value: s.open_action_items ?? '-', color: 'blue' },
          { label: 'Unacknowledged Decisions', value: s.unacknowledged_decisions ?? '-', color: 'yellow' },
          { label: 'Active Milestones', value: s.active_milestones ?? '-', color: 'green' },
          { label: 'Overdue Items', value: s.overdue_items ?? '-', color: s.overdue_items > 0 ? 'red' : 'green' },
        ];
      },
      recentMinutes() {
        return (this.minutes || []).slice(0, 10);
      },
    },
    methods: {
      statCardColor(color) {
        const map = { blue: 'border-blue-500 text-blue-400', green: 'border-green-500 text-green-400', red: 'border-red-500 text-red-400', yellow: 'border-yellow-500 text-yellow-400', purple: 'border-purple-500 text-purple-400' };
        return map[color] || map.blue;
      },
      dotClass(running) {
        return running ? 'status-running' : 'status-stopped';
      },
      statusText(svc) {
        return svc.running ? 'Running' : (svc.status === 'stale' ? 'Stale' : 'Stopped');
      },
      statusColor(svc) {
        return svc.running ? 'text-green-400' : (svc.status === 'stale' ? 'text-yellow-400' : 'text-red-400');
      },
      errorClass(line) {
        return (line.includes('ERROR') || line.includes('Exception') || line.includes('Traceback')) ? 'text-red-400' : 'text-yellow-400';
      },
    },
    async mounted() {
      try {
        const [statsData, svcData, minData] = await Promise.all([
          api('GET', '/admin/stats'),
          api('GET', '/admin/services'),
          api('GET', '/admin/minutes/recent'),
        ]);
        this.stats = statsData;
        this.services = svcData.services || [];
        this.minutes = minData.minutes || [];
        try {
          const errData = await api('GET', '/admin/logs/recent-errors');
          this.errors = errData.errors || [];
        } catch (_) {}
      } catch (e) {
        this.loadError = e.message;
      } finally {
        this.loading = false;
      }
    },
    template: `
      <div class="max-w-6xl mx-auto">

        <div v-if="loading" class="flex items-center justify-center py-12">
          <div class="spinner"></div>
          <span class="ml-3 text-gray-400">Loading dashboard...</span>
        </div>

        <div v-else-if="loadError" class="flex items-center justify-center py-16">
          <div class="text-center">
            <div class="text-4xl mb-4 text-gray-600">⚠️</div>
            <p class="text-gray-400">Failed to load dashboard data</p>
            <p class="text-xs text-gray-600 mt-2">{{ loadError }}</p>
          </div>
        </div>

        <template v-else>
          <!-- Stats Row -->
          <div class="grid grid-cols-2 md:grid-cols-4 gap-4 mb-6">
            <div v-for="card in statCards" :key="card.label"
                 class="admin-stat-card border-l-4" :class="statCardColor(card.color)">
              <div class="text-3xl font-bold">{{ card.value }}</div>
              <div class="text-xs text-gray-400 mt-1">{{ card.label }}</div>
            </div>
          </div>

          <!-- Services Row -->
          <div class="grid grid-cols-2 gap-4 mb-6">
            <div v-for="svc in services" :key="svc.name" class="admin-card flex items-center gap-3">
              <span class="status-dot" :class="dotClass(svc.running)"></span>
              <div>
                <div class="text-sm font-bold text-gray-200">{{ svc.name.toUpperCase() }}</div>
                <div class="text-xs" :class="statusColor(svc)">{{ statusText(svc) }}</div>
                <div class="text-xs text-gray-500">PID: {{ svc.pid || '-' }}</div>
              </div>
            </div>
          </div>

          <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
            <!-- Recent Minutes -->
            <div class="admin-card">
              <h3 class="text-sm font-bold text-gray-300 mb-2">Recent Minutes</h3>
              <p v-if="recentMinutes.length === 0" class="text-gray-500 italic text-sm">No minutes found</p>
              <table v-else class="w-full text-xs">
                <thead>
                  <tr class="text-gray-500 border-b border-gray-700">
                    <th class="text-left py-1 pr-2">Date</th>
                    <th class="text-left py-1 pr-2">Meeting</th>
                    <th class="text-left py-1">Kind</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="m in recentMinutes" :key="m.id"
                      class="border-b border-gray-700/50 hover:bg-gray-700/30">
                    <td class="py-1 pr-2 text-gray-400">{{ m.held_at || '-' }}</td>
                    <td class="py-1 pr-2 text-gray-200">{{ m.meeting_name || m.title || '-' }}</td>
                    <td class="py-1 text-gray-400">{{ m.kind }}</td>
                  </tr>
                </tbody>
              </table>
            </div>

            <!-- Recent Errors -->
            <div class="admin-card">
              <h3 class="text-sm font-bold text-gray-300 mb-2">Recent Log Issues</h3>
              <p v-if="errors.length === 0" class="text-gray-500 italic text-sm">No issues found in recent logs</p>
              <div v-else class="max-h-64 overflow-y-auto">
                <div v-for="(e, i) in errors" :key="i" class="text-xs mb-1 pb-1 border-b border-gray-700/50">
                  <span class="text-gray-500">[{{ e.file }}]</span>
                  <span :class="errorClass(e.line)">{{ e.line }}</span>
                </div>
              </div>
            </div>
          </div>
        </template>
      </div>
    `,
  });

  app.mount(container.querySelector('#dashboard-app'));
  return () => { app.unmount(); };
});
