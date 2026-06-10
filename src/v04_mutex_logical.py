"""
V4 · 互斥锁 / 逻辑过期，治「缓存击穿」（真实 Redis + 真实 MySQL）

先分清它和 V3「穿透」的区别——这是 V4 的全部前提：
    · 缓存穿透（V3）：查的 id【根本不存在】，缓存永远建不起来，每次都漏到 DB。
    · 缓存击穿（V4）：查的是一个【真实存在的热点 id】，平时稳稳命中缓存；可一旦它的
      TTL 到期、缓存【瞬间消失】，而此刻又有海量并发同时涌来——它们会一起发现缓存没了、
      一起冲去查【同一条】真实数据。布隆和空值缓存都拦不住（id 是真的），DB 被同一个 key
      在一瞬间打穿。这就是「击穿」：不是一群坏 id 慢慢磨，而是一个好 key 在过期那刹那被踩塌。

V4 用两种【互不相同】的思路把它堵上，分别对应工业界两套经典解法：

    解法一 · 互斥锁（Mutual Exclusion）
        缓存 miss 后，先抢一把锁（Redis 的 SET NX）。只有抢到锁的那一个请求去查 DB、
        重建缓存；其余请求【等着】，等缓存被重建好了再读。→ DB 只被重建它的那一次打到。
        代价：没抢到锁的请求要【等】（强一致，但牺牲了一点响应速度）。

    解法二 · 逻辑过期（Logical Expiration）
        干脆让热点 key【永不物理过期】（不设 Redis TTL），而是把"过期时间"当成数据的
        一个字段塞进 value 里。读到的时候自己判断逻辑上过没过期：过期了，就抢锁、开一个
        【后台线程】去异步重建，而当前请求【立刻返回手里这份旧数据，绝不等待】。
        代价：重建那一小段时间里，大家拿到的是【略旧】的数据（牺牲一致性，换不阻塞）。

一句话记牢这两者的取舍：
        互斥锁 = 宁可让你【等】，也要给你【最新】的；
        逻辑过期 = 宁可给你【旧一点】的，也【绝不让你等】。

运行前确保两样都活着：
    .venv/bin/python src/db.py        # 初始化 MySQL（只需一次）
    redis-cli ping                    # 应返回 PONG
然后：
    .venv/bin/python src/v04_mutex_logical.py

观察重点（三个 Part 对照着看）：
    1. Part A：热点 key 过期瞬间放 50 并发，DB 被打几次？最慢的请求等了多久？（这就是击穿）
    2. Part B：换成互斥锁版，同样 50 并发，DB 被打几次？大家的耗时和 Part A 比怎么样？
    3. Part C：换成逻辑过期版，DB 被打几次？这次大家【几乎没等】——但拿到的数据"新"吗？
"""

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import cache   # 共享 Redis 连接（真实 Redis）
import db      # 共享数据源模块（真实 MySQL）

CONCURRENCY = 50
HOT_PRODUCT_ID = 1001        # 一个【真实存在】的热点商品——击穿的主角永远是真 key

CACHE_TTL = 60               # 正常数据的缓存时长（秒）
LOCK_TTL = 5                 # 锁的自动过期时间：万一抢到锁的请求中途崩了没释放，锁也能自己消失，
                             # 不至于把这个 key 永久锁死（这是分布式锁的"保命"设定，V5 细讲）
LOGICAL_TTL = 30             # 逻辑过期方案里，一份数据被认为"新鲜"的时长（秒）

KEY_PREFIX = "product:"
LOCK_PREFIX = "lock:product:"


def cache_key(product_id: int) -> str:
    return f"{KEY_PREFIX}{product_id}"


def lock_key(product_id: int) -> str:
    return f"{LOCK_PREFIX}{product_id}"


