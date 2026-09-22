"""study tools · API 回归冒烟测试（见 docs/05-协作约定.md 第 3 节）

两种运行模式：
  1) 默认（推荐，零副作用）：python tests/api_smoke.py
     在系统临时目录建一份独立 SQLite 库，用 Flask test_client 直接调用，
     不需要先启动服务，也不会碰到 app/data/study.db。
  2) 打真实服务：STUDY_TOOLS_URL=http://127.0.0.1:5000 python tests/api_smoke.py
     此模式下所有写操作都收敛在自建方向「__QA_临时方向__」内，结束前删除；
     示例数据相关的验收项（依赖空库）会自动跳过。

退出码：0 = 全部通过，1 = 有失败。
"""
import json
import os
import shutil
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.join(os.path.dirname(HERE), "app")
sys.path.insert(0, APP_DIR)

# Windows 默认 GBK 控制台打印「↔」等字符会 UnicodeEncodeError 直接崩掉整轮测试
# （V4 用户审查 #7）；无 reconfigure 的非交互环境保持原样不折腾
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

QA_URL = os.environ.get("STUDY_TOOLS_URL", "").rstrip("/")
QA_DIR = "__QA_临时方向__"

failures = []


# ---------- 调用后端：test_client 或 HTTP 两种实现统一成 call(method, path, body) ----------

def _make_test_client():
    import database
    tmp = tempfile.mkdtemp(prefix="study_tools_qa_")
    # 崩溃路径（断言失败/异常退出）也清掉临时库，不在 %TEMP% 留残骸
    import atexit
    atexit.register(shutil.rmtree, tmp, ignore_errors=True)
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
s, _ = call("POST", f"/api/directions/{did}/logs", {"minutes": 25.5, "content": "x"})
check("浮点时长 → 400（不再被 int() 静默截断，Qoder TD1 附注）", s == 400, s)
s, _ = call("POST", f"/api/directions/{did}/logs", {"minutes": True, "content": "x"})
check("布尔时长 → 400", s == 400, s)
s, _ = call("POST", f"/api/directions/{did}/logs", {"date": "2026-9-1", "minutes": 5})
check("非法日期格式 → 400", s == 400, s)
s, _ = call("POST", f"/api/directions/{did}/logs", {"date": "9999-12-31", "minutes": 5})
check("未来日期 → 400（原可写入并污染累计/活跃/热力图统计）", s == 400, s)
s, _ = call("PATCH", f"/api/logs/{lg['id']}", {"date": "9999-12-31"})
check("编辑日志为未来日期 → 400（与 POST 同口径）", s == 400, s)
s, _ = call("POST", "/api/directions/999999/logs", {"minutes": 5})
check("不存在方向的日志 → 404", s == 404, s)
s, _ = call("PATCH", f"/api/logs/{lg['id']}", {"minutes": 90})
_, logs = get(f"/api/directions/{did}/logs")
check("编辑日志时长生效", s == 200 and logs[0]["minutes"] == 90, logs[:1])
s, _ = call("PATCH", f"/api/logs/{lg['id']}", {"minutes": 30.5})
check("编辑为浮点时长 → 400", s == 400, s)

# 文本入参类型探针（Qoder 轮审缺陷 2，扫到全部文本入口）：
# `(v or '').strip()` 形状遇 123 → AttributeError 500、遇 {} → 静默吞成空串，现在一律 400
s, _ = call("POST", "/api/directions", {"name": 123})
check("方向名称非字符串 → 400（原 500）", s == 400, s)
s, _ = call("POST", "/api/directions", {"name": "文本方向", "description": {"a": 1}})
check("方向描述为对象 → 400（原静默吞掉）", s == 400, s)
s, _ = call("PATCH", f"/api/directions/{did}", {"name": 123})
check("编辑方向名称非字符串 → 400", s == 400, s)
# 严格部分更新（V4 用户审查 #2）：字段在则更新、不在则保留——
# 原实现 name 必填且全字段 UPDATE，只提交 name 会静默清空描述、只提交描述直接 400
s, _ = call("PATCH", f"/api/directions/{did}", {"description": "部分更新描述"})
check("PATCH 只提交 description → 200（原 400）", s == 200, s)
s, _ = call("PATCH", f"/api/directions/{did}", {"name": QA_DIR + "·改"})
_, rm_pu = get(f"/api/directions/{did}/roadmap")
check("部分更新：只改 name 不清空 description（原静默清空）",
      s == 200 and rm_pu["direction"]["name"] == QA_DIR + "·改"
      and rm_pu["direction"]["description"] == "部分更新描述", rm_pu["direction"])
s, _ = call("PATCH", f"/api/directions/{did}", {})
check("PATCH 空对象（无字段可更新）→ 400", s == 400, s)
call("PATCH", f"/api/directions/{did}", {"name": QA_DIR})  # 复原名貌，不动 description
s, _ = call("PATCH", f"/api/phases/{ph1['id']}", {"goal": "部分更新目标"})
check("阶段 PATCH 只提交 goal → 200（原 400）", s == 200, s)
s, _ = call("PATCH", f"/api/phases/{ph1['id']}", {"name": "阶段名·改"})
_, rm_pu2 = get(f"/api/directions/{did}/roadmap")
_p1 = next(p for p in rm_pu2["phases"] if p["id"] == ph1["id"])
check("阶段部分更新：只改 name 不清空 goal",
      s == 200 and _p1["name"] == "阶段名·改" and _p1["goal"] == "部分更新目标", _p1)
s, _ = call("PATCH", f"/api/phases/{ph1['id']}", {})
check("阶段 PATCH 空对象 → 400", s == 400, s)
s, _ = call("POST", f"/api/directions/{did}/phases", {"name": 123})
check("阶段名称非字符串 → 400（原 500）", s == 400, s)
s, _ = call("POST", f"/api/directions/{did}/phases", {"name": "文本阶段", "goal": 456})
check("阶段目标非字符串 → 400", s == 400, s)
s, _ = call("POST", f"/api/phases/{ph1['id']}/tasks", {"title": 123})
check("任务标题非字符串 → 400（原 500）", s == 400, s)
s, _ = call("POST", f"/api/phases/{ph1['id']}/tasks", {"title": "文本任务", "note": []})
check("任务备注非字符串 → 400", s == 400, s)
s, _ = call("PATCH", f"/api/tasks/{t1['id']}", {"title": 123})
check("编辑任务标题非字符串 → 400", s == 400, s)
s, _ = call("POST", f"/api/directions/{did}/logs", {"content": 123})
check("日志内容非字符串 → 400（原 500）", s == 400, s)
s, _ = call("PATCH", f"/api/logs/{lg['id']}", {"content": {"x": 1}})
check("编辑日志内容非字符串 → 400", s == 400, s)

