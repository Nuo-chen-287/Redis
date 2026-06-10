"""
V5 · 缓存雪崩 + 分布式锁演进（真实 Redis + 真实 MySQL）

从 V4 遗留的两个痛点自然引出：

    痛点一 · 缓存雪崩（Cache Avalanche）
        V4 只是一个热点 key 过期就已经很痛了。现实中更凶残的场景是：
        【成百上千个 key 在同一秒集体过期】—— 比如系统冷启动后批量加载了一波数据，
        给它们全设了相同 TTL=60s。60s 后这批 key 一起消失，所有请求同时涌向 DB，
        把数据库整个打趴。这不是「一把锥子捅穿一个点」（击穿），而是「雪崩」：一整面墙倒。

        解法很朴素：给 TTL 加随机抖动。原来 TTL=60s，现在 TTL=60+random(0,30)s。
        大家的过期时刻被打散到一个时间窗口里，不再同时死。

    痛点二 · V4 互斥锁里埋的 bug
        回忆 V4 Part B 里锁的释放：赢家查完 DB 之后直接 `cache.r.delete(lock_key)`。
        看着没问题，但设想这个时序：

            t0: 请求 A 抢到锁（value="1", TTL=5s），开始查 DB
            t3: DB 特别慢，A 还在等……
            t5: 锁的 5s TTL 到了，Redis 自动删了这把锁
            t5.1: 请求 B 发现锁没了，抢到新锁（value="1", TTL=5s），开始查 DB
            t6: A 终于查完了，执行 delete(lock_key) —— 删掉的是【B 的锁】！
            t6.1: 请求 C 发现锁又没了，抢到新锁……连锁反应，DB 被打穿

        根因：A 删锁时没有验证"这把锁还是不是我的"。

        V5 用三刀逐步修好它，最终形态就是 Java 世界里 Redisson 的 Python 等价实现：

        第一刀 · 锁的 value 放随机 token，删之前先核对是不是自己的
            → 修好了"误删别人锁"，但引入了新问题：GET + DEL 是两条命令，中间有缝隙

        第二刀 · 用 Lua 脚本把"核对 + 删除"合并为一条原子操作
            → 彻底修好误删。但还有一个问题：A 的任务真的要跑 6s，锁只有 5s，怎么办？

        第三刀 · 看门狗（Watchdog）：后台线程周期性给锁续期
            → 只要持锁者还活着、还在干活，锁就不会自动过期。干完了再主动释放。

运行前确保两样都活着：
    .venv/bin/python src/db.py        # 初始化 MySQL（只需一次）
    redis-cli ping                    # 应返回 PONG
然后：
    .venv/bin/python src/v05_avalanche_dist_lock.py
"""

import json
import random
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import cache
import db

# ═════════════════════════════════════════════════════════════════════════════
# Part A · 缓存雪崩：大量 key 同时过期 vs TTL 加随机抖动
# ═════════════════════════════════════════════════════════════════════════════

KEY_PREFIX = "product:"
NUM_PRODUCTS = 3          # 数据库里就 3 条，全用上
PRODUCT_IDS = [1001, 1002, 1003]
CONCURRENCY = 30          # 并发请求数


def cache_key(product_id: int) -> str:
    return f"{KEY_PREFIX}{product_id}"


def get_product_naive(product_id: int) -> dict | None:
    """V2 的朴素读法：miss 就直接查 DB。"""
    key = cache_key(product_id)
    cached = cache.r.get(key)
    if cached is not None:
        return json.loads(cached)
    product = db.query_product(product_id)
    if product is not None:
        cache.r.set(key, json.dumps(product), ex=60)
    return product


def warmup_same_ttl(ttl: int = 60):
    """预热：给所有商品设置【相同 TTL】的缓存 —— 埋下雪崩的种子。"""
    for pid in PRODUCT_IDS:
        product = db.query_product(pid)
        if product:
            cache.r.set(cache_key(pid), json.dumps(product), ex=ttl)


def warmup_random_ttl(base_ttl: int = 60, jitter: int = 30):
    """预热：TTL = base + random(0, jitter)，过期时刻被打散。"""
    for pid in PRODUCT_IDS:
        product = db.query_product(pid)
        if product:
            actual_ttl = base_ttl + random.randint(0, jitter)
            cache.r.set(cache_key(pid), json.dumps(product), ex=actual_ttl)


