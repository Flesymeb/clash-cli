# clash-cli

![GitHub License](https://img.shields.io/github/license/Flesymeb/clash-cli)
![GitHub top language](https://img.shields.io/github/languages/top/Flesymeb/clash-cli)

**Clash 代理节点智能管理工具** -- 基于 [clash-for-linux-install](https://github.com/nelvko/clash-for-linux-install)，新增 AI 服务解锁检测与 TUI 界面。

## 功能特性

- **AI 服务解锁检测** -- 实时检测节点是否可访问 Claude / ChatGPT / Gemini（基于 [clash-verge-rev](https://github.com/clash-verge-rev/clash-verge-rev) 检测方案）
- **智能节点扫描** -- 快速模式（随机抽样）与全量模式，自动找到最快可用节点
- **TUI 交互界面** -- 终端图形化操作，菜单驱动、键盘快捷键
- **订阅管理** -- 多订阅切换、自动更新
- **完整 clashctl 功能** -- 代理启停、Tun 模式、Mixin 配置、Web 面板

## 一键安装

```bash
# 1. 安装 mihomo 代理内核（原版 clashctl）
git clone --depth 1 https://github.com/Flesymeb/clash-cli.git \
  && cd clash-cli \
  && bash install.sh

# 2. 安装 TUI 工具（需要 Python 3.10+，pipx 自动管理环境）
pipx install git+https://github.com/Flesymeb/clash-cli.git

# 或者从本地安装
cd clash-cli && pipx install .
```

没有 pipx？先安装：`pip install pipx && pipx ensurepath`

## 使用方式

### TUI 界面（推荐）

```bash
clash-cli
```

```
╔══════════════════════════════════════════════════════════════╗
║  clash-cli                     节点选择 → 精品节点-英国-01  ║
║  Claude: ✓ GB  ChatGPT: ✓ GB  Gemini: ✓ GBR                 ║
╠══════════════════════════════════════════════════════════════╣
║  [s] Quick Scan (random 8)    [f] Full Scan (all nodes)     ║
║  [l] Node List                [u] Subscriptions              ║
║  ⏾ Auto-watch OFF  press w to enable                        ║
╠══════════════════════════════════════════════════════════════╣
║  ↑↓:nav Enter:select s:scan w:watch h:help q:quit           ║
╚══════════════════════════════════════════════════════════════╝
```

### CLI 命令

```bash
clash-cli status              # 当前节点解锁状态
clash-cli scan                # 快速扫描（随机 8 个）
clash-cli scan --full         # 全量扫描所有节点
clash-cli switch <节点名>      # 切换到指定节点
clash-cli watch               # 后台守护（每 120s 检测，挂了自动切）
clash-cli watch --once        # 一次性检测
clash-cli --help              # 帮助
```

### clashctl 原生命令

```bash
clashon                    # 开启代理
clashoff                   # 关闭代理
clashsub ls                # 查看订阅
clashsub update            # 更新订阅
clashupgrade               # 升级内核
```

## 解锁检测原理

参考 [clash-verge-rev media_unlock_checker](https://github.com/clash-verge-rev/clash-verge-rev/tree/dev/src-tauri/src/cmd/media_unlock_checker)：

| 服务 | 检测端点 | 封禁地区 |
|------|---------|---------|
| Claude | `claude.ai/cdn-cgi/trace` | AF, BY, CN, CU, HK, IR, KP, MO, RU, SY |
| ChatGPT | `chat.openai.com/cdn-cgi/trace` + `api.openai.com/compliance/cookie_requirements` | unsupported_country |
| Gemini | `gemini.google.com` (region marker) | CHN, RUS, BLR, CUB, IRN, PRK, SYR, HKG, MAC |

## 配置文件

| 文件 | 用途 |
|------|------|
| `resources/mixin.yaml` | 用户自定义配置（规则、DNS、Tun 等） |
| `resources/profiles.yaml` | 订阅列表 |
| `resources/runtime.yaml` | 运行时配置（自动生成，勿手动编辑） |

## 致谢

- [nelvko/clash-for-linux-install](https://github.com/nelvko/clash-for-linux-install) -- 原版 clashctl
- [clash-verge-rev](https://github.com/clash-verge-rev/clash-verge-rev) -- 解锁检测方案
- [mihomo](https://github.com/MetaCubeX/mihomo) -- 代理内核
- [zashboard](https://github.com/Zephyruso/zashboard) -- Web 面板

## License

[MIT](LICENSE)
