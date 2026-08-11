"""Textual TUI for clash-cli.

Kept in a separate module so that CLI subcommands do not pay the cost of
importing textual (~0.1s) unless the interactive dashboard is actually
launched. ``clash_cli.app`` imports :class:`ClashApp` from here lazily.
"""

from __future__ import annotations

import asyncio

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    ListItem,
    ListView,
    ProgressBar,
    Static,
)

from clash_cli.api.client import ClashClient
from clash_cli.api.models import UnlockStatus
from clash_cli.app import (
    COLOR_CYAN,
    COLOR_DIM,
    NODE_SKIP_NAMES,
    clean_output,
    color_delay,
    color_unlock,
    last_output_line,
)
from clash_cli.checker.unlock import check_all as check_unlock
from clash_cli.clashctl import ClashctlBridge, ClashctlError, mask_url
from clash_cli.config import Config
from clash_cli.scanner.scanner import full_scan, quick_scan


CONTROL_ACTIONS = [
    ("Kernel status", "ctl status", "Show running mihomo/clash process", ["status"]),
    ("Fallback status", "fallback", "Show ordered automatic failover", ["fallback"]),
    ("Web dashboard", "ctl ui", "Print local and public dashboard URLs", ["ui"]),
    ("Proxy status", "ctl proxy", "Show shell proxy variables", ["proxy"]),
    ("Start service", "ctl on", "Start kernel service and proxy environment", ["on"]),
    ("Stop service", "ctl off", "Stop kernel service", ["off"]),
    ("Restart service", "ctl restart", "Restart kernel service", ["restart"]),
]


# ── Main Dashboard ─────────────────────────────────────────────

class MenuItem(ListItem):
    def __init__(self, key: str, title: str, detail: str, action: str) -> None:
        super().__init__()
        self.action_name = action
        self._key = key
        self._title = title
        self._detail = detail

    def compose(self) -> ComposeResult:
        yield Static(f" {self._key.upper():<2} {self._title:<18} {self._detail}", markup=False)


