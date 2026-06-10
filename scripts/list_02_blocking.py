"""
List demo 02（重做·极简版）：亲身感受「阻塞」是什么

   LPOP  = 看一眼队列，空的就立刻空手返回 None，不等。     （= 跑下楼看一眼就上楼）
   BLPOP = 队列空的话，程序就「卡」在这一行不往下走，杵着等，  （= 搬板凳坐楼下等外卖）
           直到 ① 有人往队列塞东西（立刻醒、拿到）
               ② 或者等够 timeout 秒还没有，才放弃、返回 None。

这个文件先让你感受 ②：对着一个空队列 BLPOP，timeout=5。
你会看到程序「卡住 5 秒」一动不动 —— 那 5 秒它就是在「阻塞等待」。

⚠️ 踩过的坑（redis-py 8.0 默认值咬人，必看）：
   redis-py 8.0.0 起，redis.Redis() 默认 socket_timeout=5（客户端这条网络连接
   读数据最多等 5 秒，超了就报 "Timeout reading from socket"）。
   它会和 BLPOP(timeout=5) 的「服务端等 5 秒」撞车：两个 5 秒几乎同时到点，
   客户端 socket 抢先翻脸报错，永远轮不到服务端把正常的 None 送回来。
   铁律：客户端 socket_timeout 必须 > 阻塞命令的 timeout，或干脆设 None。
   所以下面这行特意写了 socket_timeout=None（永不主动超时，完全听 Redis 指挥）。
   ——也正因如此，写阻塞消费者时不能直接用 cache.r（它的 socket_timeout=5）。

跑法（项目根目录）：
   .venv/bin/python tmp/list_02_blocking.py
"""
import time

import redis

# socket_timeout=None：见上方「踩过的坑」。阻塞命令必须放开客户端这一侧的超时。
r = redis.Redis(host='localhost', port=6379, db=0, decode_responses=True,
                socket_timeout=None)

QUEUE = 'queue:ship'
r.delete(QUEUE)          # 确保队列是空的

print('对比两种取法，队列现在是空的：\n')

# ① 普通 LPOP：空了立刻返回，不等 ───────────────────────────────
print('① 普通 LPOP（不阻塞）：队列空 → 立刻返回，不等')
t0 = time.time()
result = r.lpop(QUEUE)
print(f'   结果：{result}   （只花了 {time.time() - t0:.1f} 秒，几乎瞬间）\n')

# ② BLPOP：空了赖着等，等够 timeout 才走 ────────────────────────
print('② BLPOP（阻塞）：队列空 → 程序会卡在这一行，杵着等 5 秒……')
print('   ⏳ 盯着屏幕，接下来 5 秒不会有任何新输出，这就是「阻塞」。')
t0 = time.time()
result = r.blpop(QUEUE, timeout=5)     # timeout=5：最多等 5 秒
print(f'   等够 5 秒还是没货，放弃：{result}   （真的等了 {time.time() - t0:.1f} 秒）')

r.delete(QUEUE)          # 收尾清 key
