"""FSRS-6 间隔重复调度封装（V3 FR9，设计见 docs/06 第 3~4 节）。

只依赖 fsrs 包与标准库，不碰 Flask / SQLite —— tests/api_smoke.py §9 直接单测。

时间纪律（docs/06 §4 勘误的落实）：
- 全链路一律 tz-aware UTC：fsrs 的 review_datetime 硬性要求 timezone.utc，
  传 naive 直接 ValueError，datetime.utcnow() 这种返回 naive 的写法禁止使用；
- 落库一律 datetime.isoformat() 原样字符串；due 列只写 Card.to_dict()['due']
  （§3 决议：唯一事实源是 fsrs 库，禁止手工 strftime 拼接）。
"""
import json
from datetime import datetime, timezone

from fsrs import Card, Rating, ReviewLog, Scheduler

# FR9.4：desired_retention=0.9。enable_fuzzing 必须显式关闭（默认开）：
# 关掉后同输入两次调度结果完全相等，测试断言与「今日队列」的确定性都依赖它。
scheduler = Scheduler(enable_fuzzing=False, desired_retention=0.9)

# FR9.2：新卡每日进入复习队列的上限。后端常量，前端不暴露、不设 settings 表。
REVIEW_NEW_LIMIT = 10


def utcnow() -> datetime:
    """全链路唯一的「现在」来源：tz-aware UTC。"""
    return datetime.now(timezone.utc)


def new_card(card_id: int) -> Card:
    """新卡。显式传 card_id（= cards.id，docs/06 §3 对齐决议），
    同时避开 Card() 缺省 card_id 时的 1ms sleep（库自带的防碰撞机制）。"""
    return Card(card_id=card_id)


def is_new(card: Card) -> bool:
    """新卡 = 从未复习过。fsrs v6 没有 New 状态：新卡就是 Learning + last_review=None。"""
    return card.last_review is None


def review(card: Card, rating: int, when: datetime | None = None) -> tuple[Card, ReviewLog]:
    """按评分调度一张卡。rating 为 1~4（忘记/困难/良好/简单）；when 缺省 = 现在。"""
    if not isinstance(rating, int) or isinstance(rating, bool) or rating not in (1, 2, 3, 4):
        raise ValueError(f"rating 必须是 1~4 的整数，收到 {rating!r}")
    if when is None:
        when = utcnow()
    return scheduler.review_card(card, Rating(rating), review_datetime=when)


def card_to_json(card: Card) -> str:
    """fsrs 列的唯一写入口：Card.to_dict() 的 JSON。"""
    return json.dumps(card.to_dict())


def card_from_json(text: str) -> Card:
    """fsrs 列的读出口。畸形数据抛 ValueError，调用方转 400 / 提示。"""
    try:
        return Card.from_json(text)
    except (ValueError, KeyError, TypeError) as e:
        raise ValueError(f"卡片调度状态损坏：{e}") from e


def due_of(card: Card) -> str:
    """due 列的唯一写入口：Card.to_dict()['due'] 原样字符串。"""
    return card.to_dict()["due"]


def is_valid_fsrs(value) -> bool:
    """备份导入校验用：是不是一份官方 Card.to_dict() 的 JSON 对象。

    from_dict 能复原且往返一致 —— 顺带挡掉字符串化的数字、缺字段、
    非法 state 等一切形状漂移（与 _BACKUP_SPEC 其余校验器同一思路）。
    """
    if not isinstance(value, dict):
        return False
    try:
        return Card.from_dict(value).to_dict() == value
    except (KeyError, TypeError, ValueError):
        return False
