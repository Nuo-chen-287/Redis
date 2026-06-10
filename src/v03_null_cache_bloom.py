"""
V3 · 缓存空值 + 布隆过滤器，治「缓存穿透」（真实 Redis + 真实 MySQL）

V2 的痛点（Part B）：查一个【不存在】的商品 id，DB 查不到返回 None，而我们只缓存
"查到了"的结果、从不缓存"查不到"这件事。于是不存在的 id 每次都穿透到 DB——这就是
「缓存穿透」。V3 用两招把它堵上，而且这两招是【叠加】的、各管一种攻击姿势：

    招式一 · 缓存空值（Cache Null）
        DB 查不到时，往缓存里写一个【空值标记】+ 短 TTL。下次同一个 id 再来，
        在缓存层就被拦下，不再穿透。→ 治"反复刷【同一个】不存在的 id"。

    招式二 · 布隆过滤器（Bloom Filter）
        在【进缓存之前】就用一个极省内存的概率结构判断"这个 id 根本不可能存在"，
        是就直接挡掉，连 Redis 都不查。→ 治"每次换【不同的】不存在 id 来刷"
        （空值缓存对这种攻击无能为力，见 Part B）。

布隆过滤器我们【手搓】在真实 Redis 的 bitmap 上（SETBIT/GETBIT），不借任何现成库，
这样你能看清它"用 k 个哈希点亮 m 个 bit"的全部内幕，以及它为什么会有【假阳性、无假阴性】。

运行前确保两样都活着：
    .venv/bin/python src/db.py        # 初始化 MySQL（只需一次）
    redis-cli ping                    # 应返回 PONG
然后：
    .venv/bin/python src/v03_null_cache_bloom.py

观察重点（三个 Part 对照着看）：
    1. Part A：反复查【同一个】不存在 id，DB 被打几次？（对比 V2 的 5 次）
    2. Part B：每次换【不同的】不存在 id，光靠空值缓存还挡得住吗？Redis 里多了什么垃圾？
    3. Part C：加上布隆过滤器后，Part B 那串不同 id 还能打到 DB 吗？真实 id 会被误挡吗？
"""

import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor

import cache   # 共享 Redis 连接（真实 Redis）
import db      # 共享数据源模块（真实 MySQL）

CONCURRENCY = 50
HOT_PRODUCT_ID = 1001
REAL_PRODUCT_IDS = [1001, 1002, 1003]   # DB 里真实存在的全部商品（init_db 塞的那三条）

CACHE_TTL = 60               # 正常数据的缓存时长（秒）
NULL_TTL = 10                # 空值标记的缓存时长——故意比正常数据【短得多】，原因见文末思考题
KEY_PREFIX = "product:"

# 空值标记：DB 里确实没有这条数据时，往缓存写这个串占位。
# 用一个一眼能认出的特殊串（而不是空字符串），这样 redis-cli 里 GET 一看就懂、也不会和
# 真实 JSON 撞上。get_product 里靠"取出来 == 这个串"来区分"缓存的 None" vs "真实数据"。
NULL_MARKER = "__NULL__"


def cache_key(product_id: int) -> str:
    return f"{KEY_PREFIX}{product_id}"


