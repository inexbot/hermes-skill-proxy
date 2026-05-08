# Hermes Skill Proxy

Hermes API Proxy — 自动注入 skill 到 system prompt，**不修改 Hermes 源码**。

## 原理

```
客户端 → Proxy (:8643) → Hermes API Server (:8642)
              ↓
         自动注入 skill 到 system prompt
```

Proxy 在收到 `/v1/chat/completions` 请求时，读取 Hermes 本地的 skill 内容，拼接进 system prompt 再转发给 Hermes。从客户端视角完全透明。

## 依赖

- Python 3.8+
- Flask
- requests

```bash
pip install flask requests
```

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `PROXY_PORT` | `8643` | Proxy 监听端口 |
| `HERMES_URL` | `http://localhost:8642` | Hermes API Server 地址 |
| `HERMES_API_KEY` | `(空)` | Hermes 认证密钥，如有需要 |
| `SKILL_NAME` | `inexbot-knowledge-base` | 要自动注入的 skill 名称 |

## 快速开始

```bash
# 前台运行
python hermes-skill-proxy.py

# 或指定参数
HERMES_URL=http://localhost:8642 PROXY_PORT=8643 python hermes-skill-proxy.py
```

## systemd 部署

```bash
# 复制服务文件
sudo cp hermes-skill-proxy.service /etc/systemd/system/

# 重载 systemd
sudo systemctl daemon-reload

# 启用开机启动
sudo systemctl enable hermes-skill-proxy

# 启动服务
sudo systemctl start hermes-skill-proxy

# 查看状态
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

## License

MIT
