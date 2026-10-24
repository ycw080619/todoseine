#!/bin/bash
# 在容器内完成：装 SQLCipher → 剥 1024 字节头 → 导出明文 → 导出 JSONL
# 用法（宿主机）：
#   docker run --rm -e NTQQ_DB_KEY=<口令> -v <nt_db目录>:/src:ro -v <输出目录>:/data \
#     python:3.11-slim --dns 114.114.114.114 --dns 8.8.8.8 \
#     bash /src/run.sh
set -euo pipefail

apt-get update -qq
apt-get install -y -qq sqlcipher >/dev/null

SRC=${SRC:-/src/nt_msg.db}
DAYS=${DAYS:-7}

# 1) 剥掉 NTQQ 的 1024 字节自定义头
tail -c +1025 "$SRC" > /data/clear.db

# 2) 生成解密 SQL（PRAGMA 顺序不能变；口令里的单引号要转义）
KEY_ESC=${NTQQ_DB_KEY//\'/\'\'}
cat > /data/dec.sql <<SQL
PRAGMA cipher_page_size = 4096;
PRAGMA key = '$KEY_ESC';
PRAGMA kdf_iter = 4000;
PRAGMA cipher_hmac_algorithm = HMAC_SHA1;
PRAGMA cipher_kdf_algorithm = PBKDF2_HMAC_SHA512;
ATTACH DATABASE '/data/plain.db' AS plaintext KEY '';
SELECT sqlcipher_export('plaintext');
DETACH DATABASE plaintext;
SQL

# 3) 导出明文库（目标文件必须不存在）
rm -f /data/plain.db
sqlcipher /data/clear.db < /data/dec.sql

# 4) 导出 JSONL
TOOLS=${TOOLS:-/tools}
python3 "$TOOLS/export.py" /data/plain.db /data/messages.jsonl "$DAYS"

# 5) 清理大文件
rm -f /data/clear.db /data/plain.db /data/dec.sql
