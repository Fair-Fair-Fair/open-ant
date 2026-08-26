"""Multi-turn memory task set (10 tasks) — Phase 5 CI automation material.

The scenario is the same fictional user as ``dataset_retrieval.py``.  Each
task models the failure mode that matters most for a personal assistant's
memory: **the user states a fact once, gets distracted, then probes the
fact several turns later** — by which time the raw conversation is far
behind and only an *extracted, indexed memory* can answer.

Structure of one task
---------------------
- ``turns`` — exactly 3 user messages:
    turn 1  states the fact (this is what memory extraction must capture)
    turn 2  unrelated distractor (forces the system to rely on the memory
            store, not on the raw chat history in context)
    turn 3  asks about the fact in a roundabout way
- ``expected_hits`` — the facts (and their ``doc_id`` counterparts in the
  retrieval dataset) a correct memory layer must inject before answering
  turn 3.  Used for manual scoring today, automated scoring in Phase 5 CI:
  run a real server + LLM session, then check the injected context or the
  answer against these expectations.

The 3-turn shape keeps tasks runnable end-to-end (one short session per
task) while still exercising *retrieval*, not just in-context recall.
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = [
    "MemoryTaskTurn",
    "ExpectedHit",
    "MemoryTask",
    "MEMORY_TASKS",
]


@dataclass(frozen=True)
class MemoryTaskTurn:
    """One user utterance in a memory task session."""

    text: str
    role: str = "user"  # reserved: assistant turns can be added later


@dataclass(frozen=True)
class ExpectedHit:
    """A fact that must be retrievable at the probing turn.

    ``doc_id`` links the fact to the retrieval corpus so the same ground
    truth serves both evals; ``fact`` is the human-readable expectation.
    """

    doc_id: str  # reference into evals.dataset_retrieval corpus
    fact: str  # what the assistant must know at turn 3


@dataclass(frozen=True)
class MemoryTask:
    """One multi-turn memory task: state → distract → probe."""

    task_id: str
    title: str  # one-line description of the memory under test
    description: str  # why this task matters (narrative)
    turns: list[MemoryTaskTurn]  # exactly 3
    expected_hits: list[ExpectedHit]  # facts required at turn 3
    difficulty: str = "normal"  # easy | normal | hard


MEMORY_TASKS: list[MemoryTask] = [
    MemoryTask(
        task_id="memory_task_01",
        title="编程语言偏好：Rust 优先",
        description=(
            "偏好类记忆是助手最常用的记忆类型——用户在选型时表达偏好，"
            "三回合后以相反问题探测，只有偏好被提取并注入才能答对。"
        ),
        turns=[
            MemoryTaskTurn(text="以后给我技术建议时记住：我宁愿用 Rust 也不用 Go，别给我推 Go 的方案。"),
            MemoryTaskTurn(text="帮我看看这个 Python 脚本为什么这么慢。"),
            MemoryTaskTurn(text="我之前跟你说过我对编程语言有什么偏好吗？你会建议我用 Go 吗？"),
        ],
        expected_hits=[
            ExpectedHit(doc_id="doc_01", fact="用户偏好 Rust 而非 Go，不要推荐 Go 方案"),
        ],
    ),
    MemoryTask(
        task_id="memory_task_02",
        title="时间敏感偏好：下午两点后不喝咖啡",
        description=(
            "带时间条件的偏好：回答『能不能喝』需要结合当前时刻与记忆中的"
            "时间限制——考记忆注入 + 简单推理的组合。"
        ),
        turns=[
            MemoryTaskTurn(text="我下午两点之后不喝咖啡，这个你帮我记牢。"),
            MemoryTaskTurn(text="今天天气怎么样？适合出门吗？"),
            MemoryTaskTurn(text="等会儿下午四点的会，我喝杯咖啡提神可以吗？"),
        ],
        expected_hits=[
            ExpectedHit(doc_id="doc_07", fact="用户下午两点后不喝咖啡（会提示不建议）"),
        ],
    ),
    MemoryTask(
        task_id="memory_task_03",
        title="健康限制：左膝旧伤与深蹲",
        description=(
            "伤病类记忆一旦丢失会给出危险建议——这是『答错有代价』的记忆类型，"
            "评分时对错误建议应从严。"
        ),
        turns=[
            MemoryTaskTurn(text="我左膝有旧伤，深蹲只能上小重量，以后别给我安排大重量深蹲计划。"),
            MemoryTaskTurn(text="帮我把这周的健身课表确认一遍。"),
            MemoryTaskTurn(text="我下周的力量训练计划里，深蹲应该怎么安排？"),
        ],
        expected_hits=[
            ExpectedHit(doc_id="doc_08", fact="左膝旧伤，深蹲限制重量，用腿举/单腿硬拉替代"),
        ],
    ),
    MemoryTask(
        task_id="memory_task_04",
        title="作息画像：夜猫子与上午不打扰",
        description=(
            "作息类记忆用于排程判断。探测问题要求『跨时间推理』：早上八点"
            "开会是否合适取决于睡眠规律——需要记忆而非对话历史。"
        ),
        turns=[
            MemoryTaskTurn(text="我一般凌晨一点半睡、上午十点起，上午十一点到两点是写码黄金时间，重要的事都安排在下午。"),
            MemoryTaskTurn(text="帮我约个牙医，周五上午有没有号？"),
            MemoryTaskTurn(text="周五早上八点的会我参加得了吗？"),
        ],
        expected_hits=[
            ExpectedHit(doc_id="doc_09", fact="夜猫子作息：约凌晨一点半睡、十点起，上午效率最高，早上八点会议不合适"),
        ],
        difficulty="hard",
    ),
    MemoryTask(
        task_id="memory_task_05",
        title="近期行程：国庆川西自驾",
        description=(
            "行程类记忆有明确时效。探测时用户自己说错细节（开车还是租车），"
            "助手应依据记忆纠正——考记忆准确性而非顺从。"
        ),
        turns=[
            MemoryTaskTurn(text="国庆我要去川西自驾，已经订好成都的酒店和租的 SUV。"),
            MemoryTaskTurn(text="帮我看下明天的天气，要不要带伞。"),
            MemoryTaskTurn(text="对了，国庆我是不是说过自己开车去川西？酒店订了吗？"),
        ],
        expected_hits=[
            ExpectedHit(doc_id="doc_12", fact="国庆川西自驾：成都出发，租 SUV，酒店已订"),
        ],
    ),
    MemoryTask(
        task_id="memory_task_06",
        title="预算红线：服务器月开销 ≤ 30 元",
        description=(
            "约束类记忆（预算/底线）是助手做推荐时的硬过滤条件。探测问题"
            "反向验证：记忆丢失会让助手推荐超预算方案。"
        ),
        turns=[
            MemoryTaskTurn(text="我的服务器预算一个月不超过三十块，别再给我推荐贵的套餐。"),
            MemoryTaskTurn(text="帮我看看这个部署报错是什么原因。"),
            MemoryTaskTurn(text="我之前说过服务器开销的底线是多少来着？你推荐的那个方案多少钱？"),
        ],
        expected_hits=[
            ExpectedHit(doc_id="doc_03", fact="服务器月预算不超过 30 元，推荐方案必须低于此线"),
        ],
    ),
    MemoryTask(
        task_id="memory_task_07",
        title="运维纪律：3-2-1 备份策略",
        description=(
            "运维策略类记忆：探测『异地副本放哪家』是具体事实抽取，"
            "回答必须精确到服务商。"
        ),
        turns=[
            MemoryTaskTurn(text="备份按 3-2-1 来：本地一份、NAS 一份、Backblaze B2 云上一份。"),
            MemoryTaskTurn(text="帮我整理一下本周的任务清单。"),
            MemoryTaskTurn(text="我的异地备份放在哪家云服务？规则是什么来着？"),
        ],
        expected_hits=[
            ExpectedHit(doc_id="doc_17", fact="备份 3-2-1：本地 + NAS + Backblaze B2 异地"),
        ],
    ),
    MemoryTask(
        task_id="memory_task_08",
        title="事故复盘：服务器 OOM",
        description=(
            "事故教训类记忆：根因 + 加固措施都要能复述，这是『决策历史』"
            "记忆的代表用例。"
        ),
        turns=[
            MemoryTaskTurn(text="记住上个月服务器 OOM 挂过一次，根因是日志文件太大，后来加了日志轮转、内存上限和 swap。"),
            MemoryTaskTurn(text="今天有什么日程安排？"),
            MemoryTaskTurn(text="服务器上次宕机的根因和修复手段，你还记得吗？"),
        ],
        expected_hits=[
            ExpectedHit(doc_id="doc_19", fact="OOM 根因：日志过大；加固：日志轮转 + 内存上限 + 2G swap"),
        ],
    ),
    MemoryTask(
        task_id="memory_task_09",
        title="日程结构：时间块安排",
        description=(
            "日程结构类记忆：用户以规则形式陈述，之后以『某时段干什么』"
            "的形式探测——规则被检索到才能推导。"
        ),
        turns=[
            MemoryTaskTurn(text="我的日程是时间块式的：上午十一点到下午两点写代码，下午两点到五点处理消息和会议。"),
            MemoryTaskTurn(text="帮我点个外卖，中午吃什么好？"),
            MemoryTaskTurn(text="我每天下午的时间段是安排来做什么的？"),
        ],
        expected_hits=[
            ExpectedHit(doc_id="doc_20", fact="下午两点到五点处理消息与会议，晚间阅读复盘"),
        ],
    ),
    MemoryTask(
        task_id="memory_task_10",
        title="发布红线：密钥永不入库",
        description=(
            "安全红线类记忆，与历史事故（0.1.0 密钥泄露）绑定。探测包含"
            "『打包时有什么红线』——需要同时召回协议与门禁两段记忆。"
        ),
        turns=[
            MemoryTaskTurn(text="open-ant 用 MIT 开源没问题，但 .env 和密钥永远不许进仓库和打包产物，发布必须过 check_publish。"),
            MemoryTaskTurn(text="帮我写一段项目简介。"),
            MemoryTaskTurn(text="我的项目开源协议是什么？打包的时候有什么红线？"),
        ],
        expected_hits=[
            ExpectedHit(doc_id="doc_10", fact="MIT 许可；.env/密钥不入库不进 sdist"),
            ExpectedHit(doc_id="doc_06", fact="发布须过 check_publish 门禁（0.1.0 密钥泄露事故的教训）"),
        ],
        difficulty="hard",
    ),
]

MEMORY_TASK_ID_SET: frozenset[str] = frozenset(t.task_id for t in MEMORY_TASKS)
