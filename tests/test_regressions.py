from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

from clash_cli.api.models import ProxyGroup, UnlockStatus
from clash_cli.api.client import ClashApiError, ClashClient
from clash_cli.app import _sub_add_needs_prompt, proxy_exports, resolve_switch_target
from clash_cli.clashctl import ClashctlBridge, _process_uses_proxy
from clash_cli.config import Config
import clash_cli.scanner.scanner as scanner


class CliRegressionTests(unittest.TestCase):
    def run_cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        src = str(Path(__file__).resolve().parents[1] / "src")
        env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
        return subprocess.run(
            [sys.executable, "-m", "clash_cli.app", *args],
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )

    def test_native_help_passthrough(self) -> None:
        cases = [
            (("sub", "--help"), "Usage: ccli sub COMMAND"),
            (("ctl", "--help"), "Usage: ccli ctl COMMAND"),
            (("proxy", "--help"), "Usage: ccli proxy [on|off]"),
            (("fallback", "--help"), "Usage: ccli fallback"),
        ]
        for args, expected in cases:
            with self.subTest(args=args):
                result = self.run_cli(*args)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(expected, result.stdout)

    def test_sub_add_prompt_detection(self) -> None:
        self.assertTrue(_sub_add_needs_prompt(["add"]))
        self.assertTrue(_sub_add_needs_prompt(["add", "--convert"]))
        self.assertFalse(_sub_add_needs_prompt(["add", "--help"]))
        self.assertFalse(_sub_add_needs_prompt(["add", "--bad"]))
        self.assertFalse(_sub_add_needs_prompt(["add", "https://example.com/sub"]))

    def test_invalid_runtime_args_return_usage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            resources = root / "resources"
            resources.mkdir()
            (resources / "mixin.yaml").write_text("_custom:\n  system-proxy:\n    enable: false\n")
            bridge = ClashctlBridge(Config(clashctl_dir=root))

            self.assertEqual(bridge.proxy(["maybe"]).returncode, 2)
            self.assertIn("usage: ccli proxy", bridge.proxy(["maybe"]).stderr)

            result = asyncio.run(bridge.tun(["maybe"]))
            self.assertEqual(result.returncode, 2)
            self.assertIn("usage: ccli tun", result.stderr)

    def test_on_reports_immediate_kernel_exit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            resources = root / "resources"
            bin_dir = root / "bin"
            resources.mkdir()
            bin_dir.mkdir()
            (resources / "config.yaml").write_text("proxies: []\nproxy-groups: []\nrules: []\n")
            (resources / "mixin.yaml").write_text("mixed-port: 7890\n")
            kernel = bin_dir / "mihomo"
            kernel.write_text(
                "#!/usr/bin/env sh\n"
                "for arg in \"$@\"; do\n"
                "  if [ \"$arg\" = \"-t\" ]; then exit 0; fi\n"
                "done\n"
                "echo fake kernel failed >&2\n"
                "exit 7\n"
            )
            kernel.chmod(0o755)

            bridge = ClashctlBridge(Config(clashctl_dir=root))
            result = asyncio.run(bridge.on())
            self.assertEqual(result.returncode, 1)
            self.assertIn("exited immediately", result.stderr)
            self.assertIn("fake kernel failed", result.stderr)

    def test_on_already_running_prints_status_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            resources = root / "resources"
            bin_dir = root / "bin"
            resources.mkdir()
            bin_dir.mkdir()
            (resources / "mixin.yaml").write_text(
                "mixed-port: 7899\nexternal-controller: 127.0.0.1:9191\n"
            )
            kernel = bin_dir / "mihomo"
            kernel.write_text("#!/usr/bin/env sh\nsleep 60\n")
            kernel.chmod(0o755)

            bridge = ClashctlBridge(Config(clashctl_dir=root))
            bridge._pid = lambda: 4321  # type: ignore[method-assign]
            result = asyncio.run(bridge.on())

            self.assertEqual(result.returncode, 0)
            self.assertIn("proxy", result.stdout)
            self.assertIn("selected", result.stdout)
            self.assertIn("service", result.stdout)
            self.assertIn("mihomo pid 4321", result.stdout)
            self.assertNotIn("mihomo already running", result.stdout)

    def test_off_waits_for_kernel_process_to_exit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            resources = root / "resources"
            bin_dir = root / "bin"
            resources.mkdir()
            bin_dir.mkdir()
            kernel = bin_dir / "mihomo"
            kernel.write_text(
                "#!/usr/bin/env sh\n"
                "trap 'exit 0' TERM\n"
                "while :; do sleep 1; done\n"
            )
            kernel.chmod(0o755)
            bridge = ClashctlBridge(Config(clashctl_dir=root))

            async def run() -> None:
                proc = await asyncio.create_subprocess_exec(str(kernel))
                try:
                    for _ in range(20):
                        if bridge._pid() == proc.pid:
                            break
                        await asyncio.sleep(0.05)
                    self.assertEqual(bridge._pid(), proc.pid)
                    result = await bridge.off()
                    self.assertEqual(result.returncode, 0)
                    await asyncio.wait_for(proc.wait(), timeout=2)
                    self.assertIsNone(bridge._pid())
                finally:
                    if proc.returncode is None:
                        proc.kill()
                        await proc.wait()

            asyncio.run(run())

    def test_config_parse_errors_are_visible_to_doctor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            resources = root / "resources"
            bin_dir = root / "bin"
            resources.mkdir()
            bin_dir.mkdir()
            (resources / "mixin.yaml").write_text("secret: [\n")
            kernel = bin_dir / "mihomo"
            kernel.write_text("#!/usr/bin/env sh\nexit 0\n")
            kernel.chmod(0o755)

            config = Config.load(root)
            self.assertTrue(config.config_errors)

            bridge = ClashctlBridge(Config(clashctl_dir=root))
            result = asyncio.run(bridge.doctor())
            self.assertEqual(result.returncode, 1)
            self.assertIn("FAIL config", result.stdout)

    def test_config_reads_selector_group_from_match_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            resources = root / "resources"
            resources.mkdir()
            (resources / "mixin.yaml").write_text(
                "rules:\n"
                "  suffix:\n"
                "    - MATCH,custom-selector\n"
            )

            config = Config.load(root)
            self.assertEqual(config.selector_group, "custom-selector")

    def test_merge_config_has_one_terminal_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            resources = root / "resources"
            bin_dir = root / "bin"
            resources.mkdir()
            bin_dir.mkdir()
            (resources / "config.yaml").write_text(
                "proxies:\n"
                "  - name: node-a\n"
                "    type: http\n"
                "    server: 127.0.0.1\n"
                "    port: 8080\n"
                "proxy-groups:\n"
                "  - name: provider-group\n"
                "    type: select\n"
                "    proxies: [node-a]\n"
                "rules:\n"
                "  - GEOIP,CN,DIRECT\n"
                "  - MATCH,provider-group\n"
            )
            (resources / "mixin.yaml").write_text(
                "rules:\n"
                "  prefix:\n"
                "    - DOMAIN,api64.ipify.org,DIRECT\n"
                "  suffix:\n"
                "    - MATCH,节点选择\n"
            )
            kernel = bin_dir / "mihomo"
            kernel.write_text("#!/usr/bin/env sh\nexit 0\n")
            kernel.chmod(0o755)

            bridge = ClashctlBridge(Config.load(root))
            asyncio.run(bridge.merge_config())
            runtime = yaml.safe_load((resources / "runtime.yaml").read_text())
            rules = runtime["rules"]
            self.assertEqual([rule for rule in rules if rule.startswith("MATCH,")], ["MATCH,节点选择"])
            self.assertEqual(rules[-1], "MATCH,节点选择")
            self.assertNotIn("DOMAIN,api64.ipify.org,DIRECT", rules)
            group_names = {group["name"] for group in runtime["proxy-groups"]}
            self.assertIn("节点选择", group_names)

    def test_fallback_set_and_off_update_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            resources = root / "resources"
            bin_dir = root / "bin"
            resources.mkdir()
            bin_dir.mkdir()
            (resources / "config.yaml").write_text(
                "proxies:\n"
                "  - {name: node-a, type: http, server: 127.0.0.1, port: 8080}\n"
                "  - {name: node-b, type: http, server: 127.0.0.1, port: 8081}\n"
                "proxy-groups:\n"
                "  - name: 节点选择\n"
                "    type: select\n"
                "    proxies: [node-a, node-b]\n"
                "rules:\n"
                "  - MATCH,节点选择\n"
            )
            (resources / "mixin.yaml").write_text(
                "rules:\n"
                "  suffix:\n"
                "    - MATCH,节点选择\n"
            )
            kernel = bin_dir / "mihomo"
            kernel.write_text("#!/usr/bin/env sh\nexit 0\n")
            kernel.chmod(0o755)
            bridge = ClashctlBridge(Config.load(root))

            result = asyncio.run(bridge.clashctl(["fallback", "set", "node-a", "node-b"]))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("fallback  on", result.stdout)
            runtime = yaml.safe_load((resources / "runtime.yaml").read_text())
            fallback = next(group for group in runtime["proxy-groups"] if group["name"] == "ccli-fallback")
            self.assertEqual(fallback["type"], "fallback")
            self.assertEqual(fallback["proxies"], ["node-a", "node-b"])
            self.assertEqual(runtime["rules"][-1], "MATCH,ccli-fallback")
            self.assertEqual(Config.load(root).selector_group, "ccli-fallback")

            result = asyncio.run(bridge.clashctl(["fallback", "off"]))
            self.assertEqual(result.returncode, 0, result.stderr)
            runtime = yaml.safe_load((resources / "runtime.yaml").read_text())
            self.assertEqual(runtime["rules"][-1], "MATCH,节点选择")
            self.assertFalse(any(group["name"] == "ccli-fallback" for group in runtime["proxy-groups"]))

    def test_fallback_rejects_unknown_nodes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            resources = root / "resources"
            resources.mkdir()
            (resources / "config.yaml").write_text("proxies: []\nproxy-groups: []\nrules: []\n")
            bridge = ClashctlBridge(Config.load(root))
            result = asyncio.run(bridge.clashctl(["fallback", "set", "node-a", "node-b"]))
            self.assertEqual(result.returncode, 1)
            self.assertIn("unknown fallback node", result.stderr)

    def test_subconverter_port_conflict_uses_free_port(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            root = Path(tmp)
            sub_dir = root / "bin" / "subconverter"
            sub_dir.mkdir(parents=True)
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            occupied_port = int(listener.getsockname()[1])
            (sub_dir / "pref.yml").write_text(f"server:\n  port: {occupied_port}\n")

            bridge = ClashctlBridge(Config(clashctl_dir=root))

            async def not_ready(_port: int) -> bool:
                return False

            bridge._subconverter_ready = not_ready  # type: ignore[method-assign]
            new_port = asyncio.run(bridge._prepare_subconverter_port())
            self.assertNotEqual(new_port, occupied_port)
            self.assertEqual(bridge._subconverter_port(), new_port)

    def test_api_client_records_switch_error(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            port = int(sock.getsockname()[1])

        client = ClashClient(f"http://127.0.0.1:{port}", "secret")
        ok = asyncio.run(client.switch_node("节点选择", "node-a"))
        self.assertFalse(ok)
        self.assertIn("switch", client.last_error)

    def test_subscription_id_validation_returns_usage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bridge = ClashctlBridge(Config(clashctl_dir=Path(tmp)))
            cases = [
                ["use", "abc"],
                ["del", "abc"],
                ["update", "abc"],
                ["use", "0"],
            ]
            for args in cases:
                with self.subTest(args=args):
                    result = asyncio.run(bridge.clashsub(args))
                    self.assertEqual(result.returncode, 2)
                    self.assertIn("id must be a positive integer", result.stderr)

    def test_subscription_output_masks_urls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            resources = root / "resources"
            resources.mkdir()
            url = "https://user:password@example.com/sub/token123?secret=abc"
            (resources / "profiles.yaml").write_text(
                "use: 1\n"
                "profiles:\n"
                "  - id: 1\n"
                "    path: /tmp/1.yaml\n"
                f"    url: {url}\n"
            )
            (resources / "profiles.log").write_text(f"added subscription [1] {url}\n")

            bridge = ClashctlBridge(Config(clashctl_dir=root))
            listed = bridge.sub_list().stdout
            logged = bridge.sub_log().stdout
            self.assertNotIn("token123", listed)
            self.assertNotIn("secret=abc", listed)
            self.assertNotIn("user:password", listed)
            self.assertNotIn("token123", logged)
            self.assertNotIn("secret=abc", logged)
            self.assertNotIn("user:password", logged)
            self.assertIn("https://example.com/...", listed)
            self.assertIn("https://example.com/...", logged)

    def test_random_switch_target_excludes_current_node(self) -> None:
        async def run() -> None:
            client = FakeClient()
            target, mode = await resolve_switch_target(client, "custom", None)
            self.assertEqual(mode, "random")
            self.assertEqual(target, "node-b")

        asyncio.run(run())

    def test_manual_switch_target_is_preserved(self) -> None:
        async def run() -> None:
            client = FakeClient()
            target, mode = await resolve_switch_target(client, "custom", "node-a")
            self.assertEqual(mode, "manual")
            self.assertEqual(target, "node-a")

        asyncio.run(run())

    def test_manual_switch_rejects_fallback_group(self) -> None:
        async def run() -> None:
            client = FakeClient(group_type="Fallback")
            with self.assertRaisesRegex(RuntimeError, "fallback controls"):
                await resolve_switch_target(client, "custom", "node-a")

        asyncio.run(run())

    def test_proxy_chain_resolves_to_leaf(self) -> None:
        async def run() -> None:
            client = ClashClient("http://127.0.0.1:9090", "secret")
            groups = {
                "节点选择": ProxyGroup("节点选择", "Selector", "自动选择", ["自动选择"]),
                "自动选择": ProxyGroup("自动选择", "URLTest", "台湾-02", ["台湾-02"]),
                "台湾-02": ProxyGroup("台湾-02", "Vmess", "", []),
            }

            async def get_group(name: str) -> ProxyGroup:
                return groups[name]

            client.get_group = get_group  # type: ignore[method-assign]
            self.assertEqual(
                await client.get_proxy_chain("节点选择"),
                ["节点选择", "自动选择", "台湾-02"],
            )
            self.assertEqual(await client.get_current_node("节点选择"), "台湾-02")

        asyncio.run(run())

    def test_proxy_chain_rejects_cycles(self) -> None:
        async def run() -> None:
            client = ClashClient("http://127.0.0.1:9090", "secret")

            async def get_group(name: str) -> ProxyGroup:
                current = "group-b" if name == "group-a" else "group-a"
                return ProxyGroup(name, "Selector", current, [current])

            client.get_group = get_group  # type: ignore[method-assign]
            with self.assertRaises(ClashApiError):
                await client.get_proxy_chain("group-a")

        asyncio.run(run())

    def test_proxy_exports_use_mixed_http_port(self) -> None:
        exports = proxy_exports(Config(proxy_url="http://127.0.0.1:7890"))
        self.assertIn("export ALL_PROXY='http://127.0.0.1:7890'", exports)
        self.assertNotIn("socks5h://", exports)
        self.assertTrue(
            _process_uses_proxy(
                b"HTTP_PROXY=http://127.0.0.1:7890\0",
                "http://127.0.0.1:7890",
            )
        )


class FakeClient:
    def __init__(self, *, switch_ok: bool = True, group_type: str = "Selector") -> None:
        self.switch_ok = switch_ok
        self.group_type = group_type
        self.switches: list[tuple[str, str]] = []
        self.delay = {"node-a": 20, "node-b": 30, "outside": 1}

    async def get_group(self, name: str) -> ProxyGroup:
        return ProxyGroup(name=name, group_type=self.group_type, current="node-a", nodes=["node-a", "node-b"])

    async def get_real_nodes(self) -> list[str]:
        raise AssertionError("scanner should use the requested proxy group, not all real nodes")

    async def test_delay(self, node_name: str) -> int:
        return self.delay[node_name]

    async def switch_node(self, group_name: str, node_name: str) -> bool:
        self.switches.append((group_name, node_name))
        return self.switch_ok


class ScannerRegressionTests(unittest.TestCase):
    def test_scan_rejects_fallback_group(self) -> None:
        async def run() -> None:
            client = FakeClient(group_type="Fallback")
            with self.assertRaisesRegex(RuntimeError, "fallback controls"):
                await scanner.quick_scan(client, "http://127.0.0.1:7890", group_name="custom")

        asyncio.run(run())

    def test_quick_scan_uses_requested_group(self) -> None:
        original_check = scanner.check_unlock

        async def fake_check(_proxy_url: str) -> UnlockStatus:
            return UnlockStatus(claude="ok:US", chatgpt="ok:US", gemini="ok:USA")

        async def run() -> None:
            scanner.check_unlock = fake_check
            client = FakeClient()
            result = await scanner.quick_scan(client, "http://127.0.0.1:7890", sample_size=2, group_name="custom")
            self.assertIsNotNone(result.best)
            self.assertTrue(all(group == "custom" for group, _node in client.switches))

        try:
            asyncio.run(run())
        finally:
            scanner.check_unlock = original_check

    def test_unlock_test_does_not_check_original_node_when_switch_fails(self) -> None:
        original_check = scanner.check_unlock

        async def forbidden_check(_proxy_url: str) -> UnlockStatus:
            raise AssertionError("unlock check should not run after switch failure")

        async def run() -> None:
            scanner.check_unlock = forbidden_check
            client = FakeClient(switch_ok=False)
            status = await scanner._test_node_unlock(
                client,
                "http://127.0.0.1:7890",
                "node-b",
                "node-a",
                "custom",
            )
            self.assertFalse(status.all_ok)
            self.assertEqual(status.claude, "fail")
            self.assertEqual(client.switches, [("custom", "node-b")])

        try:
            asyncio.run(run())
        finally:
            scanner.check_unlock = original_check


if __name__ == "__main__":
    unittest.main()
