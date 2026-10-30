#!/bin/bash
# ═══════════════════════════════════════════════════════════════
# NEUROMIX // GRID — Ubuntu 生产部署脚本
# 适用系统: Ubuntu 20.04 / 22.04 / 24.04
# 用法:
#   sudo bash deploy/ubuntu.sh [--domain your.domain.com] [--port 2510]
#
# 此脚本完成:
#   1. 安装 Python3, pip, nginx, git
#   2. 创建专用系统用户 neuromix
#   3. 安装应用到 /opt/neuromix
#   4. 创建 systemd 服务 (neuromix.service)
#   5. 配置 Nginx 反向代理（可选 HTTPS）
# ═══════════════════════════════════════════════════════════════
set -euo pipefail

# ── 参数解析 ──────────────────────────────────────────────────
DOMAIN=""
APP_PORT="2510"
INSTALL_DIR="/opt/neuromix"
APP_USER="neuromix"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

while [[ $# -gt 0 ]]; do
  case $1 in
    --domain) DOMAIN="$2"; shift 2;;
    --port)   APP_PORT="$2"; shift 2;;
    *)        shift;;
  esac
done

# ── 颜色 ──────────────────────────────────────────────────────
RED=$(printf '\033[0;31m'); GREEN=$(printf '\033[0;32m')
YELLOW=$(printf '\033[1;33m'); CYAN=$(printf '\033[0;36m')
NC=$(printf '\033[0m'); BOLD=$(printf '\033[1m')
log_info()  { printf "%s[INFO]%s  %s\n" "$GREEN"  "$NC" "$1"; }
log_warn()  { printf "%s[WARN]%s  %s\n" "$YELLOW" "$NC" "$1"; }
log_title() { printf "\n%s%s=== %s ===%s\n\n" "$CYAN" "$BOLD" "$1" "$NC"; }

# ── 权限检查 ──────────────────────────────────────────────────
if [ "$(id -u)" != "0" ]; then
  printf "%s[ERROR]%s 请使用 sudo 运行此脚本\n" "$RED" "$NC"
  exit 1
fi

log_title "NEUROMIX // GRID 生产部署"
log_info "安装目录: $INSTALL_DIR"
log_info "监听端口: $APP_PORT"
[ -n "$DOMAIN" ] && log_info "域名: $DOMAIN"

# ── 1. 安装系统依赖 ────────────────────────────────────────────
log_title "1/5 安装系统依赖"
apt-get update -qq
apt-get install -y -qq python3 python3-pip python3-venv nginx git curl
log_info "✓ 系统依赖已安装"

# ── 2. 创建专用用户 ────────────────────────────────────────────
log_title "2/5 创建应用用户"
if ! id "$APP_USER" &>/dev/null; then
  useradd --system --shell /bin/bash --create-home --home-dir "/home/$APP_USER" "$APP_USER"
  log_info "✓ 用户 $APP_USER 已创建"
else
  log_warn "用户 $APP_USER 已存在，跳过创建"
fi

# ── 3. 安装应用文件 ────────────────────────────────────────────
log_title "3/5 部署应用文件"
mkdir -p "$INSTALL_DIR"

# 同步应用文件（保留 data/ uploads/ log/ 目录）
rsync -a --exclude='data/' --exclude='uploads/' --exclude='log/' \
  --exclude='__pycache__/' --exclude='*.pyc' --exclude='.git/' \
  "$PROJECT_DIR/" "$INSTALL_DIR/"

# 创建必要目录
mkdir -p "$INSTALL_DIR/data" "$INSTALL_DIR/uploads" "$INSTALL_DIR/log"

# 创建 Python 虚拟环境
if [ ! -d "$INSTALL_DIR/venv" ]; then
  python3 -m venv "$INSTALL_DIR/venv"
  log_info "✓ Python 虚拟环境已创建"
fi

# 安装 Python 依赖
"$INSTALL_DIR/venv/bin/pip" install --quiet --upgrade pip
"$INSTALL_DIR/venv/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt"
log_info "✓ Python 依赖已安装"

# 设置权限
chown -R "$APP_USER:$APP_USER" "$INSTALL_DIR"
chmod 750 "$INSTALL_DIR"
chmod 640 "$INSTALL_DIR/env.pro" 2>/dev/null || true
log_info "✓ 文件权限已设置"

# ── 4. 配置 systemd 服务 ──────────────────────────────────────
log_title "4/5 配置 systemd 服务"
cat > /etc/systemd/system/neuromix.service << EOF
[Unit]
Description=NEUROMIX // GRID - Music Sharing Platform
After=network.target
Documentation=https://github.com/neuromix/grid

[Service]
Type=exec
User=${APP_USER}
Group=${APP_USER}
WorkingDirectory=${INSTALL_DIR}
EnvironmentFile=${INSTALL_DIR}/env.pro
ExecStart=${INSTALL_DIR}/venv/bin/gunicorn app.main:app \\
    --worker-class uvicorn.workers.UvicornWorker \\
    --workers \${GUNICORN_WORKERS:-4} \\
    --bind 127.0.0.1:${APP_PORT} \\
    --timeout 120 \\
    --keepalive 5 \\
    --access-logfile ${INSTALL_DIR}/log/access.log \\
    --error-logfile ${INSTALL_DIR}/log/error.log \\
    --log-level info
