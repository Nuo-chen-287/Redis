"""
redis_crud/delete_one.py · 删「指定」的缓存（对应 Redis 命令：DEL）

类比 MySQL：DELETE FROM product WHERE id = 1001——只不过这里删的是 Redis 缓存，不是 DB。
DEL 会返回它真正删掉的 key 数量：
    1  → 删成功；
    0  → 这个 key 本来就不存在。

这一步正是 Cache-Aside「写路径」里『删缓存』动作的最小原型：
以后改了 DB，就靠 DEL 把旧缓存清掉，让下次读触发回源、写回新值。

运行（默认删 1001，也可以传 id）：
    .venv/bin/python src/redis_crud/delete_one.py
    .venv/bin/python src/redis_crud/delete_one.py 1002
"""

import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cache  # noqa: E402


def delete_product(product_id) -> int:
    key = f"product:{product_id}"
    removed = cache.r.delete(key)        # 返回删掉的数量：1 或 0
    if removed == 1:
        print(f"  已删除 {key}")
    else:
        print(f"  {key} 不存在，无需删除（DEL 返回 0）")
    return removed


def main():
    product_id = sys.argv[1] if len(sys.argv) > 1 else 1001
    print(f"删除指定商品 id={product_id} 的缓存：\n")
    delete_product(product_id)


if __name__ == "__main__":
    main()
