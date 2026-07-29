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

整体顺序：**缓存三大问题（V2–V4）→ 分布式锁 / Redisson（V5）→ 持久化（V6）→ 高可用与复制一致性（V7）→ 缓存一致性（V8）→ 企业级分层拦截（V9）**。

| 版本 | 副标题 | 解决的痛点 | 新引出的痛点 |
|------|--------|-----------|-------------|
| **V1** | 裸查询基线（无缓存） | —（建立基线） | 每次都打慢 DB，DB 扛不住 |
| **V2** | 加缓存（Cache-Aside） | 热点命中，DB 压力骤降 | 查不存在的 id → **缓存穿透** |
| **V3** | 缓存空值 + 布隆过滤器 | 挡住穿透 | 热点 key 过期瞬间 → **缓存击穿** |
| **V4** | 互斥锁重建 | 只放一个请求重建 | 大量 key 同时失效 → **缓存雪崩** |
| **V5** | 手搓分布式锁 ≈ Redisson | 简易锁的隐藏 bug | （锁做对了，转向底层运维） |
| **V6** | RDB vs AOF 持久化 | 重启不丢数据 | 单点宕机服务仍会瘫 |
| **V7** | 主从同步 + 哨兵 | 自动故障转移，并看清异步复制的数据窗口 | Redis 与 MySQL 仍可能不一致 |
| **V8** | 缓存一致性 | 正确处理 DB 与缓存的双写顺序和失败 | 单一缓存层仍挡不住所有流量 |
| **V9** | 企业级分层拦截 | 限流、本地缓存、Redis、连接池与降级逐层保护 DB | （专题收尾） |

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
  - 解决：Redis 宕机期间整个服务瘫痪（单点故障）。本机起 1 主 2 从 + 3 哨兵，手动 kill 主节点，看哨兵自动选新主、服务自愈；再冻结副本制造复制延迟，验证异步副本不会永远和主节点一样新。

- **V8 · 缓存一致性：DB 变了，缓存怎么办**
  - 解决：补齐写路径，对比双写顺序、并发旧值回填和删除失败，最终形成可恢复的最终一致方案。

- **V9 · 企业级分层拦截链路**
  - 解决：让突发请求依次经过网关限流、本地缓存、Redis、连接池和熔断降级，用逐层计数看清每层到底拦住了什么。

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


## V6：RDB vs AOF 持久化 —— 数据能回来，服务仍会中断

V5 把缓存雪崩和分布式锁处理得更可靠，但那些方案都有一个共同前提：Redis 进程还活着。Redis 的主要工作集在内存里，`SET` 返回成功只说明写进了当前进程的内存，并不自动等于“机器重启后还能回来”。

这一版不直接杀掉日常使用的 6379 实例，而是在临时目录、随机端口启动三个真实 Redis 子进程，再用 `SIGKILL` 制造突然崩溃。我们会亲眼看到无持久化、RDB 和 AOF 的恢复边界。

### 示例代码

```python
"""
V6 · RDB vs AOF：Redis 进程崩溃后，数据到底能不能回来

V5 已经让缓存面对并发时更稳，但 Redis 仍然把主要数据放在内存里。进程一旦崩溃，
没有落盘的数据就会消失。V6 用三个隔离的真实 Redis 实例依次演示：

    1. 不开启持久化：崩溃重启后，刚写入的数据全部消失；
    2. RDB：快照之前的数据回来，快照之后的新数据丢失；
    3. AOF everysec：已刷盘的写命令会被重放，数据可以恢复。

脚本不会修改或停止日常使用的 6379 实例。每个实验都在临时目录、随机端口启动
独立 redis-server，结束后自动清理。

运行：
    .venv/bin/python src/v06_rdb_aof.py

观察重点：
    1. 内存里曾经 GET 到的数据，为什么重启后可能不存在？
    2. RDB 恢复的是哪个时间点，为什么快照后的 key 会丢？
    3. AOF 保存的是“写命令”而不是“内存照片”，重启时如何恢复数据？
    4. 持久化能让数据回来，但 Redis 重启期间，客户端还能正常服务吗？
"""

from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import redis

START_TIMEOUT_SECONDS = 5
AOF_FSYNC_TIMEOUT_MS = 5000


def find_free_port() -> int:
    """向操作系统申请一个当前空闲端口，避免碰到日常使用的 6379。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class IsolatedRedis:
    """管理一个只服务于当前实验的 redis-server 子进程。"""

    def __init__(self, workdir: Path, mode: str):
        self.workdir = workdir
        self.mode = mode
        self.port = find_free_port()
        self.process: subprocess.Popen | None = None
        self.workdir.mkdir(parents=True, exist_ok=True)

        executable = shutil.which("redis-server")
        if executable is None:
            raise RuntimeError("找不到 redis-server，请先安装 Redis 并确认它在 PATH 中")
        self.executable = executable

    def command(self) -> list[str]:
        appendonly = "yes" if self.mode == "aof" else "no"
        return [
            self.executable,
            "--port", str(self.port),
            "--bind", "127.0.0.1",
            "--protected-mode", "no",
            "--daemonize", "no",
            "--dir", str(self.workdir),
            "--dbfilename", "dump.rdb",
            "--save", "",  # 关闭自动快照；RDB 实验会在明确的位置手动 SAVE。
            "--appendonly", appendonly,
            "--appendfsync", "everysec",
            "--appenddirname", "appendonlydir",
            "--loglevel", "warning",
            "--logfile", str(self.workdir / "redis.log"),
        ]

    def start(self) -> redis.Redis:
        self.process = subprocess.Popen(
            self.command(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        client = redis.Redis(host="127.0.0.1", port=self.port, decode_responses=True)
        deadline = time.monotonic() + START_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                log = (self.workdir / "redis.log").read_text(errors="replace")
                raise RuntimeError(f"隔离 Redis 启动失败：\n{log}")
            try:
                if client.ping():
                    return client
            except redis.ConnectionError:
                time.sleep(0.05)
        self.stop()
        raise TimeoutError(f"Redis 在 {START_TIMEOUT_SECONDS}s 内没有启动完成")

    def crash(self) -> None:
        """使用 SIGKILL 模拟突然崩溃，避免正常 SHUTDOWN 偷偷补一次落盘。"""
        if self.process is None or self.process.poll() is not None:
            return
        os.kill(self.process.pid, signal.SIGKILL)
        self.process.wait(timeout=START_TIMEOUT_SECONDS)
        self.process = None

    def stop(self) -> None:
        if self.process is None or self.process.poll() is not None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=START_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=START_TIMEOUT_SECONDS)
        finally:
            self.process = None


def part_a_no_persistence(root: Path) -> None:
    print("=" * 72)
    print("Part A · 只有内存：写成功，不等于崩溃后还存在")
    print("=" * 72)
    server = IsolatedRedis(root / "no-persistence", mode="none")
    try:
        client = server.start()
        client.set("order:20260723", "paid")
        print("崩溃前 GET order:20260723 ->", client.get("order:20260723"))

        server.crash()
        client = server.start()
        recovered = client.get("order:20260723")
        print("崩溃重启后                  ->", recovered)
        assert recovered is None
        print("结论：没有持久化文件，内存数据随进程一起消失。")
    finally:
        server.stop()


def part_b_rdb(root: Path) -> None:
    print("\n" + "=" * 72)
    print("Part B · RDB：恢复一张旧照片，而不是崩溃前的每次写入")
    print("=" * 72)
    server = IsolatedRedis(root / "rdb", mode="rdb")
    try:
        client = server.start()
        client.set("product:1001", "snapshot-version")
        client.save()  # 把这个明确时刻作为恢复边界，实验结果才是确定的。
        rdb_path = server.workdir / "dump.rdb"
        print(f"执行 SAVE，生成 dump.rdb（{rdb_path.stat().st_size} bytes）")

        client.set("product:1002", "written-after-snapshot")
        print("快照后又写入 product:1002，随后立刻模拟崩溃")
        server.crash()

        client = server.start()
        before = client.get("product:1001")
        after = client.get("product:1002")
        print("重启后，快照前的 product:1001 ->", before)
        print("重启后，快照后的 product:1002 ->", after)
        assert before == "snapshot-version"
        assert after is None
        print("结论：RDB 恢复速度快，但两次快照之间的新写入可能丢失。")
    finally:
        server.stop()


def part_c_aof(root: Path) -> None:
    print("\n" + "=" * 72)
    print("Part C · AOF everysec：把写命令落盘，重启时重新播放")
    print("=" * 72)
    server = IsolatedRedis(root / "aof", mode="aof")
    try:
        client = server.start()
        client.set("inventory:1001", "99")

        # everysec 理论上可能丢最近约 1 秒；WAITAOF 把演示边界钉在“本地已刷盘”。
        local_fsynced, replicas_fsynced = client.execute_command(
            "WAITAOF", 1, 0, AOF_FSYNC_TIMEOUT_MS
        )
        print(
            "WAITAOF ->",
            f"本地已刷盘={local_fsynced}, 副本已刷盘={replicas_fsynced}",
        )
        assert local_fsynced == 1

        aof_files = sorted((server.workdir / "appendonlydir").glob("*"))
        print("AOF 文件 ->", ", ".join(path.name for path in aof_files))
        server.crash()

        client = server.start()
        recovered = client.get("inventory:1001")
        print("崩溃重启后 GET inventory:1001 ->", recovered)
        assert recovered == "99"
        print("结论：Redis 重放 AOF 中的写命令，恢复出崩溃前已刷盘的数据。")
    finally:
        server.stop()


def main() -> None:
    print("V6 使用随机端口启动隔离 Redis；不会改动 127.0.0.1:6379。\n")
    with tempfile.TemporaryDirectory(prefix="redis-v6-") as temp_dir:
        root = Path(temp_dir)
        part_a_no_persistence(root)
        part_b_rdb(root)
        part_c_aof(root)

    print("\n" + "-" * 72)
    print("V6 收口")
    print("-" * 72)
    print("RDB：周期性拍内存快照，文件紧凑、恢复快，但可能丢快照间隔内的数据。")
    print("AOF：记录写命令，数据更完整；文件更大，恢复时需要重放命令。")
    print("混合持久化：用 RDB 做基底、AOF 保存增量，是二者的工程折中。")
    print("\n思考题（带着这些进 V7）：")
    print("1. 数据可以从磁盘恢复，但 Redis 从崩溃到重启期间，请求由谁处理？")
    print("2. 如果准备一个副本，主节点挂了以后，客户端怎样知道该连接谁？")
    print("3. 主从复制是异步的；主刚写完就宕机，新主一定拥有最后那次写入吗？")


if __name__ == "__main__":
    main()
```

