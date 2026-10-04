# 测试 / Tests

本目录是 Server Log Filter 的测试套件。**113 个用例**，覆盖过滤行为、配置、
命令面、发布打包、**真实 MCDR 端到端**，以及本插件最核心的安全属性
（被隐去的行仍保留 `process`，事件照常分发）。

MCDR 是**硬依赖**：插件配置类继承自 `mcdreforged.api.utils.Serializable`，
没有 MCDR 连 `import server_log_filter` 都会失败。所以下面的步骤是必需的。

## 安装测试依赖

不需要建虚拟环境（`python -m venv` 在某些受限环境里会因为 `ensurepip` 失败）。
直接装到一个本地目录：

```bash
python -m pip install --target .testlibs -r tests/requirements-test.txt
```

`.testlibs/` 已在 `.gitignore` 里，不会被提交。

## 运行

```bash
# Linux / macOS
PYTHONPATH=.testlibs python -m pytest tests -v

# Windows PowerShell
$env:PYTHONPATH=".testlibs"; python -m pytest tests -v
```

预期输出结尾：

```
113 passed
```

## 覆盖内容

| 分组 | 用例数 | 说明 |
|---|---|---|
| 过滤行为与安全属性 | 30 | 目标刷屏行命中（3）；15 类关键行逐一验证**不**被误伤；`content` 为 `""`/`None`/纯空白不崩溃（5）；`hidden()` 保留 `process` 且绝不等于 `discarded()` |
| 规则编译与容错 | 4 | 非法正则被跳过并告警，其余规则照常工作；空/空白规则静默丢弃 |
| 计数、重载与重置 | 7 | 多规则独立计数；`reload` 后归零；首条命中规则胜出且只计一次 |
| 配置对象 | 3 | 默认值与 README 文档一致；JSON 反序列化；往返稳定 |
| MCDR 契约 | 2 | `InfoActionFlag.hidden()` 的常量构成；`InfoFilter` 允许改写 `action_flag` |
| 命令面与元数据 | 8 | `on_load` 注册项；状态/测试/重载命令输出；`reload` 的 ADMIN 权限门禁；插件元数据与 `MIN_MCDR_VERSION` 同步 |
| 发布打包 | 6 | 见下 |
| **零命中提醒** | **13** | 阈值语义、命中归零、启动失败不计入、热重载不误判、可关闭、`reset`、历史持久化，见下 |
| **灾难性回溯防护** | **21** | 8 种真实写法全部放行；4 类危险模式被拦下；开关与超时可配置且**确实接在 `on_load` 上** |
| **轻量化不变量** | **6** | 热路径的结构性断言（缓存 `hidden()`、无规则短路、命中即停），外加两条宽松的耗时护栏 |
| **端到端（真实 MCDR）** | **13** | 见下 |
| **合计** | **113** | |

> 计数含 `@pytest.mark.parametrize` 展开后的用例数，与 `pytest --collect-only` 一致。

## 端到端测试（`tests/test_e2e.py`）

这一层做单测原理上做不到的事：**启动一个真的 MCDR**，加载 `pack.py` 产出的 `.mcdr`
（不是源码目录），用一个假服务端跑完整生命周期，然后检查 MCDR **真正的控制台输出**。

流程：`Done (...)!` 启动 → 玩家进出 → 两条目标刷屏行 → 若干无害行 → `Stopping server` 退出。
假服务端以退出码 0 结束，因此 MCDR 能走完正常的停止流程并触发插件的 `on_server_stop`。

| 用例 | 断言 |
|---|---|
| `test_target_lines_are_hidden_from_the_console` | 目标行回显次数为 **0** |
| `test_innocent_lines_still_reach_the_console` | 版本行 / `Preparing level` / 玩家进出 / `moved too quickly` / `lost connection` / `Stopping server` / `Saving players` 全部保留 |
| `test_hidden_line_keeps_process_flag_end_to_end` | **安全属性的真正守门测试**，见下 |
| `test_mcdr_completes_the_lifecycle` | `Server stopped` / `Server process stopped with code 0` 正常出现（仅确认整轮运行健康，**不**证明安全属性） |
| `test_plugin_reports_what_it_hid` | 停止时汇总为「隐去 **3** 行」（2 条目标行 + 1 条 canary） |
| `test_packaged_plugin_is_what_was_loaded` | 加载的确实是 `.mcdr`，插件目录里没有源码副本 |
| `test_plugin_generated_its_default_config` | 首次运行生成的字段集合与文档完全一致 |
| `test_plugin_keeps_its_state_file_out_of_the_user_config` | 统计写入独立的 `state.json`，用户手写的 `config.json` 不被插件改动 |
| `test_healthy_run_reports_no_stale_rule` | 首次运行没有历史，且两条规则都命中，因此**不该**有任何提醒 |
| `test_stale_rule_warning_arrives_after_the_server_finished_starting` | 提醒**出现在服务端 `Done` 之后**（同一次运行里比对行序），且 `SERVER_STARTUP` 确实派发过 |
| `test_stale_rule_warning_is_specific_and_actionable` | 提醒里含阈值、真实连续次数、`!!logfilter test` 与 `reset` 的处置提示 |
| `test_only_the_idle_rule_is_flagged` | 本轮命中过的规则不会被牵连进提醒 |
| `test_state_file_advances_by_one_session` | 关服后 `session_index` 与各规则的连续零命中次数被正确推进 |
| `test_plugin_generated_its_default_config` | 首次运行生成 `config/server_log_filter/config.json`，字段与默认值正确 |

