"""
Hash 入门 demo 04：存海量小对象，Hash 比「一堆独立 String」省内存

场景：存 1000 个用户，每人 5 个字段（name/age/city/email/level）。
   A：每人一个 Hash       → 1000 个 key
   B：每个字段拆成 String → 5000 个 key
比一比两种存法各占多少内存。

用 MEMORY USAGE <key> 量单个 key 占的字节，加起来对比。
跑完会自动清掉自己造的 key，不污染你的库。

跑法（项目根目录）：
   .venv/bin/python tmp/hash_04_memory.py
"""
import redis

r = redis.Redis(host='localhost', port=6379, db=0, decode_responses=True)

N = 1000
fields = {'name': '张三', 'age': '28', 'city': '北京',
          'email': 'zhangsan@example.com', 'level': '3'}

# 先清掉上次可能残留的测试 key
for i in range(N):
    r.delete(f'demo:userhash:{i}')
    for f in fields:
        r.delete(f'demo:userstr:{i}:{f}')

# ---------- A：每人一个 Hash ----------
mem_hash = 0
for i in range(N):
    key = f'demo:userhash:{i}'
    r.hset(key, mapping=fields)
    mem_hash += r.memory_usage(key)

# ---------- B：每个字段一个独立 String ----------
mem_str = 0
for i in range(N):
    for f, v in fields.items():
        key = f'demo:userstr:{i}:{f}'
        r.set(key, v)
        mem_str += r.memory_usage(key)

print(f'A  Hash（{N} 个 key）       : {mem_hash:>10} 字节  ≈ {mem_hash/1024:.1f} KB')
print(f'B  String（{N*len(fields)} 个 key）: {mem_str:>10} 字节  ≈ {mem_str/1024:.1f} KB')
print(f'B 比 A 多用了 {mem_str/mem_hash:.1f} 倍内存')

# 清理：把自己造的测试 key 全删掉
for i in range(N):
    r.delete(f'demo:userhash:{i}')
    for f in fields:
        r.delete(f'demo:userstr:{i}:{f}')
print('（测试 key 已清理）')
