#!/usr/bin/env bash
# Ubuntu 服务器一键部署 TG 统计机器人（Docker Compose）
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${APP_DIR}"

echo "==> 部署目录: ${APP_DIR}"

if [[ ! -f .env ]]; then
  if [[ -f .env.example ]]; then
    cp .env.example .env
    echo "==> 已生成 .env，请先编辑填写 TG_API_ID / TG_API_HASH / TG_BOT_TOKEN / TG_ADMIN_CHAT_ID"
    echo "    nano .env"
    exit 1
  else
    echo "错误: 缺少 .env 与 .env.example"
    exit 1
  fi
fi

# 安装 Docker（如未安装）
if ! command -v docker >/dev/null 2>&1; then
  echo "==> 安装 Docker..."
  sudo apt-get update
  sudo apt-get install -y ca-certificates curl gnupg
  sudo install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  sudo chmod a+r /etc/apt/keyrings/docker.gpg
  echo \
    "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
    $(. /etc/os-release && echo "${VERSION_CODENAME}") stable" | \
    sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
  sudo apt-get update
  sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
  sudo usermod -aG docker "${USER}" || true
  echo "==> Docker 已安装。若首次使用请重新登录 SSH 或执行: newgrp docker"
fi

mkdir -p sessions data

COMPOSE_CMD="docker compose"
if ! docker compose version >/dev/null 2>&1; then
  if command -v docker-compose >/dev/null 2>&1; then
    COMPOSE_CMD="docker-compose"
  else
    echo "错误: 未找到 docker compose，请安装 docker-compose-plugin"
    exit 1
  fi
fi

echo "==> 构建并启动服务..."
${COMPOSE_CMD} -f docker-compose.yml up -d --build

echo "==> 等待健康检查..."
sleep 5
${COMPOSE_CMD} -f docker-compose.yml ps

PUBLISH_PORT="$(grep -E '^WEB_PUBLISH_PORT=' .env 2>/dev/null | cut -d= -f2- || echo 8006)"
PUBLISH_PORT="${PUBLISH_PORT:-8006}"

echo ""
echo "=========================================="
echo " 部署完成"
echo " 访问: http://<服务器IP>:${PUBLISH_PORT}"
echo " 健康检查: curl http://127.0.0.1:${PUBLISH_PORT}/health"
echo " 查看日志: ${COMPOSE_CMD} -f docker-compose.yml logs -f"
echo " 停止服务: ${COMPOSE_CMD} -f docker-compose.yml down"
echo "=========================================="
