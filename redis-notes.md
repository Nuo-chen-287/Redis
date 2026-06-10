# Redis 专题学习笔记

> 学习方式：**痛点驱动 + 渐进迭代**。每一版只解决一个痛点，解决旧痛点时往往冒出新痛点，那个新痛点就是下一版的入口。
> 全程 Python（`redis-py`）。分布式锁那一版我们**手搓 Redisson 的等价实现**，逐个特性对照讲解。

---

## 关于这四个名词（开篇先理清）

学之前先把容易混淆的四个东西归位，避免一上来就乱：

| 名称 | 它到底是什么 | 类比 |
|------|------------|------|
| **Redis** | 内存数据库 / 缓存中间件（C 写的服务端） | 超快的"内存版数据库" |
| **Redisson** | Redis 的一个 **Java 客户端库**（封装了分布式工具） | 操作 Redis 的"高级遥控器" |
| **RabbitMQ** | 消息队列中间件（Erlang） | 传统通用的"邮局" |
| **RocketMQ** | 消息队列中间件（Java，阿里） | 为高吞吐/金融优化的"邮局" |

- **Redis 和 Redisson 是一对**：一个是服务端，一个是连它的 Java 客户端。
- **RabbitMQ 和 RocketMQ 是一对**：都是消息队列（MQ），互为竞品。
- 这两对是**完全不同层面**的东西，不是竞争关系。本专题只学前一对，MQ 暂不研究。

> ⚠️ **Python 与 Redisson 的关系**：Redisson 是 Java 独占库，Python 里没有。但它的价值在于底层封装了一套**正确的分布式锁实现**（唯一标识、Lua 原子解锁、看门狗续期、可重入）。所以我们用 Python 亲手把这套东西搓出来（见 V5），每搓一个特性就对照"这就是 Redisson 帮你做的事"——比直接调 `lock.lock()` 理解得深得多。

---

## 贯穿场景

一个电商 **「商品详情 + 库存扣减」服务**：

- 底层有一个**故意很慢的"数据库"**（用 `time.sleep` 模拟慢查询）；
- 上层用 Redis 当缓存和协调工具；
- 我们会不断给它加压、制造故障，亲眼看每一版怎么崩、又怎么被救活。

---

## 学习路线总览

整体顺序：**缓存三大问题（V2–V4）→ 分布式锁 / Redisson（V5）→ 持久化（V6）→ 高可用（V7）**。

| 版本 | 副标题 | 解决的痛点 | 新引出的痛点 |
|------|--------|-----------|-------------|
| **V1** | 裸查询基线（无缓存） | —（建立基线） | 每次都打慢 DB，DB 扛不住 |
| **V2** | 加缓存（Cache-Aside） | 热点命中，DB 压力骤降 | 查不存在的 id → **缓存穿透** |
| **V3** | 缓存空值 + 布隆过滤器 | 挡住穿透 | 热点 key 过期瞬间 → **缓存击穿** |
| **V4** | 互斥锁重建 | 只放一个请求重建 | 大量 key 同时失效 → **缓存雪崩** |
| **V5** | 手搓分布式锁 ≈ Redisson | 简易锁的隐藏 bug | （锁做对了，转向底层运维） |
| **V6** | RDB vs AOF 持久化 | 重启不丢数据 | 单点宕机服务仍会瘫 |
| **V7** | 主从同步 + 哨兵 | 自动故障转移、读写分离 | （收尾：对比 Cluster 集群） |

### 各版本详解

- **V1 · 裸查询基线（无缓存）**
  - 做什么：请求直接打"慢 DB"，每次查询 sleep ~200ms。
  - 痛点：并发一上来，每个请求都压在 DB 上，响应慢、DB 要被打爆。→ 该上缓存了。

- **V2 · 加缓存（Cache-Aside）→ 撞上「缓存穿透」**
  - 解决：先查缓存，未命中再查 DB 并回填。热点数据命中后 DB 压力骤降。
  - 新痛点：故意查**不存在的商品 id**，缓存永远不命中、每次穿透到 DB；有人拿不存在的 id 狂刷就能绕过缓存打爆 DB。

- **V3 · 缓存空值 + 布隆过滤器，治「穿透」→ 撞上「缓存击穿」**
  - 解决：把"查不到"也缓存起来（空值 + 短 TTL），再加布隆过滤器在入口拦截。
  - 新痛点：一个**热点 key 过期的瞬间**，大量并发同时发现缓存没了、一起冲去重建 → DB 被击穿。引出"重建时加锁"。

- **V4 · 互斥锁重建，治「击穿」→ 撞上「缓存雪崩」**
  - 解决：只让一个请求去重建缓存，其余等待，DB 不再被同一个 key 击穿。
  - 新痛点：大量 key 在**同一时刻集体过期**（或 Redis 整个挂了）→ 全线缓存同时失效 → 雪崩。解法：随机 TTL、逻辑过期、多级兜底、熔断降级。

- **V5 · 把「锁」做对：手搓分布式锁 ≈ Redisson**
  - 解决：V4 的简易锁在分布式/多进程下有 bug（误删别人的锁、锁提前过期但业务没跑完、不可重入）。一步步修：`SET NX PX` → 唯一 value + Lua 原子解锁 → 看门狗自动续期 → 可重入。每步对照 Redisson 的对应特性。
  - 产出：一个可用的 Python 分布式锁 + 一张"它 vs Redisson"对照表。

