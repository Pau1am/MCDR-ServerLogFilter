# Changelog

本插件所有值得一提的变更都记录在此。插件包内也附带同一份 `CHANGELOG.md`。

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

---

## [1.1.1] - 2026-10-04

### 变更

**发布包不再包含两个 README，产物体积减半：28 KB → 14 KB。**

`README.md` / `README_en.md` 从打包白名单中移除。理由：MCDR 从不读取它们，
内容与 Releases 页面重复，而两者加起来占了整个包压缩体积的 **49.6%**
（13,783 字节 / 27,805 字节）。

发布包内容现在是固定的 4 个文件：

| 文件 | 说明 |
| --- | --- |
| `mcdreforged.plugin.json` | 插件元数据（必需） |
| `server_log_filter/__init__.py` | 插件代码 |
| `CHANGELOG.md` | 变更记录，随包分发 |
| `LICENSE` | MIT 许可证 |

`CHANGELOG.md` 与 `LICENSE` **保留在包内**：前者是「本次改了什么写进包内自带说明文件」
这一约定的载体，后者随分发更符合 MIT 的常规做法。

新增 `test_packager_ships_exactly_the_allowlist`，把产物内容钉死——
以后任何对白名单的改动都会让测试变红，必须显式修改测试，
不会再有「体积悄悄变了但没人说」的情况。

> **行为没有任何变化。** 这一版只动打包清单，插件代码、配置项、命令与
> 兼容性要求（MCDR >= 2.15.0）与 1.1.0 完全一致。仅为了拿到更小的包才需要升级。

---

## [1.1.0] - 2026-10-04

### 新增

1. **零命中提醒。** 某条规则如果连续若干个开服周期一次都没命中，会在下一次**开服完成
   （控制台出现 `Done`）之后**提醒一次，列出规则原文、连续零命中次数与历史命中情况，
   并给出处置建议。用于发现写错的规则、以及发现「日志已不再产生、规则可以删了」。
   - 新配置 `warn_about_stale_rules`（默认 `true`）、`stale_rule_threshold`（默认 `3`，设 `0` 关闭）
   - 统计存放在 `config/server_log_filter/state.json`，与用户手写的 `config.json` **分开**，互不干扰
   - 只在**完成过启动**的周期上统计；服务端启动失败不会把规则刷成零命中
   - 插件热重载时会把已有点击计数接续过来，避免把一次开服周期切成两段后误判
   - 新命令 `!!logfilter reset`（admin）：清空连续零命中计数
   - `!!logfilter` 状态输出中会标出各规则的连续零命中次数

2. **正则安全检查。** 载入时用短探测串检查每条规则是否存在灾难性回溯，命中则跳过该规则并报错。
   带嵌套量词的正则（如 `(a+)+$`）实测**单行**即可耗时数百毫秒乃至数秒，会冻结 MCDR 主线程。
   - 新配置 `validate_patterns`（默认 `true`）、`pattern_probe_timeout_ms`（默认 `25`）
   - 该检查只在载入时执行一次，正常规则耗时微秒级

### 优化

- `InfoActionFlag.hidden()` 改为构造时计算一次并复用（它是不变常量），去掉命中路径上的一次函数调用
- 无规则时直接短路返回，不再取日志正文
- 规则容器改为 `tuple`（迭代略快，且避免外部误改；整体赋值天然原子）
- 正则只编译一次：校验用过的编译结果直接交给匹配使用

> **关于性能的说明**：上述优化加起来约减少 10% 的单行开销，但绝对值本来就极小——实测
> 单行过滤约 0.2 µs，不到 MCDR 自身解析同一行开销的十分之一；即便 1000 行/秒的极端突发，
> 每小时累计也只有 0.7 秒。真正会拖慢服务端的是**写坏的正则**（见上），而不是规则条数。
> 数字可用 `benchmarks/bench_filter.py` 复现。

### 变更

- 版本号 1.0.2 → 1.1.0

### 仓库内新增工具（不随插件包分发）

- `benchmarks/bench_filter.py` —— 性能基准，上文那些数字都由它产出，可自行复跑
- `tools/mutation_check.py` —— 变异检查：故意改坏实现，确认测试会变红
- `tools/mcdr_matrix.py` —— 跨 MCDR 版本矩阵：传入若干装了 MCDR 的解释器，
  自动核对加载、过滤、提醒、状态文件与命令树

### 兼容性

**最低 MCDR 版本要求没有变化，仍是 2.15.0。**

原因是门槛只由 `InfoActionFlag` 决定（2.15.0 引入），而本次新增功能用到的其它 API
——`save_config_simple`、带 `file_name` 的 `load_config_simple`、嵌套 `Serializable`、
`on_server_startup`——经逐版本检查，**自 MCDR 2.13.0 起就全部存在**。

