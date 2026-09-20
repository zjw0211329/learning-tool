"""「嵌入式」示例数据生成，用于首次演示与验收（见《02》验收标准第 1 条）。

V3（FR9.7）：附带 6 张复习卡。所有时间一律 datetime.now(timezone.utc) ± timedelta
动态生成 —— 写死日期会让演示库随时间腐烂（到期卡永远停在演示当天）。
"""
import random
from datetime import date, timedelta

from review import card_to_json, due_of, new_card, review, utcnow

PHASES = [
    ("阶段一 · C 语言与计算机基础", "能把指针、结构体、内存模型讲清楚", [
        "复习指针与数组的关系，写 5 个小程序",
        "学习结构体、联合体与内存对齐",
        "搞懂栈与堆：手写一个简易动态内存分配器",
        "阅读《C 和指针》第 1~6 章",
    ]),
    ("阶段二 · 单片机入门（STM32）", "点亮 LED 到串口通信，脱离教程独立开发", [
        "搭建开发环境：Keil / STM32CubeIDE",
        "GPIO 点灯 + 按键消抖",
        "外部中断与定时器",
        "串口通信：printf 重定向与收发协议",
        "ADC 采集 + PWM 控制呼吸灯",
    ]),
    ("阶段三 · RTOS 与驱动开发", "理解任务调度，能写简单驱动", [
        "学习 FreeRTOS 任务、队列、信号量",
        "移植 FreeRTOS 到自己的板子",
        "阅读 GPIO / UART 外设手册，写裸寄存器驱动",
        "I2C / SPI 驱动 OLED 屏幕",
    ]),
    ("阶段四 · 项目实战", "完成一个可展示的综合项目", [
        "选题：环境监测小站（温湿度 + OLED + 上位机）",
        "画原理图、焊面包板原型",
        "固件开发与联调",
        "写项目总结博客",
    ]),
]

TIL_SAMPLES = [
    "看了指针章节，&和*终于理顺了，但二级指针还是绕，画了内存图才懂。",
    "GPIO 点灯成功！踩坑：忘了开 RCC 时钟，灯一直不亮，查了两小时。",
    "中断服务函数里不能延时，否则 HardFault，改成置标志位在主循环处理。",
    "串口乱码原来是波特率配错，时钟树没算对，重新算了 PLL 分频。",
    "FreeRTOS 的队列是拷贝传值不是传指针，传大结构体要小心栈溢出。",
    "I2C 总线卡死，学会了九宮格恢复法：手动给 SCL 打 9 个时钟。",
    "今天只看了 20 分钟书，效率一般，明天把手机放客厅。",
    "焊了一下排针，第一次焊得歪歪扭扭，第二排就顺了。",
]

# 已复习的 4 张：同一时刻两次 Good 把卡推进 Review 态（due = 时刻 + 2 天），
# 复习时刻回推 30 天 → due 已过期 28 天，稳稳出现在「今日待复习」里。
_REVIEWED_CARDS = [
    ("指针和数组的关系是什么？", "数组名在表达式中会退化为指向首元素的指针；sizeof 与取地址 & 是例外。"),
    ("struct 为什么要内存对齐？", "CPU 按对齐边界访问内存更快；结构体总大小需为最大对齐值的整数倍。"),
    ("GPIO 点灯不亮，最先该查什么？", "RCC 时钟是否使能 —— 外设没有时钟，写寄存器不生效。"),
    ("中断服务函数里为什么不能延时？", "阻塞会饿死同级中断；应置标志位，处理放回主循环。"),
]

# 新卡 2 张：从未复习（fsrs v6 无 New 态，即 Learning + last_review 为空）。
_NEW_CARDS = [
    ("FreeRTOS 队列传值还是传引用？", "拷贝传值；传大结构体要传指针并注意生命周期与栈溢出。"),
    ("I2C 总线卡死了怎么办？", "九时钟恢复法：主机手动补 9 个 SCL 时钟，让从机释放 SDA。"),
]


def _insert_card(db, direction_id, front, back, card, history):
    """插入一张卡并落它的历史复习记录。先插行拿 id，再把 card_id 对齐到表 id
    （docs/06 §3 决议：fsrs.card_id == cards.id，全链路只用一套编号）。"""
    cur = db.execute(
        "INSERT INTO cards(direction_id, front, back, fsrs, due) VALUES (?, ?, ?, '', '')",
        (direction_id, front, back),
    )
    card_id = cur.lastrowid
    card.card_id = card_id
    db.execute(
        "UPDATE cards SET fsrs = ?, due = ? WHERE id = ?",
        (card_to_json(card), due_of(card), card_id),
    )
    for rating, when in history:
        db.execute(
            "INSERT INTO review_logs(card_id, rating, reviewed_at) VALUES (?, ?, ?)",
            (card_id, rating, when.isoformat()),
        )


def insert_demo(db) -> int:
    """插入示例方向及其阶段/任务/日志，返回新方向 id。调用方保证库为空或允许新增。"""
    today = date.today()
    cur = db.execute(
        "INSERT INTO directions(name, description) VALUES (?, ?)",
        ("嵌入式", "大一下开始的主攻方向：C → STM32 → RTOS → 项目实战"),
    )
    direction_id = cur.lastrowid

    rng = random.Random(42)
    for p_idx, (p_name, p_goal, task_titles) in enumerate(PHASES):
        cur = db.execute(
            "INSERT INTO phases(direction_id, name, goal, sort_order) VALUES (?, ?, ?, ?)",
            (direction_id, p_name, p_goal, p_idx),
        )
        phase_id = cur.lastrowid
        for t_idx, title in enumerate(task_titles):
            # 演示进度：阶段一全部完成、阶段二大部分完成、阶段三个别进行中
            if p_idx == 0:
                status = "done"
            elif p_idx == 1:
                status = "done" if t_idx < 3 else ("doing" if t_idx == 3 else "todo")
            elif p_idx == 2 and t_idx == 0:
                status = "doing"
            else:
                status = "todo"
            done_at = None
            if status == "done":
                done_days_ago = rng.randint(50, 110) - (t_idx * 3)
                done_at = (today - timedelta(days=max(3, done_days_ago))).strftime("%Y-%m-%d")
            db.execute(
                "INSERT INTO tasks(phase_id, title, status, sort_order, done_at) VALUES (?, ?, ?, ?, ?)",
                (phase_id, title, status, t_idx, done_at),
            )

    # 近 60 天里约六成天有学习记录
    for offset in range(59, -1, -1):
        day = today - timedelta(days=offset)
        if rng.random() > 0.6:
            continue
        minutes = rng.choice([20, 30, 45, 45, 60, 90, 120])
        content = rng.choice(TIL_SAMPLES) if rng.random() < 0.4 else ""
        db.execute(
            "INSERT INTO logs(direction_id, date, minutes, content) VALUES (?, ?, ?, ?)",
            (direction_id, day.isoformat(), minutes, content),
        )

    # V3（FR9.7）：6 张复习卡，时间全部动态回推
    now = utcnow()
    past = now - timedelta(days=30)
    for front, back in _REVIEWED_CARDS:
        card = new_card(0)
        card, _ = review(card, 3, when=past)
        card, _ = review(card, 3, when=past)
        _insert_card(db, direction_id, front, back, card, [(3, past), (3, past)])
    for front, back in _NEW_CARDS:
        _insert_card(db, direction_id, front, back, new_card(0), [])
    return direction_id