### 运行示例

```
$ .venv/bin/python src/v06_rdb_aof.py
V6 使用随机端口启动隔离 Redis；不会改动 127.0.0.1:6379。

========================================================================
Part A · 只有内存：写成功，不等于崩溃后还存在
========================================================================
崩溃前 GET order:20260723 -> paid
崩溃重启后                  -> None
结论：没有持久化文件，内存数据随进程一起消失。

========================================================================
Part B · RDB：恢复一张旧照片，而不是崩溃前的每次写入
========================================================================
执行 SAVE，生成 dump.rdb（124 bytes）
快照后又写入 product:1002，随后立刻模拟崩溃
重启后，快照前的 product:1001 -> snapshot-version
重启后，快照后的 product:1002 -> None
结论：RDB 恢复速度快，但两次快照之间的新写入可能丢失。

========================================================================
Part C · AOF everysec：把写命令落盘，重启时重新播放
========================================================================
WAITAOF -> 本地已刷盘=1, 副本已刷盘=0
AOF 文件 -> appendonly.aof.1.base.rdb, appendonly.aof.1.incr.aof, appendonly.aof.manifest
崩溃重启后 GET inventory:1001 -> 99
结论：Redis 重放 AOF 中的写命令，恢复出崩溃前已刷盘的数据。

------------------------------------------------------------------------
V6 收口
------------------------------------------------------------------------
RDB：周期性拍内存快照，文件紧凑、恢复快，但可能丢快照间隔内的数据。
AOF：记录写命令，数据更完整；文件更大，恢复时需要重放命令。
混合持久化：用 RDB 做基底、AOF 保存增量，是二者的工程折中。

思考题（带着这些进 V7）：
1. 数据可以从磁盘恢复，但 Redis 从崩溃到重启期间，请求由谁处理？
2. 如果准备一个副本，主节点挂了以后，客户端怎样知道该连接谁？
3. 主从复制是异步的；主刚写完就宕机，新主一定拥有最后那次写入吗？
```

### 原理以及特点

**RDB 保存的是某个时间点的内存快照。** Redis 通常通过后台 `BGSAVE` 派生子进程，借助操作系统的 Copy-on-Write 生成临时快照，完成后再原子替换 `dump.rdb`。文件紧凑、恢复快，适合备份和快速重启；代价是两次快照之间的新写入可能丢失。本实验用阻塞式 `SAVE` 故意钉住恢复边界：`product:1001` 在快照中，所以能回来；`product:1002` 写在快照后，所以崩溃后消失。生产环境通常用 `BGSAVE`，不会在请求路径直接执行 `SAVE`。

**AOF 保存的是写命令。** Redis 每收到一次修改，就把对应命令追加到 AOF；重启时重新执行这些命令来重建内存。常见刷盘策略是 `always`、`everysec`、`no`：越频繁刷盘，丢数据窗口越小，但写入开销越高。项目使用 `everysec`，理论上突然断电时可能丢最近约 1 秒；实验通过 `WAITAOF` 明确等到本地刷盘后再崩溃，因此恢复结果是确定的。

Redis 8 的 AOF 已经不是单个无限增长的文本文件。本次真实输出中的三个文件分别承担：

| 文件 | 作用 |
|---|---|
| `appendonly.aof.1.base.rdb` | 重写时生成的紧凑基底，采用 RDB 格式 |
| `appendonly.aof.1.incr.aof` | 基底之后新增的写命令 |
| `appendonly.aof.manifest` | 记录加载顺序与当前有效文件 |

这就是混合持久化的直观形态：RDB 负责快速恢复大块历史状态，增量 AOF 补上后续写入。AOF 开启时，Redis 重启会优先按 AOF 恢复，因为它通常比单独的 RDB 更新。

当前本机 6379 的真实配置是：RDB 自动快照规则为 `3600 1 / 300 100 / 60 10000`，AOF 关闭，落盘目录是 `/opt/homebrew/var/db/redis`。也就是说，它当前偏向“恢复快、允许丢失最近一段写入”的缓存型选择。

- **RDB 优点**：文件小、适合备份、全量恢复快，对正常写入的持续开销较低。
- **RDB 缺点**：恢复点比较粗；生成快照需要 fork，在大内存实例上会产生 CPU、内存和延迟压力。
- **AOF 优点**：数据丢失窗口更小，写入历史更细，`everysec` 是常见折中。
- **AOF 缺点**：文件和持续 I/O 通常更多，需要重写；恢复增量命令也有成本。
- **边界**：持久化不是高可用，也不是数据库备份的替代品。它解决“重启后数据能不能回来”，不解决“Redis 挂着的这段时间谁接请求”。

> 思考题（带着这些进 V7）：
> 1. 磁盘文件完好，但 Redis 进程宕机 30 秒，这 30 秒内客户端连接谁？
> 2. 准备一个实时副本后，主节点挂了，谁负责判断故障、选择新主并通知客户端？
> 3. 主从复制通常是异步的。如果主节点刚确认一次写入就宕机，新主是否一定拥有这次写入？


## V7：主从复制 + Sentinel 自动故障转移 —— 数据有人接班