class MainScreen(Screen):
    BINDINGS = [
        Binding("up", "cursor_up", "Up", show=False),
        Binding("down", "cursor_down", "Down", show=False),
        Binding("enter", "select", "Select"),
        Binding("s", "quick_scan", "Quick Scan"),
        Binding("f", "full_scan", "Full Scan"),
        Binding("l", "node_list", "Node List"),
        Binding("u", "subscriptions", "Subscriptions"),
        Binding("c", "control", "Control"),
        Binding("w", "toggle_watch", "Auto Watch"),
        Binding("r", "refresh", "Refresh"),
        Binding("h", "show_help", "Help"),
        Binding("question_mark", "show_help", "Help", show=False),
    ]

    WATCH_INTERVAL = 120  # seconds between health checks

    def __init__(self, config: Config, client: ClashClient) -> None:
        super().__init__()
        self.config = config
        self.client = client
        self._watching = False
        self._watch_timer: asyncio.Task | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical(id="dashboard"):
            yield Static("", id="status-box")
            yield ListView(
                MenuItem("s", "Quick scan", "Find a working node", "quick_scan"),
                MenuItem("f", "Full scan", "Test every node", "full_scan"),
                MenuItem("l", "Nodes", "Switch proxy node", "node_list"),
                MenuItem("u", "Subscriptions", "Use or update profiles", "subscriptions"),
                MenuItem("c", "Control", "Service and proxy tools", "control"),
                id="main-menu",
            )
            yield Static("", id="watch-line")
        yield Footer()

    async def on_mount(self) -> None:
        await self.refresh_status()
        self.query_one("#main-menu", ListView).focus()
        self._update_watch_line()

    def _update_watch_line(self) -> None:
        line = self.query_one("#watch-line", Static)
        if self._watching:
            line.update(f"  [bold yellow]⏳ Auto-watch ON[/] (every {self.WATCH_INTERVAL}s)  [dim]press w to toggle[/]")
        else:
            line.update(f"  [dim]Auto-watch OFF  press w to enable[/]")

    async def _watch_loop(self) -> None:
        """Background task: periodically check unlock and auto-switch."""
        while self._watching:
            await asyncio.sleep(self.WATCH_INTERVAL)
            if not self._watching:
                break
            try:
                unlock = await check_unlock(self.config.proxy_url)
                if not unlock.primary_ok:
                    self.notify(
                        f"Node failed! Claude:{unlock.claude} ChatGPT:{unlock.chatgpt} Gemini:{unlock.gemini}",
                        severity="error",
                    )
                    self.notify("Auto-scanning for working node...", severity="warning", timeout=5)
                    result = await quick_scan(
                        self.client, self.config.proxy_url,
                        group_name=self.config.selector_group,
                    )
                    if result.best:
                        self.notify(
                            f"Auto-switched to: {result.best.name} ({result.best.delay}ms)",
                            severity="information",
                            timeout=5,
                        )
                    else:
                        self.notify("No working node found!", severity="error", timeout=10)
            except Exception:
                pass
            await self.refresh_status()

    def action_toggle_watch(self) -> None:
        self._watching = not self._watching
        if self._watching:
            self._watch_timer = asyncio.create_task(self._watch_loop())
        self._update_watch_line()
        self.notify(
            f"Auto-watch {'ON' if self._watching else 'OFF'}",
            severity="information",
            timeout=2,
        )

    async def refresh_status(self) -> None:
        try:
            current = await self.client.get_current_node(self.config.selector_group)
            unlock = await check_unlock(self.config.proxy_url)
        except Exception as e:
            current = f"Error: {e}"
            unlock = UnlockStatus()

        status = self.query_one("#status-box", Static)
        status.update(
            f"[bold]clash-cli[/bold]  "
            f"group=[{COLOR_CYAN}]{self.config.selector_group}[/{COLOR_CYAN}]  "
            f"node=[{COLOR_CYAN}]{current}[/{COLOR_CYAN}]  "
            f"sub=[{COLOR_CYAN}]#{self.config.current_sub_id}[/{COLOR_CYAN}]\n"
            f"proxy=[{COLOR_CYAN}]{self.config.proxy_url}[/{COLOR_CYAN}]  "
            f"api=[{COLOR_CYAN}]{self.config.api_base}[/{COLOR_CYAN}]\n"
            f"Service regions  Claude: {color_unlock(unlock.claude)}  "
            f"ChatGPT: {color_unlock(unlock.chatgpt)}  "
            f"Gemini: {color_unlock(unlock.gemini)}"
        )

    def action_show_help(self) -> None:
        self.app.push_screen(HelpScreen())

    def action_select(self) -> None:
        menu = self.query_one("#main-menu", ListView)
        if menu.index is not None and menu.index < len(menu.children):
            item = menu.children[menu.index]
            if hasattr(item, "action_name"):
                action = item.action_name
                getattr(self, f"action_{action}", lambda: None)()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if hasattr(event.item, "action_name"):
            getattr(self, f"action_{event.item.action_name}", lambda: None)()

    def action_quick_scan(self) -> None:
        self.app.push_screen(ScanScreen(self.config, self.client, mode="quick"))

    def action_full_scan(self) -> None:
        self.app.push_screen(ScanScreen(self.config, self.client, mode="full"))

    def action_node_list(self) -> None:
        self.app.push_screen(NodeListScreen(self.config, self.client))

    def action_subscriptions(self) -> None:
        self.app.push_screen(SubScreen(self.config, self.client))

    def action_control(self) -> None:
        self.app.push_screen(ControlScreen(self.config))

    async def action_refresh(self) -> None:
        self.config = Config.load()
        await self.refresh_status()


# ── Scan Progress Screen ───────────────────────────────────────

