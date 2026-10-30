#!/bin/bash
# ═══════════════════════════════════════════════════════════════
# NEUROMIX // GRID - 自动化测试脚本
# 测试所有核心 API 端点和业务逻辑
# 用法: bash scripts/test.sh [base_url]
# ═══════════════════════════════════════════════════════════════
set -e

BASE="${1:-http://localhost:2510}"
PYTHON="${HOME}/ins/miniconda/bin/python"
[ ! -f "$PYTHON" ] && PYTHON="python3"

# 颜色
GREEN=$(printf '\033[0;32m')
RED=$(printf '\033[0;31m')
CYAN=$(printf '\033[0;36m')
NC=$(printf '\033[0m')
BOLD=$(printf '\033[1m')

PASS=0; FAIL=0
TEST_EMAIL="testrunner_$(date +%s)@grid.net"
TEST_USER="testrunner_$(date +%s)"
TOKEN=""

assert() {
    local name="$1" expected="$2" actual="$3"
    if [ "$actual" = "$expected" ]; then
        printf "  ${GREEN}✓${NC} %s\n" "$name"
        PASS=$((PASS+1))
    else
        printf "  ${RED}✗${NC} %s (期望: %s, 实际: %s)\n" "$name" "$expected" "$actual"
        FAIL=$((FAIL+1))
    fi
}

http_code() {
    curl -sw "%{http_code}" -o /dev/null "$@"
}

json_get() {
    # json_get <url> <key> [token]
    local url="$1" key="$2" auth="${3:-}"
    local resp
    if [ -n "$auth" ]; then
        resp=$(curl -sf "$url" -H "Authorization: Bearer $auth" 2>/dev/null || echo '{}')
    else
        resp=$(curl -sf "$url" 2>/dev/null || echo '{}')
    fi
    echo "$resp" | $PYTHON -c "import json,sys; d=json.load(sys.stdin); print(d.get('$key',''))" 2>/dev/null || echo ""
}

printf "\n${BOLD}${CYAN}═══ NEUROMIX // GRID 自动化测试 ═══${NC}\n\n"
printf "目标: %s\n\n" "$BASE"

# ── 1. 健康检查 ──────────────────────────────────────────────────
printf "${BOLD}[1] 基础健康检查${NC}\n"
code=$(http_code "$BASE/health")
assert "GET /health 返回200" "200" "$code"

status=$(json_get "$BASE/health" "status")
assert "健康状态为 ok" "ok" "$status"

# ── 2. 数据 API ──────────────────────────────────────────────────
printf "\n${BOLD}[2] 数据读取 API${NC}\n"
code=$(http_code "$BASE/api/songs?limit=1")
assert "GET /api/songs 返回200" "200" "$code"

code=$(http_code "$BASE/api/categories")
assert "GET /api/categories 返回200" "200" "$code"

code=$(http_code "$BASE/api/artists")
assert "GET /api/artists 返回200" "200" "$code"

code=$(http_code "$BASE/api/charts/hot-songs")
assert "GET /api/charts/hot-songs 返回200" "200" "$code"

# 搜索
code=$(http_code "$BASE/api/songs?q=adele")
assert "歌曲搜索返回200" "200" "$code"

# ── 3. 认证 API ──────────────────────────────────────────────────
printf "\n${BOLD}[3] 用户认证${NC}\n"

# 注册
REG=$(curl -sf -X POST "$BASE/api/auth/register" \
    -H "Content-Type: application/json" \
    -d "{\"email\":\"$TEST_EMAIL\",\"username\":\"$TEST_USER\",\"password\":\"test1234\"}" 2>/dev/null)
reg_ok=$(echo "$REG" | $PYTHON -c "import json,sys; d=json.load(sys.stdin); print('ok' if d.get('access_token') else 'fail')" 2>/dev/null)
assert "注册新用户" "ok" "$reg_ok"
TOKEN=$(echo "$REG" | $PYTHON -c "import json,sys; print(json.load(sys.stdin).get('access_token',''))" 2>/dev/null)

# 重复注册应该失败
code=$(curl -sw "%{http_code}" -o /dev/null -X POST "$BASE/api/auth/register" \
    -H "Content-Type: application/json" \
    -d "{\"email\":\"$TEST_EMAIL\",\"username\":\"${TEST_USER}2\",\"password\":\"test\"}" 2>/dev/null)