- **V6 · 数据别丢：RDB vs AOF 持久化**
  - 解决：Redis 重启后内存数据全没的问题。搞懂 RDB（快照）、AOF（追加日志）的取舍、混合持久化与恢复行为。亲手 kill Redis 看数据丢没丢。

- **V7 · 别单点：主从同步 + 哨兵自动故障转移**
  - 解决：Redis 宕机期间整个服务瘫痪（单点故障）。本机起 1 主 2 从 + 3 哨兵，演示主从复制、读写分离；手动 kill 主节点，看哨兵自动选新主、服务自愈。最后对比 Cluster 集群方案。

---

## 环境准备

- **Python 依赖**（装在项目 `.venv` 里）：`.venv/bin/python -m pip install -r requirements.txt`
  - `redis`（redis-py 客户端）、`PyMySQL`（MySQL 驱动）、`DBUtils`（连接池）。
  - 后续所有命令都用 `.venv/bin/python` 跑，确保用的是项目虚拟环境。
- **Redis**：本机已通过 `brew install redis` 安装。
  - 启动：`brew services start redis` 或 `redis-server`；验证：`redis-cli ping` → `PONG`。
  - V7 的主从/哨兵会用到多个端口的实例，届时提供现成配置文件。
- **MySQL**（真实数据源，不用 mock）：本机 MySQL 8.x，库名 `redis_learning`。
  - 连接配置在 `src/db.py` 顶部，默认读环境变量 `Localhost_MYSQL_*`，缺省回退到本机默认。
  - **首次需初始化**（建库/建表/塞测试数据，可重复执行）：`.venv/bin/python src/db.py`
  - `src/db.py` 是贯穿所有版本的共享数据源模块，提供 `query_product()` 与查询计数。

---

<!-- 下面从 V1 开始，每学完一版就在此追加对应章节 -->

## V1：裸查询基线（无缓存）

学缓存之前，得先**亲眼看到没有缓存有多痛**——否则后面加的每一行缓存代码都只是"别人说要这么写"，而不是你自己感受到的需求。

所以 V1 故意写得最朴素：商品详情查询直接打**真实的本机 MySQL**，中间没有任何缓存层。

数据源抽到了共享模块 `src/db.py`（后面每版复用），其中要说清楚**哪部分真、哪部分模拟**：连 MySQL、连接池、取数据 100% 真实；但单行主键查询只要零点几毫秒，那样 V2 加缓存就看不出省了多少。真实的商品详情页往往要 join 商品/库存/促销好几张表，是个慢查询，所以用 MySQL 的 `SLEEP()` 在服务端加一段可配置延迟代表"重查询"——它**真实地占住一个 DB 连接**，从而真实地造成连接池排队。连接池 `maxconnections=10` 就是"DB 同时最多处理 10 个查询"的硬上限，超出的请求只能排队，这正是 DB 成为瓶颈的根源。

我们让 **50 个用户同时查看同一个热门商品**，盯住核心指标：**DB 到底被查了多少次。**

### 示例代码

`src/db.py`（共享数据源，真实 MySQL + 连接池）：

```python
"""
db.py · 共享数据源模块（贯穿整个 Redis 专题）
连接本机真实 MySQL，对外提供 query_product()。后面每一版都复用它。
SLOW_QUERY_SECONDS 用 MySQL SLEEP() 模拟"重查询"，真实占住一个连接 → 真实造成连接池排队。
直接运行可初始化数据库：.venv/bin/python src/db.py
"""

import os
import threading

import pymysql
from dbutils.pooled_db import PooledDB

MYSQL_HOST = os.environ.get("Localhost_MYSQL_HOST", "127.0.0.1")
MYSQL_PORT = int(os.environ.get("Localhost_MYSQL_PORT", "3306"))
MYSQL_USER = os.environ.get("Localhost_MYSQL_USER", "root")
MYSQL_PASSWORD = os.environ.get("Localhost_MYSQL_PASSWORD", "12345678")
MYSQL_DB = os.environ.get("Localhost_MYSQL_DB", "redis_learning")

SLOW_QUERY_SECONDS = 0.2   # 模拟"复杂查询"的耗时；设 0 看真实裸速度
DB_POOL_SIZE = 10          # 连接池上限 = DB 能同时处理的查询数。超过就排队，这正是瓶颈所在

_pool = PooledDB(
    creator=pymysql, maxconnections=DB_POOL_SIZE, blocking=True,  # 池满则排队，贴近真实 DB
    host=MYSQL_HOST, port=MYSQL_PORT, user=MYSQL_USER, password=MYSQL_PASSWORD,
    database=MYSQL_DB, charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor,
)

_query_count = 0
_count_lock = threading.Lock()


def query_product(product_id: int) -> dict | None:
    """查询商品详情。每次调用都真实地占用一个连接 + 执行真实 SQL。"""
    global _query_count
    with _count_lock:
        _query_count += 1
    conn = _pool.connection()      # 从池里借连接，借不到就排队
    try:
        with conn.cursor() as cur:
            if SLOW_QUERY_SECONDS > 0:                       # 单独 SLEEP，保证无论是否命中都占住连接
                cur.execute("SELECT SLEEP(%s)", (SLOW_QUERY_SECONDS,))
            cur.execute("SELECT id, name, price, stock FROM product WHERE id = %s", (product_id,))
            return cur.fetchone()
    finally:
        conn.close()               # 归还连接给池子（不是真关闭）


def get_query_count() -> int:
    return _query_count


def reset_query_count() -> None:
    global _query_count
    with _count_lock:
        _query_count = 0


def init_db() -> None:
    """建库、建表、塞测试数据，可重复执行。"""
    root = pymysql.connect(host=MYSQL_HOST, port=MYSQL_PORT, user=MYSQL_USER,
                           password=MYSQL_PASSWORD, charset="utf8mb4")
    try:
        with root.cursor() as cur:
            cur.execute(f"CREATE DATABASE IF NOT EXISTS {MYSQL_DB} DEFAULT CHARACTER SET utf8mb4")
        root.commit()
    finally:
        root.close()
    conn = pymysql.connect(host=MYSQL_HOST, port=MYSQL_PORT, user=MYSQL_USER,
                           password=MYSQL_PASSWORD, database=MYSQL_DB, charset="utf8mb4")
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS product (
                    id INT PRIMARY KEY, name VARCHAR(128) NOT NULL,
                    price INT NOT NULL, stock INT NOT NULL
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
            cur.executemany(
                "REPLACE INTO product (id, name, price, stock) VALUES (%s, %s, %s, %s)",
                [(1001, "AirPods Pro 2", 1899, 100),
                 (1002, "iPhone 16 Pro", 7999, 50),
                 (1003, "MacBook Air M4", 8999, 30)],
            )
        conn.commit()
    finally:
        conn.close()
    print(f"✅ 数据库 {MYSQL_DB} 初始化完成（product 表已就绪，3 条测试数据）")


if __name__ == "__main__":
    init_db()
```