class ScanScreen(Screen):
    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("q", "back", "Back"),
    ]

    def __init__(self, config: Config, client: ClashClient, mode: str = "quick") -> None:
        super().__init__()
        self.config = config
        self.client = client
        self.mode = mode

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical(id="scan-area"):
            yield Static(
                f"[bold]  {'Quick Scan' if self.mode == 'quick' else 'Full Scan'}[/bold]",
                id="scan-title",
            )
            yield Static("  Starting...", id="scan-log")
            yield ProgressBar(total=100, id="scan-progress")
        yield Footer()

    async def on_mount(self) -> None:
        asyncio.create_task(self.run_scan())

    async def run_scan(self) -> None:
        log = self.query_one("#scan-log", Static)
        progress = self.query_one("#scan-progress", ProgressBar)

        try:
            if self.mode == "quick":
                result = await quick_scan(
                    self.client,
                    self.config.proxy_url,
                    group_name=self.config.selector_group,
                )
            else:
                result = await full_scan(
                    self.client,
                    self.config.proxy_url,
                    group_name=self.config.selector_group,
                )
        except Exception as e:
            log.update(f"[red]  Error: {e}[/]")
            return

        progress.progress = 100

        lines = []
        for node in result.nodes:
            marker = " → " if node.name == (result.best and result.best.name) else "   "
            lines.append(
                f"{marker}[bold]{node.name:<20}[/bold]  "
                f"{color_delay(node.delay)}  "
                f"Claude:{color_unlock(node.unlock.claude)}  "
                f"ChatGPT:{color_unlock(node.unlock.chatgpt)}  "
                f"Gemini:{color_unlock(node.unlock.gemini)}"
            )

        if result.best:
            lines.append(f"\n  [green bold]Found: {result.best.name} ({result.best.delay}ms, all unlocked)[/]")
        else:
            lines.append("\n  [red bold]No fully unlocked node found[/]")

        log.update("\n".join(lines))

    def action_back(self) -> None:
        self.app.pop_screen()


# ── Node List Screen ───────────────────────────────────────────

