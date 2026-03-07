#!/bin/bash
# ============================================================
#  Argus 中文交互式部署脚本
#  适配第三方 OpenAI 兼容 API
# ============================================================

set -e

# ---------- 颜色 ----------
R='\033[0;31m'; G='\033[0;32m'; Y='\033[1;33m'; C='\033[0;36m'; B='\033[1m'; N='\033[0m'

info()  { echo -e "${G}[✓]${N} $1"; }
warn()  { echo -e "${Y}[!]${N} $1"; }
error() { echo -e "${R}[✗]${N} $1"; exit 1; }
ask()   { echo -e "${C}[?]${N} $1"; }

divider() { echo -e "${B}──────────────────────────────────────────${N}"; }

is_absolute_path() {
    case "$1" in
        /*) return 0 ;;
        *) return 1 ;;
    esac
}

detect_primary_ip() {
    local ip=""
    if command -v hostname &>/dev/null; then
        ip=$(hostname -I 2>/dev/null | awk '{print $1}')
    fi
    if [ -z "$ip" ] && command -v ip &>/dev/null; then
        ip=$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i = 1; i <= NF; i++) if ($i == "src") {print $(i + 1); exit}}')
    fi
    echo "${ip:-127.0.0.1}"
}

build_responses_upstream_url() {
    local base="$1"
    base="${base%/}"

    if [[ "$base" == */v1/responses ]]; then
        echo "$base"
    elif [[ "$base" == */v1 ]]; then
        echo "$base/responses"
    else
        echo "$base/v1/responses"
    fi
}

# ---------- 欢迎 ----------
clear
echo ""
echo -e "${B}    ╔══════════════════════════════════╗${N}"
echo -e "${B}    ║     Argus 交互式部署脚本 v1.0    ║${N}"
echo -e "${B}    ║   自托管 AI 助手网关 (Telegram)  ║${N}"
echo -e "${B}    ╚══════════════════════════════════╝${N}"
echo ""

# ---------- 1. 检查 & 安装 Docker ----------
divider
echo -e "${B}[1/6] 检查 Docker 环境${N}"
divider

if ! command -v docker &>/dev/null; then
    warn "Docker 未安装"
    ask "是否自动安装 Docker? (y/n)"
    read -r install_docker
    if [[ "$install_docker" =~ ^[Yy]$ ]]; then
        info "正在安装 Docker，请稍候..."
        curl -fsSL https://get.docker.com | sh
        systemctl enable --now docker
        info "Docker 安装完成"
    else
        error "Docker 是必须的，无法继续部署"
    fi
else
    info "Docker 已安装: $(docker --version | awk '{print $3}')"
fi

if ! docker compose version &>/dev/null; then
    error "Docker Compose 插件未找到，请升级 Docker 到 20.10+"
fi
info "Docker Compose 就绪: $(docker compose version --short)"
echo ""

# ---------- 2. 选择安装路径 ----------
divider
echo -e "${B}[2/6] 配置安装路径${N}"
divider

DEFAULT_DIR="${HOME}/argus"
ask "Argus 安装目录 [默认: ${DEFAULT_DIR}]:"
read -r DEPLOY_DIR
DEPLOY_DIR="${DEPLOY_DIR:-$DEFAULT_DIR}"

DEFAULT_DATA="${HOME}/.argus"
ask "数据持久化目录 [默认: ${DEFAULT_DATA}]:"
read -r DATA_DIR
DATA_DIR="${DATA_DIR:-$DEFAULT_DATA}"

if ! is_absolute_path "$DATA_DIR"; then
    error "数据持久化目录必须是绝对路径（例如 /srv/argus 或 ${HOME}/.argus）"
fi

info "安装目录: ${DEPLOY_DIR}"
info "数据目录: ${DATA_DIR}"
echo ""

# ---------- 3. 配置 API ----------
divider
echo -e "${B}[3/6] 配置 AI 模型 API（支持第三方兼容接口）${N}"
divider

echo ""
echo -e "  Argus 通过 ${B}ARGUS_OPENAI_RESPONSES_UPSTREAM_URL${N} 支持"
echo -e "  任何 OpenAI 兼容的第三方 API（中转站等）。"
echo ""

ask "API Base URL（第三方中转地址，末尾不要加 /）:"
echo -e "  ${Y}示例: https://api.openai.com  或  https://your-proxy.com${N}"
read -r API_BASE_URL
while [ -z "$API_BASE_URL" ]; do
    warn "API Base URL 不能为空"
    ask "API Base URL:"
    read -r API_BASE_URL
done
# 去掉末尾斜杠
API_BASE_URL="${API_BASE_URL%/}"
RESPONSES_UPSTREAM_URL=$(build_responses_upstream_url "$API_BASE_URL")

