"""study tools —— 个人学习管理软件（MVP）

Flask 入口 + 全部 API 路由。接口设计见 docs/03-概要设计.md 第 4 节。
启动：python app/main.py  （默认 http://127.0.0.1:5000）
"""
import sqlite3
from datetime import date, datetime, timedelta

from flask import Flask, g, jsonify, request, send_from_directory

from database import close_db, get_db, init_db
from demo_data import insert_demo

app = Flask(__name__, static_folder="static", static_url_path="/static")
app.teardown_appcontext(close_db)

TASK_STATUSES = ("todo", "doing", "done", "skipped")

# 备份文件的元信息标识（import 时用于校验）
BACKUP_APP_ID = "study-tools"
BACKUP_VERSION = 1


# ---------- 小工具 ----------

def row_dict(row):
    return None if row is None else dict(row)


def rows_dicts(rows):
    return [dict(r) for r in rows]


def bad_request(msg):
    return jsonify({"error": msg}), 400


def not_found(msg="资源不存在"):
    return jsonify({"error": msg}), 404


def parse_date(text, field="date"):
    try:
        return date.fromisoformat(text)
    except (TypeError, ValueError):
        raise ValueError(f"{field} 格式应为 YYYY-MM-DD")


def get_direction_or_none(direction_id):
    return row_dict(get_db().execute(
        "SELECT * FROM directions WHERE id = ?", (direction_id,)
    ).fetchone())


def direction_progress(direction_id):
    """任务统计与进度。

    total 为有效任务数（不含已跳过），与阶段进度口径一致（审查发现的问题修复，
    见 tests/api_smoke.py 已知项）；进度 = done / total。
    """
    r = get_db().execute(
        """SELECT COUNT(*) AS total,
                  SUM(CASE WHEN status = 'done' THEN 1 ELSE 0 END) AS done,
                  SUM(CASE WHEN status = 'doing' THEN 1 ELSE 0 END) AS doing,
                  SUM(CASE WHEN status = 'skipped' THEN 1 ELSE 0 END) AS skipped
           FROM tasks
           WHERE phase_id IN (SELECT id FROM phases WHERE direction_id = ?)""",
        (direction_id,),
    ).fetchone()
    done = r["done"] or 0
    skipped = r["skipped"] or 0
    total = (r["total"] or 0) - skipped
    progress = round(done / total * 100) if total > 0 else 0
    return {"total": total, "done": done, "doing": r["doing"] or 0,
            "skipped": skipped, "progress": progress}


def active_dates(direction_id):
    """该方向「有学习记录」的日期集合（有时长或有内容）。"""
    rows = get_db().execute(
        """SELECT DISTINCT date FROM logs
           WHERE direction_id = ? AND (minutes > 0 OR content != '')""",
        (direction_id,),
    ).fetchall()
    return {r["date"] for r in rows}


def enrich_direction(d):
    """补充方向的学习统计字段（时长/活跃天数/连续天数）。"""
    d["total_minutes"] = get_db().execute(
        "SELECT COALESCE(SUM(minutes), 0) AS m FROM logs WHERE direction_id = ?",
        (d["id"],),
    ).fetchone()["m"]
    dates = active_dates(d["id"])
    d["active_days"] = len(dates)
    d["streak"] = calc_streak(dates)
    return d


def calc_streak(dates):
    """连续记录天数：从今天往前数，今天没记则从昨天开始。"""
    today = date.today()
    day = today if today.isoformat() in dates else today - timedelta(days=1)
    streak = 0
    while day.isoformat() in dates:
        streak += 1
        day -= timedelta(days=1)
    return streak


def monday_of(d):
    return d - timedelta(days=d.weekday())


def next_sort_order(db, table, peer_col, peer_id):
    """同组内下一个 sort_order = MAX + 1。

    表名/列名只允许代码内常量，不接请求参数（docs/05 §3.4）。
    库里万一有非整数的脏 sort_order（手工改库、历史数据），MAX() 会返回字符串，
    直接 +1 就是 str + int → TypeError → 500，把「添加任务」这种核心操作打死；
    所以这里退回按已有条数排。
    """
    raw = db.execute(
        f"SELECT COALESCE(MAX(sort_order), -1) AS m FROM {table} WHERE {peer_col} = ?",
        (peer_id,),
    ).fetchone()["m"]
    if not _is_int(raw):
        raw = db.execute(
            f"SELECT COUNT(*) AS n FROM {table} WHERE {peer_col} = ?", (peer_id,)
        ).fetchone()["n"] - 1
    return raw + 1