`src/v01_no_cache.py`（无缓存基线）：

```python
"""
V1 · 裸查询基线（无缓存，真实 MySQL）
商品详情查询直接打真实 MySQL，没有任何缓存。建立基线，让"每个请求都压在 DB 上"显形。

运行前先初始化：.venv/bin/python src/db.py
然后：        .venv/bin/python src/v01_no_cache.py

观察重点：
    1. DB 被查询的总次数 == 请求总数吗？（无缓存时 50 请求 = 50 次查询）
    2. 50 个并发受限于连接池，总耗时多久？
    3. 1 秒内来 1 万次请求，DB 扛得住吗？
"""

import time
from concurrent.futures import ThreadPoolExecutor

import db   # 共享数据源模块（真实 MySQL）

CONCURRENCY = 50
HOT_PRODUCT_ID = 1001


def get_product(product_id: int) -> dict | None:
    """对外业务接口。V1 里直接转手问 DB。"""
    return db.query_product(product_id)


def main():
    db.reset_query_count()
    print(f"模拟：{CONCURRENCY} 个用户同时查看热门商品 {HOT_PRODUCT_ID}")
    print(f"（DB 单次查询约 {db.SLOW_QUERY_SECONDS*1000:.0f}ms，连接池上限 {db.DB_POOL_SIZE}）\n")

    start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        results = list(pool.map(lambda _: get_product(HOT_PRODUCT_ID), range(CONCURRENCY)))
    elapsed = time.perf_counter() - start

    ok = sum(1 for r in results if r is not None)
    print(f"成功拿到商品的请求：{ok}/{CONCURRENCY}")
    print(f"示例返回：{results[0]}")
    print(f"DB 实际被查询次数：{db.get_query_count()}   <-- 注意：等于请求数，一次都没省")
    print(f"总耗时：{elapsed:.2f}s   （受限于连接池，请求只能 {db.DB_POOL_SIZE} 个一批地排队过）")
    # ...（末尾打印通往 V2 的思考题，略）


if __name__ == "__main__":
    main()
```

### 运行示例

```
$ .venv/bin/python src/db.py
✅ 数据库 redis_learning 初始化完成（product 表已就绪，3 条测试数据）

$ .venv/bin/python src/v01_no_cache.py
模拟：50 个用户同时查看热门商品 1001
（DB 单次查询约 200ms，连接池上限 10）

成功拿到商品的请求：50/50
示例返回：{'id': 1001, 'name': 'AirPods Pro 2', 'price': 1899, 'stock': 100}
DB 实际被查询次数：50   <-- 注意：等于请求数，一次都没省
总耗时：1.06s   （受限于连接池，请求只能 10 个一批地排队过）
```

**关键观察**：50 个请求查的是**完全相同**的商品数据，真实 MySQL 却被实打实查了 **50 次**。其中 49 次纯属浪费——数据根本没变。总耗时 1.06s 几乎全花在连接池排队上（50 请求 ÷ 10 连接 × 200ms ≈ 1s）。把 `CONCURRENCY` 调到 5000，DB 连接池直接被打爆。

### 原理以及特点

V1 这种"每次请求都直达数据源"的模式，本质问题是：**它没有利用"数据会被重复读取"这个事实**。

互联网业务有一个极强的特征——**读多写少 + 热点集中**。一个爆款商品，可能 1 秒内被几万人查看，但它的价格、名称几小时才变一次。让每一次查看都去问一遍 DB，等于反复问同一个问题、反复得到同一个答案。

