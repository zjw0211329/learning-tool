"""「嵌入式」示例数据生成，用于首次演示与验收（见《02》验收标准第 1 条）。"""
import random
from datetime import date, timedelta

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
    return direction_id
