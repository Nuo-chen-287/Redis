"""
list_03_wakeup.py · BLPOP「被叫醒」的现场(demo 02 的另一半)

demo 02 你看到的是 BLPOP「等到超时、放弃返回 None」。
这个 demo 给你看更关键的另一半:BLPOP「有货时被瞬间叫醒」。

设计:一个进程里开两条线,模拟两个独立角色 ——
   · 消费者(主线程):BLPOP(timeout=0) 死等,有货才走。趴下时掐表。
   · 生产者(后台线程):故意先 sleep 几秒,再 RPUSH 塞一条。

看点:消费者「醒来的时刻」会精确地咬住「生产者塞货的时刻」。
   跑两轮,生产者分别拖 3 秒、1 秒;你会看到消费者就跟着在 ~3.0s、~1.0s 醒。
   说明它不是按某个固定超时醒的,而是 Redis 一收到 RPUSH 就主动把它捅醒 = 零延迟。
   而且这几秒它是「趴着睡」不烧 CPU(对比 sleep 轮询那个跷跷板)。

⚠️ 同 demo 02 的坑:阻塞命令必须放开客户端 socket_timeout(设 None),
   否则 timeout=0 的死等会被默认的 socket_timeout=5 在第 5 秒打断报错。

跑法(项目根目录):
   .venv/bin/python tmp/list_03_wakeup.py
"""
import time
import threading

import redis

# socket_timeout=None:阻塞命令(尤其 timeout=0 死等)必须放开客户端这侧的超时。
r = redis.Redis(host='localhost', port=6379, db=0, decode_responses=True,
                socket_timeout=None)

QUEUE = 'queue:ship'
r.delete(QUEUE)   # 干净开局,确保队列是空的


def producer(delay, payload):
    """生产者:先睡 delay 秒,再往队列右边塞一条(模拟『过了一会儿才有订单进来』)。"""
    time.sleep(delay)
    print(f'   [生产者] 睡了 {delay} 秒,现在 RPUSH 塞进 → {payload}')
    r.rpush(QUEUE, payload)


def consume_one_round(delay, payload):
    """消费者:趴下死等一条,记录从趴下到醒来用了多久。"""
    # 先把生产者挂到后台,让它 delay 秒后才塞货
    threading.Thread(target=producer, args=(delay, payload), daemon=True).start()

    print(f'   [消费者] BLPOP 死等中(队列现在空的,我趴下了)……')
    t0 = time.time()
    key, value = r.blpop(QUEUE, timeout=0)   # timeout=0 = 死等,有货才返回
    waited = time.time() - t0
    print(f'   [消费者] 啪!醒了 → 从「{key}」拿到「{value}」  '
          f'(趴了 {waited:.2f} 秒就醒,正好咬住生产者塞货的时刻)')


print('=' * 60)
print('第 1 轮:生产者拖 3 秒才塞 → 看消费者是不是 ~3.0 秒才醒')
print('=' * 60)
consume_one_round(3, '发货任务#1-订单A')

print()
print('=' * 60)
print('第 2 轮:生产者只拖 1 秒 → 看消费者是不是改成 ~1.0 秒就醒')
print('=' * 60)
consume_one_round(1, '发货任务#2-订单B')

print()
print('结论:消费者醒来的秒数 = 生产者塞货的秒数。')
print('     不是固定超时,是「一有货就被瞬间叫醒」,且趴着等的那几秒不烧 CPU。')

r.delete(QUEUE)   # 收尾清 key
