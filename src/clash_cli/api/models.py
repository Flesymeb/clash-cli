"""Data models for clash-cli."""

from __future__ import annotations

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
