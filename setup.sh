#!/bin/bash
#
# Hermes Skill Proxy 一键部署脚本
# 自动完成：克隆仓库、加载 skill、安装依赖、配置 systemd、启动服务
#
# 用法：
#   bash <(curl -fsSL https://raw.githubusercontent.com/inexbot/hermes-skill-proxy/main/setup.sh)
#
# 或克隆后本地运行：
#   chmod +x setup.sh && ./setup.sh
#

set -e

PROXY_DIR="${HOME}/hermes-skill-proxy"
SKILLS_DIR="${HOME}/.hermes/skills/productivity"
SKILL_NAME="inexbot-knowledge-base"
PROXY_REPO="inexbot/hermes-skill-proxy"
SKILL_REPO="inexbot/inexbot-knowledge-base"

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()    { echo -e "${GREEN}[INFO]${NC} $1"; }
warn()    { echo -e "${YELLOW}[WARN]${NC} $1"; }
error()   { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

# 检测网络连通性（带超时）
check_connectivity() {
  curl -sf --connect-timeout 5 https://github.com > /dev/null 2>&1
}

# 克隆仓库，支持 HTTPS 超时后自动切换 SSH
# 用法: git_clone_or_fetch <repo> <dir>
git_clone_or_fetch() {
  local repo=$1
  local dir=$2
  local url_https="https://github.com/${repo}.git"
  local url_ssh="git@github.com:${repo}.git"

  if [[ -d "${dir}" && -d "${dir}/.git" ]]; then
    info "目录已存在，更新中：${dir}"
    git -C "${dir}" pull origin main 2>/dev/null || git -C "${dir}" pull 2>/dev/null || true
    return 0
  fi

  info "克隆 ${repo}..."
  if git clone "${url_https}" "${dir}" --depth=1 2>&1; then
    info "HTTPS 克隆成功"
    return 0
  fi

  warn "HTTPS 克隆超时，尝试 SSH..."
  if command -v ssh &>/dev/null && git clone "${url_ssh}" "${dir}" --depth=1 2>&1; then
    info "SSH 克隆成功"
    return 0
  fi

  error "HTTPS 和 SSH 均无法访问 ${repo}，请检查网络或确认已配置 GitHub SSH Key"
}

echo ""
echo "============================================"
echo "  Hermes Skill Proxy 一键部署"
echo "============================================"
echo ""

# 检测是否以 root 运行
if [[ $EUID -eq 0 ]]; then
    error "请勿使用 root 运行此脚本。使用有 sudo 权限的普通用户账号。"
fi

# 检测基础命令
for cmd in python3 git curl sudo; do
  if ! command -v $cmd &>/dev/null; then
    error "未找到 ${cmd}，请先安装"
  fi
done

# 检测 systemd
if ! command -v systemctl &>/dev/null; then
    error "未找到 systemctl，本脚本需要在支持 systemd 的 Linux 上运行"
fi

# ---------- Step 1: 克隆 hermes-skill-proxy ----------
info "Step 1/6：克隆 hermes-skill-proxy..."
git_clone_or_fetch "${PROXY_REPO}" "${PROXY_DIR}"

# ---------- Step 2: 克隆 inexbot-knowledge-base ----------
info "Step 2/6：克隆 inexbot-knowledge-base skill..."
SKILL_PATH="${SKILLS_DIR}/${SKILL_NAME}"
git_clone_or_fetch "${SKILL_REPO}" "${SKILL_PATH}"

# ---------- Step 3: 安装 Python 依赖 ----------
info "Step 3/6：安装 Python 依赖（flask, requests）..."
pip3 install flask requests -q && info "依赖安装完成" || error "pip install 失败"

# ---------- Step 4: 安装 systemd 服务 ----------
info "Step 4/6：安装 hermes-skill-proxy systemd 服务..."
SERVICE_FILE="${PROXY_DIR}/hermes-skill-proxy.service"
SERVICE_DEST="/etc/systemd/system/hermes-skill-proxy.service"

if [[ ! -f "${SERVICE_FILE}" ]]; then
    error "服务文件不存在：${SERVICE_FILE}"
fi

# 替换服务文件中的路径为实际用户目录
sed "s|/home/inexbot|${HOME}|g" "${SERVICE_FILE}" > /tmp/hermes-skill-proxy.service
sudo -S -p '' cp /tmp/hermes-skill-proxy.service "${SERVICE_DEST}"
sudo -S -p '' systemctl daemon-reload
info "systemd 服务文件已安装"

# ---------- Step 5: 启动服务 ----------
info "Step 5/6：启动 hermes-skill-proxy 服务..."
sudo -S -p '' systemctl enable hermes-skill-proxy
sudo -S -p '' systemctl restart hermes-skill-proxy

sleep 2

if systemctl is-active --quiet hermes-skill-proxy; then
    info "服务启动成功"
else
    error "服务启动失败，请运行以下命令排查：
  sudo systemctl status hermes-skill-proxy
  sudo journalctl -u hermes-skill-proxy -n 30"
fi

# ---------- Step 6: 验证 ----------
info "Step 6/6：验证部署结果..."
HEALTH=$(curl -sf --max-time 10 http://localhost:8643/health 2>&1) || true
if [[ -z "${HEALTH}" ]]; then
    warn "health 检查未通过，请确认 Hermes Gateway 已在 localhost:8642 运行"
else
    echo "${HEALTH}" | python3 -c "
import sys, json
d = json.load(sys.stdin)
status = 'OK' if d.get('skill_loaded') else 'FAIL'
print(f\"  skill_loaded : {status} ({d.get('skill')})\")
print(f\"  hermes_url   : {d.get('hermes_url')}\")
print(f\"  status       : {d.get('status')}\")
"
fi

# ---------- 完成 ----------
echo ""
echo "============================================"
echo "  部署完成！"
echo "============================================"
echo ""
echo "  Proxy 地址 : http://localhost:8643"
echo "  Health     : http://localhost:8643/health"
echo ""
echo "  下一步："
echo "  1. 将客户端（官网后端等）指向 http://localhost:8643"
echo "     （原来是 http://localhost:8642）"
echo ""
echo "  2. 重启 Hermes Gateway 使新 skill 生效："
echo "     sudo systemctl restart hermes-gateway"
echo ""
echo "  常用命令："
echo "  sudo systemctl status hermes-skill-proxy   # 查看状态"
echo "  sudo journalctl -u hermes-skill-proxy -f  # 查看日志"
echo "  sudo systemctl restart hermes-skill-proxy # 重启"
echo ""
