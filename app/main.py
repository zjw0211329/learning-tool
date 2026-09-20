"""study tools —— 个人学习管理软件（MVP）

Flask 入口 + 全部 API 路由。接口设计见 docs/03-概要设计.md 第 4 节。
启动：python app/main.py  （默认 http://127.0.0.1:5000）
"""
import json
import sqlite3
from datetime import date, datetime, timedelta, timezone

from flask import Flask, g, jsonify, request, send_from_directory

from database import close_db, get_db, init_db
from demo_data import insert_demo
from review import (
    REVIEW_NEW_LIMIT,
    card_from_json,
    card_to_json,
    due_of,
    is_valid_fsrs,
    new_card,
    review,
    utcnow,
)

app = Flask(__name__, static_folder="static", static_url_path="/static")
app.teardown_appcontext(close_db)

TASK_STATUSES = ("todo", "doing", "done", "skipped")

# 备份文件的元信息标识（import 时用于校验）。
# v2：新增 cards / review_logs 两表（V3 FR9）；v1 备份仍可导入（兼容矩阵见 docs/06 §5）
BACKUP_APP_ID = "study-tools"
BACKUP_VERSION = 2
ACCEPTED_VERSIONS = {1, 2}


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


def _day_start_utc_iso():
    """本地「今天 00:00」换算成 UTC ISO 串 —— 复习计数（今日已完成/今日新卡）的日界。

    review_logs 全存 UTC；日界若直接用 UTC 零点，东八区用户早上 8 点前的复习
    会被记到「昨天」。按本地日换算才符合用户对「今日」的直觉。
    """
    local_midnight = datetime.now().astimezone().replace(
        hour=0, minute=0, second=0, microsecond=0)
    return local_midnight.astimezone(timezone.utc).isoformat()


def _parse_minutes(value):
    """时长必须是严格的非负整数（bool 除外），否则返回 None。

    原实现 int(body.get(...)) 会把 25.5 静默截成 25、把 true 存成 1 —— 报错
    文案写着「必须是非负整数」却并不执行（Qoder TD1 附注的糊涂账）。现在浮点
    一律 400，与备份导入的 minutes 校验同口径。
    """
    return value if _is_int(value) and value >= 0 else None


# ---------- 静态页面 ----------

@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


# ---------- 方向 ----------

@app.get("/api/directions")
def list_directions():
    """方向列表。

    TD2 清偿：原先每方向 3 次查询（任务统计/日志汇总/活跃日期），N 个方向 3N+1 次；
    现在固定 5 条 SQL（方向本身 + 4 条全局聚合，V3 起多一条卡片徽标聚合），与方向数量无关。
    口径必须与单方向接口（roadmap/stats）完全一致，防回归断言见 tests [8]。
    """
    db = get_db()
    directions = rows_dicts(db.execute("SELECT * FROM directions ORDER BY id").fetchall())
    if not directions:
        return jsonify(directions)

    # 任务统计：total 口径与 direction_progress() 一致（不含 skipped）
    task_by_dir = {r["direction_id"]: r for r in rows_dicts(db.execute(
        """SELECT p.direction_id,
                  COUNT(*) AS total,
                  SUM(CASE WHEN t.status = 'done' THEN 1 ELSE 0 END) AS done,
                  SUM(CASE WHEN t.status = 'doing' THEN 1 ELSE 0 END) AS doing,
                  SUM(CASE WHEN t.status = 'skipped' THEN 1 ELSE 0 END) AS skipped
           FROM tasks t JOIN phases p ON p.id = t.phase_id
           GROUP BY p.direction_id""").fetchall())}

    # 日志汇总与活跃日期集合（DISTINCT 让行数从「日志总数」降到「方向数×活跃天数」）
    log_by_dir = {r["direction_id"]: r for r in rows_dicts(db.execute(
        """SELECT direction_id,
                  COALESCE(SUM(minutes), 0) AS total_minutes
           FROM logs GROUP BY direction_id""").fetchall())}
    dates_by_dir = {}
    for r in db.execute(
            "SELECT DISTINCT direction_id, date FROM logs WHERE minutes > 0 OR content != ''"):
        dates_by_dir.setdefault(r["direction_id"], set()).add(r["date"])

    # V3：今日待复习徽标（docs/06 §5）——一次聚合查全部方向，不在详情页逐方向查。
    # 只数「复习过且到期」的卡：新卡（last_review 为空）走每日上限配额、不算
    # 「待复习」，否则徽标会和复习视图的队列计数口径打架。
    now_iso = utcnow().isoformat()
    due_by_dir = {r["direction_id"]: r["n"] for r in db.execute(
        """SELECT direction_id, COUNT(*) AS n FROM cards
           WHERE due <= ? AND json_extract(fsrs, '$.last_review') IS NOT NULL
           GROUP BY direction_id""", (now_iso,)).fetchall()}

    for d in directions:
        t = task_by_dir.get(d["id"], {})
        total = (t.get("total") or 0) - (t.get("skipped") or 0)
        done = t.get("done") or 0
        d.update({
            "total": total,
            "done": done,
            "doing": t.get("doing") or 0,
            "skipped": t.get("skipped") or 0,
            "progress": round(done / total * 100) if total > 0 else 0,
        })
        d["total_minutes"] = log_by_dir.get(d["id"], {}).get("total_minutes", 0)
        dates = dates_by_dir.get(d["id"], set())
        d["active_days"] = len(dates)
        d["streak"] = calc_streak(dates)
        d["due_today"] = due_by_dir.get(d["id"], 0)
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
    raw_minutes = body.get("minutes")
    minutes = 0 if raw_minutes is None else _parse_minutes(raw_minutes)
    if minutes is None:
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
        minutes = _parse_minutes(body["minutes"])
        if minutes is None:
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


