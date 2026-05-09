# Hermes Skill Proxy

HTTP API 中转服务，将请求转发给 Hermes Gateway 并自动注入 skill 到 system prompt。

## 架构

```
客户端 → :8643 (Proxy) → :8642 (Hermes Gateway)
              ↓
        自动注入 skill 到 system prompt
```

Proxy 收到 `/v1/chat/completions` 请求后，读取 skill 内容拼入 system prompt，再转发给 Hermes Gateway，最后把流式响应返回给客户端。对客户端完全透明。

## 部署步骤

### 1. 配置 Hermes Gateway API Server

在 `${HOME}/.hermes/config.yaml` 中添加或确认以下配置：

```yaml
gateway:
  api_server:
    enabled: true
    host: 127.0.0.1
    port: 8642
    api_key: ""          # 如需认证则填入密钥，留空则无认证
```

确认 Hermes Gateway 配置生效：
```bash
hermes gateway restart
# 或
sudo systemctl restart hermes-gateway
```

验证：
```bash
curl http://127.0.0.1:8642/health
```

### 2. 安装 hermes-skill-proxy

```bash
git clone https://github.com/inexbot/hermes-skill-proxy.git "${HOME}/hermes-skill-proxy"
cd "${HOME}/hermes-skill-proxy"
pip3 install flask requests
```

### 3. 安装 systemd 服务

```bash
SERVICE_FILE="${HOME}/hermes-skill-proxy/hermes-skill-proxy.service"
sudo cp "${SERVICE_FILE}" /etc/systemd/system/hermes-skill-proxy.service
# 将服务文件中的路径替换为实际用户目录
sudo sed -i "s|/home/inexbot|${HOME}|g" /etc/systemd/system/hermes-skill-proxy.service
sudo systemctl daemon-reload
sudo systemctl enable hermes-skill-proxy
sudo systemctl start hermes-skill-proxy
```

### 4. 克隆 skill（如需自动注入知识库）

```bash
SKILL_PATH="${HOME}/.hermes/skills/productivity/inexbot-knowledge-base"
git clone https://github.com/inexbot/inexbot-knowledge-base.git "${SKILL_PATH}"
```

### 5. 验证

```bash
curl http://localhost:8643/health
```

期望返回：
```json
{"status": "ok", "skill": "inexbot-knowledge-base", "skill_loaded": true, "hermes_url": "http://localhost:8642"}
```

## 一键部署

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/inexbot/hermes-skill-proxy/main/setup.sh)
```

## 配置说明

### Proxy 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `PROXY_PORT` | `8643` | Proxy 监听端口 |
| `HERMES_URL` | `http://localhost:8642` | Hermes Gateway 地址 |
| `HERMES_API_KEY` | `(空)` | 与 Gateway api_key 一致时填入 |

### Gateway 与 Proxy 的对应关系

| 项目 | Gateway | Proxy |
|------|---------|-------|
| 监听地址 | `127.0.0.1` | `0.0.0.0` |
| 端口 | `8642` | `8643` |
| api_key | `config.yaml` 中的值 | `HERMES_API_KEY` 环境变量 |

如果 Gateway 配置了 `api_key`，Proxy 启动时需要设置相同密钥：
```bash
HERMES_API_KEY=your-key PROXY_PORT=8643 python hermes-skill-proxy.py
```

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

## 部署架构图

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