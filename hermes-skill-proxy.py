"""
Hermes API Proxy — 双知识库检索 + 注入 system prompt

支持两个知识库：
  - inexbot:       doc.inexbot.com（产品技术文档）
  - inexbot-open:   open.inexbot.com（开放平台/二次开发）

启动时加载两个 index.json 到内存，每 5 小时自动重载。
每次请求在内存中做关键词匹配（毫秒级）。

用法:
  python hermes-skill-proxy.py
  # 默认监听 8643，转发到 localhost:8642

环境变量:
  PROXY_PORT=8643
  HERMES_URL=http://localhost:8642
  HERMES_API_KEY=hermes-website-search
"""

import os
import json
import time
import threading
from datetime import datetime
from pathlib import Path

from flask import Flask, request, Response
import requests
import jieba

# ── 配置 ──────────────────────────────────────────────────────────────────

HERMES_URL = os.getenv("HERMES_URL", "http://localhost:8642")
HERMES_API_KEY = os.getenv("HERMES_API_KEY", "hermes-website-search")
PROXY_PORT = int(os.getenv("PROXY_PORT", "8643"))

# 双知识库配置
KB_CONFIGS = [
    {
        "name": "inexbot",
        "label": "纳博特产品文档",
        "base_url": "https://doc.inexbot.com",
        "index_file": Path.home() / ".hermes" / "kb" / "inexbot" / "index.json",
        "md_dir": Path.home() / ".hermes" / "kb" / "inexbot" / "md",
    },
    {
        "name": "inexbot-open",
        "label": "纳博特开放平台",
        "base_url": "https://open.inexbot.com",
        "index_file": Path.home() / ".hermes" / "kb" / "inexbot-open" / "index.json",
        "md_dir": Path.home() / ".hermes" / "kb" / "inexbot-open" / "md",
    },
]

LOG_FILE = Path.home() / ".hermes" / "kb" / "inexbot" / "questions.log"
RELOAD_INTERVAL = 5 * 3600  # 5 小时
TOP_K = 2  # 每个知识库取 top-k 条结果（少而精，给每篇更多空间）

# 问题类型关键词（用于定向搜索知识库）
SYSTEM_KEYWORDS = {
    "标定", "焊接", "控制器", "示教器", "通讯",
    "总线", "故障", "报错", "版本", "规格", "选型", "码垛", "喷涂",
    "冲压", "打磨", "切割", "搬运", "传送带", "视觉", "传感器",
    "坐标系", "运动", "变量", "工艺", "伺服", "安全",
    "安装", "接线", "维护", "保养", "零点", "电机", "编码器",
    "Modbus", "EtherCAT", "Ethernet", "CAN", "Profinet",
    "IO", "急停", "使能", "手轮", "面板", "复位",
}

# 强开发关键词（命中任一即强制走 open）
STRONG_DEV_KEYWORDS = {
    "JSON", "API", "二次开发", "ROS", "SDK", "HAL",
    "JSON协议", "端口协议", "协议解析", "自定义指令", "上位机",
    "主站库", "C++", "Python", "C#", "回调", "socket",
    "源码", "GitHub", "插件", "SDK",
}

# 弱开发关键词（和系统词共存时才走 open）
WEAK_DEV_KEYWORDS = {
    "接口", "示例", "Demo", "教程", "功能码", "开发", "编程",
    "WebSocket", "HTTP", "gRPC", "线程",
}

DEV_KEYWORDS = STRONG_DEV_KEYWORDS | WEAK_DEV_KEYWORDS

# ── 知识库内存索引 ────────────────────────────────────────────────────────

# 数据结构: { "kb_name": { path: {"title_tokens": set, "desc_tokens": set, "word_counts": dict} } }
_doc_scores: dict = {}
# 原始 index.json 数据
_index_data: dict = {}
# 每个 KB 的 base_url
_kb_urls: dict = {}
# 每个 KB 的 md_dir（用于读取完整文档）
_kb_md_dirs: dict = {}

