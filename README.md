# MCDR-ServerLogFilter

**语言 / Language:** **简体中文** | [English](README_en.md)

一个 MCDReforged 插件：**把服务端的刷屏日志从 MCDR 控制台隐去，同时完整保留服务端自己的日志文件。**

[![MCDR](https://img.shields.io/badge/MCDReforged-%3E%3D2.13-blue)](https://mcdreforged.com/)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Python](https://img.shields.io/badge/python-%3E%3D3.9-blue)](https://www.python.org/)

---

## 为什么需要它

MCDR 会把服务端打印的每一行原样回显到控制台。绝大多数情况下这正是我们想要的，但有些服务端版本会反复打印不含任何有效信息的日志行，例如 Minecraft 26.3 的：

```
[Server thread/INFO]: Player Steve standing on air - force-sending blocks below
```

这是 26.3 新增的「幽灵方块自动修复」机制打的 INFO 日志，属于官方**误报**（Mojira [MC-311474](https://bugs.mojang.com/browse/MC-311474) / [MC-311727](https://bugs.mojang.com/browse/MC-311727)）——玩家正常跳跃和移动时也会触发，每人每 10 秒最多一条，会持续刷屏。

直接改服务端的 `log4j2.xml` 也能过滤，但那是**全局性**改动：改错了可能让服务端丢失重要日志，而且只对服务端生效。本插件换了个思路——**只在 MCDR 这一侧动手**。

## 两个关键设计

### 1. 只隐去回显，不丢弃信息

插件使用 MCDR 的 `InfoActionFlag.hidden()`，**不是** `discarded()`：

| | 控制台回显 | 派发给插件事件 | MCDR 状态检测 |
|---|---|---|---|
| `hidden()`（本插件） | ❌ 不显示 | ✅ 照常 | ✅ 正常 |
| `discarded()` | ❌ 不显示 | ❌ 收不到 | ⚠️ 可能失效 |

保留 `process` 意味着这些行**依然会正常派发给 MCDR 的信息响应器和插件事件**。因此**即使你的规则写得过于宽泛，也不会破坏 MCDR 自身的「服务端启动完成 / 停止 / 玩家进出」检测**——最多只是控制台上看不到而已。这是本插件最重要的安全属性。

### 2. 服务端日志完全不受影响

服务端自己的 `server/logs/latest.log` 由服务端进程用 log4j 独立写入，与本插件无关，**原始记录一条不少**。所以：

- 想查完整的原始日志 → 翻 `server/logs/latest.log`
- 想让 MCDR 控制台 / 网页面板干净 → 用本插件

## 安装

1. 从 [Releases](../../releases) 下载 `ServerLogFilter-vX.Y.Z.mcdr`，或自行打包（见下）
2. 放进对应 MCDR 实例的 `plugins/` 目录：

```
<MCDR目录>/plugins/ServerLogFilter-vX.Y.Z.mcdr
```

有多个子服就每个子服各放一份，配置互相独立。

3. 生效：

- MCDR 运行中：`!!MCDR reload plugin server_log_filter`
- 未运行：直接开服

## 配置

首次加载会自动生成 `config/server_log_filter/config.json`：

```json
{
    "patterns": [
        "standing on air - force-sending blocks below"
    ],
    "log_matched_lines": false,
    "report_on_server_stop": true
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `patterns` | `string[]` | 正则规则列表。对每行日志的**正文**（MCDR 已剥掉 `[时间] [线程/级别]:` 前缀）做匹配，命中即隐去 |
| `log_matched_lines` | `bool` | 调试用。设为 `true` 会把被隐去的行以 INFO 级别写进 MCDR 日志，方便确认规则生效 |
| `report_on_server_stop` | `bool` | 服务端停止时，在 MCDR 日志里汇总本次共隐去多少行 |

> **匹配方式是 `re.search`（包含匹配），不是全匹配。**
> 写 `standing on air` 就能命中整行，不需要加 `.*`。
> 反过来要注意：**正则里的 `.` 匹配任意字符**，想匹配字面点号请写 `\.`。

配置文件是 JSON，**不支持注释**。

## 命令

| 命令 | 权限 | 说明 |
|---|---|---|
| `!!logfilter` | user | 查看状态：规则数与各规则命中次数 |
| `!!logfilter list` | user | 同上 |
| `!!logfilter test <文本>` | user | 测试某行是否会被隐去，并指出命中哪条规则 |
| `!!logfilter reload` | admin | 重读配置文件并立即生效，无需重启 |

`!!logfilter test` 特别实用：把日志原文粘进去就能确认规则对不对，不用真等它触发。

## 用例：加更多规则

把要过滤的日志正文加进 `patterns` 数组即可：

```json
{
    "patterns": [
        "standing on air - force-sending blocks below",
        "Ignoring chat session from .* due to missing Services public key",
        "^\\[Async Chat Thread"
    ],
    "log_matched_lines": false,
    "report_on_server_stop": true
}
```

改完先 `!!logfilter test <文本>` 验证，再 `!!logfilter reload`。

规则写错不会导致插件崩溃——编译失败的规则会被跳过并在 MCDR 日志里给出警告，其余规则照常工作。

## 已验证内容

### 单元测试（过滤逻辑、边界与安全属性）

- 目标刷屏行被隐去，且保留 `process`（关键安全属性）
- **11 类绝不能误伤的行逐一验证不受影响**：启动完成 `Done (...)!`、玩家进出、`Stopping the server`、
  `moved too quickly`、`moved wrongly`、`Rejecting UseItemOnPacket`、`dropping items too fast`、
  聊天签名问题、`lost connection`、版本启动行
- `content` 为 `""` / `None` / 纯空白时不崩溃
- 换玩家名同样命中
- 多规则各自独立计数；`reload` 后计数归零
- 非法正则被跳过且产生警告，不影响其他规则
- 近乎相同但不同的行**不**命中（证明不是无脑全过滤）

### 端到端（真实 MCDR 2.15.7 + 假服务端，跑完整生命周期）

控制台回显实测：

| 日志行 | 结果 |
|---|---|
| `Player <任意玩家> standing on air - force-sending blocks below` | ✅ 隐去（出现 0 次） |
| `Steve moved too quickly!` / `moved wrongly!` | ✅ 保留 |
| `Steve joined the game` | ✅ 保留 |
| 任意普通日志行 | ✅ 保留 |
| `Done (0.648s)! For help, type "help"` | ✅ 保留（MCDR 启动检测正常） |
| `Stopping the server` | ✅ 保留（MCDR 停止检测正常） |

插件日志：

```
插件 server_log_filter@1.0.1 已加载
已启用 1 条日志过滤规则；命中后仅从 MCDR 控制台隐去，服务端日志不受影响
本次运行共从 MCDR 控制台隐去 3 行服务端日志（服务端日志文件不受影响）
```

全程**无任何报错**。

## 自行打包

仓库结构为 MCDR 标准的「根元数据 + 同名代码子包」：

```
MCDR-ServerLogFilter/
├── mcdreforged.plugin.json
├── LICENSE
├── README.md
├── README_en.md
└── server_log_filter/
    └── __init__.py
```

打包（务必排除 `__pycache__` 与 `.pyc`）：

```python
import zipfile
from pathlib import Path

src = Path(".").resolve()
out = Path("ServerLogFilter-v1.0.1.mcdr")
skip = {".git", "__pycache__"}

files = [
    p for p in src.rglob("*")
    if p.is_file()
    and not (skip & set(p.parts))
    and p.suffix != ".pyc"
]
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    for p in files:
        z.write(p, p.relative_to(src).as_posix())
```

## 环境要求

- MCDReforged **>= 2.13.0**（`InfoFilter` API 自 2.13 起可用）
- Python >= 3.9（随 MCDR 自带）

## 许可证

[MIT](LICENSE)