# ─────────────────────────────────────────────────────────────────────────────
# 基线：V2 那套朴素读法（无锁）。Part A 用它来【制造】击穿现场。
# ─────────────────────────────────────────────────────────────────────────────
def get_product_naive(product_id: int) -> dict | None:
    """Cache-Aside 最朴素的写法：miss 了就【各查各的 DB】。没有任何并发保护。"""
    key = cache_key(product_id)

    cached = cache.r.get(key)
    if cached is not None:
        return json.loads(cached)

    # 缓存没有 → 直接回源。问题就在这：50 个线程会【同时】走到这一行，一起查同一条 DB。
    product = db.query_product(product_id)
    if product is not None:
        cache.r.set(key, json.dumps(product), ex=CACHE_TTL)
    return product


# ─────────────────────────────────────────────────────────────────────────────
# 解法一：互斥锁重建。只放一个请求去查 DB，其余等它把缓存填好再读。
# ─────────────────────────────────────────────────────────────────────────────
def get_product_with_mutex(product_id: int) -> dict | None:
    key = cache_key(product_id)

    # ① 先查缓存，命中直接返回（绝大多数请求走这条快路，不会去抢锁）
    cached = cache.r.get(key)
    if cached is not None:
        return json.loads(cached)

    # ② 未命中 → 抢锁。SET NX：只有 key 不存在时才设置成功，天然保证"同一时刻只有一个赢家"。
    #    ex=LOCK_TTL 给锁一个兜底过期，防止赢家崩溃后锁永远不释放、把 key 锁死。
    lkey = lock_key(product_id)
    got_lock = cache.r.set(lkey, "1", nx=True, ex=LOCK_TTL)

    if got_lock:
        # —— 我是赢家：负责重建缓存 ——
        try:
            # 双重检查（double-check）：抢到锁后【再读一次缓存】。因为从"我 miss"到"我抢到锁"
            # 之间，可能已经有别的赢家把缓存填好了（在更复杂的时序里会发生）。读到就别白查 DB。
            cached = cache.r.get(key)
            if cached is not None:
                return json.loads(cached)

            product = db.query_product(product_id)      # 全场只有这一次真正查 DB
            if product is not None:
                cache.r.set(key, json.dumps(product), ex=CACHE_TTL)
            return product
        finally:
            # 重建完，释放锁，让等待的人能继续。
            # 注意这里直接 del——其实埋了个 bug（可能误删别人的锁），这正是 V5 分布式锁要修的第一刀。
            cache.r.delete(lkey)
    else:
        # —— 我是输家：有人正在重建，我【自旋等待】缓存出现，再读 ——
        # 不是去查 DB，而是反复瞄缓存；这就是"只放一个请求重建、其余等结果"的体现。
        for _ in range(100):              # 最多等 100 * 20ms = 2s，兜底防止万一死等
            time.sleep(0.02)
            cached = cache.r.get(key)
            if cached is not None:
                return json.loads(cached)
        # 等太久（极端情况，正常不会走到）：兜底自己查一次，别把请求卡死
        return db.query_product(product_id)


# ─────────────────────────────────────────────────────────────────────────────
# 解法二：逻辑过期。key 永不物理过期，"过期时间"写进 value 自己判断；过期就异步重建、绝不阻塞。
# ─────────────────────────────────────────────────────────────────────────────
def _pack(product: dict, ttl: int) -> str:
    """把数据连同一个【逻辑过期时刻】打包成 value。expire_at 是一个未来的 Unix 时间戳。"""
    return json.dumps({"data": product, "expire_at": time.time() + ttl})


def _rebuild_async(product_id: int, lkey: str) -> None:
    """后台线程里干的活：慢悠悠查 DB、把缓存换成带【新】逻辑过期时间的数据，最后释放锁。"""
    try:
        product = db.query_product(product_id)
        if product is not None:
            cache.r.set(cache_key(product_id), _pack(product, LOGICAL_TTL))  # 仍然不设 Redis TTL
    finally:
        cache.r.delete(lkey)