# ---------- 复习卡片（V3 FR9） ----------

def _card_view(row):
    """DB 行 → API 视图：fsrs 解析成对象并冗余 state 字段（前端列表/徽标用）。"""
    card = card_from_json(row["fsrs"])
    d = dict(row)
    d["fsrs"] = card.to_dict()
    d["state"] = card.state.value
    return d


@app.get("/api/directions/<int:direction_id>/cards")
def list_cards(direction_id):
    """卡片列表（FR9.1），含调度状态与 due。"""
    if get_direction_or_none(direction_id) is None:
        return not_found("方向不存在")
    rows = rows_dicts(get_db().execute(
        "SELECT * FROM cards WHERE direction_id = ? ORDER BY id", (direction_id,)
    ).fetchall())
    return jsonify([_card_view(r) for r in rows])


@app.post("/api/directions/<int:direction_id>/cards")
def create_card(direction_id):
    """新建卡片（FR9.1）：先插行拿 id，再写调度状态 —— Card(card_id=表id)
    对齐决议（docs/06 §3）要求构造状态时就得知道表 id。"""
    if get_direction_or_none(direction_id) is None:
        return not_found("方向不存在")
    body = request.get_json(silent=True) or {}
    front = (body.get("front") or "").strip()
    if not front:
        return bad_request("卡片正面不能为空")
    back = (body.get("back") or "").strip()
    db = get_db()
    cur = db.execute(
        "INSERT INTO cards(direction_id, front, back, fsrs, due) VALUES (?, ?, ?, '', '')",
        (direction_id, front, back),
    )
    card = new_card(cur.lastrowid)
    db.execute(
        "UPDATE cards SET fsrs = ?, due = ? WHERE id = ?",
        (card_to_json(card), due_of(card), cur.lastrowid),
    )
    db.commit()
    return jsonify(_card_view(row_dict(db.execute(
        "SELECT * FROM cards WHERE id = ?", (cur.lastrowid,)
    ).fetchone()))), 201


@app.patch("/api/cards/<int:card_id>")
def update_card(card_id):
    """编辑卡片正反面（FR9.1）。只动文本，不碰调度状态。"""
    db = get_db()
    card = row_dict(db.execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone())
    if card is None:
        return not_found("卡片不存在")
    body = request.get_json(silent=True) or {}
    front = (body.get("front") if "front" in body else card["front"]) or ""
    if not front.strip():
        return bad_request("卡片正面不能为空")
    back = body.get("back") if "back" in body else card["back"]
    db.execute(
        "UPDATE cards SET front = ?, back = ? WHERE id = ?",
        (front.strip(), (back or "").strip(), card_id),
    )
    db.commit()
    return jsonify(_card_view(row_dict(db.execute(
        "SELECT * FROM cards WHERE id = ?", (card_id,)
    ).fetchone())))