# ---------- 静态页面 ----------

@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


# ---------- 方向 ----------

@app.get("/api/directions")
def list_directions():
    directions = rows_dicts(get_db().execute(
        "SELECT * FROM directions ORDER BY id"
    ).fetchall())
    for d in directions:
        d.update(direction_progress(d["id"]))
        enrich_direction(d)
    return jsonify(directions)


@app.post("/api/directions")
def create_direction():
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    if not name:
        return bad_request("方向名称不能为空")
    db = get_db()
    cur = db.execute(
        "INSERT INTO directions(name, description) VALUES (?, ?)",
        (name, (body.get("description") or "").strip()),
    )
    db.commit()
    return jsonify(row_dict(db.execute(
        "SELECT * FROM directions WHERE id = ?", (cur.lastrowid,)
    ).fetchone())), 201


@app.patch("/api/directions/<int:direction_id>")
def update_direction(direction_id):
    if get_direction_or_none(direction_id) is None:
        return not_found("方向不存在")
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    if not name:
        return bad_request("方向名称不能为空")
    db = get_db()
    db.execute(
        "UPDATE directions SET name = ?, description = ? WHERE id = ?",
        (name, (body.get("description") or "").strip(), direction_id),
    )
    db.commit()
    return jsonify({"ok": True})


@app.delete("/api/directions/<int:direction_id>")
def delete_direction(direction_id):
    db = get_db()
    cur = db.execute("DELETE FROM directions WHERE id = ?", (direction_id,))
    db.commit()
    if cur.rowcount == 0:
        return not_found("方向不存在")
    return jsonify({"ok": True})


# ---------- 路线图：阶段与任务 ----------

@app.get("/api/directions/<int:direction_id>/roadmap")
def get_roadmap(direction_id):
    direction = get_direction_or_none(direction_id)
    if direction is None:
        return not_found("方向不存在")
    phases = rows_dicts(get_db().execute(
        "SELECT * FROM phases WHERE direction_id = ? ORDER BY sort_order, id",
        (direction_id,),
    ).fetchall())
    for phase in phases:
        tasks = rows_dicts(get_db().execute(
            "SELECT * FROM tasks WHERE phase_id = ? ORDER BY sort_order, id",
            (phase["id"],),
        ).fetchall())
        phase["tasks"] = tasks
        total = len([t for t in tasks if t["status"] != "skipped"])
        done = len([t for t in tasks if t["status"] == "done"])
        phase["total"], phase["done"] = total, done
        phase["progress"] = round(done / total * 100) if total else 0
    direction.update(direction_progress(direction_id))
    enrich_direction(direction)
    return jsonify({"direction": direction, "phases": phases})


@app.post("/api/directions/<int:direction_id>/phases")
def create_phase(direction_id):
    if get_direction_or_none(direction_id) is None:
        return not_found("方向不存在")
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    if not name:
        return bad_request("阶段名称不能为空")
    db = get_db()
    new_order = next_sort_order(db, "phases", "direction_id", direction_id)
    cur = db.execute(
        "INSERT INTO phases(direction_id, name, goal, sort_order) VALUES (?, ?, ?, ?)",
        (direction_id, name, (body.get("goal") or "").strip(), new_order),
    )
    db.commit()
    return jsonify(row_dict(db.execute(
        "SELECT * FROM phases WHERE id = ?", (cur.lastrowid,)
    ).fetchone())), 201


@app.patch("/api/phases/<int:phase_id>")
def update_phase(phase_id):
    db = get_db()
    if row_dict(db.execute("SELECT id FROM phases WHERE id = ?", (phase_id,)).fetchone()) is None:
        return not_found("阶段不存在")
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    if not name:
        return bad_request("阶段名称不能为空")
    db.execute(
        "UPDATE phases SET name = ?, goal = ? WHERE id = ?",
        (name, (body.get("goal") or "").strip(), phase_id),
    )
    db.commit()
    return jsonify({"ok": True})