ask "API Key:"
read -r API_KEY
while [ -z "$API_KEY" ]; do
    warn "API Key 不能为空"
    ask "API Key:"
    read -r API_KEY
done

info "API 地址: ${API_BASE_URL}"
info "API Key:  ${API_KEY:0:10}***"
echo ""

# ---------- 4. 配置 Telegram ----------
divider
echo -e "${B}[4/6] 配置 Telegram Bot${N}"
divider

echo ""
echo -e "  如果还没有 Bot Token，请在 Telegram 中找 ${B}@BotFather${N}"
echo -e "  发送 /newbot 创建一个新 Bot，获取 Token。"
echo ""

ask "Telegram Bot Token [留空则跳过，稍后配置]:"
read -r TG_TOKEN

if [ -z "$TG_TOKEN" ]; then
    warn "Telegram Bot Token 未设置，部署后需手动配置"
    TG_TOKEN=""
    SKIP_TG=true
else
    info "Bot Token: ${TG_TOKEN:0:10}***"
    SKIP_TG=false
fi

ask "Telegram 消息流式输出模式 [默认: auto] (auto/true/false):"
read -r TG_STREAMING
TG_STREAMING="${TG_STREAMING:-auto}"

echo ""
ask "管理员 Chat ID（预留字段，当前版本暂未生效；多个用逗号隔开，可留空）:"
read -r TG_ADMIN_IDS
echo ""

# ---------- 5. 资源限制 ----------
divider
echo -e "${B}[5/6] 运行时资源限制${N}"
divider

echo ""
echo -e "  为每个 AI 会话容器设置资源限制，防止占满服务器。"
echo -e "  小内存服务器建议设置。"
echo ""

ask "CPU 限制 [默认: 0.8 核]:"
read -r RT_CPUS
RT_CPUS="${RT_CPUS:-0.8}"

ask "内存限制 [默认: 768m]:"
read -r RT_MEM
RT_MEM="${RT_MEM:-768m}"

ask "进程数限制 [默认: 512]:"
read -r RT_PIDS
RT_PIDS="${RT_PIDS:-512}"

info "CPU: ${RT_CPUS}  内存: ${RT_MEM}  进程: ${RT_PIDS}"
echo ""

# ---------- 确认 ----------
divider
echo -e "${B}配置确认${N}"
divider
echo ""
echo -e "  安装目录:      ${B}${DEPLOY_DIR}${N}"
echo -e "  数据目录:      ${B}${DATA_DIR}${N}"
echo -e "  API 地址:      ${B}${API_BASE_URL}${N}"
echo -e "  API Key:       ${B}${API_KEY:0:10}***${N}"
echo -e "  Telegram Bot:  ${B}$([ "$SKIP_TG" = true ] && echo '稍后配置' || echo "${TG_TOKEN:0:10}***")${N}"
echo -e "  流式输出:      ${B}${TG_STREAMING}${N}"
echo -e "  管理员 ID:     ${B}${TG_ADMIN_IDS:-未设置（当前版本暂未使用）}${N}"
echo -e "  资源限制:      ${B}${RT_CPUS} CPU / ${RT_MEM} 内存 / ${RT_PIDS} 进程${N}"
echo ""

ask "确认开始部署? (y/n)"
read -r confirm
if [[ ! "$confirm" =~ ^[Yy]$ ]]; then
    warn "已取消部署"
    exit 0
fi
echo ""

# ---------- 6. 开始部署 ----------
divider
echo -e "${B}[6/6] 正在部署...${N}"
divider

SERVER_IP=$(detect_primary_ip)

# 克隆 / 更新仓库
if [ -d "$DEPLOY_DIR/.git" ]; then
    info "更新已有仓库..."
    cd "$DEPLOY_DIR"
    git pull origin main
elif [ -d "$DEPLOY_DIR" ] && [ -n "$(ls -A "$DEPLOY_DIR" 2>/dev/null)" ]; then
    error "安装目录已存在且不是 Argus Git 仓库，请换一个目录或先清理该目录"
else
    info "克隆 Argus 仓库..."
    git clone https://github.com/yym68686/argus.git "$DEPLOY_DIR"
    cd "$DEPLOY_DIR"
fi

# 生成 Token
ARGUS_TOKEN=$(openssl rand -hex 16)
info "已生成 Gateway Token: ${ARGUS_TOKEN}"

# 创建数据目录
mkdir -p "${DATA_DIR}/gateway"
info "数据目录已就绪"

# 写入 .env
cat > "$DEPLOY_DIR/.env" << EOF
# ===== Argus 环境配置 (由部署脚本自动生成) =====
# 生成时间: $(date '+%Y-%m-%d %H:%M:%S')

# 供 Docker 内的 Telegram Bot 推导 gateway 地址使用。
HOST=gateway

