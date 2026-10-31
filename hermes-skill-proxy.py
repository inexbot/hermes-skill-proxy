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
import sys
import json
import re
import time
import hashlib
import threading
from datetime import datetime
from pathlib import Path
from urllib.parse import quote as url_quote, unquote

from flask import Flask, request, Response
import requests
import jieba

# ── 配置 ──────────────────────────────────────────────────────────────────

HERMES_URL = os.getenv("HERMES_URL", "http://localhost:8642")
HERMES_API_KEY = os.getenv("HERMES_API_KEY", "hermes-website-search")
PROXY_PORT = int(os.getenv("PROXY_PORT", "8643"))

# 上游请求超时（秒）。
# ⚠️ 2026-10-31：原先硬编码 timeout=120，但 KB 检索后注入的 system prompt
# 约 12k–20k 字符，实测 gateway 需要 30–140s 才返回 —— 120s 会让
# 「查手册类问题」随机失败成 HTTP 502（实测 4 次里挂 2 次）。
# 改为可配置，默认 300s（可用 HERMES_TIMEOUT 环境变量覆盖）。
HERMES_TIMEOUT = int(os.getenv("HERMES_TIMEOUT", "300"))

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
FEEDBACK_FILE = Path.home() / ".hermes" / "kb" / "inexbot" / "feedback.log"
RELOAD_INTERVAL = 5 * 3600  # 5 小时
TOP_K = 5  # 每个知识库取 top-k 条结果

# 系统关键词 —— 从索引文档标题自动派生（每次重载时更新）
SYSTEM_KEYWORDS: set = set()