**这不是空断言**：把插件从 `plugins/` 拿掉后重跑，目标行回显 2 次，第 1 条用例会失败。

### 为什么必须有 canary：一段值得记下的教训

最初的端到端组里有一条
`test_mcdr_startup_and_stop_detection_still_work`，断言 `Done (...)` / `Stopping server` /
`Server stopped` 会出现，docstring 声称「若插件退化成 `discarded()` 就会失败」。

**这个声明是错的。** 实测：把插件里的 `InfoActionFlag.hidden()` 换成
`InfoActionFlag.discarded()`（即破坏本插件最核心的安全属性），端到端 6 条用例**全部通过**，
只有 2 条单元用例抓到。

原因有两层：

1. **默认规则只匹配刷屏行**，而刷屏行本来就不参与 MCDR 的生命周期判定 —— 所以
   `hidden()` 和 `discarded()` 对这些行的可见性**毫无区别**。
2. **停止检测根本不是行驱动的**。`test_server_stopping` 只用来断开 RCON
   （`server_reactor.py`），停止事件由**服务端进程退出**触发。因此任何「停止仍被检测到」
   的断言都**永远**无法区分这两种实现。

修法（已实装）——让 canary 规则去命中一条 MCDR **真正依赖**的行。经查，启动检测确实是行驱动的：

```python
# mcdreforged/info_reactor/impl/server_reactor.py
if handler.test_server_startup_done(info):
    self.mcdr_server.add_flag(MCDReforgedFlag.SERVER_STARTUP)
    self.mcdr_server.plugin_manager.dispatch_event(MCDRPluginEvents.SERVER_STARTUP, ())
```

所以端到端运行会额外注入一条 canary 规则（`For help, type`，命中启动行），并挂一个探针插件
记录 `on_server_startup` 是否被调用，然后**两头都断言**：

| 实现 | canary 行离开控制台 | `SERVER_STARTUP` 仍派发 | 判定 |
|---|---|---|---|
| `hidden()`（正确） | ✅ | ✅ | 通过 |
| `discarded()`（变异） | ✅ | ❌ | **失败** |

这样「什么都没过滤」和「把整条丢掉了」两种退化都逃不掉。

> ⚠️ **canary 模式必须正则安全。** 第一版直接用完整启动行
> `Done (0.648s)! For help, type "help"` 当模式，其中的 `(` `)` 被正则当成分组，
> 导致**永远匹配不到**、canary 静默失效、测试又退化成空断言。用 `For help, type`
> 这种无元字符的子串才稳。这同时也印证了 README 里「匹配用 `re.search`，别自己加 `.*`」那条提醒。

两个让测试跑得快且稳定的细节：

- `handler_detection: false` —— MCDR 的处理器自动探测会先采样**一整分钟**
  （`HANDLER_DETECTION_MINIMUM_SAMPLING_TIME = 60`）才启动服务端，必须关掉并显式指定 handler。
- `start_command` 里的解释器路径**要加引号**：路径含空格（如 `Paul LAM`）时，不加引号会被
  `cmd.exe` 截断成 `'C:\Users\Paul' is not recognized...`。

端到端组共用一个 MCDR 实例（约 5～6 秒，仅启动一次 MCDR）；`MCDR_SKIP_E2E=1` 可跳过，
缺少 `.testlibs` 时自动 skip。

## 发布打包的回归防护

`pack.py` 是仓库唯一的打包器，它的产物由这里 6 个用例把关：

- `test_packaged_artifact_is_loadable` —— 跑一遍 `pack.py`，把产物交给 **MCDR 自己的**
  `PackedPlugin._check_dir_legality` 校验