# --- Gateway 认证 ---
ARGUS_TOKEN=${ARGUS_TOKEN}

# --- 数据路径 ---
ARGUS_HOME_HOST_PATH=${DATA_DIR}

# --- Runtime 配置 ---
ARGUS_RUNTIME_INSTALL_CMD="npm i -g @openai/codex"
ARGUS_RUNTIME_CMD="codex app-server"

# --- 资源限制 ---
ARGUS_RUNTIME_CPUS=${RT_CPUS}
ARGUS_RUNTIME_MEM_LIMIT=${RT_MEM}
ARGUS_RUNTIME_MEMSWAP_LIMIT=${RT_MEM}
ARGUS_RUNTIME_PIDS_LIMIT=${RT_PIDS}

# --- 第三方 API 配置 ---
OPENAI_API_KEY=${API_KEY}
ARGUS_OPENAI_RESPONSES_UPSTREAM_URL=${RESPONSES_UPSTREAM_URL}

# --- Telegram Bot ---
TELEGRAM_BOT_TOKEN=${TG_TOKEN}
TELEGRAM_DRAFT_STREAMING=${TG_STREAMING}
TELEGRAM_ADMIN_CHAT_IDS=${TG_ADMIN_IDS}

# --- Web UI (取消注释以启用) ---
# NEXT_PUBLIC_ARGUS_WS_URL="ws://${SERVER_IP}:8080/ws?token=${ARGUS_TOKEN}"
EOF

info ".env 配置文件已写入"

# 构建并启动
info "构建 Docker 镜像（首次需要几分钟）..."
COMPOSE_ARGS=()
if [ "$SKIP_TG" = false ]; then
    COMPOSE_ARGS+=(--profile tg)
else
    warn "未提供 Telegram Bot Token，本次仅启动 gateway；稍后可单独启用 tg profile"
fi

docker compose "${COMPOSE_ARGS[@]}" up --build -d

# 等待启动
info "等待服务启动..."
sleep 8

# 检查状态
echo ""
divider
echo -e "${B}服务运行状态${N}"
divider
docker compose "${COMPOSE_ARGS[@]}" ps
echo ""

# ---------- 完成 ----------
echo ""
echo -e "${B}╔══════════════════════════════════════════╗${N}"
echo -e "${B}║          ${G}部署成功!${N}${B}                       ║${N}"
echo -e "${B}╚══════════════════════════════════════════╝${N}"
echo ""
echo -e "  ${B}服务器 IP:${N}     ${SERVER_IP}"
echo -e "  ${B}Gateway:${N}       http://${SERVER_IP}:8080"
echo -e "  ${B}ARGUS_TOKEN:${N}   ${ARGUS_TOKEN}"
echo -e "  ${B}WebSocket:${N}     ws://${SERVER_IP}:8080/ws?token=${ARGUS_TOKEN}"
echo ""

if [ "$SKIP_TG" = true ]; then
    echo -e "  ${Y}>>> 你还需要配置 Telegram Bot Token:${N}"
    echo ""
    echo -e "      1. 编辑配置:  ${B}nano ${DEPLOY_DIR}/.env${N}"
    echo -e "         找到 TELEGRAM_BOT_TOKEN 行，替换为真实 Token"
    echo ""
    echo -e "      2. 重启服务:  ${B}cd ${DEPLOY_DIR} && docker compose --profile tg up -d${N}"
    echo ""
    echo -e "      3. 在 Telegram 给你的 Bot 发 ${B}/start${N}"
    echo ""
else
    echo -e "  ${G}>>> 现在就可以在 Telegram 给你的 Bot 发 /start 开始使用了${N}"
    echo ""
fi

COMPOSE_CMD="docker compose"
WEB_COMPOSE_CMD="docker compose --profile web"
if [ "$SKIP_TG" = false ]; then
    COMPOSE_CMD="docker compose --profile tg"
    WEB_COMPOSE_CMD="docker compose --profile tg --profile web"
fi

echo -e "  ${B}常用命令:${N}"
echo -e "    查看日志:  cd ${DEPLOY_DIR} && ${COMPOSE_CMD} logs -f"
echo -e "    重启服务:  cd ${DEPLOY_DIR} && ${COMPOSE_CMD} restart"
echo -e "    停止服务:  cd ${DEPLOY_DIR} && ${COMPOSE_CMD} down"
echo -e "    更新升级:  cd ${DEPLOY_DIR} && git pull && ${COMPOSE_CMD} up --build -d"
echo ""
echo -e "    启用 Web UI:  cd ${DEPLOY_DIR} && ${WEB_COMPOSE_CMD} up --build -d"
echo -e "    Web 地址:     http://${SERVER_IP}:3000"
echo ""
divider
