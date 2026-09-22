/* study tools · 前端应用（Vue 3，免构建）
 * 四个视图：总览 home / 方向详情 detail / 统计回顾 stats / 复习卡片 review（V3）
 */
const { createApp } = Vue;

/* ---------- 通用工具 ---------- */

// 本地日期 YYYY-MM-DD（不用 toISOString，避免时区偏移）
function todayStr() {
  const d = new Date();
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

function addDays(dateStr, n) {
  const d = new Date(dateStr + "T00:00:00");
  d.setDate(d.getDate() + n);
  const p = (x) => String(x).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

function fmtMinutes(m) {
  if (!m || m <= 0) return "0 分钟";
  if (m < 60) return `${m} 分钟`;
  const h = Math.floor(m / 60), min = m % 60;
  return min ? `${h} 小时 ${min} 分` : `${h} 小时`;
}

// 到期时间（后端存 UTC ISO）→ 本地直觉文案。
// 分钟档必须先于小时档：FSRS 默认学习步是 1/10 分钟（Qoder 轮审缺陷 1——
// 原实现 9.5 分钟后的卡会显示「约 1 小时后」，误差 60 倍）。
function fmtDue(iso) {
  if (!iso) return "";
  const min = (new Date(iso) - Date.now()) / 60000;
  if (min <= 0) return "已到期";
  if (min < 60) return `约 ${Math.max(1, Math.round(min))} 分钟后`;
  if (min < 1440) return `约 ${Math.round(min / 60)} 小时后`;
  return `${Math.round(min / 1440)} 天后`;
}

const STATUS_LABELS = { todo: "未开始", doing: "进行中", done: "已完成", skipped: "已跳过" };

const COLLAPSED_KEY = "st_collapsed_phases";

function loadCollapsedPhases() {
  try {
    const v = JSON.parse(localStorage.getItem(COLLAPSED_KEY));
    return v && typeof v === "object" ? v : {};
  } catch (_) {
    return {};
  }
}

// fsrs v6 状态值：1 学习中 / 2 复习中 / 3 重学中（没有 New 态，新卡看 last_review）
const CARD_STATE_LABELS = { 1: "学习中", 2: "复习中", 3: "重学中" };

// 番茄钟参数（V2 FR8）：25 分钟专注 / 5 分钟休息
const POMO_FOCUS = 25 * 60;
const POMO_BREAK = 5 * 60;
const PAGE_TITLE = "study tools · 个人学习管理";

const app = createApp({
  data() {    return {
      view: "home",
      loadingDirections: false,
      seeding: false,
      toastMsg: "",
      toastTimer: null,

      directions: [],
      showNewDir: false,
      newDir: { name: "", description: "" },

      currentId: null,
      currentDirection: null,
      detail: { direction: null, phases: [] },
      recentLogs: [],
      newPhase: { name: "", goal: "" },
      newLog: { date: todayStr(), minutes: null, content: "" },

      // 阶段折叠（V4 FR10）：roadmap 每次全量重建，状态必须独立于 phase 对象生命周期；
      // localStorage 记忆跨会话，损坏 JSON 一律回退为全部展开（不弹错）
      collapsedPhases: loadCollapsedPhases(),

      stats: null,
      review: null,
      reviewDate: todayStr(),

      // 复习卡片（V3 FR9）
      reviewQueue: [],
      reviewCounts: null,
      reviewFlipped: false,
      cards: [],
      newCard: { front: "", back: "" },

      // 番茄钟（纯前端计时，专注结束自动写入学习日志）
      pomodoro: { mode: "focus", remaining: POMO_FOCUS, running: false },
      pomodoroCount: 0,
    };
  },

  computed: {
    pomoDisplay() {
      const m = Math.floor(this.pomodoro.remaining / 60);
      const s = this.pomodoro.remaining % 60;
      return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
    },

    // 队列首张 = 当前复习中的卡
    currentCard() {
      return this.reviewQueue[0] || null;
    },

    // 详情页「今日待复习」徽标：数据来自方向列表接口的聚合（docs/06 §5）
    dueToday() {
      const d = this.directions.find((x) => x.id === this.currentId);
      return d ? d.due_today : 0;
    },

    // 方向绑定视图（详情/统计）的可用性：已选中方向，或唯一方向可自动选中
    tabsLocked() {
      return !this.currentId && this.directions.length !== 1;
    },
  },

  mounted() {
    this.loadDirections();
    window.addEventListener("resize", () => this.resizeCharts());
    // 暴露实例句柄，便于自动化测试与调试
    window.__app = this;
  },

  methods: {
    /* ---------- 基础 ---------- */
    // 标记「已经 toast 过」的错误：errorHandler 据此降为 debug；
    // 未标记的是真实渲染/逻辑错误，必须留在 error 级别（Qoder 审计：全静默会掩盖新缺陷）
    _handledError(msg) {
      const e = new Error(msg);
      e.handled = true;
      return e;
    },

    async api(url, options = {}) {
      const opts = { headers: { "Content-Type": "application/json" }, ...options };
      if (opts.body && typeof opts.body !== "string") opts.body = JSON.stringify(opts.body);
      let res;
      try {
        res = await fetch(url, opts);
      } catch (_) {
        // 网络层失败（服务未启动 / 断网）：fetch 直接 reject，必须在这里 toast，
        // 否则整页静默无反馈（Qoder 审计：后端不可达时前端全静默）
        const msg = "无法连接服务器，请确认程序已启动（python app/main.py 或 run.bat）";
        this.toast(msg);
        throw this._handledError(msg);
      }
      let data = null;
      try { data = await res.json(); } catch (_) { /* 空响应 */ }
      if (!res.ok) {
        const msg = (data && data.error) || `请求失败（${res.status}）`;
        this.toast(msg);
        throw this._handledError(msg);
      }
      return data;
    },

    toast(msg) {
      this.toastMsg = msg;
      clearTimeout(this.toastTimer);
      this.toastTimer = setTimeout(() => (this.toastMsg = ""), 2600);
    },

    fmtMinutes,
    fmtDue,
    statusLabel(s) { return STATUS_LABELS[s] || s; },

    /* ---------- 总览 ---------- */
    async loadDirections() {
      this.loadingDirections = true;
      try {
        this.directions = await this.api("/api/directions");
        // currentId 悬空复位（Qoder 多标签边界）：A 标签页删了方向，B 标签页
        // 还选着它——界面内的 deleteDirection 自己会复位，但跨标签不会。
        // 检测到选中项已不存在就归零，导航回到诚实状态而不是对着 404 显示假数据。
        if (this.currentId && !this.directions.some((d) => d.id === this.currentId)) {
          this.currentId = null;
          this.currentDirection = null;
        }
      } finally {
        this.loadingDirections = false;
      }
    },

    async createDirection() {
      if (!this.newDir.name) { this.toast("请先填写方向名称"); return; }
      await this.api("/api/directions", { method: "POST", body: this.newDir });
      this.newDir = { name: "", description: "" };
      this.showNewDir = false;
      await this.loadDirections();
      this.toast("方向已创建");
    },

    async deleteDirection(d) {
      if (!confirm(`确定删除方向「${d.name}」吗？\n其阶段、任务与学习日志会一并删除，且无法恢复。`)) return;
      await this.api(`/api/directions/${d.id}`, { method: "DELETE" });
      if (this.currentId === d.id) { this.currentId = null; this.currentDirection = null; }
      await this.loadDirections();
      this.toast("已删除");
    },

    async loadDemo() {
      this.seeding = true;
      try {
        await this.api("/api/demo", { method: "POST" });
        await this.loadDirections();
        this.toast("示例数据已载入");
      } finally {
        this.seeding = false;
      }
    },

    /* ---------- 方向详情 ---------- */
    // 单一方向时所有方向绑定视图共用自动选中（Anki 式直觉）——此前只有「复习」
    // 有这个逻辑，导致新开页面时「方向详情」「统计回顾」灰着、必须先绕道「复习」
    // （用户实测报告的缺陷）；多个方向时仍由点卡片/视图内下拉选择
    _ensureCurrentId() {
      if (!this.currentId && this.directions.length === 1) {
        this.currentId = this.directions[0].id;
        this.currentDirection = this.directions[0];
      }
      return this.currentId;
    },

    async openDetail(id) {
      id = id || this._ensureCurrentId();
      if (!id) return;
      this.currentId = id;
      this.view = "detail";
      // 番茄钟不随切换方向重置：它绑定的是「开始时」的方向（见 pomoToggle）
      await Promise.all([this.loadRoadmap(), this.loadRecentLogs()]);
    },

    async loadRoadmap() {
      this.detail = await this.api(`/api/directions/${this.currentId}/roadmap`);
      this.currentDirection = this.detail.direction;
    },

    async loadRecentLogs() {
      // 侧栏只展示最近记录，避免方向日志变多后全量拉取
      this.recentLogs = await this.api(`/api/directions/${this.currentId}/logs?limit=30`);
    },

    async addPhase() {
      if (!this.newPhase.name) { this.toast("请先填写阶段名称"); return; }
      await this.api(`/api/directions/${this.currentId}/phases`, { method: "POST", body: this.newPhase });
      this.newPhase = { name: "", goal: "" };
      await this.loadRoadmap();
    },

    async editPhase(p) {
      const name = prompt("阶段名称：", p.name);
      if (name === null) return;
      const goal = prompt("阶段目标（可留空）：", p.goal || "");
      if (goal === null) return;
      await this.api(`/api/phases/${p.id}`, { method: "PATCH", body: { name, goal } });
      await this.loadRoadmap();
    },

    async deletePhase(p) {
      if (!confirm(`删除阶段「${p.name}」及其全部任务？`)) return;
      await this.api(`/api/phases/${p.id}`, { method: "DELETE" });
      await this.loadRoadmap();
    },

    // 阶段折叠（V4 FR10）：响应式副本 + localStorage 双写；展开为默认态
    isCollapsed(p) {
      return !!this.collapsedPhases[p.id];
    },

    togglePhase(p) {
      if (this.collapsedPhases[p.id]) delete this.collapsedPhases[p.id];
      else this.collapsedPhases[p.id] = true;
      try {
        localStorage.setItem(COLLAPSED_KEY, JSON.stringify(this.collapsedPhases));
      } catch (_) {
        // 隐私模式/配额满：持久化失败降级为仅本次会话（内存状态已正确），
        // 不让这里冒未捕获错误（Qoder nit）
      }
    },

    async addTask(p) {
      if (!p._addTitle) { this.toast("请先填写任务标题"); return; }
      await this.api(`/api/phases/${p.id}/tasks`, { method: "POST", body: { title: p._addTitle } });
      p._addTitle = "";
      await this.loadRoadmap();
    },

    // 点击状态圆点：未开始 → 进行中 → 已完成 → 跳过 → 未开始（四态循环）
    async cycleTask(t) {
      const next = { todo: "doing", doing: "done", done: "skipped", skipped: "todo" }[t.status];
      await this.api(`/api/tasks/${t.id}`, { method: "PATCH", body: { status: next } });
      await this.loadRoadmap();
    },

    /* ---------- 排序与移动（V2 FR6） ---------- */
    async reorderPhase(p, direction) {
      await this.api(`/api/phases/${p.id}/reorder`, { method: "POST", body: { direction } });
      await this.loadRoadmap();
    },

    async reorderTask(t, direction) {
      await this.api(`/api/tasks/${t.id}/reorder`, { method: "POST", body: { direction } });
      await this.loadRoadmap();
    },

    async moveTask(t, ev) {
      const phaseId = Number(ev.target.value);
      if (!phaseId) return;
      await this.api(`/api/tasks/${t.id}/move`, { method: "POST", body: { phase_id: phaseId } });
      await this.loadRoadmap();
      this.toast("任务已移动");
    },

    async editTask(t) {
      const title = prompt("任务标题：", t.title);
      if (title === null) return;
      const note = prompt("任务备注（可留空）：", t.note || "");
      if (note === null) return;
      await this.api(`/api/tasks/${t.id}`, { method: "PATCH", body: { title, note } });
      await this.loadRoadmap();
    },

    async deleteTask(t) {
      if (!confirm(`删除任务「${t.title}」？`)) return;
      await this.api(`/api/tasks/${t.id}`, { method: "DELETE" });
      await this.loadRoadmap();
    },

    async addLog() {
      const minutes = this.newLog.minutes || 0;
      const content = this.newLog.content || "";
      if (!minutes && !content) { this.toast("时长和内容至少填一项"); return; }
      await this.api(`/api/directions/${this.currentId}/logs`, {
        method: "POST",
        body: { date: this.newLog.date, minutes, content },
      });
      this.newLog.minutes = null;
      this.newLog.content = "";
      await Promise.all([this.loadRoadmap(), this.loadRecentLogs()]);
      this.toast("已记录 ✓");
    },

    async deleteLog(l) {
      if (!confirm("删除这条记录？")) return;
      await this.api(`/api/logs/${l.id}`, { method: "DELETE" });
      await Promise.all([this.loadRoadmap(), this.loadRecentLogs()]);
    },

    /* ---------- 统计回顾 ---------- */
    async openStats(id) {
      id = id || this._ensureCurrentId();
      if (!id) return;
      this.currentId = id;
      this.view = "stats";
      this.reviewDate = todayStr();
      await Promise.all([this.loadStats(), this.loadReview()]);
    },

    async loadStats() {
      this.stats = await this.api(`/api/directions/${this.currentId}/stats`);
      await this.$nextTick();
      this.renderCharts();
    },

    async loadReview() {
      this.review = await this.api(
        `/api/directions/${this.currentId}/review?date=${this.reviewDate}`);
    },

    shiftWeek(n) {
      this.reviewDate = addDays(this.reviewDate, n * 7);
      this.loadReview();
    },

    renderCharts() {
      if (!this.stats) return;

      // 统计视图是 v-if 挂载的：切换视图后 DOM 会被重建，缓存的旧 ECharts 实例
      // 指向已卸载的节点导致图表空白（审查缺陷 A）。因此每次先校验实例的 DOM，
      // 不一致则 dispose 后重新 init。
      const heatEl = this.$refs.heatmapEl;
      if (heatEl) {
        if (!window.__heatmap || window.__heatmap.getDom() !== heatEl) {
          if (window.__heatmap) window.__heatmap.dispose();
          window.__heatmap = echarts.init(heatEl);
        }
        const end = todayStr();
        const start = addDays(end, -364);
        const maxMin = Math.max(60, ...this.stats.heatmap.map((x) => x.minutes));
        window.__heatmap.setOption({
          tooltip: {
            // 时长统一人性化口径（V4 FR11）：原始分钟 → 「X 小时 Y 分」
            formatter: (p) => `${p.value[0]}<br>学习 ${fmtMinutes(p.value[1] || 0)}`,
          },
          visualMap: {
            min: 0, max: maxMin, show: false,
            inRange: { color: ["#e0e7ff", "#818cf8", "#4f46e5"] },
          },
          calendar: {
            range: [start, end],
            cellSize: ["auto", 14],
            left: 40, right: 12, top: 24,
            itemStyle: { color: "#f1f2f4", borderColor: "#fff", borderWidth: 2 },
            splitLine: { show: false },
            dayLabel: { nameMap: "ZH", fontSize: 10 },
            monthLabel: { nameMap: "ZH", fontSize: 10 },
            yearLabel: { show: false },
          },
          series: [{
            type: "heatmap", coordinateSystem: "calendar",
            data: this.stats.heatmap.filter((x) => x.minutes > 0)
              .map((x) => [x.date, x.minutes]),
          }],
        });
      }

      // 周时长条形图
      const weekEl = this.$refs.weeklyEl;
      if (weekEl) {
        if (!window.__weekly || window.__weekly.getDom() !== weekEl) {
          if (window.__weekly) window.__weekly.dispose();
          window.__weekly = echarts.init(weekEl);
        }
        // 周时长条形图（V4 FR11）：统一小时口径——柱高/Y 轴用小时（1 位小数），
        // 但 tooltip 必须从原始分钟取值：小时值经过 toFixed(1) 舍入，反推分钟
        // 会失真（20 分钟 → 0.3h → 显示 18 分钟），视觉近似、标注必须精确
        window.__weekly.setOption({
          grid: { left: 44, right: 12, top: 16, bottom: 26 },
          tooltip: {
            trigger: "axis",
            formatter: (ps) => {
              const w = this.stats.weekly[ps[0].dataIndex];
              return `${ps[0].name}<br>${fmtMinutes(w ? w.minutes : 0)}`;
            },
          },
          xAxis: {
            type: "category",
            data: this.stats.weekly.map((w) => w.week_start.slice(5)),
          },
          yAxis: { type: "value", name: "小时" },
          series: [{
            type: "bar", barMaxWidth: 26,
            itemStyle: { color: "#6366f1", borderRadius: [4, 4, 0, 0] },
            data: this.stats.weekly.map((w) => +(w.minutes / 60).toFixed(1)),
          }],
        });
      }
    },

    resizeCharts() {
      if (window.__heatmap) window.__heatmap.resize();
      if (window.__weekly) window.__weekly.resize();
    },

    /* ---------- 复习卡片（V3 FR9） ---------- */
    async loadReviewAll() {
      this.currentDirection = this.directions.find((d) => d.id === this.currentId) || null;
      await Promise.all([this.loadCards(), this.loadQueue()]);
    },

    async loadCards() {
      this.cards = await this.api(`/api/directions/${this.currentId}/cards`);
    },

    async loadQueue() {
      const data = await this.api(`/api/directions/${this.currentId}/review/queue`);
      this.reviewQueue = data.queue;
      this.reviewCounts = data.counts;
      this.reviewFlipped = false;
    },

    flipCard() {
      this.reviewFlipped = true;
    },

    // 四级评分（FR9.3）：忘记=1 / 困难=2 / 良好=3 / 简单=4
    async rateCard(rating) {
      const card = this.currentCard;
      if (!card) return;
      try {
        await this.api(`/api/cards/${card.id}/review`, { method: "POST", body: { rating } });
      } catch (_) {
        // 评分被拒（如并发标签页先一步把新卡配额用光）时也必须刷新队列——
        // 否则屏幕上留着一张永远评不动的旧卡（Qoder 并发实测）；失败原因
        // api() 已 toast。reviewFlipped 在 loadQueue 内复位，不会露出反面
        await this.loadQueue();
        return;
      }
      // 队列/计数/卡片列表刷新；方向列表一起刷，详情页徽标保持同步
      await Promise.all([this.loadQueue(), this.loadCards(), this.loadDirections()]);
    },

    async addCard() {
      if (!this.newCard.front) { this.toast("请先填写卡片正面"); return; }
      await this.api(`/api/directions/${this.currentId}/cards`, {
        method: "POST", body: this.newCard,
      });
      this.newCard = { front: "", back: "" };
      await this.loadReviewAll();
      this.toast("卡片已创建");
    },

    async editCard(c) {
      const front = prompt("卡片正面：", c.front);
      if (front === null) return;
      const back = prompt("卡片背面（可留空）：", c.back || "");
      if (back === null) return;
      await this.api(`/api/cards/${c.id}`, { method: "PATCH", body: { front, back } });
      await this.loadCards();
    },

    async deleteCard(c) {
      if (!confirm(`删除卡片「${c.front}」及其复习记录？`)) return;
      await this.api(`/api/cards/${c.id}`, { method: "DELETE" });
      await this.loadReviewAll();
    },

    cardState(c) {
      if (c.corrupt) return "⚠ 状态损坏";
      if (c.fsrs && c.fsrs.last_review === null) return "新卡";
      return CARD_STATE_LABELS[c.state] || "—";
    },

    // 新卡的 due=创建时刻，字面显示「已到期」会误导；实际含义是今天就能学。
    // 损坏卡（corrupt）提示删除重建 —— 删除按钮就在同一行，这是唯一的自救入口。
    cardDue(c) {
      if (c.corrupt) return "请删除重建";
      if (c.fsrs && c.fsrs.last_review === null) return "今日可学";
      return fmtDue(c.due);
    },

    /* ---------- 导航 ---------- */
    goHome() {
      this.view = "home";
      this.loadDirections();
    },

    goReview() {
      this.view = "review";
      if (this._ensureCurrentId()) this.loadReviewAll();
    },

    switchReviewDir(ev) {
      const id = Number(ev.target.value);
      if (!id || id === this.currentId) return;
      this.currentId = id;
      this.loadReviewAll();
    },

    /* ---------- 番茄钟（V2 FR8） ---------- */
    // 计时采用 endTime 反算剩余秒数（审查修复）：后台标签页的 setInterval
    // 会被浏览器节流到约 1 次/分钟，逐秒 -1 会严重偏慢；用绝对时间戳反算则不受影响。
    // 开始时绑定 direction_id，专注期间切换方向也不会把日志记错方向。
    pomoToggle() {
      if (this.pomodoro.running) {
        this.pomoStop();
        return;
      }
      this._pomoDirectionId = this.currentId;
      this._pomoEndAt = Date.now() + this.pomodoro.remaining * 1000;
      this.pomodoro.running = true;
      this._pomoTimer = setInterval(() => this.pomoTick(), 1000);
      this.pomoTitle();
    },

    pomoStop() {
      if (this._pomoTimer) clearInterval(this._pomoTimer);
      this._pomoTimer = null;
      this.pomodoro.running = false;
      document.title = PAGE_TITLE;
    },

    pomoReset() {
      this.pomoStop();
      this.pomodoro.mode = "focus";
      this.pomodoro.remaining = POMO_FOCUS;
    },

    pomoTick() {
      this.pomodoro.remaining = Math.max(0, Math.round((this._pomoEndAt - Date.now()) / 1000));
      this.pomoTitle();
      if (this.pomodoro.remaining <= 0) this.pomoFinish();
    },

    pomoTitle() {
      document.title = this.pomodoro.running
        ? `${this.pomoDisplay} ${this.pomodoro.mode === "focus" ? "🍅" : "☕"} study tools`
        : PAGE_TITLE;
    },

    async pomoFinish() {
      this.pomoStop();
      if (this.pomodoro.mode === "focus") {
        // 专注结束：自动记入「开始时所在方向」的学习日志
        this.pomodoroCount += 1;
        const dirId = this._pomoDirectionId || this.currentId;
        const focusMinutes = POMO_FOCUS / 60;   // TD1：不再硬编码 25，跟随 POMO_FOCUS
        try {
          await this.api(`/api/directions/${dirId}/logs`, {
            method: "POST",
            body: { date: todayStr(), minutes: focusMinutes, content: "🍅 番茄钟专注" },
          });
          if (this.currentId === dirId) await this.loadRecentLogs();
          this.toast(`🍅 专注 ${focusMinutes} 分钟完成，已记入学习日志，休息一下吧`);
        } catch (_) {
          // 失败的具体原因 this.api 已 toast（如：目标方向已删除、时长非整数）
          this.toast("🍅 专注完成，但日志记录失败");
        }
        this.pomodoro.mode = "break";
        this.pomodoro.remaining = POMO_BREAK;
      } else {
        this.toast("☕ 休息结束，准备下一个番茄吧");
        this.pomodoro.mode = "focus";
        this.pomodoro.remaining = POMO_FOCUS;
      }
    },

    /* ---------- 数据导入（V2 FR7） ---------- */
    pickImport() {
      this.$refs.importInput.click();
    },

    async doImport(ev) {
      const file = ev.target.files[0];
      ev.target.value = "";  // 允许重复选择同一文件
      if (!file) return;
      let data;
      try {
        data = JSON.parse(await file.text());
      } catch (_) {
        this.toast("文件不是有效的 JSON");
        return;
      }
      // JSON.parse 合法但非对象（null / 数字 / 字符串 / 数组）时，下面 data[k]
      // 会抛 TypeError 让导入静默死掉——先挡掉（V4 用户审查 #6）
      if (!data || typeof data !== "object" || Array.isArray(data)) {
        this.toast("文件不是有效的备份 JSON");
        return;
      }
      const n = (k) => Array.isArray(data[k]) ? data[k].length : 0;
      if (!confirm(
        `导入将【覆盖】当前全部数据，替换为备份中的内容：\n\n` +
        `方向 ${n("directions")} 个 · 阶段 ${n("phases")} 个 · 任务 ${n("tasks")} 个 · 日志 ${n("logs")} 条\n\n` +
        `此操作无法撤销，确定继续吗？`)) return;
      let res;
      try {
        res = await this.api("/api/import", { method: "POST", body: data });
      } catch (_) {
        return; // 失败原因 this.api 已经 toast 过，这里只负责不再抛未捕获的 rejection
      }
      await this.loadDirections();
      // 导入是整库替换，原先停用的详情/统计/复习视图 id 可能已不存在，一并复位
      this.currentId = null;
      this.currentDirection = null;
      this.detail = { direction: null, phases: [] };
      this.stats = null;
      this.review = null;
      this.reviewQueue = [];
      this.reviewCounts = null;
      this.cards = [];
      // 折叠表按 phase id 索引，导入会重新分配全部 id，不复位就张冠李戴
      // （Qoder 实测：折叠阶段 id 72 → 导入后 72 撞上另一阶段 → 莫名其妙处于折叠态）
      this.collapsedPhases = {};
      try {
        localStorage.removeItem(COLLAPSED_KEY);
      } catch (_) {
        // 隐私模式极端场景：清除失败不影响内存态已复位（用户审查 #6 顺带项）
      }
      this.view = "home";
      this.toast(`导入成功：方向 ${res.counts.directions} 个、日志 ${res.counts.logs} 条`);
    },
  },
});

// api() 失败时已把可读原因 toast 给用户，方法层抛出的同一错误再进控制台只会
// 变成 unhandled rejection 噪音（Qoder 轮审次要项；minutes 严格化后手滑输
// 小数即可触发）。只对「已 toast 过」的 handled 错误降为 debug —— 未标记的
// 是真实渲染/逻辑错误，必须保持 error 级别，全静默会掩盖新缺陷。
app.config.errorHandler = (err) => {
  if (err && err.handled) {
    console.debug("[study tools] 已由 toast 呈现的错误：", err.message);
  } else {
    console.error("[study tools] 未处理的错误：", err);
  }
};

// errorHandler 只覆盖 Vue 亲自调度的上下文；mounted / goHome / goReview /
// switchReviewDir / shiftWeek 等处「裸调 async 方法」的 rejection 会逃到
// window（Qoder 实测死后端场景下 goHome/shiftWeek 各留一条 unhandled
// rejection）。与其在 6 个调用点各补 .catch(() => {})，不如收模式：window
// 层只拦「已 toast 过」（handled 标记）并 preventDefault；未标记的真实错误
// 照常上报，不被吞。
window.addEventListener("unhandledrejection", (ev) => {
  if (ev.reason && ev.reason.handled) {
    ev.preventDefault();
    console.debug("[study tools] 已由 toast 呈现的错误：", ev.reason && ev.reason.message);
  }
});

app.mount("#app");
