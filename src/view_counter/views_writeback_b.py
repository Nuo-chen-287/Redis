"""
view_counter/views_writeback_b.py · 视频播放量「攒批回写」方案 B（Redis 存增量）

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
这是什么问题？
    视频播放量写得极其频繁（热门视频一秒几千次）。如果每次播放都去 UPDATE 数据库，
    数据库会被写操作压垮。所以思路是：把「+1」先攒在 Redis 里，定时再批量刷回 MySQL。
    这套「写操作先进缓存、异步回写数据库」的玩法，工业界叫 write-behind（异步回写）。

    本文件只讲「思路」——不保证连得上库、也不保证 MySQL/Redis 里有对应数据。
    把它当成带注释的伪代码读，重点看「数据加几次、加在哪、谁覆盖谁」。
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

方案 A vs 方案 B —— 核心区别就一句话：Redis 里存的是「总量」还是「增量」？

┌─────────────┬──────────────────────────────┬──────────────────────────────┐
│             │  方案 A：Redis 存「总量」      │  方案 B：Redis 存「增量」(本文)│
├─────────────┼──────────────────────────────┼──────────────────────────────┤
│ Redis 里是啥 │ 完整播放量，比如 151          │ 自上次回写后新增了多少，比如 51 │
│ 缓存没命中时 │ 从 DB 读出总量 → 放进 Redis   │ 不需要预读 DB，delta 从 0 起算  │
│ 每次播放    │ INCR（在总量上 +1）           │ INCR（在增量上 +1）            │
│ 要显示总数  │ 直接读 Redis（快）            │ DB 的值  +  Redis 的 delta     │
│ 定时回写 DB │ DB = Redis 值（覆盖！）       │ DB += delta，然后 delta 清零    │
│ Redis 万一挂│ 危险：覆盖可能把 DB 写小/写没  │ 安全：最多丢掉这一小段没回写的  │
│             │                              │ 增量，DB 里的历史总量纹丝不动   │
└─────────────┴──────────────────────────────┴──────────────────────────────┘

为什么大厂偏向方案 B？
    因为方案 A 的回写是「覆盖」，一旦 Redis 抽风（被驱逐、重启丢数据、key 过期），
    可能拿一个偏小甚至是 0 的值去覆盖数据库，把真实播放量直接抹掉。
    方案 B 的 DB 永远只做「加法」，且加的是纯增量，缓存丢了顶多丢掉还没回写的那几十次，
    数据库里的历史总量永远是对的。

⚠️ 最容易踩的坑（你最初的假想就栽在这）：
    千万不要「播放时给 DB +1，回写时又把含着原值的缓存再加回 DB 一次」——
    那样原来的总量会被算两遍（双重计数）。
    方案 B 之所以不会：热路径里 DB 完全不动，只动 Redis 的 delta；
    DB 只在回写那一刻被加一次，且加的是不含历史值的纯增量。

运行（仅示意，库不通也没关系，看注释就行）：
    .venv/bin/python src/view_counter/views_writeback_b.py
"""

import os
import sys

# 本文件在 src 的二级目录里，和 redis_crud/ 一样要把上一级 src 加进搜索路径，
# 才能 import cache（复用全项目唯一的 Redis 连接）和 db（真实 MySQL）。
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cache  # noqa: E402  复用 cache.r
import db     # noqa: E402  复用 MySQL 连接（这里只用到示意性的读/写）


def _delta_key(video_id: int) -> str:
    """增量 key。命名带前缀，和别的业务 key 隔开（项目约定：靠 key 前缀隔离，不靠 db 编号）。"""
    return f"video:{video_id}:views_delta"


# ──────────────────────────────────────────────────────────────────────────
# 热路径①：用户播放一次视频
#   只动 Redis，绝不碰数据库。这正是「攒批」的意义——把高频写挡在数据库门外。
# ──────────────────────────────────────────────────────────────────────────
def on_play(video_id: int) -> None:
    # INCR 是服务端原子自增：哪怕一万个用户同时播放，也不会丢更新、不会数错。
    # （对比：Python 自己 count += 1 在多进程/多机部署下会丢更新——这正是非用 Redis 不可的原因。）
    cache.r.incr(_delta_key(video_id))


