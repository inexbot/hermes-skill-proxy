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

# ---------- Step 1: 检查并配置 npm 和 pip ----------
info "Step 1/8：检查 npm 和 pip..."

# 检测 pip
if ! command -v pip3 &>/dev/null && ! command -v pip &>/dev/null; then
    warn "pip 未安装，尝试安装..."
    if command -v apt-get &>/dev/null; then
        sudo apt-get install -y python3-pip >/dev/null 2>&1 && info "pip 安装完成" || warn "pip 安装失败"
    else
        warn "无法自动安装 pip，请手动安装"
    fi
fi

# 检测 npm
if ! command -v npm &>/dev/null; then
    warn "npm 未安装，尝试安装..."
    if command -v apt-get &>/dev/null; then
        sudo apt-get install -y npm >/dev/null 2>&1 && info "npm 安装完成" || warn "npm 安装失败"
    elif command -v brew &>/dev/null; then
        brew install node >/dev/null 2>&1 && info "npm 安装完成" || warn "npm 安装失败"
    else
        warn "无法自动安装 npm，请手动安装"
    fi
fi

# 配置 npm 镜像
if command -v npm &>/dev/null; then
    npm config set registry https://registry.npmmirror.com/ 2>/dev/null && info "npm 镜像配置完成" || warn "npm 镜像配置失败"
fi

# 配置 pip 镜像
if command -v pip3 &>/dev/null; then
# ---------- Step 2: 克隆 hermes-skill-proxy ----------
info "Step 2/8：克隆 hermes-skill-proxy..."
git_clone_or_fetch "${PROXY_REPO}" "${PROXY_DIR}"

# ---------- Step 3: 克隆 inexbot-knowledge-base ----------
info "Step 3/8：克隆 inexbot-knowledge-base skill..."
SKILL_PATH="${SKILLS_DIR}/${SKILL_NAME}"
git_clone_or_fetch "${SKILL_REPO}" "${SKILL_PATH}"

# 验证 skill 目录存在
if [[ ! -f "${SKILL_PATH}/SKILL.md" ]]; then
    warn "Skill 目录不存在或 SKILL.md 缺失：${SKILL_PATH}"
fi

# ---------- Step 4: 爬取一次知识库 ----------
info "Step 4/8：爬取知识库（首次部署）..."
if [[ -f "${SKILL_PATH}/scripts/crawler.py" ]]; then
    python3 "${SKILL_PATH}/scripts/crawler.py" && info "知识库爬取完成" || warn "知识库爬取失败，继续部署..."
else
    warn "爬虫脚本不存在，跳过：${SKILL_PATH}/scripts/crawler.py"
fi

# ---------- Step 5: 安装 Python 依赖 ----------
info "Step 5/8：安装 Python 依赖（flask, requests）..."
pip3 install flask requests -q && info "依赖安装完成" || error "pip install 失败"

# ---------- Step 6: 安装 systemd 服务 ----------
info "Step 6/8：安装 hermes-skill-proxy systemd 服务..."
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

# ---------- Step 7: 启动服务 ----------
info "Step 7/8：启动 hermes-skill-proxy 服务..."
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

# ---------- Step 8: 配置每日定时爬取 ----------
info "Step 8/8：配置每日 11:00 定时爬取任务..."
CRON_PROMPT="爬取纳博特科技知识库 https://doc.inexbot.com

步骤：
1. 运行爬虫脚本：
   python3 ~/.hermes/skills/productivity/inexbot-knowledge-base/scripts/crawler.py
2. 检查输出 ~/.hermes/kb/inexbot/ 是否包含 index.json 和 md/ 目录
3. 记录爬取结果（成功/失败/页数）"

# 检查是否已存在同名定时任务
EXISTING_JOB=$(cronjob action=list 2>/dev/null | grep -c "inexbot-knowledge-base daily crawl" || true)
if [[ "${EXISTING_JOB}" -eq 0 ]]; then
    # 创建定时任务（后台运行，不阻塞部署）
    cronjob action=create \
      name="inexbot-knowledge-base daily crawl" \
      prompt="${CRON_PROMPT}" \
      schedule="0 11 * * *" \
      skills='["productivity/inexbot-knowledge-base"]' \
      deliver=local >/dev/null 2>&1 && info "定时任务创建成功（每天 11:00）" || warn "定时任务创建失败"
else
    info "定时任务已存在，跳过"
fi
HEALTH=$(curl -sf --max-time 10 http://localhost:8643/health 2>&1) || true
if [[ -z "${HEALTH}" ]]; then
    warn "health 检查未通过，请确认 Hermes Gateway 已在 localhost:8642 运行"
else
    echo "${HEALTH}" | python3 -c "
import sys, json
d = json.load(sys.stdin)
skill_ok = d.get('skill_loaded') == True
status = 'OK' if skill_ok else 'FAIL'
skill_name = d.get('skill', '')
print(f\"  skill_loaded : {status} ({skill_name})\")
if not skill_ok:
    print(f"  WARNING: skill 未加载，请确认已执行：")
    print(f"    git clone https://github.com/inexbot/inexbot-knowledge-base.git \${HOME}/.hermes/skills/productivity/inexbot-knowledge-base")
print(f"  hermes_url   : {d.get('hermes_url')}")
print(f"  status       : {d.get('status')}")
"
fi

# 验证定时任务
CRON_JOB=$(cronjob action=list 2>/dev/null | grep "inexbot-knowledge-base daily crawl" || true)
if [[ -n "${CRON_JOB}" ]]; then
    echo "  cron job     : OK (inexbot-knowledge-base daily crawl)"
else
    echo "  cron job     : WARN (未找到定时任务，请手动检查)"
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
