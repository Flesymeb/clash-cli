"""Node scanner with quick and full modes."""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass

from clash_cli.api.client import ClashClient
from clash_cli.api.models import Node, UnlockStatus
from clash_cli.checker.unlock import check_all as check_unlock


@dataclass
class ScanResult:
    nodes: list[Node]
    best: Node | None = None


async def _test_node_delay(
    client: ClashClient, name: str, sem: asyncio.Semaphore
) -> tuple[str, int]:
    async with sem:
        delay = await client.test_delay(name)
        return name, delay


async def _test_node_unlock(
    client: ClashClient, proxy_url: str, name: str, original_node: str,
    group_name: str = "节点选择",
) -> UnlockStatus:
    """Switch to node, test unlock, switch back."""
    if not await client.switch_node(group_name, name):
        return UnlockStatus(claude="fail", chatgpt="fail", gemini="fail")

    try:
        await asyncio.sleep(0.3)
        return await check_unlock(proxy_url)
    finally:
        await client.switch_node(group_name, original_node)


async def quick_scan(
    client: ClashClient, proxy_url: str, sample_size: int = 8,
    group_name: str = "节点选择",
) -> ScanResult:
    """Random sample, test delay, then unlock for fast ones."""
    group = await client.get_group(group_name)
    if group.group_type.lower() == "fallback":
        raise RuntimeError("fallback controls node selection; run ccli fallback off before scanning")
    nodes = group.nodes
    original = group.current

    sample = random.sample(nodes, min(sample_size, len(nodes)))
    sem = asyncio.Semaphore(12)

    delay_tasks = [_test_node_delay(client, n, sem) for n in sample]
    delay_results = await asyncio.gather(*delay_tasks)

    # Sort by delay (descending: slowest first). We iterate from fastest.
    all_sorted = sorted(
        [(n, d) for n, d in delay_results if d > 0],
        key=lambda x: x[1],
    )

    result_nodes: list[Node] = []
    best: Node | None = None

    for name, delay in all_sorted:
        # Stop early if we found a good node and this one is too slow
        if delay >= 3000 and best is not None:
            break
        if delay >= 8000:
            continue

        unlock = await _test_node_unlock(client, proxy_url, name, original, group_name)
        node = Node(name=name, delay=delay, unlock=unlock)
        result_nodes.append(node)
        if unlock.primary_ok and (best is None or delay < (best.delay or 99999)):
            best = node
            break  # Found a working node, done

    if best and not await client.switch_node(group_name, best.name):
        best = None

    if best is None:
        # 采样未命中可解锁节点,回退全节点扫描(full_scan 内部会自行切换到 best)
        return await full_scan(client, proxy_url, group_name=group_name)

    return ScanResult(nodes=result_nodes, best=best)


async def full_scan(
    client: ClashClient, proxy_url: str, group_name: str = "节点选择",
) -> ScanResult:
    """Test all nodes: delay first, then unlock, sorted by speed."""
    group = await client.get_group(group_name)
    if group.group_type.lower() == "fallback":
        raise RuntimeError("fallback controls node selection; run ccli fallback off before scanning")
    nodes = group.nodes
    original = group.current

    sem = asyncio.Semaphore(12)
    delay_tasks = [_test_node_delay(client, n, sem) for n in nodes]
    delay_results = await asyncio.gather(*delay_tasks)

    # Sort all nodes by delay (excluding timeouts)
    all_sorted = sorted(
        [(n, d) for n, d in delay_results if d > 0],
        key=lambda x: x[1],
    )

    result_nodes: list[Node] = []
    best: Node | None = None

    for name, delay in all_sorted:
        unlock = await _test_node_unlock(client, proxy_url, name, original, group_name)
        node = Node(name=name, delay=delay, unlock=unlock)
        result_nodes.append(node)
        if unlock.primary_ok and (best is None or delay < (best.delay or 99999)):
            best = node

    if best and not await client.switch_node(group_name, best.name):
        best = None

    return ScanResult(nodes=result_nodes, best=best)
