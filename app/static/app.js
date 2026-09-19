/* study tools · 前端应用（Vue 3，免构建）
 * 三个视图：总览 home / 方向详情 detail / 统计回顾 stats
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

const STATUS_LABELS = { todo: "未开始", doing: "进行中", done: "已完成", skipped: "已跳过" };

// 番茄钟参数（V2 FR8）：25 分钟专注 / 5 分钟休息
const POMO_FOCUS = 25 * 60;
const POMO_BREAK = 5 * 60;
const PAGE_TITLE = "study tools · 个人学习管理";

createApp({
  data() {
    return {
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

      stats: null,
      review: null,
      reviewDate: todayStr(),

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
  },

  mounted() {
    this.loadDirections();
    window.addEventListener("resize", () => this.resizeCharts());
    // 暴露实例句柄，便于自动化测试与调试
    window.__app = this;
  },

  methods: {
    /* ---------- 基础 ---------- */
    async api(url, options = {}) {
      const opts = { headers: { "Content-Type": "application/json" }, ...options };
      if (opts.body && typeof opts.body !== "string") opts.body = JSON.stringify(opts.body);
      const res = await fetch(url, opts);
      let data = null;
      try { data = await res.json(); } catch (_) { /* 空响应 */ }
      if (!res.ok) {
        const msg = (data && data.error) || `请求失败（${res.status}）`;
        this.toast(msg);
        throw new Error(msg);
      }
      return data;
    },

    toast(msg) {
      this.toastMsg = msg;
      clearTimeout(this.toastTimer);
      this.toastTimer = setTimeout(() => (this.toastMsg = ""), 2600);
    },

    fmtMinutes,
    statusLabel(s) { return STATUS_LABELS[s] || s; },

    /* ---------- 总览 ---------- */
    async loadDirections() {
      this.loadingDirections = true;
      try {
        this.directions = await this.api("/api/directions");
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
    async openDetail(id) {
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
            formatter: (p) => `${p.value[0]}<br>学习 ${p.value[1] || 0} 分钟`,
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
        window.__weekly.setOption({
          grid: { left: 44, right: 12, top: 16, bottom: 26 },
          tooltip: { trigger: "axis" },
          xAxis: {
            type: "category",
            data: this.stats.weekly.map((w) => w.week_start.slice(5)),
          },
          yAxis: { type: "value", name: "分钟" },
          series: [{
            type: "bar", barMaxWidth: 26,
            itemStyle: { color: "#6366f1", borderRadius: [4, 4, 0, 0] },
            data: this.stats.weekly.map((w) => w.minutes),
          }],
        });
      }
    },

    resizeCharts() {
      if (window.__heatmap) window.__heatmap.resize();
      if (window.__weekly) window.__weekly.resize();
    },

    /* ---------- 导航 ---------- */
    goHome() {
      this.view = "home";
      this.loadDirections();
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
        try {
          await this.api(`/api/directions/${dirId}/logs`, {
            method: "POST",
            body: { date: todayStr(), minutes: 25, content: "🍅 番茄钟专注" },
          });
          if (this.currentId === dirId) await this.loadRecentLogs();
          this.toast("🍅 专注 25 分钟完成，已记入学习日志，休息一下吧");
        } catch (_) {
          this.toast("🍅 专注完成，但日志记录失败（目标方向可能已删除）");
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
      // 导入是整库替换，原先停用的详情/统计视图 id 可能已不存在，一并复位
      this.currentId = null;
      this.currentDirection = null;
      this.detail = { direction: null, phases: [] };
      this.stats = null;
      this.review = null;
      this.view = "home";
      this.toast(`导入成功：方向 ${res.counts.directions} 个、日志 ${res.counts.logs} 条`);
    },
  },
}).mount("#app");
