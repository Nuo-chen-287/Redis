"""
zset_03_delay_queue.py · ZSet 的第二场景:延迟队列 / 定时任务(score = 时间戳)

接 Set 会话留的钩子:
   Set 边界 2 说过 ——「封禁 IP 1 小时自动解封」做不了,因为 Set 里单个成员【不能单独设 TTL】。
   ZSet 正好补上:把「解封时间戳」当 score 存进去,谁的时间到了就捞谁出来解封。

核心心法(整个延迟队列就这一句):
   score 不再是「分数」,而是「这件事该在什么时刻发生」的【时间戳】。
   于是「排序」自动变成了「按时间先后排好的待办队列」——
   最该先处理的(时间最早的)永远在最前面。
   到点没到点,就是一句话:score <= 现在时间 → 到点了,该处理。

主命令:
   ZRANGEBYSCORE  = Z RANGE BY SCORE   按【分数区间】取成员(这里区间 = 「从最早 到 现在」)
                    取法:ZRANGEBYSCORE key -inf <now>  → 所有「时间 <= 现在」= 到点的
   ZADD / ZREM    塞任务 / 处理完移除

同一个模子能套一大类活儿(都是「到某个时刻该干某事」):
   · 封禁自动解封    score = 解封时间戳
   · 订单超时取消    score = 下单时间 + 30 分钟
   · 定时任务调度    score = 计划执行时刻

跑法(项目根目录):
   .venv/bin/python tmp/zset_03_delay_queue.py
"""
import time
import redis

r = redis.Redis(host='localhost', port=6379, db=0, decode_responses=True)

KEY = 'ban:auto_unban'      # 待解封队列:成员 = 被封的 IP,score = 该解封的时间戳
r.delete(KEY)              # 干净开局

now = int(time.time())     # 现在的时间戳(秒)

# ---------- 1. 封三个 IP,各自约定不同的解封时刻 ----------
# 故意造两个「已经该解封了」(解封时刻在过去)、一个「还没到」(在未来),
# 这样不用真的等,就能看出「到点的被捞出来、没到点的留着」。
print(f'=== 1. 封禁三个 IP,把【解封时间戳】当 score 存进去(现在 now={now}) ===')
r.zadd(KEY, {'1.1.1.1': now - 60})    # 60 秒前就该解封了 → 已到点
r.zadd(KEY, {'2.2.2.2': now - 5})     # 5 秒前就该解封了  → 已到点
r.zadd(KEY, {'3.3.3.3': now + 3600})  # 1 小时后才解封    → 还没到
print('   1.1.1.1 → 解封时刻 now-60(早就该放了)')
print('   2.2.2.2 → 解封时刻 now-5 (刚刚该放)')
print('   3.3.3.3 → 解封时刻 now+3600(还得关一小时)')
print(f'   当前队列里一共 {r.zcard(KEY)} 个待解封\n')

# ---------- 2. 一次「巡检」:谁到点了就捞谁 ----------
# 这就是延迟队列的心脏:ZRANGEBYSCORE key -inf now
#   -inf = 负无穷(从最早的),now = 现在 → 取出所有「解封时刻 <= 现在」的,即到点的。
print('=== 2. worker 巡检一次:捞出所有「解封时刻 <= 现在」的 IP ===')
due = r.zrangebyscore(KEY, '-inf', now, withscores=True)
print(f'   ZRANGEBYSCORE -inf {now} → 到点的:{[ip for ip, _ in due]}')
print('   注意 3.3.3.3 没被捞出来 —— 它的解封时刻还在未来,自动留在队列里\n')

# ---------- 3. 处理到点的:解封 + 从队列移除 ----------
print('=== 3. 逐个解封,并用 ZREM 把处理完的踢出队列 ===')
for ip, when in due:
    print(f'   解封 {ip}(约定解封时刻 {int(when)},现在 {now},确实到点了)')
    r.zrem(KEY, ip)    # 处理完移除,避免下次巡检重复处理
print(f'   解封完,队列里还剩 {r.zcard(KEY)} 个(就是那个没到点的 3.3.3.3)\n')

print('收口:')
print('  · score 当时间戳,ZSet 的「按分排序」就变成「按时间排好的待办队列」。')
print('  · 真实系统里第 2 步会是一个 worker【每隔几秒巡检一次】,反复捞「现在到点的」来处理。')
print('  · 这一招补上了 Set 做不到的「成员级 TTL / 定时触发」—— 这也是 ZSet 收尾的最后一张面孔。')

r.delete(KEY)   # 收尾清 key
