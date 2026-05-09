# Hermes Skill Proxy

Hermes API Proxy — 自动注入 skill 到 system prompt，**不修改 Hermes 源码**。

## 架构

```
客户端 → Proxy (:8643) → Hermes Gateway (:8642)
              ↓
        自动注入 skill 到 system prompt
```

Proxy 在收到 `/v1/chat/completions` 请求时，读取 Hermes 本地的 skill 内容，拼接进 system prompt 再转发给 Hermes。从客户端视角完全透明。

## 自动部署（另一个 Hermes 执行）

当收到指令"帮我部署 https://github.com/inexbot/hermes-skill-proxy"时，执行以下步骤。

### Step 1: 部署 Hermes Gateway（如果没有）

```bash
# 安装 Hermes Gateway（如果没有）
[ -d "${HOME}/hermes-gateway" ] || git clone https://github.com/nousresearch/hermes-gateway.git "${HOME}/hermes-gateway"

# 安装依赖
cd "${HOME}/hermes-gateway" && pip install -r requirements.txt

# 配置 Gateway（默认监听 8642）
# 配置项：config.yaml 或环境变量
# 关键配置：
#   server.port: 8642
#   server.host: 0.0.0.0

# 安装 systemd 服务
[ -f "${HOME}/hermes-gateway/hermes-gateway.service" ] && {
  sed "s|/home/inexbot|${HOME}|g" "${HOME}/hermes-gateway/hermes-gateway.service" > /tmp/hermes-gateway.service
  sudo cp /tmp/hermes-gateway.service /etc/systemd/system/hermes-gateway.service
  sudo systemctl daemon-reload
  sudo systemctl enable hermes-gateway
  sudo systemctl start hermes-gateway
}

# 确认 Gateway 运行
sleep 2 && curl -s http://localhost:8642/health || ss -tlnp | grep 8642
```

### Step 2: 克隆 hermes-skill-proxy

```bash
PROXY_DIR="${HOME}/hermes-skill-proxy"
git clone https://github.com/inexbot/hermes-skill-proxy.git "${PROXY_DIR}"
```

### Step 3: 安装依赖

```bash
pip3 install flask requests
```

### Step 4: 克隆 skill（可选，如果需要自动注入知识库）

```bash
SKILL_NAME="inexbot-knowledge-base"
SKILL_PATH="${HOME}/.hermes/skills/productivity/${SKILL_NAME}"
git clone https://github.com/inexbot/inexbot-knowledge-base.git "${SKILL_PATH}"
```

### Step 5: 安装 systemd 服务

```bash
SERVICE_FILE="${PROXY_DIR}/hermes-skill-proxy.service"
SERVICE_DEST="/etc/systemd/system/hermes-skill-proxy.service"

# 将服务文件中的 /home/inexbot 替换为实际用户目录
sed "s|/home/inexbot|${HOME}|g" "${SERVICE_FILE}" > /tmp/hermes-skill-proxy.service
sudo cp /tmp/hermes-skill-proxy.service "${SERVICE_DEST}"
sudo systemctl daemon-reload
```

### Step 6: 启动并启用服务

```bash
sudo systemctl enable hermes-skill-proxy
sudo systemctl start hermes-skill-proxy
```

### Step 7: 验证

```bash
curl http://localhost:8643/health
```

期望返回：
```json
{"status": "ok", "skill": "inexbot-knowledge-base", "skill_loaded": true, "hermes_url": "http://localhost:8642"}
```

### 一键部署（等效于上面所有步骤）

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/inexbot/hermes-skill-proxy/main/setup.sh)
```

## 手动部署（自己用）

### 依赖

```bash
pip install flask requests
```

### 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `PROXY_PORT` | `8643` | Proxy 监听端口 |
| `HERMES_URL` | `http://localhost:8642` | Hermes API Server 地址 |
| `HERMES_API_KEY` | `(空)` | Hermes 认证密钥，如有需要 |
| `SKILL_NAME` | `inexbot-knowledge-base` | 要自动注入的 skill 名称 |

### 运行

```bash
# 前台运行
python hermes-skill-proxy.py

# 或指定参数
HERMES_URL=http://localhost:8642 PROXY_PORT=8643 python hermes-skill-proxy.py
```

### systemd 部署

```bash
sudo cp hermes-skill-proxy.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable hermes-skill-proxy
sudo systemctl start hermes-skill-proxy
sudo systemctl status hermes-skill-proxy
```

服务文件默认从 `~/.hermes/hermes-agent` 加载 skill，请确保 Hermes 已安装在默认路径。

## API 端点

| 端点 | 方法 | 说明 |
|------|------|------|
| `/v1/chat/completions` | POST | 代理 chat completions，自动注入 skill |
| `/health` | GET | 健康检查，返回 skill 加载状态 |

### Health 响应示例

```json
{
  "status": "ok",
  "skill": "inexbot-knowledge-base",
  "skill_loaded": true,
  "hermes_url": "http://localhost:8642"
}
```

## 部署架构示例

```
                          ┌─────────────────┐
                          │  官网后端 (Node) │
                          │  localhost:3001 │
                          └────────┬────────┘
                                   │ HTTP
                          ┌────────▼────────┐
                          │  Proxy (:8643) │  ← 自动注入 skill
                          │  Flask         │
                          └────────┬────────┘
                                   │ HTTP
                          ┌────────▼────────┐
                          │ Hermes (:8642)  │
                          │ API Server     │
                          └─────────────────┘
```

## 问题日志

每次收到用户问题时，proxy 自动记录到本地文件，用于分析和优化知识库覆盖。

**记录文件**：`~/.hermes/kb/inexbot/questions.log`

**记录格式**：每行一条 JSON
```json
{"time": "2026-05-08 16:30:00", "question": "工具手标定有几种方法"}
```

**查看最近记录**：
```bash
tail -20 ~/.hermes/kb/inexbot/questions.log
```

**统计高频问题**：
```bash
cat ~/.hermes/kb/inexbot/questions.log | jq -r .question | sort | uniq -c | sort -nr | head -20
```

## 运维命令

```bash
# 查看服务状态
sudo systemctl status hermes-skill-proxy
sudo systemctl status hermes-gateway

# 查看实时日志
sudo journalctl -u hermes-skill-proxy -f
sudo journalctl -u hermes-gateway -f

# 重启服务
sudo systemctl restart hermes-skill-proxy
sudo systemctl restart hermes-gateway

# 查看 health
curl http://localhost:8643/health

# 检查端口
ss -tlnp | grep -E '8642|8643'
```

## License

MIT