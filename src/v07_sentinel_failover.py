"""
V7 · 主从复制 + Sentinel 自动故障转移

V6 解决了“Redis 重启后数据能不能回来”，但节点从崩溃到恢复期间仍然无法服务。
V7 提前准备两个副本，再用三个 Sentinel 自动完成三件事：

    1. 多个 Sentinel 达到 quorum 后，确认旧主客观下线；
    2. 选出负责人，把一个数据较新的副本提升为新主；
    3. Sentinel-aware 客户端重新发现新主并继续写入。

脚本会在临时目录和随机端口启动 1 主 + 2 副本 + 3 Sentinel，真实 SIGKILL
旧主，再观察故障转移。它不会修改或停止日常使用的 127.0.0.1:6379。

运行：
    .venv/bin/python src/v07_sentinel_failover.py

观察重点：
    1. 写入主节点后，两个副本是否都复制到了数据？
    2. 旧主崩溃后，Sentinel 多久发现并提升了哪个副本？
    3. 客户端为什么不需要写死新主端口，也能继续写入？
    4. 旧主恢复后为什么变成新主的副本，而不是抢回主节点身份？
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
START_TIMEOUT_SECONDS = 5
FAILOVER_TIMEOUT_SECONDS = 20


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
        print("Part A · 1 主 + 2 副本：先准备好能接班的数据节点")
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
        print("Part B · SIGKILL 旧主：Sentinel 判断、选主并重组复制关系")
        print("=" * 72)
        failover_started = time.monotonic()
        master.crash()
        print(f"旧主 {HOST}:{master_port} 已被 SIGKILL")

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
        print(f"新主保留旧数据 -> {new_master.client().get(key_before)}")

        print("\n" + "=" * 72)
        print("Part C · 客户端重发现新主；旧主恢复后成为副本")
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
    print("边界：故障转移不是零中断；异步复制也不保证最后一次写入绝不丢失。")


if __name__ == "__main__":
    main()
