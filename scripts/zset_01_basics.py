"""
zset_01_basics.py · ZSet 长什么样:成员 + 分数,Redis 自动按分数排序

心智模型(接着上次的五类型对比):
   String = 一坨值
   Hash   = 小字典
   List   = 排队的人(有序、可重复)
   Set    = 一个班的学号(无序、不重复)
   ZSet   = 带分数的排行榜(不重复 + 按分数自动排序)  ← 就是它

一句话:ZSet = 带分数的 Set。
   - 继承 Set:成员唯一,不重复
   - 多出来的:每个成员绑一个分数 score,Redis 永远帮你按 score 排好

本 demo 只演示一件事:塞乱序的分数进去,Redis 吐出来自动是排好序的。

命令(缩写先拆开记):
   ZADD       = Z ADD              塞「分数 + 成员」进去
   ZRANGE     = Z RANGE            按分数【升序】取一段(从低到高)
   ZREVRANGE  = Z REVerse RANGE    按分数【降序】取一段(从高到低)← 排行榜常用
   ZCARD      = Z CARDinality      有几个成员(= size)

跑法(项目根目录):
   .venv/bin/python tmp/zset_01_basics.py
"""
import redis

r = redis.Redis(host='localhost', port=6379, db=0, decode_responses=True)

KEY = 'game:rank'          # 一局游戏的积分排行榜
r.delete(KEY)              # 干净开局

# ---------- 1. 故意乱序塞进去 ----------
# ZADD 的参数:{成员: 分数}。注意是 成员→分数 的字典。
print('=== 1. 把玩家和分数乱序塞进去(故意不按顺序) ===')
r.zadd(KEY, {'张三': 87})
r.zadd(KEY, {'李四': 95})
r.zadd(KEY, {'王五': 72})
r.zadd(KEY, {'赵六': 95})   # 注意:赵六和李四同分 95
print('塞入顺序:张三87 → 李四95 → 王五72 → 赵六95(乱的)')
print(f'一共几个人 ZCARD:{r.zcard(KEY)}\n')

# ---------- 2. Redis 已经帮你排好了 ----------
print('=== 2. 取出来:Redis 自动按分数排好,不用你排 ===')

# 升序:从低到高。0, -1 表示「从第 0 个到最后一个」= 全部
asc = r.zrange(KEY, 0, -1, withscores=True)
print(f'ZRANGE 升序(低→高):{asc}')

# 降序:从高到低 —— 排行榜要的就是这个
desc = r.zrevrange(KEY, 0, -1, withscores=True)
print(f'ZREVRANGE 降序(高→低):{desc}  ← 第一名在最前面')

print()
print('看两个细节:')
print('  · 塞的时候是乱的,取出来永远是排好序的 —— 排序是 Redis 维护的,不是你算的')
print('  · 同分(李四95、赵六95)时,Redis 再按【成员名字典序】兜底排,保证顺序稳定')

r.delete(KEY)   # 收尾清 key
