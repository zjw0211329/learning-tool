"""study tools —— 个人学习管理软件（MVP）

Flask 入口 + 全部 API 路由。接口设计见 docs/03-概要设计.md 第 4 节。
启动：python app/main.py  （默认 http://127.0.0.1:5000）
"""
import sqlite3
from datetime import date, timedelta

from flask import Flask, g, jsonify, request, send_from_directory

from database import close_db, get_db, init_db
from demo_data import insert_demo

app = Flask(__name__, static_folder="static", static_url_path="/static")
app.teardown_appcontext(close_db)

TASK_STATUSES = ("todo", "doing", "done", "skipped")


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
    """任务统计与进度：done / (total - skipped)。"""
    r = get_db().execute(
        """SELECT COUNT(*) AS total,
                  SUM(CASE WHEN status = 'done' THEN 1 ELSE 0 END) AS done,
                  SUM(CASE WHEN status = 'doing' THEN 1 ELSE 0 END) AS doing,
                  SUM(CASE WHEN status = 'skipped' THEN 1 ELSE 0 END) AS skipped
           FROM tasks
           WHERE phase_id IN (SELECT id FROM phases WHERE direction_id = ?)""",
        (direction_id,),
    ).fetchone()
    total, done = r["total"] or 0, r["done"] or 0
    skipped = r["skipped"] or 0
    denom = total - skipped
    progress = round(done / denom * 100) if denom > 0 else 0
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
    max_order = db.execute(
        "SELECT COALESCE(MAX(sort_order), -1) AS m FROM phases WHERE direction_id = ?",
        (direction_id,),
    ).fetchone()["m"]
    cur = db.execute(
        "INSERT INTO phases(direction_id, name, goal, sort_order) VALUES (?, ?, ?, ?)",
        (direction_id, name, (body.get("goal") or "").strip(), max_order + 1),
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
    max_order = db.execute(
        "SELECT COALESCE(MAX(sort_order), -1) AS m FROM tasks WHERE phase_id = ?",
        (phase_id,),
    ).fetchone()["m"]
    cur = db.execute(
        "INSERT INTO tasks(phase_id, title, note, sort_order) VALUES (?, ?, ?, ?)",
        (phase_id, title, (body.get("note") or "").strip(), max_order + 1),
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
        key = monday_of(date.fromisoformat(d_str)).isoformat()
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


# ---------- 启动 ----------

if __name__ == "__main__":
    init_db()
    # debug=False：个人本地工具无需热重载，也避免调试模式带来的安全隐患
    app.run(host="127.0.0.1", port=5000, debug=False)