# ─────────────────────────────────────────────────────────────────────────────
# 招式二的实现：手搓一个跑在 Redis bitmap 上的布隆过滤器
# ─────────────────────────────────────────────────────────────────────────────
class RedisBloomFilter:
    """
    布隆过滤器：一个【只会误判"在"、绝不会误判"不在"】的概率型集合。

    它本质就是一条很长的二进制位（这里直接用 Redis 的一个 key 当 bitmap）：
      - add(x)：拿 k 个不同的哈希函数算出 x 的 k 个位置，把这 k 个 bit 全置 1；
      - might_contain(x)：同样算出 k 个位置，
            * 只要有【任意一个】 bit 是 0  → x【绝对没被加进来过】（无假阴性，可放心挡）；
            * k 个 bit 全是 1            → x【可能】加进来过（也可能是别人把这些位凑巧点亮的 → 假阳性）。

    为什么"无假阴性"对我们至关重要：我们要用它在入口挡掉"绝对不存在"的 id。如果它会把
    真实存在的 id 误判成"不在"（假阴性），那真商品就被错杀了——所幸布隆过滤器从结构上
    就不可能假阴性，只会偶尔放过几个本该挡掉的（假阳性），那种漏网的顶多多查一次 DB，无害。
    """

    def __init__(self, redis_client, key: str, size_bits: int, num_hashes: int):
        self.r = redis_client
        self.key = key              # 这条 bitmap 在 Redis 里的 key
        self.m = size_bits          # 位数组总长度 m（越大碰撞越少、假阳性率越低、越费内存）
        self.k = num_hashes         # 哈希函数个数 k（每个元素点亮 k 个 bit）

    def _positions(self, item) -> list[int]:
        """把一个元素映射成 k 个 bit 下标。用'双哈希'技巧从一次哈希派生出 k 个，省去算 k 遍。"""
        data = str(item).encode()
        h = hashlib.md5(data).digest()
        h1 = int.from_bytes(h[:8], "big")    # 取前 8 字节当第一个哈希
        h2 = int.from_bytes(h[8:], "big")    # 取后 8 字节当第二个哈希
        # 经典双哈希组合：第 i 个位置 = (h1 + i*h2) % m，等价于 k 个独立哈希，但只算了一次 md5
        return [(h1 + i * h2) % self.m for i in range(self.k)]

    def add(self, item) -> None:
        """把元素加入集合：点亮它的 k 个 bit。用 pipeline 把 k 次 SETBIT 一次性发出去。"""
        pipe = self.r.pipeline()
        for pos in self._positions(item):
            pipe.setbit(self.key, pos, 1)
        pipe.execute()

    def might_contain(self, item) -> bool:
        """查询：k 个 bit 只要有一个是 0 就【绝对不在】，全 1 才【可能在】。"""
        pipe = self.r.pipeline()
        for pos in self._positions(item):
            pipe.getbit(self.key, pos)
        bits = pipe.execute()
        return all(bits)   # 全 1 → 可能在；有任何 0 → 绝对不在


# 全局布隆过滤器实例。m=8192 bit（1KB）、k=4，对我们这仨商品绰绰有余；
# 真实业务会按"预计元素数 n + 容忍的假阳性率 p"用公式算 m 和 k（文末思考题里点一下）。
BLOOM = RedisBloomFilter(cache.r, key="bloom:product_ids", size_bits=8192, num_hashes=4)


def warm_bloom_filter() -> None:
    """把 DB 里【所有真实存在的 id】灌进布隆过滤器。真实业务通常在启动时/定时全量预热。"""
    cache.r.delete(BLOOM.key)            # 清掉上一次运行残留的 bitmap，从干净状态开始
    for pid in REAL_PRODUCT_IDS:
        BLOOM.add(pid)


# ─────────────────────────────────────────────────────────────────────────────
# 两个版本的业务接口，用来对照"只有空值缓存" vs "空值缓存 + 布隆过滤器"
# ─────────────────────────────────────────────────────────────────────────────
def get_product_null_cache(product_id: int) -> dict | None:
    """
    招式一【单独】上场：Cache-Aside + 缓存空值。Part A / Part B 用它。
    和 V2 的唯一区别：DB 查不到时也往缓存写一个【空值标记】，而不是什么都不存。
    """
    key = cache_key(product_id)

    # ① 先问缓存
    cached = cache.r.get(key)
    if cached is not None:
        if cached == NULL_MARKER:
            return None                # 命中"空值标记"：之前查过，DB 里确实没有 → 直接挡下
        return json.loads(cached)      # 命中真实数据

    # ② 缓存没有，回源查真实 DB（那个 ~200ms 的慢查询）
    product = db.query_product(product_id)

    # ③ 回填缓存——【关键改动】：查到与否都写，只是写的内容和 TTL 不同
    if product is not None:
        cache.r.set(key, json.dumps(product), ex=CACHE_TTL)
    else:
        cache.r.set(key, NULL_MARKER, ex=NULL_TTL)   # 把"查不到"也缓存起来，短 TTL
    return product