- **优点**：实现极简，数据永远最新（没有缓存就没有"缓存与 DB 不一致"的烦恼）。
- **缺点**：
  - DB 查询次数 = 请求次数，热点数据把 DB 压力放大成百上千倍；
  - 响应慢，用户每次都要等完整的 DB 查询；
  - DB 是最贵、最难水平扩展的一环，却被迫扛了所有流量。

这正是缓存要解决的：**把"重复的、变化不频繁的"读结果，暂存在一个又快又能扛高并发的地方（Redis）**，让绝大多数请求在缓存层就被满足，DB 只在缓存没有时才被打扰。

> 思考题（带着这些进 V2）：
> 1. 50 个请求查同一个商品却查了 DB 50 次——理想情况下，DB 应该只被查**几次**？
> 2. 把结果缓存进 Redis 后，第 2 个及以后的请求耗时会从 200ms 降到什么量级？总耗时会变成多少？
> 3. "先查缓存、没有再查 DB、查到回填缓存"——这个套路叫 Cache-Aside。如果有人专门拿**不存在的商品 id**来查，缓存里永远不会有，会发生什么？

---

## V2：加缓存（Cache-Aside 旁路缓存）→ 撞上「缓存穿透」

V1 让我们亲眼看到了痛：50 个请求查同一个商品，真实 MySQL 被实打实查了 50 次，其中 49 次纯属浪费。V2 就来治这个痛——在 DB 前面加一层 **Redis 缓存**。

### Cache-Aside 是什么

最经典、用得最多的缓存套路，中文叫「旁路缓存」。核心就三步，全在 `get_product()` 里：

1. **先问缓存**：命中就直接返回，DB 一点都不碰；
2. **没命中再查 DB**：这一步才是那个 ~200ms 的慢查询；
3. **回填缓存**：把查到的结果写回 Redis，下次同样的 id 在第 ① 步就被拦下。

它的名字「旁路」就体现在：缓存不在 DB 的必经之路上，而是“路边”的一个快查点。应用自己负责查缓存、查 DB、回填——缓存只是个被动的 key-value 仓库，不知道 DB 的存在。这跟“应用只管读写缓存、由缓存自己去同步 DB”的另一套模式（read/write-through）正好相反，我们这个专题一直用 Cache-Aside，因为它最简单、最通用。

为什么这招对互联网业务特别灵？因为业务有个极强的特征——**读多写少 + 热点集中**。一个爆款商品 1 秒被几万人查看，但价格名称几小时才变一次。让绝大多数“重复的读”停在又快又能扛并发的 Redis 上，DB 只在缓存没有时才被打扰。

### 新增了一个共享模块 `src/cache.py`

和 `db.py` 是一对：`db.py` 提供“慢而权威”的真实 MySQL，`cache.py` 提供“快而易失”的真实 Redis。后面 V3–V7 都复用它。

```python
"""cache.py · 共享 Redis 连接模块（从 V2 起贯穿整个专题）"""
import os
import redis

REDIS_HOST = os.environ.get("Localhost_REDIS_HOST", "127.0.0.1")
REDIS_PORT = int(os.environ.get("Localhost_REDIS_PORT", "6379"))
REDIS_DB = int(os.environ.get("Localhost_REDIS_DB", "0"))

# 全局共享客户端。redis-py 内部自带连接池，多线程共用这一个对象是安全的。
# decode_responses=True → 存取都是 str，方便配合 json.dumps / json.loads。
r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB, decode_responses=True)
```

> 小知识：我们把商品 dict 用 `json.dumps` 序列化成字符串存进一个普通 String key（如 `product:1001`）。Redis 还有 Hash 类型也能存对象，各有取舍——先用最直观的 JSON String，类型选型的话题留给后面。

### 核心代码 `src/v02_cache_aside.py`

```python
import json, time
from concurrent.futures import ThreadPoolExecutor
import cache   # 共享 Redis 连接（真实 Redis）
import db      # 共享数据源模块（真实 MySQL）

CACHE_TTL = 60               # 缓存过期时间（秒）。先记住有这东西，TTL 的坑 V3/V4 再算
KEY_PREFIX = "product:"      # key 命名习惯：用前缀做业务区分，如 product:1001

def cache_key(product_id: int) -> str:
    return f"{KEY_PREFIX}{product_id}"

def get_product(product_id: int) -> dict | None:
    """Cache-Aside 三步走。和 V1 的唯一区别：DB 从“第一站”降级成“缓存没有时才去的备胎”。"""
    key = cache_key(product_id)

    # ① 先问缓存
    cached = cache.r.get(key)
    if cached is not None:
        return json.loads(cached)          # 命中：直接返回，DB 一点没碰

    # ② 缓存没有，回源查真实 DB（那个 ~200ms 的慢查询）
    product = db.query_product(product_id)

    # ③ 把结果写回缓存，下次同样的 id 在第 ① 步就被拦下
    if product is not None:
        cache.r.set(key, json.dumps(product), ex=CACHE_TTL)
    # ⚠️ 注意这个 if：只有“查到了”才回填。查不到（None）什么都没存——
    #    这正是下面「缓存穿透」的根源，记住这一行。
    return product
```

### 运行示例