def load_single_index(cfg: dict):
    """加载单个知识库索引"""
    name = cfg["name"]
    index_file = cfg["index_file"]

    if not index_file.exists():
        print(f"[proxy] WARNING: {name} index not found at {index_file}")
        return

    with open(index_file, encoding="utf-8") as f:
        raw = json.load(f)

    _index_data[name] = raw
    _kb_urls[name] = cfg["base_url"]
    _kb_md_dirs[name] = cfg.get("md_dir")

    scores = {}
    for path, item in raw.items():
        title_words = set(jieba.cut(item.get("title", "")))
        title_words = {w for w in title_words if len(w) >= 2}
        desc_words = set(jieba.cut(item.get("description", "")))
        desc_words = {w for w in desc_words if len(w) >= 2}
        content_words = set(jieba.cut(item.get("content_snippet", "")))
        content_words = {w for w in content_words if len(w) >= 2}
        scores[path] = {
            "title_tokens": title_words,
            "desc_tokens": desc_words,
            "content_tokens": content_words,
            "word_counts": item.get("word_counts", {}),
        }
    _doc_scores[name] = scores
    print(f"[proxy] {name}: {len(raw)} docs loaded")

def load_all_indexes():
    """加载所有知识库"""
    for cfg in KB_CONFIGS:
        load_single_index(cfg)
    total = sum(len(v) for v in _index_data.values())
    print(f"[proxy] Total: {total} docs across {len(_index_data)} indexes")

load_all_indexes()

# ── 定时重载 ──────────────────────────────────────────────────────────────

def schedule_reload():
    load_all_indexes()
    threading.Timer(RELOAD_INTERVAL, schedule_reload).start()

threading.Timer(RELOAD_INTERVAL, schedule_reload).start()

# ── 内存检索 ──────────────────────────────────────────────────────────────

def read_full_content(kb_name: str, path: str, query: str = "") -> str:
    """读取文档内容，提取与 query 最相关的段落（不是只取开头）"""
    md_dir = _kb_md_dirs.get(kb_name)
    if not md_dir:
        return ""
    slug = path.lstrip("/").replace("/", "-")
    md_path = md_dir / f"{slug}.md"
    if not md_path.exists():
        from urllib.parse import quote
        slug2 = quote(path.lstrip("/"), safe="").replace("/", "-")
        md_path = md_dir / f"{slug2}.md"
    if not md_path.exists():
        return ""

    try:
        with open(md_path, encoding="utf-8") as f:
            content = f.read()
    except Exception:
        return ""

    if not query or len(content) < 12000:
        return content[:12000]

    # 按 ## 或 ### 标题分段
    import re
    sections = re.split(r'\n(?=#{2,3}\s)', content)
    if len(sections) <= 2:
        return content[:12000]

    query_words = {w for w in jieba.cut(query) if len(w) >= 2}
    if not query_words:
        return content[:12000]

    # 给每个段打分，取 top 段
    scored = []
    for sec in sections:
        score = sum(1 for w in query_words if w in sec)
        scored.append((score, sec))

    # 按相关性排序，高分的在前面
    scored.sort(key=lambda x: -x[0])
    # 取所有有分的段 + 少量低分段，最多 15000 字
    result = []
    total_len = 0
    for _, sec in scored:
        result.append(sec.strip())
        total_len += len(sec)
        if total_len > 15000:
            break

    return "\n\n".join(result)


