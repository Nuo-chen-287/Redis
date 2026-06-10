"""
zset_02_leaderboard.py · ZSet 的主场:一个「活着、天天在变」的排行榜

接上一问:demo 01 演的是排行榜「静止」的样子(塞进去 → 取出来是排好的)。
本 demo 演的是它「活着」的样子 —— 真实排行榜每时每刻在变,你要对它做三个动词,
而这三个动词自己拿 Python 的 list/dict 做都很贵,ZSet 就是为它们存在的:

   动词 1  某人加分     ZINCRBY   原地 +N,加完整个榜【依然是排好的】,你不用重排
   动词 2  我第几名?    ZREVRANK  反查名次,O(log N),不用把所有人扫一遍数
   动词 3  取前 N 名     ZREVRANGE 只碰榜首那几个,不碰其余几百万人

对比:同样的事自己用 Python list 做 ——
   加一个人的分 → 得重新排序整个列表          O(N log N)
   查某人第几名 → 得遍历数他前面有几个人        O(N)
ZSet 把这两件事都压到了 O(log N)。值钱的不是「读出来有序」,是「改完还有序、且改和查都便宜」。

命令(缩写拆开记):
   ZINCRBY    = Z INCR(increment 增加) BY     给某成员的分数加 N
   ZREVRANK   = Z REVerse RANK                 按【降序】的名次(第 0 名 = 分最高)
   ZSCORE     = Z SCORE                        查某成员当前几分
   ZREVRANGE  = Z REVerse RANGE                按分数降序取一段(排行榜取前 N)

跑法(项目根目录):
   .venv/bin/python tmp/zset_02_leaderboard.py
"""
import redis

r = redis.Redis(host='localhost', port=6379, db=0, decode_responses=True)

KEY = 'game:rank:today'        # 今天这局的积分榜
r.delete(KEY)                  # 干净开局

# 先有个初始榜(假设大家昨天就有底分了)
r.zadd(KEY, {'张三': 30, '李四': 50, '王五': 20, '赵六': 45})
print('=== 初始榜(降序) ===')
for name, score in r.zrevrange(KEY, 0, -1, withscores=True):
    print(f'   {name}: {int(score)}')
print()

# ---------- 动词 1:某人赢了一局,加分 ----------
# 真实业务里你【不会】「读旧分 → +10 → 写回」,而是一条 ZINCRBY 原地加。
# 关键:加完之后,榜【不用你手动重排】,Redis 自己就维护好了。
print('=== 动词 1:王五连赢两局,各 +10、+15(ZINCRBY 原地加,自动重排) ===')
new1 = r.zincrby(KEY, 10, '王五')   # 返回加完后的新分数
print(f'   王五 +10 → 现在 {int(new1)} 分')
new2 = r.zincrby(KEY, 15, '王五')
print(f'   王五 +15 → 现在 {int(new2)} 分')
print('   加完后的榜(注意王五已经自己往上爬了,我一行排序代码没写):')
for name, score in r.zrevrange(KEY, 0, -1, withscores=True):
    print(f'      {name}: {int(score)}')
print()

# ---------- 动词 2:「我第几名?我多少分?」 ----------
# 给一个人,反查名次。ZREVRANK 是 O(log N) —— 不用把所有人捞回来数。
# 注意:名次从 0 开始,所以 +1 才是人话里的「第几名」。
print('=== 动词 2:查「王五现在第几名、多少分」(不用捞全榜) ===')
rank = r.zrevrank(KEY, '王五')      # 0 = 第一名
score = r.zscore(KEY, '王五')
print(f'   ZREVRANK 王五 → {rank}(从 0 数)→ 人话:第 {rank + 1} 名')
print(f'   ZSCORE   王五 → {int(score)} 分')
print()

# ---------- 动词 3:取前 N 名(只碰榜首,不碰其余几百万人) ----------
print('=== 动词 3:取前 3 名(ZREVRANGE 0 2,排行榜页面就要这个) ===')
top3 = r.zrevrange(KEY, 0, 2, withscores=True)
for i, (name, score) in enumerate(top3, start=1):
    print(f'   第 {i} 名:{name}  {int(score)} 分')
print()
print('收口:这三件事 —— 加分、查名次、取前 N —— 在几百万人的榜上 ZSet 都是 O(log N) 级。')
print('     同样的事用 Python list 做,改一个人的分就得整列重排,查名次就得整列遍历。')
print('     这就是 demo 01「有序」之外、ZSet 真正值钱的地方:改完还有序,改和查都便宜。')

r.delete(KEY)   # 收尾清 key
