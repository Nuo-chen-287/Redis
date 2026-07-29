"""
V8 · 缓存一致性（真实 Redis + 真实 MySQL）

Part A 只更新 MySQL，故意制造脏缓存；Part B 补齐标准 Cache-Aside 写路径；
Part C 再复现旧读请求晚回填和删除失败，分别用延迟双删与有限重试收口。

运行前：
    .venv/bin/python src/db.py
    redis-cli ping
然后：
    .venv/bin/python src/v08_cache_consistency.py

观察重点：
    1. MySQL 已经变成新价格后，业务读取为什么仍返回旧价格？
    2. 脏数据会持续多久？TTL 到期后为什么又自动一致了？
    3. 更新 DB 后删除缓存，下一次读取是否立即拿到新价格？
    4. 旧读请求为什么能在第一次 DEL 后把旧值写回 Redis？
    5. 本地重试能处理短暂故障，为什么仍不能覆盖应用进程崩溃？
"""

import json
import threading
import time

import cache
import db
import redis

PRODUCT_ID = 1001
CACHE_TTL = 4  # 实验只等 4 秒；生产 TTL 往往更长，脏数据窗口也会随之变长。
DELAYED_DELETE_SECONDS = 0.3
KEY_PREFIX = "consistency:product:"


def cache_key(product_id: int) -> str:
    return f"{KEY_PREFIX}{product_id}"


def get_product(product_id: int) -> dict | None:
    """沿用 V2 的 Cache-Aside 读路径。"""
    key = cache_key(product_id)
    cached = cache.r.get(key)
    if cached is not None:
        return json.loads(cached)

    product = db.query_product(product_id)
    if product is not None:
        cache.r.set(key, json.dumps(product), ex=CACHE_TTL)
    return product


def update_price_db_only(product_id: int, new_price: int) -> None:
    """Part A 故意只改权威数据源，不碰缓存。"""
    db.update_product_price(product_id, new_price)


def update_price_db_then_delete(product_id: int, new_price: int) -> None:
    """Part B：先提交权威数据，再删除可以重新生成的缓存副本。"""
    db.update_product_price(product_id, new_price)
    cache.r.delete(cache_key(product_id))


def wait_for_cache_expiry(key: str) -> None:
    while cache.r.exists(key):
        time.sleep(0.05)


def read_then_fill_late(
    product_id: int,
    db_read_done: threading.Event,
    allow_cache_fill: threading.Event,
    outcome: dict,
) -> None:
    """先读到旧 DB，再暂停到写请求删完缓存后才回填。"""
    try:
        product = db.query_product(product_id)
        outcome["product"] = product
        db_read_done.set()
        if not allow_cache_fill.wait(timeout=5):
            raise TimeoutError("等待晚回填信号超时")
        if product is not None:
            cache.r.set(cache_key(product_id), json.dumps(product), ex=CACHE_TTL)
    except Exception as exc:
        outcome["error"] = exc
        db_read_done.set()


def delete_cache_with_retry(key: str, delete_fn=cache.r.delete, attempts: int = 3) -> int:
    """处理短暂 Redis 故障；返回实际执行到第几次才成功。"""
    for attempt in range(1, attempts + 1):
        try:
            delete_fn(key)
            return attempt
        except redis.RedisError:
            if attempt == attempts:
                raise
            time.sleep(0.05)
    raise AssertionError("unreachable")