V6 解决的是“Redis 重启后数据能不能回来”，但从进程崩溃到重启完成之间，客户端仍然没有服务可用。V7 把问题从“数据能否恢复”推进到“故障期间谁来接班”：提前运行副本，让它持续复制主节点；再用 Sentinel 监控故障、选择新主，并让客户端重新发现主节点。

本版用六个隔离进程把高可用和复制一致性放到一个真实现场里：1 个主节点、2 个副本、3 个 Sentinel。脚本先用 `WAIT` 确认一条写入已经到达两个副本；再冻结并断开副本，制造“主节点已返回成功、副本却没有收到”的窗口，随后杀死主节点。脚本只操作临时随机端口，不碰日常使用的 6379；由于所有进程仍在同一台 Mac 上，本实验模拟的是 Redis 进程故障，不是整台物理机断电。

### 示例代码

```python
"""
V7 · 主从复制 + Sentinel 自动故障转移

V6 解决了“Redis 重启后数据能不能回来”，但节点从崩溃到恢复期间仍然无法服务。
V7 提前准备两个副本，再用三个 Sentinel 自动完成三件事：

    1. 多个 Sentinel 达到 quorum 后，确认旧主客观下线；
    2. 选出负责人，把一个数据较新的副本提升为新主；
    3. Sentinel-aware 客户端重新发现新主并继续写入。

本版还会故意暂停并断开两个副本，在主节点已经向客户端返回成功、但写入尚未
复制出去的窗口杀死主节点。故障转移后，新主将缺少这次已确认的写入。

脚本会在临时目录和随机端口启动 1 主 + 2 副本 + 3 Sentinel，真实 SIGKILL
旧主，再观察故障转移。它不会修改或停止日常使用的 127.0.0.1:6379。

运行：
    .venv/bin/python src/v07_sentinel_failover.py

观察重点：
    1. 写入主节点后，两个副本是否都复制到了数据？
    2. SET 已经返回成功，但 WAIT=0 时，故障转移后这条数据还在吗？
    3. 旧主崩溃后，Sentinel 多久发现并提升了哪个副本？
    4. WAIT 能缩小数据丢失窗口，为什么仍不能把 Redis 变成强一致数据库？
    5. 旧主恢复后为什么变成新主的副本，而不是抢回主节点身份？
"""

from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Callable

import redis
from redis.sentinel import MasterNotFoundError, Sentinel

HOST = "127.0.0.1"
MASTER_NAME = "mymaster"
QUORUM = 2
DOWN_AFTER_MS = 1000
START_TIMEOUT_SECONDS = 10
FAILOVER_TIMEOUT_SECONDS = 20
REPLICA_ACK_TIMEOUT_MS = 200


def allocate_ports(count: int) -> list[int]:
    """一次占住多个临时端口，避免六个进程意外拿到重复端口。"""
    sockets: list[socket.socket] = []
    try:
        for _ in range(count):
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.bind((HOST, 0))
            sockets.append(sock)
        return [sock.getsockname()[1] for sock in sockets]
    finally:
        for sock in sockets:
            sock.close()


def wait_until(
    description: str,
    predicate: Callable[[], bool],
    timeout: float,
) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            if predicate():
                return
        except (redis.RedisError, OSError) as exc:
            last_error = exc
        time.sleep(0.05)
    detail = f"，最后错误：{last_error}" if last_error else ""
    raise TimeoutError(f"等待超时：{description}{detail}")


class RedisNode:
    """一个隔离的 Redis 数据节点，可作为主节点或副本启动。"""

    def __init__(
        self,
        executable: str,
        workdir: Path,
        name: str,
        port: int,
        replica_of: tuple[str, int] | None = None,
    ):
        self.executable = executable
        self.workdir = workdir / name
        self.name = name
        self.port = port
        self.replica_of = replica_of
        self.process: subprocess.Popen | None = None
        self.paused = False
        self.workdir.mkdir(parents=True, exist_ok=True)

    @property
    def log_path(self) -> Path:
        return self.workdir / "redis.log"

    def command(self) -> list[str]:
        command = [
            self.executable,
            "--port", str(self.port),
            "--bind", HOST,
            "--protected-mode", "no",
            "--daemonize", "no",
            "--dir", str(self.workdir),
            "--save", "",  # V7 只观察高可用；持久化已经在 V6 单独验证。
            "--appendonly", "no",
            "--loglevel", "notice",
            "--logfile", str(self.log_path),
        ]
        if self.replica_of is not None:
            host, port = self.replica_of
            command.extend(["--replicaof", host, str(port)])
        return command

    def client(self) -> redis.Redis:
        return redis.Redis(
            host=HOST,
            port=self.port,
            decode_responses=True,
            socket_connect_timeout=0.5,
            socket_timeout=0.5,
        )

    def start(self) -> None:
        self.process = subprocess.Popen(
            self.command(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        def ready() -> bool:
            if self.process is not None and self.process.poll() is not None:
                log = self.log_path.read_text(errors="replace")
                raise RuntimeError(f"{self.name} 启动失败：\n{log}")
            return bool(self.client().ping())

        wait_until(f"{self.name} 启动", ready, START_TIMEOUT_SECONDS)

    def crash(self) -> None:
        """SIGKILL 模拟进程突然死亡，不给节点执行优雅退出的机会。"""
        if self.process is None or self.process.poll() is not None:
            return
        os.kill(self.process.pid, signal.SIGKILL)
        self.process.wait(timeout=START_TIMEOUT_SECONDS)
        self.process = None
        self.paused = False

    def pause(self) -> None:
        """冻结副本，稳定制造它无法继续处理复制流的窗口。"""
        if self.process is None or self.process.poll() is not None:
            raise RuntimeError(f"{self.name} 尚未运行，无法暂停")
        os.kill(self.process.pid, signal.SIGSTOP)
        self.paused = True

    def resume(self) -> None:
        if self.process is None or self.process.poll() is not None:
            return
        os.kill(self.process.pid, signal.SIGCONT)
        self.paused = False

    def stop(self) -> None:
        if self.process is None or self.process.poll() is not None:
            return
        if self.paused:
            self.resume()
        self.process.terminate()
        try:
            self.process.wait(timeout=START_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=START_TIMEOUT_SECONDS)
        finally:
            self.process = None
            self.paused = False


class SentinelNode:
    """一个独立 Sentinel 进程；配置文件必须可写，因为 Sentinel 会持久化拓扑。"""

    def __init__(
        self,
        executable: str,
        workdir: Path,
        name: str,
        port: int,
        master_port: int,
    ):
        self.executable = executable
        self.workdir = workdir / name
        self.name = name
        self.port = port
        self.master_port = master_port
        self.process: subprocess.Popen | None = None
        self.workdir.mkdir(parents=True, exist_ok=True)

    @property
    def config_path(self) -> Path:
        return self.workdir / "sentinel.conf"

    @property
    def log_path(self) -> Path:
        return self.workdir / "sentinel.log"

    def write_config(self) -> None:
        self.config_path.write_text(
            "\n".join([
                f"port {self.port}",
                f"bind {HOST}",
                "protected-mode no",
                "daemonize no",
                f"dir {self.workdir}",
                f"logfile {self.log_path}",
                f"sentinel monitor {MASTER_NAME} {HOST} {self.master_port} {QUORUM}",
                f"sentinel down-after-milliseconds {MASTER_NAME} {DOWN_AFTER_MS}",
                f"sentinel failover-timeout {MASTER_NAME} 10000",
                f"sentinel parallel-syncs {MASTER_NAME} 1",
                "",
            ]),
            encoding="utf-8",
        )

    def client(self) -> redis.Redis:
        return redis.Redis(
            host=HOST,
            port=self.port,
            decode_responses=True,
            socket_connect_timeout=0.5,
            socket_timeout=0.5,
        )

    def start(self) -> None:
        self.write_config()
        self.process = subprocess.Popen(
            [self.executable, str(self.config_path), "--sentinel"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        def ready() -> bool:
            if self.process is not None and self.process.poll() is not None:
                log = self.log_path.read_text(errors="replace")
                raise RuntimeError(f"{self.name} 启动失败：\n{log}")
            return bool(self.client().ping())

        wait_until(f"{self.name} 启动", ready, START_TIMEOUT_SECONDS)

    def stop(self) -> None:
        if self.process is None or self.process.poll() is not None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=START_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=START_TIMEOUT_SECONDS)
        finally:
            self.process = None


def replication_info(node: RedisNode) -> dict:
    return node.client().info("replication")


def sentinel_peer_count(node: SentinelNode) -> int:
    peers = node.client().execute_command("SENTINEL", "SENTINELS", MASTER_NAME)
    return len(peers)


def relevant_sentinel_events(nodes: list[SentinelNode]) -> list[str]:
    """从真实日志中抽出故障转移骨架，隐藏 PID、时间戳等随机噪声。"""
    event_names = [
        "+sdown",
        "+odown",
        "+elected-leader",
        "+selected-slave",
        "+promoted-slave",
        "+switch-master",
    ]
    seen: set[str] = set()
    for node in nodes:
        log = node.log_path.read_text(errors="replace")
        for event in event_names:
            if event in log:
                seen.add(event)
    return [event for event in event_names if event in seen]


def write_through_sentinel(client: redis.Redis, key: str, value: str) -> None:
    """故障窗口内允许短暂失败；重试时连接池会重新向 Sentinel 发现主节点。"""
    deadline = time.monotonic() + FAILOVER_TIMEOUT_SECONDS
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            client.set(key, value)
            return
        except redis.RedisError as exc:
            last_error = exc
            time.sleep(0.1)
    raise TimeoutError(f"客户端在故障转移后仍无法写入：{last_error}")


def run_demo(root: Path, executable: str) -> None:
    ports = allocate_ports(6)
    master_port, replica_1_port, replica_2_port = ports[:3]
    sentinel_ports = ports[3:]

    master = RedisNode(executable, root, "master", master_port)
    replicas = [
        RedisNode(
            executable,
            root,
            "replica-1",
            replica_1_port,
            replica_of=(HOST, master_port),
        ),
        RedisNode(
            executable,
            root,
            "replica-2",
            replica_2_port,
            replica_of=(HOST, master_port),
        ),
    ]
    sentinels = [
        SentinelNode(executable, root, f"sentinel-{index}", port, master_port)
        for index, port in enumerate(sentinel_ports, start=1)
    ]
    all_redis_nodes = [master, *replicas]

    try:
        print("=" * 72)
        print("Part A · WAIT 写入：先确认两个副本都拥有安全数据")
        print("=" * 72)
        master.start()
        for replica in replicas:
            replica.start()

        wait_until(
            "两个副本完成初始同步",
            lambda: all(
                replication_info(replica).get("master_link_status") == "up"
                for replica in replicas
            ),
            START_TIMEOUT_SECONDS,
        )

        for sentinel_node in sentinels:
            sentinel_node.start()
        wait_until(
            "三个 Sentinel 互相发现",
            lambda: all(sentinel_peer_count(node) >= 2 for node in sentinels),
            START_TIMEOUT_SECONDS,
        )

        sentinel = Sentinel(
            [(HOST, port) for port in sentinel_ports],
            socket_timeout=0.5,
            decode_responses=True,
        )
        discovered_before = sentinel.discover_master(MASTER_NAME)
        sentinel_master = sentinel.master_for(MASTER_NAME, socket_timeout=0.5)

        print(f"主节点       -> {HOST}:{master_port}")
        print(f"副本节点     -> {HOST}:{replica_1_port}, {HOST}:{replica_2_port}")
        print(f"Sentinel     -> {', '.join(str(port) for port in sentinel_ports)}")
        print(f"quorum       -> {QUORUM}")
        print(f"客户端发现主 -> {discovered_before[0]}:{discovered_before[1]}")

        key_before = "order:20260724"
        sentinel_master.set(key_before, "paid-before-failover")
        acknowledged = sentinel_master.wait(2, 3000)
        replica_values = [replica.client().get(key_before) for replica in replicas]
        print(f"写入 {key_before}，WAIT 确认副本数 -> {acknowledged}")
        print(f"两个副本读到 -> {replica_values}")
        assert acknowledged == 2
        assert replica_values == ["paid-before-failover", "paid-before-failover"]

        print("\n" + "=" * 72)
        print("Part B · 制造复制延迟：SET 成功不等于副本已经收到")
        print("=" * 72)
        for replica in replicas:
            replica.pause()

        # 先断开复制连接，避免命令已经进入本机 socket 缓冲区后才杀主，
        # 让“副本没有收到这次写入”成为确定结果，而不是依赖机器快慢。
        disconnected = master.client().execute_command(
            "CLIENT", "KILL", "TYPE", "REPLICA"
        )
        wait_until(
            "主节点确认两个复制连接都已断开",
            lambda: replication_info(master).get("connected_slaves") == 0,
            START_TIMEOUT_SECONDS,
        )

        key_at_risk = "order:20260724:at-risk"
        set_result = sentinel_master.set(key_at_risk, "paid-but-not-replicated")
        at_risk_acknowledged = sentinel_master.wait(2, REPLICA_ACK_TIMEOUT_MS)
        print(f"冻结副本并断开复制连接 -> {disconnected} 个")
        print(f"主节点 SET {key_at_risk} 返回 -> {set_result}")
        print(
            f"WAIT 2 {REPLICA_ACK_TIMEOUT_MS} 确认副本数 -> "
            f"{at_risk_acknowledged}"
        )
        assert set_result is True
        assert at_risk_acknowledged == 0

        print("\n" + "=" * 72)
        print("Part C · SIGKILL 旧主：Sentinel 只能从落后的副本中选新主")
        print("=" * 72)
        failover_started = time.monotonic()
        master.crash()
        print(f"旧主 {HOST}:{master_port} 已被 SIGKILL")
        for replica in replicas:
            replica.resume()

        def discover_new_master() -> bool:
            try:
                return sentinel.discover_master(MASTER_NAME)[1] != master_port
            except MasterNotFoundError:
                return False

        wait_until(
            "Sentinel 完成故障转移",
            discover_new_master,
            FAILOVER_TIMEOUT_SECONDS,
        )
        discovered_after = sentinel.discover_master(MASTER_NAME)
        failover_seconds = time.monotonic() - failover_started
        new_master_port = discovered_after[1]
        new_master = next(node for node in replicas if node.port == new_master_port)
        remaining_replica = next(node for node in replicas if node.port != new_master_port)

        wait_until(
            "剩余副本改为复制新主",
            lambda: (
                replication_info(remaining_replica).get("master_port") == new_master_port
                and replication_info(remaining_replica).get("master_link_status") == "up"
            ),
            FAILOVER_TIMEOUT_SECONDS,
        )
        events = relevant_sentinel_events(sentinels)
        print("Sentinel 事件 ->", " -> ".join(events))
        print(f"新主节点      -> {discovered_after[0]}:{new_master_port}")
        print(f"故障转移耗时  -> {failover_seconds:.2f}s")
        safe_value = new_master.client().get(key_before)
        lost_value = new_master.client().get(key_at_risk)
        print(f"WAIT=2 的安全写入 -> {safe_value}")
        print(f"WAIT=0 的窗口写入 -> {lost_value}")
        assert safe_value == "paid-before-failover"
        assert lost_value is None

        print("\n" + "=" * 72)
        print("Part D · 客户端重发现新主；旧主恢复后成为副本")
        print("=" * 72)
        key_after = "order:20260724:after"
        write_through_sentinel(sentinel_master, key_after, "paid-after-failover")
        new_master_client = new_master.client()
        acknowledged = new_master_client.wait(1, 3000)
        print(f"原 Sentinel 客户端继续写入 {key_after} -> 成功")
        print(f"当前主节点中的值 -> {new_master_client.get(key_after)}")
        print(f"剩余副本确认数   -> {acknowledged}")

        master.start()
        wait_until(
            "恢复后的旧主被改造成新主的副本",
            lambda: (
                replication_info(master).get("role") == "slave"
                and replication_info(master).get("master_port") == new_master_port
                and replication_info(master).get("master_link_status") == "up"
            ),
            FAILOVER_TIMEOUT_SECONDS,
        )
        print(
            f"旧主 {master_port} 恢复后的角色 -> replica，"
            f"复制新主 {new_master_port}"
        )
        print(f"恢复后的旧主读到新数据 -> {master.client().get(key_after)}")

    finally:
        for sentinel_node in sentinels:
            sentinel_node.stop()
        for node in all_redis_nodes:
            node.stop()


def main() -> None:
    executable = shutil.which("redis-server")
    if executable is None:
        raise RuntimeError("找不到 redis-server，请先安装 Redis 并确认它在 PATH 中")

    print("V7 使用随机端口启动 6 个隔离进程；不会改动 127.0.0.1:6379。\n")
    with tempfile.TemporaryDirectory(prefix="redis-v7-") as temp_dir:
        run_demo(Path(temp_dir), executable)

    print("\n" + "-" * 72)
    print("V7 收口")
    print("-" * 72)
    print("主从复制：提前准备拥有相近数据的副本，但默认是异步复制。")
    print("Sentinel：确认故障、选出新主、维护新拓扑，并供客户端发现当前主节点。")
    print("客户端：连接 Sentinel-aware 代理，不把主节点端口写死在业务代码中。")
    print("一致性：SET 返回只代表主节点执行成功，不代表副本已经收到。")
    print("WAIT：能等待副本确认、缩小丢失窗口，但超时也不会撤销已经执行的写入。")
    print("结论：Redis 主从复制默认是最终一致，不保证副本永远和主节点一样新。")


if __name__ == "__main__":
    main()
```

