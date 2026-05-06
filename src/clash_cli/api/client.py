"""Async Clash REST API client."""

from __future__ import annotations

from urllib.parse import quote

import httpx

from .models import Node, ProxyGroup

SKIP_TYPES = frozenset({
    "Selector", "URLTest", "Fallback", "LoadBalance",
    "Direct", "Reject", "Compatible", "Pass", "RejectDrop",
})
SKIP_PREFIXES = ("剩余", "距离", "套餐")
SKIP_KEYWORDS = ("sillygoose", "呆鹅云")
SKIP_NAMES = frozenset({"DIRECT", "REJECT", "PASS", "REJECT-DROP", "自动节点", "故障节点", "故障转移", "自动选择"})


def _is_real_node(name: str, node_type: str) -> bool:
    if node_type in SKIP_TYPES:
        return False
    if name in SKIP_NAMES:
        return False
    if any(name.startswith(p) for p in SKIP_PREFIXES):
        return False
    if any(k in name for k in SKIP_KEYWORDS):
        return False
    return True


class ClashClient:
    def __init__(self, api_base: str, secret: str):
        self.api_base = api_base.rstrip("/")
        self.headers = {"Authorization": f"Bearer {secret}"}

    async def get_proxies(self) -> dict[str, ProxyGroup]:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{self.api_base}/proxies", headers=self.headers
            )
            resp.raise_for_status()
            data = resp.json()

        groups: dict[str, ProxyGroup] = {}
        for name, info in data.get("proxies", {}).items():
            ptype = info.get("type", "")
            # Only process proxy groups (Selector, URLTest, etc.) that have "all"
            if ptype not in SKIP_TYPES:
                continue
            nodes = [
                n for n in info.get("all", [])
                if _is_real_node(n, "")
            ]
            if not nodes:
                continue
            groups[name] = ProxyGroup(
                name=name,
                group_type=ptype,
                current=info.get("now", ""),
                nodes=nodes,
            )
        return groups

    async def get_group(self, name: str) -> ProxyGroup:
        encoded = quote(name)
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{self.api_base}/proxies/{encoded}", headers=self.headers
            )
            resp.raise_for_status()
            data = resp.json()

        return ProxyGroup(
            name=name,
            group_type=data.get("type", ""),
            current=data.get("now", ""),
            nodes=[n for n in data.get("all", []) if _is_real_node(n, "")],
        )

    async def get_real_nodes(self) -> list[str]:
        groups = await self.get_proxies()
        # Find the selector group
        for g in groups.values():
            if g.group_type == "select" and g.nodes:
                return g.nodes
        # Fallback: return all unique node names from all groups
        all_nodes = set()
        for g in groups.values():
            all_nodes.update(g.nodes)
        return sorted(all_nodes)

    async def get_current_node(self, group_name: str) -> str:
        g = await self.get_group(group_name)
        return g.current

    async def test_delay(
        self, node_name: str, timeout: int = 7000, url: str = "https://www.google.com"
    ) -> int:
        encoded = quote(node_name)
        try:
            async with httpx.AsyncClient(timeout=timeout / 1000 + 3) as client:
                resp = await client.get(
                    f"{self.api_base}/proxies/{encoded}/delay",
                    params={"timeout": timeout, "url": url},
                    headers=self.headers,
                )
                data = resp.json()
                return data.get("delay", -1)
        except Exception:
            return -1

    async def switch_node(self, group_name: str, node_name: str) -> bool:
        encoded = quote(group_name)
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.put(
                    f"{self.api_base}/proxies/{encoded}",
                    json={"name": node_name},
                    headers=self.headers,
                )
                return resp.status_code == 204
        except Exception:
            return False

    async def reload_config(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.put(
                    f"{self.api_base}/configs",
                    json={"path": ""},
                    headers=self.headers,
                )
                return resp.status_code == 204
        except Exception:
            return False
