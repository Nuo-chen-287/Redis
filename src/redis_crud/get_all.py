"""
redis_crud/get_all.py · 把 Redis 里「所有」数据取出来（对应 Redis 命令：SCAN）

类比 MySQL：相当于 SELECT * FROM ...（不带 WHERE）。

但这里有个大坑，取『所有 key』有两个命令：
    KEYS *  ：一次性返回全部 key。简单，但它会【阻塞整个 Redis】直到扫完，
              key 上百万时能把线上 Redis 卡死 → 生产环境基本禁用。
    SCAN    ：游标式分批扫，每次只取一小撮，不阻塞别人 → 生产环境用这个。
redis-py 把 SCAN 封装成了 scan_iter()，当成普通迭代器用，底层自动翻页。

顺便复习上一课：只有 String 类型能直接 GET；别的类型（Hash/List/...）要用各自的命令。
所以这里先用 TYPE 看一眼类型，是 string 才 GET。

运行：
    .venv/bin/python src/redis_crud/get_all.py
"""

import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cache  # noqa: E402


def main():
    print("用 SCAN 遍历 db0 里的所有 key：\n")
    count = 0
    for key in cache.r.scan_iter(match="*"):
        key_type = cache.r.type(key)
        if key_type == "string":
            try:
                value = cache.r.get(key)
            except UnicodeDecodeError:
                # 有的 string 存的是【二进制】而非文本，比如 V3 用 SETBIT 手搓的
                # 布隆过滤器位图 bloom:product_ids。cache.py 开了 decode_responses=True，
                # 会把所有返回值按 UTF-8 解码，碰到 0x80 这种非文本字节就报错。
                # 它本来就不是给人读的，这里跳过解码、只标注一下。
                value = f"<二进制数据，不是 UTF-8 文本（如布隆过滤器位图），不强行解码>"
        else:
            value = f"<{key_type} 类型，不能用 GET 取，需要对应类型的命令>"
        print(f"  {key}  ({key_type})  →  {value}")
        count += 1

    if count == 0:
        print("  （空的——先跑一下 set_data.py 写点数据进来）")
    else:
        print(f"\n共 {count} 个 key。")


if __name__ == "__main__":
    main()