def get_product_logical_expire(product_id: int) -> dict | None:
    key = cache_key(product_id)
    cached = cache.r.get(key)

    if cached is None:
        # 逻辑过期方案的前提是：热点 key 由后台【提前预热】好、永远在缓存里躺着。
        # 真的一个都没有（冷启动）→ 退化成查一次 DB 兜底建起来。
        product = db.query_product(product_id)
        if product is not None:
            cache.r.set(key, _pack(product, LOGICAL_TTL))
        return product

    obj = json.loads(cached)

    # ① 还没到逻辑过期时间 → 数据新鲜，直接返回（热点 key 绝大多数时间走这条路）
    if obj["expire_at"] > time.time():
        return obj["data"]

    # ② 已逻辑过期 → 抢锁。抢到的人【开后台线程】去异步重建，没抢到的人什么都不用做。
    lkey = lock_key(product_id)
    if cache.r.set(lkey, "1", nx=True, ex=LOCK_TTL):
        threading.Thread(target=_rebuild_async, args=(product_id, lkey), daemon=True).start()

    # ③ 关键：无论抢没抢到锁，当前请求都【立刻返回手里这份旧数据，绝不等待重建】。
    #    代价就是这一小段重建窗口里，所有人拿到的是上一版的（略旧的）数据。
    return obj["data"]


# ─────────────────────────────────────────────────────────────────────────────
# 小工具：用一批并发请求打同一个热点 key，返回 (总墙钟耗时, 每个请求各自的耗时列表)
# ─────────────────────────────────────────────────────────────────────────────
def hammer(get_fn, product_id: int, concurrency: int) -> tuple[float, list[float]]:
    def one_request(_):
        t0 = time.perf_counter()
        get_fn(product_id)
        return time.perf_counter() - t0

    start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        latencies = list(pool.map(one_request, range(concurrency)))
    return time.perf_counter() - start, latencies


def _fmt(latencies: list[float]) -> str:
    return (f"最慢 {max(latencies) * 1000:6.1f}ms · "
            f"最快 {min(latencies) * 1000:5.1f}ms · "
            f"平均 {sum(latencies) / len(latencies) * 1000:6.1f}ms")


# ─────────────────────────────────────────────────────────────────────────────
# 三个对照实验
# ─────────────────────────────────────────────────────────────────────────────
def part_a_breakdown():
    """Part A：还原缓存击穿现场——热点 key 过期瞬间，50 并发一起穿到 DB。"""
    print("=" * 68)
    print("Part A · 缓存击穿现场：热点 key 过期的那一瞬，50 并发一起涌入（无锁）")
    print("=" * 68)

    # 先把热点缓存预热好（模拟"平时它一直在缓存里、稳稳命中"）
    cache.r.delete(lock_key(HOT_PRODUCT_ID))
    get_product_naive(HOT_PRODUCT_ID)

    # 关键一步：手动删掉热点 key，精确模拟"它的 TTL 刚好在此刻到期、缓存瞬间消失"
    cache.r.delete(cache_key(HOT_PRODUCT_ID))
    db.reset_query_count()

    elapsed, lat = hammer(get_product_naive, HOT_PRODUCT_ID, CONCURRENCY)

    print(f"\n  {CONCURRENCY} 个并发同时查【真实热点】id={HOT_PRODUCT_ID}，而它刚好缓存过期：")
    print(f"  DB 被打了 {db.get_query_count()} 次  ← 没有任何保护，几乎每个请求都各查各的 DB")
    print(f"  请求耗时：{_fmt(lat)}")
    print(f"  总墙钟：{elapsed:.3f}s  ← DB 连接池(10) 被打满、排队，所以整体被拖慢")
    print(f"\n  → 一个【真实存在】的好 key，在过期刹那被并发踩塌。布隆/空值都救不了它，因为 id 是真的。")


