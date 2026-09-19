"""study tools · API 回归冒烟测试（见 docs/05-协作约定.md 第 3 节）

两种运行模式：
  1) 默认（推荐，零副作用）：python tests/api_smoke.py
     在系统临时目录建一份独立 SQLite 库，用 Flask test_client 直接调用，
     不需要先启动服务，也不会碰到 app/data/study.db。
  2) 打真实服务：STUDY_TOOLS_URL=http://127.0.0.1:5000 python tests/api_smoke.py
     此模式下所有写操作都收敛在自建方向「__QA_临时方向__」内，结束前删除；
     示例数据相关的验收项（依赖空库）会自动跳过。

退出码：0 = 全部通过（已知问题只告警不算失败），1 = 有失败。
"""
import json
import os
import shutil
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import date, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.join(os.path.dirname(HERE), "app")
sys.path.insert(0, APP_DIR)

QA_URL = os.environ.get("STUDY_TOOLS_URL", "").rstrip("/")
QA_DIR = "__QA_临时方向__"

failures = []
warnings = []


# ---------- 调用后端：test_client 或 HTTP 两种实现统一成 call(method, path, body) ----------

def _make_test_client():
    import database
    tmp = tempfile.mkdtemp(prefix="study_tools_qa_")
    database.DB_PATH = os.path.join(tmp, "study.db")
    database.DATA_DIR = tmp
    import main
    main.init_db()
    return main.app.test_client(), tmp


_client = None
_tmpdir = None
if not QA_URL:
    _client, _tmpdir = _make_test_client()


def call(method, path, body=None):
    """返回 (status, parsed_json_or_None)。不发 expect，由调用方断言。"""
    if _client is not None:
        kw = {}
        if body is not None:
            kw["json"] = body
        resp = _client.open(path, method=method, **kw)
        raw = resp.get_data()
        try:
            payload = json.loads(raw) if raw else None
        except Exception:
            payload = None
        return resp.status_code, payload
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(QA_URL + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw) if raw else None
        except Exception:
            return e.code, None
    try:
        return r.status, json.loads(raw) if raw else None
    except Exception:
        return r.status, None


def get(path):
    return call("GET", path)


def raw_get(path):
    """静态资源等非 JSON 请求，只关心状态码与字节数。"""
    if _client is not None:
        resp = _client.get(path)
        return resp.status_code, len(resp.get_data())
    with urllib.request.urlopen(QA_URL + path, timeout=10) as r:
        return r.status, len(r.read())


# ---------- 断言工具 ----------

