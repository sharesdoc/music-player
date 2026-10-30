#!/bin/bash
# ═══════════════════════════════════════════════════════════════
# NEUROMIX // GRID - 发布打包脚本
# 生成可直接部署的发布包 (.tar.gz)
#
# 用法:
#   bash scripts/release.sh [version]       # 生成 neuromix-<version>.tar.gz
#   bash scripts/release.sh 1.2.0           # 指定版本号
#   bash scripts/release.sh                 # 从 git tag 或 main.py 自动取版本
#
# 输出: dist/neuromix-<version>.tar.gz
# 部署: 解压后执行 sudo bash deploy/ubuntu.sh
# ═══════════════════════════════════════════════════════════════
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

# 颜色
RED=$(printf '\033[0;31m')
GREEN=$(printf '\033[0;32m')
YELLOW=$(printf '\033[1;33m')
CYAN=$(printf '\033[0;36m')
BOLD=$(printf '\033[1m')
NC=$(printf '\033[0m')

log_info()  { printf "%s[INFO]%s  %s\n" "$GREEN"  "$NC" "$1"; }
log_warn()  { printf "%s[WARN]%s  %s\n" "$YELLOW" "$NC" "$1"; }
log_error() { printf "%s[ERROR]%s %s\n" "$RED"    "$NC" "$1" >&2; }
log_title() { printf "\n%s%s=== %s ===%s\n\n" "$CYAN" "$BOLD" "$1" "$NC"; }

# ── 版本号 ──────────────────────────────────────────────────────
detect_version() {
    # 优先用参数
    if [ -n "${1:-}" ]; then
        echo "$1"; return
    fi
    # 其次 git tag
    local tag
    tag=$(git describe --tags --abbrev=0 2>/dev/null || true)
    if [ -n "$tag" ]; then
        echo "${tag#v}"; return
    fi
    # 最后从 main.py 的 version 字段
    local ver
    ver=$(grep -oP 'version="([^"]+)"' app/main.py 2>/dev/null | head -1 | cut -d'"' -f2)
    if [ -n "$ver" ]; then
        echo "$ver"; return
    fi
    echo "0.0.0-dev"
}

VERSION=$(detect_version "${1:-}")
DIST_DIR="$PROJECT_DIR/dist"
RELEASE_NAME="neuromix-${VERSION}"
ARCHIVE="${RELEASE_NAME}.tar.gz"
STAGING="$DIST_DIR/.staging/$RELEASE_NAME"

log_title "NEUROMIX // GRID 发布打包"
printf "  版本: %s%s%s\n" "$YELLOW" "$VERSION" "$NC"
printf "  输出: %s%s%s\n\n" "$YELLOW" "dist/$ARCHIVE" "$NC"

# ── 1. 检查环境 ─────────────────────────────────────────────────
log_info "检查工作目录..."
if [ ! -f "app/main.py" ]; then
    log_error "请在项目根目录运行此脚本"
    exit 1
fi

# 检查 git 状态（仅警告，不阻断）
if git status --porcelain 2>/dev/null | head -1 | grep -q .; then
    log_warn "工作区有未提交的修改"
fi

# ── 2. 运行测试（如果服务在跑） ─────────────────────────────────
PORT=$(grep -oP 'APP_PORT=\K\d+' env.loc 2>/dev/null || echo "2510")
if curl -sf "http://localhost:$PORT/health" &>/dev/null; then
    log_info "检测到运行中的服务，执行测试..."
    if bash scripts/test.sh "http://localhost:$PORT"; then
        log_info "✓ 所有测试通过"
    else
        log_error "测试失败，终止打包"
        exit 1
    fi
else
    log_warn "服务未运行，跳过测试（建议先 run start 再打包）"
fi

# ── 3. 清理 & 准备暂存区 ────────────────────────────────────────
log_info "准备打包暂存区..."
rm -rf "$STAGING"
mkdir -p "$STAGING"

# ── 4. 复制文件 ──────────────────────────────────────────────────
log_info "复制应用文件..."