### 运行示例

```
$ .venv/bin/python src/v07_sentinel_failover.py
V7 使用随机端口启动 6 个隔离进程；不会改动 127.0.0.1:6379。

========================================================================
Part A · WAIT 写入：先确认两个副本都拥有安全数据
========================================================================
主节点       -> 127.0.0.1:53653
副本节点     -> 127.0.0.1:53654, 127.0.0.1:53655
Sentinel     -> 53656, 53657, 53658
quorum       -> 2
客户端发现主 -> 127.0.0.1:53653
写入 order:20260724，WAIT 确认副本数 -> 2
两个副本读到 -> ['paid-before-failover', 'paid-before-failover']

========================================================================
Part B · 制造复制延迟：SET 成功不等于副本已经收到
========================================================================
冻结副本并断开复制连接 -> 2 个
主节点 SET order:20260724:at-risk 返回 -> True
WAIT 2 200 确认副本数 -> 0

========================================================================
Part C · SIGKILL 旧主：Sentinel 只能从落后的副本中选新主
========================================================================
旧主 127.0.0.1:53653 已被 SIGKILL
Sentinel 事件 -> +sdown -> +odown -> +elected-leader -> +selected-slave -> +promoted-slave -> +switch-master
新主节点      -> 127.0.0.1:53654
故障转移耗时  -> 2.26s
WAIT=2 的安全写入 -> paid-before-failover
WAIT=0 的窗口写入 -> None

========================================================================
Part D · 客户端重发现新主；旧主恢复后成为副本
========================================================================
原 Sentinel 客户端继续写入 order:20260724:after -> 成功
当前主节点中的值 -> paid-after-failover
剩余副本确认数   -> 1
旧主 53653 恢复后的角色 -> replica，复制新主 53654
恢复后的旧主读到新数据 -> paid-after-failover

------------------------------------------------------------------------
V7 收口
------------------------------------------------------------------------
主从复制：提前准备拥有相近数据的副本，但默认是异步复制。
Sentinel：确认故障、选出新主、维护新拓扑，并供客户端发现当前主节点。
客户端：连接 Sentinel-aware 代理，不把主节点端口写死在业务代码中。
一致性：SET 返回只代表主节点执行成功，不代表副本已经收到。
WAIT：能等待副本确认、缩小丢失窗口，但超时也不会撤销已经执行的写入。
结论：Redis 主从复制默认是最终一致，不保证副本永远和主节点一样新。
```

