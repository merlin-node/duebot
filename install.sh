#!/usr/bin/env bash
# DueBot 一键安装：只需粘贴 Bot Token 和 User ID
set -e
cd "$(dirname "$0")"

G='\033[32m'; Y='\033[33m'; R='\033[31m'; N='\033[0m'
ok()   { echo -e "${G}✔ $*${N}"; }
warn() { echo -e "${Y}! $*${N}"; }
die()  { echo -e "${R}✘ $*${N}"; exit 1; }
ask()  { read -rp "$1" "$2" </dev/tty; }

echo -e "\n${G}━━━━━━━━  DueBot 安装  ━━━━━━━━${N}\n"

[ "$(id -u)" -eq 0 ] || die "请用 root 运行：sudo bash install.sh"

# 1. Docker
if ! command -v docker >/dev/null 2>&1; then
  warn "未检测到 Docker，开始自动安装…"
  curl -fsSL https://get.docker.com | sh || die "Docker 安装失败，国内机器请看 README 里的镜像安装方法"
fi
if docker compose version >/dev/null 2>&1; then DC="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then DC="docker-compose"
else die "缺少 docker compose 插件"; fi
ok "Docker 就绪"

# 2. 配置
if [ -f .env ]; then
  ask "检测到已有配置，直接沿用？[Y/n] " keep
  [[ "$keep" =~ ^[Nn] ]] && rm -f .env
fi

if [ ! -f .env ]; then
  while true; do
    ask "粘贴 Bot Token：" TOKEN
    TOKEN="$(echo "$TOKEN" | tr -d '[:space:]')"
    [[ "$TOKEN" =~ ^[0-9]+:[A-Za-z0-9_-]{30,}$ ]] && break
    warn "格式不对，应该像 123456789:AAxxxx…"
  done
  while true; do
    ask "粘贴你的 User ID：" UIDS
    UIDS="$(echo "$UIDS" | tr -d '[:space:]')"
    [[ "$UIDS" =~ ^[0-9]+$ ]] && break
    warn "只能是数字，例如 123456789"
  done
  ask "代理地址（海外机器直接回车跳过）：" PROXY
  PROXY="$(echo "$PROXY" | tr -d '[:space:]')"

  {
    echo "BOT_TOKEN=$TOKEN"
    echo "ADMIN_ID=$UIDS"
    echo "TZ=Asia/Shanghai"
    [ -n "$PROXY" ] && echo "PROXY=$PROXY"
  } > .env
  chmod 600 .env
  ok "配置已保存"
fi

# 3. 启动
mkdir -p data
echo "拉取镜像并启动…"
$DC pull || die "镜像拉取失败：请确认 GitHub 上的 duebot 镜像已设为 Public（见 README）"
$DC up -d
sleep 5

if [ "$(docker inspect -f '{{.State.Running}}' duebot 2>/dev/null)" = "true" ]; then
  echo
  ok "DueBot 已启动！去 Telegram 给你的 bot 发送 /menu"
  echo -e "   查看日志：${Y}docker logs -f duebot${N}\n"
else
  docker logs --tail 30 duebot 2>&1 || true
  die "启动失败，上面是日志"
fi