# 核心应用
cp -r app "$STAGING/app"
# 排除 __pycache__
find "$STAGING/app" -type d -name '__pycache__' -exec rm -rf {} + 2>/dev/null || true

# 配置 & 脚本
cp requirements.txt "$STAGING/"
cp config.yml       "$STAGING/"
cp env.pro          "$STAGING/"
cp run              "$STAGING/"
cp install.sh       "$STAGING/"
chmod +x "$STAGING/run" "$STAGING/install.sh"

cp -r scripts       "$STAGING/scripts"
cp -r deploy        "$STAGING/deploy"
chmod +x "$STAGING/deploy/"*.sh 2>/dev/null || true
chmod +x "$STAGING/scripts/"*.sh 2>/dev/null || true

# 空目录占位（确保部署时目录结构正确）
mkdir -p "$STAGING/data"
mkdir -p "$STAGING/uploads"
mkdir -p "$STAGING/log"
touch "$STAGING/data/.gitkeep"
touch "$STAGING/uploads/.gitkeep"
touch "$STAGING/log/.gitkeep"

# 版本文件
printf '%s\n' "$VERSION" > "$STAGING/VERSION"

# ── 5. 安全检查：不打包敏感文件 ──────────────────────────────────
log_info "安全检查..."
# 明确排除开发环境配置和数据库
rm -f "$STAGING/env.loc" 2>/dev/null
rm -f "$STAGING/data/music.db" 2>/dev/null
# 排除 .git
rm -rf "$STAGING/.git" 2>/dev/null

# 检查有没有意外带入敏感内容（排除本脚本自身的 grep 模式）
_leak_pattern='local-dev-secret'
if grep -rq --include='*.py' --include='*.yml' --include='*.env' --include='env.*' "$_leak_pattern" "$STAGING/" 2>/dev/null; then
    log_error "发现开发环境密钥泄露到发布包，终止"
    exit 1
fi
# 确认 env.loc 不在包内
if [ -f "$STAGING/env.loc" ]; then
    log_error "env.loc 不应出现在发布包中"
    exit 1
fi
log_info "✓ 安全检查通过"

# ── 6. 打包 ──────────────────────────────────────────────────────
log_info "生成 $ARCHIVE ..."
mkdir -p "$DIST_DIR"
tar -czf "$DIST_DIR/$ARCHIVE" -C "$DIST_DIR/.staging" "$RELEASE_NAME"

# 清理暂存区
rm -rf "$DIST_DIR/.staging"

# 包大小
SIZE=$(du -sh "$DIST_DIR/$ARCHIVE" | awk '{print $1}')

# ── 7. 生成校验和 ─────────────────────────────────────────────────
cd "$DIST_DIR"
shasum -a 256 "$ARCHIVE" > "${ARCHIVE}.sha256"
cd "$PROJECT_DIR"

# ── 完成 ─────────────────────────────────────────────────────────
printf "\n${GREEN}${BOLD}╔══════════════════════════════════════════════╗${NC}\n"
printf "${GREEN}${BOLD}║  打包成功                                    ║${NC}\n"
printf "${GREEN}${BOLD}╚══════════════════════════════════════════════╝${NC}\n\n"
printf "  文件: ${CYAN}dist/%s${NC}\n" "$ARCHIVE"
printf "  大小: ${YELLOW}%s${NC}\n" "$SIZE"
printf "  校验: ${CYAN}dist/%s${NC}\n\n" "${ARCHIVE}.sha256"
printf "  ${BOLD}部署步骤:${NC}\n"
printf "    1. ${CYAN}scp dist/%s user@server:/tmp/${NC}\n" "$ARCHIVE"
printf "    2. ${CYAN}ssh user@server${NC}\n"
printf "    3. ${CYAN}cd /tmp && tar xzf %s${NC}\n" "$ARCHIVE"
printf "    4. ${CYAN}cd %s${NC}\n" "$RELEASE_NAME"
printf "    5. ${CYAN}cp env.pro .env && vi .env${NC}  # 填写 JWT_SECRET 等\n"
printf "    6. ${CYAN}sudo bash deploy/ubuntu.sh${NC}\n\n"
