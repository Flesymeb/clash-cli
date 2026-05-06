"""Configuration loader for clash-cli."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class Subscription:
    id: int
    path: str
    url: str
    is_current: bool = False


@dataclass
class Config:
    api_base: str = "http://127.0.0.1:9090"
    secret: str = ""
    proxy_url: str = "http://127.0.0.1:7890"
    selector_group: str = "节点选择"
    clashctl_dir: Path = field(default_factory=lambda: Path.home() / "clashctl")
    subscriptions: list[Subscription] = field(default_factory=list)
    current_sub_id: int = 0

    @classmethod
    def load(cls) -> Config:
        cfg = cls()

        # Env overrides
        cfg.api_base = os.environ.get("CLASH_API", cfg.api_base)
        cfg.secret = os.environ.get("CLASH_SECRET", cfg.secret)
        cfg.proxy_url = os.environ.get("CLASH_PROXY", cfg.proxy_url)

        # Determine clashctl directory: check cwd's resources/ first (dev mode),
        # then ~/clashctl, then ~/.clashctl
        cwd_resources = Path.cwd() / "resources"
        if cwd_resources.exists():
            cfg.clashctl_dir = Path.cwd()
        else:
            for d in [Path.home() / "clashctl", Path.home() / ".clashctl"]:
                if d.exists():
                    cfg.clashctl_dir = d
                    break

        resources = cfg.clashctl_dir / "resources"

        # Read mixin.yaml for secret and port
        mixin_path = resources / "mixin.yaml"
        if mixin_path.exists():
            try:
                with open(mixin_path) as f:
                    mixin = yaml.safe_load(f) or {}
                if not cfg.secret:
                    cfg.secret = mixin.get("secret", "")
                port = mixin.get("mixed-port", 7890)
                cfg.proxy_url = f"http://127.0.0.1:{port}"
                ctrl = mixin.get("external-controller", "127.0.0.1:9090")
                if not ctrl.startswith("http"):
                    ctrl = f"http://{ctrl}"
                cfg.api_base = ctrl
            except Exception:
                pass

        # Read profiles.yaml for subscriptions
        profiles_path = resources / "profiles.yaml"
        if profiles_path.exists():
            try:
                with open(profiles_path) as f:
                    data = yaml.safe_load(f) or {}
                cfg.current_sub_id = data.get("use", 0)
                for p in data.get("profiles", []):
                    cfg.subscriptions.append(
                        Subscription(
                            id=p.get("id", 0),
                            path=p.get("path", ""),
                            url=p.get("url", ""),
                            is_current=p.get("id") == cfg.current_sub_id,
                        )
                    )
            except Exception:
                pass

        return cfg
