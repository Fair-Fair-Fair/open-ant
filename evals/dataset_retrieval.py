"""Retrieval evaluation dataset — hand-written, in this file on purpose.

Why a .py file instead of .json?  Because the dataset doubles as the
*interview narrative*: every doc and every query carries an inline comment
explaining what it is designed to test (semantic rewrite, vague query,
multi-doc recall, …).  That intent would be invisible in a JSON blob.

Corpus design (mirrors a real personal-assistant memory bank)
-------------------------------------------------------------
- One fictional user (a solo developer building the open-ant agent).  20
  short docs (150–300 Chinese chars each), ``doc_01`` .. ``doc_20``,
  covering preferences / project background / decision history — exactly
  the mix a memory store accumulates over weeks of chatting.
- Each doc carries 3–5 keywords (used for human review and as a loose
  BM25 sanity check, NOT as part of the ground truth).

Query design (30 annotated queries)
-----------------------------------
- Queries are *paraphrases*, never verbatim sentences from the docs — the
  dataset must measure semantic retrieval, not substring matching.
- Coverage mix, tagged per query via ``query_type``:
    specific  — one doc, clear keyword-ish wording (baseline)
    vague     — fuzzy phrasing, no doc-specific terms ("我是不是挺看重隐私的?")
    rewrite   — the same fact asked with completely different vocabulary
    combined  — a single question whose answer spans 2 docs (tests fusion)
  Ground truth sizes range 1–3 docs; doc-level IDs (the runner dedupes
  chunk hits back to their source doc before scoring).
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = [
    "RetrievalDoc",
    "RetrievalQuery",
    "RETRIEVAL_DOCS",
    "RETRIEVAL_QUERIES",
    "DOC_ID_SET",
]


@dataclass(frozen=True)
class RetrievalDoc:
    """One corpus document.  ``keywords`` are for review/sanity only —
    ground truth lives exclusively in the queries."""

    doc_id: str  # doc_01 .. doc_20
    text: str  # 150–300 Chinese chars
    keywords: list[str] = field(default_factory=list)  # 3–5 terms, human-review aid


@dataclass(frozen=True)
class RetrievalQuery:
    """One annotated query.  ``ground_truth`` is the set of doc_ids a good
    retrieval system must surface for this query."""

    query_id: str  # q_01 .. q_30
    query: str  # Chinese natural-language question (paraphrased)
    ground_truth: list[str]  # 1–3 doc ids
    query_type: str  # specific | vague | rewrite | combined
    note: str = ""  # design intent — what this query is testing


# ---------------------------------------------------------------------------
# 语料：20 篇（题材 = 虚构用户 林一 的个人助手记忆库）
# 语言偏好、项目背景、决策历史、生活细节、运维记录……模拟真实长期积累的记忆。
# ---------------------------------------------------------------------------

RETRIEVAL_DOCS: list[RetrievalDoc] = [
    # ── 技术偏好 / 决策历史 ──────────────────────────────────────────────
    RetrievalDoc(
        doc_id="doc_01",
        text=(
            "用户写系统程序时明显更偏爱 Rust：看重内存安全、无垃圾回收带来的"
            "可预测性能，以及强类型系统在重构时的安全感。他对 Go 的评价不高，"
            "认为 goroutine 的调试体验复杂、错误处理写得啰嗦，除非团队已有约定"
            "否则不会主动选用。近期他计划把 open-ant 里对性能敏感的文本索引"
            "模块用 Rust 重写，并在简历中把 Rust 项目放在显眼位置。他同时要求"
            "团队里的代码评审以 Rust 的 clippy 规则作为默认风格基线，把好习惯"
            "固化进流程。"
        ),
        keywords=["rust", "go", "编程语言选型", "内存安全"],
        # 设计意图：偏好类记忆。查询若只问"选型原则"而不提 Rust/Go，能命中
        # 才说明检索真正理解了内容（语义而非关键词）。
    ),
    RetrievalDoc(
        doc_id="doc_02",
        text=(
            "open-ant 是用户独立开发维护的个人 AI 助手运行时，基于 Python 3.12 "
            "与 asyncio，主打『本地优先』：聊天记录、记忆库、检索索引默认全部"
            "落在自己的机器上，只有模型推理请求才出网。项目的核心叙事是"
            "harness engineering——把上下文工程沉淀为可插拔的流水线中间件。"
            "他希望这个项目成为秋招简历上的主要技术叙事。他明确反对把助手做"
            "成纯云端 SaaS，坚持核心逻辑与数据都在本地跑，云端只做可选扩展。"
        ),
        keywords=["open-ant", "harness engineering", "本地优先", "asyncio"],
    ),
    RetrievalDoc(
        doc_id="doc_03",
        text=(
            "用户的部署目标是一台 2 核 4G 的廉价云服务器，月预算不超过三十元，"
            "操作系统装 Ubuntu 24.04。所有服务用 Docker Compose 编排，数据卷"
            "统一挂载在 /var/lib/open-ant。为控制内存占用，他特意选择轻量的"
            "SQLite 而非常驻 MySQL。他还留了一台备用机器在同机房，主要用来跑"
            "定时任务和夜间批处理，主服务器挂了可以临时顶班。他坚信一旦服务器"
            "被攻击或流量超限，靠备份能在半天内完整重建环境。"
        ),
        keywords=["服务器", "部署", "docker", "预算"],
    ),
    RetrievalDoc(
        doc_id="doc_04",
        text=(
            "用户最早用 ChromaDB 做本地向量库，后来因两点原因计划迁移到 "
            "Qdrant：一是数据量上来后 ChromaDB 的带过滤查询性能不够稳定，"
            "二是想用 Qdrant 云服务省去自维护索引的负担。迁移时他保留了 "
            "VectorStore 抽象接口，切换只需改一处工厂方法。他同时坚持评估"
            "先行：没有 recall@5 与 MRR 数字做对比之前，不下迁移结论。选型"
            "清单里还对比过 Milvus 和 pgvector，最后因为云服务托管成本和社区"
            "活跃度选了 Qdrant。"
        ),
        keywords=["chromadb", "qdrant", "向量库迁移", "评估先行"],
    ),
    RetrievalDoc(
        doc_id="doc_05",
        text=(
            "用户的日常开发工作流是终端三件套：Neovim 写代码、tmux 管会话、"
            "git 走命令行，浏览器只用来查资料。他拒绝重量级 IDE，理由是启动慢、"
            "界面干扰多，改配置都要点好几层菜单，远不如文本文件直接。代码写完"
            "必须跑通单测和 ruff 才允许合并，习惯每个提交只做一件事，方便日后"
            "二分定位问题。他还给自己配了一套 dotfiles 仓库，换新机器一条命令"
            "恢复全部环境，配置即代码。周末偶尔折腾终端配色与补全插件，但以不"
            "影响写码效率为限，过度折腾会被他自己叫停。"
        ),
        keywords=["neovim", "tmux", "终端工作流", "git"],
    ),
    RetrievalDoc(
        doc_id="doc_06",
        text=(
            "用户信奉测试驱动：先写失败用例再实现，关键模块必须覆盖边界条件，"
            "异常路径也要有断言。他给项目立了一条铁律——发布前全量测试必须保持"
            "绿色、lint 零告警，否则不进版本号。这个习惯源于一次事故：有一版"
            "发布没跑测试，真实 API 密钥被带进了 sdist 安装包，教训深刻。此后"
            "他给发布流程加了一道 check_publish 门禁：打包内容白名单核对、密钥"
            "扫描、发布前全量回归。他说宁可慢一天也不冒险，质量门槛是给未来的"
            "自己省事的，不是给流程添堵的。"
        ),
        keywords=["测试", "tdd", "check_publish", "发布门禁"],
    ),
    # ── 生活偏好 ─────────────────────────────────────────────────────────
    RetrievalDoc(
        doc_id="doc_07",
        text=(
            "用户每天固定喝两杯手冲咖啡，偏好中浅烘焙的埃塞俄比亚豆子，喜欢"
            "花香与果酸调性的口感。下午两点以后他不再喝咖啡，以免咖啡因影响"
            "夜间睡眠，改喝花草茶或白开水。家里常备一套手冲器具，出差也会带"
            "挂耳包，酒店没有手冲条件就退而求其次喝美式。他拒绝速溶咖啡和连锁"
            "店的加糖拿铁，认为那算不上真正的咖啡，顶多算含咖啡因的甜饮料。"
            "如果哪天睡眠不足，他会把第一杯咖啡推迟到上午十点以后，让咖啡因的"
            "作用时段更合理。"
        ),
        keywords=["咖啡", "手冲", "埃塞俄比亚", "下午两点"],
    ),
    RetrievalDoc(
        doc_id="doc_08",
        text=(
            "用户的健身目标是年底完成一场全程马拉松，为此目前保持每周三次"
            "力量训练加两次有氧跑的节奏。因为左膝有旧伤，他做深蹲时严格控制"
            "重量，改用腿举和单腿硬拉替代，避免膝部受力过大。饮食上训练日会"
            "额外补充蛋白粉，长距离拉练前会提前一天储备碳水，赛后马上补充"
            "电解质。他避开健身房高峰时段，习惯选早晨或深夜人少的时候去，也把"
            "跑步安排在傍晚的凉快时段。他要求助手在安排运动计划时避开左膝的"
            "高负荷动作，并在连续疲劳超过一周时提醒他休息。"
        ),
        keywords=["健身", "马拉松", "膝盖旧伤", "深蹲"],
    ),
    RetrievalDoc(
        doc_id="doc_09",
        text=(
            "用户是典型的夜猫子：凌晨一点到两点之间入睡，上午十点左右起床，"
            "午后才算真正开始一天。上午十一点到下午两点是他一天里效率最高的"
            "编码时段，写出的代码量约等于全天的一半。因此重要会议他都安排在"
            "下午，上午尽量不被打扰，也婉拒一切上午的面试或路演。他尝试过早睡"
            "早起打卡，坚持两周就放弃，承认生物钟短期内改不过来，与其硬拗不如"
            "顺应。他给助手的提醒是：约会议、约出行，先看时间是不是在下午，"
            "别踩他上午的代码时间。"
        ),
        keywords=["作息", "夜猫子", "会议安排", "高效时段"],
    ),
    RetrievalDoc(
        doc_id="doc_10",
        text=(
            "open-ant 采用 MIT 许可开源，用户对开源的态度是『代码公开、密钥"
            "保密』。仓库里全部源码都可见，但 .env、workspace 数据与各类凭据"
            "一律不入库，日志里也做了脱敏。他因此在打包配置里维护了一份 sdist "
            "白名单，防止构建产物夹带敏感文件，发布前还会人工复查一次。他认为"
            "开源是简历叙事的一部分，能展示代码风格和工程习惯，但安全底线绝不"
            "动摇。那次密钥泄露事故之后，他把『宁可慢一天也不冒险』写进了团队"
            "的发布清单第一条。"
        ),
        keywords=["mit", "开源", "密钥保密", "sdist 白名单"],
    ),
    RetrievalDoc(
        doc_id="doc_11",
        text=(
            "用户在隐私问题上立场鲜明：聊天记录与记忆数据只存本地，绝不默认"
            "同步到任何云端或第三方。他评估过云端向量库与自托管方案，最终选择"
            "折中——检索在本机运行，只有模型推理请求出网。他对一切可自我托管"
            "的服务有天然好感，比如自建 RSS、自建网盘、自建密码管理器，都认真"
            "研究过。他甚至考虑用 Tailscale 组私有网络，把家里机器上的服务暴露"
            "给自己所有设备，而不过公网。买云服务时他会优先选择数据主权声明"
            "清晰的地区，宁可慢一点也不把隐私当筹码。"
        ),
        keywords=["隐私", "本地存储", "自托管", "tailscale"],
    ),
    # ── 近期计划 / 状态 ──────────────────────────────────────────────────
    RetrievalDoc(
        doc_id="doc_12",
        text=(
            "用户计划国庆假期去川西自驾，路线是成都出发，经四姑娘山到新都桥"
            "再折返，租一辆 SUV 出行。他已订好成都的酒店和租车公司，返程前会"
            "绕道都江堰看一眼水利工程，据说顺路。他担心高原反应，出发前一周"
            "开始服用红景天，并让助手列了一份高原药品清单和氧气储备建议。行程"
            "总共安排五天四夜，每天驾驶控制在四小时以内，留出拍照和徒步的时间。"
            "他要求助手在国庆前三天提醒他：检查车辆保险、更新离线地图、给家人"
            "报备行程。"
        ),
        keywords=["旅行", "川西自驾", "国庆", "成都"],
    ),
    RetrievalDoc(
        doc_id="doc_13",
        text=(
            "用户正处于减脂期，日常饮食高蛋白低碳水：早餐鸡蛋加燕麦，午餐"
            "鸡胸肉配杂粮饭，晚餐蔬菜配鱼。加餐只允许两种：训练后一杯蛋白粉，"
            "或者一把原味坚果，甜食和含糖饮料都在禁止列表里。周末他会安排一顿"
            "放纵餐，首选牛油锅底的火锅，毛肚、黄喉、虾滑都是必点项。他用 "
            "app 记录热量，但允许两成的浮动，认为过度精确会影响心情，反而"
            "难以坚持。减脂目标不是一个具体体重数字，而是体脂率回到去年秋天"
            "的水平，他为此每两周测一次。"
        ),
        keywords=["减脂", "高蛋白", "火锅", "放纵餐"],
    ),
    RetrievalDoc(
        doc_id="doc_14",
        text=(
            "用户正在自学计算机图形学，目标是三个月内用 WebGPU 写出一台小型"
            "渲染器，能实时渲染带光照和阴影的场景。他每天固定投入三十分钟，读"
            "资料与写代码交替进行，周末会额外加练一小时。为此他专门配置了一台"
            "带独立显卡的开发机，显存和内存都按渲染需求升级过。他认为图形学能"
            "补齐他计算机基础里的短板，理解光线、变换与着色，对写系统程序也有"
            "帮助。他还计划在渲染器完成后开源出来，顺便把图形学入门笔记整理成"
            "系列文章。"
        ),
        keywords=["图形学", "webgpu", "渲染器", "学习计划"],
    ),
    # ── 服务 / 工具生态 ──────────────────────────────────────────────────
    RetrievalDoc(
        doc_id="doc_15",
        text=(
            "用户的域名在 Namecheap 购买，DNS 与 CDN 都托管在 Cloudflare，"
            "免费套餐足够他个人项目使用。Telegram 是他唯一的主力消息渠道，"
            "所有机器人与服务的告警都推送到一个专用频道，手机震动常开。他几乎"
            "不用邮件，邮箱只用于注册账户，日常通知基本被忽略——这也是他给助手"
            "接 Telegram 而不是邮箱的原因。告警分级他也定过：崩溃类必须发短信"
            "和电话，普通告警只进频道，避免半夜被无关消息吵醒。"
        ),
        keywords=["cloudflare", "域名", "telegram", "告警通知"],
    ),
    RetrievalDoc(
        doc_id="doc_16",
        text=(
            "用户技术类书籍偏爱系统设计主题，最近在重读《数据密集型应用系统"
            "设计》，把它当床头常备书。非技术阅读则喜欢人物传记，尤其是工程师"
            "和创业者的自传，他从中找自己做判断的参照。他的习惯是速读加笔记："
            "先看目录和序言建立框架，再逐章摘录，每月保持两本的量。笔记统一"
            "存在一个 markdown 仓库里，按主题归档，写周报时会翻出来复用。他"
            "认为读书笔记比读书本身更值钱：读过的会忘，但提炼过一遍的结论不会。"
        ),
        keywords=["阅读", "系统设计", "读书笔记", "传记"],
    ),
    # ── 运维纪律 / 历史事故 ─────────────────────────────────────────────
    RetrievalDoc(
        doc_id="doc_17",
        text=(
            "用户的备份遵循 3-2-1 原则：本地磁盘一份、NAS 一份、Backblaze B2 "
            "云上一份异地副本，共三份数据。服务器每天凌晨三点自动备份数据库与"
            "配置目录，脚本先做一致性快照再上传，失败会告警到 Telegram。本地"
            "机器用 Time Machine 风格的增量备份，NAS 每周做一次全量并保留四个"
            "版本。他给助手设了每月提醒：第一个周末检查备份恢复演练是否通过，"
            "任何一步失败都要当天修复。"
        ),
        keywords=["备份", "backblaze", "3-2-1", "异地副本"],
    ),
    RetrievalDoc(
        doc_id="doc_18",
        text=(
            "用户喜欢简洁的文字沟通：即时消息能一句话说清就绝不多发第二句，"
            "更讨厌语音消息和超过三十秒的长语音条，收到会直接转文字再回。邮件"
            "方面他坚持当天清空收件箱，超过三天未回复的邮件直接归档处理，不再"
            "翻看。他给团队的建议是：重要的事情写文档而不是开会口述，消息能"
            "打字就不要发语音。他认为沟通效率是团队协作中最容易被低估的环节，"
            "也是他评判工具好坏的标准之一。"
        ),
        keywords=["沟通", "文字消息", "语音消息", "邮件"],
    ),
    RetrievalDoc(
        doc_id="doc_19",
        text=(
            "上个月用户的服务器发生一次 OOM 事故：夜间高峰时 Nginx 与 Node "
            "进程被内核杀死，网站约挂了四十分钟。排查发现根因是日志文件过大"
            "挤占了内存，nginx 的 access 日志一个月涨了十几个 G，没有轮转。"
            "事后做了三项加固：给日志加轮转、限制单进程内存上限、启用 2G 的 "
            "swap 分区作为兜底。他还给监控加了内存使用率告警，阈值设在使用率"
            "超过百分之八十五时触发。这次事故让他养成每月查看一次内存监控的"
            "习惯，也把『先轮转再扩容』写进了运维手册。"
        ),
        keywords=["故障", "oom", "服务器", "日志轮转"],
    ),
    RetrievalDoc(
        doc_id="doc_20",
        text=(
            "用户采用时间块管理日程：上午十一点到下午两点写代码，下午两点到"
            "五点集中处理会议与消息。晚上八点后是阅读与复盘时间，他不安排任何"
            "会议，也尽量不接需要动脑的电话。他试过番茄钟，觉得二十五分钟一个"
            "节奏太碎，不适合需要长时间沉浸的编程，十分钟的休息打断心流。他"
            "认为日程表是给自己看的，对外承诺总会留出缓冲，实际交付时间通常是"
            "承诺时间的双倍。每周日晚他会花十五分钟排下一周的三个时间块，其他"
            "零碎事务全部塞进下午的缓冲区。"
        ),
        keywords=["时间管理", "日程", "番茄钟", "时间块"],
    ),
]

DOC_ID_SET: frozenset[str] = frozenset(doc.doc_id for doc in RETRIEVAL_DOCS)


# ---------------------------------------------------------------------------
# 标注查询：30 条（ground truth = 必须被检索到的 doc_id 集合）
# 措辞刻意与原文错开；query_type 标注测试意图。
# ---------------------------------------------------------------------------

RETRIEVAL_QUERIES: list[RetrievalQuery] = [
    # ── q_01..q_10：specific（相对具体，基线难度）────────────────────────
    RetrievalQuery(
        query_id="q_01",
        query="我写代码最讨厌啰嗦的错误处理，我是不是更吃系统级语言那一套？",
        ground_truth=["doc_01"],
        query_type="rewrite",
        note="改写：原文说『Go 错误处理写得啰嗦、偏好 Rust』，查询不含 Rust/Go 字样。",
    ),
    RetrievalQuery(
        query_id="q_02",
        query="我的个人助手项目对外宣传的话，应该突出什么理念？",
        ground_truth=["doc_02"],
        query_type="specific",
        note="『个人助手项目』→ open-ant；『理念』→ 本地优先。",
    ),
    RetrievalQuery(
        query_id="q_03",
        query="我在云上租的那台小机器是什么配置？一个月开销多少？装的什么系统？",
        ground_truth=["doc_03"],
        query_type="specific",
        note="数字类事实（2C4G / 30 元 / Ubuntu）只存在于 doc_03。",
    ),
    RetrievalQuery(
        query_id="q_04",
        query="我当时换向量数据库的核心理由是什么？换之前坚持先做什么对比？",
        ground_truth=["doc_04"],
        query_type="specific",
        note="『换向量数据库』命中迁移决策；『先做什么对比』→ 评估先行。",
    ),
    RetrievalQuery(
        query_id="q_05",
        query="我写代码是用图形界面里的开发工具，还是纯键盘流的终端？",
        ground_truth=["doc_05"],
        query_type="rewrite",
        note="『图形界面开发工具』= IDE；『纯键盘流』= Neovim/tmux，全改写。",
    ),
    RetrievalQuery(
        query_id="q_06",
        query="项目发版之前有哪些强制检查？去年那次密钥泄露事故是怎么发生的？",
        ground_truth=["doc_06", "doc_10"],
        query_type="combined",
        note="跨两篇：门禁/事故在 doc_06，『密钥不入库』红线在 doc_10。",
    ),
    RetrievalQuery(
        query_id="q_07",
        query="下午开会前想喝点提神的，我平时喝什么豆子、什么口味舒服？",
        ground_truth=["doc_07"],
        query_type="rewrite",
        note="『提神』与『下午』共同约束：doc_07 下午两点后不喝——语义检索需抓关系。",
    ),
    RetrievalQuery(
        query_id="q_08",
        query="我备战马拉松的训练安排是怎样的？膝盖有没有伤病影响？",
        ground_truth=["doc_08"],
        query_type="specific",
        note="运动目标 + 伤病限制，同一篇。",
    ),
    RetrievalQuery(
        query_id="q_09",
        query="明天早上八点给我安排个会合不合适？我几点睡几点起？",
        ground_truth=["doc_09"],
        query_type="rewrite",
        note="『早上八点开会』需关联『夜猫子作息、上午不打扰』——隐式推理。",
    ),
    RetrievalQuery(
        query_id="q_10",
        query="我的开源项目用的什么协议？别人拿去商用我介意吗？",
        ground_truth=["doc_10"],
        query_type="specific",
        note="协议事实；『介意吗』→ MIT 许可 + 开放态度。",
    ),
    # ── q_11..q_20：specific / rewrite 混合（中等难度）────────────────────
    RetrievalQuery(
        query_id="q_11",
        query="我的聊天记录会被存到别人的服务器上吗？数据默认放在哪里？",
        ground_truth=["doc_11"],
        query_type="rewrite",
        note="『别人的服务器』= 云端；原文说『只存本地、不默认同步』。",
    ),
    RetrievalQuery(
        query_id="q_12",
        query="国庆出行是坐飞机还是自己开车？具体路线怎么走？",
        ground_truth=["doc_12"],
        query_type="rewrite",
        note="『自己开车』= 自驾；路线细节在 doc_12。",
    ),
    RetrievalQuery(
        query_id="q_13",
        query="我控制体重期间的饮食结构是什么？每周有没有破戒的一顿？",
        ground_truth=["doc_13"],
        query_type="rewrite",
        note="『控制体重』= 减脂期；『破戒』= 放纵餐。",
    ),
    RetrievalQuery(
        query_id="q_14",
        query="我最近业余在捣鼓的那个技术方向，用什么语言写的？",
        ground_truth=["doc_14"],
        query_type="rewrite",
        note="『捣鼓的技术』= 自学图形学；『语言』= WebGPU 着色器相关（原文用 WebGPU）。",
    ),
    RetrievalQuery(
        query_id="q_15",
        query="机器人的推送消息我一般在哪里收？域名和加速是哪个服务商？",
        ground_truth=["doc_15"],
        query_type="specific",
        note="Telegram 渠道 + Cloudflare 托管，同一篇内两个事实。",
    ),
    RetrievalQuery(
        query_id="q_16",
        query="我最近在读什么书？我读书有做笔记的习惯吗？",
        ground_truth=["doc_16"],
        query_type="specific",
        note="书单与阅读习惯。",
    ),
    RetrievalQuery(
        query_id="q_17",
        query="除了我自己的硬盘，我的数据还在哪里留了副本？",
        ground_truth=["doc_17"],
        query_type="rewrite",
        note="『副本』= NAS 与 B2 异地备份。",
    ),
    RetrievalQuery(
        query_id="q_18",
        query="别人给我发一条 59 秒的语音，我会是什么反应？",
        ground_truth=["doc_18"],
        query_type="vague",
        note="情境化提问：需要从『讨厌语音消息』推断行为反应。",
    ),
    RetrievalQuery(
        query_id="q_19",
        query="上个月网站半夜挂过一次，还记得根因吗？后来做了哪些加固？",
        ground_truth=["doc_19"],
        query_type="specific",
        note="历史事故复盘：根因 + 三项加固。",
    ),
    RetrievalQuery(
        query_id="q_20",
        query="我一天里哪些时间段固定用来做什么？",
        ground_truth=["doc_20"],
        query_type="specific",
        note="时间块安排。",
    ),
    # ── q_21..q_30：vague / combined（高难度）────────────────────────────
    RetrievalQuery(
        query_id="q_21",
        query="我是不是一个挺在意数据安全的人？",
        ground_truth=["doc_11"],
        query_type="vague",
        note="无任何关键词，纯语义判断：隐私立场 → 数据安全在意程度。",
    ),
    RetrievalQuery(
        query_id="q_22",
        query="我适合哪种工作节奏？早晨状态好还是晚上状态好？",
        ground_truth=["doc_09", "doc_20"],
        query_type="combined",
        note="跨两篇：生物钟（doc_09）+ 日程安排（doc_20）。",
    ),
    RetrievalQuery(
        query_id="q_23",
        query="我训练日的蛋白质补充和减脂期饮食是怎么配合的？",
        ground_truth=["doc_08", "doc_13"],
        query_type="combined",
        note="训练日补蛋白粉（doc_08）+ 减脂饮食（doc_13）。",
    ),
    RetrievalQuery(
        query_id="q_24",
        query="为了跑图形学我专门做了什么硬件上的准备？",
        ground_truth=["doc_14"],
        query_type="rewrite",
        note="『硬件准备』→ 带独显的开发机。",
    ),
    RetrievalQuery(
        query_id="q_25",
        query="我挑软件和工具时最看重什么？",
        ground_truth=["doc_05", "doc_11"],
        query_type="vague",
        note="抽象问题，需召回工具观（终端效率，doc_05）与本地优先（doc_11）两篇。",
    ),
    RetrievalQuery(
        query_id="q_26",
        query="万一服务器彻底挂了，我有什么办法把数据找回来？",
        ground_truth=["doc_03", "doc_17"],
        query_type="combined",
        note="重建信心（doc_03 备用机器 + 靠备份重建）+ 备份机制（doc_17）。",
    ),
    RetrievalQuery(
        query_id="q_27",
        query="我每个月在云端基础设施上大概烧多少钱？都买了哪些服务？",
        ground_truth=["doc_03", "doc_15"],
        query_type="combined",
        note="服务器预算（doc_03）+ 域名/DNS 服务（doc_15）。",
    ),
    RetrievalQuery(
        query_id="q_28",
        query="接下来三个月我有什么想达成的技术目标？",
        ground_truth=["doc_14"],
        query_type="rewrite",
        note="『三个月目标』→ 三个月内渲染器（原文同样说法，但问题用『技术目标』包装）。",
    ),
    RetrievalQuery(
        query_id="q_29",
        query="我喝咖啡的时间会不会影响我睡觉？",
        ground_truth=["doc_07", "doc_09"],
        query_type="combined",
        note="跨两篇：两点后停咖啡（doc_07）⇄ 夜间入睡（doc_09），因果关联题。",
    ),
    RetrievalQuery(
        query_id="q_30",
        query="我是不是那种凡事都提前规划好的人？",
        ground_truth=["doc_20"],
        query_type="vague",
        note="性格画像式提问，无实体词，测语义理解而非字面匹配。",
    ),
]

QUERY_ID_SET: frozenset[str] = frozenset(q.query_id for q in RETRIEVAL_QUERIES)