def part_a_db_only() -> None:
    original = db.query_product(PRODUCT_ID)
    if original is None:
        raise RuntimeError(f"商品 {PRODUCT_ID} 不存在，请先运行 .venv/bin/python src/db.py")

    key = cache_key(PRODUCT_ID)
    old_price = original["price"]
    new_price = old_price + 100

    try:
        cache.r.delete(key)
        db.reset_query_count()

        print("=" * 68)
        print("阶段 1 · 预热缓存：MySQL 与 Redis 目前一致")
        print("=" * 68)
        warmed = get_product(PRODUCT_ID)
        print(f"MySQL 商品价格      -> {old_price}")
        print(f"首次业务读取        -> {warmed['price']}（cache miss，回源并写入 Redis）")
        print(f"Redis TTL           -> {cache.r.ttl(key)} 秒")
        print(f"DB 查询次数         -> {db.get_query_count()}")

        print("\n" + "=" * 68)
        print("阶段 2 · 只更新 MySQL：缓存开始变脏")
        print("=" * 68)
        update_price_db_only(PRODUCT_ID, new_price)
        authoritative = db.query_product(PRODUCT_ID)
        stale = get_product(PRODUCT_ID)
        print(f"MySQL 最新价格      -> {authoritative['price']}")
        print(f"业务接口读取        -> {stale['price']}（cache hit，仍是旧值）")
        print(f"Redis 剩余 TTL      -> {cache.r.ttl(key)} 秒")
        print(f"DB 查询次数         -> {db.get_query_count()}（业务读取命中缓存，没有回源）")
        assert authoritative["price"] == new_price
        assert stale["price"] == old_price

        print("\n" + "=" * 68)
        print("阶段 3 · 等 TTL 到期：下一次读取才恢复一致")
        print("=" * 68)
        wait_for_cache_expiry(key)
        fresh = get_product(PRODUCT_ID)
        print(f"缓存到期后的读取    -> {fresh['price']}（cache miss，重新查询 MySQL）")
        print(f"DB 查询次数         -> {db.get_query_count()}")
        assert fresh["price"] == new_price

        print("\n结论：只更新 DB 只能依赖 TTL 最终一致；TTL 有多长，旧值最多就可能暴露多久。")
    finally:
        db.update_product_price(PRODUCT_ID, old_price)
        cache.r.delete(key)
        print(f"实验清理：MySQL 价格已恢复为 {old_price}，实验缓存已删除。")

def part_b_db_then_delete() -> None:
    original = db.query_product(PRODUCT_ID)
    if original is None:
        raise RuntimeError(f"商品 {PRODUCT_ID} 不存在，请先运行 .venv/bin/python src/db.py")

    key = cache_key(PRODUCT_ID)
    old_price = original["price"]
    new_price = old_price + 100

    try:
        cache.r.delete(key)
        warmed = get_product(PRODUCT_ID)
        db.reset_query_count()

        print("\n" + "#" * 68)
        print("Part B · 更新 MySQL，再删除 Redis")
        print("#" * 68)
        print(f"更新前业务读取      -> {warmed['price']}（旧值已在 Redis）")
        print(f"更新前缓存存在      -> {cache.r.exists(key)}")

        update_price_db_then_delete(PRODUCT_ID, new_price)
        print(f"MySQL 已更新        -> {new_price}")
        print(f"更新后缓存存在      -> {cache.r.exists(key)}（DEL 已移除旧副本）")

        first = get_product(PRODUCT_ID)
        first_query_count = db.get_query_count()
        second = get_product(PRODUCT_ID)
        print(f"更新后第一次读取    -> {first['price']}（cache miss，回源新值）")
        print(f"更新后第二次读取    -> {second['price']}（cache hit）")
        print(f"两次读取的 DB 查询数 -> {db.get_query_count()}（只有第一次回源）")
        assert first["price"] == new_price
        assert second["price"] == new_price
        assert first_query_count == 1
        assert db.get_query_count() == 1

        print("\n结论：先更新 DB 再删除缓存，把脏数据窗口缩短到了下一次读取重建缓存之前。")
    finally:
        db.update_product_price(PRODUCT_ID, old_price)
        cache.r.delete(key)
        print(f"实验清理：MySQL 价格已恢复为 {old_price}，实验缓存已删除。")

    print("\n思考题（带着这些进 Part C）：")
    print("1. 如果一个读请求更早查到了旧 DB，却在 DEL 之后才把旧值写入缓存，会发生什么？")
    print("2. 延迟双删中的第二次 DEL，为什么要故意等一小段时间？")
    print("3. 如果 Redis 故障导致两次 DEL 都失败，系统怎样自动恢复？")


