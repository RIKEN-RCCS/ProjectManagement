/* ================================================================
   Terminology Editor — 用語集編集
   ================================================================ */

registerAdminPage('terminology', async (container) => {
  container.innerHTML = `<div id="term-app" class="h-full flex flex-col p-4"></div>`;

  const app = Vue.createApp({
    data() {
      return {
        terms: [],
        grid: null,
        loading: false,
      };
    },

    methods: {
      async loadTerms() {
        this.loading = true;
        try {
          const data = await api('GET', '/terminology');
          this.terms = (data.rows || []).map(r => ({
            ...r,
            aliases: r.aliases ? (() => { try { return JSON.parse(r.aliases).join(', '); } catch { return r.aliases; } })() : '',
            meeting_kinds: r.meeting_kinds ? (() => { try { return JSON.parse(r.meeting_kinds).join(', '); } catch { return r.meeting_kinds; } })() : '',
            deleted: false,
          }));
          this.grid.setGridOption('rowData', this.terms);
        } catch (e) {
          toast('用語の読み込みに失敗しました', 'negative');
        } finally {
          this.loading = false;
        }
      },

      async saveTerms() {
        this.grid.stopEditing();
        const rows = [];
        this.grid.forEachNode(node => rows.push(node.data));

        const toSave = rows.map(r => ({
          term: r.term,
          category: r.category || 'unknown',
          aliases: r.aliases || '',
          source: r.source || 'manual',
          meeting_kinds: r.meeting_kinds || '',
          deleted: r.deleted || false,
        }));

        try {
          const res = await api('POST', '/terminology/save', { rows: toSave });
          toast(`${res.updated} 件を保存しました`, 'positive');
          await this.loadTerms();
        } catch (e) {
          toast('保存に失敗しました', 'negative');
        }
      },

      async addTerm() {
        const term = prompt('追加する用語を入力してください：');
        if (!term || !term.trim()) return;
        try {
          await api('POST', '/terminology/add', { content: term.trim() });
          toast(`「${term.trim()}」を追加しました`, 'positive');
          await this.loadTerms();
        } catch (e) {
          toast('追加に失敗しました', 'negative');
        }
      },

      bulkDelete() {
        const rows = [];
        this.grid.forEachNode(node => rows.push(node.data));
        const toDelete = rows.filter(r => r.deleted);
        if (toDelete.length === 0) {
          toast('削除チェックを入れた行がありません', 'info');
          return;
        }
        if (!confirm(`${toDelete.length} 件を削除しますか？`)) return;
        this.saveTerms();
      },
    },

    mounted() {
      const el = document.getElementById('term-grid');
      this.grid = agGrid.createGrid(el, {
        columnDefs: [
          { field: 'deleted', headerName: '削除', width: 55, pinned: 'left',
            cellRenderer: 'agCheckboxCellRenderer',
            cellEditor: 'agCheckboxCellEditor',
            cellRendererParams: { disabled: false } },
          { field: 'term', headerName: '用語', width: 200, pinned: 'left' },
          { field: 'category', headerName: 'カテゴリ', width: 100,
            cellEditor: 'agSelectCellEditor',
            cellEditorParams: { values: ['app', 'milestone', 'person', 'project', 'other', 'unknown'] } },
          { field: 'aliases', headerName: '別表記（カンマ区切り）', width: 300 },
          { field: 'source', headerName: '出典', editable: false, width: 100 },
          { field: 'frequency', headerName: '頻度', editable: false, width: 70 },
          { field: 'last_seen', headerName: '最終確認', editable: false, width: 170 },
          { field: 'meeting_kinds', headerName: '会議種別', width: 200 },
        ],
        defaultColDef: {
          editable: true, resizable: true, sortable: true, filter: true,
          wrapText: true, autoHeight: true,
        },
        domLayout: 'autoHeight',
        rowData: [],
        stopEditingWhenCellsLoseFocus: true,
        singleClickEdit: true,
      });
      this.loadTerms();
    },

    template: `
      <div class="flex items-center gap-3 mb-3 flex-none">
        <h2 class="text-lg font-bold text-gray-200">用語辞書</h2>
        <button @click="addTerm" class="bg-green-700 hover:bg-green-600 text-white px-3 py-1 rounded text-sm">
          ＋ 追加
        </button>
        <button @click="saveTerms" class="bg-blue-700 hover:bg-blue-600 text-white px-3 py-1 rounded text-sm">
          保存
        </button>
        <button @click="bulkDelete" class="bg-red-800 hover:bg-red-700 text-white px-3 py-1 rounded text-sm">
          削除実行
        </button>
        <span v-if="loading" class="text-gray-400 text-sm">読み込み中...</span>
        <span class="text-gray-500 text-sm ml-auto">{{ terms.length }} 件</span>
      </div>
      <div id="term-grid" class="ag-theme-quartz-dark flex-1 w-full" style="min-height:300px"></div>
    `,
  });

  app.mount(container.querySelector('#term-app'));
  return () => { app.unmount(); };
});