| MCDR 版本 | 结果 |
| --- | --- |
| 2.13.0 / 2.14.1 | 仍被依赖检查拦下，提示「依赖项 mcdreforged@x.y.z 不满足版本约束 >=2.15.0」 |
| **2.15.0**（最低支持） | 加载、过滤、零命中提醒、状态文件、命令（含 `!!logfilter reset`）全部通过 |
| 2.15.7 / 2.16.0 | 同上，全部通过 |

在 2.15.0 / 2.15.7 / 2.16.0 上，**新功能行为完全一致**，已逐项实测确认：
提醒出现时机（在 `Done` 之后）、提醒内容（规则名 / 连续次数 / 「从未命中过」）、
`session_index` 与连续零命中计数的推进、状态文件写入、命令树（`!!logfilter` 及
`list` / `reload` / `reset` / `test` 四个子命令）、以及零运行时异常。

MC 侧结论不变：过滤在 MCDR 侧完成，**与 MC 版本无耦合**。

---

## [1.0.2] - 2026-10-02

### 修复

1. **最低 MCDR 版本声明由 `>=2.13.0` 修正为 `>=2.15.0`。**
   经逐版本实测，`mcdreforged.api.types.InfoActionFlag` 是 **MCDR 2.15.0** 才引入的 API，
   2.13.0 / 2.13.1 / 2.13.2 / 2.14.0 / 2.14.1 的整包内均不存在该类。
   旧声明会让这些版本上的用户在加载阶段直接遇到
   `ImportError: cannot import name 'InfoActionFlag' from 'mcdreforged.api.types'`，
   且插件被标记为加载失败。

2. **低版本 MCDR 上改为给出可读提示。**
   `InfoActionFlag` 改为容错导入；当 MCDR < 2.15.0 时，插件会打印
   「需要 MCDR >= 2.15.0，当前版本不支持 InfoActionFlag，插件已停用」并放弃注册，
   而不是抛出难以理解的裸 `ImportError`。
   配合修正后的依赖声明，实际运行时由 MCDR 原生依赖检查直接拦截，
   提示为 `依赖项 mcdreforged@x.y.z 不满足版本约束 >=2.15.0`。

   > 本插件「只摘控制台回显、保留事件分发」的设计依赖 `InfoActionFlag.hidden()`；
   > 2.14.x 及更早的 `InfoFilter` 只有「返回 `False` 即丢弃整条」的语义，无法等价实现，
   > 因此不存在兼容 2.14 及以下版本的写法。

### 未改动

- 过滤逻辑、命令、配置项与文件格式**全部不变**，从 1.0.1 升级无需调整任何配置。

### 兼容性实测结论

| 维度 | 结果 |
| --- | --- |
| MCDR 2.13.0 / 2.14.1 | 1.0.1 加载失败；1.0.2 由依赖检查明确拦截 |
| MCDR 2.15.0 / 2.15.7 / 2.16.0 | 加载与过滤均正常 |
| MC 1.16.5 / 1.19.4 / 1.20.1 / 1.20.6 / 1.21.8 / 1.21.11 / 26.1 / 26.2 / 26.3 | 真实服务端实测通过，服务端原生日志完整 |

---

## [1.0.1] - 2026-10-01

### 变更

- 元数据迁移到新 schema：`authors`（含 `link`）、`links.homepage`、`license: MIT`，
  以满足 MCDReforged 官方插件目录的收录校验。

---

## [1.0.0] - 2026-10-01

### 新增

- 首个版本。基于 `InfoActionFlag.hidden()` 实现「只从 MCDR 控制台隐去、保留事件分发」的日志过滤。
- 命令：`!!logfilter`（状态）、`list`、`test <文本>`、`reload`（需 admin）。
- 配置：`config/server_log_filter/config.json` 的 `patterns[]` / `log_matched_lines` / `report_on_server_stop`。
- 匹配使用 `re.search`（包含匹配），正则**不要**自己加 `.*`。

[1.1.1]: https://github.com/Pau1am/MCDR-ServerLogFilter/releases/tag/v1.1.1
[1.1.0]: https://github.com/Pau1am/MCDR-ServerLogFilter/releases/tag/v1.1.0
[1.0.2]: https://github.com/Pau1am/MCDR-ServerLogFilter/releases/tag/v1.0.2
[1.0.1]: https://github.com/Pau1am/MCDR-ServerLogFilter/releases/tag/v1.0.1
[1.0.0]: https://github.com/Pau1am/MCDR-ServerLogFilter/releases/tag/v1.0.0