@app.delete("/api/phases/<int:phase_id>")
def delete_phase(phase_id):
    db = get_db()
    cur = db.execute("DELETE FROM phases WHERE id = ?", (phase_id,))
    db.commit()
    if cur.rowcount == 0:
        return not_found("阶段不存在")
    return jsonify({"ok": True})


@app.post("/api/phases/<int:phase_id>/tasks")
def create_task(phase_id):
    db = get_db()
    if row_dict(db.execute("SELECT id FROM phases WHERE id = ?", (phase_id,)).fetchone()) is None:
        return not_found("阶段不存在")
    body = request.get_json(silent=True) or {}
    title = (body.get("title") or "").strip()
    if not title:
        return bad_request("任务标题不能为空")
    new_order = next_sort_order(db, "tasks", "phase_id", phase_id)
    cur = db.execute(
        "INSERT INTO tasks(phase_id, title, note, sort_order) VALUES (?, ?, ?, ?)",
        (phase_id, title, (body.get("note") or "").strip(), new_order),
    )
    db.commit()
    return jsonify(row_dict(db.execute(
        "SELECT * FROM tasks WHERE id = ?", (cur.lastrowid,)
    ).fetchone())), 201


@app.patch("/api/tasks/<int:task_id>")
def update_task(task_id):
    db = get_db()
    task = row_dict(db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone())
    if task is None:
        return not_found("任务不存在")
    body = request.get_json(silent=True) or {}
    status = body.get("status")
    if status is not None:
        if status not in TASK_STATUSES:
            return bad_request(f"status 必须是 {'/'.join(TASK_STATUSES)} 之一")
        # 状态变更记录完成时间；取消完成则清空
        done_at = task["done_at"]
        if status == "done" and task["status"] != "done":
            done_at = date.today().isoformat()
        elif status != "done":
            done_at = None
        db.execute(
            "UPDATE tasks SET status = ?, done_at = ? WHERE id = ?",
            (status, done_at, task_id),
        )
    if "title" in body or "note" in body:
        title = (body.get("title") if "title" in body else task["title"]) or ""
        if not title.strip():
            return bad_request("任务标题不能为空")
        note = body.get("note") if "note" in body else task["note"]
        db.execute(
            "UPDATE tasks SET title = ?, note = ? WHERE id = ?",
            (title.strip(), note or "", task_id),
        )
    db.commit()
    return jsonify(row_dict(db.execute(
        "SELECT * FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()))


@app.delete("/api/tasks/<int:task_id>")
def delete_task(task_id):
    db = get_db()
    cur = db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    db.commit()
    if cur.rowcount == 0:
        return not_found("任务不存在")
    return jsonify({"ok": True})


# ---------- 排序与移动（V2 FR6）----------

def swap_order(table, peer_col, item_id, direction):
    """同级内上移/下移：按 (sort_order, id) 的当前次序与相邻项交换位置。

    直接重写同级全部 sort_order 为 0..n-1，天然规避重复值导致的死循环。
    """
    db = get_db()
    item = row_dict(db.execute(f"SELECT * FROM {table} WHERE id = ?", (item_id,)).fetchone())
    if item is None:
        return not_found("目标不存在")
    siblings = rows_dicts(db.execute(
        f"SELECT id FROM {table} WHERE {peer_col} = ? ORDER BY sort_order, id",
        (item[peer_col],),
    ))
    idx = next((i for i, s in enumerate(siblings) if s["id"] == item_id), None)
    if idx is None:
        return not_found("目标不存在")
    j = idx - 1 if direction == "up" else idx + 1
    if 0 <= j < len(siblings):
        order = [s["id"] for s in siblings]
        order[idx], order[j] = order[j], order[idx]
        for pos, oid in enumerate(order):
            db.execute(f"UPDATE {table} SET sort_order = ? WHERE id = ?", (pos, oid))
        db.commit()
    return jsonify({"ok": True})


@app.post("/api/phases/<int:phase_id>/reorder")
def reorder_phase(phase_id):
    direction = (request.get_json(silent=True) or {}).get("direction")
    if direction not in ("up", "down"):
        return bad_request("direction 必须是 up 或 down")
    return swap_order("phases", "direction_id", phase_id, direction)


@app.post("/api/tasks/<int:task_id>/reorder")
def reorder_task(task_id):
    direction = (request.get_json(silent=True) or {}).get("direction")
    if direction not in ("up", "down"):
        return bad_request("direction 必须是 up 或 down")
    return swap_order("tasks", "phase_id", task_id, direction)


@app.post("/api/tasks/<int:task_id>/move")
def move_task(task_id):
    db = get_db()
    task = row_dict(db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone())
    if task is None:
        return not_found("任务不存在")
    body = request.get_json(silent=True) or {}
    try:
        target_phase_id = int(body.get("phase_id"))
    except (TypeError, ValueError):
        return bad_request("phase_id 不能为空")
    cur_phase = row_dict(db.execute(
        "SELECT direction_id FROM phases WHERE id = ?", (task["phase_id"],)).fetchone())
    target_phase = row_dict(db.execute(
        "SELECT direction_id FROM phases WHERE id = ?", (target_phase_id,)).fetchone())
    if target_phase is None:
        return not_found("目标阶段不存在")
    if cur_phase["direction_id"] != target_phase["direction_id"]:
        return bad_request("只能在同一方向内的阶段之间移动任务")
    new_order = next_sort_order(db, "tasks", "phase_id", target_phase_id)
    db.execute(
        "UPDATE tasks SET phase_id = ?, sort_order = ? WHERE id = ?",
        (target_phase_id, new_order, task_id),
    )
    db.commit()
    return jsonify({"ok": True})


# ---------- 学习日志 ----------

@app.get("/api/directions/<int:direction_id>/logs")
def list_logs(direction_id):
    if get_direction_or_none(direction_id) is None:
        return not_found("方向不存在")
    sql = "SELECT * FROM logs WHERE direction_id = ?"
    params = [direction_id]
    try:
        if request.args.get("from"):
            sql += " AND date >= ?"
            params.append(parse_date(request.args["from"], "from").isoformat())
        if request.args.get("to"):
            sql += " AND date <= ?"
            params.append(parse_date(request.args["to"], "to").isoformat())
    except ValueError as e:
        return bad_request(str(e))
    sql += " ORDER BY date DESC, id DESC"
    if request.args.get("limit"):
        try:
            limit = int(request.args["limit"])
        except ValueError:
            return bad_request("limit 必须是正整数")
        if limit <= 0:
            return bad_request("limit 必须是正整数")
        sql += " LIMIT ?"
        params.append(limit)
    return jsonify(rows_dicts(get_db().execute(sql, params).fetchall()))


@app.post("/api/directions/<int:direction_id>/logs")
def create_log(direction_id):
    if get_direction_or_none(direction_id) is None:
        return not_found("方向不存在")
    body = request.get_json(silent=True) or {}
    try:
        day = parse_date(body.get("date") or date.today().isoformat()).isoformat()
    except ValueError as e:
        return bad_request(str(e))
    try:
        minutes = int(body.get("minutes") or 0)
    except (TypeError, ValueError):
        return bad_request("时长必须是非负整数（分钟）")
    if minutes < 0:
        return bad_request("时长必须是非负整数（分钟）")
    content = (body.get("content") or "").strip()
    if minutes == 0 and not content:
        return bad_request("时长和内容至少填一项")
    db = get_db()
    cur = db.execute(
        "INSERT INTO logs(direction_id, date, minutes, content) VALUES (?, ?, ?, ?)",
        (direction_id, day, minutes, content),
    )
    db.commit()
    return jsonify(row_dict(db.execute(
        "SELECT * FROM logs WHERE id = ?", (cur.lastrowid,)
    ).fetchone())), 201


@app.patch("/api/logs/<int:log_id>")
def update_log(log_id):
    db = get_db()
    log = row_dict(db.execute("SELECT * FROM logs WHERE id = ?", (log_id,)).fetchone())
    if log is None:
        return not_found("日志不存在")
    body = request.get_json(silent=True) or {}
    new_date = log["date"]
    if "date" in body:
        try:
            new_date = parse_date(body["date"]).isoformat()
        except ValueError as e:
            return bad_request(str(e))
    minutes = log["minutes"]
    if "minutes" in body:
        try:
            minutes = int(body["minutes"] or 0)
        except (TypeError, ValueError):
            return bad_request("时长必须是非负整数（分钟）")
        if minutes < 0:
            return bad_request("时长必须是非负整数（分钟）")
    content = (body.get("content") if "content" in body else log["content"]) or ""
    content = content.strip()
    if minutes == 0 and not content:
        return bad_request("时长和内容至少填一项")
    db.execute(
        "UPDATE logs SET date = ?, minutes = ?, content = ? WHERE id = ?",
        (new_date, minutes, content, log_id),
    )
    db.commit()
    return jsonify({"ok": True})


@app.delete("/api/logs/<int:log_id>")
def delete_log(log_id):
    db = get_db()
    cur = db.execute("DELETE FROM logs WHERE id = ?", (log_id,))
    db.commit()
    if cur.rowcount == 0:
        return not_found("日志不存在")
    return jsonify({"ok": True})


# ---------- 统计与回顾 ----------

@app.get("/api/directions/<int:direction_id>/stats")
def get_stats(direction_id):
    if get_direction_or_none(direction_id) is None:
        return not_found("方向不存在")

    task_stat = direction_progress(direction_id)

    # 阶段进度
    phases = rows_dicts(get_db().execute(
        "SELECT id, name, goal FROM phases WHERE direction_id = ? ORDER BY sort_order, id",
        (direction_id,),
    ).fetchall())
    for phase in phases:
        r = get_db().execute(
            """SELECT COUNT(*) AS total,
                      SUM(CASE WHEN status = 'done' THEN 1 ELSE 0 END) AS done,
                      SUM(CASE WHEN status = 'skipped' THEN 1 ELSE 0 END) AS skipped
               FROM tasks WHERE phase_id = ?""",
            (phase["id"],),
        ).fetchone()
        total = (r["total"] or 0) - (r["skipped"] or 0)
        done = r["done"] or 0
        phase["total"], phase["done"] = total, done
        phase["progress"] = round(done / total * 100) if total else 0

    today = date.today()
    start = (today - timedelta(days=364)).isoformat()
    daily = rows_dicts(get_db().execute(
        """SELECT date, SUM(minutes) AS minutes FROM logs
           WHERE direction_id = ? AND date >= ? GROUP BY date""",
        (direction_id, start),
    ).fetchall())
    heatmap = {r["date"]: r["minutes"] for r in daily}

    # 近 12 周逐周时长（周一为首）
    this_monday = monday_of(today)
    weeks = []
    for i in range(11, -1, -1):
        week_start = this_monday - timedelta(weeks=i)
        weeks.append({"week_start": week_start.isoformat(), "minutes": 0})
    week_index = {w["week_start"]: w for w in weeks}
    for d_str, minutes in heatmap.items():
        if not minutes:
            continue
        try:
            week_day = date.fromisoformat(d_str)
        except ValueError:
            # 库里万一混入非法日期（历史脏数据）时跳过，一条坏数据不该打死整个统计页
            continue
        key = monday_of(week_day).isoformat()
        if key in week_index:
            week_index[key]["minutes"] += minutes

    dates = active_dates(direction_id)
    total_minutes = get_db().execute(
        "SELECT COALESCE(SUM(minutes), 0) AS m FROM logs WHERE direction_id = ?",
        (direction_id,),
    ).fetchone()["m"]

    return jsonify({
        "tasks": task_stat,
        "phases": phases,
        "total_minutes": total_minutes,
        "active_days": len(dates),
        "streak": calc_streak(dates),
        "heatmap": [{"date": d, "minutes": m} for d, m in heatmap.items()],
        "weekly": weeks,
    })


@app.get("/api/directions/<int:direction_id>/review")
def get_review(direction_id):
    if get_direction_or_none(direction_id) is None:
        return not_found("方向不存在")
    try:
        day = parse_date(request.args.get("date") or date.today().isoformat())
    except ValueError as e:
        return bad_request(str(e))
    week_start, week_end = monday_of(day), monday_of(day) + timedelta(days=6)
    s, e = week_start.isoformat(), week_end.isoformat()

    logs = rows_dicts(get_db().execute(
        """SELECT * FROM logs WHERE direction_id = ? AND date BETWEEN ? AND ?
           ORDER BY date, id""",
        (direction_id, s, e),
    ).fetchall())
    done_tasks = rows_dicts(get_db().execute(
        """SELECT t.id, t.title, t.done_at, p.name AS phase_name
           FROM tasks t JOIN phases p ON p.id = t.phase_id
           WHERE p.direction_id = ? AND t.status = 'done' AND t.done_at BETWEEN ? AND ?
           ORDER BY t.done_at""",
        (direction_id, s, e),
    ).fetchall())

    days = []
    for i in range(7):
        d = (week_start + timedelta(days=i)).isoformat()
        day_logs = [l for l in logs if l["date"] == d]
        days.append({
            "date": d,
            "minutes": sum(l["minutes"] for l in day_logs),
            "logs": day_logs,
        })

    return jsonify({
        "week_start": s,
        "week_end": e,
        "total_minutes": sum(l["minutes"] for l in logs),
        "days": days,
        "done_tasks": done_tasks,
    })


# ---------- 示例数据 ----------

@app.post("/api/demo")
def load_demo():
    db = get_db()
    existing = db.execute("SELECT COUNT(*) AS n FROM directions").fetchone()["n"]
    if existing > 0:
        return bad_request("已有数据，为避免混淆不重复载入示例（如需演示请先删除现有方向）")
    insert_demo(db)
    db.commit()
    return jsonify({"ok": True}), 201


# ---------- 数据导出 / 导入（V2 FR7）----------

@app.get("/api/export")
def export_data():
    db = get_db()
    payload = {
        "app": BACKUP_APP_ID,
        "version": BACKUP_VERSION,
        "exported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "directions": rows_dicts(db.execute("SELECT * FROM directions ORDER BY id")),
        "phases": rows_dicts(db.execute("SELECT * FROM phases ORDER BY id")),
        "tasks": rows_dicts(db.execute("SELECT * FROM tasks ORDER BY id")),
        "logs": rows_dicts(db.execute("SELECT * FROM logs ORDER BY id")),
    }
    resp = jsonify(payload)
    if request.args.get("download"):
        resp.headers["Content-Disposition"] = "attachment; filename=study-tools-backup.json"
    return resp


def _is_int(value):
    """bool 是 int 的子类，这里要排除掉（True 当 1 存进库是隐性脏数据）。"""
    return isinstance(value, int) and not isinstance(value, bool)


def _is_iso_date(text):
    """必须是规范的 YYYY-MM-DD 且是真实存在的日历日期。

    只用正则校验形状会放过 2026-13-45 这种，导入后 /api/stats 里的
    date.fromisoformat 会抛 ValueError 把统计页打死。
    round-trip 相等用于拒绝 ISO 周日期等可解析但不规范的形式
    （如 2026-W01-1，能被 fromisoformat 解析但不符合应用的日期字符串约定）。
    """
    if not isinstance(text, str) or len(text) != 10:
        return False
    try:
        return date.fromisoformat(text).isoformat() == text
    except ValueError:
        return False


def _is_nonempty_str(value):
    return isinstance(value, str) and value.strip() != ""


def _is_optional_text(value):
    """可选文本字段：None 或字符串放行，其它类型（数字/对象）拒绝。"""
    return value is None or isinstance(value, str)


def _is_sort_order(value):
    # None 放行（导入时按 0 处理）；字符串会导致之后 MAX(sort_order)+1 变 str+int → 500
    return value is None or _is_int(value)


def _is_minutes(value):
    # minutes 不允许 None（与 sort_order 的历史行为刻意不同，保持不变）
    return _is_int(value) and value >= 0


def _is_status(value):
    return value in TASK_STATUSES


def _is_date_or_none(value):
    return value is None or _is_iso_date(value)


_ABSENT = object()


def _opt(fn):
    """把校验器包装成「字段可缺省」：缺省放行，给了值就必须合法。"""
    def check(value):
        return True if value is _ABSENT else fn(value)
    return check


# 备份校验声明表（表驱动，Qoder 审查建议）：{表名: [(字段, 校验器), ...]}
# 每条记录还必须是 dict；跨表外键引用在 _valid_backup 里单独校验。
# 新增可导入字段时在这里加一行即可，不要回到手写 if 的老路（三轮审查漏网的教训）。
_BACKUP_SPEC = {
    "directions": [
        ("id", _is_int),
        ("name", _is_nonempty_str),
        ("description", _opt(_is_optional_text)),
    ],
    "phases": [
        ("id", _is_int),
        ("name", _is_nonempty_str),
        ("goal", _opt(_is_optional_text)),
        ("sort_order", _opt(_is_sort_order)),
    ],
    "tasks": [
        ("id", _is_int),
        ("title", _is_nonempty_str),
        ("note", _opt(_is_optional_text)),
        ("status", _is_status),
        ("sort_order", _opt(_is_sort_order)),
        ("done_at", _opt(_is_date_or_none)),
    ],
    "logs": [
        ("id", _is_int),
        ("date", _is_iso_date),
        ("minutes", _opt(_is_minutes)),
        ("content", _opt(_is_optional_text)),
    ],
}


def _valid_backup(body):
    """校验备份（表驱动）：元信息 → 逐表逐字段跑声明表 → 跨表外键。返回错误信息或 None。

    原则（FR7.2）：任何畸形输入都走 400，不能漏到 INSERT 阶段抛 KeyError/TypeError 变 500。
    """
    if body.get("app") != BACKUP_APP_ID or body.get("version") != BACKUP_VERSION:
        return "不是有效的 study tools 备份文件"
    for key in _BACKUP_SPEC:
        if not isinstance(body.get(key), list):
            return f"备份缺少 {key} 数据"

    for table, fields in _BACKUP_SPEC.items():
        for row in body[table]:
            if not isinstance(row, dict):
                return f"{table} 中存在非对象记录"
            for field, validator in fields:
                value = row.get(field, _ABSENT)
                if validator(value):
                    continue
                shown = "<缺失>" if value is _ABSENT else repr(value)
                return f"{table} 中存在非法 {field}: {shown}"

    # 外键引用（跨表，进不了单表声明表）
    dir_ids = {d["id"] for d in body["directions"]}
    phase_ids = {p["id"] for p in body["phases"]}
    for p in body["phases"]:
        if p.get("direction_id") not in dir_ids:
            return "phases 中存在指向不存在方向的记录"
    for t in body["tasks"]:
        if t.get("phase_id") not in phase_ids:
            return "tasks 中存在指向不存在阶段的记录"
    for l in body["logs"]:
        if l.get("direction_id") not in dir_ids:
            return "logs 中存在指向不存在方向的记录"
    return None


@app.post("/api/import")
def import_data():
    body = request.get_json(silent=True) or {}
    err = _valid_backup(body)
    if err:
        return bad_request(err)

    db = get_db()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        # 单事务整库替换：先清后写，任何一步失败整体回滚
        db.execute("DELETE FROM logs")
        db.execute("DELETE FROM tasks")
        db.execute("DELETE FROM phases")
        db.execute("DELETE FROM directions")
        for d in body["directions"]:
            db.execute(
                "INSERT INTO directions(id, name, description, created_at) VALUES (?, ?, ?, ?)",
                (d["id"], d["name"], d.get("description") or "", d.get("created_at") or now))
        for p in body["phases"]:
            db.execute(
                "INSERT INTO phases(id, direction_id, name, goal, sort_order, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (p["id"], p["direction_id"], p["name"], p.get("goal") or "",
                 p.get("sort_order") or 0, p.get("created_at") or now))
        for t in body["tasks"]:
            db.execute(
                "INSERT INTO tasks(id, phase_id, title, note, status, sort_order, created_at, done_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (t["id"], t["phase_id"], t["title"], t.get("note") or "", t["status"],
                 t.get("sort_order") or 0, t.get("created_at") or now, t.get("done_at")))
        for l in body["logs"]:
            db.execute(
                "INSERT INTO logs(id, direction_id, date, minutes, content, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (l["id"], l["direction_id"], l["date"], l.get("minutes") or 0,
                 l.get("content") or "", l.get("created_at") or now))
        db.commit()
    except (sqlite3.Error, KeyError, TypeError, ValueError) as e:
        # 校验已挡掉绝大多数畸形数据；这里兜底，保证任何失败都是 400 + 回滚而不是 500
        db.rollback()
        return bad_request(f"导入失败，已回滚原数据：{e}")
    return jsonify({
        "ok": True,
        "counts": {k: len(body[k]) for k in ("directions", "phases", "tasks", "logs")},
    })


# ---------- 启动 ----------

if __name__ == "__main__":
    init_db()
    # debug=False：个人本地工具无需热重载，也避免调试模式带来的安全隐患
    app.run(host="127.0.0.1", port=5000, debug=False)
