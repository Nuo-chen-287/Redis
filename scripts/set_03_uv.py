"""
set_03_uv.py · Set 的第三张面孔:靠去重做「这是不是第一次?」

钩子 = SADD 的返回值:它返回「这次真正新增了几个」,不是集合总数。
   返回 1 → 这个值以前没有,你是第一个   → 第一次
   返回 0 → 这个值早就在了                → 重复

这一个返回值就能干一大类活儿:
   · UV 统计   :一个用户今天来多少次,都只算 1 个独立访客
   · 抽奖去重   :同一个人不能中两次
   · 防重复提交 :同一个订单号只处理一次

本 demo 用 UV(Unique Visitor 独立访客):
   key 设计:uv:页面:日期  →  里面装「今天来过的所有 user_id」
   SCARD 这个 key = 今天的 UV 数(自动去重,不用你操心)

跑法(项目根目录):
   .venv/bin/python tmp/set_03_uv.py
"""
import redis

r = redis.Redis(host='localhost', port=6379, db=0, decode_responses=True)

KEY = 'uv:home:2026-06-01'      # 首页 6/1 当天的独立访客集合
r.delete(KEY)                   # 干净开局

# 模拟一天的访问流水:user1 来了 3 次,user2 来 1 次,user3 来 2 次
visits = ['user1', 'user2', 'user1', 'user3', 'user1', 'user3']

print('=== 一天的访问流水(注意 SADD 返回值:1=第一次, 0=重复) ===')
for uid in visits:
    first = r.sadd(KEY, uid)        # 返回 1 or 0
    tag = '第一次来 ✅ (UV +1)' if first == 1 else '今天来过了,不重复计数'
    print(f'  {uid} 访问 → SADD 返回 {first} → {tag}')

print(f'\n今天到底有几个独立访客?SCARD → {r.scard(KEY)}')
print(f'真实独立访客是:{r.smembers(KEY)}')
print('  注意:总共发生了 6 次访问,但 UV 只有 3 —— 重复的人被去重了,你一行代码没写\n')

# ---------- 同理:抽奖去重 ----------
print('=== 同款套路做抽奖去重:用返回值判断「能不能中」 ===')
WINNERS = 'lottery:winners'
r.delete(WINNERS)
for uid in ['userA', 'userB', 'userA']:     # userA 想中两次
    if r.sadd(WINNERS, uid) == 1:
        print(f'  {uid} 中奖 🎉')
    else:
        print(f'  {uid} 已经中过了,不能再中 ❌')

r.delete(KEY, WINNERS)   # 收尾清 key
