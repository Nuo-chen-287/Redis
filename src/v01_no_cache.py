"""
V1 · 裸查询基线（无缓存，真实 MySQL）

整个专题故意写"坏"的起点：商品详情查询直接打真实的本机 MySQL，没有任何缓存。
数据源在 db.py（真实连接池 + 真实 SQL，外加可配置的慢查询延迟来代表"重查询"）。

与后续版本的关系：
    V1 没有 Redis、没有缓存，目的是建立一条基线——让"每个请求都压在 DB 上"这件事
    用数字直观地显示出来。V2 会在它前面加一层 Redis 缓存，再回头对比这组数字。

运行前先初始化数据库（只需一次）：
    .venv/bin/python src/db.py
然后：
    .venv/bin/python src/v01_no_cache.py

观察重点（运行后重点看这几个数字）：
    1. DB 被查询的总次数 == 请求总数吗？（无缓存时，50 个请求 = 50 次 DB 查询）
    2. 总耗时：50 个并发请求受限于连接池，整体花了多久？
    3. 想象一下：如果这是个热门商品，1 秒内来 1 万次请求，DB 扛得住吗？
"""

import time
from concurrent.futures import ThreadPoolExecutor

import db   # 共享数据源模块（真实 MySQL）

CONCURRENCY = 50   # 同时打进来的请求数（模拟一个热门商品被很多人同时看）
HOT_PRODUCT_ID = 1001


def get_product(product_id: int) -> dict | None:
    """对外的"业务接口"。V1 里它什么都不做，直接转手问 DB。"""
    return db.query_product(product_id)


def main():
    db.reset_query_count()
    print(f"模拟：{CONCURRENCY} 个用户同时查看热门商品 {HOT_PRODUCT_ID}")
    print(f"（DB 单次查询约 {db.SLOW_QUERY_SECONDS*1000:.0f}ms，连接池上限 {db.DB_POOL_SIZE}）\n")

    start = time.perf_counter()
    # 用线程池模拟并发请求，全都查同一个热门商品
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        results = list(pool.map(lambda _: get_product(HOT_PRODUCT_ID), range(CONCURRENCY)))
    elapsed = time.perf_counter() - start

    ok = sum(1 for r in results if r is not None)
    print(f"成功拿到商品的请求：{ok}/{CONCURRENCY}")
    print(f"示例返回：{results[0]}")
    print(f"DB 实际被查询次数：{db.get_query_count()}   <-- 注意：等于请求数，一次都没省")
    print(f"总耗时：{elapsed:.2f}s   （受限于连接池，请求只能 {db.DB_POOL_SIZE} 个一批地排队过）")

    print("\n" + "-" * 56)
    print("思考题（带着这些进 V2）：")
    print("1. 这 50 个请求查的是同一个商品，数据完全一样，")
    print("   却让 DB 老老实实查了 50 次——这 49 次是不是白费了？")
    print("2. 如果把第一次查到的结果存进 Redis，后面的请求直接读，")
    print("   DB 查询次数能降到几次？总耗时会变成多少？")
    print("3. 缓存放进程内存里行不行？多台服务器各存各的会有什么问题？（这就是为什么要 Redis）")


if __name__ == "__main__":
    main()
