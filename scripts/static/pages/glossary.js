/* ================================================================
   Glossary Editor — 構造化テキスト編集（コデザイン項目・ルール・リファレンス等）
   ================================================================ */

registerAdminPage('glossary', async (container) => {
  container.innerHTML = `<div id="glossary-app" class="h-full flex flex-col p-4"></div>`;

  const app = Vue.createApp({
    data() {
      return {
        items: [],
        grid: null,
        loading: false,
      };
    },

    methods: {
      async loadItems() {
        this.loading = true;
        try {
          const data = await api('GET', '/glossary');
          this.items = (data.rows || []).map(r => ({
            ...r,
            deleted: false,
          }));
          this.grid.setGridOption('rowData', this.items);
        } catch (e) {
          toast('用語集の読み込みに失敗しました', 'negative');
        } finally {
          this.loading = false;
        }
      },

      async saveItems() {
        this.grid.stopEditing();
        const rows = [];
        this.grid.forEachNode(node => rows.push(node.data));

        const toSave = rows.map(r => ({
          id: r.id,
          title: r.title || '',
          content: r.content || '',
          category: r.category || '',
          deleted: r.deleted || false,
        }));

        try {
          const res = await api('POST', '/glossary/save', { rows: toSave });
          toast(`${res.updated} 件を保存しました`, 'positive');
          await this.loadItems();
        } catch (e) {
          toast('保存に失敗しました', 'negative');
        }
      },

      async addItem() {
        const title = prompt('追加する項目名を入力してください：');
        if (!title || !title.trim()) return;
        try {
          await api('POST', '/glossary/add', { title: title.trim(), content: '', category: '' });
          toast(`「${title.trim()}」を追加しました`, 'positive');
          await this.loadItems();
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
        this.saveItems();
      },
    },

    mounted() {
      const el = document.getElementById('glossary-grid');
      this.grid = agGrid.createGrid(el, {
        columnDefs: [
          { field: 'deleted', headerName: '削除', width: 55, pinned: 'left',
            cellRenderer: 'agCheckboxCellRenderer',
            cellEditor: 'agCheckboxCellEditor',
            cellRendererParams: { disabled: false } },
          { field: 'id', headerName: 'ID', width: 60, editable: false, pinned: 'left' },
          { field: 'title', headerName: '項目名', width: 200 },
          { field: 'content', headerName: '内容（Markdown）', width: 500,
            cellEditor: 'agLargeTextCellEditor',
            cellEditorPopup: true,
            cellEditorParams: { maxLength: 10000, rows: 10, cols: 60 } },
          { field: 'category', headerName: 'カテゴリ', width: 120,
            cellEditor: 'agSelectCellEditor',
            cellEditorParams: { values: ['codesign', 'rule', 'reference', 'memo', 'other', ''] } },
          { field: 'updated_at', headerName: '更新日', width: 170, editable: false },
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
      this.loadItems();
    },

    template: `
      <div class="flex items-center gap-3 mb-3 flex-none">
        <h2 class="text-lg font-bold text-gray-200">プロジェクト用語集 (glossary)</h2>
        <button @click="addItem" class="bg-green-700 hover:bg-green-600 text-white px-3 py-1 rounded text-sm">
          ＋ 追加
        </button>
        <button @click="saveItems" class="bg-blue-700 hover:bg-blue-600 text-white px-3 py-1 rounded text-sm">
          保存
        </button>
        <button @click="bulkDelete" class="bg-red-800 hover:bg-red-700 text-white px-3 py-1 rounded text-sm">
          削除実行
        </button>
        <span v-if="loading" class="text-gray-400 text-sm">読み込み中...</span>
        <span class="text-gray-500 text-sm ml-auto">{{ items.length }} 件</span>
      </div>
      <div id="glossary-grid" class="ag-theme-quartz-dark flex-1 w-full" style="min-height:300px"></div>
    `,
  });

  app.mount(container.querySelector('#glossary-app'));
  return () => { app.unmount(); };
});