assert "重复邮箱注册返回4xx" "1" "$([ "$code" = '400' ] || [ "$code" = '422' ] && echo 1 || echo 0)"

# 登录
LOGIN=$(curl -sf -X POST "$BASE/api/auth/login" \
    -H "Content-Type: application/json" \
    -d "{\"email_or_username\":\"$TEST_EMAIL\",\"password\":\"test1234\"}" 2>/dev/null)
login_ok=$(echo "$LOGIN" | $PYTHON -c "import json,sys; d=json.load(sys.stdin); print('ok' if d.get('access_token') else 'fail')" 2>/dev/null)
assert "登录成功" "ok" "$login_ok"

# 错误密码
code=$(curl -sw "%{http_code}" -o /dev/null -X POST "$BASE/api/auth/login" \
    -H "Content-Type: application/json" \
    -d "{\"email_or_username\":\"$TEST_EMAIL\",\"password\":\"wrongpass\"}" 2>/dev/null)
assert "错误密码返回401" "401" "$code"

# ── 4. 需要认证的 API ────────────────────────────────────────────
printf "\n${BOLD}[4] 认证保护 API${NC}\n"

# 未认证访问
code=$(http_code "$BASE/api/user/credits")
assert "未认证访问配额返回401" "401" "$code"

# 已认证访问
code=$(http_code "$BASE/api/user/credits" -H "Authorization: Bearer $TOKEN")
assert "已认证访问配额返回200" "200" "$code"

code=$(http_code "$BASE/api/user/history" -H "Authorization: Bearer $TOKEN")
assert "已认证访问历史返回200" "200" "$code"

# ── 5. 流式播放 ──────────────────────────────────────────────────
printf "\n${BOLD}[5] 音频流播放${NC}\n"
code=$(http_code "$BASE/api/songs/1/stream")
assert "GET /api/songs/1/stream 返回200" "200" "$code"

# Range 请求
code=$(curl -sw "%{http_code}" -o /dev/null -H "Range: bytes=0-1023" "$BASE/api/songs/1/stream" 2>/dev/null)
# 接受200或206
[ "$code" = "200" ] || [ "$code" = "206" ] && assert "Range 请求返回 200/206" "200" "200" || assert "Range 请求返回 200/206" "200" "$code"

# ── 6. 上传文件校验 ──────────────────────────────────────────────
printf "\n${BOLD}[6] 上传校验${NC}\n"

# 上传非音频文件（应被拒绝）
echo "this is not audio" > /tmp/test_fake.mp3
code=$(curl -sw "%{http_code}" -o /dev/null \
    -H "Authorization: Bearer $TOKEN" \
    -F "file=@/tmp/test_fake.mp3;type=audio/mpeg" \
    "$BASE/api/songs/upload" 2>/dev/null)
assert "伪装mp3被拒绝(400)" "400" "$code"
rm -f /tmp/test_fake.mp3

# 上传 .exe 文件（不支持的扩展名）
echo "MZ fake exe" > /tmp/test_evil.exe
code=$(curl -sw "%{http_code}" -o /dev/null \
    -H "Authorization: Bearer $TOKEN" \
    -F "file=@/tmp/test_evil.exe;type=application/octet-stream" \
    "$BASE/api/songs/upload" 2>/dev/null)
assert "不支持的扩展名被拒绝(400/422)" "1" "$([ "$code" = '400' ] || [ "$code" = '422' ] && echo 1 || echo 0)"
rm -f /tmp/test_evil.exe

# ── 7. 下载配额校验 ──────────────────────────────────────────────
printf "\n${BOLD}[7] 下载配额校验${NC}\n"
code=$(curl -sw "%{http_code}" -o /dev/null \
    -H "Authorization: Bearer $TOKEN" \
    "$BASE/api/songs/1/download" 2>/dev/null)
assert "无配额下载返回403" "403" "$code"

# ── 8. 文件内容去重 ──────────────────────────────────────────────
printf "\n${BOLD}[8] 文件内容去重（全局唯一性）${NC}\n"

