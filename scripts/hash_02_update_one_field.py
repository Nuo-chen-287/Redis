"""
Hash 入门 demo 02：改一个字段，String+JSON 有多累，Hash 有多爽

场景：商品卖掉一件，库存 stock 要 -1。
我们用两种存法各改一次，体会差别。

跑法（项目根目录）：
   .venv/bin/python tmp/hash_02_update_one_field.py
"""
import json
import redis

r = redis.Redis(host='localhost', port=6379, db=0, decode_responses=True)

r.delete('product:json', 'product:hash')

# ---------- 存法 A：String + JSON ----------
# 整个对象序列化成一个 JSON 字符串，塞进一个 String
r.set('product:json', json.dumps({'name': '机械键盘', 'price': 299, 'stock': 50}))

print('===== A：String + JSON，库存 -1 =====')
# 想改 stock，必须走「读回来 -> 解析 -> 改 -> 再序列化 -> 写回去」四步
raw = r.get('product:json')          # 1. 把整个对象搬回内存
obj = json.loads(raw)                # 2. 解析成 dict
obj['stock'] -= 1                    # 3. 改其中一个字段
r.set('product:json', json.dumps(obj))  # 4. 整个对象再写回去
print('改完：', r.get('product:json'))


# ---------- 存法 B：Hash ----------
r.hset('product:hash', mapping={'name': '机械键盘', 'price': '299', 'stock': '50'})

print()
print('===== B：Hash，库存 -1 =====')
# 一行：直接对 stock 这个字段做原子 -1，别的字段碰都不碰
r.hincrby('product:hash', 'stock', -1)
print('改完：', r.hgetall('product:hash'))
