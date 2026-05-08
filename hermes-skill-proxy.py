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
from flask import Flask, request, Response
import requests

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
        sys.path.insert(0, os.path.expanduser("~/.hermes/hermes-agent"))
        from agent.skill_commands import build_preloaded_skills_prompt
        prompt, loaded, missing = build_preloaded_skills_prompt([SKILL_NAME], task_id=None)
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


def proxy_request():
    if not request.is_json:
        return Response("application/json required", status=400)

    body = request.get_json()

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