```
$ .venv/bin/python src/v02_cache_aside.py
============================================================
Part A · 缓存的威力（热门商品 = 存在的 id）
============================================================
第 1 个请求（冷缓存）：缓存里没有 → 回源查 DB → 写回 Redis
  此刻 DB 查询次数：1  ← 就这一次
紧接着 50 个用户并发查看同一商品（缓存已热）：
  这 50 个并发里真正打到 DB 的：0 个  ← 期望是 0
  DB 总查询次数：1     （V1 同场景是 51 次）
  这批并发总耗时：0.012s   （V1 约 1s，几乎全耗在连接池排队）

============================================================
Part B · 一个能绕过缓存的攻击：反复查【不存在】的商品
============================================================
连续 5 次查询不存在的商品 id=9999：
  第 1 次 → 返回 None，DB 累计被查 1 次
  ...
  第 5 次 → 返回 None，DB 累计被查 5 次
  5 次查询，DB 被打了 5 次——缓存一次都没挡住！
```

### 关键观察

**Part A——缓存的威力**：同样是 50 个并发查同一个热门商品，DB 查询次数从 V1 的 **50 次塌到 1 次**，总耗时从 **~1s 降到 0.012s**（快了约 80 倍）。那 49 次重复的慢查询全被 Redis 在第 ① 步拦下了，DB 只在第一次被打扰一下。这就是缓存对“读多写少 + 热点集中”场景的降维打击。

> 代码里有个小心机：Part A 先单独跑了 1 个“预热”请求再放并发，是为了**干净地展示稳态命中**（DB 恰好 1 次）。如果不预热、50 个请求一上来同时扑向冷缓存，会有不止 1 个在第一瞬间一起扑空、一起回源——那是另一个坑「缓存击穿」，我们 V4 专门算账。

### 原理以及特点

- **优点**：DB 压力骤降；热点读几乎都在内存里完成，响应从百毫秒级降到毫秒级；Redis 单机就能扛十万级 QPS，正好补上“DB 难水平扩展”的短板。
- **代价（缓存的“原罪”，后面慢慢还）**：
  - **一致性**：DB 改了、缓存还是旧的（这就是“缓存与 DB 不一致”，更新策略是个大专题）；
  - **TTL 取舍**：TTL 太长→数据陈旧，太短→命中率低、回源变多；
  - **多了一个要维护、会挂、会满的组件**（V6 持久化、V7 高可用就是来兜底它的）。

### 新痛点：缓存穿透（Cache Penetration）

**Part B** 故意查一个**不存在**的商品 `id=9999`，连查 5 次——结果 DB 被实打实打了 **5 次**，缓存一次都没挡住。

根因就在那行 `if product is not None`：DB 查不到返回 `None`，而我们**只缓存“查到了”的结果，从不缓存“查不到”这个事实**。于是不存在的 id 永远在第 ① 步扑空、每次都穿透到 DB。

这就是**缓存穿透**：缓存对它彻底失效。如果有人专门拿不存在的 id（甚至每次换不同的 id）来狂刷，请求会像穿过一层纸一样直达 DB，缓存形同虚设——这是一种很常见的攻击/打爆 DB 的方式。

> 思考题（带着这些进 V3）：
> 1. 既然“查不到”也是个确定的答案，能不能把它也缓存起来（空值 + 短 TTL）？这样不存在的 id 第 2 次起也能在缓存层被拦下。会有什么副作用（比如缓存被一堆没用的空值塞满、或真数据刚写入时被短暂的空值挡住）？
> 2. 如果攻击者每次都用**不同的**不存在 id（9999、9998、9997…），缓存空值挡得过来吗？能不能在**进缓存之前**就判断“这个 id 根本不可能存在”？（提示：布隆过滤器）
> 3. 这两招——**缓存空值** 和 **布隆过滤器**——正是 V3 的主角。

---

## V3：缓存空值 + 布隆过滤器，治「缓存穿透」→ 撞上「缓存击穿」

V2 的 Part B 暴露了第一个新痛点：查一个**不存在**的商品 id，DB 查不到返回 `None`，而我们那行 `if product is not None` 只缓存"查到了"的结果、从不缓存"查不到"这件事。于是不存在的 id 每次都穿透到 DB——这就是**缓存穿透**。有人专门拿不存在的 id 狂刷，缓存形同虚设。

V3 用**两招叠加**把它堵上。关键是要理解：这两招治的不是同一种攻击姿势。

- **招式一 · 缓存空值**：DB 查不到时，往缓存写一个**空值标记 + 短 TTL**。下次**同一个** id 再来就在缓存层被拦下。→ 治"反复刷同一个不存在 id"。
- **招式二 · 布隆过滤器**：在**进缓存之前**就判断"这个 id 根本不可能存在"，是就直接挡掉，连 Redis 都不查。→ 治"每次换不同 id 来刷"——这正是空值缓存挡不住的那种。

### 招式一为什么不够：换 id 攻击

空值缓存的前提是"同一个 key 会被重复查"。可如果攻击者每次都用**不同的**不存在 id（9000、9001、9002…），每个新 id 都是一次全新的 miss，空值缓存一个都拦不住，DB 照样被一次次打；更糟的是 Redis 里还堆出一大堆没用的空值 key，攻击者拿无穷多 id 就能把内存撑爆。所以必须在**进缓存之前**就有一道闸门——这就是布隆过滤器。

### 布隆过滤器：一个"只会误判'在'、绝不误判'不在'"的集合

布隆过滤器本质是一条很长的二进制位（这里直接用 Redis 的一个 key 当 bitmap，`SETBIT`/`GETBIT` 操作它）：

