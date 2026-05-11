"""
Hermes API Proxy — 自动检索知识库 + 注入 system prompt

启动时加载 index.json 到内存，每 5 小时自动重载。
每次请求在内存中做关键词匹配（毫秒级），不跑子进程。

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

KB_ROOT = Path.home() / ".hermes" / "kb" / "inexbot"
INDEX_FILE = KB_ROOT / "index.json"
BASE_URL = "https://doc.inexbot.com"
LOG_FILE = KB_ROOT / "questions.log"
RELOAD_INTERVAL = 5 * 3600  # 5 小时

# ── 知识库内存索引 ────────────────────────────────────────────────────────

# 预计算的数据结构
_index_data: dict = {}          # 原始 index.json 内容
_doc_scores: dict = {}          # path → {"title_tokens": set, "desc_tokens": set, "word_counts": dict}

def load_index():
    """加载 index.json 并预计算检索所需的数据结构"""
    global _index_data, _doc_scores

    if not INDEX_FILE.exists():
        print(f"[proxy] WARNING: index.json not found at {INDEX_FILE}")
        _index_data = {}
        _doc_scores = {}
        return

    with open(INDEX_FILE, encoding="utf-8") as f:
        _index_data = json.load(f)

    _doc_scores = {}
    for path, item in _index_data.items():
        title_words = set(jieba.cut(item.get("title", "")))
        title_words = {w for w in title_words if len(w) >= 2}
        desc_words = set(jieba.cut(item.get("description", "")))
        desc_words = {w for w in desc_words if len(w) >= 2}
        _doc_scores[path] = {
            "title_tokens": title_words,
            "desc_tokens": desc_words,
            "word_counts": item.get("word_counts", {}),
        }

    print(f"[proxy] Index loaded: {len(_index_data)} docs")

load_index()

# ── 定时重载 ──────────────────────────────────────────────────────────────

def schedule_reload():
    load_index()
    threading.Timer(RELOAD_INTERVAL, schedule_reload).start()

# 启动 5 小时后的第一次重载
threading.Timer(RELOAD_INTERVAL, schedule_reload).start()

# ── 内存检索 ──────────────────────────────────────────────────────────────

def search_kb(query: str, top_k: int = 5) -> list:
    """在内存索引中检索相关文档，返回 top_k 条"""
    if not _doc_scores:
        return []

    query_words = set(jieba.cut(query))
    query_words = {w for w in query_words if len(w) >= 2}

    scores = {}
    for path, ds in _doc_scores.items():
        score = 0.0
        for w in query_words:
            if w in ds["title_tokens"]:
                score += 4
            if w in ds["desc_tokens"]:
                score += 2
            if w in ds["word_counts"]:
                score += 0.5 * ds["word_counts"][w]

        if score > 0:
            scores[path] = score

    ranked = sorted(scores.items(), key=lambda x: -x[1])[:top_k]
    return [_index_data[path] for path, _ in ranked]


def format_kb_results(results: list) -> str:
    """格式化检索结果为 prompt 文本"""
    if not results:
        return ""

    lines = [
        "",
        "【知识库检索结果】",
        f"以下是从纳博特文档库中检索到的 {len(results)} 篇相关内容，请直接基于这些内容回答用户问题：",
        "",
    ]

    for i, item in enumerate(results, 1):
        title = item.get("title", "")
        path = item.get("path", "")
        url = BASE_URL + path
        desc = item.get("description", "")
        snippet = item.get("content_snippet", "")[:800]

        lines.append(f"--- 文档 {i} ---")
        lines.append(f"标题：{title}")
        lines.append(f"链接：{url}")
        if desc:
            lines.append(f"简介：{desc}")
        if snippet:
            lines.append(f"正文摘要：{snippet}")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("【回答要求】")
    lines.append("1. 直接基于上面检索到的知识库内容回答，不要凭记忆猜测")
    lines.append("2. 如果检索内容足以回答问题，给出完整答案；如果内容覆盖不足，明确说明'文档中未找到完整信息'并基于已有内容给出部分答案")
    lines.append("3. 答案末尾必须列出所有引用过的文档链接，格式为：📄 原文：标题 | 链接")
    lines.append("4. 使用简洁专业的技术语言，适当使用 Markdown 格式")

    return "\n".join(lines)


# ── 日志记录 ──────────────────────────────────────────────────────────────

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


# ── 核心代理逻辑 ──────────────────────────────────────────────────────────

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

    # 内存检索知识库
    kb_results = search_kb(user_question, top_k=5)
    kb_context = format_kb_results(kb_results) if kb_results else ""

    # 构建 system prompt
    if kb_context:
        # 有检索结果：注入真实内容
        system_prompt = (
            "你是一个专业的纳博特科技（iNexBot）工业机器人AI助手，你可以直接使用下面的检索内容回答问题。\n"
            + kb_context
        )
    else:
        # 无检索结果：通用提示
        system_prompt = (
            "你是一个专业的纳博特科技（iNexBot）工业机器人AI助手。\n"
            "回答要求：\n"
            "1. 如果你了解纳博特相关产品和技术，直接回答\n"
            "2. 如果不确定，建议用户查阅 https://doc.inexbot.com 或联系技术支持\n"
            "3. 使用简洁专业的技术语言，适当使用 Markdown 格式来组织回答\n"
            "4. 不要出现 ~/workspace 或任何本地路径\n"
            "5. 链接必须是 https://doc.inexbot.com/ 或 https://www.inexbot.com 开头"
        )

    # 从请求体获取 auth
    auth = request.headers.get("Authorization", "")
    if HERMES_API_KEY and not auth:
        auth = f"Bearer {HERMES_API_KEY}"

    # 注入 system prompt（替换或插入）
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

    # 流式转发
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
    return Response(json.dumps({
        "status": "ok",
        "index_docs": len(_index_data),
        "index_loaded": len(_index_data) > 0,
        "hermes_url": HERMES_URL,
    }), status=200)


if __name__ == "__main__":
    print(f"[proxy] Starting Hermes Skill Proxy on port {PROXY_PORT}")
    print(f"[proxy] Forwarding to {HERMES_URL}")
    print(f"[proxy] Index: {len(_index_data)} docs loaded")
    print(f"[proxy] Reload interval: {RELOAD_INTERVAL}s ({RELOAD_INTERVAL/3600}h)")
    app.run(host="0.0.0.0", port=PROXY_PORT, threaded=True)
