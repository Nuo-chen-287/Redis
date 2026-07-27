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