- `add(x)`：用 k 个不同哈希算出 x 的 k 个 bit 位置，全置 1；
- `might_contain(x)`：同样算出 k 个位置——只要**有任意一个 bit 是 0** → x **绝对没加进来过**（无假阴性）；**k 个全是 1** → x **可能**加进来过（也可能是别人凑巧点亮的，即**假阳性**）。

这个"无假阴性"的性质对我们至关重要：我们要用它在入口挡掉"绝对不存在"的 id。布隆从结构上**不可能假阴性**——真实存在的 id 一定被放行，绝不会错杀真商品；最多偶尔假阳性放过个别坏 id，那种漏网的顶多多查一次 DB，无害。

> 我们**手搓**了这个布隆过滤器（没用任何现成库），就是为了让你看清"k 个哈希点亮 m 个 bit"的全部内幕。代码里用了**双哈希技巧**：只算一次 `md5`，从它的前 8 字节 `h1` 和后 8 字节 `h2` 派生出 k 个位置 `(h1 + i*h2) % m`，省去算 k 遍哈希。

### 示例代码

`src/v03_null_cache_bloom.py`（节选核心部分）：

```python
NULL_TTL = 10                # 空值标记的 TTL，故意比正常数据短得多
NULL_MARKER = "__NULL__"     # 一眼能认出的空值占位串（区别于真实 JSON）


class RedisBloomFilter:
    """跑在 Redis bitmap 上的布隆过滤器：无假阴性，只可能假阳性。"""
    def __init__(self, redis_client, key, size_bits, num_hashes):
        self.r, self.key, self.m, self.k = redis_client, key, size_bits, num_hashes

    def _positions(self, item):
        h = hashlib.md5(str(item).encode()).digest()
        h1 = int.from_bytes(h[:8], "big")
        h2 = int.from_bytes(h[8:], "big")
        return [(h1 + i * h2) % self.m for i in range(self.k)]   # 双哈希派生 k 个位置

    def add(self, item):
        pipe = self.r.pipeline()
        for pos in self._positions(item):
            pipe.setbit(self.key, pos, 1)
        pipe.execute()

    def might_contain(self, item):
        pipe = self.r.pipeline()
        for pos in self._positions(item):
            pipe.getbit(self.key, pos)
        return all(pipe.execute())   # 有任一 0 → 绝对不在；全 1 → 可能在


def get_product_with_bloom(product_id):
    # ⓪ 布隆闸门：绝对不存在 → 立刻返回，连 Redis 都不碰
    if not BLOOM.might_contain(product_id):
        return None
    # ① 问缓存（含空值标记）
    cached = cache.r.get(cache_key(product_id))
    if cached is not None:
        return None if cached == NULL_MARKER else json.loads(cached)
    # ② 回源 DB
    product = db.query_product(product_id)
    # ③ 回填：查到存数据(长 TTL)，查不到也存空值标记(短 TTL)
    if product is not None:
        cache.r.set(cache_key(product_id), json.dumps(product), ex=CACHE_TTL)
    else:
        cache.r.set(cache_key(product_id), NULL_MARKER, ex=NULL_TTL)
    return product
```

### 运行示例

```
Part A · 缓存空值：反复查【同一个】不存在的 id=9999
  第 1 次 → 返回 None，DB 累计被查 1 次
  ...
  第 5 次 → 返回 None，DB 累计被查 1 次
  5 次查询，DB 只被打了 1 次（V2 同场景是 5 次）。

Part B · 空值缓存的软肋：每次换一个【不同的】不存在 id
  DB 被打了 10 次  ← 每个新 id 都是一次全新的 miss，空值缓存拦不住
  顺带：Redis 里多了 10 个垃圾空值 key（攻击者可以拿无穷多 id 把内存撑爆）。

Part C · 布隆过滤器：在入口挡掉『换 id 攻击』
  被布隆当场挡掉的：10/10
  DB 被打了 0 次  ← 期望 0：根本没走到查 DB 那步
  真实商品 id=1001 是否被误挡？布隆放行 = True   ← 无假阴性，真商品绝不被错杀

Part D · 合体压测：50 并发混打『真实热点 + 不存在 id』
  DB 被打了 0 次  ← 热点命中缓存、坏 id 被布隆挡掉
  总耗时：0.017s
```

### 关键观察

- **Part A**：反复刷同一个不存在 id，DB 从 V2 的 5 次塌到 **1 次**——第 1 次回源扑空后写了空值标记，后面全被缓存拦下。
- **Part B**：换成 10 个**互不相同**的不存在 id，DB 又被打了 **10 次**，空值缓存彻底失效，还堆出 10 个垃圾 key。这就是空值缓存的边界。
- **Part C**：同样那 10 个 id，加上布隆过滤器后被**全部当场挡掉，DB 0 次**；而真实 id=1001 被正确放行（验证了无假阴性）。
- **Part D**：50 并发混打真实热点 + 不存在 id，预热后 DB **0 次**、17ms——两招合体后，穿透这条路被彻底封死。

### 原理以及特点

