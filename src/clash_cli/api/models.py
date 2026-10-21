"""Data models for clash-cli."""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class UnlockStatus:
    claude: str = "pending"    # "ok:US" | "blocked:CN" | "fail" | "pending"
    chatgpt: str = "pending"   # "ok:US" | "blocked:CN" | "fail" | "pending"
    gemini: str = "pending"    # "ok:USA" | "blocked:CHN" | "fail" | "pending"

    @property
    def all_ok(self) -> bool:
        return all(
            s.startswith("ok") for s in [self.claude, self.chatgpt, self.gemini]
        )

    @property
    def primary_ok(self) -> bool:
        """Whether the required Claude and ChatGPT checks passed."""
        return self.claude.startswith("ok") and self.chatgpt.startswith("ok")

    def summary(self) -> str:
        parts = []
        for name, val in [("Claude", self.claude), ("ChatGPT", self.chatgpt), ("Gemini", self.gemini)]:
            if val.startswith("ok"):
                parts.append(f"{name}:✓ {val[3:]}")
            elif val.startswith("blocked"):
                parts.append(f"{name}:✗ {val[8:]}")
            elif val == "fail":
                parts.append(f"{name}:?")
            else:
                parts.append(f"{name}:···")
        return "  ".join(parts)


@dataclass
class Node:
    name: str
    node_type: str = ""
    delay: int | None = None   # ms, None = not tested, -1 = timeout
    unlock: UnlockStatus = field(default_factory=UnlockStatus)
    is_current: bool = False


@dataclass
class ProxyGroup:
    name: str
    group_type: str = "select"  # select, url-test, fallback, loadbalance
    current: str = ""
    nodes: list[str] = field(default_factory=list)


# Providers inject pseudo-nodes that render account info / ads in dashboards
# (e.g. "剩余流量：87.37 GB", "套餐到期：2026-11-16", "🍃美国|官网:…").
# They are not real proxies: selecting one breaks routing.
INFO_NODE_RE = re.compile(
    r"流量|套餐|到期|重置|官网|佣金|付费|教程|请勿|优惠|拥堵|招募|续费"
)

NODE_SKIP_NAMES = frozenset({
    "DIRECT", "REJECT", "PASS", "REJECT-DROP",
    "自动节点", "故障节点", "故障转移", "自动选择",
})

# HK egress IPs are blocked by Claude/ChatGPT; skip them in auto-selection
# paths (scan, random switch, watch). Manual switching stays unrestricted.
HK_NODE_RE = re.compile(r"香港|🇭🇰")


def is_blocked_region(name: str) -> bool:
    return bool(HK_NODE_RE.search(name))


def is_info_node(name: str) -> bool:
    return bool(INFO_NODE_RE.search(name))