# 顶层非对象 JSON 体探针（全面审计发现）：get_json(silent=True) 只压制解析
# 错误，[1,2]/"text"/123 这类合法 JSON 原样返回，`or {}` 归一不掉真值，
# body.get 直接 AttributeError → 500；json_body() 统一入口后一律 400
for desc, raw in (("数组", [1, 2]), ("字符串", "text"), ("数字", 123), ("布尔", True)):
    s, _ = call("POST", "/api/directions", raw)
    check(f"顶层非对象 JSON 体（{desc}）→ 400（原 500）", s == 400, s)
s, _ = call("POST", f"/api/phases/{ph1['id']}/reorder", [1])
check("reorder 顶层非对象 → 400（原 500）", s == 400, s)
s, _ = call("POST", f"/api/tasks/{t1['id']}/move", "x")
check("move 顶层非对象 → 400（原 500）", s == 400, s)
# phase_id 严格整数（V4 用户审查 #3）：int() 原来会把 1.5 / true / "1" 静默转换接受
for desc, v in (("浮点", ph1["id"] + 0.5), ("字符串", str(ph1["id"])), ("布尔", True)):
    s, _ = call("POST", f"/api/tasks/{t1['id']}/move", {"phase_id": v})
    check(f"move phase_id 为{desc} → 400（原被 int() 静默转换）", s == 400, s)
s, _ = call("POST", "/api/import", [1, 2])
check("import 顶层非对象 → 400（原 500）", s == 400, s)

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
    # （原 known「方向/阶段 total 口径统一」已删：三处 total 统一不含 skipped 后
    # 该断言永真，留着会让人误以为还有未决告警 —— Qoder 文档审计指认的僵尸）

today = date.today()
monday = today - timedelta(days=today.weekday())
s, rv = get(f"/api/directions/{did}/review?date={today.isoformat()}")
check("周报：以周一为周首", s == 200 and rv["week_start"] == monday.isoformat(), rv.get("week_start"))
check("周报：7 天且总时长自洽",
      len(rv["days"]) == 7 and rv["total_minutes"] == sum(x["minutes"] for x in rv["days"]), rv["total_minutes"])
check("周报：含本周完成任务", any(t["id"] == t1["id"] for t in rv["done_tasks"]), rv["done_tasks"])
s, _ = get(f"/api/directions/{did}/review?date=not-a-date")
check("周报：非法日期 → 400", s == 400, s)
s, _ = get(f"/api/directions/{did}/review?date=9999-12-31")
check("周报：年末最后一周日期 → 400（周一+6 天溢出，原 500）", s == 400, s)

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
s, n = raw_get("/favicon.ico")
check("/favicon.ico → 200（浏览器默认请求，原 404；图标源 assets/studytool.ico）",
      s == 200 and n >= 1000, f"{s}, {n}B")

# 启动器换行符护栏（run.bat 闪退事故的防回归）：cmd 按 GBK 解析批处理，
# UTF-8 中文注释 + LF 行尾会吞掉下一行首字符（python→ython、pause→ause，
# 窗口闪退且无提示）；run.sh 相反，CRLF 会破坏 bash 与 shebang。
_bat = open(os.path.join(os.path.dirname(HERE), "run.bat"), "rb").read()
check("run.bat 全行 CRLF（cmd 按 GBK 解析，UTF-8+LF 会吞行 → 闪退）",
      len(_bat) > 0 and b"\r\n" in _bat
      and _bat.replace(b"\r\n", b"").count(b"\n") == 0,
      f"孤立 LF {_bat.replace(b'\r\n', b'').count(b'\n')} 处")
_sh = open(os.path.join(os.path.dirname(HERE), "run.sh"), "rb").read()
check("run.sh 全行 LF（CRLF 会破坏 bash 与 shebang）",
      len(_sh) > 0 and b"\r\n" not in _sh and _sh.count(b"\n") > 0,
      f"CRLF {_sh.count(b'\r\n')} 处")

# 桌面图标资源护栏（Qoder 建议升级为深度断言）：逐帧 PNG 签名、偏移连续、
# 文件尾对齐、尺寸互不重复——"六档多尺寸"从声明变成断言，重新生成时少档
# 或结构损坏会被立刻抓住
import struct
_ico = open(os.path.join(os.path.dirname(HERE), "assets", "studytool.ico"), "rb").read()
_ico_frames = struct.unpack_from("<H", _ico, 4)[0]
check("assets/studytool.ico 为有效多帧 ICO（≥4 档）",
      _ico[:4] == b"\x00\x00\x01\x00" and _ico_frames >= 4,
      f"{len(_ico)}B, {_ico_frames} 帧")
_ok_frames, _seen_sizes, _off, _tail_ok = 0, set(), 6 + 16 * _ico_frames, True
for _i in range(_ico_frames):
    _w = _ico[6 + 16 * _i] or 256
    _size, _data_off = struct.unpack_from("<II", _ico, 6 + 16 * _i + 8)
    _is_png = _ico[_data_off:_data_off + 8] == b"\x89PNG\r\n\x1a\n"
    _ok_frames += int(_is_png and _data_off == _off)
    _seen_sizes.add(_w if _is_png else -_w)
    _off = _data_off + _size
check("ICO 每帧为 PNG 且偏移首尾连续", _ok_frames == _ico_frames, f"{_ok_frames}/{_ico_frames}")
check("ICO 文件尾对齐且尺寸互不重复",
      _off == len(_ico) and len(_seen_sizes) == _ico_frames, (_off, len(_ico)))

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

    # V3（FR9.7）：示例复习卡 —— 到期时间动态生成，禁止写死日期
    _, cards5 = get(f"/api/directions/{demo['id']}/cards")
    check("示例：6 张复习卡", len(cards5) == 6, len(cards5))
    s, q5 = get(f"/api/directions/{demo['id']}/review/queue")
    check("示例：队列 = 4 张到期卡 + 2 张新卡",
          s == 200 and q5["counts"]["due"] == 4 and q5["counts"]["new"] == 2
          and q5["counts"]["total"] == 6 and len(q5["queue"]) == 6, q5.get("counts"))
    now5 = datetime.now(timezone.utc).isoformat()
    fresh5 = [c for c in cards5 if c["fsrs"]["last_review"] is None]
    overdue5 = [c for c in cards5 if c["fsrs"]["last_review"] is not None]
    check("示例：2 张新卡，due = 创建时刻（今天）",
          len(fresh5) == 2 and all(c["due"][:10] == now5[:10] for c in fresh5),
          [c["due"] for c in fresh5])
    check("示例：4 张到期卡，due 已过期且 30 天前复习过",
          len(overdue5) == 4 and all(c["due"] < now5 for c in overdue5)
          and all(c["fsrs"]["last_review"] < now5[:10] + "T" for c in overdue5),
          [c["due"] for c in overdue5])
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

