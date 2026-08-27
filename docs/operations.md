# 运维手册（Phase 5B）

> 备份 / 迁移 / 排障 / 指标告警。所有凭据一律在仓库外 .env，本文档只出现变量名。

## 1. 备份与恢复

### MySQL（会话/消息/outbox/审计/成本——唯一不可丢的数据）

```bash
# 备份（本机）
mysqldump -h 127.0.0.1 -u root -p open_ant > open_ant_backup_$(date +%F).sql
# 恢复
mysql -h 127.0.0.1 -u root -p open_ant < open_ant_backup_YYYY-MM-DD.sql
```

docker compose 环境：`docker compose -f src/docker-compose.yml exec mysql mysqldump ...`
（或用 `docker volume` 直接备份 mysql-data 卷）。含 alembic 迁移的升级前必做。

### Qdrant（记忆/文档向量）

云服务在控制台用 snapshot 功能；本地容器备份 `qdrant-data` 卷即可。向量可重算
（语料还在就能 re-ingest），优先级低于 MySQL。

### Neo4j（记忆图）

Aura 云在控制台快照；本地容器备份 `neo4j-data` 卷。

### Redis

**纯缓存可丢**（embedding 缓存/限流计数），不备份。丢失后果：embedding 重新计算、
限流窗口重置——均可接受。

## 2. 数据迁移

| 场景 | 操作 |
|---|---|
| 结构迁移 | 启动自动跑 `alembic upgrade head`（storage.backend=mysql 时），无需手动 |
| Chroma → Qdrant | `open-ant migrate-chroma --workspace ./workspace`（每 100 条打进度，失败计数不中断） |
| JSONL 历史 → MySQL | 手工步骤：确保 storage.backend=mysql 且 .env 凭据就绪 → 将 workspace/.history 下 jsonl 会话逐条重新导入（或保留 JSONL 只读归档） |

## 3. 排障手册（按症状）

| 症状 | 第一步 | 深入 |
|---|---|---|
| 启动即报错 | `open-ant doctor --workspace ./workspace`（十一项自检，坏配置点名） | doctor 报 ERROR 的项直接修 .env/config |
| 消息不回复 | `curl http://127.0.0.1:8000/readyz`（哪个组件 down 一目了然） | RabbitMQ 队列积压：管理台 15672 看 `ant.*` 队列与 `ant.dlq` 深度 |
| 消息进死信 | 管理台 ant.dlq 查看 | 消费端日志 grep "Event exceeded max retries" |
| 检索结果差 | `python -m evals.run_retrieval_eval` 复跑对照 | 看 evals/report_retrieval.md；中文语料可切 `memory.sparse_model: jieba`（重建集合后） |
| 成本异常 | 查 usage_records 表 | `SELECT model, SUM(prompt_tokens), SUM(completion_tokens), SUM(cost) FROM usage_records GROUP BY model;`（表结构以 alembic 迁移为准） |
| crash-loop | 日志 grep "crash #" | 指数退避 5s→120s，稳定 300s 自动重置；连续 crash #5 以上要查根因 |
| 凭据问题 | doctor 的 mysql/rabbitmq/qdrant/neo4j 行 | 密码打码输出，值只改 .env |

## 4. 监控指标（/metrics 的 openant_* 族）

| 指标 | 含义 | 告警建议 |
|---|---|---|
| openant_events_total | 按 event_type/source 的消费计数 | 突降=上游断流；突增=异常洪峰 |
| openant_queue_depth | 内存总线队列深度 / outbox 未发布行数 | outbox 持续增长 = broker 不通 |
| openant_tool_calls_total / _duration_seconds | 工具调用量与延迟 | p95 > tool_timeout 的一半即需查慢工具 |
| openant_llm_requests_total / _duration_seconds / tokens_total | LLM 调用/延迟/token（model 维度） | 与 usage_records 交叉对账成本 |
| openant_http_requests_total | API 请求计数（method/path） | 4xx/5xx 占比报警 |

/healthz 只管进程存活；依赖健康用 /readyz（区分 not_configured 与 down，只有 down 才 503）。