def get_product_with_bloom(product_id: int) -> dict | None:
    """
    招式一 + 招式二【叠加】，本版的最终形态。Part C 用它。
    在最前面加一道布隆过滤器闸门：布隆说"绝对不存在"就立刻返回，连 Redis 都不碰。
    """
    # ⓪ 布隆闸门（入口拦截）：绝对不存在 → 直接挡，省掉后面所有开销
    if not BLOOM.might_contain(product_id):
        return None

    # 走到这说明布隆判定"可能存在"（真存在，或极少数假阳性）。后面就是招式一那套：
    key = cache_key(product_id)
    cached = cache.r.get(key)
    if cached is not None:
        if cached == NULL_MARKER:
            return None
        return json.loads(cached)

    product = db.query_product(product_id)
    if product is not None:
        cache.r.set(key, json.dumps(product), ex=CACHE_TTL)
    else:
        cache.r.set(key, NULL_MARKER, ex=NULL_TTL)   # 假阳性漏进来的情况：仍兜底缓存空值
    return product


def _clear_product_keys(ids) -> None:
    """清掉这些 id 的缓存 key（含空值标记），让每个 Part 从干净状态开始。"""
    for pid in ids:
        cache.r.delete(cache_key(pid))


# ─────────────────────────────────────────────────────────────────────────────
# 三个对照实验
# ─────────────────────────────────────────────────────────────────────────────
def part_a_null_cache_same_id():
    """Part A：缓存空值，治"反复刷【同一个】不存在 id"。对比 V2 的 5 次全穿透。"""
    print("=" * 64)
    print("Part A · 缓存空值：反复查【同一个】不存在的 id=9999")
    print("=" * 64)

    missing_id = 9999
    _clear_product_keys([missing_id])
    db.reset_query_count()

    print(f"\n连续 5 次查询不存在的商品 id={missing_id}（用『空值缓存』版本）：")
    for i in range(5):
        result = get_product_null_cache(missing_id)
        print(f"  第 {i + 1} 次 → 返回 {result}，DB 累计被查 {db.get_query_count()} 次")

    print(f"\n  5 次查询，DB 只被打了 {db.get_query_count()} 次（V2 同场景是 5 次）。")
    print(f"  第 1 次回源扑空后，我们把『查不到』写成空值标记 `{NULL_MARKER}`（TTL={NULL_TTL}s），")
    print(f"  后面 4 次都在缓存层第 ① 步被拦下。→ 反复刷同一个不存在 id 的穿透，被堵住了。")


def part_b_null_cache_breaks():
    """Part B：空值缓存的软肋——攻击者每次换【不同的】不存在 id。"""
    print("\n" + "=" * 64)
    print("Part B · 空值缓存的软肋：每次换一个【不同的】不存在 id")
    print("=" * 64)

    # 一串各不相同、且都不存在的 id（模拟攻击者每次换 id 来绕过空值缓存）
    attack_ids = list(range(9000, 9010))   # 9000~9009，共 10 个，全都不在 DB 里
    _clear_product_keys(attack_ids)
    db.reset_query_count()

    print(f"\n依次查询 {len(attack_ids)} 个【互不相同】的不存在 id（仍用『空值缓存』版本）：")
    for pid in attack_ids:
        get_product_null_cache(pid)
    print(f"  DB 被打了 {db.get_query_count()} 次  ← 每个新 id 都是一次全新的 miss，空值缓存拦不住")

    # 这些请求还在 Redis 里留下了一堆没用的空值 key
    junk = sum(1 for pid in attack_ids if cache.r.get(cache_key(pid)) == NULL_MARKER)
    print(f"  顺带：Redis 里多了 {junk} 个垃圾空值 key（攻击者可以拿无穷多 id 把内存撑爆）。")
    print(f"\n  → 空值缓存只能挡『重复的同一个 id』。换 id 攻击 + 撑爆内存，得在【进缓存前】拦。")