# sort_order 必须连续（曾经的重构把 MAX+1 又 +1 了一次，造成新建项跳号）
check("新建阶段 sort_order 连续", [pA["sort_order"], pB["sort_order"]] == [0, 1],
      (pA["sort_order"], pB["sort_order"]))
check("新建任务 sort_order 连续无跳号", [ta["sort_order"], tb["sort_order"], tc["sort_order"]] == [0, 1, 2],
      [ta["sort_order"], tb["sort_order"], tc["sort_order"]])

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
s, _ = get(f"/api/directions/{did6}/logs?limit={'9' * 30}")
check("limit 超 int64 → 400（SQLite 绑参 OverflowError，原 500）", s == 400, s)

# 清理 V2 临时方向
call("DELETE", f"/api/directions/{did6}")
call("DELETE", f"/api/directions/{d7['id']}")

# ---------- 7. V2 FR7：数据导出导入（仅临时库模式，避免整库替换真实数据） ----------

print("\n[7] 数据导出导入（V2）")
if _client is not None:
    _, dirs_before = get("/api/directions")
    s, backup = get("/api/export")
    check("导出：元信息完整",
          s == 200 and backup["app"] == "study-tools" and backup["version"] == 2
          and isinstance(backup.get("exported_at"), str), backup.get("app"))
    counts = {k: len(backup[k]) for k in
              ("directions", "phases", "tasks", "logs", "cards", "review_logs")}
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

    # V2.1 审查补充：畸形字段一律 400，不得漏到 INSERT 阶段抛 KeyError 变 500、也不得放行入库
    def _mutate(fn):
        b = json.loads(json.dumps(backup))
        fn(b)
        return b

    s, _ = call("POST", "/api/import", _mutate(lambda b: b["tasks"][0].pop("title")))
    check("任务缺 title → 400（而非 500）", s == 400, s)
    s, _ = call("POST", "/api/import", _mutate(lambda b: b["tasks"][0].__setitem__("title", "   ")))
    check("任务标题全空白 → 400", s == 400, s)
    s, _ = call("POST", "/api/import", _mutate(lambda b: b["phases"][0].pop("name")))
    check("阶段缺 name → 400（而非 500）", s == 400, s)
    s, _ = call("POST", "/api/import", _mutate(lambda b: b["directions"][0].pop("id")))
    check("方向缺 id → 400", s == 400, s)
    if backup["logs"]:
        for label, mutate in (
            ("时长为字符串", lambda b: b["logs"][0].__setitem__("minutes", "abc")),
            ("时长为负数", lambda b: b["logs"][0].__setitem__("minutes", -500)),
            ("时长为布尔", lambda b: b["logs"][0].__setitem__("minutes", True)),
            ("日历上不存在的日期", lambda b: b["logs"][0].__setitem__("date", "2026-13-45")),
            ("非 YYYY-MM-DD 写法", lambda b: b["logs"][0].__setitem__("date", "20260130")),
            ("日期为 null", lambda b: b["logs"][0].__setitem__("date", None)),
        ):
            s, _ = call("POST", "/api/import", _mutate(mutate))
            check(f"{label} → 400", s == 400, s)
    # V2.1.1 主开发补充：可解析但不规范的形式（ISO 周日期能骗过 fromisoformat，
    # 但不符合应用 YYYY-MM-DD 约定）以及 done_at 字段（此前完全未校验）
    s, _ = call("POST", "/api/import", _mutate(
        lambda b: b["logs"][0].__setitem__("date", "2026-W01-1")))
    check("ISO 周日期（可解析但不规范）→ 400", s == 400, s)
    s, _ = call("POST", "/api/import", _mutate(
        lambda b: b["tasks"][0].__setitem__("done_at", "2026-13-45")))
    check("任务 done_at 非法 → 400", s == 400, s)
    # 审查者第二轮对抗探针补口：非对象记录、未校验的数值/文本字段
    s, _ = call("POST", "/api/import", _mutate(lambda b: b["logs"].append(None)))
    check("logs 里混入 null 记录 → 400（而非 500）", s == 400, s)
    s, _ = call("POST", "/api/import", _mutate(lambda b: b["directions"].append("x")))
    check("directions 里混入字符串记录 → 400", s == 400, s)
    s, _ = call("POST", "/api/import", _mutate(
        lambda b: b["tasks"][0].__setitem__("sort_order", "abc")))
    check("任务 sort_order 为字符串 → 400", s == 400, s)
    s, _ = call("POST", "/api/import", _mutate(
        lambda b: b["phases"][0].__setitem__("sort_order", "abc")))
    check("阶段 sort_order 为字符串 → 400", s == 400, s)
    s, _ = call("POST", "/api/import", _mutate(
        lambda b: b["tasks"][0].__setitem__("sort_order", 1.5)))
    check("任务 sort_order 为浮点 → 400", s == 400, s)
    s, _ = call("POST", "/api/import", _mutate(
        lambda b: b["tasks"][0].__setitem__("note", 123)))
    check("任务备注为整数 → 400", s == 400, s)
    s, _ = call("POST", "/api/import", _mutate(
        lambda b: b["phases"][0].__setitem__("goal", {"x": 1})))
    check("阶段目标为对象 → 400", s == 400, s)
    s, _ = call("POST", "/api/import", _mutate(
        lambda b: b["logs"][0].__setitem__("minutes", 30.5)))
    check("时长为浮点 → 400", s == 400, s)
    s, _ = call("POST", "/api/import", _mutate(
        lambda b: b["tasks"][0].__setitem__("done_at", "2026-W01-1")))
    check("done_at 为 ISO 周日期 → 400", s == 400, s)
    # version 严格类型（V4 用户审查 #1）：数组/对象在 set 成员测试上直接
    # TypeError → 500；True==1、2.0==2 被 in 的相等语义错误放行
    for desc, v in (("数组", [1]), ("对象", {"v": 2}), ("布尔", True), ("浮点", 2.0)):
        s, _ = call("POST", "/api/import", _mutate(
            lambda b: b.__setitem__("version", v)))
        check(f"version 为{desc} → 400（原 500 / 误接受）", s == 400, s)
    # 外键字段为不可哈希类型：原来在外键段的 set 成员测试上 TypeError → 500，
    # 现在声明表先拒（400，带中文标签）
    s, _ = call("POST", "/api/import", _mutate(
        lambda b: b["phases"][0].__setitem__("direction_id", [1])))
    check("外键字段为数组 → 400（原 TypeError 500）", s == 400, s)
    # sort_order 为 null / 缺失是安全的（导入时归 0），不应被拒
    s, _ = call("POST", "/api/import", _mutate(
        lambda b: b["tasks"][0].__setitem__("sort_order", None)))
    check("sort_order 为 null → 200（按 0 处理）", s == 200, s)
    call("POST", "/api/import", backup)

    _, dirs_ok2 = get("/api/directions")
    check("上述畸形导入均未破坏现库", len(dirs_ok2) == counts["directions"], len(dirs_ok2))

    # 兜底：库里已存在非法日期（历史脏数据）时，统计接口仍须 200 而不是整页 500
    import database
    import sqlite3
    poison_dir_id = backup["directions"][0]["id"]
    conn = sqlite3.connect(database.DB_PATH)
    conn.execute("INSERT INTO logs(direction_id, date, minutes, content) VALUES (?, ?, ?, ?)",
                 (poison_dir_id, "2026-13-45", 30, "脏数据"))
    conn.commit()
    conn.close()
    s, _ = get(f"/api/directions/{poison_dir_id}/stats")
    check("库中存在非法日期时 /stats 仍 200", s == 200, s)
    call("POST", "/api/import", backup)  # 还原干净数据

    # 兜底：库里已有非整数 sort_order（手工改库、历史脏数据）时，
    # 「添加任务 / 添加阶段」这类核心操作不能被一条脏数据打死成 500
    poison_phase = backup["phases"][0]["id"]
    conn = sqlite3.connect(database.DB_PATH)
    conn.execute("UPDATE tasks SET sort_order = 'abc' WHERE phase_id = ?", (poison_phase,))
    conn.execute("UPDATE phases SET sort_order = 'abc' WHERE direction_id = ?", (poison_dir_id,))
    conn.commit()
    conn.close()
    s, _ = call("POST", f"/api/phases/{poison_phase}/tasks", {"title": "脏排序下新建任务"})
    check("脏 sort_order 下新建任务仍 201", s == 201, s)
    s, _ = call("POST", f"/api/directions/{poison_dir_id}/phases", {"name": "脏排序下新建阶段"})
    check("脏 sort_order 下新建阶段仍 201", s == 201, s)
    call("POST", "/api/import", backup)  # 还原干净数据

    # 导入后新建方向不得撞已导入的 id（Qoder 审查项：sqlite_sequence 守护断言）
    s, fresh = call("POST", "/api/directions", {"name": QA_DIR})
    max_backup_dir = max(x["id"] for x in backup["directions"])
    check("导入后新建方向 id > 备份最大 id（AUTOINCREMENT 自推进）",
          s == 201 and fresh["id"] > max_backup_dir, (fresh["id"], max_backup_dir))
    call("DELETE", f"/api/directions/{fresh['id']}")

    # ---- V3：备份版本兼容矩阵（docs/06 §5，§9.4 验收）----
    # v3 库 × v1 备份：v1（V3 之前）没有 cards/review_logs 键 → 按空表放行
    v1_shape = {k: backup[k] for k in
                ("app", "exported_at", "directions", "phases", "tasks", "logs")}
    v1_shape["version"] = 1
    s, res = call("POST", "/api/import", v1_shape)
    check("v3 库 × v1 备份 → 200，cards/review_logs 按空表",
          s == 200 and res["counts"]["cards"] == 0 and res["counts"]["review_logs"] == 0, res)
    _, cards_v1 = get(f"/api/directions/{v1_shape['directions'][0]['id']}/cards")
    check("v1 导入后卡片被清空（整库替换语义）", cards_v1 == [], len(cards_v1))

    # v3 库 × v2 备份：往返一致
    s, res = call("POST", "/api/import", backup)
    check("v3 库 × v2 备份 → 200 且卡片/复习记录计数一致",
          s == 200 and res["counts"]["cards"] == counts["cards"]
          and res["counts"]["review_logs"] == counts["review_logs"], res)
    _, cards_rt = get(f"/api/directions/{backup['directions'][0]['id']}/cards")
    check("v2 备份往返：卡片正反面/due/调度状态逐字段还原",
          {c["id"]: (c["front"], c["back"], c["due"], c["fsrs"]) for c in cards_rt}
          == {c["id"]: (c["front"], c["back"], c["due"], c["fsrs"]) for c in backup["cards"]},
          len(cards_rt))

    # done_at 键整体缺失的合法 v2 备份必须可导入（Qoder 轮审缺陷 3：
    # 声明表把 done_at 声明为可缺省，INSERT 却直取 t["done_at"] 裸 KeyError）
    no_done = json.loads(json.dumps(backup))
    for t_ in no_done["tasks"]:
        t_.pop("done_at", None)
    s, res = call("POST", "/api/import", no_done)
    check("v2 备份缺 done_at 键 → 200（声明说可选就得真可选）",
          s == 200 and res["counts"]["tasks"] == counts["tasks"], (s, res))
    call("POST", "/api/import", backup)  # 还原

    # 版本高于本程序支持 → 文案明说（不再笼统报"不是有效的备份"）
    hi = json.loads(json.dumps(backup)); hi["version"] = 99
    s, res = call("POST", "/api/import", hi)
    check("备份版本 99 → 400 且文案明说高于本程序支持的 2",
          s == 400 and "高于本程序支持的 2" in res["error"], res)

    # v2 备份必须带两张新表
    no_cards = json.loads(json.dumps(backup)); no_cards.pop("cards")
    s, _ = call("POST", "/api/import", no_cards)
    check("v2 备份缺 cards 键 → 400", s == 400, s)

    # cards / review_logs 字段与跨表一致性探针（对抗性，同上方风格）
    s, _ = call("POST", "/api/import", _mutate(
        lambda b: b["cards"][0]["fsrs"].__setitem__("card_id", 999999)))
    check("调度状态与卡片 id 不一致 → 400", s == 400, s)
    s, _ = call("POST", "/api/import", _mutate(
        lambda b: b["cards"][0]["fsrs"].__setitem__("due", "2099-01-01T00:00:00+00:00")))
    check("调度状态与 due 列不一致 → 400", s == 400, s)
    s, _ = call("POST", "/api/import", _mutate(
        lambda b: b["cards"][0].__setitem__("due", "2026-09-19T12:00:00+08:00")))
    check("due 非 UTC 时区 → 400", s == 400, s)
    s, _ = call("POST", "/api/import", _mutate(
        lambda b: b["cards"][0].__setitem__("fsrs", {"card_id": 1, "state": 1})))
    check("调度状态缺字段 → 400", s == 400, s)
    s, _ = call("POST", "/api/import", _mutate(
        lambda b: b["cards"][0].__setitem__("fsrs", "not-a-dict")))
    check("调度状态非对象 → 400", s == 400, s)
    s, _ = call("POST", "/api/import", _mutate(
        lambda b: b["cards"][0].pop("front")))
    check("卡片缺正面 → 400", s == 400, s)
    s, _ = call("POST", "/api/import", _mutate(
        lambda b: b["cards"][0].__setitem__("direction_id", 999999)))
    check("卡片指向不存在方向 → 400", s == 400, s)
    if backup["review_logs"]:
        s, _ = call("POST", "/api/import", _mutate(
            lambda b: b["review_logs"][0].__setitem__("rating", 5)))
        check("复习记录评分越界 → 400", s == 400, s)
        s, _ = call("POST", "/api/import", _mutate(
            lambda b: b["review_logs"][0].__setitem__("card_id", 999999)))
        check("复习记录指向不存在卡片 → 400", s == 400, s)
        s, _ = call("POST", "/api/import", _mutate(
            lambda b: b["review_logs"][0].__setitem__("reviewed_at", "2026-09-19 12:00:00")))
        check("复习时间非 ISO 格式 → 400", s == 400, s)
    call("POST", "/api/import", backup)  # 还原
    _, dirs_ok3 = get("/api/directions")
    check("兼容矩阵探针均未破坏现库", len(dirs_ok3) == counts["directions"], len(dirs_ok3))
