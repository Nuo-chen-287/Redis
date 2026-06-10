"""
redis_crud/set_data.py · 往 Redis 里「存」数据（对应 Redis 命令：SET）

类比 MySQL：这就是 Redis 版的 INSERT。
    MySQL：INSERT INTO product ... 把一行写进表；
    Redis：SET key value         把一个 value 写进某个 key。

我们沿用 V2 的存法：String 类型 + json.dumps —— 把整个商品对象拍成一个 JSON 字符串，
存进 key = product:<id>。存完不设 TTL，方便你去 DataGrip 的 db0 里慢慢看。

运行：
    .venv/bin/python src/redis_crud/set_data.py
"""

import json
import os
import sys

# 本文件在 src 的二级目录里，比 cache.py 深一层。
# 把上一级 src 加进模块搜索路径，才能 import cache、复用那唯一的 Redis 连接。
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cache  # noqa: E402  复用 cache.r（全项目共用一个 Redis 连接）

# 几条示例商品。字段沿用 V2 那套：id / name / price / stock。
PRODUCTS = [
    {"id": 1001, "name": "机械键盘", "price": 299, "stock": 50},
    {"id": 1002, "name": "无线鼠标", "price": 99, "stock": 200},
    {"id": 1003, "name": "27寸显示器", "price": 1299, "stock": 30},
]


def set_product(product: dict) -> None:
    key = f"product:{product['id']}"
    # ensure_ascii=False：让中文按原样存，DataGrip 里看着是「机械键盘」而不是 \uXXXX。
    cache.r.set(key, json.dumps(product, ensure_ascii=False))
    print(f"  已写入 {key} → {product}")


def main():
    print("开始往 Redis 写入商品数据（String + JSON）...\n")
    for p in PRODUCTS:
        set_product(p)
    print(f"\n完成，共写入 {len(PRODUCTS)} 个 key。")
    print("去 DataGrip 的 db0 刷新一下，应能看到 product:1001 / 1002 / 1003。")


if __name__ == "__main__":
    main()