def search_single_kb(kb_name: str, query: str, top_k: int = 3) -> list:
    """在单个知识库中检索"""
    if kb_name not in _doc_scores or kb_name not in _index_data:
        return []

    ds = _doc_scores[kb_name]
    idx = _index_data[kb_name]
    base_url = _kb_urls.get(kb_name, "")

    query_words = set(jieba.cut(query))
    query_words = {w for w in query_words if len(w) >= 2}

    scores = {}
    for path, item_ds in ds.items():
        score = 0.0
        for w in query_words:
            if w in item_ds["title_tokens"]:
                score += 4
            if w in item_ds["desc_tokens"]:
                score += 2
            if w in item_ds["word_counts"]:
                score += 0.5 * item_ds["word_counts"][w]
        if score > 0:
            scores[path] = score

    ranked = sorted(scores.items(), key=lambda x: -x[1])[:top_k]
    results = []
    for path, score in ranked:
        item = idx[path]
        # 读完整 MD，提取与问题最相关的段落
        snippet = item.get("content_snippet", "")
        if len(snippet) < 500:
            full = read_full_content(kb_name, path, query)
            if full:
                snippet = full

        results.append({
            "title": item.get("title", ""),
            "path": path,
            "url": base_url + path.replace(" ", "%20"),
            "description": item.get("description", ""),
            "content_snippet": snippet[:6000],
            "kb_name": kb_name,
            "score": score,
        })
    return results

def classify_intent(query: str) -> set:
    """根据问题关键词判断应该搜索哪些知识库"""
    has_strong_dev = any(kw in query for kw in STRONG_DEV_KEYWORDS)
    has_weak_dev = any(kw in query for kw in WEAK_DEV_KEYWORDS)
    has_system = any(kw in query for kw in SYSTEM_KEYWORDS)

    # 强开发关键词 → 只走 open
    if has_strong_dev:
        return {"inexbot-open"}

    # 系统关键词 → 只走 doc
    if has_system:
        return {"inexbot"}

    # 弱开发关键词但无系统词 → open
    if has_weak_dev:
        return {"inexbot-open"}

    # 都没命中 → 全搜
    return {"inexbot", "inexbot-open"}


def search_all_kb(query: str) -> dict:
    """搜索知识库（根据问题意图定向搜索，单库时多取结果）"""
    allowed = classify_intent(query)
    single_kb = len(allowed) == 1
    k = 6 if single_kb else 3

    all_results = {}
    for cfg in KB_CONFIGS:
        name = cfg["name"]
        if name not in allowed:
            continue
        results = search_single_kb(name, query, k)
        if results:
            all_results[cfg["label"]] = results
    return all_results

def format_results(all_results: dict) -> str:
    """格式化所有检索结果为 prompt 文本"""
    if not all_results:
        return ""

    total = sum(len(v) for v in all_results.values())
    lines = [
        "",
        "【知识库检索结果】",
        f"以下是从纳博特文档库中检索到的 {total} 篇相关内容，请直接基于这些内容回答用户问题：",
        "",
    ]

    doc_num = 0
    for kb_label, results in all_results.items():
        for item in results:
            doc_num += 1
            lines.append(f"--- 文档 {doc_num} ---")
            lines.append(f"标题：{item['title']}")
            lines.append(f"链接：{item['url']}")
            if item["description"]:
                lines.append(f"简介：{item['description']}")
            if item["content_snippet"]:
                lines.append(f"正文：{item['content_snippet']}")
            lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("【回答要求】")
    lines.append("1. 综合上面所有文档的内容详尽回答，列出具体步骤、参数说明和示例，不要省略细节")
    lines.append("2. 如果文档覆盖了用户问题的多个方面，每个方面都要详细说明")
    lines.append("3. 文档正文中的配图如果与回答相关，直接复制 ![说明](链接) 放在对应文字后")
    lines.append("4. 答案末尾直接复制粘贴下面的「引用清单」，不要自己编造链接")
    lines.append("5. 使用专业的技术语言，用 Markdown 表格/代码块/列表组织详细内容")
    lines.append("")
    lines.append("【引用清单】（直接复制到答案末尾）")
    for item in all_results.values():
        for doc in item:
            lines.append(f"- [{doc['title']}]({doc['url']})")
    lines.append("")

    return "\n".join(lines)


# ── 日志 ──────────────────────────────────────────────────────────────────