- `test_packager_excludes_repo_infrastructure` —— `conftest.py` / `pack.py` / `tests/` /
  `.testlibs/` / `__pycache__` 都不在包里
- `test_packager_ships_exactly_the_allowlist` —— **把产物内容钉死为 4 个文件**
  （`mcdreforged.plugin.json` / `server_log_filter/__init__.py` / `CHANGELOG.md` / `LICENSE`），
  并断言两个 README **不在**包里。这样任何对白名单的改动都必须显式改测试，
  避免再出现「体积悄悄变了但没人知道」
- `test_packager_keeps_artifact_small` —— 文件数 < 20 且 < 200 KiB
- `test_packager_ships_submodules_recursively` —— 在临时副本里植入
  `server_log_filter/sub/helper.py`，断言它**确实被打了进去**
- `test_packager_root_entries_would_be_illegal_if_denylisted` —— 用真实的黑名单产物断言
  MCDR **确实会拒绝**它，防止有人把 `pack.py` "简化"回黑名单写法

这一节存在的直接原因：本测试套件自己的 `conftest.py` 位于仓库根目录，而 MCDR 禁止 `.mcdr`
包含根级模块。如果打包器用 `rglob("*")` 加简短的排除列表（README 早期写法），产物会以
`IllegalPluginStructure` 加载失败；装了 `.testlibs/` 后还会膨胀到 **1362 个文件 / 7.11 MB**。
这类问题不会在单元测试里暴露，只有真的打包并交给 MCDR 校验才会发现。

> 另一条同样隐蔽的坑：白名单如果写成 `rel.parent == Path(PACKAGE_NAME)`，就只收**直接子级**，
> `server_log_filter/sub/helper.py` 这类嵌套子模块会被**静默丢弃**——打出一个缺文件的包，
> 而且不报任何错。现在的实现用 `rel.parts[0]`，并有上面的用例守着。

## 零命中提醒（第 8 组）

统计只在**开服 / 关服两个时点**更新，逐行过滤路径上没有任何额外开销
（有专门用例从结构上证明这一点：过滤 50 行期间不会触发任何状态写盘）。

两条最容易被忽略、也最要紧的保护：

1. **只统计「完成过启动」的周期。** 否则服务端连续启动失败（mod 报错之类）会把所有规则
   刷成零命中，然后给出完全错误的提醒。用例：`test_a_session_that_never_reached_startup_is_not_counted`。
2. **插件热重载要接续计数。** MCDR 的 `!!MCDR reload plugin` 会重新执行 `on_load`，
   新过滤器从 0 开始计数；若不管，一次正常命中的开服周期会被切成两段、看起来像零命中。
   用例：`test_reload_does_not_fake_an_idle_session`。

## 灾难性回溯防护（第 9 组）

带嵌套量词的正则匹配失败时指数级回溯，实测**单行**就要数百毫秒到数秒：

| 模式 | 输入长度 | 单行耗时 |
|---|---|---|
| `(a+)+$` | 23 | 182 ms |
| `(a+)+$` | 27 | 3.0 s |
| `^(a|a)*$` | 25 | 753 ms |

对照正常过滤：**0.2 µs/行**，不到 MCDR 自身解析同一行开销的十分之一。
也就是说这类规则比正常情况慢 **数百万倍**，足以冻结 MCDR 主线程——
这才是本插件唯一可能真正影响性能的情形。

防护做法是在载入时用 `a*22` / `a*22+"!"` 等短探测串试跑每条规则，超预算即跳过。
参数是这样标定的（`benchmarks/bench_filter.py` 可复现）：

- 4 类危险模式在探测串上耗时 190~380 ms，全部超过 25 ms 预算 → 被拦下
- 8 种真实写法在同一批探测串上最坏仅几微秒 → 余量约 **9000 倍**，不会误伤

（具体数值随机器而变，这里看的是数量级差距。）

注意最后三条用例守的是**接线**而不是函数本身：变异测试发现，只在 `_build_rules` 层面测
「危险模式被拦下」，把 `on_load` 里的 `validate=_config.validate_patterns` 改成
`validate=False` 时测试**依然全绿**。补上 `test_on_load_actually_applies_the_probe` /
`test_on_load_respects_validate_patterns_false` / `test_on_load_honours_the_timeout_setting`
之后才守得住。

## 变异测试

判断一组测试有没有效，唯一可靠的办法是**故意把实现改坏，看测试会不会变红**。
本套件的关键断言都经过这一步验证，可以直接复跑：