def part_c_bloom_blocks():
    """Part C：加上布隆过滤器，在入口挡掉 Part B 那种换 id 攻击。"""
    print("\n" + "=" * 64)
    print("Part C · 布隆过滤器：在入口挡掉『换 id 攻击』")
    print("=" * 64)

    warm_bloom_filter()
    print(f"\n已把真实商品 {REAL_PRODUCT_IDS} 灌入布隆过滤器（key={BLOOM.key}，m={BLOOM.m}bit，k={BLOOM.k}）")

    # 同样那串换 id 攻击，这次走『布隆 + 空值缓存』版本
    attack_ids = list(range(9000, 9010))
    _clear_product_keys(attack_ids)
    db.reset_query_count()

    blocked = 0
    for pid in attack_ids:
        if not BLOOM.might_contain(pid):
            blocked += 1
        get_product_with_bloom(pid)
    print(f"\n查询 {len(attack_ids)} 个互不相同的不存在 id（『布隆 + 空值缓存』版本）：")
    print(f"  被布隆当场挡掉的：{blocked}/{len(attack_ids)}")
    print(f"  DB 被打了 {db.get_query_count()} 次  ← 期望 0：根本没走到查 DB 那步")

    # 真实 id 不能被误挡：布隆必须放行，并能正常拿到数据
    _clear_product_keys([HOT_PRODUCT_ID])
    db.reset_query_count()
    real = get_product_with_bloom(HOT_PRODUCT_ID)
    print(f"\n真实商品 id={HOT_PRODUCT_ID} 是否被误挡？布隆放行 = {BLOOM.might_contain(HOT_PRODUCT_ID)}")
    print(f"  拿到：{real}")
    print(f"  （布隆【无假阴性】：真实存在的一定放行，绝不会被错杀。最多偶尔假阳性放过个别坏 id。）")


def part_d_combined_under_load():
    """Part D：把两招合起来扛一波并发，回到 V1/V2 那个 50 并发的老场景看总账。"""
    print("\n" + "=" * 64)
    print("Part D · 合体压测：50 并发混打『真实热点 + 不存在 id』")
    print("=" * 64)

    warm_bloom_filter()
    _clear_product_keys([HOT_PRODUCT_ID] + list(range(9000, 9010)))
    get_product_with_bloom(HOT_PRODUCT_ID)   # 预热热点缓存（稳态命中，理由见 V2 笔记）
    db.reset_query_count()

    # 一半请求查真实热点商品，一半查随机的不存在 id
    def one_request(i: int):
        pid = HOT_PRODUCT_ID if i % 2 == 0 else 9000 + (i % 10)
        return get_product_with_bloom(pid)

    start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        list(pool.map(one_request, range(CONCURRENCY)))
    elapsed = time.perf_counter() - start

    print(f"\n  {CONCURRENCY} 个并发（真实热点 + 不存在 id 各半），预热后：")
    print(f"  DB 被打了 {db.get_query_count()} 次  ← 热点命中缓存、坏 id 被布隆挡掉，期望 0")
    print(f"  总耗时：{elapsed:.3f}s")


def main():
    part_a_null_cache_same_id()
    part_b_null_cache_breaks()
    part_c_bloom_blocks()
    part_d_combined_under_load()

    print("\n" + "-" * 64)
    print("思考题（带着这些进 V4）：")
    print("1. 空值标记的 TTL 故意设得很短（10s）。如果设很长会怎样？反过来，如果一个 id 先被")
    print("   写了空值、紧接着 DB 里真的插入了这条数据，用户会不会在这 10s 里看到『不存在』？")
    print("2. 布隆过滤器只进不出——商品下架（id 失效）了，bitmap 里那几个 bit 还亮着。这要紧吗？")
    print("   想删一个元素行不行？（提示：直接清 bit 会误伤别人，所以才有了 Counting Bloom 等变体。）")
    print("3. Part A/D 我们都【预热】了一次再放并发。如果一个热点 key 的 TTL 正好到期、缓存瞬间")
    print("   消失，而此刻 1 万个请求同时涌来——它们会一起发现缓存没了、一起冲去查同一条 DB。")
    print("   布隆和空值缓存都拦不住这种（id 是真实存在的），这就是下一版的『缓存击穿』。")


if __name__ == "__main__":
    main()
