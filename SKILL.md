---
name: Hermes Skill Proxy Deploy Guide
description: 自动部署 hermes-skill-proxy 中转服务 + 配置 Hermes Gateway
trigger: 当需要部署 hermes-skill-proxy 时加载此技能
---

# Hermes Skill Proxy 部署指南

## 部署目标

在目标机器上构建以下架构：
```
客户端 → Proxy (:8643) → Hermes Gateway (:8642)
```

## 前置要求

- Linux + systemd（Ubuntu 18.04 / 20.04 / 22.04 等）
- Python 3.8+
- git, curl, pip, sudo
- Hermes Gateway 已安装并运行在 localhost:8642

## 完整部署步骤

### Step 1: 克隆仓库

```bash
PROXY_DIR="${HOME}/hermes-skill-proxy"
SKILL_NAME="inexbot-knowledge-base"
SKILL_PATH="${HOME}/.hermes/skills/productivity/${SKILL_NAME}"

git clone https://github.com/inexbot/hermes-skill-proxy.git "${PROXY_DIR}"
git clone https://github.com/inexbot/inexbot-knowledge-base.git "${SKILL_PATH}"
```

### Step 2: 安装 Python 依赖

```bash
pip3 install flask requests
```

### Step 3: 安装 systemd 服务

```bash
SERVICE_FILE="${PROXY_DIR}/hermes-skill-proxy.service"
SERVICE_DEST="/etc/systemd/system/hermes-skill-proxy.service"

# 将服务文件中的 /home/inexbot 替换为实际用户目录
sed "s|/home/inexbot|${HOME}|g" "${SERVICE_FILE}" > /tmp/hermes-skill-proxy.service
sudo cp /tmp/hermes-skill-proxy.service "${SERVICE_DEST}"
sudo systemctl daemon-reload
```

### Step 4: 启动并启用服务

```bash
sudo systemctl enable hermes-skill-proxy
sudo systemctl start hermes-skill-proxy
```

### Step 5: 验证部署

```bash
curl http://localhost:8643/health
```

期望返回：
```json
{"status": "ok", "skill": "inexbot-knowledge-base", "skill_loaded": true, "hermes_url": "http://localhost:8642"}
```

## 一键部署命令

直接运行（远程机器执行）：
```bash
bash <(curl -fsSL https://raw.githubusercontent.com/inexbot/hermes-skill-proxy/main/setup.sh)
```

本地运行：
```bash
chmod +x setup.sh && ./setup.sh
```

## 架构说明

### 工作原理

Proxy 在收到 `/v1/chat/completions` 请求时：
1. 从本地 skill 目录读取预置内容
2. 将 skill 内容注入到 system prompt 的最前面
3. 转发给 Hermes Gateway（:8642）
4. 流式响应原样返回给客户端

### 数据流

```
客户端（官网后端）
  ↓ HTTP POST /v1/chat/completions
Proxy (:8643) — 自动注入 skill 到 system prompt
  ↓ HTTP POST
Hermes Gateway (:8642) — 处理请求，返回流式响应
  ↓
Proxy 转发流式响应
  ↓
客户端
```

### Skill 加载机制

启动时调用：
```python
from agent.skill_commands import build_preloaded_skills_prompt
prompt, loaded, missing = build_preloaded_skills_prompt(["inexbot-knowledge-base"], task_id=None)
```

Skill 目录：`${HOME}/.hermes/skills/productivity/inexbot-knowledge-base/`

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `PROXY_PORT` | `8643` | Proxy 监听端口 |
| `HERMES_URL` | `http://localhost:8642` | Hermes Gateway 地址 |
| `HERMES_API_KEY` | `(空)` | Hermes 认证密钥（如有） |
| `SKILL_NAME` | `inexbot-knowledge-base` | 要注入的 skill 名称 |

## 关键文件说明

| 文件 | 作用 |
|------|------|
| `hermes-skill-proxy.py` | Flask 中转服务，核心逻辑 |
| `hermes-skill-proxy.service` | systemd 服务单元（User=inexbot） |
| `setup.sh` | 一键部署脚本，自动化上述所有步骤 |
| `SKILL.md` | 本文件，部署知识库 |

## 运维命令

```bash
# 查看服务状态
sudo systemctl status hermes-skill-proxy

# 查看实时日志
sudo journalctl -u hermes-skill-proxy -f

# 重启服务
sudo systemctl restart hermes-skill-proxy

# 查看 health 状态
curl http://localhost:8643/health

# 查看问题日志（Proxy 记录的用户提问）
tail -20 ~/.hermes/kb/inexbot/questions.log
```

## 常见问题排查

### skill_loaded: false

可能原因：
1. skill 目录路径不对
2. skill 内容为空或损坏

解决：
```bash
# 检查 skill 目录
ls -la ~/.hermes/skills/productivity/inexbot-knowledge-base/

# 重启 proxy
sudo systemctl restart hermes-skill-proxy
```

### 连接 8642 失败

```bash
# 检查 Hermes Gateway 是否运行
sudo systemctl status hermes-gateway

# 检查端口监听
ss -tlnp | grep 8642
```

### 端口 8643 被占用

```bash
# 查找占用进程
ss -tlnp | grep 8643

# 修改端口：编辑 hermes-skill-proxy.service
# 设置 Environment="PROXY_PORT=8644"
sudo systemctl daemon-reload
sudo systemctl restart hermes-skill-proxy
```

## 与 Hermes Gateway 的关系

- Proxy 依赖 Hermes Gateway 运行在 8642 端口
- Proxy 的 `After=hermes-gateway.service` 确保启动顺序
- Proxy 只做转发和注入，不修改 Hermes 源码
- 两个服务相互独立，可单独管理

## 扩展场景

### 修改默认 skill

修改 systemd 服务中的 `SKILL_NAME` 环境变量：
```bash
sudo systemctl edit hermes-skill-proxy --full
# 修改 Environment=SKILL_NAME=your-skill-name
sudo systemctl restart hermes-skill-proxy
```

### 添加认证

```bash
# 编辑服务文件
sudo systemctl edit hermes-skill-proxy --full
# 添加 Environment="HERMES_API_KEY=your-key"
sudo systemctl restart hermes-skill-proxy
```