def log_question(body: dict):
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        question = ""
        for msg in reversed(body.get("messages", [])):
            if msg.get("role") == "user":
                question = msg.get("content", "")[:500]
                break
        if question:
            entry = {"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "question": question}
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[proxy] log_question error: {e}")


# ── Flask App ─────────────────────────────────────────────────────────────

app = Flask(__name__)


def proxy_request():
    if not request.is_json:
        return Response("application/json required", status=400)

    body = request.get_json()
    log_question(body)

    # 提取用户问题
    user_question = ""
    for msg in reversed(body.get("messages", [])):
        if msg.get("role") == "user":
            user_question = msg.get("content", "")
            break

    # 搜索所有知识库
    all_results = search_all_kb(user_question)
    kb_context = format_results(all_results) if all_results else ""

    # 构建 system prompt
    if kb_context:
        system_prompt = (
            "你是一个专业的纳博特科技（iNexBot）工业机器人AI助手。"
            "你可以直接使用下面的检索内容回答问题。\n"
            + kb_context
        )
    else:
        system_prompt = (
            "你是一个专业的纳博特科技（iNexBot）工业机器人AI助手。\n"
            "回答要求：\n"
            "1. 如果你了解纳博特相关产品和技术，直接回答\n"
            "2. 如果不确定，建议用户查阅 https://doc.inexbot.com 或 https://open.inexbot.com\n"
            "3. 使用简洁专业的技术语言，适当使用 Markdown 格式来组织回答\n"
            "4. 不要出现 ~/workspace 或任何本地路径\n"
            "5. 链接必须以 https://doc.inexbot.com/ 或 https://open.inexbot.com/ 或 https://www.inexbot.com 开头"
        )

    # 认证
    auth = request.headers.get("Authorization", "")
    if HERMES_API_KEY and not auth:
        auth = f"Bearer {HERMES_API_KEY}"

    # 注入 system prompt
    new_messages = []
    has_system = False
    for msg in body.get("messages", []):
        if msg.get("role") == "system":
            has_system = True
            new_messages.append({
                **msg,
                "content": system_prompt + "\n\n" + (msg.get("content") or "")
            })
        else:
            new_messages.append(msg)

    if not has_system:
        new_messages.insert(0, {"role": "system", "content": system_prompt})

    new_body = {**body, "messages": new_messages}
    # 确保 MiniMax 不限制输出长度
    if "max_tokens" not in new_body:
        new_body["max_tokens"] = 131072

    # 转发到 Hermes
    headers = {"Content-Type": "application/json"}
    if auth:
        headers["Authorization"] = auth

    try:
        resp = requests.post(
            f"{HERMES_URL}/v1/chat/completions",
            json=new_body,
            headers=headers,
            timeout=120,
            stream=True,
        )
    except Exception as e:
        return Response(json.dumps({"error": {"message": str(e)}}), status=502)

    def generate():
        for chunk in resp.iter_content(chunk_size=None):
            if chunk:
                yield chunk

    return Response(generate(), headers=dict(resp.headers))


@app.route("/v1/chat/completions", methods=["POST"])
def chat_completions():
    return proxy_request()


@app.route("/health", methods=["GET"])
def health():
    index_info = {}
    for cfg in KB_CONFIGS:
        name = cfg["name"]
        index_info[name] = {
            "docs": len(_index_data.get(name, {})),
            "loaded": name in _index_data,
        }
    return Response(json.dumps({
        "status": "ok",
        "indexes": index_info,
        "hermes_url": HERMES_URL,
    }), status=200)


if __name__ == "__main__":
    print(f"[proxy] Starting Hermes Skill Proxy on port {PROXY_PORT}")
    print(f"[proxy] Forwarding to {HERMES_URL}")
    print(f"[proxy] Reload interval: {RELOAD_INTERVAL}s ({RELOAD_INTERVAL/3600}h)")
    for cfg in KB_CONFIGS:
        name = cfg["name"]
        n = len(_index_data.get(name, {}))
        print(f"[proxy]   {name}: {n} docs")
    app.run(host="0.0.0.0", port=PROXY_PORT, threaded=True)
