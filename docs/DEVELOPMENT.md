# 开发与打包

这一份面向**要改代码、打包、或核对兼容性**的人。
只想把插件用起来的读者请回 [README.md](../README.md)。

- [仓库结构](#仓库结构)
- [自行打包](#自行打包)
- [测试](#测试)
- [已验证内容](#已验证内容)
- [环境要求的由来](#环境要求的由来)
- [版本兼容性实测](#版本兼容性实测)
- [发布流程](#发布流程)

---

## 仓库结构

本插件是 MCDR 标准的「根元数据 + 同名代码子包」：

```
MCDR-ServerLogFilter/
├── mcdreforged.plugin.json    插件元数据
├── server_log_filter/         代码子包（与插件 id 同名）
│   └── __init__.py
├── CHANGELOG.md               变更记录（随发布包分发）
├── LICENSE
├── README.md / README_en.md   主文档，面向使用者（不打包）
├── docs/DEVELOPMENT.md        本文件（不打包）
├── pack.py                    打包器
├── tests/                     测试套件（含 tests/README.md 细则）
├── tools/                     变异检查、跨版本矩阵
└── benchmarks/                性能基准
```

只有 `pack.py` 的白名单里列出的文件会进发布包，其余一律不外发。

## 自行打包

```bash
python pack.py            # 生成 ServerLogFilter-v<版本号>.mcdr
```

发布包里只有 **4 个文件**：

| 文件 | 说明 |
|---|---|
| `mcdreforged.plugin.json` | 插件元数据（必需） |
| `server_log_filter/__init__.py` | 插件代码 |
| `CHANGELOG.md` | 变更记录，随包分发 |
| `LICENSE` | MIT 许可证 |

两个 README **不打包**：MCDR 从不读取它们，内容与 Releases 页面重复，而去掉后产物体积几乎减半。

> **为什么必须用白名单？** 早期版本的这一节用的是 `rglob("*")` 加一个很短的 `skip` 列表，
> 那是**黑名单**思路，会被仓库里任何新文件悄悄带进发布包。两个具体后果：
>
> 1. **发布包直接加载失败。** MCDR 会校验 `.mcdr` 根级条目（`PackedPlugin._check_dir_legality`），
>    根目录出现 `conftest.py`、`setup.py` 这类模块就抛
>    `IllegalPluginStructure: Packed plugin cannot contain other module`。测试用的
>    `conftest.py` 恰好就在根目录，所以黑名单方案会让插件**完全无法加载**。
> 2. **体积失控。** 按 `tests/README.md` 装了 `.testlibs/` 之后，黑名单会把整个 MCDR
>    及其依赖一起打进包里：实测 **1362 个文件、7.11 MB**。
>
> `pack.py` 本身也不进包，原因和 `conftest.py` 相同——根级模块会让 MCDR 拒绝加载。

> ⚠️ **`.mcdr` 是含时间戳的 zip，因此哈希不能跨检出目录复现。**
> `git checkout` 会重写工作区文件的 mtime，同一个目录连续构建两次哈希是稳定的，
> 但换检出目录或重新 checkout 之后构建，哈希就会变（**内容相同**）。
> 所以 **Release 正文里的 SHA256 必须用实际上传的那个文件来算**。

## 测试

```bash
python -m pip install --target .testlibs -r tests/requirements-test.txt
PYTHONPATH=.testlibs python -m pytest tests -v     # Windows: $env:PYTHONPATH=".testlibs"
```

当前 **148 个用例**，分三层：

| 层 | 位置 | 说明 |
|---|---|---|
| 单元 | `tests/test_plugin.py` | 过滤逻辑、配置对象、命令面、打包、迁移、坏配置保全 |
| 端到端 | `tests/test_e2e.py` | 启动**真实 MCDR**，加载 `pack.py` 产出的 `.mcdr`，用假服务端跑完整生命周期 |
| 跨版本 | `tools/mcdr_matrix.py` | 同一个包在多个 MCDR 版本上逐项核对 |

另有两个工具：

```bash
python tools/mutation_check.py    # 故意改坏实现，确认测试会变红（14 个变异，14/14 应被抓住）
python benchmarks/bench_filter.py # 性能基准，README / CHANGELOG 里引用的数字都由它产出
```

`tools/mcdr_matrix.py` 用法：传入若干「装了 MCDR 的解释器」，插件包会用 `pack.py` 现场构建。

```bash
python tools/mcdr_matrix.py /path/to/mcdr-2.15.0/python /path/to/mcdr-2.15.7/python /path/to/mcdr-2.16.0/python
python tools/mcdr_matrix.py --current
```

细则（用例分组、变异清单、踩过的坑）见 [tests/README.md](../tests/README.md)。

## 已验证内容

### 单元测试（过滤逻辑、边界与安全属性）

- 目标刷屏行被隐去，且保留 `process`（关键安全属性）
- **15 类绝不能误伤的行逐一验证不受影响**：启动完成 `Done (...)!`、玩家进出、`Stopping the server` /
  `Stopping server`、`moved too quickly`、`moved wrongly`、`Rejecting UseItemOnPacket`、
  `dropping items too fast`、聊天签名问题、`lost connection`、版本启动行、`Preparing level`、
  死亡消息、`Saving and pausing game...`
- `content` 为 `""` / `None` / 纯空白时不崩溃
- 换玩家名同样命中
- 多规则各自独立计数；`reload` 后计数归零
- 非法正则被跳过且产生警告，不影响其他规则
- 近乎相同但不同的行**不**命中（证明不是无脑全过滤）
- **零命中提醒**：达阈值才提醒、命中即归零、启动失败的周期不计入、热重载不误判、
  可从配置关闭、`reset` 能清空、提醒内容不重复
- **正则安全检查**：4 类灾难性回溯模式被拦下，8 种真实写法全部放行
- **升级提示与迁移**：旧版本格式的配置在真实 MCDR 上自动补齐，并在日志里报告新增了哪些选项；
  每个配置项都必须写明加入版本，且说明必须**紧挨在选项正上方一行**（均有测试保证）
- **坏配置保全**：解析失败时原文件被备份为 `config.json.old`，新配置以默认值重建，
  并在日志里给出原因与出错行列；空文件、顶层非对象同样处理

### 端到端（真实 MCDR + 假服务端，跑完整生命周期）

控制台回显实测（默认规则）：

| 日志行 | 结果 |
|---|---|
| `Player <任意玩家> standing on air - force-sending blocks below` | ✅ 隐去（出现 0 次） |
| `Steve moved too quickly!` / `moved wrongly!` | ✅ 保留 |
| `Steve joined the game` | ✅ 保留 |
| 任意普通日志行 | ✅ 保留 |
| `Stopping server` | ✅ 保留 |

插件日志：

```
插件 server_log_filter@1.1.0 已加载
已启用 1 条日志过滤规则；命中后仅从 MCDR 控制台隐去，服务端日志不受影响
本次运行共从 MCDR 控制台隐去 3 行服务端日志（服务端日志文件不受影响）
```

全程**无任何报错**。

另外还有一条**带 canary 的端到端用例**，专门守「隐去但保留事件分发」这个安全属性：
它额外让过滤规则命中**服务端启动行**，然后同时断言「该行确实离开了控制台」和
「MCDR 的 `SERVER_STARTUP` 事件仍然被派发」。这样 `hidden()` 与 `discarded()`
这两种实现才会产生可观测的差异——否则默认规则只匹配刷屏行，而刷屏行不参与生命周期判定，
两种实现看起来一模一样。

测试还断言了最关键的安全属性——被隐去的行**保留 `process`、仅摘掉 `echo_to_console`**，
并且**真的没有出现在控制台上**，同时**事件仍然照常派发**。
如果未来 MCDR 改变 `hidden()` 的语义，测试会直接失败，而不是让插件在服务器上静默出问题。

端到端组共用一个 MCDR 实例（约 5～6 秒），可用 `MCDR_SKIP_E2E=1` 跳过。

## 环境要求的由来

- **MCDReforged >= 2.15.0**

  > 为什么不是 2.13？本插件的核心设计依赖 `InfoActionFlag.hidden()`（只摘控制台回显、保留事件分发），
  > 而这个类是 **MCDR 2.15.0** 才引入的。2.14.x 及更早的 `InfoFilter` 只有「返回 `False` 即丢弃整条」
  > 的语义，无法等价实现本插件的行为，所以不支持。
  > 装到旧版本上不会静默出错——MCDR 会直接提示 `依赖项 mcdreforged@x.y.z 不满足版本约束 >=2.15.0`。

- Python >= 3.9（随 MCDR 自带）

**门槛只由 `InfoActionFlag` 决定。** 后续版本新增功能用到的 API
（`save_config_simple`、带 `file_name` 的 `load_config_simple`、嵌套 `Serializable`、
`on_server_startup`、`data_processor`）经逐版本检查，**自 MCDR 2.13.0 起就全部存在**，
因此最低版本要求一直没有变化。

## 版本兼容性实测

| MCDR 版本 | 结果 |
|---|---|
| 2.13.0 / 2.14.1 | 被 MCDR 的依赖检查拦下，提示 `依赖项 mcdreforged@x.y.z 不满足版本约束 >=2.15.0` |
| **2.15.0**（最低支持） | 加载、过滤、零命中提醒、状态文件、命令（含 `!!logfilter reset`）全部实测通过 |
| 2.15.7 | 同上，全部通过 |
| 2.16.0 | 同上，全部通过 |

全部功能在这三个版本上行为**完全一致**（实测：提醒出现时机、连续零命中计数推进、
状态文件写入、命令树注册均相同，零异常）。可用 `tools/mcdr_matrix.py` 自行复跑。

Minecraft 侧：过滤发生在 MCDR 侧（对服务端 stdout 逐行匹配），
因此**与 MC 版本无耦合**。1.16.5 / 1.19.4 / 1.20.1 / 1.20.6 / 1.21.8 / 1.21.11 /
26.1 / 26.2 / 26.3 均已用真实服务端实测通过。

唯一与 MC 版本相关的是**默认规则的目标日志**：`standing on air - force-sending blocks below`
仅由 **MC 26.3** 产生；装到更早版本上插件照常工作，只是默认规则不会命中任何内容。

## 发布流程

1. 改动合入 `main`（对外动作前先征得维护者同意）
2. 更新 `CHANGELOG.md` 与 `mcdreforged.plugin.json` 里的版本号
3. 判定是否属于**实质更新**：只有用户拿到的东西变了（插件行为 / 配置项 / 命令 / 兼容性）
   才发新版本；纯测试、纯文档、纯打包脚本改动不发版本
4. `python pack.py` 构建产物，并记下**该文件**的 SHA256
5. 创建 Release 并上传资产；正文里写明改动与 SHA256

> 打 tag 可以交给 Release API 一并完成：创建 Release 时同时给 `tag_name` 与
> `target_commitish=<sha>` 即可，不必单独 push tag。
