"""
cache.py · 共享 Redis 连接模块（从 V2 起贯穿整个专题）

和 db.py 是一对：db.py 提供"慢而权威"的真实 MySQL，cache.py 提供"快而易失"的
真实 Redis。后面每一版的缓存代码都复用这里的连接，专注在「怎么用 Redis」上。

这里 100% 连真实的本机 Redis（brew 装的那个），不是 mock：
    - decode_responses=True：让 get() 直接返回 str 而不是 bytes，省得每次手动 .decode()。
    - 单独抽出 reset_db_for_demo() 之类的 helper 没必要——清 key 直接用 r.delete()。

运行前确认 Redis 活着：
    redis-cli ping   →   PONG
"""

import os

import redis

# ── Redis 连接配置（从环境变量读，缺省回退本机默认）──────────────────
REDIS_HOST = os.environ.get("Localhost_REDIS_HOST", "127.0.0.1")
REDIS_PORT = int(os.environ.get("Localhost_REDIS_PORT", "6379"))
REDIS_DB = int(os.environ.get("Localhost_REDIS_DB", "0"))

# 全局共享的客户端。redis-py 内部自带连接池，多线程直接共用这一个对象是安全的。
# decode_responses=True → 存取的都是 str，方便配合 json.dumps / json.loads。
r = redis.Redis(
    host=REDIS_HOST,
    port=REDIS_PORT,
    db=REDIS_DB,
    decode_responses=True,
)


if __name__ == "__main__":
    # 直接运行本文件做个连通性自检
    print("PING →", r.ping())
    print(f"已连上 Redis {REDIS_HOST}:{REDIS_PORT} (db={REDIS_DB})")