else:
    print("SKIP  真实服务模式跳过（整库替换不应用于真实数据）")

# ---------- 8. TD2 防回归：方向列表与详情口径逐字段一致 ----------

print("\n[8] 列表与详情口径一致（TD2 防回归）")
# Qoder 轮审补充：此前该步只有 demo 一个方向（8 字段 × 1 方向，覆盖面薄）。
# 显式构造边界方向 —— 空方向 / 全部任务 skipped（分母为 0 边界）/
# 只有零时长日志（active_days/streak 口径 + 同日多条零时长日志）——
# 让分母边界与口径真的被跨接口（列表 × roadmap × stats）比对过。
s, bd_empty = call("POST", "/api/directions", {"name": QA_DIR, "description": "边界：空方向"})
s, bd_skip = call("POST", "/api/directions", {"name": QA_DIR, "description": "边界：全 skipped"})
_, ph_b = call("POST", f"/api/directions/{bd_skip['id']}/phases", {"name": "唯一阶段"})
for i in (1, 2):
    _, tk_b = call("POST", f"/api/phases/{ph_b['id']}/tasks", {"title": f"跳过{i}"})
    call("PATCH", f"/api/tasks/{tk_b['id']}", {"status": "skipped"})
s, bd_note = call("POST", "/api/directions", {"name": QA_DIR, "description": "边界：有内容无时长"})
call("POST", f"/api/directions/{bd_note['id']}/logs", {"minutes": 0, "content": "纯笔记一条"})
call("POST", f"/api/directions/{bd_note['id']}/logs", {"minutes": 0, "content": "纯笔记二条"})