def simulate_avalanche(label: str):
    """模拟所有缓存同时消失后的并发冲击。"""
    for pid in PRODUCT_IDS:
        cache.r.delete(cache_key(pid))
    db.reset_query_count()

    def random_query(_):
        pid = random.choice(PRODUCT_IDS)
        get_product_naive(pid)

    start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        list(pool.map(random_query, range(CONCURRENCY)))
    elapsed = time.perf_counter() - start

    print(f"  [{label}] {CONCURRENCY} 并发查 {NUM_PRODUCTS} 个商品，DB 被打了 {db.get_query_count()} 次，耗时 {elapsed:.3f}s")


def part_a():
    print("=" * 72)
    print("Part A · 缓存雪崩：大量 key 同时过期 vs TTL 加随机抖动")
    print("=" * 72)

    print("\n  场景复现：3 个商品全设 TTL=60s，假设此刻它们【同时过期】——")
    simulate_avalanche("同时过期，无保护")

    print("\n  解法：TTL 加随机抖动（base=60s + random 0~30s）")
    print("  这样 3 个 key 的过期时刻被打散到 60s~90s 的窗口里，不再同时消失。")
    print("  下面演示打散后，如果只有其中 1 个过期（另外 2 个还活着）——")
    warmup_random_ttl()
    cache.r.delete(cache_key(PRODUCT_IDS[0]))   # 只让 1 个过期
    db.reset_query_count()

    def random_query(_):
        pid = random.choice(PRODUCT_IDS)
        get_product_naive(pid)

    start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        list(pool.map(random_query, range(CONCURRENCY)))
    elapsed = time.perf_counter() - start

    print(f"  [只 1 个过期] {CONCURRENCY} 并发，DB 被打了 {db.get_query_count()} 次，耗时 {elapsed:.3f}s")
    print(f"  → DB 压力从「被全面碾压」降到「只被一个 key 的 miss 打几下」，系统扛得住。")

    print("\n  雪崩的解法本质就一句话：")
    print("    TTL = 固定基础值 + random(0, 抖动范围)")
    print("  让大量 key 的过期时刻【均匀散开】，而不是约好同一秒一起死。")


# ═════════════════════════════════════════════════════════════════════════════
# Part B · 分布式锁演进：从 V4 的 bug 到工业级
# ═════════════════════════════════════════════════════════════════════════════

LOCK_KEY = "lock:product:1001"
LOCK_TTL = 5   # 锁的自动过期时间（秒）


# ─────────────────────────────────────────────────────────────────────────────
# 第一刀：token 验证 —— 删之前先看是不是自己的锁
# ─────────────────────────────────────────────────────────────────────────────

def lock_v1_acquire(lock_key: str, ttl: int) -> str | None:
    """抢锁。value 存一个随机 token（UUID），成功返回 token，失败返回 None。"""
    token = str(uuid.uuid4())
    if cache.r.set(lock_key, token, nx=True, ex=ttl):
        return token
    return None


def lock_v1_release(lock_key: str, token: str) -> bool:
    """释放锁：先 GET 验证 value 是否是自己的 token，是才 DEL。"""
    current = cache.r.get(lock_key)
    if current == token:
        cache.r.delete(lock_key)
        return True
    return False    # 不是自己的锁（已被别人抢走），不删


# ─────────────────────────────────────────────────────────────────────────────
# 第二刀：Lua 原子释放 —— GET + DEL 合并为一条原子命令
# ─────────────────────────────────────────────────────────────────────────────

# 为什么需要 Lua？因为 lock_v1_release 里的 GET 和 DEL 是两条独立命令，
# 在它们之间，有极小概率出现这个时序：
#     t0: A 执行 GET → 看到 token 是自己的 → 准备 DEL
#     t0+ε: 锁刚好 TTL 到期，Redis 自动删了
#     t0+2ε: B 抢到新锁
#     t0+3ε: A 的 DEL 执行 → 删掉的是 B 的锁！
# Lua 脚本在 Redis 服务端原子执行，整个"核对 + 删除"中间不会被插入任何其他命令。

RELEASE_LUA = """
if redis.call("GET", KEYS[1]) == ARGV[1] then
    return redis.call("DEL", KEYS[1])
else
    return 0
end
"""

