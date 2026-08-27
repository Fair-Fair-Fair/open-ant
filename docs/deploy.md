# 部署说明（Phase 5C：Docker 部署）

> 本地全栈一键起：app + MySQL + RabbitMQ + Redis；Qdrant/Neo4j 默认用 .env 里的云服务
> （本地容器备选已写在 docker-compose.yml 注释中）。依据 workspace/plan.md §5.2。
> 凭据纪律：本文档只出现变量名，不出现任何密钥值；值一律放仓库外的 .env。

## 0. 前置条件

- Docker Engine + Compose v2（Docker Desktop 自带；BuildKit 默认开启，Dockerfile 用到 cache mount）
- 仓库已 git pull 到最新（含 Dockerfile / docker-compose.yml / 本文件）
- open-ant 根目录存在 .env（变量：MYSQL_USERNAME、MYSQL_PASSWORD、RABBITMQ_USERNAME、
  RABBITMQ_PASSWORD、LLM/embedding/websearch 各 key、NEO4J_URI/NEO4J_USERNAME/NEO4J_PASSWORD、
  QDRANT_URL/QDRANT_API_KEY 等；缺哪个按需补）
- `workspace/config.user.yaml` 必须让 uvicorn 监听 0.0.0.0 才能从宿主机访问：

  ```yaml
  api:
    host: 0.0.0.0
    port: 8000
  ```

  安全提示：api.host=0.0.0.0 会触发启动警告横幅；建议同时配好 api token（认证配置），
  且 compose 默认只把 8000 发布到宿主机回环地址（127.0.0.1:8000）。

## 1. 首次启动

```bash
# 在 open-ant 根目录执行。--env-file .env 供 compose 做 ${VAR} 插值（mysql 用户映射用）。
# 刻意不把 .env 复制进 src/：src/ 是 git 仓库根，仓库内不放密钥（0.1.0 泄露事故教训）。
docker compose -f src/docker-compose.yml --env-file .env config   # 语法校验（可选）
docker compose -f src/docker-compose.yml --env-file .env up -d    # 拉镜像+build+启动
docker compose -f src/docker-compose.yml ps                       # 等 mysql/rabbitmq/redis/app 均 healthy
```

app 依赖三件套 healthy 后才启动（depends_on condition: service_healthy）。

验证：

```bash
curl http://127.0.0.1:8000/healthz    # liveness：进程存活即 200
curl http://127.0.0.1:8000/readyz     # readiness：mysql/rabbitmq/qdrant/neo4j 全通 200；
                                      # 有组件 down 时 503 且 body 逐个列出状态
docker compose -f src/docker-compose.yml exec app open-ant doctor --workspace /workspace
open http://127.0.0.1:15672           # RabbitMQ 管理台，guest/guest
```

首次 build 需下载镜像与依赖，耗时较长；之后只改代码时重建很快（pip 缓存 + 层缓存）。

## 2. 升级

```bash
git pull
docker compose -f src/docker-compose.yml --env-file .env build --pull   # 重建（拉新基础镜像）
docker compose -f src/docker-compose.yml --env-file .env up -d          # 滚动重启
docker compose -f src/docker-compose.yml ps && curl http://127.0.0.1:8000/readyz
```

MySQL 数据在命名卷 mysql-data，升级不丢。涉及 alembic 迁移的升级请先备份该卷。

## 3. 回滚

- 升级前给当前镜像打标签保留：`docker tag open-ant-app:latest open-ant-app:pre-upgrade`
- 回滚 = 不 rebuild，直接切旧镜像：docker-compose.yml 的 app.image 改为
  `open-ant-app:pre-upgrade`，再 `docker compose ... up -d`
- 数据库结构回滚不在本流程范围（依赖 alembic 迁移，迁移前备份 mysql-data 卷）。

## 4. 停止 / 清理

```bash
docker compose -f src/docker-compose.yml --env-file .env down      # 停并删容器，卷保留
docker compose -f src/docker-compose.yml --env-file .env down -v   # 连 mysql-data 卷一起删（慎用，数据全丢）
```

## 5. 单机 systemd 备选（Linux 服务器，不跑 Docker）

需宿主机自带 MySQL/RabbitMQ/Redis（或另行容器化）。服务文件只引变量名，值只在 .env：

```ini
[Unit]
Description=open-ant server
After=network-online.target

[Service]
Type=simple
User=openant
WorkingDirectory=/opt/open-ant
EnvironmentFile=/opt/open-ant/.env
ExecStart=/opt/open-ant/.venv/bin/open-ant server --workspace /opt/open-ant/workspace
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
systemctl enable --now open-ant
journalctl -u open-ant -f
```

## 6. 已知边界（如实声明）

- 镜像内没有 bash 与 docker CLI：agent 的 bash 工具与 docker 沙箱后端在容器内不可用
  （需要时可自建镜像扩展，见 Dockerfile）。
- 默认镜像未装本地 embedding 模型（sentence-transformers）：.env 的 EMBED_MODEL_TYPE=local
  在容器内缺依赖；可改用 API embedding（EMBED_MODEL_TYPE=api）或 Dockerfile 追加
  `pip install '.[embeddings]'`。
- HEALTHCHECK 只探进程存活（/healthz）；真实依赖探活用 /readyz。
- RabbitMQ guest/guest 仅限本地开发（compose 已放开 loopback 限制并注释说明）。
