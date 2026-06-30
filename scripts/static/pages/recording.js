/* ================================================================
   Recording Pipeline Page — Vue 3
   ================================================================ */

registerAdminPage('recording', async (container) => {
  container.innerHTML = `<div id="recording-app"></div>`;

  const app = Vue.createApp({
    data() {
      return {
        // Form state
        inputMode: 'upload',   // 'upload' | 'path'
        meetingName: '',
        heldAt: '',
        skipSeconds: 0,
        selectedFiles: [], // Array<File>
        serverPath: '',
        serverVttPath: '',
        submitting: false,
        submittedLabel: '',
        // Meeting names for datalist
        meetingNames: [],
        // History
        history: [],
        historyLoaded: false,
        logModalOpen: false,
        logModalTitle: '',
        logModalContent: '',
        logModalLoading: false,
      };
    },
    computed: {
      canSubmit() {
        if (this.submitting || !this.meetingName.trim() || !this.heldAt) return false;
        if (this.inputMode === 'path') return this.serverPath.trim().length > 0;
        return this.selectedFiles.length > 0;
      },
      fileSummary() {
        const files = this.selectedFiles;
        if (files.length === 0) return '';
        let audio = 0, vtt = 0, totalSize = 0;
        for (const f of files) {
          if (f.name.toLowerCase().endsWith('.vtt')) vtt++; else audio++;
          totalSize += f.size;
        }
        return `${files.length} files selected`
          + ` (${audio} audio, ${vtt} VTT, ${(totalSize / 1024 / 1024).toFixed(1)} MB total)`;
      },
      fileDetails() {
        return this.selectedFiles.map(f => {
          const isVtt = f.name.toLowerCase().endsWith('.vtt');
          return `${f.name} (${(f.size / 1024 / 1024).toFixed(1)} MB)` + (isVtt ? ' [VTT]' : '');
        }).join('\n');
      },
      hasFiles() {
        return this.selectedFiles.length > 0;
      },
    },
    methods: {
      async loadMeetingNames() {
        try {
          const data = await api('GET', '/admin/minutes/meetings');
          this.meetingNames = data.meetings || [];
        } catch (_) {}
      },
      async loadHistory() {
        try {
          const data = await api('GET', '/admin/jobs?kind=recording&limit=20');
          this.history = data.jobs || [];
          this.historyLoaded = true;
        } catch (_) {
          this.historyLoaded = true;
        }
      },
      onFileSelected(e) {
        for (const f of e.target.files) {
          this.selectedFiles.push(f);
        }
        e.target.value = '';
      },
      onDrop(e) {
        e.preventDefault();
        this.$refs.uploadZone.classList.remove('dragging');
        if (e.dataTransfer.files.length > 0) {
          for (const f of e.dataTransfer.files) {
            this.selectedFiles.push(f);
          }
        }
      },
      clearFiles() {
        this.selectedFiles = [];
      },
      async submitForm() {
        if (!this.canSubmit) return;
        this.submitting = true;

        if (this.inputMode === 'path') {
          this.submittedLabel = 'Starting...';
          try {
            const res = await fetch('/api/admin/recording/start', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({
                file_path: this.serverPath.trim(),
                meeting_name: this.meetingName.trim(),
                held_at: this.heldAt,
                skip_seconds: this.skipSeconds,
                vtt_path: this.serverVttPath.trim() || null,
              }),
            });
            const data = await res.json();
            if (!res.ok) {
              toast(data.error || 'Start failed', 'negative');
              this.submitting = false;
              this.submittedLabel = '';
              return;
            }
            toast('Pipeline started! Job: ' + data.job_id, 'positive');
            this.submittedLabel = 'Submitted';
            this.serverPath = '';
            this.serverVttPath = '';
            this.loadHistory();
            setTimeout(() => { this.submitting = false; this.submittedLabel = ''; }, 3000);
          } catch (e) {
            toast('Error: ' + e.message, 'negative');
            this.submitting = false;
            this.submittedLabel = '';
          }
          return;
        }

        this.submittedLabel = 'Uploading...';
        const formData = new FormData();
        for (const f of this.selectedFiles) {
          formData.append('files', f);
        }
        formData.append('meeting_name', this.meetingName.trim());
        formData.append('held_at', this.heldAt);
        formData.append('skip_seconds', String(this.skipSeconds));

        try {
          const res = await fetch('/api/admin/recording/upload', { method: 'POST', body: formData });
          const data = await res.json();
          if (!res.ok) {
            toast(data.error || 'Upload failed', 'negative');
            this.submitting = false;
            this.submittedLabel = '';
            return;
          }
          toast('Pipeline started! Job: ' + data.job_id, 'positive');
          this.submittedLabel = 'Submitted';
          this.clearFiles();
          this.loadHistory();
          setTimeout(() => {
            this.submitting = false;
            this.submittedLabel = '';
          }, 3000);
        } catch (e) {
          toast('Upload error: ' + e.message, 'negative');
          this.submitting = false;
          this.submittedLabel = '';
        }
      },
      async viewLog(job) {
        this.logModalTitle = `Job ${job.id} (${this.jobMeeting(job)})`;
        this.logModalContent = '';
        this.logModalOpen = true;
        this.logModalLoading = true;
        try {
          const data = await api('GET', `/admin/jobs/${job.id}/log?lines=200`);
          if (data.error) {
            this.logModalContent = data.error;
          } else {
            const lines = data.lines || [];
            this.logModalContent = lines.length === 0
              ? '(empty log)'
              : `<div class="text-xs text-gray-400 mb-1">${esc(data.file)} &mdash; ${data.total_lines} total lines</div><pre class="text-xs text-gray-300 leading-relaxed">${lines.map(l => esc(l)).join('')}</pre>`;
          }
        } catch (e) {
          this.logModalContent = 'Failed to load log: ' + e.message;
        } finally {
          this.logModalLoading = false;
        }
      },
      statusIcon(job) {
        if (job.status === 'success') return '✅';
        if (job.status === 'error') return '❌';
        if (job.status === 'running') return '🔄';
        return '⏳';
      },
      jobMeeting(job) {
        try {
          const p = JSON.parse(job.params_json || '{}');
          return p.meeting_name || '-';
        } catch (_) { return '-'; }
      },
    },
    mounted() {
      this.loadMeetingNames();
      this.loadHistory();
      // Poll history every 10s
      this._pollTimer = setInterval(() => this.loadHistory(), 10000);
    },
    unmounted() {
      if (this._pollTimer) clearInterval(this._pollTimer);
    },
    template: `
      <div class="max-w-4xl mx-auto">
        <h2 class="text-xl font-bold text-white mb-4">Recording Pipeline</h2>

        <!-- Upload Form -->
        <div class="admin-card mb-6">
          <h3 class="text-sm font-bold text-gray-300 mb-3">Recording Pipeline</h3>

          <!-- Mode tabs -->
          <div class="flex gap-2 mb-4">
            <button @click="inputMode='upload'"
                    class="px-3 py-1.5 rounded text-xs font-medium transition-colors"
                    :class="inputMode==='upload' ? 'bg-blue-600 text-white' : 'bg-gray-700 text-gray-400 hover:bg-gray-600'">
              📁 ファイルをアップロード
            </button>
            <button @click="inputMode='path'"
                    class="px-3 py-1.5 rounded text-xs font-medium transition-colors"
                    :class="inputMode==='path' ? 'bg-blue-600 text-white' : 'bg-gray-700 text-gray-400 hover:bg-gray-600'">
              🖥 サーバー上のパスを指定
            </button>
          </div>

          <!-- Upload zone -->
          <div v-if="inputMode==='upload'">
            <div ref="uploadZone"
                 class="upload-zone mb-4"
                 :class="{ 'has-files': hasFiles }"
                 @click="$refs.fileInput.click()"
                 @dragover.prevent="$refs.uploadZone.classList.add('dragging')"
                 @dragleave.prevent="$refs.uploadZone.classList.remove('dragging')"
                 @drop.prevent="onDrop">
              <div class="text-center">
                <div class="text-3xl mb-2 text-gray-500">📁</div>
                <p class="text-gray-400">Drag & drop files here or click to select</p>
                <p class="text-xs text-gray-600 mt-1">MP4, M4A, WAV, MP3 + VTT (optional)</p>
              </div>
              <input ref="fileInput" type="file"
                     accept=".mp4,.m4a,.wav,.mp3,.vtt" multiple class="hidden"
                     @change="onFileSelected">
            </div>
          </div>

          <!-- Server path zone -->
          <div v-else class="mb-4 space-y-2">
            <div>
              <label class="text-xs text-gray-400 block mb-1">音声ファイルのパス (サーバー上) *</label>
              <input type="text" v-model="serverPath"
                     placeholder="/lvs0/.../data/processing/2026-06-30_Meeting.mp4"
                     class="bg-gray-700 border border-gray-600 rounded px-3 py-2 text-xs w-full text-gray-200 font-mono">
            </div>
            <div>
              <label class="text-xs text-gray-400 block mb-1">VTT ファイルのパス（省略可）</label>
              <input type="text" v-model="serverVttPath"
                     placeholder="/lvs0/.../data/processing/2026-06-30_Meeting.vtt"
                     class="bg-gray-700 border border-gray-600 rounded px-3 py-2 text-xs w-full text-gray-200 font-mono">
            </div>
            <p class="text-xs text-gray-600">ファイルを SCP 等でサーバーに配置した後、フルパスを入力してください。</p>
          </div>

          <div class="grid grid-cols-1 md:grid-cols-2 gap-3 mb-3">
            <div>
              <label class="text-xs text-gray-400 block mb-1">Meeting Name *</label>
              <input type="text" v-model="meetingName" list="rec-meeting-list"
                     placeholder="e.g. Leader Meeting"
                     class="bg-gray-700 border border-gray-600 rounded px-3 py-2 text-sm w-full text-gray-200">
              <datalist id="rec-meeting-list">
                <option v-for="name in meetingNames" :key="name" :value="name"></option>
              </datalist>
            </div>
            <div>
              <label class="text-xs text-gray-400 block mb-1">Date (held_at) *</label>
              <input type="date" v-model="heldAt"
                     class="bg-gray-700 border border-gray-600 rounded px-3 py-2 text-sm w-full text-gray-200">
            </div>
          </div>
          <div class="flex items-center gap-4 mb-3">
            <div>
              <label class="text-xs text-gray-400 block mb-1">Skip first (seconds)</label>
              <input type="number" v-model.number="skipSeconds" min="0"
                     class="bg-gray-700 border border-gray-600 rounded px-3 py-2 text-sm w-28 text-gray-200">
            </div>
          </div>
          <div class="flex gap-2">
            <button @click="submitForm" :disabled="!canSubmit"
                    class="bg-blue-600 hover:bg-blue-700 text-white rounded px-5 py-2 text-sm font-medium"
                    :class="{ 'opacity-50 cursor-not-allowed': !canSubmit }">
              {{ submittedLabel || 'Submit Pipeline' }}
            </button>
            <button v-if="inputMode==='upload' && hasFiles" @click="clearFiles"
                    class="bg-gray-600 hover:bg-gray-500 text-white rounded px-3 py-2 text-sm font-medium">
              Clear
            </button>
            <div v-if="inputMode==='upload' && hasFiles" class="text-xs text-gray-500 self-center" :title="fileDetails">
              <span class="text-green-400 font-medium">{{ selectedFiles.length }} files selected</span>
              {{ fileSummary.slice(fileSummary.indexOf('(')) }}
            </div>
          </div>
        </div>

        <!-- Active Jobs -->
        <div class="admin-card mb-6">
          <h3 class="text-sm font-bold text-gray-300 mb-2">Active Jobs</h3>
          <div>
            <p v-if="!historyLoaded" class="text-gray-500 italic text-sm">Loading...</p>
            <p v-else-if="history.filter(j => j.status === 'queued' || j.status === 'running').length === 0"
               class="text-gray-500 italic text-sm">No active jobs</p>
            <table v-else class="w-full text-xs">
              <thead>
                <tr class="text-gray-500 border-b border-gray-700">
                  <th class="text-left py-1 pr-2">Job</th>
                  <th class="text-left py-1 pr-2">Meeting</th>
                  <th class="text-left py-1 pr-2">Status</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="j in history.filter(j => j.status === 'queued' || j.status === 'running')" :key="j.id"
                    class="border-b border-gray-700/50 hover:bg-gray-700/30">
                  <td class="py-1 pr-2 font-mono text-gray-400">{{ j.id }}</td>
                  <td class="py-1 pr-2 text-gray-200">{{ jobMeeting(j) }}</td>
                  <td class="py-1 pr-2"><span class="text-gray-400">{{ j.status }}</span></td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>

        <!-- History -->
        <div class="admin-card">
          <h3 class="text-sm font-bold text-gray-300 mb-2">History</h3>
          <div>
            <p v-if="!historyLoaded" class="text-gray-500 italic text-sm">Loading...</p>
            <p v-else-if="history.length === 0" class="text-gray-500 italic text-sm">No history</p>
            <table v-else class="w-full text-xs">
              <thead>
                <tr class="text-gray-500 border-b border-gray-700">
                  <th class="text-left py-1 pr-2">Job</th>
                  <th class="text-left py-1 pr-2">Meeting</th>
                  <th class="text-left py-1 pr-2">Status</th>
                  <th class="text-left py-1 pr-2">Duration</th>
                  <th class="text-left py-1">Actions</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="j in history" :key="j.id"
                    class="border-b border-gray-700/50 hover:bg-gray-700/30">
                  <td class="py-1 pr-2 font-mono text-gray-400">{{ j.id }}</td>
                  <td class="py-1 pr-2 text-gray-200">{{ jobMeeting(j) }}</td>
                  <td class="py-1 pr-2">{{ statusIcon(j) }} <span class="text-gray-400">{{ j.status }}</span></td>
                  <td class="py-1 pr-2 text-gray-400">{{ fmtDuration(j.started_at, j.finished_at) }}</td>
                  <td class="py-1">
                    <span class="text-blue-400 cursor-pointer hover:underline" @click="viewLog(j)">log</span>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      </div>

      <!-- Log Modal -->
      <Teleport to="body">
        <div v-if="logModalOpen" class="fixed inset-0 z-50 flex items-center justify-center p-4">
          <div class="absolute inset-0 bg-black/60" @click="logModalOpen = false"></div>
          <div class="relative rounded-lg shadow-xl p-0 w-[800px] max-h-[80vh] bg-gray-800 text-gray-200 flex flex-col">
            <div class="flex items-start justify-between p-4 border-b border-gray-700 flex-shrink-0">
              <h2 class="text-sm font-bold text-white">{{ logModalTitle }}</h2>
              <button type="button" @click="logModalOpen = false"
                      class="text-gray-400 hover:text-white text-xl leading-none">&times;</button>
            </div>
            <div class="p-4 overflow-y-auto" style="max-height: calc(80vh - 60px);">
              <p v-if="logModalLoading" class="text-gray-500 italic text-xs">Loading log...</p>
              <div v-else v-html="logModalContent" class="text-xs"></div>
            </div>
          </div>
        </div>
      </Teleport>
    `,
  });

  app.config.globalProperties.fmtDateTime = window.fmtDateTime || (v => v || '');
  app.config.globalProperties.esc = window.esc || (v => v || '');
  app.config.globalProperties.fmtDuration = window.fmtDuration || ((s, e) => s && e ? '...' : '');
  app.mount(container.querySelector('#recording-app'));
  return () => { app.unmount(); };
});