# ──────────────────────────────────────────────────────────────────────────
# 读路径：要给用户展示「当前总播放量」
#   方案 B 的总量是拼出来的： 数据库里的历史值  +  Redis 里还没回写的增量。
# ──────────────────────────────────────────────────────────────────────────
def get_total_views(video_id: int) -> int:
    # 1) 数据库里的历史总量（上一次回写后落盘的值）。
    #    这里偷懒用 query_product 示意一下「从 DB 读一个数」，真实项目应是 videos 表的 views 字段。
    row = db.query_product(video_id)
    db_views = (row or {}).get("views", 0)

    # 2) Redis 里自上次回写以来新增的增量（可能为 None → 当 0）。
    delta = int(cache.r.get(_delta_key(video_id)) or 0)

    # 3) 真正的总量 = 历史 + 增量。
    return db_views + delta


# ──────────────────────────────────────────────────────────────────────────
# 冷路径：定时回写（比如每 30 秒 / 每分钟由一个后台任务调一次）
#   把攒在 Redis 的增量「搬」进数据库，让 DB 追平，然后把已搬走的那部分从 delta 里扣掉。
# ──────────────────────────────────────────────────────────────────────────
def flush_to_db(video_id: int) -> None:
    key = _delta_key(video_id)

    # ⚠️ 并发关键点：从「读出 delta」到「回写完成」这中间，用户还在不停 INCR。
    #    所以不能用 r.delete(key) 简单清零——那会把回写期间新增的播放次数一起抹掉（丢数据）。
    #
    #    正确做法：先把当前增量「原子取走并清零」，回写期间新来的 INCR 会从 0 重新攒起，
    #    一点不丢。redis-py 里用 getdel（GETDEL：读到旧值的同时把 key 删掉，二者原子）。
    #    （老版本 Redis 没有 GETDEL，就用 GETSET key 0 等价替代。）
    amount = cache.r.getdel(key)
    amount = int(amount or 0)
    if amount == 0:
        return  # 这段时间没人看，无需回写

    # DB 只做「加法」，且加的是纯增量 amount（不含历史值）→ 永不重复计数。
    #    真实 SQL 大致是：UPDATE videos SET views = views + %s WHERE id = %s
    #    这里只示意，不保证表结构存在。
    _db_add_views(video_id, amount)


def _db_add_views(video_id: int, amount: int) -> None:
    """示意：把 amount 累加到数据库的 views 字段。真实项目里就是一条 UPDATE。"""
    # conn = db._pool.connection()
    # with conn.cursor() as cur:
    #     cur.execute("UPDATE videos SET views = views + %s WHERE id = %s", (amount, video_id))
    # conn.commit()
    print(f"  [回写] 给视频 {video_id} 的 DB 播放量 += {amount}（纯增量，不含历史值）")


# ──────────────────────────────────────────────────────────────────────────
# 把整条链路串起来演示一遍（即便库不通，前面的逻辑和注释才是重点）。
# ──────────────────────────────────────────────────────────────────────────
def main():
    video_id = 1001
    print("方案 B：Redis 存增量 + 定时回写\n")

    print("① 模拟 51 次播放（热路径只动 Redis，不碰 DB）...")
    for _ in range(51):
        on_play(video_id)
    print(f"   现在 Redis 里 {_delta_key(video_id)} 攒了:",
          cache.r.get(_delta_key(video_id)))

    print("\n② 定时任务触发回写（把增量搬进 DB，并把 delta 清零）...")
    flush_to_db(video_id)
    print("   回写后 Redis 增量:", cache.r.get(_delta_key(video_id)), "（已清零，新播放从 0 重新攒）")


if __name__ == "__main__":
    main()