- **缓存空值**：实现极简（查不到也 `set` 一下），代价是①缓存里会塞进一些空值占位，②若一个 id 先被写空值、紧接着 DB 真插入了数据，用户会在空值 TTL 内短暂看到"不存在"——所以空值 TTL 要**短**，在"挡穿透"和"数据新鲜"之间取平衡。
- **布隆过滤器**：极省内存（几千个 bit 就能表示一大批 id 的存在性），入口拦截、连 Redis 查询都省掉。代价是①有**假阳性**（极少数坏 id 会漏过去，但只是多查一次 DB，无害），②**只进不出**——元素加进去就删不掉（删 bit 会误伤共享该 bit 的其他元素），所以商品下架后 bitmap 里的 bit 仍亮着，需要靠定时**整体重建**来回收。
- **适用场景**：两招通常**叠加**用——布隆挡住"绝对不存在"的海量乱 id，空值缓存兜底布隆的假阳性 + 那些"曾经存在、现已删除"的 id。

> 思考题（带着这些进 V4）：
> 1. 空值标记的 TTL 故意设得很短（10s）。设很长会怎样？如果一个 id 先被写了空值、紧接着 DB 里真插入了这条数据，用户会不会在这 10s 里持续看到"不存在"？（缓存与 DB 的一致性，又一次冒头。）
> 2. 布隆过滤器只进不出——商品下架（id 失效）后 bitmap 里那几个 bit 还亮着。想删一个元素行不行？（提示：直接清 bit 会误伤共享该 bit 的别人，所以才有 Counting Bloom 等变体；工程上更常用"定时整体重建"。）
> 3. 我们一直在**预热**热点缓存后再放并发。可如果一个热点 key 的 TTL 正好到期、缓存**瞬间消失**，而此刻上万请求同时涌来——它们会一起发现缓存没了、一起冲去查**同一条**真实存在的数据。布隆和空值缓存都拦不住（id 是真的存在），这就是 V4 的**缓存击穿**。

## V4：互斥锁 / 逻辑过期，治「缓存击穿」→ 撞上「缓存雪崩」

先把「击穿」和上一版的「穿透」彻底分清，这是 V4 的全部前提：

| | 缓存穿透（V3） | 缓存击穿（V4） |
|---|---|---|
| 查的 id | **根本不存在** | **真实存在的热点** |
| 缓存能不能建起来 | 建不起来（查不到） | 平时稳稳命中 |
| 出事的时刻 | 持续地、每次都漏 | 热点 key **过期那一瞬** |
| 谁来背锅 | 一群坏 id 慢慢磨 | 一个好 key 被并发踩塌 |
| V3 的招还管用吗 | 管用 | **全失效**（布隆/空值都拦真 id 不住） |

击穿的画面：一个热点商品平时一直命中缓存，岁月静好。某一刻它的 TTL 到期、缓存**瞬间消失**，而此刻恰好上万请求涌来——它们一起发现缓存没了、一起冲去查**同一条**真实数据。DB 被同一个 key 在一瞬间打穿、连接池排队、响应飙升。

V4 给两套**互不相同**的经典解法：

- **解法一 · 互斥锁**：miss 后先抢一把锁（`SET NX`），只有赢家去查 DB、重建缓存，其余请求**自旋等**缓存出现再读。→ DB 只挨重建那一次。代价：输家要**等**。
- **解法二 · 逻辑过期**：让热点 key **永不物理过期**（不设 Redis TTL），把"过期时间"当字段塞进 value。读到判定逻辑过期了，就抢锁、开**后台线程**异步重建，而当前请求**立刻返回手里那份旧数据、绝不等待**。代价：重建那一小会儿大家拿到的是**略旧**的数据。

一句话记牢取舍：**互斥锁＝宁可让你等，也给你最新的；逻辑过期＝宁可给你旧一点的，也绝不让你等。**

### 示例代码

完整代码见 `src/v04_mutex_logical.py`。三个核心函数（朴素基线 / 互斥锁 / 逻辑过期）：

```python
# ── 基线：V2 那套朴素读法（无锁）。Part A 用它【制造】击穿现场 ──
def get_product_naive(product_id):
    key = cache_key(product_id)
    cached = cache.r.get(key)
    if cached is not None:
        return json.loads(cached)
    # 缓存没有 → 直接回源。问题就在这：50 个线程会【同时】走到这行，一起查同一条 DB。
    product = db.query_product(product_id)
    if product is not None:
        cache.r.set(key, json.dumps(product), ex=CACHE_TTL)
    return product


# ── 解法一：互斥锁。只放一个请求去查 DB，其余等它把缓存填好再读 ──
def get_product_with_mutex(product_id):
    key = cache_key(product_id)
    cached = cache.r.get(key)
    if cached is not None:                       # ① 命中直接返回（绝大多数请求走这条快路）
        return json.loads(cached)

    lkey = lock_key(product_id)
    got_lock = cache.r.set(lkey, "1", nx=True, ex=LOCK_TTL)   # ② 未命中 → 抢锁（NX：只一个赢家）

    if got_lock:
        try:
            cached = cache.r.get(key)            # 双重检查：抢到锁后再读一次，别人可能已填好
            if cached is not None:
                return json.loads(cached)
            product = db.query_product(product_id)            # 全场只有这一次真正查 DB
            if product is not None:
                cache.r.set(key, json.dumps(product), ex=CACHE_TTL)
            return product
        finally:
            cache.r.delete(lkey)                 # 释放锁（直接 del 埋了 bug → V5 来修）
    else:
        for _ in range(100):                     # 输家：自旋等缓存出现（不是去查 DB）
            time.sleep(0.02)
            cached = cache.r.get(key)
            if cached is not None:
                return json.loads(cached)
        return db.query_product(product_id)      # 兜底，正常走不到


# ── 解法二：逻辑过期。永不物理过期，过期就异步重建、绝不阻塞 ──
def get_product_logical_expire(product_id):
    key = cache_key(product_id)
    cached = cache.r.get(key)
    if cached is None:                           # 冷启动兜底（逻辑过期假设热点已被预热）
        product = db.query_product(product_id)
        if product is not None:
            cache.r.set(key, _pack(product, LOGICAL_TTL))
        return product

    obj = json.loads(cached)
    if obj["expire_at"] > time.time():           # ① 没到逻辑过期 → 新鲜，直接返回
        return obj["data"]

    lkey = lock_key(product_id)                  # ② 已过期 → 抢锁，赢家开后台线程异步重建
    if cache.r.set(lkey, "1", nx=True, ex=LOCK_TTL):
        threading.Thread(target=_rebuild_async, args=(product_id, lkey), daemon=True).start()
    return obj["data"]                           # ③ 关键：抢没抢到锁都【立刻返回旧数据】，绝不等
```

