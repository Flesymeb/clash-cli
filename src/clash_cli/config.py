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
    config_errors: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, clashctl_dir: Path | None = None) -> Config:
        cfg = cls()

        # Env overrides
        cfg.api_base = os.environ.get("CLASH_API", cfg.api_base)
        cfg.secret = os.environ.get("CLASH_SECRET", cfg.secret)
        cfg.proxy_url = os.environ.get("CLASH_PROXY", cfg.proxy_url)

        env_home = os.environ.get("CLASH_HOME")
        if clashctl_dir is not None:
            cfg.clashctl_dir = clashctl_dir.expanduser()
        elif env_home:
            cfg.clashctl_dir = Path(env_home).expanduser()
        else:
            # Prefer the installed runtime. The repository's resources/ folder
            # is only a development fallback and may not contain the live secret.
            candidates = [Path.home() / "clashctl", Path.home() / ".clashctl"]
            cwd_resources = Path.cwd() / "resources"
            if cwd_resources.exists():
                candidates.append(Path.cwd())
            for d in candidates:
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
                if not isinstance(mixin, dict):
                    raise ValueError("expected a YAML mapping")
                if not cfg.secret:
                    cfg.secret = mixin.get("secret", "")
                port = mixin.get("mixed-port", 7890)
                cfg.proxy_url = f"http://127.0.0.1:{port}"
                ctrl = mixin.get("external-controller", "127.0.0.1:9090")
                if not ctrl.startswith("http"):
                    ctrl = f"http://{ctrl}"
                cfg.api_base = ctrl
                rules = mixin.get("rules", {})
                suffix = rules.get("suffix", []) if isinstance(rules, dict) else []
                for rule in reversed(suffix or []):
                    if isinstance(rule, str) and rule.strip().upper().startswith("MATCH,"):
                        cfg.selector_group = rule.strip().split(",", 2)[1].strip()
                        break
                custom = mixin.get("_custom", {})
                if isinstance(custom, dict):
                    if custom.get("selector-group"):
                        cfg.selector_group = str(custom["selector-group"])
                    fallback = custom.get("fallback", {})
                    fallback_nodes = fallback.get("nodes", []) if isinstance(fallback, dict) else []
                    if (
                        isinstance(fallback, dict)
                        and fallback.get("enable")
                        and isinstance(fallback_nodes, list)
                        and len(fallback_nodes) >= 2
                    ):
                        cfg.selector_group = str(fallback.get("name", "ccli-fallback"))
            except Exception as e:
                cfg.config_errors.append(f"{mixin_path}: {e}")

        # Read profiles.yaml for subscriptions
        profiles_path = resources / "profiles.yaml"
        if profiles_path.exists():
            try:
                with open(profiles_path) as f:
                    data = yaml.safe_load(f) or {}
                if not isinstance(data, dict):
                    raise ValueError("expected a YAML mapping")
                cfg.current_sub_id = data.get("use", 0)
                for p in data.get("profiles", []):
                    if not isinstance(p, dict):
                        raise ValueError("profiles entries must be mappings")
                    cfg.subscriptions.append(
                        Subscription(
                            id=p.get("id", 0),
                            path=p.get("path", ""),
                            url=p.get("url", ""),
                            is_current=p.get("id") == cfg.current_sub_id,
                        )
                    )
            except Exception as e:
                cfg.config_errors.append(f"{profiles_path}: {e}")

        return cfg
