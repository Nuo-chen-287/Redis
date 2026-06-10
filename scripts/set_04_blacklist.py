"""
set_04_blacklist.py · 应用场景:黑名单 / 白名单(Set 面孔1「去重的容器」)

语义先分清(最容易混):
   黑名单 = 默认放行,在名单里就拦   (SISMEMBER True  → 踢)
   白名单 = 默认拒绝,不在名单里就拦  (SISMEMBER False → 踢)

核心动作:每个请求进来,拿 IP/user 去 SISMEMBER 问一句,O(1) 决定放行还是拦截。

为什么用 Redis Set,而不是 MySQL 的 WHERE ip IN(...) 或 Python 本地 set:
   · O(1) 内存判断 —— 拦截在每个请求的关键路径上,高频,扛不住每次查磁盘
   · 多机共享     —— 50 台 web 机问的是同一份名单
   · 即时生效     —— 运营后台 SADD 一个 IP,所有机器下一个请求立刻拦到

跑法(项目根目录):
   .venv/bin/python tmp/set_04_blacklist.py
"""
import redis

r = redis.Redis(host='localhost', port=6379, db=0, decode_responses=True)

BLACK = 'blacklist:ip'        # IP 黑名单
WHITE = 'whitelist:user'      # 内测白名单(只有这些 user 能进)
r.delete(BLACK, WHITE)

# ---------- 黑名单:在里面就拦 ----------
r.sadd(BLACK, '10.0.0.1', '10.0.0.2')     # 运营把两个恶意 IP 拉黑

def allow_by_blacklist(ip):
    if r.sismember(BLACK, ip):
        return f'❌ 拦截({ip} 在黑名单)'
    return f'✅ 放行({ip})'

print('=== 黑名单:默认放行,命中就拦 ===')
for ip in ['10.0.0.1', '8.8.8.8', '10.0.0.2']:
    print(f'  {ip:>10} → {allow_by_blacklist(ip)}')

# ---------- 白名单:不在里面就拦 ----------
r.sadd(WHITE, 'userA', 'userB')           # 只有 A、B 拿到内测资格

def allow_by_whitelist(uid):
    if r.sismember(WHITE, uid):
        return f'✅ 放行({uid} 有内测资格)'
    return f'❌ 拦截({uid} 不在白名单)'

print('\n=== 白名单:默认拒绝,不在名单就拦 ===')
for uid in ['userA', 'userC', 'userB']:
    print(f'  {uid:>6} → {allow_by_whitelist(uid)}')

# ---------- 运营动态加黑,立刻生效 ----------
print('\n=== 运营把 8.8.8.8 临时拉黑,下一个请求立刻拦到 ===')
r.sadd(BLACK, '8.8.8.8')
print(f'  8.8.8.8 → {allow_by_blacklist("8.8.8.8")}  ← 不用重启、不用改代码')

r.delete(BLACK, WHITE)