# 重新登录（前面流程可能已有其他测试，确保 token 有效）
RELOGIN=$(curl -s -X POST "$BASE/api/auth/login" \
    -H "Content-Type: application/json" \
    -d "{\"email_or_username\":\"$TEST_EMAIL\",\"password\":\"test1234\"}" 2>/dev/null)
TOKEN2=$($PYTHON -c "import json,sys; print(json.load(sys.stdin).get('access_token',''))" <<< "$RELOGIN" 2>/dev/null)

# 生成最小有效 MP3 (ID3 magic bytes + 随机填充)，每次运行内容不同避免去重误判
$PYTHON -c "
import os, time
# 用随机字节确保每次测试内容唯一（不污染去重数据库）
random_payload = os.urandom(512) + str(time.time()).encode()
data = b'ID3\x04\x00\x00\x00\x00\x00\x0a' + random_payload
open('/tmp/test_dedup.mp3', 'wb').write(data)
"

# 第一次上传 → 201 成功
code=$(curl -sw "%{http_code}" -o /dev/null \
    -H "Authorization: Bearer $TOKEN2" \
    -F "file=@/tmp/test_dedup.mp3;type=audio/mpeg" \
    "$BASE/api/songs/upload" 2>/dev/null)
assert "首次上传有效MP3成功(201)" "201" "$code"

# 第二次上传完全相同文件 → 400 被拒绝（去重）
code=$(curl -sw "%{http_code}" -o /dev/null \
    -H "Authorization: Bearer $TOKEN2" \
    -F "file=@/tmp/test_dedup.mp3;type=audio/mpeg" \
    "$BASE/api/songs/upload" 2>/dev/null)
assert "重复上传相同内容被拒绝(400)" "400" "$code"

# 验证第二次失败后配额没有增加（只加了1次）
CREDITS=$(curl -s "$BASE/api/user/credits" -H "Authorization: Bearer $TOKEN2" 2>/dev/null)
UPLOAD_CNT=$($PYTHON -c "import json,sys; print(json.load(sys.stdin).get('upload_count',0))" <<< "$CREDITS" 2>/dev/null)
assert "重复上传不累计上传次数(upload=1)" "1" "$UPLOAD_CNT"

rm -f /tmp/test_dedup.mp3

# ── 9. 管理后台 API ─────────────────────────────────────────────
printf "\n${BOLD}[9] 管理后台 (admin)${NC}\n"

# 未认证访问 admin 应返回 401
code=$(http_code "$BASE/api/admin/stats")
assert "未认证访问 /api/admin/stats 返回401" "401" "$code"

# 普通用户访问 admin 应返回 403
code=$(http_code "$BASE/api/admin/stats" -H "Authorization: Bearer $TOKEN2")
assert "普通用户访问 admin 返回403" "403" "$code"

# admin 登录
ADMIN_LOGIN=$(curl -s -X POST "$BASE/api/auth/login" \
    -H "Content-Type: application/json" \
    -d '{"email_or_username":"admin","password":"'"${TEST_ADMIN_PASSWORD:?请设置 TEST_ADMIN_PASSWORD 为 env.loc 中管理员的明文密码}"'"}' 2>/dev/null)
ADMIN_TOKEN=$($PYTHON -c "import json,sys; print(json.load(sys.stdin).get('access_token',''))" <<< "$ADMIN_LOGIN" 2>/dev/null)
is_admin=$($PYTHON -c "import json,sys; d=json.load(sys.stdin); print(d.get('user',{}).get('is_admin',0))" <<< "$ADMIN_LOGIN" 2>/dev/null)
assert "admin 登录成功且 is_admin=1" "1" "$is_admin"

# admin 访问统计
code=$(http_code "$BASE/api/admin/stats" -H "Authorization: Bearer $ADMIN_TOKEN")
assert "admin 访问统计返回200" "200" "$code"

# admin 访问歌曲列表
code=$(http_code "$BASE/api/admin/songs?status=all" -H "Authorization: Bearer $ADMIN_TOKEN")
assert "admin 查看歌曲列表返回200" "200" "$code"

# admin 访问用户列表
code=$(http_code "$BASE/api/admin/users" -H "Authorization: Bearer $ADMIN_TOKEN")
assert "admin 查看用户列表返回200" "200" "$code"

# admin 访问上传日志
code=$(http_code "$BASE/api/admin/logs/upload" -H "Authorization: Bearer $ADMIN_TOKEN")
assert "admin 查看上传日志返回200" "200" "$code"

