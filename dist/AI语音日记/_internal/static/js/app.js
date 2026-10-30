(function () {
  const { createApp } = Vue;

  const MAX_SECONDS = 300;
  const CONV_KEY = 'ai-diary-conversations';
  const EMOJI = { 开心: '😊', 平静: '😌', 难过: '😢', 焦虑: '😟', 兴奋: '🤩', 疲惫: '😪', 生气: '😠' };
  const CITE_SPLIT = /(\[\d+\])/;

  const pad = (n) => String(n).padStart(2, '0');
  const isoDate = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;

  async function api(path, opts = {}) {
    const headers = { 'X-Diary-Client': '1', ...(opts.headers || {}) };
    if (opts.json !== undefined) {
      headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(opts.json);
    }
    const res = await fetch(path, { ...opts, headers });
    if (!res.ok) {
      let msg = `请求失败 (${res.status})`;
      try { const j = await res.json(); if (j.detail) msg = typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail); } catch (_) {}
      const err = new Error(msg);
      err.status = res.status;
      throw err;
    }
    return res;
  }
  const getJSON = async (path, opts) => (await api(path, opts)).json();

  function loadConvs() {
    try {
      const v = JSON.parse(localStorage.getItem(CONV_KEY) || '[]');
      return Array.isArray(v) ? v : [];
    } catch (_) { return []; }
  }

  createApp({
    data() {
      const now = new Date();
      return {
        view: 'diary',
        configured: false,
        status: {},
        form: { data_path: '', deepseek_key: '', dashscope_key: '' },
        testResult: null,
        busy: { pick: false, test: false, save: false, rebuild: false },
        indexCount: 0,
        toast: { text: '', type: '' },
        today: isoDate(now),
        maxSeconds: MAX_SECONDS,

        cal: { year: now.getFullYear(), month: now.getMonth() + 1, days: {} },
        selectedDate: isoDate(now),
        dayEntries: [],
        tags: [],
        search: { q: '', tag: '', start: '', end: '', active: false, results: [] },
        editing: { id: null, summary: '', tags: '' },
        rec: { state: 'idle', seconds: 0, level: 0 },
        lastResult: null,

        convs: loadConvs(),
        activeConvId: null,
        activeMsgIdx: null,
        activeSource: null,
        question: '',
        asking: false,
        sampleQuestions: ['我最近一周都做了什么?', '我上次心情不好是因为什么?', '我和谁一起吃过饭?'],
      };
    },

    computed: {
      calCells() {
        const { year, month, days } = this.cal;
        const first = new Date(year, month - 1, 1);
        const lead = (first.getDay() + 6) % 7;  // 周一为第一列
        const total = new Date(year, month, 0).getDate();
        const cells = Array(lead).fill(null);
        for (let d = 1; d <= total; d++) {
          cells.push({ day: d, date: `${year}-${pad(month)}-${pad(d)}`, count: days[d] || 0 });
        }
        return cells;
      },
      activeConv() {
        return this.convs.find((c) => c.id === this.activeConvId) || null;
      },
      activeMsg() {
        const c = this.activeConv;
        if (!c) return null;
        if (this.activeMsgIdx != null && c.messages[this.activeMsgIdx]) return c.messages[this.activeMsgIdx];
        for (let i = c.messages.length - 1; i >= 0; i--) if (c.messages[i].role === 'assistant') return c.messages[i];
        return null;
      },
    },

    watch: {
      view(v) {
        if (v === 'diary') this.refreshDiary();
        if (v === 'settings') this.loadSettings();
      },
    },

    async mounted() {
      window.addEventListener('hashchange', () => this.route());
      window.addEventListener('beforeunload', (e) => {
        if (this.rec.state !== 'idle') { e.preventDefault(); e.returnValue = ''; }
      });
      try {
        this.status = await getJSON('/api/config/status');
      } catch (e) {
        this.notify('无法连接本地服务: ' + e.message, 'error');
        return;
      }
      this.configured = this.status.configured;
      this.form.data_path = this.status.data_path || this.status.default_data_path || '';
      if (this.convs.length) this.activeConvId = this.convs[0].id;
      this.route();
    },

    methods: {
      // ---------- 通用 ----------
      route() {
        if (!this.configured) { this.view = 'setup'; return; }
        const v = (location.hash.match(/^#\/(\w+)/) || [])[1];
        const next = ['diary', 'qa', 'settings'].includes(v) ? v : 'diary';
        if (next === this.view && next === 'diary') this.refreshDiary();
        this.view = next;
      },
      notify(text, type = 'info', ms = 3500) {
        this.toast = { text, type };
        clearTimeout(this._toastTimer);
        this._toastTimer = setTimeout(() => { this.toast = { text: '', type: '' }; }, ms);
      },
      handleError(e) {
        if (e.status === 409) { this.configured = false; this.view = 'setup'; }
        this.notify(e.message, 'error', 6000);
      },
      emoji(e) { return EMOJI[e] || '📝'; },
      fmtSeconds(s) { s = Math.floor(s); return `${Math.floor(s / 60)}:${pad(s % 60)}`; },
      fmtDate(d) {
        const dt = new Date(d + 'T00:00:00');
        return `${dt.getFullYear()}年${dt.getMonth() + 1}月${dt.getDate()}日 星期${'日一二三四五六'[dt.getDay()]}`;
      },

      // ---------- 配置 ----------
      async pickFolder() {
        this.busy.pick = true;
        try {
          const r = await getJSON('/api/config/pick-folder', { method: 'POST' });
          if (r.path) this.form.data_path = r.path;
        } catch (e) { this.handleError(e); } finally { this.busy.pick = false; }
      },
      async testConfig() {
        this.busy.test = true;
        this.testResult = null;
        try {
          this.testResult = await getJSON('/api/config/test', { method: 'POST', json: {
            deepseek_key: this.form.deepseek_key, dashscope_key: this.form.dashscope_key } });
        } catch (e) { this.handleError(e); } finally { this.busy.test = false; }
      },
      async saveConfig() {
        this.busy.save = true;
        try {
          await getJSON('/api/config/setup', { method: 'POST', json: this.form });
          this.status = await getJSON('/api/config/status');
          this.configured = this.status.configured;
          this.form.deepseek_key = this.form.dashscope_key = '';
          this.notify('已保存', 'success');
          if (this.view === 'setup') { location.hash = '#/diary'; this.route(); }
        } catch (e) { this.handleError(e); } finally { this.busy.save = false; }
      },
      async loadSettings() {
        this.testResult = null;
        try {
          this.status = await getJSON('/api/config/status');
          this.form.data_path = this.status.data_path || '';
          this.indexCount = (await getJSON('/api/index/status')).count;
        } catch (e) { this.handleError(e); }
      },
      async rebuildIndex() {
        this.busy.rebuild = true;
        try {
          const r = await getJSON('/api/index/rebuild', { method: 'POST' });
          this.indexCount = r.total;
          this.notify(`同步完成: 新增/更新 ${r.added} 条, 移除 ${r.removed} 条`, 'success');
        } catch (e) { this.handleError(e); } finally { this.busy.rebuild = false; }
      },
      async openFolder() {
        try { await api('/api/open-folder', { method: 'POST' }); } catch (e) { this.handleError(e); }
      },

      // ---------- 日记 ----------
      async refreshDiary() {
        await Promise.all([this.loadCalendar(), this.loadDay(this.selectedDate), this.loadTags()]);
      },
      async loadCalendar() {
        try {
          const r = await getJSON(`/api/diary/calendar?year=${this.cal.year}&month=${this.cal.month}`);
          this.cal.days = r.days;
        } catch (e) { this.handleError(e); }
      },
      async loadDay(date) {
        try { this.dayEntries = (await getJSON(`/api/diary/day/${date}`)).entries; } catch (e) { this.handleError(e); }
      },
      async loadTags() {
        try { this.tags = (await getJSON('/api/diary/tags')).tags; } catch (_) {}
      },
      shiftMonth(delta) {
        let m = this.cal.month + delta, y = this.cal.year;
        if (m < 1) { m = 12; y--; } else if (m > 12) { m = 1; y++; }
        this.cal.year = y; this.cal.month = m; this.cal.days = {};
        this.loadCalendar();
      },
      selectDate(date) {
        this.selectedDate = date;
        this.editing.id = null;
        this.search.active = false;
        this.loadDay(date);
      },
      goToday() { this.jumpTo(this.today); },
      jumpTo(date) {
        const [y, m] = date.split('-').map(Number);
        const monthChanged = y !== this.cal.year || m !== this.cal.month;
        this.cal.year = y; this.cal.month = m;
        if (this.view !== 'diary') location.hash = '#/diary';
        else if (monthChanged) this.loadCalendar();
        this.selectDate(date);
      },
      async doSearch() {
        const p = new URLSearchParams();
        if (this.search.q.trim()) p.set('q', this.search.q.trim());
        if (this.search.tag) p.set('tag', this.search.tag);
        if (this.search.start) p.set('start', this.search.start);
        if (this.search.end) p.set('end', this.search.end);
        if (![...p.keys()].length) { this.clearSearch(); return; }
        try {
          this.search.results = (await getJSON('/api/diary/search?' + p)).entries;
          this.search.active = true;
        } catch (e) { this.handleError(e); }
      },
      clearSearch() {
        Object.assign(this.search, { q: '', tag: '', start: '', end: '', active: false, results: [] });
      },
      startEdit(e) {
        this.editing = { id: e.id, summary: e.summary, tags: e.tags.join(' ') };
      },
      async saveEdit(e) {
        const tags = this.editing.tags.split(/[\s,，]+/).filter(Boolean);
        try {
          const updated = await getJSON(`/api/diary/${e.date}/${e.id}`, { method: 'PUT',
            json: { summary: this.editing.summary, tags } });
          Object.assign(e, updated);
          this.editing.id = null;
          this.loadTags();
          this.notify('已保存', 'success');
        } catch (err) { this.handleError(err); }
      },
      async removeEntry(e) {
        if (!confirm(`确定删除 ${e.date} ${e.time} 的这条记录吗?删除后无法恢复。`)) return;
        try {
          await api(`/api/diary/${e.date}/${e.id}`, { method: 'DELETE' });
          this.dayEntries = this.dayEntries.filter((x) => x.id !== e.id);
          this.loadCalendar();
          this.loadTags();
        } catch (err) { this.handleError(err); }
      },
      printDay() { window.print(); },

      // ---------- 录音 ----------
      async toggleRecord() {
        if (this.rec.state === 'idle') return this.startRecord();
        if (this.rec.state === 'recording') return this.stopRecord();
      },
      async startRecord() {
        this._recorder = new WavRecorder();
        try {
          await this._recorder.start((lvl) => { this.rec.level = lvl; });
        } catch (e) {
          this._recorder.cancel();
          this.notify(e.message, 'error', 6000);
          return;
        }
        this.rec = { state: 'recording', seconds: 0, level: 0 };
        this._recTimer = setInterval(() => {
          this.rec.seconds = this._recorder.seconds;
          if (this.rec.seconds >= MAX_SECONDS) this.stopRecord();
        }, 200);
      },
      async stopRecord() {
        if (this.rec.state !== 'recording') return;
        clearInterval(this._recTimer);
        this.rec.state = 'processing';
        try {
          const { blob, seconds } = await this._recorder.stop();
          if (seconds < 0.8) throw new Error('录音太短了, 请至少说一句话');
          const fd = new FormData();
          fd.append('audio', blob, 'record.wav');
          const r = await getJSON('/api/diary/record', { method: 'POST', body: fd });
          this.lastResult = r;
          this.notify('日记已保存', 'success');
          this.jumpTo(r.entry.date);
          this.loadCalendar();
          this.loadTags();
        } catch (e) {
          this.handleError(e);
        } finally {
          this.rec = { state: 'idle', seconds: 0, level: 0 };
        }
      },

      // ---------- 问答 ----------
      saveConvs() {
        try { localStorage.setItem(CONV_KEY, JSON.stringify(this.convs.slice(0, 50))); } catch (_) {}
      },
      newConv() {
        const c = { id: Date.now().toString(36), title: '', messages: [] };
        this.convs.unshift(c);
        this.activeConvId = c.id;
        this.activeMsgIdx = null;
        this.saveConvs();
      },
      deleteConv(id) {
        if (!confirm('删除这个对话?')) return;
        this.convs = this.convs.filter((c) => c.id !== id);
        if (this.activeConvId === id) this.activeConvId = this.convs.length ? this.convs[0].id : null;
        this.activeMsgIdx = null;
        this.saveConvs();
      },
      onEnter(e) {
        if (e.shiftKey || e.isComposing || e.keyCode === 229) return;  // 中文输入法选词时的回车不发送
        e.preventDefault();
        this.ask();
      },
      segments(text) {
        return (text || '').split(CITE_SPLIT).filter(Boolean).map((s) => {
          const m = s.match(/^\[(\d+)\]$/);
          return m ? { cite: Number(m[1]) } : { text: s };
        });
      },
      showSource(msgIdx, n) {
        this.activeMsgIdx = msgIdx;
        this.activeSource = n;
        this.$nextTick(() => {
          const el = document.getElementById('src-' + n);
          if (el) el.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
        });
      },
      scrollChat() {
        this.$nextTick(() => { const el = this.$refs.messages; if (el) el.scrollTop = el.scrollHeight; });
      },
      stopAsk() {
        if (this._abort) this._abort.abort();
      },
      async ask(q) {
        const question = (q || this.question).trim();
        if (!question || this.asking) return;
        if (!this.activeConv) this.newConv();
        const conv = this.activeConv;
        const history = conv.messages.filter((m) => !m.error && m.content)
          .map((m) => ({ role: m.role, content: m.content })).slice(-6);
        if (!conv.title) conv.title = question.slice(0, 20);
        conv.messages.push({ role: 'user', content: question });
        conv.messages.push({ role: 'assistant', content: '', sources: [], cited: [], pending: true, error: '' });
        const msg = conv.messages[conv.messages.length - 1];  // 取响应式代理, 修改才能刷新界面
        this.activeMsgIdx = conv.messages.length - 1;
        this.activeSource = null;
        this.question = '';
        this.asking = true;
        this.scrollChat();

        this._abort = new AbortController();
        try {
          const res = await api('/api/qa/ask', { method: 'POST', json: { question, history }, signal: this._abort.signal });
          const reader = res.body.getReader();
          const decoder = new TextDecoder();
          let buf = '';
          for (;;) {
            const { value, done } = await reader.read();
            if (done) break;
            buf += decoder.decode(value, { stream: true });
            let idx;
            while ((idx = buf.indexOf('\n\n')) >= 0) {
              const frame = buf.slice(0, idx);
              buf = buf.slice(idx + 2);
              let event = 'message', data = '';
              for (const line of frame.split('\n')) {
                if (line.startsWith('event: ')) event = line.slice(7);
                else if (line.startsWith('data: ')) data += line.slice(6);
              }
              if (!data) continue;
              const payload = JSON.parse(data);
              if (event === 'sources') msg.sources = payload;
              else if (event === 'delta') { msg.content += payload; this.scrollChat(); }
              else if (event === 'done') msg.cited = payload.cited || [];
              else if (event === 'error') msg.error = '回答失败: ' + payload;
            }
          }
        } catch (e) {
          if (e.name === 'AbortError') msg.error = msg.content ? '' : '已停止';
          else if (e.status === 409) this.handleError(e);
          else msg.error = '回答失败: ' + e.message;
        } finally {
          msg.pending = false;
          this.asking = false;
          this._abort = null;
          this.saveConvs();
          this.scrollChat();
        }
      },
    },
  }).mount('#app');
})();
