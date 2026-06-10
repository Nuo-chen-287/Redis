"""
list_04_recent.py · List 的另一种玩法:只留最新 N 条(不当队列用)

心智模型换了:这里 List 不是「传送带(一头进一头出)」,而是
「一个最多只留 N 条、旧的自动被挤掉的滚动榜」。

套路 = LPUSH(新的塞最前面) + LTRIM(只保留前 N 条,其余全删)。
场景:用户「最近浏览的商品」,首页只显示最新 3 个。

LTRIM key start stop = 只保留下标 [start, stop] 这一段,区间外的统统删掉。
   LTRIM key 0 2  → 只留下标 0、1、2 共 3 条 = 钉死最多 3 条。

跑法(项目根目录):
   .venv/bin/python tmp/list_04_recent.py
"""
import redis

r = redis.Redis(host='localhost', port=6379, db=0, decode_responses=True)

KEY = 'recent:user:1'      # 用户1 的最近浏览
KEEP = 3                   # 只留最新 3 条
r.delete(KEY)              # 干净开局


def view(product):
    """模拟用户看了一个商品:塞到最前面,再砍到只剩最新 KEEP 条。"""
    r.lpush(KEY, product)          # 新的塞最左边(头部)
    r.ltrim(KEY, 0, KEEP - 1)      # 只保留下标 0..KEEP-1,旧的被挤出去删掉
    print(f'看了 {product:>3} 后 → 最近浏览:{r.lrange(KEY, 0, -1)}')


print(f'(只留最新 {KEEP} 条,看 A B C D E 五个商品,留意 A 怎么被挤出去)\n')
for p in ['A', 'B', 'C', 'D', 'E']:
    view(p)

print(f'\n最终「最近浏览」只剩最新 {KEEP} 条,最新在最前:{r.lrange(KEY, 0, -1)}')
print(f'列表长度被钉死在 {r.llen(KEY)},无论看多少都不会再涨')

r.delete(KEY)   # 收尾清 key