def part_b_mutex():
    """Part B：互斥锁——只放一个请求去重建，其余等结果。DB 只被打 1 次。"""
    print("\n" + "=" * 68)
    print("Part B · 互斥锁重建：miss 后先抢锁，只放一个去查 DB，其余等缓存填好")
    print("=" * 68)

    cache.r.delete(lock_key(HOT_PRODUCT_ID))
    get_product_with_mutex(HOT_PRODUCT_ID)              # 预热

    cache.r.delete(cache_key(HOT_PRODUCT_ID))           # 同样模拟"过期瞬间"
    db.reset_query_count()

    elapsed, lat = hammer(get_product_with_mutex, HOT_PRODUCT_ID, CONCURRENCY)

    print(f"\n  同样 {CONCURRENCY} 个并发、同样的过期瞬间，这次走【互斥锁】版本：")
    print(f"  DB 被打了 {db.get_query_count()} 次  ← 期望 1：只有抢到锁的那个请求查了 DB")
    print(f"  请求耗时：{_fmt(lat)}")
    print(f"  总墙钟：{elapsed:.3f}s")
    print(f"\n  → DB 不再被击穿。代价：没抢到锁的请求要【自旋等】那一个赢家把缓存重建好（看最慢耗时）。")


def part_c_logical_expire():
    """Part C：逻辑过期——过期了也不等，抢到锁的开后台线程异步重建，大家立刻拿旧数据走。"""
    print("\n" + "=" * 68)
    print("Part C · 逻辑过期：key 永不物理过期，过期就异步重建，请求【绝不等待】")
    print("=" * 68)

    cache.r.delete(lock_key(HOT_PRODUCT_ID))

    # 把热点 key 种成【已经逻辑过期】的状态：先查一次真实数据，但把 expire_at 设在过去。
    # 这样 50 并发一进来就都会判定"逻辑上过期了"，精确还原"热点刚过期"的时刻。
    seed = db.query_product(HOT_PRODUCT_ID)
    cache.r.set(cache_key(HOT_PRODUCT_ID),
                json.dumps({"data": seed, "expire_at": time.time() - 1}))  # 过期时刻在过去
    db.reset_query_count()                              # 预热这次查询不算进 demo

    elapsed, lat = hammer(get_product_logical_expire, HOT_PRODUCT_ID, CONCURRENCY)

    # 并发请求自己【不等】重建，所以后台重建线程可能还没跑完。等它一下再数 DB 次数。
    time.sleep(LOCK_TTL + 0.5)

    print(f"\n  同样 {CONCURRENCY} 个并发、热点已逻辑过期，这次走【逻辑过期】版本：")
    print(f"  DB 被打了 {db.get_query_count()} 次  ← 期望 1：只有一个后台线程在异步重建")
    print(f"  请求耗时：{_fmt(lat)}  ← 注意最慢的也极快：没有任何请求【等】过重建")
    print(f"  总墙钟：{elapsed:.3f}s")
    print(f"\n  → 没人等，DB 也只挨一次。代价：重建那一小会儿，大家拿到的是【上一版旧数据】。")


def main():
    part_a_breakdown()
    part_b_mutex()
    part_c_logical_expire()

    print("\n" + "-" * 68)
    print("两种解法的取舍，一句话记牢：")
    print("  互斥锁  = 宁可让你【等】，也要给你【最新】的（强一致，牺牲响应）")
    print("  逻辑过期 = 宁可给你【旧一点】的，也【绝不让你等】（高可用，牺牲一致性）")
    print("-" * 68)
    print("思考题（带着这些进 V5）：")
    print("1. Part A 我们只删了【一个】热点 key 就这么痛。如果是【成千上万】个 key 因为当初设了")
    print("   相同 TTL、在同一秒集体过期，会发生什么？（这就是『缓存雪崩』——解法之一是给 TTL")
    print("   加一个随机抖动，别让大家约好同一时刻一起死。）")
    print("2. 互斥锁版里，赢家释放锁用的是直接 `DEL lock`。设想：赢家的查询特别慢、超过了锁的")
    print("   5s 自动过期，锁先自己没了 → 别人趁机抢到新锁；这时第一个赢家终于查完、一 DEL，")
    print("   删掉的是【别人】的锁。这个 bug 怎么修？（提示：锁的 value 放一个只有自己知道的随机")
    print("   令牌，删之前先核对是不是自己的——这正是 V5 手搓分布式锁要解决的第一个问题。）")
    print("3. 逻辑过期版永不设 Redis TTL，热点 key 会【永远占着内存】。冷门 key 也这么干行不行？")
    print("   什么样的 key 才值得用逻辑过期这套重武器？")


if __name__ == "__main__":
    main()