_, dirs8 = get("/api/directions")
check("存在可对比的方向（含 3 个边界方向）", len(dirs8) >= 4, len(dirs8))
for d in dirs8:
    _, rm8 = get(f"/api/directions/{d['id']}/roadmap")
    dd = rm8["direction"]
    for k in ("total", "done", "doing", "skipped", "progress",
              "total_minutes", "active_days", "streak"):
        check(f"方向{d['id']} 列表.{k} == 详情.{k}", d[k] == dd[k], (d[k], dd[k]))
    _, st8 = get(f"/api/directions/{d['id']}/stats")
    for k in ("total", "done", "doing", "skipped", "progress"):
        check(f"方向{d['id']} 列表.{k} == stats.tasks.{k}", d[k] == st8["tasks"][k], (d[k], st8["tasks"][k]))
    for k in ("total_minutes", "active_days", "streak"):
        check(f"方向{d['id']} 列表.{k} == stats.{k}", d[k] == st8[k], (d[k], st8[k]))

# 边界口径抽查（Qoder 边界数据集）
one_skip = status_of(bd_skip["id"])
check("边界：全 skipped 方向 total=0 且 progress=0（分母边界）",
      one_skip["total"] == 0 and one_skip["progress"] == 0 and one_skip["skipped"] == 2, one_skip)
one_note = status_of(bd_note["id"])
check("边界：同日多条零时长日志 → active_days=1 / streak=1 / total_minutes=0",
      one_note["active_days"] == 1 and one_note["streak"] == 1
      and one_note["total_minutes"] == 0, one_note)

for d in (bd_empty, bd_skip, bd_note):
    call("DELETE", f"/api/directions/{d['id']}")

# ---------- 9. 复习卡片（V3 FR9） ----------

print("\n[9] 复习卡片（V3）")
import review as review_mod

# 9.1 调度器基准（docs/06 §4/§9.1；不碰 DB，两种模式都跑）。
# 断言值为本机 fsrs==6.3.2 实测基线 —— 显式传 review_datetime，无参 Card() 的
# due 是「现在」，不传会随运行时间漂移。
T0 = datetime(2026, 9, 19, 12, 0, 0, tzinfo=timezone.utc)
c = review_mod.new_card(1)
check("新卡：card_id 显式对齐、初始为 Learning 态",
      c.card_id == 1 and c.state.value == 1 and c.last_review is None, c.to_dict())
c1, _ = review_mod.review(c, 3, when=T0)
d1 = c1.to_dict()
check("Good 基线：state=1/step=1/stability=2.3065/difficulty≈2.1181/due=+10 分钟",
      d1["state"] == 1 and d1["step"] == 1 and d1["stability"] == 2.3065
      and round(d1["difficulty"], 4) == 2.1181
      and d1["due"] == "2026-09-19T12:10:00+00:00", d1)
