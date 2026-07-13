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


class ClashApiError(RuntimeError):
    """Raised when the Clash REST API cannot be reached or returns an error."""


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


def _api_quote(value: str) -> str:
    return quote(value, safe="")


def _is_select_group(group_type: str) -> bool:
    return group_type.lower() in {"select", "selector"}


def _format_http_error(context: str, error: Exception) -> str:
    if isinstance(error, httpx.HTTPStatusError):
        status = error.response.status_code
        text = error.response.text.strip().replace("\n", " ")[:160]
        suffix = f": {text}" if text else ""
        return f"{context}: HTTP {status}{suffix}"
    return f"{context}: {error}"


class ClashClient:
    def __init__(self, api_base: str, secret: str):
        self.api_base = api_base.rstrip("/")
        self.headers = {"Authorization": f"Bearer {secret}"}
        self.last_error = ""

    async def get_proxies(self) -> dict[str, ProxyGroup]:
        try:
            async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
                resp = await client.get(
                    f"{self.api_base}/proxies", headers=self.headers
                )
                resp.raise_for_status()
                data = resp.json()
        except (httpx.HTTPError, ValueError) as e:
            self.last_error = _format_http_error("get proxies failed", e)
            raise ClashApiError(self.last_error) from e

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
        encoded = _api_quote(name)
        try:
            async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
                resp = await client.get(
                    f"{self.api_base}/proxies/{encoded}", headers=self.headers
                )
                resp.raise_for_status()
                data = resp.json()
        except (httpx.HTTPError, ValueError) as e:
            self.last_error = _format_http_error(f"get group {name!r} failed", e)
            raise ClashApiError(self.last_error) from e

        return ProxyGroup(
            name=name,
            group_type=data.get("type", ""),
            current=data.get("now", ""),
            nodes=[n for n in data.get("all", []) if _is_real_node(n, "")],
        )

    async def ping(self) -> str:
        try:
            async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
                resp = await client.get(
                    f"{self.api_base}/proxies", headers=self.headers
                )
                resp.raise_for_status()
            self.last_error = ""
            return "ok"
        except httpx.HTTPError as e:
            self.last_error = _format_http_error("api ping failed", e)
            raise ClashApiError(self.last_error) from e

    async def get_real_nodes(self) -> list[str]:
        groups = await self.get_proxies()
        # Find the selector group
        for g in groups.values():
            if _is_select_group(g.group_type) and g.nodes:
                return g.nodes
        # Fallback: return all unique node names from all groups
        all_nodes = set()
        for g in groups.values():
            all_nodes.update(g.nodes)
        return sorted(all_nodes)

    async def get_current_node(self, group_name: str) -> str:
        chain = await self.get_proxy_chain(group_name)
        return chain[-1]

    async def get_proxy_chain(self, group_name: str, max_depth: int = 8) -> list[str]:
        chain = [group_name]
        for _ in range(max_depth):
            group = await self.get_group(chain[-1])
            current = group.current
            if not current:
                return chain
            if current in chain:
                path = " -> ".join([*chain, current])
                raise ClashApiError(f"proxy group cycle detected: {path}")
            chain.append(current)
        raise ClashApiError(f"proxy group chain exceeds {max_depth} levels: {' -> '.join(chain)}")

    async def test_delay(
        self, node_name: str, timeout: int = 7000, url: str = "https://www.google.com"
    ) -> int:
        encoded = _api_quote(node_name)
        try:
            async with httpx.AsyncClient(timeout=timeout / 1000 + 3, trust_env=False) as client:
                resp = await client.get(
                    f"{self.api_base}/proxies/{encoded}/delay",
                    params={"timeout": timeout, "url": url},
                    headers=self.headers,
                )
                resp.raise_for_status()
                data = resp.json()
                self.last_error = ""
                return data.get("delay", -1)
        except (httpx.HTTPError, ValueError) as e:
            self.last_error = _format_http_error(f"test delay for {node_name!r} failed", e)
            return -1

    async def switch_node(self, group_name: str, node_name: str) -> bool:
        encoded = _api_quote(group_name)
        try:
            async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
                resp = await client.put(
                    f"{self.api_base}/proxies/{encoded}",
                    json={"name": node_name},
                    headers=self.headers,
                )
                if resp.status_code == 204:
                    self.last_error = ""
                    return True
                self.last_error = f"switch {group_name!r} to {node_name!r} failed: HTTP {resp.status_code}"
                return False
        except httpx.HTTPError as e:
            self.last_error = _format_http_error(f"switch {group_name!r} to {node_name!r} failed", e)
            return False

    async def reload_config(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
                resp = await client.put(
                    f"{self.api_base}/configs",
                    json={"path": ""},
                    headers=self.headers,
                )
                if resp.status_code == 204:
                    self.last_error = ""
                    return True
                self.last_error = f"reload config failed: HTTP {resp.status_code}"
                return False
        except httpx.HTTPError as e:
            self.last_error = _format_http_error("reload config failed", e)
            return False