### 原理以及特点

**主从复制先把“接班的数据”准备好。** 主节点接收写入，再把复制流发送给两个副本。副本默认只读，并持续保持自己的内存状态接近主节点。本实验先写入 `order:20260724`，再用 `WAIT 2 3000` 等待两个副本确认，确保后续杀主时这条数据已经存在于副本中。

**异步复制不保证副本永远和主节点一样新。** 普通 `SET` 返回成功，只表示主节点已经执行命令；它不会等副本处理完同一条复制流。本实验冻结并断开两个副本后，`SET order:20260724:at-risk ...` 仍然返回 `True`，紧接着执行 `WAIT 2 200` 却只得到 `0`。这时杀掉主节点，Sentinel 只能从两个落后的副本中选主，所以新主保留了之前 `WAIT=2` 的数据，却查不到刚刚已经返回成功的窗口写入：

```
副本停止接收复制流
        ↓
主节点执行 SET，并向客户端返回成功
        ↓
WAIT 返回 0：没有副本确认这次复制位置
        ↓
主节点突然死亡
        ↓
Sentinel 提升一个落后的副本
        ↓
客户端成功过、但只存在于旧主内存里的写入丢失
```

**`WAIT` 缩小丢失窗口，但不提供强一致。** `WAIT 2 3000` 表示等待两个副本确认当前连接此前写入对应的复制位置，最多等 3000ms；返回值是实际确认数量。它有三个必须记住的边界：

- 超时返回数量不足时，主节点上的写入**不会回滚**，业务必须自己决定重试、报错还是接受风险。
- 副本确认的是已接收并处理复制流，不代表数据已经刷入副本磁盘；磁盘持久化是 V6 的另一层问题。
- 即使已有副本确认，同时故障、网络分区和选主边界仍然存在；Redis 主从复制因此仍是最终一致模型，不会因为调用 `WAIT` 就变成多数派提交的强一致系统。

生产环境还能配置 `min-replicas-to-write` 和 `min-replicas-max-lag`：当主节点观察不到足够多、延迟处在阈值内的副本时，主动拒绝新的写入。这能避免主节点在“一个可用副本都没有”的状态下继续积累风险数据，但它检查的是副本近期是否在线，不是每条写入都完成多数派提交，所以同样只是降低 RPO，而不是消灭数据丢失窗口。

**Sentinel 是独立的控制进程，不转发业务数据。** 三个 Sentinel 持续探测主节点；一个 Sentinel 先记录 `+sdown`（主观下线），达到 quorum=2 后形成 `+odown`（客观下线）。随后 Sentinel 之间选出故障转移负责人，选择合适的副本，发送 `REPLICAOF NO ONE` 将它提升为新主，并让其他副本改为复制新主。真实日志里的事件链正好对应这条因果链：`+sdown → +odown → +elected-leader → +selected-slave → +promoted-slave → +switch-master`。

**客户端通过服务发现而不是固定端口连接主节点。** `redis-py` 的 `Sentinel(...).master_for("mymaster")` 会先向 Sentinel 询问当前主节点地址，然后直接连接 Redis 主节点。故障转移后，原来的客户端对象在连接失败时重新发现新主，因此业务代码不需要把 `51098` 写死。Sentinel 自己不是代理，正常读写不会经过 Sentinel 转发。

旧主恢复时不会抢回主节点身份。Sentinel 会把它重新配置成新主的副本，等待复制追平；这是为了避免旧主和新主同时接受写入形成脑裂。

| 组件 | 负责什么 | 不负责什么 |
|---|---|---|
| 主节点 | 接受写入、提供当前服务 | 不能独自保证故障后继续服务 |
| 副本 | 提前复制数据、准备接班 | 默认不会自动决定自己成为主 |
| Sentinel | 探测、判断故障、选主、维护拓扑、提供发现 | 不保存业务数据、不代理业务请求 |
| Sentinel-aware 客户端 | 询问当前主并在故障后重连 | 不会让已经失败的旧连接继续可用 |