c2, _ = review_mod.review(c1, 3, when=T0)
check("再 Good → Review 态，due=+2 天",
      c2.state.value == 2 and c2.due.isoformat() == "2026-09-21T12:00:00+00:00", c2.due)
c3, _ = review_mod.review(c2, 1, when=T0)
check("再 Again → Relearning，due=+10 分钟（同刻三连评序列 stability≈0.7751）",
      c3.state.value == 3 and c3.due.isoformat() == "2026-09-19T12:10:00+00:00"
      and round(c3.stability, 4) == 0.7751, (c3.state, c3.due, c3.stability))
# docs/06 §4 两序列口径：每次在上一张卡的 due 时点评 → 0.6077（r2 的 0.608 即此序列）
c3b = review_mod.new_card(11)
c3b, _ = review_mod.review(c3b, 3, when=T0)
c3b, _ = review_mod.review(c3b, 3, when=c3b.due)
c3b, _ = review_mod.review(c3b, 1, when=c3b.due)
check("按 due 时点逐次评的序列 stability≈0.6077（两条序列都有主）",
      c3b.state.value == 3 and round(c3b.stability, 4) == 0.6077, c3b.stability)
check("fsrs 列序列化往返一致（card_to_json ↔ card_from_json）",
      review_mod.card_from_json(review_mod.card_to_json(c3)).to_dict() == c3.to_dict())
ra, _ = review_mod.review(review_mod.new_card(7), 3, when=T0)
rb, _ = review_mod.review(review_mod.new_card(7), 3, when=T0)
check("enable_fuzzing=False：同输入两次调度结果完全相等", ra.to_dict() == rb.to_dict())
try:
    review_mod.review(review_mod.new_card(8), 3, when=datetime(2026, 9, 19, 12))
    naive_rejected = False
except ValueError:
    naive_rejected = True
check("naive datetime 被拒绝（必须 tz-aware UTC）", naive_rejected)

# 间隔单调性：先两次 Good 推进 Review 态，此后每次恰好在到期时刻复习，间隔单调不减
cm = review_mod.new_card(9)
cm, _ = review_mod.review(cm, 3, when=T0)
cm, _ = review_mod.review(cm, 3, when=T0)
gaps = []
for _ in range(5):
    when = cm.due
    cm, _ = review_mod.review(cm, 3, when=when)
    gaps.append((cm.due - when).days)
check("Review 态后连续 Good：due 间隔单调不减", all(a <= b for a, b in zip(gaps, gaps[1:])), gaps)

# 9.2 卡片 API 全链路（QA 方向内，两种模式都跑，结束清理）
s, d9 = call("POST", "/api/directions", {"name": QA_DIR, "description": "V3 复习测试"})
check("复习临时方向 → 201", s == 201, s)
d9id = d9["id"]
s, _ = call("POST", f"/api/directions/{d9id}/cards", {"front": "   "})
check("空正面建卡 → 400", s == 400, s)
# 文本入参类型探针（Qoder 轮审缺陷 2 的卡片入口）
s, _ = call("POST", f"/api/directions/{d9id}/cards", {"front": 123})
check("卡片正面非字符串 → 400（原 500）", s == 400, s)
s, _ = call("POST", f"/api/directions/{d9id}/cards", {"front": "a", "back": {"x": 1}})
check("卡片背面为对象 → 400（原静默吞成空串）", s == 400, s)
made = []
for i in range(12):
    _, card = call("POST", f"/api/directions/{d9id}/cards", {"front": f"卡{i}", "back": f"答{i}"})
    made.append(card)
check("建 12 张卡 → fsrs.card_id 与表 id 对齐（docs/06 §3 决议）",
      all(c["fsrs"]["card_id"] == c["id"] for c in made), [c["fsrs"]["card_id"] for c in made[:3]])
s, cards9 = get(f"/api/directions/{d9id}/cards")
check("卡片列表 → 12 张且均为未复习新卡（state=1 / last_review 空）",
      len(cards9) == 12 and all(c["state"] == 1 and c["fsrs"]["last_review"] is None for c in cards9),
      len(cards9))
s, q9 = get(f"/api/directions/{d9id}/review/queue")
check("队列：12 张新卡截到每日上限 10（§9.2）",
      s == 200 and len(q9["queue"]) == 10 and q9["counts"]["new"] == 10
      and q9["counts"]["due"] == 0 and q9["counts"]["total"] == 12
      and q9["counts"]["done_today"] == 0, q9.get("counts"))
_, dirs9 = get("/api/directions")
check("列表徽标：新卡（due=创建时刻）不计入「今日待复习」",
      next(d for d in dirs9 if d["id"] == d9id)["due_today"] == 0,
      [d.get("due_today") for d in dirs9])

for bad in (0, 5, "3", True, None, 2.5):
    s, _ = call("POST", f"/api/cards/{made[0]['id']}/review", {"rating": bad})
    check(f"非法 rating {bad!r} → 400", s == 400, s)
s, _ = call("POST", "/api/cards/999999/review", {"rating": 3})
check("复习不存在的卡 → 404", s == 404, s)
s, _ = get("/api/directions/999999/cards")
check("不存在方向的卡片列表 → 404", s == 404, s)
s, _ = get("/api/directions/999999/review/queue")
check("不存在方向的队列 → 404", s == 404, s)

# 评分：Good → Learning（due +10 分钟）；再 Good → Review（due +2 天）；
# 两者 due 都在未来 → 移出当日队列、不出现在今日队列
s, r9 = call("POST", f"/api/cards/{made[0]['id']}/review", {"rating": 3})
check("评分 Good → 200，落 last_review 且 due 晚于现在",
      s == 200 and r9["state"] == 1 and r9["fsrs"]["last_review"] is not None
      and r9["due"] > datetime.now(timezone.utc).isoformat(), r9.get("due"))
_, q9b = get(f"/api/directions/{d9id}/review/queue")
check("评分后该卡移出当日队列（§9.2）",
      all(c["id"] != made[0]["id"] for c in q9b["queue"]), len(q9b["queue"]))
check("队列计数：done_today=1，新卡配额被当日消耗（10-1=9）",
      q9b["counts"]["done_today"] == 1 and q9b["counts"]["new"] == 9
      and len(q9b["queue"]) == 9, q9b["counts"])
