"""
List 入门 demo 01：List 长什么样 + 当队列用

一句话：List = 一个 key 底下挂「一排有顺序的元素」，像现实里排队的一列人。
   String 是  key -> 一坨值
   Hash  是  key -> { 字段: 值, ... }      一个小字典
   List  是  key -> [元素, 元素, 元素, ...]  一排有先后顺序的值

场景钩子：接上「发货走 MQ」。
   订单系统不停往队列「右边」塞发货任务（RPUSH，生产者）；
   发货服务从队列「左边」一个个取出来处理（LPOP，消费者）。
   右进左出 = 先塞进去的先被处理 = FIFO 先进先出 = 排队的公平顺序。

跑法（项目根目录）：
   .venv/bin/python tmp/list_01_basics.py
"""
import redis

r = redis.Redis(host='localhost', port=6379, db=0, decode_responses=True)

QUEUE = 'queue:ship'        # 发货任务队列
r.delete(QUEUE)             # 清掉上次残留，保证干净

# ── 生产者：订单系统把 3 个发货任务从「右边」塞进队列 ────────────────
# RPUSH = Right PUSH，从右端推入。按 1→2→3 的顺序塞。
r.rpush(QUEUE, '发货任务#1-订单A')
r.rpush(QUEUE, '发货任务#2-订单B')
r.rpush(QUEUE, '发货任务#3-订单C')

print('=== LRANGE 0 -1：从头到尾看一眼整条队列长啥样 ===')
print(r.lrange(QUEUE, 0, -1))         # 0 到 -1 = 第一个到最后一个 = 全部

print()
print('=== LLEN：队列里现在排着几个任务 ===')
print(r.llen(QUEUE))

print()
print('=== 发货服务上班，从「左边」一个个取出来处理（LPOP）===')
# LPOP = Left POP，从左端弹出并删除。注意取出的顺序：
print('取出 →', r.lpop(QUEUE))        # 谁先被处理？
print('取出 →', r.lpop(QUEUE))
print('取出 →', r.lpop(QUEUE))

print()
print('=== 全取完后，队列还剩啥 ===')
print(r.lrange(QUEUE, 0, -1))         # 空了

print()
print('=== TYPE：在 Redis 眼里这个 key 的类型 ===')
print(r.type(QUEUE))

r.delete(QUEUE)   # 收尾，清掉造的 key
