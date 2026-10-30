# 生产环境配置模板
# 部署时将此文件复制为 .env 并填写真实值
# 机密信息必须通过此文件或系统环境变量配置，不可提交到代码库

APP_ENV=production
APP_DEBUG=false
APP_PORT=8080
LOG_LEVEL=INFO

# 【必填】JWT 密钥，生产环境必须使用强随机值
# 生成命令: python3 -c "import secrets; print(secrets.token_hex(64))"
JWT_SECRET=CHANGE_ME_USE_STRONG_RANDOM_SECRET

# 数据库路径
DATABASE_PATH=/data/neuromix/music.db

# 音乐路径
SYSTEM_MUSIC_PATH=/var/www/html/downloads/mp3
UPLOAD_PATH=/data/neuromix/uploads

# AI（OpenAI 兼容接口，用于歌曲自动分类）
# AI_BASE_URL=https://api.openai.com/v1
AI_API_KEY=sk-CHANGE_ME
AI_MODEL=gpt-4o-mini
AI_ENABLED=true