class NodeListScreen(Screen):
    BINDINGS = [
        Binding("up", "cursor_up", "Up", show=False),
        Binding("down", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("j", "cursor_down", "Down", show=False),
        Binding("escape", "back", "Back"),
        Binding("q", "back", "Back"),
        Binding("t", "test_unlock", "Test"),
        Binding("d", "test_delays", "Delays"),
        Binding("s", "quick_scan", "Quick Scan"),
        Binding("f", "full_scan", "Full Scan"),
        Binding("r", "refresh_list", "Refresh"),
    ]

    DELAY_CONCURRENCY = 12

    def __init__(self, config: Config, client: ClashClient) -> None:
        super().__init__()
        self.config = config
        self.client = client
        self._node_list: list[str] = []
        self._current_node: str = ""
        self._group_type: str = ""
        self._delays: dict[str, int] = {}
        self._unlocks: dict[str, UnlockStatus] = {}
        self._testing_unlock = False
        self._render_gen = 0

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical():
            yield Static("", id="node-header", markup=False)
            yield DataTable(id="node-table")
            yield Static("", id="node-status")
        yield Footer()

    async def on_mount(self) -> None:
        await self._load_nodes()
        self.query_one("#node-table", DataTable).focus()

    async def _load_nodes(self) -> None:
        table = self.query_one("#node-table", DataTable)
        table.clear(columns=True)
        table.add_columns("#", "Name", "Latency", "Claude", "ChatGPT", "Gemini")
        table.cursor_type = "row"

        try:
            group = await self.client.get_group(self.config.selector_group)
            self._group_type = group.group_type
            self._node_list = [n for n in group.nodes if n not in NODE_SKIP_NAMES]
            try:
                chain = await self.client.get_proxy_chain(self.config.selector_group)
                self._current_node = chain[-1]
            except Exception:
                self._current_node = group.current
        except Exception:
            self._node_list = []
            self._current_node = ""
            self._group_type = ""

        self._render_header()
        for i, name in enumerate(self._node_list, 1):
            self._add_row(i, name)

        # Position cursor on current node
        try:
            idx = self._node_list.index(self._current_node)
            table.move_cursor(row=idx)
        except (ValueError, Exception):
            pass

        if self._node_list and not self._delays:
            asyncio.create_task(self._test_all_delays())

    def _render_header(self) -> None:
        header = self.query_one("#node-header", Static)
        mode = f"type={self._group_type}  " if self._group_type else ""
        header.update(
            f"Nodes  group={self.config.selector_group}  {mode}current={self._current_node}"
        )

    def _unlock_text(self, unlock: UnlockStatus | None, field: str) -> Text:
        if unlock is None:
            return Text("···", style=COLOR_DIM)
        return Text.from_markup(color_unlock(getattr(unlock, field)))

    def _add_row(self, index: int, name: str) -> None:
        table = self.query_one("#node-table", DataTable)
        marker = "→" if name == self._current_node else " "
        delay = self._delays.get(name)
        unlock = self._unlocks.get(name)
        table.add_row(
            str(index),
            f"{marker} {name}",
            Text.from_markup(color_delay(delay)),
            self._unlock_text(unlock, "claude"),
            self._unlock_text(unlock, "chatgpt"),
            self._unlock_text(unlock, "gemini"),
            key=name,
        )

    def _set_status(self, text: str) -> None:
        self.query_one("#node-status", Static).update(text)

    def _selected_node(self) -> str:
        table = self.query_one("#node-table", DataTable)
        try:
            row = table.cursor_coordinate.row
        except Exception:
            row = getattr(table, "cursor_row", None)
        if row is None or row < 0 or row >= len(self._node_list):
            return ""
        return self._node_list[row]

    def _is_fallback(self) -> bool:
        return self._group_type.lower() == "fallback"

    async def _test_all_delays(self) -> None:
        if not self._node_list:
            return
        gen = self._render_gen
        table = self.query_one("#node-table", DataTable)
        total = len(self._node_list)
        sem = asyncio.Semaphore(self.DELAY_CONCURRENCY)
        done = 0
        self._set_status(f"Testing delays... 0/{total}")

        async def test_one(name: str) -> None:
            nonlocal done
            async with sem:
                delay = await self.client.test_delay(name)
            if self._render_gen != gen:
                return  # a refresh invalidated this batch
            self._delays[name] = delay
            try:
                table.update_cell(name, "Latency", Text.from_markup(color_delay(delay)))
            except Exception:
                pass
            done += 1
            self._set_status(f"Testing delays... {done}/{total}")

        await asyncio.gather(*[test_one(n) for n in self._node_list])
        if self._render_gen == gen:
            self._set_status("Delays ready.  t: test unlock | Enter: switch")

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Handle Enter on a row - switch to that node."""
        row = event.cursor_row
        if row is not None and row < len(self._node_list):
            name = self._node_list[row]
            asyncio.create_task(self._do_switch(name))

    async def _do_switch(self, name: str) -> None:
        if self._testing_unlock:
            self.notify("Unlock test in progress, please wait", severity="warning", timeout=3)
            return
        if self._is_fallback():
            self.notify(
                "fallback controls selection; run ccli fallback off before manual switching",
                severity="error", timeout=6,
            )
            return
        try:
            ok = await self.client.switch_node(self.config.selector_group, name)
        except Exception as e:
            self.notify(f"Failed to switch to: {name}: {e}", severity="error", timeout=6)
            return
        if not ok:
            detail = f": {self.client.last_error}" if self.client.last_error else ""
            self.notify(f"Failed to switch to: {name}{detail}", severity="error", timeout=6)
            return
        old = self._current_node
        self._current_node = name
        self._refresh_marker(old)
        self._refresh_marker(name)
        self._render_header()
        self.notify(f"Switched to: {name}", severity="information", timeout=3)

    def _refresh_marker(self, name: str) -> None:
        if not name or name not in self._node_list:
            return
        table = self.query_one("#node-table", DataTable)
        marker = "→" if name == self._current_node else " "
        try:
            table.update_cell(name, "Name", f"{marker} {name}")
        except Exception:
            pass

    async def action_test_unlock(self) -> None:
        name = self._selected_node()
        if not name:
            self.notify("No node selected", severity="warning", timeout=3)
            return
        if self._testing_unlock:
            self.notify("Unlock test already running", severity="warning", timeout=3)
            return
        if self._is_fallback():
            self.notify(
                "fallback controls selection; run ccli fallback off before testing",
                severity="error", timeout=6,
            )
            return
        asyncio.create_task(self._test_node_unlock(name))

    async def _test_node_unlock(self, name: str) -> None:
        self._testing_unlock = True
        self._set_status(f"Testing unlock on {name} (traffic rerouted briefly)...")
        original = self._current_node or name
        try:
            if not await self.client.switch_node(self.config.selector_group, name):
                self.notify(f"Failed to switch to {name}", severity="error", timeout=6)
                return
            await asyncio.sleep(0.3)
            unlock = await check_unlock(self.config.proxy_url)
            self._unlocks[name] = unlock
            table = self.query_one("#node-table", DataTable)
            for col, field in (("Claude", "claude"), ("ChatGPT", "chatgpt"), ("Gemini", "gemini")):
                try:
                    table.update_cell(name, col, self._unlock_text(unlock, field))
                except Exception:
                    pass
            self.notify(
                f"{name}: Claude {unlock.claude} | ChatGPT {unlock.chatgpt} | Gemini {unlock.gemini}",
                severity="information" if unlock.primary_ok else "warning",
                timeout=5,
            )
        except Exception as e:
            self.notify(f"Test failed: {e}", severity="error", timeout=6)
        finally:
            if original and original != name:
                await self.client.switch_node(self.config.selector_group, original)
            self._testing_unlock = False
            self._set_status("")

    async def action_test_delays(self) -> None:
        if self._testing_unlock:
            self.notify("Unlock test in progress, please wait", severity="warning", timeout=3)
            return
        self._delays.clear()
        self._render_gen += 1
        await self._test_all_delays()

    def action_back(self) -> None:
        self.app.pop_screen()

    def action_quick_scan(self) -> None:
        self.app.push_screen(ScanScreen(self.config, self.client, mode="quick"))

    def action_full_scan(self) -> None:
        self.app.push_screen(ScanScreen(self.config, self.client, mode="full"))

    async def action_refresh_list(self) -> None:
        self._delays.clear()
        self._unlocks.clear()
        self._render_gen += 1
        await self._load_nodes()


# ── Subscription Screen ────────────────────────────────────────

class SubScreen(Screen):
    BINDINGS = [
        Binding("up", "cursor_up", "Up", show=False),
        Binding("down", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("j", "cursor_down", "Down", show=False),
        Binding("r", "update_subscription", "Update"),
        Binding("escape", "back", "Back"),
        Binding("q", "back", "Back"),
    ]

    def __init__(self, config: Config, client: ClashClient) -> None:
        super().__init__()
        self.config = config
        self.client = client
        self.bridge = ClashctlBridge(config)

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical():
            yield Static("", id="sub-header", markup=False)
            yield DataTable(id="sub-table")
        yield Footer()

    async def on_mount(self) -> None:
        await self._load_subscriptions()
        self.query_one("#sub-table", DataTable).focus()

    async def _load_subscriptions(self) -> None:
        self.bridge = ClashctlBridge(self.config)
        self.config = Config.load(self.bridge.root)

        table = self.query_one("#sub-table", DataTable)
        table.clear(columns=True)
        table.add_columns("#", "ID", "Path", "URL")
        table.cursor_type = "row"

        for i, sub in enumerate(self.config.subscriptions):
            marker = "✓" if sub.is_current else " "
            table.add_row(marker, str(sub.id), sub.path, mask_url(sub.url))

        header = self.query_one("#sub-header", Static)
        header.update(f"Subscriptions  current=#{self.config.current_sub_id}")

        # Focus on current
        for i, sub in enumerate(self.config.subscriptions):
            if sub.is_current:
                table.move_cursor(row=i)
                break

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Handle Enter on a row - switch subscription."""
        row = event.cursor_row
        if row is not None and row < len(self.config.subscriptions):
            sub = self.config.subscriptions[row]
            asyncio.create_task(self._use_subscription(sub.id))

    def _selected_subscription(self):
        table = self.query_one("#sub-table", DataTable)
        row = None
        try:
            row = table.cursor_coordinate.row
        except Exception:
            row = getattr(table, "cursor_row", None)
        if row is None or row < 0 or row >= len(self.config.subscriptions):
            return None
        return self.config.subscriptions[row]

    async def _run_sub_command(self, args: list[str | int], success: str) -> bool:
        try:
            result = await self.bridge.clashsub(args)
        except ClashctlError as e:
            self.notify(str(e), severity="error", timeout=8)
            return False

        if result.returncode != 0:
            self.notify(last_output_line(result) or "clashsub failed", severity="error", timeout=8)
            return False

        self.notify(last_output_line(result) or success, severity="information", timeout=4)
        await self._load_subscriptions()

        if isinstance(self.app, ClashApp):
            self.app.config = self.config
            if self.config.secret:
                self.app.client = ClashClient(self.config.api_base, self.config.secret)
                self.client = self.app.client
        return True

    async def _use_subscription(self, sub_id: int) -> None:
        await self._run_sub_command(["use", sub_id], f"Subscription #{sub_id} selected")

    async def _update_subscription(self, sub_id: int) -> None:
        await self._run_sub_command(["update", sub_id], f"Subscription #{sub_id} updated")

    async def action_update_subscription(self) -> None:
        sub = self._selected_subscription()
        if sub is None:
            self.notify("No subscription selected", severity="warning", timeout=3)
            return
        await self._update_subscription(sub.id)

    def action_back(self) -> None:
        self.app.pop_screen()


# ── Control Screen ──────────────────────────────────────────────

class ControlScreen(Screen):
    BINDINGS = [
        Binding("up", "cursor_up", "Up", show=False),
        Binding("down", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("j", "cursor_down", "Down", show=False),
        Binding("escape", "back", "Back"),
        Binding("q", "back", "Back"),
    ]

    def __init__(self, config: Config) -> None:
        super().__init__()
        self.config = config
        self.bridge = ClashctlBridge(config)

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical(id="control-area"):
            yield Static("Control  service, dashboard, proxy environment", id="control-header", markup=False)
            yield DataTable(id="control-table")
            yield Static("", id="control-output", markup=False)
        yield Footer()

    async def on_mount(self) -> None:
        table = self.query_one("#control-table", DataTable)
        table.cursor_type = "row"
        table.add_columns("Action", "Command", "Purpose")
        for title, command, detail, _args in CONTROL_ACTIONS:
            table.add_row(title, command, detail)
        table.focus()
        self._set_output(
            "Enter runs the selected command.\n"
            "Current shell proxy cannot be changed by a Python subprocess.\n"
            'After starting the service, run: eval "$(ccli env)"'
        )

    def _set_output(self, text: str) -> None:
        self.query_one("#control-output", Static).update(clean_output(text))

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.cursor_row < 0 or event.cursor_row >= len(CONTROL_ACTIONS):
            return
        asyncio.create_task(self._run_action(event.cursor_row))

    async def _run_action(self, row: int) -> None:
        title, _command, _detail, args = CONTROL_ACTIONS[row]
        self._set_output(f"Running {title}...")
        try:
            if args and args[0] == "restart":
                result = await self.bridge.restart()
            else:
                result = await self.bridge.clashctl(args)
        except ClashctlError as e:
            self._set_output(str(e))
            self.notify(str(e), severity="error", timeout=6)
            return

        output = result.output or f"{title} completed"
        if args in (["on"], ["proxy", "on"]):
            output += '\n\nTo enable proxy in this shell, run: eval "$(ccli env)"'
        if args == ["off"]:
            output += '\n\nTo remove proxy variables in this shell, run: eval "$(ccli env --unset)"'
        self._set_output(output)
        severity = "information" if result.returncode == 0 else "error"
        self.notify(last_output_line(result) or title, severity=severity, timeout=4)

    def action_back(self) -> None:
        self.app.pop_screen()


# ── Help Screen ─────────────────────────────────────────────────

class HelpScreen(Screen):
    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("q", "back", "Back"),
    ]
    HELP_TEXT = """
[bold]Keys[/]
  [bold]↑↓ / jk[/]  Navigate
  [bold]Enter[/]    Select / Switch
  [bold]q[/]         Quit
  [bold]Esc[/]       Back

[bold]Main Menu[/]
  [bold]s[/]         Quick Scan (random 8)
  [bold]f[/]         Full Scan (all nodes)
  [bold]l[/]         Node List
  [bold]u[/]         Subscriptions
  [bold]c[/]         Control
  [bold]w[/]         Toggle Auto-watch
  [bold]r[/]         Refresh status
  [bold]h / ?[/]     This help

[bold]Node List[/]
  [bold]Enter[/]     Switch to selected node
  [bold]t[/]         Test unlock on selected node
  [bold]d[/]         Test delays for all nodes
  [bold]r[/]         Refresh node list
  [bold]s[/]         Quick Scan
  [bold]f[/]         Full Scan

[bold]Subscriptions[/]
  [bold]Enter[/]     Use selected subscription
  [bold]r[/]         Update selected subscription

[bold]CLI Commands[/]
  [green]clash-cli[/]                  Launch TUI
  [green]clash-cli status[/]           Show current node
  [green]clash-cli scan[/]             Quick scan
  [green]clash-cli scan --full[/]      Full scan
  [green]clash-cli switch <name>[/]    Switch node
  [green]clash-cli watch[/]            Auto-watch daemon
  [green]clash-cli watch --once[/]     One-shot check
  [green]clash-cli sub <args>[/]       Run clashsub
  [green]clash-cli ctl <args>[/]       Manage local runtime
  [green]eval "$(ccli env)"[/]         Enable proxy vars in current shell
"""

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical(id="help-area"):
            yield Static(self.HELP_TEXT)
        yield Footer()

    def action_back(self) -> None:
        self.app.pop_screen()


# ── Main App ───────────────────────────────────────────────────

class ClashApp(App):
    TITLE = "clash-cli"
    BINDINGS = [Binding("q", "quit", "Quit")]
    CSS = """
    Screen {
        layout: vertical;
        background: #0f1419;
        color: #d8dee9;
    }
    #dashboard {
        height: 1fr;
        padding: 1 2;
    }
    #status-box {
        height: auto;
        padding: 1;
        border: round #4c566a;
        background: #161b22;
        margin: 0 0 1 0;
    }
    #main-menu {
        height: 1fr;
        border: round #2e3440;
        background: #111820;
    }
    #main-menu ListItem {
        padding: 0 2;
        height: 2;
    }
    #main-menu ListItem.--highlight {
        background: #243447;
        color: white;
    }
    #scan-area {
        height: 1fr;
        padding: 1 2;
    }
    #scan-title {
        height: auto;
        margin: 0 0 1 0;
    }
    #scan-log {
        height: 1fr;
    }
    #scan-progress {
        margin: 1 0 0 0;
    }
    #node-header, #sub-header {
        height: auto;
        padding: 1 2;
        background: #161b22;
    }
    #node-table, #sub-table {
        height: 1fr;
    }
    #control-area {
        height: 1fr;
        padding: 1 2;
    }
    #control-header {
        height: auto;
        padding: 0 0 1 0;
    }
    #control-table {
        height: 14;
    }
    #control-output {
        height: 1fr;
        margin: 1 0 0 0;
        padding: 1;
        border: round #4c566a;
        background: #111820;
    }
    #help-area {
        height: 1fr;
        padding: 1 2;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self.config = Config.load()
        if not self.config.secret:
            self.exit(message="Error: Clash API secret not set.\n"
                "Run: clashsecret <your-secret>\n"
                "Or set CLASH_SECRET environment variable.")
        self.client = ClashClient(self.config.api_base, self.config.secret)

    def action_quit(self) -> None:
        self.exit()

    def on_mount(self) -> None:
        self.push_screen(MainScreen(self.config, self.client))