- **高可用不是零中断**：本次实测故障转移耗时约 2.26 秒；真实时间受 `down-after-milliseconds`、选举和重连策略影响。
- **异步复制有数据窗口**：本实验已经真实验证，主节点返回成功后仍可能得到 `WAIT=0`，故障转移后新主缺少该写入。
- **副本读可能是旧数据**：Sentinel 不自动做负载均衡；如果应用主动读副本，需要接受复制延迟，强一致读仍应访问主节点。
- **副本不是备份**：错误的 `DEL` 也会被同步；RDB/AOF 仍然负责持久化和备份边界。
- **部署要跨故障域**：三个 Sentinel 和多个 Redis 节点如果全在同一台机器上，只能防进程故障，防不了整机断电。

V7 的完整心智模型是：

```
主节点接收写入
      ↓ 异步复制到副本
Sentinel 持续探测主节点
      ↓
quorum 达成，确认 ODOWN
      ↓
选择副本并提升为新主
      ↓
客户端重新向 Sentinel 查询主地址
      ↓
连接新主继续服务
```

到这里，Redis 自身的可靠性链路已经闭合：V6 负责单节点重启后的数据恢复，V7 负责节点故障后的自动接班，同时明确异步复制只能提供最终一致。下一版把视角从 Redis 内部移到应用数据：MySQL 已经更新时，Redis 里的旧缓存该怎么办？

> 思考题（带着这些进 V8）：
> 1. 商品库存已经在 MySQL 从 100 改成 99，但 Redis 仍缓存 100，读请求究竟应该相信谁？
> 2. 同时写 MySQL 和 Redis 时，如果第一步成功、第二步失败，换一下执行顺序就能消灭问题吗？
> 3. MySQL 与 Redis 是两个独立系统，无法直接共享一个本地事务时，我们能追求的是强一致，还是可恢复的最终一致？

---

## V8：缓存一致性

### Part A：只更新 DB，制造脏缓存基线

V2 建立了完整的 Cache-Aside 读路径，却一直没有处理写操作。只要商品数据永远不变，这条读路径就没有问题；一旦后台修改了 MySQL，Redis 里已经缓存的旧对象并不会收到通知。

Part A 先采用最省事、也最不可靠的写法：**只更新权威数据源 MySQL，完全不处理 Redis**。这一部分故意把缓存 TTL 缩短到 4 秒，让“不一致窗口”能在一次运行中完整显形。生产缓存如果设置 10 分钟 TTL，同一个问题就可能持续 10 分钟。

### 示例代码

```python
"""
V8 · 缓存一致性 · Part A：只更新 DB，故意制造脏缓存（真实 Redis + 真实 MySQL）

V2 已有完整读路径，但没有写路径。本版先采用最省事的写法：商品价格只更新 MySQL，
完全不处理 Redis。这样能建立缓存一致性的坏基线，亲眼看到两个系统同时保存不同价格。

运行前：
    .venv/bin/python src/db.py
    redis-cli ping
然后：
    .venv/bin/python src/v08_cache_consistency.py

观察重点：
    1. MySQL 已经变成新价格后，业务读取为什么仍返回旧价格？
    2. 脏数据会持续多久？TTL 到期后为什么又自动一致了？
    3. TTL 能兜底最终一致，为什么仍不能替代主动处理缓存？
"""

import json
import time

import cache
import db

PRODUCT_ID = 1001
CACHE_TTL = 4  # 实验只等 4 秒；生产 TTL 往往更长，脏数据窗口也会随之变长。
KEY_PREFIX = "consistency:product:"


def cache_key(product_id: int) -> str:
    return f"{KEY_PREFIX}{product_id}"


def get_product(product_id: int) -> dict | None:
    """沿用 V2 的 Cache-Aside 读路径。"""
    key = cache_key(product_id)
    cached = cache.r.get(key)
    if cached is not None:
        return json.loads(cached)

    product = db.query_product(product_id)
    if product is not None:
        cache.r.set(key, json.dumps(product), ex=CACHE_TTL)
    return product


def update_price_db_only(product_id: int, new_price: int) -> None:
    """Part A 故意只改权威数据源，不碰缓存。"""
    db.update_product_price(product_id, new_price)


def wait_for_cache_expiry(key: str) -> None:
    while cache.r.exists(key):
        time.sleep(0.05)


def main() -> None:
    original = db.query_product(PRODUCT_ID)
    if original is None:
        raise RuntimeError(f"商品 {PRODUCT_ID} 不存在，请先运行 .venv/bin/python src/db.py")

    key = cache_key(PRODUCT_ID)
    old_price = original["price"]
    new_price = old_price + 100

    try:
        cache.r.delete(key)
        db.reset_query_count()

        print("=" * 68)
        print("阶段 1 · 预热缓存：MySQL 与 Redis 目前一致")
        print("=" * 68)
        warmed = get_product(PRODUCT_ID)
        print(f"MySQL 商品价格      -> {old_price}")
        print(f"首次业务读取        -> {warmed['price']}（cache miss，回源并写入 Redis）")
        print(f"Redis TTL           -> {cache.r.ttl(key)} 秒")
        print(f"DB 查询次数         -> {db.get_query_count()}")

        print("\n" + "=" * 68)
        print("阶段 2 · 只更新 MySQL：缓存开始变脏")
        print("=" * 68)
        update_price_db_only(PRODUCT_ID, new_price)
        authoritative = db.query_product(PRODUCT_ID)
        stale = get_product(PRODUCT_ID)
        print(f"MySQL 最新价格      -> {authoritative['price']}")
        print(f"业务接口读取        -> {stale['price']}（cache hit，仍是旧值）")
        print(f"Redis 剩余 TTL      -> {cache.r.ttl(key)} 秒")
        print(f"DB 查询次数         -> {db.get_query_count()}（业务读取命中缓存，没有回源）")
        assert authoritative["price"] == new_price
        assert stale["price"] == old_price

        print("\n" + "=" * 68)
        print("阶段 3 · 等 TTL 到期：下一次读取才恢复一致")
        print("=" * 68)
        wait_for_cache_expiry(key)
        fresh = get_product(PRODUCT_ID)
        print(f"缓存到期后的读取    -> {fresh['price']}（cache miss，重新查询 MySQL）")
        print(f"DB 查询次数         -> {db.get_query_count()}")
        assert fresh["price"] == new_price

        print("\n结论：只更新 DB 只能依赖 TTL 最终一致；TTL 有多长，旧值最多就可能暴露多久。")
    finally:
        db.update_product_price(PRODUCT_ID, old_price)
        cache.r.delete(key)
        print(f"实验清理：MySQL 价格已恢复为 {old_price}，实验缓存已删除。")

    print("\n思考题（带着这些进 Part B）：")
    print("1. 更新 DB 后立刻删除缓存，能不能把脏数据窗口从 4 秒缩短到下一次读取？")
    print("2. 为什么业界通常选择删除缓存，而不是再写一遍缓存？")
    print("3. 如果先删除缓存、再更新 DB，中间恰好进来一个读请求，它会回填什么值？")


if __name__ == "__main__":
    main()
```

### 运行示例

```text
$ .venv/bin/python src/v08_cache_consistency.py
====================================================================
阶段 1 · 预热缓存：MySQL 与 Redis 目前一致
====================================================================
MySQL 商品价格      -> 1899
首次业务读取        -> 1899（cache miss，回源并写入 Redis）
Redis TTL           -> 4 秒
DB 查询次数         -> 1

====================================================================
阶段 2 · 只更新 MySQL：缓存开始变脏
====================================================================
MySQL 最新价格      -> 1999
业务接口读取        -> 1899（cache hit，仍是旧值）
Redis 剩余 TTL      -> 4 秒
DB 查询次数         -> 2（业务读取命中缓存，没有回源）

====================================================================
阶段 3 · 等 TTL 到期：下一次读取才恢复一致
====================================================================
缓存到期后的读取    -> 1999（cache miss，重新查询 MySQL）
DB 查询次数         -> 3

结论：只更新 DB 只能依赖 TTL 最终一致；TTL 有多长，旧值最多就可能暴露多久。
实验清理：MySQL 价格已恢复为 1899，实验缓存已删除。
```