```bash
python tools/mutation_check.py
```

脚本会依次注入 7 个缺陷，要求相关用例变红；全绿即视为测试失效。已确认能被抓住的变异：

| 变异 | 抓住它的用例 |
|---|---|
| 关闭零命中提醒 | 第 8 组多例 |
| 阈值判断 `>=` 改成 `>` | `test_idle_rule_is_reported_once_the_streak_reaches_the_threshold` |
| 去掉「必须完成启动」的保护 | `test_a_session_that_never_reached_startup_is_not_counted` |
| 热重载计数接续失效 | `test_reload_does_not_fake_an_idle_session` |
| 热重载丢失「本周期已完成启动」标记 | `test_reload_carries_the_running_session_over_to_the_new_module` |
| 关闭灾难性回溯探测 | `test_on_load_actually_applies_the_probe` 等 |
| 提醒时机提前到 `Done` 之前 | `test_stale_rule_warning_arrives_after_the_server_finished_starting` |

> 教训留在这里：本套件的端到端组**曾经**声称能挡住 `hidden()` → `discarded()` 的回归，
> 实测 6 条用例全部漏过，只有 2 条单元用例抓到。现在那条属性由
> `test_hidden_line_keeps_process_flag_end_to_end` 用 canary + 探针插件真正守住。



## 与 README「已验证内容」的关系

README 的「已验证内容」一节描述了这些行为，但此前仓库中并没有对应代码。
本套件把那些描述变成了**可重复执行**的断言，其中最关键的一条是：

```python
info.action_flag = InfoActionFlag.hidden()   # 只摘控制台回显
assert InfoActionFlag.process in info.action_flag        # 事件分发保留
assert InfoActionFlag.echo_to_console not in info.action_flag
```

这条断言在真实 MCDR 2.16.0 上运行，因此如果未来 MCDR 改变了 `hidden()` 的语义，
测试会立刻失败，而不是让插件在用户的服务器上静默出问题。

**注意分工**：上面这条是**单元**层的断言（直接检查 action_flag 的位），
`test_hidden_line_keeps_process_flag_end_to_end` 则是它的**端到端**对应物
（在真机上观察事件是否仍被派发）。两层都要有——单元层精确指出哪一位丢了，
端到端层证明确实穿透了 MCDR 的完整链路。最早只有单元层，端到端层是补上来的。

> 判断测试有没有效的方法：**跑变异测试**——故意把被测代码改坏，看测试会不会变红。
> 光看「全绿」没有说服力，本套件最初就是 64 项全绿却漏掉了最关键的回归。

## MCDR 版本覆盖

`pytest` 跑在钉住的 MCDR 2.16.0 上（`tests/requirements-test.txt`）。除单元 / 端到端两层外，
新增功能还在**最低支持版本**上单独验证过，否则「最低 2.15.0」只是未经验证的声明。
这个矩阵可以直接复跑：

```bash
# 传入若干「装了 MCDR 的 python 解释器」，插件包会用 pack.py 现场构建
python tools/mcdr_matrix.py /path/to/mcdr-2.15.0/python /path/to/mcdr-2.15.7/python /path/to/mcdr-2.16.0/python

# 或者只测当前解释器
python tools/mcdr_matrix.py --current
```

实测结果（2026-10-04）：

| 检查 | 2.15.0 | 2.15.7 | 2.16.0 |
|---|---|---|---|
| 插件加载 + 过滤 | ✅ | ✅ | ✅ |
| 零命中提醒出现，且在 `Done` 之后 | ✅ | ✅ | ✅ |
| 提醒内容（规则名 / 连续次数 / 从未命中过） | ✅ | ✅ | ✅ |
| `state.json` 写入、`session_index` 与 streak 正确推进 | ✅ | ✅ | ✅ |
| 无关日志行未被误伤 | ✅ | ✅ | ✅ |
| 运行时异常 | 0 | 0 | 0 |

**12 项检查在三个版本上逐项一致。** 2.13.0 / 2.14.1 仍按设计被 MCDR 依赖检查拦下
（提示「不满足版本约束 >=2.15.0」）。

另经逐版本 API 检查确认：新增功能用到的 `save_config_simple`、带 `file_name` 的
`load_config_simple`、嵌套 `Serializable`、`on_server_startup` 自 **2.13.0** 起均可用，
因此**最低版本要求没有变化**。

命令树的注册也做过跨版本核对（2.15.0 / 2.15.7 / 2.16.0 均能构造出
`!!logfilter` 及 `list` / `reload` / `reset` / `test` 四个子命令，零报错）。
