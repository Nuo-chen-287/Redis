"""
Hash 入门 demo 03：HINCRBY 是原子的，String+JSON 的「读-改-写」不是

场景：库存 100，100 个人「同时」下单，每人扣 1。正确结果必须是 0。
我们开 100 个并发线程，分别用两种方式扣库存，看谁能扣对。

跑法（项目根目录）：
   .venv/bin/python tmp/hash_03_atomic.py
"""
import json
import time
import threading
import redis

r = redis.Redis(host='localhost', port=6379, db=0, decode_responses=True)


# ---------- 方式 A：String + JSON 的「读-改-写」 ----------
def buy_json():
    # 1. 读回整个对象
    obj = json.loads(r.get('product:json'))
    # 2. 在 Python 里改（这中间有个时间缝隙！）
    obj['stock'] -= 1
    time.sleep(0.001)   # 故意放大那个缝隙，模拟真实网络/计算耗时
    # 3. 写回去
    r.set('product:json', json.dumps(obj))


# ---------- 方式 B：Hash 的 HINCRBY ----------
def buy_hash():
    # 「读-改-写」三步被 Redis 压成一个不可打断的原子动作
    r.hincrby('product:hash', 'stock', -1)


def run(label, init_stock, work, read_stock):
    # 重置库存为 100
    init_stock()
    threads = [threading.Thread(target=work) for _ in range(100)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    print(f'{label}：100 人各扣 1 后，剩余库存 = {read_stock()}（正确应为 0）')


print('===== A：String + JSON 读-改-写 =====')
run(
    'A',
    lambda: r.set('product:json', json.dumps({'stock': 100})),
    buy_json,
    lambda: json.loads(r.get('product:json'))['stock'],
)

print()
print('===== B：Hash HINCRBY =====')
run(
    'B',
    lambda: r.hset('product:hash', 'stock', 100),
    buy_hash,
    lambda: r.hget('product:hash', 'stock'),
)