### 原理以及特点

缓存不是 MySQL 的自动副本。MySQL 执行 `UPDATE` 时只修改自己的数据页和事务日志，它不知道 Redis 里存在 `consistency:product:1001`；Redis 也不会主动监听 MySQL。因此写操作完成后，两个系统可以同时保存不同版本的数据。

读请求仍然优先访问 Redis。只要旧 key 还存在，它就会在缓存命中分支直接返回，根本没有机会查询已经更新的 MySQL。TTL 到期后 key 被 Redis 删除，下一次读取才会 cache miss、回源并把新价格写回来。这属于**依赖过期时间兜底的最终一致**。

- **优点**：写路径最简单；Redis 故障不会阻止 MySQL 更新；TTL 到期后最终能够自行恢复。
- **缺点**：TTL 期间持续返回旧数据；TTL 越长，最坏不一致窗口越长；TTL 太短又会降低命中率、增加 DB 压力。
- **适用场景**：能容忍较长延迟的弱一致数据，例如非关键推荐结果或低实时性配置。
- **不适用场景**：价格、库存、权限、订单状态等用户会立即验证或涉及业务决策的数据。

> 思考题（带着这些进 Part B）：
> 1. 更新 DB 后立刻删除缓存，能否把脏数据窗口从“等 TTL”缩短成“等下一次读取”？
> 2. 为什么 Cache-Aside 的写路径通常选择删除缓存，而不是同时更新缓存？
> 3. 如果执行顺序变成“先删除缓存、再更新 DB”，两步之间进入的读请求会从 MySQL 读到新值还是旧值？

### Part B：先更新 MySQL，再删除 Redis

Part A 的问题是写路径只修改 MySQL，Redis 中的旧副本只能等待 TTL 自然过期。Part B 补齐 Cache-Aside 最常用的写策略：**先提交 MySQL 事务，再删除对应的缓存 key**。

删除后，Redis 暂时没有这条商品数据。下一次读取按照原有 Cache-Aside 读路径回源 MySQL，拿到新值并重新缓存。因此我们不需要在写请求里构造、序列化并维护另一份商品对象。

### 新增代码

以下代码继续位于同一个 `src/v08_cache_consistency.py`：

```python
def update_price_db_then_delete(product_id: int, new_price: int) -> None:
    """Part B：先提交权威数据，再删除可以重新生成的缓存副本。"""
    db.update_product_price(product_id, new_price)
    cache.r.delete(cache_key(product_id))


def part_b_db_then_delete() -> None:
    original = db.query_product(PRODUCT_ID)
    if original is None:
        raise RuntimeError(f"商品 {PRODUCT_ID} 不存在，请先运行 .venv/bin/python src/db.py")

    key = cache_key(PRODUCT_ID)
    old_price = original["price"]
    new_price = old_price + 100

    try:
        cache.r.delete(key)
        warmed = get_product(PRODUCT_ID)
        db.reset_query_count()

        print("\n" + "#" * 68)
        print("Part B · 更新 MySQL，再删除 Redis")
        print("#" * 68)
        print(f"更新前业务读取      -> {warmed['price']}（旧值已在 Redis）")
        print(f"更新前缓存存在      -> {cache.r.exists(key)}")

        update_price_db_then_delete(PRODUCT_ID, new_price)
        print(f"MySQL 已更新        -> {new_price}")
        print(f"更新后缓存存在      -> {cache.r.exists(key)}（DEL 已移除旧副本）")

        first = get_product(PRODUCT_ID)
        first_query_count = db.get_query_count()
        second = get_product(PRODUCT_ID)
        print(f"更新后第一次读取    -> {first['price']}（cache miss，回源新值）")
        print(f"更新后第二次读取    -> {second['price']}（cache hit）")
        print(f"两次读取的 DB 查询数 -> {db.get_query_count()}（只有第一次回源）")
        assert first["price"] == new_price
        assert second["price"] == new_price
        assert first_query_count == 1
        assert db.get_query_count() == 1

        print("\n结论：先更新 DB 再删除缓存，把脏数据窗口缩短到了下一次读取重建缓存之前。")
    finally:
        db.update_product_price(PRODUCT_ID, old_price)
        cache.r.delete(key)
        print(f"实验清理：MySQL 价格已恢复为 {old_price}，实验缓存已删除。")
```

### 运行示例

```text
####################################################################
Part B · 更新 MySQL，再删除 Redis
####################################################################
更新前业务读取      -> 1899（旧值已在 Redis）
更新前缓存存在      -> 1
MySQL 已更新        -> 1999
更新后缓存存在      -> 0（DEL 已移除旧副本）
更新后第一次读取    -> 1999（cache miss，回源新值）
更新后第二次读取    -> 1999（cache hit）
两次读取的 DB 查询数 -> 1（只有第一次回源）

结论：先更新 DB 再删除缓存，把脏数据窗口缩短到了下一次读取重建缓存之前。
实验清理：MySQL 价格已恢复为 1899，实验缓存已删除。
```

### 原理以及特点

必须先更新 DB，再删除缓存。`db.update_product_price()` 成功返回时 MySQL 事务已经提交；如果 DB 更新失败，代码不会执行后面的 `DEL`，原缓存仍然对应旧 DB 状态。如果顺序反过来，在 `DEL` 与数据库提交之间进入的读请求可能从旧 DB 读取数据，并把旧值重新写回缓存。

选择删除而不是更新缓存还有一个并发优势。两个写请求直接更新缓存时，数据库提交顺序和 Redis 写入顺序可能相反，导致较早的值最后覆盖较新的值；两个写请求都执行 `DEL` 时，顺序无关紧要，最终状态都是“缓存不存在”。

- **优点**：实现短；`DEL` 幂等；缓存按需重建；绝大多数业务使用这一方案已经足够。
- **代价**：更新后的第一次读取会 cache miss，需要承担一次 DB 查询。
- **一致性级别**：显著缩短脏数据窗口，但 MySQL 与 Redis 没有共享事务，因此仍不是强一致。

这个方案还留有一条概率较低、但真实存在的并发时序：

```text
t1  读请求缓存未命中，开始查询旧 DB
t2  写请求更新 DB 为新值
t3  写请求删除缓存
t4  更早的读请求才结束查询，把旧值写回缓存
```

此时旧值是在 `DEL` **之后**才被写回的，第一次删除自然清理不到它。延迟双删会在更新 DB 并第一次删除后等待一个覆盖正常读请求耗时的间隔，再执行第二次 `DEL`，尝试清走这种晚到的旧值。这是 Part C 要稳定复现并处理的痛点。

延迟双删也不是最终保证：等待多久需要估算，进程可能在第二次删除前崩溃，Redis 故障也可能让两次删除都失败。因此后面仍要讨论可重试的缓存失效，而不是把 `sleep + DEL` 当成分布式事务。

> 思考题（带着这些进 Part C）：
> 1. 上述时序中，为什么读请求能够在 DB 更新前拿到旧值，却在 `DEL` 之后才写缓存？
> 2. 第二次删除至少要晚于哪个动作，才能清掉旧值回填？
> 3. 如果执行第二次删除的应用进程崩溃，谁来继续完成这次缓存失效？

### Part C：旧值晚回填、延迟双删与删除重试

Part B 的“更新 DB → 删除缓存”解决了绝大多数不一致，但第一次删除只能清理**当时已经存在**的 key。如果一个更早开始的读请求已经从旧 DB 拿到了数据，却在第一次 `DEL` 之后才执行缓存回填，旧值仍会重新出现在 Redis。