s, r9b = call("POST", f"/api/cards/{made[0]['id']}/review", {"rating": 3})
check("再 Good → Review 态（明日之后到期）", s == 200 and r9b["state"] == 2, r9b.get("state"))
_, q9c = get(f"/api/directions/{d9id}/review/queue")
check("明日到期卡不出现在今日队列（§9.2）",
      all(c["id"] != made[0]["id"] for c in q9c["queue"]), len(q9c["queue"]))
_, bk9 = get("/api/export")
logs9 = [r for r in bk9["review_logs"] if r["card_id"] == made[0]["id"]]
check("评分落 review_logs（2 条、评分=3、UTC ISO）",
      len(logs9) == 2 and all(r["rating"] == 3 for r in logs9)
      and all(r["reviewed_at"].endswith("+00:00") for r in logs9), len(logs9))

# 服务端强制每日新卡上限（V4 用户审查 #5）：队列把 12 张新卡截到 10，但评分
# 接口原来不设防——直接调用可复习第 11 张。把当日配额耗尽（已耗 1，再评队列
# 剩余 9 张新卡）后，对队列外第 11 张直接评分 → 400
for c9 in made[1:10]:
    call("POST", f"/api/cards/{c9['id']}/review", {"rating": 3})
s, _ = call("POST", f"/api/cards/{made[10]['id']}/review", {"rating": 3})
check("配额耗尽后第 11 张新卡直接评分 → 400（原 200，绕过队列上限）", s == 400, s)
s, _ = call("POST", f"/api/cards/{made[0]['id']}/review", {"rating": 3})
check("配额耗尽不影响已学卡（last_review 非空）的后续评分", s == 200, s)

# 编辑 / 删除 / 级联
s, e9 = call("PATCH", f"/api/cards/{made[1]['id']}", {"front": "改过的正面", "back": "新背面"})
check("编辑卡片 → 200", s == 200 and e9["front"] == "改过的正面" and e9["back"] == "新背面", e9)
s, _ = call("PATCH", f"/api/cards/{made[1]['id']}", {"front": "  "})
check("编辑为空正面 → 400", s == 400, s)
s, _ = call("PATCH", f"/api/cards/{made[1]['id']}", {"front": 123})
check("编辑卡片正面非字符串 → 400（原 500）", s == 400, s)
s, _ = call("PATCH", f"/api/cards/{made[1]['id']}", {"back": []})
check("编辑卡片背面非字符串 → 400", s == 400, s)
s, _ = call("PATCH", "/api/cards/999999", {"front": "x"})
check("编辑不存在的卡 → 404", s == 404, s)
s, _ = call("DELETE", f"/api/cards/{made[1]['id']}")
check("删除卡片 → 200", s == 200, s)
_, cards9b = get(f"/api/directions/{d9id}/cards")
check("删除后卡片列表 11 张", len(cards9b) == 11, len(cards9b))
s, _ = call("DELETE", f"/api/cards/{made[1]['id']}")
check("重复删除 → 404", s == 404, s)
call("DELETE", f"/api/cards/{made[0]['id']}")
_, bk9b = get("/api/export")
check("删卡后其 review_logs 级联清除",
      all(r["card_id"] != made[0]["id"] for r in bk9b["review_logs"]), len(bk9b["review_logs"]))
call("DELETE", f"/api/directions/{d9id}")

