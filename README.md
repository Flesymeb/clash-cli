# clash-cli

Clash 代理节点智能管理 TUI 工具。

- 一键检测节点能否访问 Claude / ChatGPT / Gemini
- 快速扫描找到最快可用节点，自动切换
- 后台守护，节点挂了自动切
- 短命令：`ccli`

## 安装

```bash
# 1. 安装代理内核
git clone --depth 1 https://github.com/Flesymeb/clash-cli.git && cd clash-cli && bash install.sh

# 2. 安装 TUI
pipx install git+https://github.com/Flesymeb/clash-cli.git
```

（需要 Python 3.10+，`pipx` 自动隔离环境。没有 pipx？`pip install pipx && pipx ensurepath`）

## 使用

```bash
ccli              # 启动 TUI
ccli --version    # 查看 ccli 版本
ccli status       # 查看当前节点解锁状态
ccli status --no-check # 只看当前选择，不跑解锁检测
ccli scan         # 快速扫描（随机 8 个，找到可用即换）
ccli scan --full  # 全量扫描所有节点
ccli switch <名>   # 切换到指定节点
ccli switch       # 随机切换一个节点，并输出延迟/解锁状态
ccli fallback list # 查看可用于 fallback 的节点名
ccli fallback set "主节点" "备用节点" # 配置有序故障转移
ccli fallback auto --size 5 # 扫描当前订阅并生成自动 fallback 池
ccli fallback refresh # 重新测速并刷新自动池
ccli fallback     # 查看 fallback 当前落点和检测配置
ccli fallback off # 关闭故障转移，恢复节点选择组
ccli watch        # 后台守护（每 120s 检测，挂了自动切）
ccli watch --once # 一次性检测

ccli sub          # 查看订阅
ccli sub use <id> # 切换订阅
ccli sub update   # 更新当前订阅
ccli sub add <url>
ccli sub del <id>

ccli ctl on       # 开启本地代理内核
ccli ctl off      # 关闭本地代理内核
ccli doctor       # 检查运行环境、配置、API、订阅和 shell 代理
ccli ctl ui       # 查看 Web 控制台地址
ccli ctl proxy on # 开启系统代理
ccli ctl tun on   # 开启 Tun 模式
ccli ctl log      # 查看内核日志

eval "$(ccli env)"         # 让当前 shell 生效代理变量
eval "$(ccli env --unset)" # 清理当前 shell 代理变量
eval "$(ccli shell-init)"  # 安装当前 shell 函数，之后可直接 ccli on/off
ccli shell-init --install  # 写入 ~/.bashrc / ~/.zshrc / fish conf.d
```

TUI 快捷键：`↑↓` 导航 `Enter` 选择 `s` 扫描 `w` 守护 `h` 帮助 `q` 退出。订阅页中 `Enter` 切换订阅，`r` 更新订阅。

`fallback auto` 会保留延迟检测成功且 Claude、ChatGPT 均可用的节点，按延迟排序并优先覆盖不同地区；Gemini 结果仅展示，不参与筛选。启用自动池后，`sub use` 和当前订阅的 `sub update` 会在内核运行时自动重建池；内核未运行或扫描失败时，新订阅仍会生效，但 fallback 会保持关闭并标记为 `stale`。

注意：普通可执行文件不能把代理环境变量写回当前 shell。`install.sh` 会写入 shell 集成；如果你只通过 `pipx install` 安装了 Python 包，请补跑 `ccli shell-init --install`，然后重开终端。

安装器会优先查询 Mihomo、yq 和 subconverter 的最新版本；网络不可用时回退到 `.env` 中的固定版本。Mihomo 安装完成后会尝试授予 TUN 所需的 `CAP_NET_ADMIN`，失败时 `ccli tun on` 会给出可直接执行的修复命令。

订阅添加和更新会先规范化为 UTF-8，并拒绝空响应、HTML 登录页和未包含任何节点/provider 的配置；验证失败不会覆盖当前订阅。

`ccli on` 只会更新当前 shell 的代理变量。已经运行的 Codex 或其他程序不会自动继承新环境；请先执行 `ccli on`，再启动对应程序。`ccli doctor` 会报告未继承代理变量的 Codex 进程。

状态中的 `route` 是 Mihomo 实际代理链，`selected` 是最终叶子节点。Claude / ChatGPT / Gemini 的 region 是各服务自己的地区判定，可能因服务侧定位差异而不完全一致。

## 致谢

- [nelvko/clash-for-linux-install](https://github.com/nelvko/clash-for-linux-install) — 原版 clashctl
- [clash-verge-rev](https://github.com/clash-verge-rev/clash-verge-rev) — 解锁检测方案

MIT