@app.delete("/api/cards/<int:card_id>")
def delete_card(card_id):
    """删除卡片（FR9.1），review_logs 经外键级联一并删除。"""
    db = get_db()
    cur = db.execute("DELETE FROM cards WHERE id = ?", (card_id,))
    db.commit()
    if cur.rowcount == 0:
        return not_found("卡片不存在")
    return jsonify({"ok": True})


@app.get("/api/directions/<int:direction_id>/review/queue")
def review_queue(direction_id):
    """今日复习队列与计数（FR9.2 / FR9.6）。

    到期卡（due ≤ now 且复习过）优先、全部进入队列；新卡按每日上限截取 ——
    上限的消耗量按「今天首评过的卡」计（该卡首条 review_logs 落在今天），
    这样上午复习过 10 张新卡后，晚上刷新不会再冒出新卡。
    """
    if get_direction_or_none(direction_id) is None:
        return not_found("方向不存在")
    db = get_db()
    now_iso = utcnow().isoformat()
    day_start = _day_start_utc_iso()
    rows = rows_dicts(db.execute(
        "SELECT * FROM cards WHERE direction_id = ? ORDER BY due, id", (direction_id,)
    ).fetchall())

    cards = [(r, card_from_json(r["fsrs"])) for r in rows]
    # 新卡的 due = 创建时刻，天然 ≤ now，但属于新卡配额而非「到期复习」
    due_cards = [r for r, c in cards if c.last_review is not None and r["due"] <= now_iso]
    new_cards = [r for r, c in cards if c.last_review is None]

    # 今日已引入的新卡数（限制在本方向内）：每张卡的首条复习记录落在今天
    introduced = db.execute(
        """SELECT COUNT(*) AS n FROM (
               SELECT l.card_id, MIN(l.reviewed_at) AS first_at
               FROM review_logs l JOIN cards c ON c.id = l.card_id
               WHERE c.direction_id = ?
               GROUP BY l.card_id)
           WHERE first_at >= ?""",
        (direction_id, day_start),
    ).fetchone()["n"]
    new_allowance = max(0, REVIEW_NEW_LIMIT - introduced)
    queue = due_cards + new_cards[:new_allowance]

    done_today = db.execute(
        """SELECT COUNT(*) AS n FROM review_logs l JOIN cards c ON c.id = l.card_id
           WHERE c.direction_id = ? AND l.reviewed_at >= ?""",
        (direction_id, day_start),
    ).fetchone()["n"]

    return jsonify({
        "queue": [_card_view(r) for r in queue],
        "counts": {
            "due": len(due_cards),
            "new": min(len(new_cards), new_allowance),
            "done_today": done_today,
            "total": len(rows),
        },
    })


