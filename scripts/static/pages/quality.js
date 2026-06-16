/* ================================================================
   Quality Page — Vue 3
   ================================================================ */

registerAdminPage('quality', async (container) => {
  container.innerHTML = `<div id="quality-app"></div>`;

  const app = Vue.createApp({
    data() {
      return {
        screenIncludeDec: false,
        shortThreshold: 25,
        scanning: false,
        scanError: '',
        actionGroups: [],     // [{category, items:[{id, content, ...}], selected:Set<int>}]
        decisionGroups: [],
        actionFlagged: 0,
        decisionFlagged: 0,
        deleting: false,
      };
    },
    computed: {
      totalSelected() {
        const aiSelected = this.actionGroups.reduce((s, g) => s + g.selected.size, 0);
        const dcSelected = this.decisionGroups.reduce((s, g) => s + g.selected.size, 0);
        return aiSelected + dcSelected;
      },
      hasResults() {
        return this.actionGroups.length > 0 || this.decisionGroups.length > 0;
      },
    },
    methods: {
      categoryLabel(cat) {
        return {
          exact_dup: '完全重複（正規化後一致）',
          near_dup:  '類似重複（先頭一致・表現違い）',
          ambiguous: '曖昧・短すぎ',
        }[cat] || cat;
      },
      categoryBadge(cat) {
        return {
          exact_dup: 'bg-red-700 text-red-100',
          near_dup:  'bg-orange-700 text-orange-100',
          ambiguous: 'bg-yellow-700 text-yellow-100',
        }[cat] || 'bg-gray-700 text-gray-100';
      },
      preselect(items, category) {
        // 推奨: ambiguous は単独アイテムなので未選択。
        // exact_dup/near_dup は先頭が最古（ID 昇順）= 残す推奨、それ以降を選択
        const sel = new Set();
        if (category !== 'ambiguous' && items.length > 1) {
          const sorted = [...items].sort((a, b) => a.id - b.id);
          for (let i = 1; i < sorted.length; i++) sel.add(sorted[i].id);
        }
        return sel;
      },
      toggleItem(group, id) {
        if (group.selected.has(id)) group.selected.delete(id);
        else group.selected.add(id);
        // Force reactivity
        group.selected = new Set(group.selected);
      },
      toggleAllInGroup(group, value) {
        const sel = new Set();
        if (value) for (const it of group.items) sel.add(it.id);
        group.selected = sel;
      },
      async runScan() {
        this.scanning = true;
        this.scanError = '';
        try {
          const params = new URLSearchParams({
            include_decisions: this.screenIncludeDec,
            short_threshold: this.shortThreshold,
          });
          const data = await api('GET', `/admin/quality/screen-preview?${params}`);
          this.actionGroups = (data.action_items?.groups || []).map(g => ({
            category: g.category,
            items: g.items,
            selected: this.preselect(g.items, g.category),
          }));
          this.actionFlagged = data.action_items?.total_flagged || 0;
          this.decisionGroups = (data.decisions?.groups || []).map(g => ({
            category: g.category,
            items: g.items,
            selected: this.preselect(g.items, g.category),
          }));
          this.decisionFlagged = data.decisions?.total_flagged || 0;
          if (!this.hasResults) {
            toast('重複・曖昧アイテムは見つかりませんでした', 'positive');
          } else {
            toast(`AI ${this.actionFlagged} 件 / Dec ${this.decisionFlagged} 件 を検出`, 'positive');
          }
        } catch (e) {
          this.scanError = e.message;
          toast('Error: ' + e.message, 'negative');
        } finally {
          this.scanning = false;
        }
      },
      async deleteSelected() {
        if (this.totalSelected === 0) {
          toast('削除する項目を選択してください', 'negative');
          return;
        }
        const ok = await showConfirm(
          `${this.totalSelected} 件を削除`,
          `選択した ${this.totalSelected} 件を論理削除 (deleted=1) します。よろしいですか？`,
        );
        if (!ok) return;
        this.deleting = true;
        try {
          const action_item_ids = [];
          for (const g of this.actionGroups) for (const id of g.selected) action_item_ids.push(id);
          const decision_ids = [];
          for (const g of this.decisionGroups) for (const id of g.selected) decision_ids.push(id);
          const res = await api('POST', '/admin/quality/delete-items', {
            action_item_ids, decision_ids,
          });
          toast(`削除完了: AI ${res.deleted_action_items} / Dec ${res.deleted_decisions}`, 'positive');
          await this.runScan();  // 再スキャンして結果を更新
        } catch (e) {
          toast('Delete error: ' + e.message, 'negative');
        } finally {
          this.deleting = false;
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
        } catch (e) {
          toast('Import error: ' + e.message, 'negative');
        }
      },
    },
    template: `
      <div class="max-w-6xl mx-auto">
        <h2 class="text-xl font-bold text-white mb-4">Data Quality</h2>

        <div class="grid grid-cols-1 md:grid-cols-2 gap-4 mb-6">

          <!-- Screen Card -->
          <div class="admin-card">
            <div class="text-2xl mb-2">🔍</div>
            <h3 class="text-sm font-bold text-gray-200 mb-1">Detect Duplicates</h3>
            <p class="text-xs text-gray-500 mb-3">Scan pm.db for exact / near duplicates and ambiguous items</p>
            <div class="flex items-center gap-2 mb-2">
              <input type="checkbox" v-model="screenIncludeDec" id="sid"
                     class="rounded bg-gray-700 border-gray-600">
              <label for="sid" class="text-xs text-gray-400">Include decisions</label>
            </div>
            <div class="mb-3">
              <label class="text-xs text-gray-400 block mb-1">Ambiguous threshold (chars)</label>
              <input type="number" v-model.number="shortThreshold" min="0" max="200"
                     class="bg-gray-700 border border-gray-600 rounded px-2 py-1 text-sm w-24 text-gray-200">
            </div>
            <button @click="runScan" :disabled="scanning"
                    class="w-full bg-yellow-600 hover:bg-yellow-700 disabled:bg-gray-600 text-white rounded px-3 py-2 text-sm font-medium">
              {{ scanning ? 'Scanning…' : 'Run Detection' }}
            </button>
          </div>

          <!-- Relink Card -->
          <div class="admin-card">
            <div class="text-2xl mb-2">🔗</div>
            <h3 class="text-sm font-bold text-gray-200 mb-1">Relink (Bulk CSV Edit)</h3>
            <p class="text-xs text-gray-500 mb-3">Export/import CSV for advanced batch edits (milestone, assignee...)</p>
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

        <!-- Results -->
        <div v-if="scanError" class="admin-card mb-4">
          <p class="text-red-400 text-sm">{{ scanError }}</p>
        </div>

        <div v-if="hasResults" class="admin-card mb-4 sticky top-0 z-10 bg-gray-800 border-2 border-yellow-600">
          <div class="flex items-center justify-between">
            <div>
              <span class="text-sm text-gray-200">選択中: <span class="font-bold text-yellow-400">{{ totalSelected }}</span> 件</span>
              <span class="text-xs text-gray-500 ml-3">
                action_items {{ actionFlagged }} 件 / decisions {{ decisionFlagged }} 件 検出
              </span>
            </div>
            <button @click="deleteSelected" :disabled="deleting || totalSelected === 0"
                    class="bg-red-600 hover:bg-red-700 disabled:bg-gray-600 text-white rounded px-4 py-2 text-sm font-medium">
              {{ deleting ? 'Deleting…' : 'Delete Selected (deleted=1)' }}
            </button>
          </div>
        </div>

        <!-- Action Items groups -->
        <template v-if="actionGroups.length > 0">
          <h3 class="text-sm font-bold text-gray-300 mb-2 mt-6">📋 Action Items ({{ actionFlagged }} flagged)</h3>
          <div v-for="(group, gi) in actionGroups" :key="'ai-'+gi" class="admin-card mb-3">
            <div class="flex items-center justify-between mb-2">
              <div>
                <span :class="['inline-block px-2 py-0.5 rounded text-xs font-bold mr-2', categoryBadge(group.category)]">
                  {{ categoryLabel(group.category) }}
                </span>
                <span class="text-xs text-gray-500">{{ group.items.length }} 件</span>
              </div>
              <div class="flex gap-2 text-xs">
                <button @click="toggleAllInGroup(group, true)"
                        class="text-gray-400 hover:text-white">全選択</button>
                <button @click="toggleAllInGroup(group, false)"
                        class="text-gray-400 hover:text-white">全解除</button>
              </div>
            </div>
            <table class="w-full text-xs">
              <thead>
                <tr class="text-gray-500 border-b border-gray-700">
                  <th class="text-left py-1 pr-2 w-8"></th>
                  <th class="text-left py-1 pr-2 w-12">ID</th>
                  <th class="text-left py-1 pr-2 w-24">date</th>
                  <th class="text-left py-1 pr-2 w-20">assignee</th>
                  <th class="text-left py-1">content</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="(it, idx) in group.items" :key="it.id"
                    :class="['border-b border-gray-700/50', group.selected.has(it.id) ? 'bg-red-900/30' : (idx === 0 ? 'bg-green-900/20' : '')]">
                  <td class="py-1 pr-2">
                    <input type="checkbox" :checked="group.selected.has(it.id)"
                           @change="toggleItem(group, it.id)"
                           class="rounded bg-gray-700 border-gray-600">
                  </td>
                  <td class="py-1 pr-2 font-mono text-gray-400">{{ it.id }}</td>
                  <td class="py-1 pr-2 text-gray-400">{{ (it.extracted_at || '').slice(0, 10) }}</td>
                  <td class="py-1 pr-2 text-gray-300">{{ it.assignee || '-' }}</td>
                  <td class="py-1 text-gray-200">{{ it.content }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </template>

        <!-- Decisions groups -->
        <template v-if="decisionGroups.length > 0">
          <h3 class="text-sm font-bold text-gray-300 mb-2 mt-6">📝 Decisions ({{ decisionFlagged }} flagged)</h3>
          <div v-for="(group, gi) in decisionGroups" :key="'dc-'+gi" class="admin-card mb-3">
            <div class="flex items-center justify-between mb-2">
              <div>
                <span :class="['inline-block px-2 py-0.5 rounded text-xs font-bold mr-2', categoryBadge(group.category)]">
                  {{ categoryLabel(group.category) }}
                </span>
                <span class="text-xs text-gray-500">{{ group.items.length }} 件</span>
              </div>
              <div class="flex gap-2 text-xs">
                <button @click="toggleAllInGroup(group, true)"
                        class="text-gray-400 hover:text-white">全選択</button>
                <button @click="toggleAllInGroup(group, false)"
                        class="text-gray-400 hover:text-white">全解除</button>
              </div>
            </div>
            <table class="w-full text-xs">
              <thead>
                <tr class="text-gray-500 border-b border-gray-700">
                  <th class="text-left py-1 pr-2 w-8"></th>
                  <th class="text-left py-1 pr-2 w-12">ID</th>
                  <th class="text-left py-1 pr-2 w-24">decided</th>
                  <th class="text-left py-1">content</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="(it, idx) in group.items" :key="it.id"
                    :class="['border-b border-gray-700/50', group.selected.has(it.id) ? 'bg-red-900/30' : (idx === 0 ? 'bg-green-900/20' : '')]">
                  <td class="py-1 pr-2">
                    <input type="checkbox" :checked="group.selected.has(it.id)"
                           @change="toggleItem(group, it.id)"
                           class="rounded bg-gray-700 border-gray-600">
                  </td>
                  <td class="py-1 pr-2 font-mono text-gray-400">{{ it.id }}</td>
                  <td class="py-1 pr-2 text-gray-400">{{ (it.decided_at || '').slice(0, 10) }}</td>
                  <td class="py-1 text-gray-200">{{ it.content }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </template>

        <div v-if="!hasResults && !scanning" class="text-center text-gray-500 italic text-sm py-6">
          検出結果はまだありません。「Run Detection」を押してください。
        </div>

      </div>
    `,
  });

  app.mount(container.querySelector('#quality-app'));
  return () => { app.unmount(); };
});