ExecReload=/bin/kill -s HUP \$MAINPID
Restart=on-failure
RestartSec=5
StandardOutput=append:${INSTALL_DIR}/log/stdout.log
StandardError=append:${INSTALL_DIR}/log/stderr.log

# 安全加固
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=${INSTALL_DIR}/data ${INSTALL_DIR}/uploads ${INSTALL_DIR}/log

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable neuromix.service
log_info "✓ systemd 服务已配置"

# ── 初始化数据库 ──────────────────────────────────────────────
log_info "初始化数据库..."
cd "$INSTALL_DIR"
if [ -f env.pro ]; then
  set -a; source env.pro; set +a
fi
APP_ENV=production "$INSTALL_DIR/venv/bin/python" -c \
  "from app.database import init_db; init_db()" && log_info "✓ 数据库已初始化"

# ── 5. 配置 Nginx ─────────────────────────────────────────────
log_title "5/5 配置 Nginx 反向代理"

NGINX_CONF="/etc/nginx/sites-available/neuromix"
NGINX_ENABLED="/etc/nginx/sites-enabled/neuromix"
NGINX_SERVER_NAME="${DOMAIN:-_}"

cat > "$NGINX_CONF" << EOF
# NEUROMIX // GRID — Nginx 配置
# 生成时间: $(date)

upstream neuromix_backend {
    server 127.0.0.1:${APP_PORT};
    keepalive 32;
}

server {
    listen 80;
    server_name ${NGINX_SERVER_NAME};

    # 安全响应头
    add_header X-Content-Type-Options "nosniff" always;
    add_header X-Frame-Options "SAMEORIGIN" always;
    add_header X-XSS-Protection "1; mode=block" always;
    add_header Referrer-Policy "strict-origin-when-cross-origin" always;

    # 访问日志
    access_log /var/log/nginx/neuromix_access.log;
    error_log  /var/log/nginx/neuromix_error.log;

    # 客户端上传大小限制（对应 max_file_size_mb=50）
    client_max_body_size 55M;

    # API 代理
    location /api/ {
        proxy_pass http://neuromix_backend;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 300;
        proxy_connect_timeout 30;
        proxy_send_timeout 300;
        # 音频流传输：禁用缓冲以支持 Range
        proxy_buffering off;
    }

    # 健康检查
    location /health {
        proxy_pass http://neuromix_backend;
        proxy_set_header Host \$host;
        proxy_read_timeout 10;
        access_log off;
    }

    # 静态文件（直接由 Nginx 服务，更高效）
    location /static/ {
        alias ${INSTALL_DIR}/app/static/;
        expires 7d;
        add_header Cache-Control "public, immutable";
    }

    # SPA 路由：所有非 API 请求转发到后端
    location / {
        proxy_pass http://neuromix_backend;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}
EOF

# 启用站点
ln -sf "$NGINX_CONF" "$NGINX_ENABLED"
# 删除默认站点（如果存在）
rm -f /etc/nginx/sites-enabled/default
# 测试配置
nginx -t && log_info "✓ Nginx 配置通过语法检查"

# ── 启动服务 ──────────────────────────────────────────────────
log_title "启动服务"
systemctl start neuromix.service
systemctl reload nginx
sleep 2

# 健康检查
if curl -sf "http://localhost:${APP_PORT}/health" &>/dev/null; then
  log_info "✓ 应用服务正常运行 (port ${APP_PORT})"
else
  log_warn "应用可能未就绪，请检查日志:"
  log_warn "  journalctl -u neuromix -n 30"
fi

# ── 可选：HTTPS (Certbot) ──────────────────────────────────────
if [ -n "$DOMAIN" ]; then
  printf "\n${YELLOW}提示: 如需 HTTPS，请运行:${NC}\n"
  printf "  apt-get install -y certbot python3-certbot-nginx\n"
  printf "  certbot --nginx -d %s\n\n" "$DOMAIN"
fi

# ── 部署完成 ──────────────────────────────────────────────────
printf "\n${GREEN}${BOLD}╔══════════════════════════════════════════════╗${NC}\n"
printf "${GREEN}${BOLD}║  NEUROMIX // GRID  部署完成                 ║${NC}\n"
printf "${GREEN}${BOLD}╚══════════════════════════════════════════════╝${NC}\n\n"
printf "  服务状态: ${CYAN}systemctl status neuromix${NC}\n"
printf "  查看日志: ${CYAN}journalctl -u neuromix -f${NC}\n"
printf "  Nginx 日志: ${CYAN}/var/log/nginx/neuromix_*.log${NC}\n"
printf "  应用日志: ${CYAN}${INSTALL_DIR}/log/${NC}\n"
[ -z "$DOMAIN" ] && printf "  访问地址: ${CYAN}http://$(hostname -I | awk '{print $1}')${NC}\n" \
                 || printf "  访问地址: ${CYAN}http://${DOMAIN}${NC}\n"
printf "\n"
printf "  ${YELLOW}重要: 请编辑 ${INSTALL_DIR}/env.pro 配置以下必填项:${NC}\n"
printf "    JWT_SECRET=<随机长字符串>\n"
printf "    ANTHROPIC_API_KEY=<你的API密钥>\n"
printf "  修改后重启: ${CYAN}systemctl restart neuromix${NC}\n\n"
