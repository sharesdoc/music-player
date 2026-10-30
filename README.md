# NEUROMIX // GRID

一个自托管的网页音乐播放器：扫描本地音乐库，浏览器里在线播放，支持多用户、分类、歌单、评论、排行榜，并可选用 AI 自动给歌曲分类、获取歌词。

后端使用 FastAPI + SQLite，前端是单页应用（`app/static/index.html`），无需构建步骤。

## 功能

- **音乐库**：导入本地目录（mp3 / m4a / flac / ogg / wav），读取标签信息，按 SHA-256 去重
- **在线播放**：支持 HTTP Range 的流式播放，可拖动进度条；支持下载
- **用户系统**：注册 / 登录 / 注销，JWT 访问令牌 + 刷新令牌，令牌黑名单，登录失败锁定
- **内容组织**：分类、歌手、个人歌单、播放历史、评论、自定义背景图
- **排行榜**：热门歌曲 / 分类 / 歌手（每小时刷新，只统计公开歌曲）
- **上传与分享**：用户上传歌曲，可设为公开分享；删除申请由管理员审核
- **管理后台**：统计、歌曲审核、用户、分类合并 / 重命名、上传与下载日志
- **AI（可选）**：通过 OpenAI 兼容接口自动分类；歌词获取
- **安全**：每 IP 限流、安全响应头、CORS 可配置

## 环境要求

- Python 3（依赖见 `requirements.txt`）
- Linux 或 macOS（部署脚本面向 Ubuntu）
- 可选：Nginx（反向代理 / HTTPS）、任意 OpenAI 兼容的模型服务（AI 功能）

## 快速开始

```bash
git clone <your-repo-url> neuromix && cd neuromix

# 1. 准备本地配置（env.loc 不会被提交）
cp env.loc.example env.loc
#    编辑 env.loc：至少修改 JWT_SECRET、SYSTEM_MUSIC_PATH、ADMIN_PASSWORD

# 2. 安装依赖并初始化数据库
./run setup

# 3. 导入音乐库（默认读取 SYSTEM_MUSIC_PATH）
./run import

# 4. 启动
./run start
```

默认监听 `http://localhost:2510`，健康检查地址为 `/health`。

### 管理员账号

管理员不存数据库，直接由环境变量认证：

- `ADMIN_USERNAME`：用户名（默认 `admin`）
- `ADMIN_PASSWORD`：密码的 **SHA-1 哈希**（40 位十六进制）

生成哈希：

```bash
printf "%s" "你的密码" | shasum | cut -d" " -f1
```

## 配置

配置优先级：环境变量 > `config.yml`。密钥类信息只通过环境变量配置，不要写进 `config.yml`。

| 变量 | 说明 | 默认值 |
| --- | --- | --- |
| `APP_ENV` | `local` 或 `production`；生产环境未设置 `JWT_SECRET` 会拒绝启动 | `local` |
| `APP_PORT` | 监听端口 | `2510` |
| `JWT_SECRET` | JWT 签名密钥（必填，需强随机值） | 无 |
| `DATABASE_PATH` | SQLite 文件路径 | `./data/music.db` |
| `SYSTEM_MUSIC_PATH` | 现有音乐库目录 | `config.yml` 中的值 |
| `UPLOAD_PATH` | 用户上传目录 | `./uploads` |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | 超级管理员账号 / 密码 SHA-1 | 无 |
| `AI_ENABLED` | 是否启用 AI 功能 | `config.yml` 中的值 |
| `AI_BASE_URL` / `AI_API_KEY` / `AI_MODEL` | OpenAI 兼容接口的地址、密钥、模型 | 无 |

生成强随机密钥：

```bash
python3 -c "import secrets; print(secrets.token_hex(64))"
```

其余参数（上传大小限制、令牌有效期、限流、CORS 等）见 [`config.yml`](config.yml)。

## 常用命令

```text
./run setup                  安装依赖、初始化数据库
./run start | stop | restart 启动 / 停止 / 重启服务
./run status                 查看状态
./run log [err]              查看运行日志 / 错误日志
./run import [path]          导入音乐库
./run import-ai [path]       导入并用 AI 分类
./run organize [path]        按分类整理音乐文件（会移动文件，先加 --dry-run 预演）
./run backfill               给存量歌曲回填 SHA-256 哈希
./run test                   运行自动化测试
./run release [version]      打包发布
```

全局选项：`-p <端口>` 临时指定端口，`-v` 显示详细步骤，`-a` 非交互模式。

### 运行测试

测试脚本需要管理员明文密码（对应 `env.loc` 中的哈希）：

```bash
TEST_ADMIN_PASSWORD='你的密码' ./run test
```

## 生产部署

在 Ubuntu 服务器上，可使用一键安装脚本，完成依赖安装、应用部署、配置生成、Nginx 配置与服务启动：

```bash
sudo bash install --domain your.domain.com --port 2510 --music /path/to/music
```

可选参数：`--admin-user`、`--admin-pass`（默认随机生成）、`--skip-import`、`--skip-nginx`。

也可以手动部署：`APP_ENV=production ./run start` 会读取 `env.pro`，先把其中的占位值改成真实值（该文件含真实密钥后不要提交），用 Gunicorn 运行，再参考 [`nginx/example.com.conf`](nginx/example.com.conf) 配置反向代理（把 `example.com` 换成你的域名与证书路径）。

## 项目结构

```text
app/
  main.py          应用入口：路由挂载、中间件、健康检查、定时任务
  config.py        配置加载（环境变量 + config.yml）
  database.py      SQLite 初始化与连接
  auth/            注册、登录、令牌
  music/           歌曲、分类、歌单、评论、排行榜、背景图
  admin/           管理后台接口
  ai/              AI 分类与歌词
  middleware/      限流
  static/          前端单页应用
config.yml         非机密配置
env.loc.example    本地环境变量模板
env.pro            生产环境变量模板
nginx/             Nginx 配置示例
deploy/, install   部署脚本
scripts/           导入、整理、哈希回填、测试、打包脚本
run                统一管理脚本
```

## API 概览

所有接口位于 `/api` 下，需要登录的接口使用 `Authorization: Bearer <token>`。

| 前缀 | 内容 |
| --- | --- |
| `/api/auth` | `register`、`login`、`logout`、`me`、`refresh` |
| `/api/songs`、`/api/categories`、`/api/artists` | 浏览、搜索、流式播放、下载、上传、评论 |
| `/api/charts` | 热门歌曲 / 分类 / 歌手 |
| `/api/user` | 播放历史、积分、个人歌单 |
| `/api/backgrounds` | 自定义背景图 |
| `/api/admin` | 管理后台（仅管理员） |

## 安全提示

- 不要提交 `env.loc`、`.env`、填写了真实密钥的 `env.pro`、数据库、日志和上传文件（`.gitignore` 已排除）
- 生产环境务必设置强随机的 `JWT_SECRET`，并把 `config.yml` 中的 `cors.allowed_origins` 从 `["*"]` 改为具体域名
- 只托管你有权分发的音乐，版权责任由部署者自行承担

## 许可证

尚未选择许可证。发布前请在仓库根目录添加 `LICENSE` 文件，并更新本节。
