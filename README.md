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

### 1. 检查并配置 npm 和 pip

如未安装 npm 或 pip，先安装。然后配置镜像加速：

```bash
# 检测并安装 pip（如未安装）
if ! command -v pip3 &>/dev/null; then
    sudo apt-get install -y python3-pip
fi

# 检测并安装 npm（如未安装）
if ! command -v npm &>/dev/null; then
    sudo apt-get install -y npm
fi

# 配置 npm 镜像
npm config set registry https://registry.npmmirror.com/

# 配置 pip 镜像
pip3 config set global.index-url https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple
```

### 2. 配置 Hermes Gateway API Server

在 `${HOME}/.hermes/config.yaml` 中添加或确认以下配置：

```yaml
gateway:
  api_server:
    enabled: true
    host: 127.0.0.1
    port: 8642
    api_key: "hermes-website-search"          # 如需认证则填入密钥，留空则无认证
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

### 3. 安装 hermes-skill-proxy

```bash
git clone https://github.com/inexbot/hermes-skill-proxy.git "${HOME}/hermes-skill-proxy"
cd "${HOME}/hermes-skill-proxy"
pip3 install flask requests
```

### 4. 安装 systemd 服务

```bash
SERVICE_FILE="${HOME}/hermes-skill-proxy/hermes-skill-proxy.service"
sudo cp "${SERVICE_FILE}" /etc/systemd/system/hermes-skill-proxy.service
# 将服务文件中的路径替换为实际用户目录
sudo sed -i "s|/home/inexbot|${HOME}|g" /etc/systemd/system/hermes-skill-proxy.service
sudo systemctl daemon-reload
sudo systemctl enable hermes-skill-proxy
sudo systemctl start hermes-skill-proxy
```

### 5. 爬取一次知识库（首次部署）

```bash
python3 ~/.hermes/skills/productivity/inexbot-knowledge-base/scripts/crawler.py
```

确保 skill 目录存在，否则 `skill_loaded` 为 `false`。可用以下命令验证：
```bash
ls "${HOME}/.hermes/skills/productivity/inexbot-knowledge-base/SKILL.md"
```

### 6. 配置定时任务（每日 11:00 自动爬取）

```bash
cronjob action=create \
  name="inexbot-knowledge-base daily crawl" \
  prompt="爬取纳博特科技知识库 https://doc.inexbot.com

步骤：
1. 运行爬虫脚本：
   python3 ~/.hermes/skills/productivity/inexbot-knowledge-base/scripts/crawler.py
2. 检查输出 ~/.hermes/kb/inexbot/ 是否包含 index.json 和 md/ 目录
3. 记录爬取结果（成功/失败/页数）" \
  schedule="0 11 * * *" \
  skills='["productivity/inexbot-knowledge-base"]' \
  deliver=local
```

### 7. 验证

```bash
curl http://localhost:8643/health
```

期望返回：
```json
{"status": "ok", "skill": "inexbot-knowledge-base", "skill_loaded": true, "hermes_url": "http://localhost:8642"}
```

如 `skill_loaded` 为 `false`，检查 skill 目录是否正确克隆到 `${HOME}/.hermes/skills/productivity/inexbot-knowledge-base/`。

验证知识库已爬取：
```bash
ls "${HOME}/.hermes/kb/inexbot/index.json"
```

验证定时任务已创建：
```bash
cronjob action=list | grep inexbot-knowledge-base
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
| `HERMES_API_KEY` | `hermes-website-search` | 与 Gateway api_key 一致时填入 |

### Gateway 与 Proxy 的对应关系

| 项目 | Gateway | Proxy |
|------|---------|-------|
| 监听地址 | `127.0.0.1` | `0.0.0.0` |
| 端口 | `8642` | `8643` |
| api_key | `config.yaml` 中的值 | `HERMES_API_KEY` 环境变量 |

如果 Gateway 配置了 `api_key`，Proxy 启动时需要设置相同密钥：
```bash
HERMES_API_KEY=hermes-website-search PROXY_PORT=8643 python hermes-skill-proxy.py
```

## API 接口

Proxy 监听 `:8643`，对外提供以下接口：

### `POST /v1/chat/completions`

OpenAI 兼容的 chat completions 接口。会自动将 skill 内容注入到 system prompt，再转发给 Hermes Gateway。

**请求头**
```
Content-Type: application/json
Authorization: Bearer <HERMES_API_KEY>   # 仅当 Gateway 配置了 api_key 时需要
```

**请求体**（OpenAI 格式）
```json
{
  "model": "MiniMax-M2.7",
  "messages": [
    {"role": "system", "content": "你是一个助手"},
    {"role": "user", "content": "工具手标定有几种方法？"}
  ],
  "stream": true
}
```

**响应**：流式返回，与 Hermes Gateway 的响应完全一致。

---

### `GET /health`

健康检查接口。

**响应**
```json
{
  "status": "ok",
  "skill": "inexbot-knowledge-base",
  "skill_loaded": true,
  "hermes_url": "http://localhost:8642"
}
```

| 字段 | 说明 |
|------|------|
| `status` | `"ok"` 表示 Proxy 正常运行 |
| `skill` | 当前注入的 skill 名称 |
| `skill_loaded` | `true` = skill 已加载；`false` = 未找到 skill 目录 |
| `hermes_url` | Hermes Gateway 的地址 |

---

### 与原生 OpenAI API 的兼容性

Proxy 完全兼容 OpenAI Chat Completions API，客户端可直接替换 base URL：

| 对比项 | 原生 Hermes | 经由 Proxy |
|--------|-----------|-----------|
| Base URL | `http://localhost:8642/v1` | `http://localhost:8643/v1` |
| 认证 | `Authorization` header | 同左 |
| 请求格式 | OpenAI 格式 | OpenAI 格式 |
| stream | 支持 | 支持 |

客户端只需把 base URL 从 `8642` 改成 `8643`，其余代码无需修改。

## 运维命令

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