Part C 用两个 `threading.Event` 固定这条并发顺序，不再依赖线程是否碰巧撞上：读线程拿到旧值后暂停；写线程更新 DB 并删除缓存；随后才允许读线程把旧值写回。确认旧值复活后，实验延迟 0.3 秒执行第二次删除，再由下一次读取回源新 DB。

### 新增代码

以下代码继续追加在同一个 `src/v08_cache_consistency.py`：

```python
def read_then_fill_late(
    product_id: int,
    db_read_done: threading.Event,
    allow_cache_fill: threading.Event,
    outcome: dict,
) -> None:
    """先读到旧 DB，再暂停到写请求删完缓存后才回填。"""
    try:
        product = db.query_product(product_id)
        outcome["product"] = product
        db_read_done.set()
        if not allow_cache_fill.wait(timeout=5):
            raise TimeoutError("等待晚回填信号超时")
        if product is not None:
            cache.r.set(cache_key(product_id), json.dumps(product), ex=CACHE_TTL)
    except Exception as exc:
        outcome["error"] = exc
        db_read_done.set()


def delete_cache_with_retry(key: str, delete_fn=cache.r.delete, attempts: int = 3) -> int:
    """处理短暂 Redis 故障；返回实际执行到第几次才成功。"""
    for attempt in range(1, attempts + 1):
        try:
            delete_fn(key)
            return attempt
        except redis.RedisError:
            if attempt == attempts:
                raise
            time.sleep(0.05)
    raise AssertionError("unreachable")


def part_c_race_and_retry() -> None:
    original = db.query_product(PRODUCT_ID)
    if original is None:
        raise RuntimeError(f"商品 {PRODUCT_ID} 不存在，请先运行 .venv/bin/python src/db.py")

    key = cache_key(PRODUCT_ID)
    old_price = original["price"]
    new_price = old_price + 100

    try:
        print("\n" + "#" * 68)
        print("Part C · 延迟双删 + 删除失败重试")
        print("#" * 68)
        cache.r.delete(key)

        db_read_done = threading.Event()
        allow_cache_fill = threading.Event()
        outcome: dict = {}
        reader = threading.Thread(
            target=read_then_fill_late,
            args=(PRODUCT_ID, db_read_done, allow_cache_fill, outcome),
        )
        reader.start()
        if not db_read_done.wait(timeout=5):
            raise TimeoutError("读请求没有完成 DB 查询")
        if "error" in outcome:
            raise outcome["error"]

        print(f"慢读请求已从 MySQL 拿到 -> {outcome['product']['price']}，暂不回填")
        update_price_db_then_delete(PRODUCT_ID, new_price)
        print(f"写请求更新 MySQL 为 {new_price}，并完成第一次 DEL")
        print(f"第一次 DEL 后缓存存在 -> {cache.r.exists(key)}")

        allow_cache_fill.set()
        reader.join(timeout=5)
        if reader.is_alive():
            raise TimeoutError("读请求没有完成晚回填")
        if "error" in outcome:
            raise outcome["error"]

        resurrected = json.loads(cache.r.get(key))
        print(f"慢读请求随后回填 Redis -> {resurrected['price']}（旧值在 DEL 后复活）")
        assert resurrected["price"] == old_price

        # ponytail: 阻塞等待只为固定演示顺序；生产环境应异步调度第二次删除。
        time.sleep(DELAYED_DELETE_SECONDS)
        cache.r.delete(key)
        fresh = get_product(PRODUCT_ID)
        print(f"延迟 {DELAYED_DELETE_SECONDS:.1f}s 第二次 DEL 后读取 -> {fresh['price']}")
        assert fresh["price"] == new_price

        print("\n删除失败实验：第一次 DEL 模拟连接中断，第二次重试成功")
        db.update_product_price(PRODUCT_ID, old_price)
        cache.r.delete(key)
        get_product(PRODUCT_ID)
        db.update_product_price(PRODUCT_ID, new_price)

        calls = 0

        def fail_once_then_delete(cache_key_: str) -> int:
            nonlocal calls
            calls += 1
            if calls == 1:
                print("第 1 次 DEL          -> 模拟 Redis 连接失败")
                raise redis.ConnectionError("simulated connection failure")
            print("第 2 次 DEL          -> 重试成功")
            return cache.r.delete(cache_key_)

        # ponytail: 本地重试随进程崩溃而丢失；不能容忍时升级到 outbox、MQ 或 CDC。
        succeeded_on = delete_cache_with_retry(key, delete_fn=fail_once_then_delete)
        fresh_after_retry = get_product(PRODUCT_ID)
        print(f"删除在第 {succeeded_on} 次成功，随后读取 -> {fresh_after_retry['price']}")
        assert succeeded_on == 2
        assert fresh_after_retry["price"] == new_price
    finally:
        db.update_product_price(PRODUCT_ID, old_price)
        cache.r.delete(key)
```

### 运行示例

```text
####################################################################
Part C · 延迟双删 + 删除失败重试
####################################################################
慢读请求已从 MySQL 拿到 -> 1899，暂不回填
写请求更新 MySQL 为 1999，并完成第一次 DEL
第一次 DEL 后缓存存在 -> 0
慢读请求随后回填 Redis -> 1899（旧值在 DEL 后复活）
延迟 0.3s 第二次 DEL 后读取 -> 1999

删除失败实验：第一次 DEL 模拟连接中断，第二次重试成功
第 1 次 DEL          -> 模拟 Redis 连接失败
第 2 次 DEL          -> 重试成功
删除在第 2 次成功，随后读取 -> 1999

V8 收口：主方案是更新 DB 后删缓存；延迟双删缩小并发窗口；重试处理短暂失败。
若不能接受进程崩溃导致失效任务丢失，需要把任务持久化到 outbox/MQ，或订阅 binlog。
实验清理：MySQL 价格已恢复为 1899，实验缓存已删除。
```

### 原理以及特点

`threading.Event` 不是一致性方案，只是实验中的时间控制器。`db_read_done` 保证读线程已经拿到旧价格，`allow_cache_fill` 则保证它必须等第一次 `DEL` 完成后才能回填。这样每次运行都会得到同一条因果链：**旧 DB 读取发生在更新前，旧缓存写入却发生在删除后**。

延迟双删的第二次删除必须晚于旧读请求的回填，才有机会清掉复活的旧值。示例中的 0.3 秒只适合本机实验；生产环境通常参考接口的高分位 DB 查询耗时和回填耗时，并通过异步任务调度第二次删除，不能让请求线程长期 `sleep`。

有限重试处理的是 Redis 短暂断连、超时等瞬时故障。由于 `DEL` 是幂等操作，同一个 key 删除多次不会破坏正确数据，因此失败后重试很自然。但本地循环仍有明确上限：应用进程如果在下一次重试前崩溃，内存里的任务随进程一起消失。

不能容忍这种任务丢失时，需要把“让某个缓存 key 失效”变成可恢复事件：

- **事务 outbox**：更新业务表和插入失效事件使用同一个 MySQL 事务，worker 删除成功后再标记事件完成。
- **消息队列**：由消费者执行删除和重试；通常需要配合 outbox，避免 DB 提交成功但消息发送失败。
- **binlog/Canal/CDC**：监听已经提交的数据库变更，再异步删除或刷新缓存，适合不容易统一改造所有写入口的系统。
- **TTL**：仍然保留，作为所有主动失效手段都失败后的最后兜底。

这些方案追求的是**可恢复的最终一致**，而不是让 MySQL 与 Redis 获得一个天然的跨系统原子事务。对于余额、最终库存扣减等不能容忍旧值参与决策的数据，不应把普通缓存读取当成最终正确性依据。

V8 到这里形成完整链路：

```text
只更新 DB，旧缓存持续存在
        ↓
更新 DB 后 DEL，覆盖绝大多数场景
        ↓
延迟第二删，缩小旧读请求晚回填窗口
        ↓
失败重试，处理短暂 Redis 故障
        ↓
outbox / MQ / CDC，处理进程崩溃后的可靠恢复
        ↓
TTL 作为最后兜底
```