### 运行示例

```
Part A · 缓存击穿现场：热点 key 过期的那一瞬，50 并发一起涌入（无锁）
  DB 被打了 50 次  ← 没有任何保护，几乎每个请求都各查各的 DB
  请求耗时：最慢 1079.3ms · 最快 210.3ms · 平均 644.6ms
  总墙钟：1.094s  ← DB 连接池(10) 被打满、排队，整体被拖慢

Part B · 互斥锁重建：miss 后先抢锁，只放一个去查 DB，其余等缓存填好
  DB 被打了 1 次  ← 期望 1：只有抢到锁的那个请求查了 DB
  请求耗时：最慢 232.7ms · 最快 196.2ms · 平均 210.4ms
  总墙钟：0.236s

Part C · 逻辑过期：key 永不物理过期，过期就异步重建，请求【绝不等待】
  DB 被打了 1 次  ← 期望 1：只有一个后台线程在异步重建
  请求耗时：最慢 9.9ms · 最快 0.9ms · 平均 4.7ms  ← 最慢的也极快：没有任何请求等过重建
  总墙钟：0.021s
```

### 关键观察

| | DB 被打 | 最慢请求 | 总墙钟 | 拿到的数据 |
|---|---|---|---|---|
| **A 击穿（无锁）** | **50 次** | 1079ms | 1.094s | 最新 |
| **B 互斥锁** | **1 次** | 232ms（输家等了 ~200ms） | 0.236s | 最新 |
| **C 逻辑过期** | **1 次** | **9.9ms**（没人等） | 0.021s | **短暂旧数据** |

- **A→B**：DB 从 **50 次塌到 1 次**——锁把"同一刻只放一个请求重建"落地，击穿被堵住。但看最慢耗时 232ms：输家们**实打实地等**了赢家那 ~200ms 的 DB 查询。
- **B→C**：DB 同样是 **1 次**，但最慢请求从 232ms 掉到 **9.9ms**——逻辑过期版**没有任何请求等过重建**（重建被甩给后台线程）。代价藏在"拿到的数据"那列：这 50 个请求拿到的都是**重建前的旧数据**，要等后台线程跑完，下一批请求才看到新值。

### 原理以及特点

- **互斥锁**：实现直白（`SET NX` 抢锁 + 自旋等待 + `finally` 释放），保证**强一致**——大家最终都拿到最新数据。缺点：①输家**阻塞等待**，热点 key 重建的那一两百毫秒里响应变慢；②锁本身在分布式下有坑（见思考题 2，V5 专治）；③万一赢家崩了，靠 `LOCK_TTL` 兜底自动释放，但 TTL 设多长又是新权衡。
- **逻辑过期**：用"空间/一致性换时间"——key 常驻内存、永不物理过期，请求**永不阻塞**，吞吐和响应最稳。缺点：①重建窗口内返回**旧数据**（不能用于强一致场景，如库存、余额）；②实现更复杂（要包 `expire_at`、起后台线程）；③热点 key **永远占内存**，只适合少数真·热点。
- **适用场景**：能容忍短暂旧数据、且追求极致响应（首页/榜单/详情页）→ 逻辑过期；要求数据强一致、可接受偶尔等一下 → 互斥锁。工程上也常**叠加**：互斥锁兜底正确性，逻辑过期扛热点流量。

> 思考题（带着这些进 V5）：
> 1. Part A 只删了**一个**热点 key 就这么痛。如果是**成千上万**个 key 因为当初设了相同 TTL、在同一秒集体过期呢？（这就是**缓存雪崩**——解法之一：给 TTL 加**随机抖动**，别让大家约好同一刻一起死。这样缓存三大问题 穿透/击穿/雪崩 就齐了。）
> 2. 互斥锁版释放锁用的是直接 `DEL lock`。设想：赢家查得特别慢、超过锁的 5s 自动过期，锁先自己没了 → 别人趁机抢到新锁；这时第一个赢家终于查完、一 `DEL`，删掉的是**别人**的锁。怎么修？（提示：锁的 value 放一个只有自己知道的随机令牌，删之前先核对是不是自己的——这正是 **V5 手搓分布式锁**要解决的第一刀。）
> 3. 逻辑过期版永不设 Redis TTL，热点 key **永远占内存**。冷门 key 也这么干行不行？什么样的 key 才配用逻辑过期这套重武器？
