"""
redis_crud/get_one.py · 取「指定」的一条数据（对应 Redis 命令：GET）

类比 MySQL：SELECT * FROM product WHERE id = 1001。
给一个商品 id，拼出 key = product:<id>，O(1) 直接取回那一个 value。

运行（默认取 1001，也可以在命令行传 id）：
    .venv/bin/python src/redis_crud/get_one.py
    .venv/bin/python src/redis_crud/get_one.py 1002
"""

import json
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cache  # noqa: E402


def get_product(product_id) -> dict | None:
    key = f"product:{product_id}"
    value = cache.r.get(key)            # 命中返回 str；key 不存在返回 None
    if value is None:
        print(f"  {key} 不存在（缓存里没有这个 key）")
        return None
    product = json.loads(value)          # 存进去是 JSON 字符串，取出来要还原成 dict
    print(f"  {key} → {product}")
    return product


def main():
    product_id = sys.argv[1] if len(sys.argv) > 1 else 1001
    print(f"取指定商品 id={product_id}：\n")
    get_product(product_id)


if __name__ == "__main__":
    main()