# admin 访问下载日志
code=$(http_code "$BASE/api/admin/logs/download" -H "Authorization: Bearer $ADMIN_TOKEN")
assert "admin 查看下载日志返回200" "200" "$code"

# ── 10. 审核流程 ─────────────────────────────────────────────────
printf "\n${BOLD}[10] 审核流程 (review)${NC}\n"

# 找一首 pending 歌曲（刚才测试上传的）
PENDING_ID=$($PYTHON -c "
import json,sys
d = json.load(sys.stdin)
songs = d.get('songs',[])
pending = [s for s in songs if s.get('review_status')=='pending']
print(pending[0]['id'] if pending else '')
" <<< "$(curl -s "$BASE/api/admin/songs?status=pending&limit=10" \
    -H "Authorization: Bearer $ADMIN_TOKEN" 2>/dev/null)" 2>/dev/null)

if [ -n "$PENDING_ID" ]; then
    # 审核通过
    code=$(curl -sw "%{http_code}" -o /dev/null -X POST \
        "$BASE/api/admin/songs/$PENDING_ID/review" \
        -H "Authorization: Bearer $ADMIN_TOKEN" \
        -H "Content-Type: application/json" \
        -d '{"action":"approve","notes":"test auto approve"}' 2>/dev/null)
    assert "审核通过返回200" "200" "$code"

    # 确认状态变为 approved
    song_status=$(curl -s "$BASE/api/songs/$PENDING_ID" 2>/dev/null \
        | $PYTHON -c "import json,sys; print(json.load(sys.stdin).get('review_status',''))" 2>/dev/null)
    assert "审核后状态变为 approved" "approved" "$song_status"
else
    printf "  ${CYAN}⊘${NC} 跳过审核测试（无 pending 歌曲）\n"
fi

# ── 11. 可见性一致性 ─────────────────────────────────────────────
printf "\n${BOLD}[11] 分类数量一致性${NC}\n"

# 获取分类列表中第一个有歌曲的分类
CAT_CHECK=$($PYTHON -c "
import json,sys
cats = json.load(sys.stdin)
for c in cats:
    if c.get('song_count',0) > 0:
        print(f'{c[\"id\"]} {c[\"song_count\"]}'); break
" <<< "$(curl -s "$BASE/api/categories" 2>/dev/null)" 2>/dev/null)
CAT_ID=$(echo "$CAT_CHECK" | awk '{print $1}')
CAT_COUNT=$(echo "$CAT_CHECK" | awk '{print $2}')

if [ -n "$CAT_ID" ]; then
    ACTUAL=$(curl -s "$BASE/api/songs?category=$CAT_ID&limit=1" 2>/dev/null \
        | $PYTHON -c "import json,sys; print(json.load(sys.stdin).get('total',''))" 2>/dev/null)
    assert "分类 $CAT_ID 侧边栏数($CAT_COUNT)==列表数($ACTUAL)" "$CAT_COUNT" "$ACTUAL"
fi

# ── 12. 注销 ──────────────────────────────────────────────────────
printf "\n${BOLD}[12] 注销${NC}\n"
code=$(curl -sw "%{http_code}" -o /dev/null -X POST \
    -H "Authorization: Bearer $TOKEN2" \
    "$BASE/api/auth/logout" 2>/dev/null)
assert "注销返回200" "200" "$code"

# 注销后 token 应失效
code=$(http_code "$BASE/api/user/credits" -H "Authorization: Bearer $TOKEN2")
assert "注销后 token 失效(401)" "401" "$code"

# ── 结果汇总 ─────────────────────────────────────────────────────
printf "\n${BOLD}═══════════════════════════════════${NC}\n"
TOTAL=$((PASS+FAIL))
printf "测试结果: ${GREEN}✓ %d 通过${NC}  ${RED}✗ %d 失败${NC}  共 %d 项\n" "$PASS" "$FAIL" "$TOTAL"
if [ $FAIL -eq 0 ]; then
    printf "${GREEN}${BOLD}所有测试通过！${NC}\n\n"
    exit 0
else
    printf "${RED}${BOLD}有 %d 项测试失败，请检查日志${NC}\n\n" "$FAIL"
    exit 1
fi
