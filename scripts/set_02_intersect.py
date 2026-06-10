"""
set_02_intersect.py · Set 的杀手锏:服务端直接算 交集/并集/差集

场景:社交关注。
   「我关注的人」是一个 Set,「你关注的人」是一个 Set。
   三种集合运算,各对应一个真实问题:

   SINTER  交集  = 两边都有的     → 「咱俩的共同关注」(共同好友的内核)
   SUNION  并集  = 两边合起来      → 「咱俩一共关注了哪些人」(自动去重)
   SDIFF   差集  = 我有但你没有    → 「我关注、但你还没关注的」(可拿来做推荐)

关键:运算在 Redis 服务端算完,只把结果发回来。
   对比 Python:你得先把两份名单都拉到本地内存,再 a & b。
   名单几十万人时,差别就是「搬几十万条过网线」vs「只搬结果那几条」。

命令缩写:
   SINTER = Set INTERsection (交集)
   SUNION = Set UNION        (并集)
   SDIFF  = Set DIFFerence   (差集,注意有方向:A - B != B - A)

跑法(项目根目录):
   .venv/bin/python tmp/set_02_intersect.py
"""
import redis

r = redis.Redis(host='localhost', port=6379, db=0, decode_responses=True)

ME = 'follow:me'        # 我关注的人
YOU = 'follow:you'      # 你关注的人
r.delete(ME, YOU)       # 干净开局

# 我关注:Alice Bob Carol
r.sadd(ME, 'Alice', 'Bob', 'Carol')
# 你关注:Bob Carol David  (和我重叠的是 Bob、Carol)
r.sadd(YOU, 'Bob', 'Carol', 'David')

print(f'我关注的人 :{r.smembers(ME)}')
print(f'你关注的人 :{r.smembers(YOU)}\n')

# ---------- 交集:共同关注 ----------
print('=== SINTER 交集:咱俩都关注了谁(共同好友的内核) ===')
print(f'  → {r.sinter(ME, YOU)}   ← Bob、Carol 两边都有\n')

# ---------- 并集:合起来一共关注了谁 ----------
print('=== SUNION 并集:咱俩加起来一共关注了哪些人(自动去重) ===')
print(f'  → {r.sunion(ME, YOU)}   ← 4 个人,Bob/Carol 只算一次\n')

# ---------- 差集:有方向! ----------
print('=== SDIFF 差集:注意有方向,A-B 和 B-A 不一样 ===')
print(f'  我有你没有 SDIFF(me, you) → {r.sdiff(ME, YOU)}   ← Alice(可推荐给你关注)')
print(f'  你有我没有 SDIFF(you, me) → {r.sdiff(YOU, ME)}   ← David(可推荐给我关注)')

r.delete(ME, YOU)   # 收尾清 key
