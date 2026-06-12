/* ================================================================
   Minutes Management Page — Vue 3
   ================================================================ */

registerAdminPage('minutes', async (container) => {
  container.innerHTML = `<div id="minutes-app"></div>`;

  const app = Vue.createApp({
    data() {
      return {
        // List
        minutesList: [],
        total: 0,
        loading: false,
        filterKind: '',
        meetingKinds: [],
        // Edit modal
        editOpen: false,
        editMeetingId: '',
        editKind: '',
        editContent: '',
        editDecisions: [],     // Array<{content, source_context}>
        editActionItems: [],   // Array<{content, assignee, due_date}>
        editSaving: false,
        editActiveTab: 'content',
        // Delete modal
        deleteOpen: false,
        deleteMeetingId: '',
        deleteKind: '',
        deleteLabel: '',
        deleteCascadePm: true,
        deleteCascadeCanvas: false,
        deleteSaving: false,
      };
    },
    computed: {
      filteredList() {
        if (!this.filterKind) return this.minutesList;
        return this.minutesList.filter(m => m.kind === this.filterKind);
      },
    },
    methods: {
      // ---- List ---- //
      async loadList() {
        this.loading = true;
        try {
          const qs = this.filterKind ? '?kind=' + encodeURIComponent(this.filterKind) : '';
          const data = await api('GET', '/admin/minutes/list' + qs);
          this.minutesList = data.minutes || [];
          this.total = data.total || 0;
        } catch (e) {
          toast('Failed to load: ' + e.message, 'negative');
        } finally {
          this.loading = false;
        }
      },
      async loadKinds() {
        try {
          const data = await api('GET', '/admin/minutes/meetings');
          this.meetingKinds = data.meetings || [];
        } catch (_) {}
      },
      hasSlack(m) {
        return !!m.slack_file_permalink;
      },

      // ---- Edit Modal ---- //
      async openEdit(meetingId, kind) {
        this.editMeetingId = meetingId;
        this.editKind = kind;
        this.editOpen = true;
        this.editActiveTab = 'content';
        this.editContent = 'Loading...';
        this.editDecisions = [];
        this.editActionItems = [];
        this.editSaving = false;

        try {
          const [contentData, decData, aiData] = await Promise.all([
            api('GET', `/admin/minutes/content?id=${encodeURIComponent(meetingId)}&kind=${encodeURIComponent(kind)}`),
            api('GET', `/admin/minutes/decisions?id=${encodeURIComponent(meetingId)}&kind=${encodeURIComponent(kind)}`),
            api('GET', `/admin/minutes/action-items?id=${encodeURIComponent(meetingId)}&kind=${encodeURIComponent(kind)}`),
          ]);
          this.editContent = contentData.content || '(empty)';
          this.editDecisions = (decData.items || []).map(d => ({
            content: d.content || '',
            source_context: d.source_context || '',
          }));
          this.editActionItems = (aiData.items || []).map(a => ({
            content: a.content || '',
            assignee: a.assignee || '',
            due_date: a.due_date || '',
          }));
        } catch (e) {
          this.editContent = `Error loading: ${e.message}`;
        }
      },
      closeEdit() {
        this.editOpen = false;
      },
      addDecRow() {
        this.editDecisions.push({ content: '', source_context: '' });
      },
      removeDecRow(idx) {
        this.editDecisions.splice(idx, 1);
      },
      addAiRow() {
        this.editActionItems.push({ content: '', assignee: '', due_date: '' });
      },
      removeAiRow(idx) {
        this.editActionItems.splice(idx, 1);
      },
      normalizeDate(val) {
        const m = (val || '').trim().match(/^(\d{4})[\/\-](\d{1,2})[\/\-](\d{1,2})$/);
        return m ? `${m[1]}-${m[2].padStart(2,'0')}-${m[3].padStart(2,'0')}` : (val || '').trim();
      },
      async saveEdit() {
        this.editSaving = true;

        try {
          // Save content
          const contentRes = await api('POST', '/admin/minutes/content/save', {
            meeting_id: this.editMeetingId,
            kind: this.editKind,
            content: this.editContent,
          });

          // Save decisions (filter empty)
          const decItems = this.editDecisions.filter(d => d.content.trim());
          const decRes = await api('POST', '/admin/minutes/decisions/save', {
            meeting_id: this.editMeetingId,
            kind: this.editKind,
            items: decItems,
          });

          // Save action items (filter empty, normalize dates)
          const aiItems = this.editActionItems
            .filter(a => a.content.trim())
            .map(a => ({ ...a, due_date: this.normalizeDate(a.due_date) }));
          const aiRes = await api('POST', '/admin/minutes/action-items/save', {
            meeting_id: this.editMeetingId,
            kind: this.editKind,
            items: aiItems,
          });

          const parts = [
            contentRes.updated ? 'content' : null,
            `decisions (${decRes.updated || 0})`,
            `AIs (${aiRes.updated || 0})`,
          ].filter(Boolean).join(', ');

          // Publish job (from content save)
          const publishJobId = contentRes.publish_job_id;
          if (publishJobId) {
            toast(`Saved: ${parts}. Publishing to pm.db and Box... (job: ${publishJobId})`, 'positive', 8000);
            jobPoller.watch(publishJobId, {
              onComplete: (data) => {
                if (data.status === 'success') {
                  toast('Publish complete: ' + (data.summary || ''), 'positive', 5000);
                } else {
                  toast('Publish failed: ' + (data.summary || ''), 'negative', 10000);
                }
              },
            });
          } else {
            toast(`Saved: ${parts}`, 'positive');
          }

          this.closeEdit();
          this.loadList();
        } catch (e) {
          toast('Save error: ' + e.message, 'negative');
        } finally {
          this.editSaving = false;
        }
      },

      // ---- Delete Modal ---- //
      openDelete(meetingId, kind, label) {
        this.deleteMeetingId = meetingId;
        this.deleteKind = kind;
        this.deleteLabel = label;
        this.deleteCascadePm = true;
        this.deleteCascadeCanvas = false;
        this.deleteSaving = false;
        this.deleteOpen = true;
      },
      closeDelete() {
        this.deleteOpen = false;
      },
      async confirmDelete() {
        this.deleteSaving = true;
        try {
          const res = await api('POST', '/admin/minutes/delete', {
            meeting_id: this.deleteMeetingId,
            kind: this.deleteKind,
            cascade_pm: this.deleteCascadePm,
            cascade_canvas: this.deleteCascadeCanvas,
          });
          const msg = [
            `Minutes DB: ${res.minutes_db?.instances || 0} instances deleted`,
            res.cascade?.pm_db
              ? `pm.db: ${res.cascade.pm_db.meetings || 0} meetings, ${res.cascade.pm_db.decisions || 0} decisions, ${res.cascade.pm_db.action_items || 0} AIs deleted`
              : null,
            res.cascade?.canvas?.job_id
              ? `Canvas catalog regeneration: job ${res.cascade.canvas.job_id}`
              : null,
          ].filter(Boolean).join(' | ');
          toast(msg, res.minutes_db?.deleted ? 'positive' : 'negative', 5000);
          this.closeDelete();
          this.loadList();
        } catch (e) {
          toast('Delete error: ' + e.message, 'negative');
        } finally {
          this.deleteSaving = false;
        }
      },
    },
    mounted() {
      this.loadKinds();
      this.loadList();
    },
    template: `
      <div class="max-w-6xl mx-auto">
        <div class="flex items-center justify-between mb-4">
          <h2 class="text-xl font-bold text-white">Minutes Management</h2>
          <div class="flex items-center gap-2">
            <label class="text-xs text-gray-400">Filter:</label>
            <select v-model="filterKind" @change="loadList"
                    class="bg-gray-700 border border-gray-600 rounded px-2 py-1 text-sm text-gray-200">
              <option value="">All meetings</option>
              <option v-for="k in meetingKinds" :key="k" :value="k">{{ k }}</option>
            </select>
            <button @click="loadList"
                    class="bg-gray-700 hover:bg-gray-600 rounded px-3 py-1 text-sm text-gray-200">Refresh</button>
          </div>
        </div>

        <!-- Minutes table -->
        <div class="admin-card">
          <p v-if="loading" class="text-gray-500 italic text-sm">Loading...</p>
          <p v-else-if="filteredList.length === 0" class="text-gray-500 italic text-sm">No minutes found</p>
          <div v-else>
            <div class="text-xs text-gray-500 mb-2">{{ total }} meetings</div>
            <table class="w-full text-xs">
              <thead>
                <tr class="text-gray-500 border-b border-gray-700">
                  <th class="text-left py-1.5 pr-2">Date</th>
                  <th class="text-left py-1.5 pr-2">Meeting</th>
                  <th class="text-left py-1.5 pr-2">Imported</th>
                  <th class="text-left py-1.5 pr-2">Slack</th>
                  <th class="text-left py-1.5 pr-2">Actions</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="m in filteredList" :key="m.id"
                    class="border-b border-gray-700/50 hover:bg-gray-700/30">
                  <td class="py-1.5 pr-2 text-gray-200 whitespace-nowrap">{{ m.held_at || '-' }}</td>
                  <td class="py-1.5 pr-2 font-medium text-gray-200">{{ m.meeting_name || m.id }}</td>
                  <td class="py-1.5 pr-2 text-gray-400">{{ fmtDateTime(m.imported_at) }}</td>
                  <td class="py-1.5 pr-2">
                    <a v-if="hasSlack(m)" :href="m.slack_file_permalink" target="_blank"
                       class="text-blue-400 hover:underline">Slack</a>
                    <span v-else class="text-gray-600">—</span>
                  </td>
                  <td class="py-1.5 flex gap-1">
                    <button @click="openEdit(m.id, m.kind)"
                            class="bg-blue-600 hover:bg-blue-700 text-white rounded px-2 py-0.5 text-xs">Edit</button>
                    <button @click="openDelete(m.id, m.kind, m.meeting_name || m.id)"
                            class="bg-red-600 hover:bg-red-700 text-white rounded px-2 py-0.5 text-xs">Delete</button>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>

        <!-- ============ Edit Modal ============ -->
        <Teleport to="body">
          <div v-if="editOpen" class="fixed inset-0 z-50 flex items-center justify-center p-4">
            <div class="absolute inset-0 bg-black/60" @click="closeEdit"></div>
            <div class="relative rounded-lg shadow-xl p-0 w-[880px] bg-gray-800 text-gray-200 flex flex-col"
                 style="max-height: 90vh;">

              <!-- Header -->
              <div class="flex items-start justify-between p-6 pb-0 flex-shrink-0">
                <div>
                  <h2 class="text-lg font-bold text-white">Edit: {{ editMeetingId }}</h2>
                  <div class="text-xs text-gray-400 mt-0.5">Kind: {{ editKind }}</div>
                </div>
                <button type="button" @click="closeEdit"
                        class="text-gray-400 hover:text-white text-xl leading-none">&times;</button>
              </div>

              <!-- Tabs -->
              <div class="flex border-b border-gray-700 px-6 mt-3 flex-shrink-0">
                <button @click="editActiveTab='content'"
                        class="px-4 py-2 text-sm font-medium"
                        :class="editActiveTab==='content' ? 'text-blue-400 border-b-2 border-blue-400' : 'text-gray-400 border-b-2 border-transparent'">
                  Minutes</button>
                <button @click="editActiveTab='decisions'"
                        class="px-4 py-2 text-sm font-medium"
                        :class="editActiveTab==='decisions' ? 'text-blue-400 border-b-2 border-blue-400' : 'text-gray-400 border-b-2 border-transparent'">
                  Decisions</button>
                <button @click="editActiveTab='action-items'"
                        class="px-4 py-2 text-sm font-medium"
                        :class="editActiveTab==='action-items' ? 'text-blue-400 border-b-2 border-blue-400' : 'text-gray-400 border-b-2 border-transparent'">
                  Action Items</button>
              </div>

              <!-- Tab: Minutes Content -->
              <div v-show="editActiveTab==='content'" class="flex-1 p-4 overflow-y-auto">
                <textarea v-model="editContent"
                          class="w-full bg-gray-900 border border-gray-600 rounded p-3 text-sm text-gray-200 font-mono"
                          rows="22" style="min-height: 300px;"></textarea>
              </div>

              <!-- Tab: Decisions -->
              <div v-show="editActiveTab==='decisions'" class="flex-1 p-4 overflow-y-auto">
                <div class="flex items-center gap-2 mb-3">
                  <button @click="addDecRow"
                          class="bg-green-600 hover:bg-green-700 text-white rounded px-3 py-1 text-xs">+ Add</button>
                  <span class="text-xs text-gray-500">Decisions are saved independently from Minutes content</span>
                </div>
                <div v-if="editDecisions.length === 0" class="text-gray-500 italic text-xs">
                  No decisions. Click "+ Add" to create one.
                </div>
                <div v-for="(d, i) in editDecisions" :key="i" class="flex gap-2 mb-2 items-start">
                  <span class="text-xs text-gray-500 mt-2 w-6">#{{ i+1 }}</span>
                  <input type="text" v-model="d.content"
                         class="flex-1 bg-gray-900 border border-gray-600 rounded px-2 py-1.5 text-sm text-gray-200"
                         placeholder="Decision content">
                  <button @click="removeDecRow(i)"
                          class="text-red-400 hover:text-red-300 text-xs mt-1.5">&times;</button>
                </div>
              </div>

              <!-- Tab: Action Items -->
              <div v-show="editActiveTab==='action-items'" class="flex-1 p-4 overflow-y-auto">
                <div class="flex items-center gap-2 mb-3">
                  <button @click="addAiRow"
                          class="bg-green-600 hover:bg-green-700 text-white rounded px-3 py-1 text-xs">+ Add</button>
                  <span class="text-xs text-gray-500">Action Items are saved independently from Minutes content</span>
                </div>
                <div v-if="editActionItems.length === 0" class="text-gray-500 italic text-xs">
                  No action items. Click "+ Add" to create one.
                </div>
                <div v-for="(a, i) in editActionItems" :key="i" class="flex gap-2 mb-2 items-start">
                  <span class="text-xs text-gray-500 mt-2 w-6">#{{ i+1 }}</span>
                  <input type="text" v-model="a.content"
                         class="flex-[3] bg-gray-900 border border-gray-600 rounded px-2 py-1.5 text-sm text-gray-200"
                         placeholder="Action item">
                  <input type="text" v-model="a.assignee"
                         class="flex-1 bg-gray-900 border border-gray-600 rounded px-2 py-1.5 text-sm text-gray-200"
                         placeholder="Assignee">
                  <input type="text" v-model="a.due_date"
                         class="w-28 bg-gray-900 border border-gray-600 rounded px-2 py-1.5 text-sm text-gray-200"
                         placeholder="YYYY-MM-DD">
                  <button @click="removeAiRow(i)"
                          class="text-red-400 hover:text-red-300 text-xs mt-1.5">&times;</button>
                </div>
              </div>

              <!-- Footer -->
              <div class="flex justify-end gap-2 p-4 border-t border-gray-700 flex-shrink-0">
                <button type="button" @click="closeEdit"
                        class="px-4 py-1.5 text-sm text-gray-400 hover:text-white">Cancel</button>
                <button @click="saveEdit" :disabled="editSaving"
                        class="bg-blue-600 hover:bg-blue-700 text-white rounded px-4 py-1.5 text-sm font-medium"
                        :class="{ 'opacity-50': editSaving }">
                  {{ editSaving ? 'Saving...' : 'Save All' }}
                </button>
              </div>
            </div>
          </div>

          <!-- ============ Delete Modal ============ -->
          <div v-if="deleteOpen" class="fixed inset-0 z-50 flex items-center justify-center p-4">
            <div class="absolute inset-0 bg-black/60" @click="closeDelete"></div>
            <div class="relative rounded-lg shadow-xl p-6 w-[500px] bg-gray-800 text-gray-200">
              <h2 class="text-lg font-bold text-white mb-3">Confirm Delete</h2>
              <p class="text-sm mb-2">
                Delete "{{ deleteLabel }}" ({{ deleteMeetingId }})? This will remove from minutes DB.
              </p>
              <div class="bg-gray-900 rounded p-3 mb-3 text-sm">
                <label class="flex items-center gap-2 mb-2">
                  <input type="checkbox" v-model="deleteCascadePm"
                         class="rounded bg-gray-700 border-gray-600">
                  <span class="text-gray-300">Also delete from pm.db</span>
                </label>
                <label class="flex items-center gap-2">
                  <input type="checkbox" v-model="deleteCascadeCanvas"
                         class="rounded bg-gray-700 border-gray-600">
                  <span class="text-gray-300">Regenerate Canvas catalog (remove this entry)</span>
                </label>
              </div>
              <div class="flex justify-end gap-2">
                <button type="button" @click="closeDelete"
                        class="px-4 py-1.5 text-sm text-gray-400 hover:text-white">Cancel</button>
                <button @click="confirmDelete" :disabled="deleteSaving"
                        class="bg-red-600 hover:bg-red-700 text-white rounded px-4 py-1.5 text-sm font-medium"
                        :class="{ 'opacity-50': deleteSaving }">
                  {{ deleteSaving ? 'Deleting...' : 'Delete' }}
                </button>
              </div>
            </div>
          </div>
        </Teleport>
      </div>
    `,
  });

  app.config.globalProperties.fmtDateTime = window.fmtDateTime || (v => v || '');
  app.config.globalProperties.esc = window.esc || (v => v || '');
  app.config.globalProperties.fmtDuration = window.fmtDuration || ((s, e) => s && e ? '...' : '');
  const vm = app.mount(container.querySelector('#minutes-app'));
  return () => { app.unmount(); };
});
