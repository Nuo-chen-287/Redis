"""
db.py · 共享数据源模块（贯穿整个 Redis 专题）

负责连接本机真实的 MySQL，并对外提供一个 query_product()。后面每一版都复用它，
这样我们能专注在「Redis 怎么用」上，而不必每版都重写一遍数据库连接代码。

哪部分是真的、哪部分是模拟的（重要）：
    - 连接 MySQL、连接池、取数据：100% 真实。
    - 单次查询的耗时：单行主键查询其实零点几毫秒，那样 V2 加了缓存也看不出省了多少。
      真实的商品详情页往往要 join 商品/库存/促销好几张表，是个慢查询。所以这里用
      MySQL 的 SLEEP() 在服务端加一段可配置延迟，代表"这是个重查询"——它真实地占住
      一个 DB 连接，从而真实地造成连接池排队。把 SLOW_QUERY_SECONDS 设成 0 就是裸速度。

直接运行本文件可初始化数据库（建库 / 建表 / 塞测试数据），可重复执行：
    .venv/bin/python src/db.py
"""

import os
import threading

import pymysql
from dbutils.pooled_db import PooledDB

# ── MySQL 连接配置（从环境变量读，没有则用本机默认）────────────────
MYSQL_HOST = os.environ.get("Localhost_MYSQL_HOST", "127.0.0.1")
MYSQL_PORT = int(os.environ.get("Localhost_MYSQL_PORT", "3306"))
MYSQL_USER = os.environ.get("Localhost_MYSQL_USER", "root")
MYSQL_PASSWORD = os.environ.get("Localhost_MYSQL_PASSWORD", "12345678")
MYSQL_DB = os.environ.get("Localhost_MYSQL_DB", "redis_learning")

# ── 可调参数 ──────────────────────────────────────────────────────
SLOW_QUERY_SECONDS = 0.2   # 模拟"复杂查询"的耗时；设 0 看真实裸速度
DB_POOL_SIZE = 10          # 连接池上限 = DB 能同时处理的查询数。超过就排队，这正是瓶颈所在

# ── 连接池（maxconnections 满了 blocking=True 会让请求排队等待）──────
_pool = PooledDB(
    creator=pymysql,
    maxconnections=DB_POOL_SIZE,
    blocking=True,                 # 池子满了就阻塞排队，而不是报错——贴近真实 DB 行为
    host=MYSQL_HOST,
    port=MYSQL_PORT,
    user=MYSQL_USER,
    password=MYSQL_PASSWORD,
    database=MYSQL_DB,
    charset="utf8mb4",
    cursorclass=pymysql.cursors.DictCursor,
)

# 统计 DB 真正被查了多少次——这是我们要盯的核心指标（仅本进程内计数，够用）
_query_count = 0
_count_lock = threading.Lock()


def query_product(product_id: int) -> dict | None:
    """查询商品详情。每次调用都真实地占用一个连接池连接 + 执行真实 SQL。"""
    global _query_count
    with _count_lock:
        _query_count += 1

    conn = _pool.connection()      # 从池里借一个连接，借不到就排队
    try:
        with conn.cursor() as cur:
            # 单独跑一句 SLEEP，确保无论商品是否存在都会占住连接这么久，
            # 真实模拟"重查询占着 DB 资源"。生产代码当然没有这句。
            if SLOW_QUERY_SECONDS > 0:
                cur.execute("SELECT SLEEP(%s)", (SLOW_QUERY_SECONDS,))
            cur.execute(
                "SELECT id, name, price, stock FROM product WHERE id = %s",
                (product_id,),
            )
            return cur.fetchone()   # 命中返回 dict，没有则 None
    finally:
        conn.close()               # 归还连接给池子（不是真的关闭）


def get_query_count() -> int:
    return _query_count


def reset_query_count() -> None:
    global _query_count
    with _count_lock:
        _query_count = 0


def init_db() -> None:
    """建库、建表、塞测试数据，可重复执行。"""
    # 第一步：不指定 database 连上去，确保库存在
    root = pymysql.connect(
        host=MYSQL_HOST, port=MYSQL_PORT,
        user=MYSQL_USER, password=MYSQL_PASSWORD, charset="utf8mb4",
    )
    try:
        with root.cursor() as cur:
            cur.execute(
                f"CREATE DATABASE IF NOT EXISTS {MYSQL_DB} "
                f"DEFAULT CHARACTER SET utf8mb4"
            )
        root.commit()
    finally:
        root.close()

    # 第二步：在库里建表 + 塞数据
    conn = pymysql.connect(
        host=MYSQL_HOST, port=MYSQL_PORT, user=MYSQL_USER,
        password=MYSQL_PASSWORD, database=MYSQL_DB, charset="utf8mb4",
    )
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS product (
                    id    INT PRIMARY KEY,
                    name  VARCHAR(128) NOT NULL,
                    price INT NOT NULL,
                    stock INT NOT NULL
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
            # REPLACE 让重复执行也能把数据重置回初始值
            cur.executemany(
                "REPLACE INTO product (id, name, price, stock) VALUES (%s, %s, %s, %s)",
                [
                    (1001, "AirPods Pro 2", 1899, 100),
                    (1002, "iPhone 16 Pro", 7999, 50),
                    (1003, "MacBook Air M4", 8999, 30),
                ],
            )
        conn.commit()
    finally:
        conn.close()
    print(f"✅ 数据库 {MYSQL_DB} 初始化完成（product 表已就绪，3 条测试数据）")


if __name__ == "__main__":
    init_db()