_release_script = cache.r.register_script(RELEASE_LUA)


def lock_v2_acquire(lock_key: str, ttl: int) -> str | None:
    """抢锁（和 v1 一样，只是命名区分）。"""
    token = str(uuid.uuid4())
    if cache.r.set(lock_key, token, nx=True, ex=ttl):
        return token
    return None


def lock_v2_release(lock_key: str, token: str) -> bool:
    """Lua 原子释放：核对 token + 删除，一气呵成，中间不可能被插队。"""
    result = _release_script(keys=[lock_key], args=[token])
    return result == 1


# ─────────────────────────────────────────────────────────────────────────────
# 第三刀：看门狗（Watchdog）—— 持锁期间自动续期，任务完成后才释放
# ─────────────────────────────────────────────────────────────────────────────

# 问题场景：锁 TTL=5s，但 DB 查询需要 6s。到第 5s 锁自动没了，别人冲进来。
# 看门狗解法：开一个后台线程，每隔 TTL/3 检查一次"我是否还持有锁"，
# 如果是，就给锁续期 TTL —— 只要我还活着还在干活，锁就不会到期。
# 任务做完后主动释放锁，看门狗自动停止。

class DistributedLock:
    """带看门狗自动续期的分布式锁 —— Python 版 Redisson 核心逻辑。"""

    def __init__(self, lock_key: str, ttl: int = 10):
        self._lock_key = lock_key
        self._ttl = ttl
        self._token = str(uuid.uuid4())
        self._watchdog_stop = threading.Event()
        self._watchdog_thread: threading.Thread | None = None

    def acquire(self, timeout: float = 5.0) -> bool:
        """尝试获取锁。timeout 秒内反复尝试，超时返回 False。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if cache.r.set(self._lock_key, self._token, nx=True, ex=self._ttl):
                self._start_watchdog()
                return True
            time.sleep(0.05)
        return False

    def release(self) -> bool:
        """释放锁：先停看门狗，再 Lua 原子删。"""
        self._stop_watchdog()
        result = _release_script(keys=[self._lock_key], args=[self._token])
        return result == 1

    def _start_watchdog(self):
        """启动看门狗线程：每 TTL/3 续期一次。"""
        self._watchdog_stop.clear()
        self._watchdog_thread = threading.Thread(target=self._watchdog_loop, daemon=True)
        self._watchdog_thread.start()

    def _stop_watchdog(self):
        """通知看门狗停止。"""
        self._watchdog_stop.set()
        if self._watchdog_thread:
            self._watchdog_thread.join(timeout=2)

    def _watchdog_loop(self):
        """看门狗主循环：只要持锁者还活着，就反复续期。"""
        interval = self._ttl / 3    # 比如 TTL=10s，每 3.3s 续一次
        while not self._watchdog_stop.is_set():
            self._watchdog_stop.wait(timeout=interval)
            if self._watchdog_stop.is_set():
                break
            # 续期前先核对：锁还是不是我的（可能在极端情况下已经丢了）
            current = cache.r.get(self._lock_key)
            if current == self._token:
                cache.r.expire(self._lock_key, self._ttl)

    def __enter__(self):
        if not self.acquire():
            raise TimeoutError(f"获取锁 {self._lock_key} 超时")
        return self

    def __exit__(self, *_):
        self.release()


# ─────────────────────────────────────────────────────────────────────────────
# Part B 演示
# ─────────────────────────────────────────────────────────────────────────────

def demo_v4_bug():
    """复现 V4 的 bug：赢家执行慢，锁自动过期后被别人抢走，赢家查完误删别人的锁。"""
    print("\n  --- 复现 V4 的 bug：误删别人的锁 ---")

    cache.r.delete(LOCK_KEY)
    lock_ttl = 1  # 故意设很短，容易触发

    # A 抢到锁
    got = cache.r.set(LOCK_KEY, "1", nx=True, ex=lock_ttl)
    print(f"  t0: A 抢锁 → {'成功' if got else '失败'}")

    # A 的任务很慢，锁到期自动消失
    time.sleep(lock_ttl + 0.1)
    print(f"  t1: A 还在干活……但锁已经自动过期了（TTL={lock_ttl}s 到了）")

    # B 趁机抢到
    got_b = cache.r.set(LOCK_KEY, "1", nx=True, ex=lock_ttl)
    print(f"  t2: B 趁机抢锁 → {'成功' if got_b else '失败'}")

    # A 做完了，直接 DEL —— 删掉的是 B 的锁！
    cache.r.delete(LOCK_KEY)
    remaining = cache.r.get(LOCK_KEY)
    print(f"  t3: A 做完，直接 DEL → B 的锁被误删了！（当前锁状态: {remaining}）")
    print(f"  → 这就是 V4 的 bug：A 删的不是自己的锁，而是 B 的。")


def demo_first_fix():
    """第一刀：token 验证。A 删之前发现 token 不是自己的，拒绝删除。"""
    print("\n  --- 第一刀：token 验证 ---")

    cache.r.delete(LOCK_KEY)
    lock_ttl = 1

    token_a = lock_v1_acquire(LOCK_KEY, lock_ttl)
    print(f"  t0: A 抢锁成功，token={token_a[:8]}...")

    time.sleep(lock_ttl + 0.1)
    print(f"  t1: 锁自动过期")

    token_b = lock_v1_acquire(LOCK_KEY, lock_ttl)
    print(f"  t2: B 抢锁成功，token={token_b[:8]}...")

    released = lock_v1_release(LOCK_KEY, token_a)
    print(f"  t3: A 尝试释放（用自己的 token 核对）→ {'释放成功' if released else '拒绝释放（不是自己的锁）'}")

    b_still_holds = cache.r.get(LOCK_KEY) == token_b
    print(f"  → B 的锁还在: {b_still_holds}")
    print(f"  ⚠️ 但 GET + DEL 不是原子的，极端时序下仍可能误删（见第二刀）")


def demo_lua_atomic():
    """第二刀：Lua 原子释放。"""
    print("\n  --- 第二刀：Lua 原子释放 ---")

    cache.r.delete(LOCK_KEY)

    token = lock_v2_acquire(LOCK_KEY, 10)
    print(f"  抢锁成功，token={token[:8]}...")

    # 用正确 token 释放
    ok = lock_v2_release(LOCK_KEY, token)
    print(f"  用自己的 token 释放 → {ok}")

    # 尝试用错误 token 释放
    token2 = lock_v2_acquire(LOCK_KEY, 10)
    fake = lock_v2_release(LOCK_KEY, "fake-token")
    print(f"  用错误 token 释放 → {fake}（锁安全，不会被误删）")
    print(f"  → 核对+删除在 Redis 服务端一条 Lua 完成，中间不可能被插队。")

    cache.r.delete(LOCK_KEY)


def demo_watchdog():
    """第三刀：看门狗续期。任务跑 4s，锁 TTL 只有 2s，但看门狗让锁不会提前消失。"""
    print("\n  --- 第三刀：看门狗自动续期 ---")

    cache.r.delete(LOCK_KEY)

    task_duration = 4   # 任务要跑 4 秒
    lock_ttl = 2        # 锁只有 2 秒 —— 没有看门狗的话，任务还没做完锁就没了

    print(f"  任务需要 {task_duration}s，锁 TTL 只有 {lock_ttl}s")
    print(f"  没有看门狗 → 第 {lock_ttl}s 锁就自动没了，别人会冲进来")
    print(f"  有看门狗 → 每 {lock_ttl/3:.1f}s 续期一次，锁跟着任务一起活")

    lock = DistributedLock(LOCK_KEY, ttl=lock_ttl)
    lock.acquire()
    print(f"\n  t0: 获取锁成功（token={lock._token[:8]}...）")

    for i in range(task_duration):
        time.sleep(1)
        ttl_remaining = cache.r.ttl(LOCK_KEY)
        holder = cache.r.get(LOCK_KEY)
        still_mine = holder == lock._token
        print(f"  t{i+1}: 任务进行中... 锁剩余TTL={ttl_remaining}s, 仍是我的={still_mine}")

    lock.release()
    print(f"  t{task_duration}: 任务完成，主动释放锁")
    print(f"  锁状态: {cache.r.get(LOCK_KEY)}（已释放）")
    print(f"\n  → 看门狗让锁的生命周期跟着任务走：任务没完，锁就不死；任务一完，锁立刻放。")


def part_b():
    print("\n\n" + "=" * 72)
    print("Part B · 分布式锁演进：从 V4 的 bug 到工业级（三刀修好）")
    print("=" * 72)

    print("""
  回顾 V4 的互斥锁：抢锁用 SET NX，释放用 DEL。简单直接，但有个致命 bug——
  如果持锁者的任务执行时间超过了锁的 TTL，锁自动过期后被别人抢走，
  而第一个持锁者做完后直接 DEL，删掉的是别人的锁。

  V5 用三刀逐步修好它：""")

    demo_v4_bug()
    demo_first_fix()
    demo_lua_atomic()
    demo_watchdog()


# ═════════════════════════════════════════════════════════════════════════════
# Part C · 综合演示：用带看门狗的分布式锁解决缓存击穿（V4 的升级版）
# ═════════════════════════════════════════════════════════════════════════════

def get_product_with_dist_lock(product_id: int) -> dict | None:
    """V4 互斥锁的升级版：用 DistributedLock（带看门狗）替换朴素的 SET NX + DEL。"""
    key = cache_key(product_id)

    cached = cache.r.get(key)
    if cached is not None:
        return json.loads(cached)

    lock = DistributedLock(f"lock:{key}", ttl=5)
    if lock.acquire(timeout=3):
        try:
            # double-check
            cached = cache.r.get(key)
            if cached is not None:
                return json.loads(cached)
            product = db.query_product(product_id)
            if product is not None:
                ttl = 60 + random.randint(0, 30)    # 顺手加上随机抖动，防雪崩
                cache.r.set(key, json.dumps(product), ex=ttl)
            return product
        finally:
            lock.release()
    else:
        # 等缓存出现
        for _ in range(100):
            time.sleep(0.02)
            cached = cache.r.get(key)
            if cached is not None:
                return json.loads(cached)
        return db.query_product(product_id)


def part_c():
    print("\n\n" + "=" * 72)
    print("Part C · 综合：带看门狗的分布式锁 + TTL 随机抖动（V4 升级版）")
    print("=" * 72)

    HOT = 1001
    cache.r.delete(cache_key(HOT))
    cache.r.delete(f"lock:{cache_key(HOT)}")
    db.reset_query_count()

    def one_request(_):
        t0 = time.perf_counter()
        get_product_with_dist_lock(HOT)
        return time.perf_counter() - t0

    start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=50) as pool:
        latencies = list(pool.map(one_request, range(50)))
    elapsed = time.perf_counter() - start

    print(f"\n  50 并发查热点 id={HOT}（缓存已清空，模拟击穿瞬间）：")
    print(f"  DB 被打了 {db.get_query_count()} 次（期望 1）")
    print(f"  最慢 {max(latencies)*1000:.1f}ms · 最快 {min(latencies)*1000:.1f}ms · 平均 {sum(latencies)/len(latencies)*1000:.1f}ms")
    print(f"  总墙钟 {elapsed:.3f}s")
    print(f"\n  对比 V4 版本：")
    print(f"  · 不会误删别人的锁（token + Lua 保证）")
    print(f"  · 任务再慢也不怕锁过期（看门狗自动续期）")
    print(f"  · TTL 带随机抖动，避免大量 key 同时过期引发雪崩")


def main():
    part_a()
    part_b()
    part_c()

    print("\n\n" + "-" * 72)
    print("V5 收口：")
    print("-" * 72)
    print("""
  缓存雪崩：
    · 痛点：大量 key 同时过期，DB 被全面碾压
    · 解法：TTL = 固定值 + random(0, 抖动范围)

  分布式锁三刀演进：
    · V4 的 bug：直接 DEL 可能误删别人的锁
    · 第一刀：value 存随机 token，删前核对身份
    · 第二刀：GET + DEL 不原子 → Lua 脚本合并
    · 第三刀：看门狗续期 → 锁跟着任务活，不会提前消失

  最终形态（DistributedLock 类）= Java 世界里 Redisson 的核心逻辑：
    · acquire: SET NX + 启动看门狗
    · release: 停看门狗 + Lua 原子释放
    · 看门狗: 每 TTL/3 续期，持锁者活着锁就不死
""")


if __name__ == "__main__":
    main()
