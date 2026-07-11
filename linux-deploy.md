# Linux 部署指南

## 环境要求

- Ubuntu 22.04+ / Debian 12+ / Rocky 9+
- Python 3.12+（推荐 `deadsnakes` PPA 或 `pyenv`）

```bash
# Ubuntu 安装 Python 3.12
sudo add-apt-repository ppa:deadsnakes/ppa -y
sudo apt install python3.12 python3.12-venv python3.12-dev -y
```

## 推荐拓扑

```
                     Internet
                        │
                ┌───────┴───────┐
                │  nginx / caddy │  ← 反向代理 + TLS 终结
                │  :443 (https)  │
                └───────┬───────┘
                        │
                ┌───────┴───────┐
                │  open-ant      │  ← uvicorn :8000 (127.0.0.1)
                │  systemd 守护  │
                └───────┬───────┘
                        │
                ┌───────┴───────┐
                │  ~/open-ant-   │
                │  workspace/    │  ← 用户数据
                └───────────────┘
```

open-ant 只监听 127.0.0.1，由反向代理处理 TLS 和公网暴露。

## 安装

```bash
sudo useradd -m -s /bin/bash open-ant
sudo su - open-ant

git clone https://github.com/Fair-Fair-Fair/open-ant.git
cd open-ant

python3.12 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -e .

# 创建 workspace（参考 README Quick Start）
mkdir ~/open-ant-workspace
# ... config.user.yaml + agents/ ...

# 验证
open-ant server -w ~/open-ant-workspace
```

## systemd 服务

创建 `/etc/systemd/system/open-ant.service`：

```ini
[Unit]
Description=Open-Ant Agent Runtime
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=open-ant
Group=open-ant
WorkingDirectory=/home/open-ant/open-ant
Environment=PATH=/home/open-ant/open-ant/.venv/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=/home/open-ant/open-ant/.venv/bin/open-ant server -w /home/open-ant/open-ant-workspace
Restart=always
RestartSec=5

NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=/home/open-ant/open-ant-workspace
PrivateTmp=yes

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now open-ant
```

## Nginx 反向代理

```nginx
server {
    listen 443 ssl;
    server_name ant.example.com;

    ssl_certificate     /etc/letsencrypt/live/ant.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/ant.example.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_read_timeout 300s;
    }
}
```

## 运维命令

```bash
sudo journalctl -u open-ant -f          # 查看日志
sudo systemctl restart open-ant         # 重启
curl http://127.0.0.1:8000/api/agents   # 健康检查

# 更新代码
sudo su - open-ant
cd ~/open-ant && git pull
source .venv/bin/activate && pip install -e .
sudo systemctl restart open-ant
```

## 注意事项

| 点 | 说明 |
|----|------|
| **ChromaDB** | 首次启动自动在 workspace 下创建 `.memory/` |
| **文件权限** | workspace 需对 `open-ant` 用户可读写 (`chmod 700`) |
| **防火墙** | 仅开放 443（nginx），8000 不对外暴露 |
| **日志** | 应用日志写 workspace 下 `.logs/`，systemd 日志通过 journald 捕获 |