# 虚词黑名单（jieba 分词结果中需要过滤掉的常见非技术词）
_STOP_WORDS = {
    "一个", "一些", "一种", "一起", "一般", "不同", "不用", "不能",
    "不要", "之后", "之前", "之间", "什么", "介绍", "使用", "例如",
    "关于", "其他", "其中", "内容", "几个", "出来", "到了", "功能",
    "包括", "只要", "可以", "可能", "各种", "同时", "如何", "如果",
    "对于", "就是", "已经", "并且", "开始", "必须", "怎么", "所有",
    "所以", "整个", "文件", "文章", "方法", "时候", "更多", "查看",
    "没有", "注意", "然后", "版本", "现在", "由于", "相关", "说明",
    "这些", "这里", "这样", "通过", "部分", "需要", "这个", "进行",
    "还是", "还有", "那些", "那么", "都是", "问题", "页面", "首页",
    "其他", "以及", "关于", "为了", "能够", "根据",
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
    _rebuild_system_keywords()

def _rebuild_system_keywords():
    """从文档库标题自动派生系统关键词（零配置，新增文档自动生效）"""
    global SYSTEM_KEYWORDS
    from collections import Counter
    counter = Counter()
    for name, idx in _index_data.items():
        if not name.startswith("inexbot") or name == "inexbot-open":
            continue  # 只从 doc.inexbot.com 派生，不包含 open 平台（偏开发）
        for path, item in idx.items():
            title = item.get("title", "")
            words = jieba.cut(title)
            for w in words:
                w = w.strip()
                # 保留中文词（>=2字）和英文技术缩写
                if len(w) < 2 or w in _STOP_WORDS:
                    continue
                is_chinese = not w.isascii()
                is_short_en = w.isascii() and len(w) <= 3
                if is_chinese or is_short_en:
                    counter[w] += 1
    # 保留出现 ≥2 次的词（过滤偶然词和噪声），加上英文缩写（始终保留）
    system_kw = {w for w, c in counter.items() if c >= 2}
    # 补回英文技术缩写（jieba 分词对英文切得碎，但标题中常见）
    for name, idx in _index_data.items():
        for path, item in idx.items():
            title = item.get("title", "")
            for m in re.finditer(r'\b[A-Z][A-Za-z0-9]{1,15}\b', title):
                system_kw.add(m.group())
    SYSTEM_KEYWORDS = system_kw
    print(f"[proxy] Auto-derived {len(SYSTEM_KEYWORDS)} system keywords from doc titles")

# ── 定时重载 ──────────────────────────────────────────────────────────────

def schedule_reload():
    """
    定时重载索引。

    ⚠️ 重载失败（如 index.json 正被爬虫重写、JSON 解析失败）会让本函数抛异常，
    而 threading.Timer 的回调异常**不会**重新排期 —— 定时器链条就此断裂，
    proxy 会永久停留在旧索引上（实测曾出现 48 小时不重载）。
    所以这里必须 try/except，并且**无论如何都要重新排下一次定时器**。
    """
    try:
        load_all_indexes()
    except Exception as e:
        # 打印到 stderr（stdout 在 systemd 下是块缓冲，journal 里可能延迟很久才出现）
        print(f"[proxy] RELOAD FAILED: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
    finally:
        t = threading.Timer(RELOAD_INTERVAL, schedule_reload)
        t.daemon = True
        t.start()

# ── 内存检索 ──────────────────────────────────────────────────────────────

def _kb_url(base_url: str, path: str) -> str:
    """
    由 index.json 的 path key 拼出可点击的引用链接。

    ⚠️ index.json 的 key 有两种形态，编码方式相反：
      - inexbot (doc.inexbot.com)：key **已经是 percent 编码**的
        （如 /zh/%E6%8A%80%E6%9C%AF%E8%B5%84%E6%96%99）。
        若再 url_quote() 一次 → % 变成 %25 → 双重编码 → **链接 404**。
      - inexbot-open (open.inexbot.com)：key 是**解码后的明文中文**
        （如 /zh/04.上位机/01.C++/），必须编码后才能用。

    判据：key 里已含 '%' 说明它已是编码态 → 保留既有转义，只编码其余字符；
    否则按明文整体编码。两个 KB 实测均为 200。
    """
    if "%" in path:
        return base_url + url_quote(path, safe="/%")
    return base_url + url_quote(path, safe="/")


def _md_slug_candidates(path: str) -> list[str]:
    """
    解析 index 里的 path -> md 文件名候选（按优先级）。

    ⚠️ 跨组件隐式契约：crawler 的 slugify() 决定了 md 文件名，
    这里必须与各 crawler 保持一致，否则正文读不到（会静默退化成
    index.json 里的 800 字摘要，或抛 OSError 导致整个请求 500）。

    各 crawler 的 slugify() 实况：
      - inexbot (doc.inexbot.com, VitePress v2 alpha)：
          unquote -> replace("/","-") -> 去 [<>:"\|?*] -> 超 200 字节则 MD5 截断
          路径本身是 URL 编码的（/zh/%E4%BA%A7...），所以必须先 unquote。
      - inexbot-open (open.inexbot.com)：
          replace("/","-") -> 去 [<>:"\|?*]，**不做 unquote、不做长度截断**
          路径已是解码后的中文，且各 locale 前缀（zh-/en-/kr-）是路径的一部分。

    这里对两个 KB 都把「解码版」和「原样版」都作为候选，逐个试，
    谁先存在用谁 —— 这样即便某个 crawler 改了 slug 规则也不会全线崩。
    """
    raw = path.lstrip("/")
    candidates = []

    def _add(s: str):
        if s and s not in candidates:
            candidates.append(s)

    # 1) 未经解码（原样）—— open KB 的真实文件名就是这个
    _add(raw.replace("/", "-").replace("\\", "-"))
    # 2) URL 解码后 —— inexbot KB 的真实文件名
    try:
        decoded = unquote(raw)
    except Exception:
        decoded = raw
    _add(decoded.replace("/", "-").replace("\\", "-"))
    # 3) 二次编码（旧版 proxy 逻辑，保留作兜底）
    try:
        _add(url_quote(raw, safe="").replace("/", "-"))
    except Exception:
        pass

    # 清洗非法字符 + 超长时按 crawler 的规则截断
    out = []
    for s in candidates:
        try:
            s = re.sub(r'[<>:"|?*]', '', s)
        except Exception:
            pass
        if len(s.encode("utf-8")) > 200:
            head = s.encode("utf-8")[:184].decode("utf-8", errors="ignore")
            s = f"{head}-{hashlib.md5(s.encode('utf-8')).hexdigest()[:8]}"
        out.append(s)
    return out


def read_full_content(kb_name: str, path: str, query: str = "") -> str:
    """读取文档内容，提取与 query 最相关的段落（不是只取开头）"""
    md_dir = _kb_md_dirs.get(kb_name)
    if not md_dir:
        return ""
    md_path = None
    for slug in _md_slug_candidates(path):
        cand = md_dir / f"{slug}.md"
        try:
            if cand.exists():
                md_path = cand
                break
        except OSError:
            # 文件名过长 (Errno 36) 等 —— 降级到下一个候选，不要冒泡成 500
            continue
    if md_path is None:
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
    # 精确短语匹配加分（完整查询字符串出现在段落中，权重是普通词匹配的 5 倍）
    query_lower = query.strip().lower()
    scored = []
    for sec in sections:
        score = sum(1 for w in query_words if w in sec)
        # 精确短语加分：段落包含完整查询字符串
        if query_lower and query_lower in sec.lower():
            score += 10
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


def _expand_query_words(query: str) -> set:
    """扩展查询词：jieba 分词 + 组合词 + 原始短语"""
    words = set()
    # 1. jieba 基础分词
    for w in jieba.cut(query):
        w = w.strip()
        if len(w) >= 2:
            words.add(w)
    # 2. 添加完整查询作为短语匹配（处理"工具手标定"这类专有名词）
    query_clean = query.strip()
    if len(query_clean) >= 2:
        words.add(query_clean)
    # 3. 滑窗切分（2-4字组合词），提高召回
    for n in range(2, min(len(query_clean) + 1, 5)):
        for i in range(len(query_clean) - n + 1):
            chunk = query_clean[i:i+n]
            if not chunk.isspace():
                words.add(chunk)
    return words


def search_single_kb(kb_name: str, query: str, top_k: int = 3) -> list:
    """在单个知识库中检索"""
    if kb_name not in _doc_scores or kb_name not in _index_data:
        return []

    ds = _doc_scores[kb_name]
    idx = _index_data[kb_name]
    base_url = _kb_urls.get(kb_name, "")

    query_words = _expand_query_words(query)
    query_lower = query.strip().lower()

    scores = {}
    for path, item_ds in ds.items():
        score = 0.0
        title = idx[path].get("title", "").lower()
        desc = idx[path].get("description", "").lower()

        # 精确短语匹配（最高权重）
        if query_lower and query_lower in title:
            score += 20
        elif query_lower and query_lower in desc:
            score += 10

        for w in query_words:
            if w in item_ds["title_tokens"]:
                score += 3
            if w in item_ds["desc_tokens"]:
                score += 2
            if w in item_ds["content_tokens"]:
                score += 1
            if w in item_ds["word_counts"]:
                score += 0.5 * min(item_ds["word_counts"][w], 5)

        if score > 0:
            scores[path] = score

    # 相关性阈值：过滤掉明显低分的文档
    if not scores:
        return []
    max_score = max(scores.values())
    threshold = max_score * 0.3  # 低于最高分 30% 的过滤掉
    scores = {p: s for p, s in scores.items() if s >= threshold}

    ranked = sorted(scores.items(), key=lambda x: -x[1])[:top_k]
    results = []
    for path, score in ranked:
        item = idx[path]
        snippet = item.get("content_snippet", "")
        full = read_full_content(kb_name, path, query)
        if full:
            snippet = full

        results.append({
            "title": item.get("title", ""),
            "path": path,
            "url": _kb_url(base_url, path),
            "description": item.get("description", ""),
            "content_snippet": snippet[:4000],  # 从8000降到4000，减少噪声
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
    """搜索知识库（根据问题意图定向搜索）"""
    allowed = classify_intent(query)
    single_kb = len(allowed) == 1
    k = 5 if single_kb else 3  # 单库取5篇，双库各取3篇（减少噪声）

    all_results = {}
    for cfg in KB_CONFIGS:
        name = cfg["name"]
        if name not in allowed:
            continue
        results = search_single_kb(name, query, k)
        if results:
            all_results[cfg["label"]] = results

    # 调试日志：打印检索结果
    total = sum(len(v) for v in all_results.values())
    print(f"[proxy] Query: {query[:80]}")
    print(f"[proxy] Intent: {allowed}, Results: {total}")
    for label, items in all_results.items():
        for item in items:
            print(f"[proxy]   [{item['score']:.1f}] {item['title'][:60]}")

    return all_results

def format_results(all_results: dict) -> str:
    """格式化所有检索结果为 prompt 文本"""
    if not all_results:
        return ""

    total = sum(len(v) for v in all_results.values())
    lines = [
        "",
        "【知识库检索结果】",
        f"以下是从纳博特文档库中检索到的 {total} 篇相关内容：",
        "",
    ]

    doc_num = 0
    for kb_label, results in all_results.items():
        for item in results:
            doc_num += 1
            lines.append(f"--- 文档 {doc_num}（相关度：{item['score']:.1f}）---")
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
    lines.append("1. 先判断上面的文档是否与用户问题相关，只使用相关文档回答")
    lines.append("2. 如果文档与问题不相关或信息不足，明确告知「知识库中未找到相关信息」，不要编造")
    lines.append("3. 如果有相关文档，详尽回答，列出具体步骤、参数说明和示例")
    lines.append("4. 文档正文中的配图如果与回答相关，直接复制 ![说明](链接) 放在对应文字后")
    lines.append("5. 答案末尾直接复制粘贴下面的「引用清单」，只列出实际使用的文档")
    lines.append("6. 使用专业的技术语言，用 Markdown 表格/代码块/列表组织详细内容")
    lines.append("")
    lines.append("【引用清单】（只复制实际使用的文档）")
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
            "【当前状态】知识库未搜索到直接匹配的文档，但请基于你对纳博特/NexDroid控制系统、"
            "工业机器人技术的训练知识，尽力回答用户问题。\n\n"
            "回答要求：\n"
            "1. 优先基于你对纳博特产品的了解直接回答，列出具体步骤和参数\n"
            "2. 如果确实不了解某个具体细节，明确说明「该部分建议查阅官方文档确认」，不要编造\n"
            "3. 使用简洁专业的技术语言，适当使用 Markdown 来组织回答\n"
            "4. 仅使用以下合法链接：https://doc.inexbot.com/ 或 https://open.inexbot.com/ 或 https://www.inexbot.com\n"
            "5. 不要出现 ~/workspace 或任何本地路径\n"
            "6. 不要直接说「我不知道」，尝试给用户有参考价值的回答"
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
            timeout=HERMES_TIMEOUT,
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


@app.route("/api/feedback", methods=["POST"])
def feedback():
    """记录用户对 AI 回答的反馈（点赞/差评）"""
    if not request.is_json:
        return Response(json.dumps({"error": "JSON required"}), status=400)

    data = request.get_json()
    question = (data.get("question") or "").strip()[:500]
    answer = (data.get("answer") or "").strip()[:2000]
    rating = data.get("rating", "")

    if not question:
        return Response(json.dumps({"error": "question is required"}), status=400)
    if rating not in ("up", "down"):
        return Response(json.dumps({"error": "rating must be 'up' or 'down'"}), status=400)

    try:
        os.makedirs(os.path.dirname(FEEDBACK_FILE), exist_ok=True)
        entry = {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "question": question,
            "answer": answer,
            "rating": rating,
        }
        with open(FEEDBACK_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        print(f"[proxy] Feedback recorded: {rating} — {question[:80]}")
    except Exception as e:
        print(f"[proxy] feedback error: {e}")
        return Response(json.dumps({"error": "failed to save feedback"}), status=500)

    return Response(json.dumps({"status": "ok"}), status=200)


if __name__ == "__main__":
    # 启动时加载知识库索引
    load_all_indexes()

    # 启动定时重载
    schedule_reload()

    print(f"[proxy] Starting Hermes Skill Proxy on port {PROXY_PORT}")
    print(f"[proxy] Forwarding to {HERMES_URL}")
    print(f"[proxy] Reload interval: {RELOAD_INTERVAL}s ({RELOAD_INTERVAL/3600}h)")
    for cfg in KB_CONFIGS:
        name = cfg["name"]
        n = len(_index_data.get(name, {}))
        print(f"[proxy]   {name}: {n} docs")
    app.run(host="0.0.0.0", port=PROXY_PORT, threaded=True)