def part_c_race_and_retry() -> None:
    original = db.query_product(PRODUCT_ID)
    if original is None:
        raise RuntimeError(f"商品 {PRODUCT_ID} 不存在，请先运行 .venv/bin/python src/db.py")

    key = cache_key(PRODUCT_ID)
    old_price = original["price"]
    new_price = old_price + 100

    try:
        print("\n" + "#" * 68)
        print("Part C · 延迟双删 + 删除失败重试")
        print("#" * 68)
        cache.r.delete(key)

        db_read_done = threading.Event()
        allow_cache_fill = threading.Event()
        outcome: dict = {}
        reader = threading.Thread(
            target=read_then_fill_late,
            args=(PRODUCT_ID, db_read_done, allow_cache_fill, outcome),
        )
        reader.start()
        if not db_read_done.wait(timeout=5):
            raise TimeoutError("读请求没有完成 DB 查询")
        if "error" in outcome:
            raise outcome["error"]

        print(f"慢读请求已从 MySQL 拿到 -> {outcome['product']['price']}，暂不回填")
        update_price_db_then_delete(PRODUCT_ID, new_price)
        print(f"写请求更新 MySQL 为 {new_price}，并完成第一次 DEL")
        print(f"第一次 DEL 后缓存存在 -> {cache.r.exists(key)}")

        allow_cache_fill.set()
        reader.join(timeout=5)
        if reader.is_alive():
            raise TimeoutError("读请求没有完成晚回填")
        if "error" in outcome:
            raise outcome["error"]

        resurrected = json.loads(cache.r.get(key))
        print(f"慢读请求随后回填 Redis -> {resurrected['price']}（旧值在 DEL 后复活）")
        assert resurrected["price"] == old_price

        # ponytail: 阻塞等待只为固定演示顺序；生产环境应异步调度第二次删除。
        time.sleep(DELAYED_DELETE_SECONDS)
        cache.r.delete(key)
        fresh = get_product(PRODUCT_ID)
        print(f"延迟 {DELAYED_DELETE_SECONDS:.1f}s 第二次 DEL 后读取 -> {fresh['price']}")
        assert fresh["price"] == new_price

        print("\n删除失败实验：第一次 DEL 模拟连接中断，第二次重试成功")
        db.update_product_price(PRODUCT_ID, old_price)
        cache.r.delete(key)
        get_product(PRODUCT_ID)  # 重新准备一份 old_price 的旧缓存。
        db.update_product_price(PRODUCT_ID, new_price)

        calls = 0

        def fail_once_then_delete(cache_key_: str) -> int:
            nonlocal calls
            calls += 1
            if calls == 1:
                print("第 1 次 DEL          -> 模拟 Redis 连接失败")
                raise redis.ConnectionError("simulated connection failure")
            print("第 2 次 DEL          -> 重试成功")
            return cache.r.delete(cache_key_)

        # ponytail: 本地重试随进程崩溃而丢失；不能容忍时升级到 outbox、MQ 或 CDC。
        succeeded_on = delete_cache_with_retry(key, delete_fn=fail_once_then_delete)
        fresh_after_retry = get_product(PRODUCT_ID)
        print(f"删除在第 {succeeded_on} 次成功，随后读取 -> {fresh_after_retry['price']}")
        assert succeeded_on == 2
        assert fresh_after_retry["price"] == new_price

        print("\nV8 收口：主方案是更新 DB 后删缓存；延迟双删缩小并发窗口；重试处理短暂失败。")
        print("若不能接受进程崩溃导致失效任务丢失，需要把任务持久化到 outbox/MQ，或订阅 binlog。")
    finally:
        db.update_product_price(PRODUCT_ID, old_price)
        cache.r.delete(key)
        print(f"实验清理：MySQL 价格已恢复为 {old_price}，实验缓存已删除。")


def main() -> None:
    part_a_db_only()
    part_b_db_then_delete()
    part_c_race_and_retry()


if __name__ == "__main__":
    main()
