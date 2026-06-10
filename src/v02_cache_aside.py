"""
V2 · 加缓存（Cache-Aside 旁路缓存，真实 Redis + 真实 MySQL）

V1 的痛点：50 个请求查同一个商品，真实 MySQL 被实打实查了 50 次，49 次纯属浪费。
V2 在 DB 前面加一层 Redis 缓存，用最经典的 **Cache-Aside（旁路缓存）** 套路：

    ① 先问缓存 —— 命中就直接返回，DB 完全不被打扰；
    ② 没命中再查 DB（慢查询）；
    ③ 把查到的结果写回缓存，下次就能命中。

读多写少 + 热点集中的场景下，绝大多数请求都停在第 ① 步，DB 压力骤降。

运行前确保两样东西都活着：
    .venv/bin/python src/db.py        # 初始化 MySQL（只需一次）
    redis-cli ping                    # 应返回 PONG
然后：
    .venv/bin/python src/v02_cache_aside.py

观察重点：
    1. 同样 50 个并发，DB 这次被查了几次？（对比 V1 的 50 次）
    2. 缓存热了之后，总耗时从 ~1s 降到了什么量级？
    3. Part B：故意查一个【不存在】的商品 id，看缓存能不能挡住——这就是「缓存穿透」。
"""

import json
import time
from concurrent.futures import ThreadPoolExecutor

import cache   # 共享 Redis 连接（真实 Redis）
import db      # 共享数据源模块（真实 MySQL）

CONCURRENCY = 50
HOT_PRODUCT_ID = 1001        # 存在的热门商品
MISSING_PRODUCT_ID = 9999    # 不存在的商品 —— Part B 用它引出「缓存穿透」

CACHE_TTL = 60               # 缓存过期时间（秒）。先记住有这东西，TTL 的坑 V3/V4 会专门算
KEY_PREFIX = "product:"      # key 命名习惯：用前缀做业务区分，如 product:1001


def cache_key(product_id: int) -> str:
    return f"{KEY_PREFIX}{product_id}"


def get_product(product_id: int) -> dict | None:
    """
    对外业务接口（V2 版）。Cache-Aside 三步走全在这里。
    和 V1 的区别只有一点：DB 不再是第一站，而是"缓存没有时才去的备胎"。
    """
    key = cache_key(product_id)

    # ① 先问缓存
    cached = cache.r.get(key)
    if cached is not None:
        return json.loads(cached)          # 命中：直接返回，DB 一点没碰

    # ② 缓存没有，回源查真实 DB（这一步才是那个 ~200ms 的慢查询）
    product = db.query_product(product_id)

    # ③ 把结果写回缓存，下次同样的 id 就能在第 ① 步被拦下
    if product is not None:
        cache.r.set(key, json.dumps(product), ex=CACHE_TTL)
    # ⚠️ 注意这个 if：只有"查到了"才回填。查不到（None）我们什么都没存——
    #    这正是 Part B 「缓存穿透」的根源，先记住这行。

    return product


def part_a_cache_works():
    """Part A：Cache-Aside 的威力。同样 50 并发，看 DB 次数和耗时怎么塌下来。"""
    print("=" * 60)
    print("Part A · 缓存的威力（热门商品 = 存在的 id）")
    print("=" * 60)

    cache.r.delete(cache_key(HOT_PRODUCT_ID))   # 清掉残留缓存，从干净的冷状态开始
    db.reset_query_count()

    # —— 第 1 个请求：缓存是空的，必然回源查一次 DB 并回填 ——
    first = get_product(HOT_PRODUCT_ID)
    print(f"\n第 1 个请求（冷缓存）：缓存里没有 → 回源查 DB → 写回 Redis")
    print(f"  拿到：{first}")
    print(f"  此刻 DB 查询次数：{db.get_query_count()}  ← 就这一次")

    # —— 之后 50 个并发请求：缓存已经热了，应当全部命中、根本不碰 DB ——
    start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        results = list(pool.map(lambda _: get_product(HOT_PRODUCT_ID), range(CONCURRENCY)))
    elapsed = time.perf_counter() - start

    ok = sum(1 for x in results if x is not None)
    db_hits_in_batch = db.get_query_count() - 1   # 减掉前面那次预热
    print(f"\n紧接着 {CONCURRENCY} 个用户并发查看同一商品（缓存已热）：")
    print(f"  成功拿到商品：{ok}/{CONCURRENCY}")
    print(f"  这 {CONCURRENCY} 个并发里真正打到 DB 的：{db_hits_in_batch} 个  ← 期望是 0")
    print(f"  DB 总查询次数：{db.get_query_count()}     （V1 同场景是 {CONCURRENCY + 1} 次）")
    print(f"  这批并发总耗时：{elapsed:.3f}s   （V1 约 1s，几乎全耗在连接池排队）")
    print(f"\n  → 49 次重复的慢查询被 Redis 拦下了，DB 只在第一次被打扰。这就是 Cache-Aside。")


def part_b_penetration():
    """Part B：故意查不存在的商品，暴露「缓存穿透」——V2 解决旧痛点后冒出的新痛点。"""
    print("\n" + "=" * 60)
    print("Part B · 一个能绕过缓存的攻击：反复查【不存在】的商品")
    print("=" * 60)

    cache.r.delete(cache_key(MISSING_PRODUCT_ID))   # 确保它在缓存里也不存在
    db.reset_query_count()

    print(f"\n连续 5 次查询不存在的商品 id={MISSING_PRODUCT_ID}：")
    for i in range(5):
        result = get_product(MISSING_PRODUCT_ID)
        print(f"  第 {i + 1} 次 → 返回 {result}，DB 累计被查 {db.get_query_count()} 次")

    print(f"\n  5 次查询，DB 被打了 {db.get_query_count()} 次——缓存一次都没挡住！")
    print(f"  原因：DB 查不到返回 None，而 get_product 里 `if product is not None` 让我们")
    print(f"        【没有把『查不到』这件事写进缓存】。于是每次都在第 ① 步扑空、每次都穿透到 DB。")
    print(f"  这就是「缓存穿透」：有人专门拿不存在的 id 狂刷，缓存形同虚设，DB 直接裸奔。")


def main():
    part_a_cache_works()
    part_b_penetration()

    print("\n" + "-" * 60)
    print("思考题（带着这些进 V3）：")
    print("1. 既然『查不到』也是个确定的答案，能不能把它也缓存起来（比如存个空值 + 短 TTL）？")
    print("   这样不存在的 id 第 2 次起也能在缓存层被拦下。会有什么副作用？")
    print("2. 如果攻击者每次都用【不同的】不存在 id（9999、9998、9997…），缓存空值还够用吗？")
    print("   有没有办法在【进缓存之前】就判断『这个 id 根本不可能存在』？（提示：布隆过滤器）")
    print("3. Part A 里我们先预热了一次。如果不预热，50 个请求一上来同时扑向冷缓存，")
    print("   会有不止 1 个漏到 DB——那是另一个坑「缓存击穿」，我们 V4 再算这笔账。")


if __name__ == "__main__":
    main()
