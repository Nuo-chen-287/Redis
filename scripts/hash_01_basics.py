"""
Hash 入门 demo 01：Hash 长什么样

一句话：Hash = 一个 key 底下挂「多个 字段=值」，像一个小字典 / 一行表记录。
   String 是  key -> 一坨值
   Hash  是  key -> { 字段1: 值1, 字段2: 值2, ... }

跑法（项目根目录）：
   .venv/bin/python tmp/hash_01_basics.py
"""
import redis

r = redis.Redis(host='localhost', port=6379, db=0, decode_responses=True)

# 清掉上次可能残留的 key，保证每次跑结果干净
r.delete('product:1001')

# HSET：把一个商品对象存进去，一个 key 底下挂多个 field=value
r.hset('product:1001', mapping={
    'name': '机械键盘',
    'price': '299',
    'stock': '50',
})

print('=== HGETALL：把整个对象一次取回来（返回一个 dict）===')
print(r.hgetall('product:1001'))

print()
print('=== HGET：只取其中一个字段 name（不用把整个对象搬出来）===')
print(r.hget('product:1001', 'name'))

print()
print('=== TYPE：在 Redis 眼里这个 key 的类型 ===')
print(r.type('product:1001'))