def check(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   → " + str(extra)))
    if not cond:
        failures.append(name)
    return cond


def known(name, cond, detail=""):
    """已知问题：不满足只告警，不算失败（修好后请升级为 check）。"""
    if not cond:
        warnings.append(f"{name}：{detail}")
        print(f"WARN  {name} → {detail}")


def status_of(direction_id):
    _, dirs = get("/api/directions")
    return next((d for d in dirs if d["id"] == direction_id), None)


# ---------- 1. 只读接口连通性 ----------

print("\n[1] 只读接口")
s, dirs = get("/api/directions")
check("GET /api/directions → 200 且为列表", s == 200 and isinstance(dirs, list), s)
s, _ = get("/api/directions/999999/roadmap")
check("不存在的方向 → 404", s == 404, s)
s, _ = get("/api/directions/abc/roadmap")
check("非法 id → 4xx", 400 <= s < 500, s)

# ---------- 2. 临时方向：全链路 CRUD（不依赖示例数据，真实库也安全） ----------

print("\n[2] 方向 → 阶段 → 任务 → 日志 全链路（临时方向）")
_, leftover = get("/api/directions")
for d in leftover:
    if d["name"] == QA_DIR:
        call("DELETE", f"/api/directions/{d['id']}")

s, d = call("POST", "/api/directions", {"name": QA_DIR, "description": "回归测试用"})
if not check("POST /api/directions → 201", s == 201, s):
    sys.exit(1)
did = d["id"]

s, _ = call("POST", "/api/directions", {"name": "   "})
check("空名称方向 → 400", s == 400, s)

s, ph1 = call("POST", f"/api/directions/{did}/phases", {"name": "QA阶段一", "goal": "g"})
s, ph2 = call("POST", f"/api/directions/{did}/phases", {"name": "QA阶段二"})
check("创建两个阶段 → 201", ph1["sort_order"] < ph2["sort_order"], (ph1, ph2))

s, t1 = call("POST", f"/api/phases/{ph1['id']}/tasks", {"title": "QA任务1"})
s, t2 = call("POST", f"/api/phases/{ph1['id']}/tasks", {"title": "QA任务2", "note": "n"})
check("创建任务 → 201 且默认 todo", t1["status"] == "todo", t1)
s, _ = call("POST", f"/api/phases/{ph1['id']}/tasks", {"title": ""})
check("空标题任务 → 400", s == 400, s)

_, before = get(f"/api/directions/{did}/roadmap")
check("roadmap 返回阶段与任务", len(before["phases"]) == 2 and len(before["phases"][0]["tasks"]) == 2, before)

# 状态机 + done_at
_, r = call("PATCH", f"/api/tasks/{t1['id']}", {"status": "doing"})
check("todo→doing：done_at 为空", r["status"] == "doing" and r["done_at"] is None, r)
_, r = call("PATCH", f"/api/tasks/{t1['id']}", {"status": "done"})
check("doing→done：写入 done_at", r["status"] == "done" and r["done_at"], r)
done_at_value = r["done_at"]
_, r = call("PATCH", f"/api/tasks/{t1['id']}", {"status": "skipped"})
check("done→skipped：done_at 清空", r["done_at"] is None, r)
s, _ = call("PATCH", f"/api/tasks/{t1['id']}", {"status": "wrong"})
check("非法状态 → 400", s == 400, s)
call("PATCH", f"/api/tasks/{t1['id']}", {"status": "done"})

one = status_of(did)
check("方向统计接口可见", one is not None, one)
if one:
    check("方向进度：progress 与 done/total 自洽（total 已不含 skipped，口径已修复）",
          one["progress"] == round(one["done"] / max(one["total"], 1) * 100), one)

# 日志
s, lg = call("POST", f"/api/directions/{did}/logs", {"minutes": 45, "content": "回归记录"})
check("新增日志（缺省日期=今天）→ 201", s == 201 and lg["date"] == date.today().isoformat(), lg)
s, _ = call("POST", f"/api/directions/{did}/logs", {"minutes": 0, "content": "  "})
check("时长与内容皆空 → 400", s == 400, s)
s, _ = call("POST", f"/api/directions/{did}/logs", {"minutes": -1, "content": "x"})
check("负时长 → 400", s == 400, s)
s, _ = call("POST", f"/api/directions/{did}/logs", {"date": "2026-9-1", "minutes": 5})
check("非法日期格式 → 400", s == 400, s)
s, _ = call("POST", "/api/directions/999999/logs", {"minutes": 5})
check("不存在方向的日志 → 404", s == 404, s)
s, _ = call("PATCH", f"/api/logs/{lg['id']}", {"minutes": 90})
_, logs = get(f"/api/directions/{did}/logs")
check("编辑日志时长生效", s == 200 and logs[0]["minutes"] == 90, logs[:1])

# 统计与回顾（先制造一条 skipped，用于校验 total 字段口径）
call("PATCH", f"/api/tasks/{t2['id']}", {"status": "skipped"})
s, stats = get(f"/api/directions/{did}/stats")
check("stats → 200 且字段齐全",
      s == 200 and {"tasks", "phases", "total_minutes", "active_days", "streak", "heatmap", "weekly"} <= set(stats),
      list(stats)[:8] if s == 200 else s)
if s == 200:
    check("stats：周数组固定 12 项", len(stats["weekly"]) == 12, len(stats["weekly"]))
    check("stats：热力图含今天", any(h["date"] == date.today().isoformat() for h in stats["heatmap"]), stats["heatmap"])
    check("stats：streak ≥ 1（今天有记录）", stats["streak"] >= 1, stats["streak"])
    check("stats：累计分钟 ≥ 90", stats["total_minutes"] >= 90, stats["total_minutes"])
    known("方向/阶段 total 口径统一",
          sum(p["total"] for p in stats["phases"]) == stats["tasks"]["total"],
          f"阶段合计 {sum(p['total'] for p in stats['phases'])} vs 方向 {stats['tasks']['total']}"
          "（方向 total 含 skipped、阶段 total 不含，前端卡片会出现「8/17 却显示 47%」）")

today = date.today()
monday = today - timedelta(days=today.weekday())
s, rv = get(f"/api/directions/{did}/review?date={today.isoformat()}")
check("周报：以周一为周首", s == 200 and rv["week_start"] == monday.isoformat(), rv.get("week_start"))
check("周报：7 天且总时长自洽",
      len(rv["days"]) == 7 and rv["total_minutes"] == sum(x["minutes"] for x in rv["days"]), rv["total_minutes"])
check("周报：含本周完成任务", any(t["id"] == t1["id"] for t in rv["done_tasks"]), rv["done_tasks"])
s, _ = get(f"/api/directions/{did}/review?date=not-a-date")
check("周报：非法日期 → 400", s == 400, s)

# ---------- 3. 级联删除 ----------

print("\n[3] 级联删除与幂等")
s, _ = call("DELETE", f"/api/phases/{ph2['id']}")
check("删除空阶段 → 200", s == 200, s)
s, _ = call("DELETE", f"/api/phases/{ph2['id']}")
check("重复删除 → 404", s == 404, s)
s, _ = call("DELETE", f"/api/directions/{did}")
check("删除方向（级联）→ 200", s == 200, s)
_, dirs = get("/api/directions")
check("级联后临时方向消失", all(x["id"] != did for x in dirs), [x["id"] for x in dirs])
s, _ = get(f"/api/directions/{did}/stats")
check("已删方向的 stats → 404", s == 404, s)

# ---------- 4. 静态资源（离线可用，NFR3/§2） ----------

print("\n[4] 静态资源与离线依赖")
for path, min_bytes in (("/", 500), ("/static/app.js", 3000), ("/static/style.css", 1000),
                        ("/static/vendor/vue.global.prod.js", 50000),
                        ("/static/vendor/echarts.min.js", 200000)):
    s, n = raw_get(path)
    check(f"{path} → 200 且非空", s == 200 and n >= min_bytes, f"{s}, {n}B")

# ---------- 5. 示例数据验收项（仅空库临时模式） ----------

print("\n[5] 示例数据（《02》验收标准 1）")
if _client is not None:
    s, _ = call("POST", "/api/demo")
    check("空库载入示例 → 201", s == 201, s)
    s, _ = call("POST", "/api/demo")
    check("重复载入 → 400（防覆盖）", s == 400, s)
    _, dirs = get("/api/directions")
    demo = dirs[0]
    _, rm = get(f"/api/directions/{demo['id']}/roadmap")
    _, lg = get(f"/api/directions/{demo['id']}/logs")
    check("示例：4 阶段", len(rm["phases"]) == 4, len(rm["phases"]))
    check("示例：≥15 任务", demo["total"] >= 15, demo["total"])
    check("示例：≥20 天日志", len({l["date"] for l in lg}) >= 20, len(lg))
    check("示例：进度在 0~100 之间", 0 <= demo["progress"] <= 100, demo["progress"])
else:
    print("SKIP  真实服务模式跳过（避免污染/依赖空库）")

# ---------- 6. V2 FR6：排序与跨阶段移动 ----------

print("\n[6] 排序与跨阶段移动（V2）")
s, d6 = call("POST", "/api/directions", {"name": QA_DIR, "description": "V2 排序测试"})
check("V2 建临时方向 → 201", s == 201, s)
did6 = d6["id"]
_, pA = call("POST", f"/api/directions/{did6}/phases", {"name": "甲"})
_, pB = call("POST", f"/api/directions/{did6}/phases", {"name": "乙"})
ta, tb, tc = [call("POST", f"/api/phases/{pA['id']}/tasks",
                   {"title": t})[1] for t in ("任务1", "任务2", "任务3")]

def _titles(phase_id):
    _, rm6 = get(f"/api/directions/{did6}/roadmap")
    return [t["title"] for p in rm6["phases"] if p["id"] == phase_id for t in p["tasks"]]

s, _ = call("POST", f"/api/tasks/{ta['id']}/reorder", {"direction": "down"})
check("任务下移 → [2,1,3]", _titles(pA["id"]) == ["任务2", "任务1", "任务3"], _titles(pA["id"]))
s, _ = call("POST", f"/api/tasks/{tb['id']}/reorder", {"direction": "up"})
check("顶部任务上移越界 → 顺序不变", _titles(pA["id"]) == ["任务2", "任务1", "任务3"], _titles(pA["id"]))
s, _ = call("POST", f"/api/tasks/{ta['id']}/reorder", {"direction": "up"})
check("任务上移复原 → [1,2,3]", _titles(pA["id"]) == ["任务1", "任务2", "任务3"], _titles(pA["id"]))
s, _ = call("POST", f"/api/tasks/{ta['id']}/reorder", {"direction": "sideways"})
check("非法 direction → 400", s == 400, s)

s, _ = call("POST", f"/api/phases/{pA['id']}/reorder", {"direction": "down"})
_, rm6 = get(f"/api/directions/{did6}/roadmap")
check("阶段下移 → [乙, 甲]", [p["name"] for p in rm6["phases"]] == ["乙", "甲"], rm6["phases"])

# 跨阶段移动：任务1 → 乙 末尾；跨方向移动 → 400
s, _ = call("POST", f"/api/tasks/{ta['id']}/move", {"phase_id": pB["id"]})
check("任务移到其他阶段末尾", s == 200 and _titles(pB["id"]) == ["任务1"], _titles(pB["id"]))
s, d7 = call("POST", "/api/directions", {"name": QA_DIR, "description": "跨方向校验"})
_, pOther = call("POST", f"/api/directions/{d7['id']}/phases", {"name": "别的方向阶段"})
s, _ = call("POST", f"/api/tasks/{ta['id']}/move", {"phase_id": pOther["id"]})
check("跨方向移动 → 400", s == 400, s)
s, _ = call("POST", f"/api/tasks/{ta['id']}/move", {"phase_id": 999999})
check("移动到不存在阶段 → 404", s == 404, s)

# 日志 limit 参数（Qoder 审查建议项）
for m in (10, 20, 30):
    call("POST", f"/api/directions/{did6}/logs", {"minutes": m})
_, all_logs = get(f"/api/directions/{did6}/logs")
s, limited = get(f"/api/directions/{did6}/logs?limit=2")
check("logs?limit=2 → 只返回最新 2 条",
      s == 200 and len(limited) == 2 and
      [x["minutes"] for x in limited] == [x["minutes"] for x in all_logs[:2]],
      (len(limited), [x["minutes"] for x in limited]))
s, _ = get(f"/api/directions/{did6}/logs?limit=0")
check("limit=0 → 400", s == 400, s)
s, _ = get(f"/api/directions/{did6}/logs?limit=abc")
check("limit=abc → 400", s == 400, s)

# 清理 V2 临时方向
call("DELETE", f"/api/directions/{did6}")
call("DELETE", f"/api/directions/{d7['id']}")

# ---------- 7. V2 FR7：数据导出导入（仅临时库模式，避免整库替换真实数据） ----------

print("\n[7] 数据导出导入（V2）")
if _client is not None:
    _, dirs_before = get("/api/directions")
    s, backup = get("/api/export")
    check("导出：元信息完整",
          s == 200 and backup["app"] == "study-tools" and backup["version"] == 1
          and isinstance(backup.get("exported_at"), str), backup.get("app"))
    counts = {k: len(backup[k]) for k in ("directions", "phases", "tasks", "logs")}
    check("导出：数量与现库一致",
          counts["directions"] == len(dirs_before), (counts, len(dirs_before)))

    # 制造额外数据 → 导入备份 → 回到备份时点
    _, extra = call("POST", "/api/directions", {"name": QA_DIR})
    s, res = call("POST", "/api/import", backup)
    check("导入 → 200 且计数与备份一致", s == 200 and res["counts"] == counts, res)
    _, dirs_after = get("/api/directions")
    check("导入后回到备份时点（额外方向消失）",
          all(x["id"] != extra["id"] for x in dirs_after), [x["id"] for x in dirs_after])

    # 非法备份拒绝，且原数据保持不变
    bad = json.loads(json.dumps(backup))
    bad["tasks"][0]["status"] = "hacked"
    s, _ = call("POST", "/api/import", bad)
    check("非法 status → 400", s == 400, s)
    bad2 = json.loads(json.dumps(backup))
    bad2["app"] = "other"
    s, _ = call("POST", "/api/import", bad2)
    check("元信息不符 → 400", s == 400, s)
    bad3 = json.loads(json.dumps(backup))
    if bad3["logs"]:
        bad3["logs"][0]["date"] = "2026/1/1"
        s, _ = call("POST", "/api/import", bad3)
        check("非法日期 → 400", s == 400, s)
    bad4 = json.loads(json.dumps(backup))
    bad4["phases"][0]["direction_id"] = 99999999
    s, _ = call("POST", "/api/import", bad4)
    check("外键引用断裂 → 400", s == 400, s)
    _, dirs_ok = get("/api/directions")
    check("非法导入后数据未受影响", len(dirs_ok) == counts["directions"], len(dirs_ok))

    # 导入后新建方向不得撞已导入的 id（Qoder 审查项：sqlite_sequence 守护断言）
    s, fresh = call("POST", "/api/directions", {"name": QA_DIR})
    max_backup_dir = max(x["id"] for x in backup["directions"])
    check("导入后新建方向 id > 备份最大 id（AUTOINCREMENT 自推进）",
          s == 201 and fresh["id"] > max_backup_dir, (fresh["id"], max_backup_dir))
    call("DELETE", f"/api/directions/{fresh['id']}")
else:
    print("SKIP  真实服务模式跳过（整库替换不应用于真实数据）")

# ---------- 汇总 ----------

if _tmpdir and os.path.isdir(_tmpdir):
    shutil.rmtree(_tmpdir, ignore_errors=True)

print("\n" + "=" * 56)
print(f"模式：{'临时库 + test_client' if _client else '真实服务 ' + QA_URL}")
print(f"失败 {len(failures)} 项 · 已知问题 {len(warnings)} 项")
for f in failures:
    print("  FAIL -", f)
for w in warnings:
    print("  WARN -", w)
print("=" * 56)
sys.exit(1 if failures else 0)
