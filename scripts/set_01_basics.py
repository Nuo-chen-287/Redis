"""
set_01_basics.py · Set 长什么样:天然去重 + O(1) 判存在

心智模型:
   List = 排队的人(有序、可重复,张三能站两次)
   Set  = 一个班的学号(无序、不重复,同一个号绝不出现两次)

本 demo 只演示两件最基础的事:
   1. 天然去重 —— 同一个值塞多次,里面也只有一个
   2. 判存在   —— 问「在不在」一句话答,不用挨个翻(SISMEMBER)

命令(缩写先拆开记):
   SADD       = Set ADD            往集合塞(返回真正新增了几个)
   SISMEMBER  = S IS MEMBER        是成员吗 → True/False
   SMEMBERS   = S MEMBERS          列出所有成员
   SCARD      = S CARDinality      集合大小(有几个)
   SREM       = S REMove           移除

跑法(项目根目录):
   .venv/bin/python tmp/set_01_basics.py
"""
import redis

r = redis.Redis(host='localhost', port=6379, db=0, decode_responses=True)

KEY = 'class:1班'          # 1班的学号集合
r.delete(KEY)              # 干净开局

# ---------- 1. 天然去重 ----------
print('=== 1. 天然去重:1001 故意塞 3 次 ===')
n1 = r.sadd(KEY, '1001')           # 第一次塞,新增
n2 = r.sadd(KEY, '1001', '1001')   # 再塞两次同样的号
print(f'第一次 SADD 1001        → 真正新增了 {n1} 个')
print(f'再 SADD 1001 1001 两次  → 真正新增了 {n2} 个(重复的被无视了)')

r.sadd(KEY, '1002', '1003')        # 再塞两个不一样的
print(f'班里现在的学号(无序):{r.smembers(KEY)}')
print(f'班里一共几个人 SCARD  :{r.scard(KEY)}  ← 塞了那么多次,只有 3 个\n')

# ---------- 2. O(1) 判存在 ----------
print('=== 2. 判存在:这个号在不在这个班? ===')
print(f"1001 在 1班吗? SISMEMBER → {r.sismember(KEY, '1001')}")
print(f"9999 在 1班吗? SISMEMBER → {r.sismember(KEY, '9999')}  ← 不用挨个翻,一句话就答")

# ---------- 3. 移除 ----------
print('\n=== 3. 移除一个学号(转学了) ===')
r.srem(KEY, '1002')
print(f'SREM 1002 后,班里剩:{r.smembers(KEY)}')

r.delete(KEY)   # 收尾清 key