@app.post("/api/cards/<int:card_id>/review")
def review_card(card_id):
    """评分一张卡（FR9.3~9.5）：FSRS 调度 → 写回状态与 due → 落 review_logs。

    reviewed_at 与调度用的 review_datetime 是同一个值 —— 复习记录和卡片状态
    严格对应同一次计算。
    """
    db = get_db()
    row = row_dict(db.execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone())
    if row is None:
        return not_found("卡片不存在")
    body = request.get_json(silent=True) or {}
    rating = body.get("rating")
    if not _is_int(rating) or rating not in (1, 2, 3, 4):
        return bad_request("rating 必须是 1~4 的整数（1 忘记 / 2 困难 / 3 良好 / 4 简单）")
    try:
        card = card_from_json(row["fsrs"])
    except ValueError:
        return bad_request("卡片调度状态数据损坏，无法复习")
    now = utcnow()
    card, _log = review(card, rating, when=now)
    db.execute(
        "UPDATE cards SET fsrs = ?, due = ? WHERE id = ?",
        (card_to_json(card), due_of(card), card_id),
    )
    db.execute(
        "INSERT INTO review_logs(card_id, rating, reviewed_at) VALUES (?, ?, ?)",
        (card_id, rating, now.isoformat()),
    )
    db.commit()
    return jsonify(_card_view(row_dict(db.execute(
        "SELECT * FROM cards WHERE id = ?", (card_id,)
    ).fetchone())))


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
    cards = []
    for r in rows_dicts(db.execute("SELECT * FROM cards ORDER BY id").fetchall()):
        # 备份里的 fsrs 存对象而非双重编码的 JSON 字符串（可读性，v2 格式约定）
        r["fsrs"] = json.loads(r["fsrs"])
        cards.append(r)
    payload = {
        "app": BACKUP_APP_ID,
        "version": BACKUP_VERSION,
        "exported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "directions": rows_dicts(db.execute("SELECT * FROM directions ORDER BY id")),
        "phases": rows_dicts(db.execute("SELECT * FROM phases ORDER BY id")),
        "tasks": rows_dicts(db.execute("SELECT * FROM tasks ORDER BY id")),
        "logs": rows_dicts(db.execute("SELECT * FROM logs ORDER BY id")),
        "cards": cards,
        "review_logs": rows_dicts(db.execute("SELECT * FROM review_logs ORDER BY id")),
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


def _is_utc_iso(value):
    """UTC ISO 8601（必须以 +00:00 结尾）。

    due / reviewed_at 的 TEXT 字典序比较只在同构字符串下可靠（docs/06 §3 决议）；
    混进 +08:00 之类的偏移串会悄悄破坏队列与徽标的比较语义。
    """
    if not isinstance(value, str) or not value.endswith("+00:00"):
        return False
    try:
        return datetime.fromisoformat(value).tzinfo == timezone.utc
    except ValueError:
        return False


def _is_rating(value):
    # Rating 枚举 1~4；True 是 int 子类，要排除
    return _is_int(value) and value in (1, 2, 3, 4)


_ABSENT = object()


def _opt(fn):
    """把校验器包装成「字段可缺省」：缺省放行，给了值就必须合法。"""
    def check(value):
        return True if value is _ABSENT else fn(value)
    return check


# 备份校验声明表（表驱动，Qoder 审查建议）：{表名: [(字段, 校验器, 中文标签), ...]}
# 每条记录还必须是 dict；跨表外键引用在 _valid_backup 里单独校验。
# 新增可导入字段时在这里加一行即可，不要回到手写 if 的老路（三轮审查漏网的教训）。
# 中文标签用于报错文案（NFR5：toast 是用户唯一能看到的诊断信息）。
_BACKUP_SPEC = {
    "directions": [
        ("id", _is_int, "ID"),
        ("name", _is_nonempty_str, "名称"),
        ("description", _opt(_is_optional_text), "描述"),
    ],
    "phases": [
        ("id", _is_int, "ID"),
        ("name", _is_nonempty_str, "名称"),
        ("goal", _opt(_is_optional_text), "目标"),
        ("sort_order", _opt(_is_sort_order), "排序序号"),
    ],
    "tasks": [
        ("id", _is_int, "ID"),
        ("title", _is_nonempty_str, "标题"),
        ("note", _opt(_is_optional_text), "备注"),
        ("status", _is_status, "状态"),
        ("sort_order", _opt(_is_sort_order), "排序序号"),
        ("done_at", _opt(_is_date_or_none), "完成时间"),
    ],
    "logs": [
        ("id", _is_int, "ID"),
        ("date", _is_iso_date, "日期"),
        ("minutes", _opt(_is_minutes), "时长"),
        ("content", _opt(_is_optional_text), "内容"),
    ],
    "cards": [
        ("id", _is_int, "ID"),
        ("front", _is_nonempty_str, "正面"),
        ("back", _opt(_is_optional_text), "背面"),
        ("fsrs", is_valid_fsrs, "调度状态"),
        ("due", _is_utc_iso, "到期时间"),
    ],
    "review_logs": [
        ("id", _is_int, "ID"),
        ("rating", _is_rating, "评分"),
        ("reviewed_at", _is_utc_iso, "复习时间"),
    ],
}

# TD3：模块加载即自检声明表形状——新增字段误写成两元素时启动即报，而不是导入时 500
for _t, _fields in _BACKUP_SPEC.items():
    assert all(isinstance(x, tuple) and len(x) == 3 for x in _fields), \
        f"_BACKUP_SPEC[{_t!r}] 存在非 (字段, 校验器, 中文标签) 三元组的条目"


def _valid_backup(body):
    """校验备份（表驱动）：元信息 → 版本矩阵 → 逐表逐字段 → 跨表外键。返回错误信息或 None。

    原则（FR7.2）：任何畸形输入都走 400，不能漏到 INSERT 阶段抛 KeyError/TypeError 变 500。
    报错文案用声明表里的中文标签（NFR5），用户不需要认识数据库字段名。
    版本兼容矩阵（docs/06 §5）：v1 备份没有 cards/review_logs，按空表放行；
    v2 备份两张表必须存在；版本高于本程序支持时文案明说（不再笼统报无效）。
    """
    if body.get("app") != BACKUP_APP_ID:
        return "不是有效的 study tools 备份文件"
    version = body.get("version")
    if version not in ACCEPTED_VERSIONS:
        if _is_int(version) and version > max(ACCEPTED_VERSIONS):
            return (f"备份版本 {version} 高于本程序支持的 {max(ACCEPTED_VERSIONS)}，"
                    "请升级程序后再导入")
        return "不是有效的 study tools 备份文件"
    if version == 1:
        # v1（V3 之前）的备份里没有这两张表：按空表放行，导入即清空（整库替换语义）
        body.setdefault("cards", [])
        body.setdefault("review_logs", [])

    for key in _BACKUP_SPEC:
        if not isinstance(body.get(key), list):
            return f"备份缺少 {key} 数据"

    for table, fields in _BACKUP_SPEC.items():
        for row in body[table]:
            if not isinstance(row, dict):
                return f"{table} 中存在非对象记录"
            for field, validator, label in fields:
                value = row.get(field, _ABSENT)
                if validator(value):
                    continue
                shown = "<缺失>" if value is _ABSENT else repr(value)
                return f"{table} 中存在非法{label}: {shown}"

    # 外键引用（跨表，进不了单表声明表）
    dir_ids = {d["id"] for d in body["directions"]}
    phase_ids = {p["id"] for p in body["phases"]}
    card_ids = {c["id"] for c in body["cards"]}
    for p in body["phases"]:
        if p.get("direction_id") not in dir_ids:
            return "phases 中存在指向不存在方向的记录"
    for t in body["tasks"]:
        if t.get("phase_id") not in phase_ids:
            return "tasks 中存在指向不存在阶段的记录"
    for l in body["logs"]:
        if l.get("direction_id") not in dir_ids:
            return "logs 中存在指向不存在方向的记录"
    for c in body["cards"]:
        if c.get("direction_id") not in dir_ids:
            return "cards 中存在指向不存在方向的记录"
        # 对齐决议与冗余一致性（docs/06 §3）：fsrs.card_id 必须等于表 id，
        # fsrs.due 必须与 due 列同源 —— 破坏任一条都会让队列与状态打架
        if c["fsrs"].get("card_id") != c["id"]:
            return "cards 中存在调度状态与卡片 ID 不一致的记录"
        if c["fsrs"].get("due") != c["due"]:
            return "cards 中存在调度状态与到期时间不一致的记录"
    for r in body["review_logs"]:
        if r.get("card_id") not in card_ids:
            return "review_logs 中存在指向不存在卡片的记录"
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
        db.execute("DELETE FROM review_logs")
        db.execute("DELETE FROM cards")
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
                 t.get("sort_order") or 0, t.get("created_at") or now, t["done_at"]))
        for l in body["logs"]:
            db.execute(
                "INSERT INTO logs(id, direction_id, date, minutes, content, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (l["id"], l["direction_id"], l["date"], l.get("minutes") or 0,
                 l.get("content") or "", l.get("created_at") or now))
        for c in body["cards"]:
            db.execute(
                "INSERT INTO cards(id, direction_id, front, back, fsrs, due, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (c["id"], c["direction_id"], c["front"], c.get("back") or "",
                 json.dumps(c["fsrs"]), c["due"], c.get("created_at") or now))
        for r in body["review_logs"]:
            db.execute(
                "INSERT INTO review_logs(id, card_id, rating, reviewed_at) VALUES (?, ?, ?, ?)",
                (r["id"], r["card_id"], r["rating"], r["reviewed_at"]))
        db.commit()
    except (sqlite3.Error, KeyError, TypeError, ValueError) as e:
        # 校验已挡掉绝大多数畸形数据；这里兜底，保证任何失败都是 400 + 回滚而不是 500
        db.rollback()
        return bad_request(f"导入失败，已回滚原数据：{e}")
    return jsonify({
        "ok": True,
        "counts": {k: len(body[k]) for k in
                   ("directions", "phases", "tasks", "logs", "cards", "review_logs")},
    })


# ---------- 启动 ----------

if __name__ == "__main__":
    init_db()
    # debug=False：个人本地工具无需热重载，也避免调试模式带来的安全隐患
    app.run(host="127.0.0.1", port=5000, debug=False)