# 9.3 / 9.4 仅临时库模式：
# 队列到期语义需要「已过期的复习态卡」—— API 只能向前调度，用显式过去时间
# 驱动 fsrs 后直写库（模拟时间流逝）；迁移测试则建独立的老库文件。
if _client is not None:
    import sqlite3

    s, d9c = call("POST", "/api/directions", {"name": QA_DIR, "description": "队列边界"})
    d9cid = d9c["id"]
    conn = sqlite3.connect(database.DB_PATH)
    cur9 = conn.execute(
        "INSERT INTO cards(direction_id, front, back, fsrs, due) VALUES (?, ?, ?, '', '')",
        (d9cid, "过期卡", "背面"))
    card = review_mod.new_card(cur9.lastrowid)
    past = datetime.now(timezone.utc) - timedelta(days=30)
    card, _ = review_mod.review(card, 3, when=past)
    card, _ = review_mod.review(card, 3, when=past)
    conn.execute("UPDATE cards SET fsrs = ?, due = ? WHERE id = ?",
                 (review_mod.card_to_json(card), review_mod.due_of(card), cur9.lastrowid))
    conn.commit()
    conn.close()
    call("POST", f"/api/directions/{d9cid}/cards", {"front": "新卡一张", "back": ""})
    s, q9d = get(f"/api/directions/{d9cid}/review/queue")
    check("过期 Review 卡计入 due 且排队列最前（到期优先）",
          s == 200 and q9d["counts"]["due"] == 1 and q9d["queue"][0]["front"] == "过期卡",
          q9d.get("counts"))
    check("新卡排在到期卡之后",
          len(q9d["queue"]) == 2 and q9d["queue"][1]["front"] == "新卡一张",
          [c["front"] for c in q9d["queue"]])
    _, dirs9d = get("/api/directions")
    check("列表徽标：过期卡计入「今日待复习」",
          next(d for d in dirs9d if d["id"] == d9cid)["due_today"] == 1,
          [d.get("due_today") for d in dirs9d])
    call("DELETE", f"/api/directions/{d9cid}")

    # 9.3b 坏 fsrs 不打死页面（Qoder 轮审缺陷 4，同一模式的第三次收口）：
    # 列表/队列/徽标/导出保持 200，坏行降级 corrupt 标记 → 前端渲染出删除按钮自救
    s, d9e = call("POST", "/api/directions", {"name": QA_DIR, "description": "坏卡自愈"})
    d9eid = d9e["id"]
    _, good_card = call("POST", f"/api/directions/{d9eid}/cards", {"front": "好卡", "back": "b"})
    _, bad_card = call("POST", f"/api/directions/{d9eid}/cards", {"front": "坏卡", "back": "b"})
    conn = sqlite3.connect(database.DB_PATH)
    conn.execute("UPDATE cards SET fsrs = '{bad json' WHERE id = ?", (bad_card["id"],))
    conn.commit()
    conn.close()
    s, cards_e = get(f"/api/directions/{d9eid}/cards")
    check("坏 fsrs 下 GET /cards 仍 200（原 500）", s == 200, s)
    row_bad = next((c for c in cards_e if c["id"] == bad_card["id"]), None)
    row_good = next((c for c in cards_e if c["id"] == good_card["id"]), None)
    check("坏行降级 corrupt 标记（state/fsrs 置空）",
          row_bad is not None and row_bad["corrupt"] is True
          and row_bad["state"] is None and row_bad["fsrs"] is None, row_bad)
    check("好行不受牵连",
          row_good is not None and row_good.get("corrupt") is None
          and row_good["state"] == 1, row_good)
    s, q_e = get(f"/api/directions/{d9eid}/review/queue")
    check("坏 fsrs 下队列仍 200 且坏卡不入队",
          s == 200 and all(c["id"] != bad_card["id"] for c in q_e["queue"]), s)
    s, _ = get("/api/directions")
    check("坏 fsrs 下方向列表（徽标聚合 json_valid 防护）仍 200", s == 200, s)
    s, _ = get("/api/export")
    check("坏 fsrs 下导出仍 200（备份是自救通道，坏行原样带出）", s == 200, s)
    s, _ = call("POST", f"/api/cards/{bad_card['id']}/review", {"rating": 3})
    check("复习坏卡 → 400（明确文案，非 500）", s == 400, s)
    s, _ = call("DELETE", f"/api/cards/{bad_card['id']}")
    check("删除坏卡 → 200（自救入口可用）", s == 200, s)
    call("DELETE", f"/api/directions/{d9eid}")

    # 9.4 自包含迁移测试（§9.3）：内联 v2.0 四表 SCHEMA —— 建库 → 塞旧数据 →
    # 当前 init_db() → 断言新表出现且旧数据完整。不依赖 git 历史文件。
    V2_SCHEMA = """
    CREATE TABLE IF NOT EXISTS directions (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        name        TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
    );
    CREATE TABLE IF NOT EXISTS phases (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        direction_id INTEGER NOT NULL REFERENCES directions(id) ON DELETE CASCADE,
        name         TEXT NOT NULL,
        goal         TEXT NOT NULL DEFAULT '',
        sort_order   INTEGER NOT NULL DEFAULT 0,
        created_at   TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
    );
    CREATE TABLE IF NOT EXISTS tasks (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        phase_id   INTEGER NOT NULL REFERENCES phases(id) ON DELETE CASCADE,
        title      TEXT NOT NULL,
        note       TEXT NOT NULL DEFAULT '',
        status     TEXT NOT NULL DEFAULT 'todo'
                   CHECK (status IN ('todo', 'doing', 'done', 'skipped')),
        sort_order INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
        done_at    TEXT
    );
    CREATE TABLE IF NOT EXISTS logs (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        direction_id INTEGER NOT NULL REFERENCES directions(id) ON DELETE CASCADE,
        date         TEXT NOT NULL,
        minutes      INTEGER NOT NULL DEFAULT 0,
        content      TEXT NOT NULL DEFAULT '',
        created_at   TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
    );
    CREATE INDEX IF NOT EXISTS idx_logs_dir_date ON logs(direction_id, date);
    CREATE INDEX IF NOT EXISTS idx_tasks_phase   ON tasks(phase_id);
    CREATE INDEX IF NOT EXISTS idx_phases_dir    ON phases(direction_id);
    """
    mig = tempfile.mkdtemp(prefix="study_tools_mig_")
    old_path = os.path.join(mig, "v2.db")
    conn = sqlite3.connect(old_path)
    conn.executescript(V2_SCHEMA)
    conn.execute("INSERT INTO directions(id, name, description) VALUES (1, '旧方向', 'v2 库')")
    conn.execute("INSERT INTO phases(id, direction_id, name, goal, sort_order) VALUES (1, 1, '旧阶段', '', 0)")
    conn.execute("INSERT INTO tasks(id, phase_id, title, status, sort_order, done_at) "
                 "VALUES (1, 1, '旧任务', 'done', 0, '2026-01-01')")
    conn.execute("INSERT INTO logs(id, direction_id, date, minutes, content) VALUES (1, 1, '2026-01-02', 30, '旧日志')")
    conn.execute("INSERT INTO logs(id, direction_id, date, minutes, content) VALUES (2, 1, '2026-01-02', 0, '零时长旧日志')")
    conn.commit()
    conn.close()

    saved_path, saved_dir = database.DB_PATH, database.DATA_DIR
    database.DB_PATH, database.DATA_DIR = old_path, mig
    database.init_db()   # 当前 SCHEMA 全 IF NOT EXISTS：v2 老库只补两张新表
    database.init_db()   # 幂等：模拟程序反复启动
    conn = sqlite3.connect(old_path)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    check("迁移：cards / review_logs 被补建（additive-only）",
          {"cards", "review_logs"} <= tables, sorted(tables))
    idxs = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    check("迁移：两张新索引被补建",
          {"idx_cards_dir_due", "idx_review_logs_card"} <= idxs, sorted(idxs))
    old_counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in ("directions", "phases", "tasks", "logs")}
    check("迁移：旧数据原样保留（1/1/1/2）",
          old_counts == {"directions": 1, "phases": 1, "tasks": 1, "logs": 2}, old_counts)
    row = conn.execute("SELECT title, status, done_at FROM tasks WHERE id = 1").fetchone()
    check("迁移：旧数据内容可查", row == ("旧任务", "done", "2026-01-01"), row)
    conn.close()
    database.DB_PATH, database.DATA_DIR = saved_path, saved_dir
    shutil.rmtree(mig, ignore_errors=True)
    s, _ = get("/api/directions")
    check("迁移测试未影响主测试库（DB_PATH 已复原）", s == 200, s)

    # docs/02 §6.5 验收自动化（Qoder 文档审计：文档写了但零测试）：
    # 对同一临时库文件重跑 init_db()（模拟程序重启建表）→ 数据完整保留
    _, dirs_re = get("/api/directions")
    n_restart = len(dirs_re)
    database.init_db()
    _, dirs_re2 = get("/api/directions")
    check("验收 §6.5：重启（重跑 init_db）后数据完整保留",
          n_restart > 0 and len(dirs_re2) == n_restart, (n_restart, len(dirs_re2)))
else:
    print("SKIP  真实服务模式跳过 9.3/9.4（直写库与迁移仅临时库模式）")

# ---------- 汇总 ----------

if _tmpdir and os.path.isdir(_tmpdir):
    shutil.rmtree(_tmpdir, ignore_errors=True)

print("\n" + "=" * 56)
print(f"模式：{'临时库 + test_client' if _client else '真实服务 ' + QA_URL}")
print(f"失败 {len(failures)} 项")
for f in failures:
    print("  FAIL -", f)
print("=" * 56)
sys.exit(1 if failures else 0)
