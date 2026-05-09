"""
Hermes API Proxy — 自动注入 skill 到 system prompt
不修改 Hermes 源码，只做请求转发和 prompt 注入

用法:
  python hermes-skill-proxy.py
  # 默认监听 8643，转发到 localhost:8642

环境变量:
  PROXY_PORT=8643
  HERMES_URL=http://localhost:8642
  HERMES_API_KEY=xxx          # 如果 Hermes 开启了认证
  SKILL_NAME=inexbot-knowledge-base
"""

import os
import json
import re
from datetime import datetime
from flask import Flask, request, Response
import requests

LOG_FILE = os.path.expanduser("~/.hermes/kb/inexbot/questions.log")

app = Flask(__name__)

HERMES_URL = os.getenv("HERMES_URL", "http://localhost:8642")
HERMES_API_KEY = os.getenv("HERMES_API_KEY", "")
PROXY_PORT = int(os.getenv("PROXY_PORT", "8643"))
SKILL_NAME = os.getenv("SKILL_NAME", "inexbot-knowledge-base")

# 预加载 skill 内容（启动时加载一次）
SKILL_PROMPT = None

def load_skill_prompt():
    global SKILL_PROMPT
    try:
        import sys
        import importlib.util
        hermes_agent_path = os.path.expanduser("~/.hermes/hermes-agent")
        spec = importlib.util.find_spec("agent.skill_commands", hermes_agent_path)
        if spec is None:
            print(f"[proxy] WARNING: agent.skill_commands not found in {hermes_agent_path}")
            return
        module = importlib.util.module_from_spec(spec)
        sys.modules["agent.skill_commands"] = module
        spec.loader.exec_module(module)
        build_fn = getattr(module, "build_preloaded_skills_prompt", None)
        if not build_fn:
            print(f"[proxy] WARNING: build_preloaded_skills_prompt not found in agent.skill_commands")
            return
        prompt, loaded, missing = build_fn([SKILL_NAME], task_id=None)
        if loaded:
            SKILL_PROMPT = (
                "【自动加载的 Skill 指令】\n\n"
                + prompt
            )
            print(f"[proxy] Skill '{SKILL_NAME}' loaded, {len(SKILL_PROMPT)} chars")
        else:
            print(f"[proxy] WARNING: Skill '{SKILL_NAME}' not found, missing: {missing}")
    except Exception as e:
        print(f"[proxy] ERROR loading skill: {e}")

load_skill_prompt()


def log_question(body):
    """将用户问题记录到本地文件"""
    try:
        # 确保目录存在
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        # 从 messages 中提取最后一条 user 消息
        question = ""
        for msg in reversed(body.get("messages", [])):
            if msg.get("role") == "user":
                question = msg.get("content", "")[:500]  # 截断超长问题
                break
        if question:
            entry = {
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "question": question
            }
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[proxy] log_question error: {e}")


def proxy_request():
    if not request.is_json:
        return Response("application/json required", status=400)

    body = request.get_json()
    log_question(body)

    # 从请求体或 header 获取 auth key
    auth = request.headers.get("Authorization", "")
    if HERMES_API_KEY and not auth:
        auth = f"Bearer {HERMES_API_KEY}"

    # 注入 system prompt
    skill_block = SKILL_PROMPT or ""

    # 处理 messages 中的 system 消息
    modified = False
    new_messages = []
    for msg in body.get("messages", []):
        if msg.get("role") == "system":
            modified = True
            new_messages.append({
                **msg,
                "content": skill_block + "\n\n" + (msg.get("content") or "")
            })
        else:
            new_messages.append(msg)

    if not modified:
        # 没有 system 消息，在最前面插入
        new_messages.insert(0, {
            "role": "system",
            "content": skill_block
        })

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
        "skill": SKILL_NAME,
        "skill_loaded": SKILL_PROMPT is not None,
        "hermes_url": HERMES_URL,
    }), status=200)


if __name__ == "__main__":
    print(f"[proxy] Starting Hermes Skill Proxy on port {PROXY_PORT}")
    print(f"[proxy] Forwarding to {HERMES_URL}")
    print(f"[proxy] Auto-injecting skill: {SKILL_NAME}")
    app.run(host="0.0.0.0", port=PROXY_PORT, threaded=True)
