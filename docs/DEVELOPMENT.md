# 开发与打包

这一份面向**要改代码、打包、或核对兼容性**的人。
只想把插件用起来的读者请回 [README.md](../README.md)。

- [仓库结构](#仓库结构)
- [自行打包](#自行打包)
- [测试](#测试)
- [已验证内容](#已验证内容)
- [实现注记（为什么这么写）](#实现注记为什么这么写)
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
│   ├── __init__.py            插件主体
│   ├── i18n.py                语言解析 / 目录读取 / 安全翻译
│   └── lang/                  语言文件（一语言一文件，见 lang/README.md）
│       ├── en_us.json         回落语言，必须存在
│       ├── zh_cn.json
│       └── README.md          面向译者的指引（不打包）
├── CHANGELOG.md               变更记录，只留最新一版（随发布包分发）
├── LICENSE
├── README.md / README_en.md   主文档，面向使用者（不打包）
├── docs/DEVELOPMENT.md        本文件（不打包）
├── pack.py                    打包器
├── tests/                     测试套件（含 tests/README.md 细则）
├── tools/                     变异检查、跨版本矩阵
└── benchmarks/                性能基准
```

只有 `pack.py` 的白名单里列出的文件会进发布包，其余一律不外发。
白名单的判定是**结构化**的，不是一串文件名：包内**任意层级**的 `*.py`
（保证以后加子模块不会被漏掉，有 `test_packager_ships_submodules_recursively` 钉住），
加上 `lang/` 下**正好三层**的 `*.json`（`PACKAGE_DATA_DIRS` 里声明的数据目录与扩展名）。
想加一个数据目录，是改这张表、不是改文件清单。

### 为什么把语言文件打进包，而不是运行时下载

问过、量过、**决定不下载**。记录在这里，免得以后有人再"顺手优化"一遍。

包内逐条量出来的压缩体积（`ZIP_DEFLATED`，同上表的构建方式）：

| 变体 | 体积变化 |
|---|---|
| **现状**：两份语言都打包（打包器产出 **25,646 B**） | — |
| 去掉 `zh_cn.json` | −3,260 B / −12.7% |
| 两份语言都不打包 | −6,154 B / −24.0% |

三个结论：

1. **`en_us.json` 绝不能去掉。** 它是回落语言：一旦去掉，任何一次下载失败都会让
   **每一条消息退化成裸键名**——正是上面那个「静默失败」的故障模式。所以现实中能省的
   只有 `zh_cn.json`，也就是 **13%**。
2. **11% 换不来一个网络故障点。** 本插件的卖点是「零意外」：一个只在 MCDR 侧过滤日志、
   其余完全离线的小插件，如果为了 3.2 KB 变成「启动时可能要联网」，那笔账不划算——
   断网或被墙的中文用户会直接看到英文提示，而这恰恰是最需要中文的那批人。
3. **首屏还会变复杂。** 同步下载会阻塞 `on_load`（MCDR 主线程，也就是拖慢开服）；
   改成后台线程下载则会出现「前几条消息英文、之后突然变中文」的错位。
   两种都不如「文件就在包里」干净。

于是语言文件的增删仍然走 **PR + 发版**：一位译者提交 `ja_jp.json` → 合并进 `main`
→ 下个版本随包分发。想要「合并即可用」的代价，是要接受上面那三个问题，目前判断不值得。

### 关于语言文件为什么必须走 loader

MCDR 用 `zipimport` 直接从 `.mcdr` 里 import 插件，所以打包后 `__file__` 形如
`<临时目录>/xxx.mcdr/server_log_filter/__init__.py`——**它指向压缩包内部，不是一个真实路径**。
此时 `open(os.path.join(os.path.dirname(__file__), "lang", "zh_cn.json"))` 会抛
`NotADirectoryError`，若被 `try/except` 吞掉，**整份消息会静默退化成裸键名**
（控制台出现 `summary.rules_enabled` 这种字样），开发机上完全看不出来。

因此 `i18n.py` 的主路径是 loader：

```python
pkgutil.get_data(__package__, "lang/zh_cn.json")     # 首选，zip 与目录通吃
importlib.resources.files(__package__).joinpath("lang").iterdir()   # 列目录（发现新语言）
open(os.path.join(os.path.dirname(__file__), ...))   # 仅作为解包运行的兜底
```

这条约束被两处钉死：
`tests/test_plugin.py::test_the_packed_plugin_can_still_read_its_catalogues`
在子进程里 import **真实构建出来的 `.mcdr`**，断言读到的是真句子而不是键名；
`tools/mutation_check.py` 的 `skip_the_catalogues` 变异把 `pkgutil.get_data` 换成
`data = None`，确认测试会因此变红。

## 自行打包

```bash
python pack.py            # 生成 ServerLogFilter-v<版本号>.mcdr
```

发布包里只有 **7 个文件**：

| 文件 | 说明 |
|---|---|
| `mcdreforged.plugin.json` | 插件元数据（必需） |
| `server_log_filter/__init__.py` | 插件代码 |
| `server_log_filter/i18n.py` | 语言支持 |
| `server_log_filter/lang/en_us.json` | 英文文案（回落语言） |
| `server_log_filter/lang/zh_cn.json` | 中文文案 |
| `CHANGELOG.md` | 变更记录，**只含最新一个版本**，随包分发 |
| `LICENSE` | MIT 许可证 |

三个 README（`README.md` / `README_en.md` / `lang/README.md`）**都不打包**：
MCDR 从不读取它们，内容与 Releases 页面重复，而去掉后产物体积几乎减半
（实测两个根 README 就占了压缩体积的 **49.6%**）。
`lang/README.md` 是写给**做翻译的人**看的，属于仓库协作材料，对运行毫无用处。

> **包体积变化**：1.2.1 是 18,302 B（4 个文件），1.2.2 是 25,646 B，1.3.0 是 **约 26.5 KB**（都是 7 个文件）。
> 增加的 3 个文件里，`i18n.py` + 两份 `lang/*.json` 是**真实新增的内容**——
> 同一份文本仍只存一遍，中文文案与 1.2.1 的内嵌字符串逐字一致，只是从 `.py` 里搬到了
> JSON 里。多出的约 7 KB 是「一份插件现在带两门语言」的实际代价，不是打包出错。
>
> ⚠️ **这个字节数只在 LF 工作区构建时成立。** 编辑工具在 Windows 上容易写出 CRLF，
> 而打包器读的是**磁盘原始字节**，产物会因此偏大（实测同一个提交：CRLF 版 25,730 B、
> LF 版 25,646 B；`git status` 两边都是干净的，因为 `core.autocrlf` 把差异掩盖了）。
> 更隐蔽的是 `git checkout` 会把工作区重写成 LF，所以「先打包、后切过分支」也会错开。
> **打包后必须逐成员与 `git show <ref>:<path>` 比对 sha256，全部一致才算产物可信。**
> 也正因如此，包内的 `CHANGELOG.md` 只写「约 25.6 KB」，精确值只放在 Release 页面。
>
> 另外，代码里的**注释也做过一轮精简**：说明性的长段落搬到本文件（见「实现注记」），
> 代码里只留「改这里会踩什么坑」的一句话。`server_log_filter/__init__.py` 从 1110 行
> 降到 990 行（注释+docstring 占比 25% → 16%），`i18n.py` 从 252 行降到 238 行，
> 两者合计让压缩后的包小了约 **4 KB**——比砍掉一整个语言文件（3.3 KB）还多。
> 这纯粹是注释的收益，一行可执行代码都没动。

> **`CHANGELOG.md` 只保留最新版本一节**，旧条目在发新版本时删掉（全文留在 Releases 页面上）。
> 它与包一起分发，任何一个历史条目都会**永久**占着用户的体积——实测 1.2.0 → 1.2.1
> 涨的 10.3% 全部来自它自己变长。`test_changelog_keeps_only_the_latest_release`
> 钉住这一点：必须恰好只有一节，且版本号等于 `mcdreforged.plugin.json` 里的当前版本。

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

> ### 什么时候跑什么（约定）
>
> **开发过程中只跑与本次改动相关的那几条**，确认当前版本能过就行：
>
> ```bash
> PYTHONPATH=.testlibs python -m pytest tests/test_plugin.py -k language -q
> PYTHONPATH=.testlibs python -m pytest tests/test_e2e.py -k auto_follows -q
> ```
>
> **提交 PR 前才统一跑一次全量**：完整测试套件 + `tools/mutation_check.py` + 跨版本矩阵。
> 出问题就在那一轮集中修，修完再提 PR。这样既避免反复烧时间，又保证合并前是完整的证据。
> 汇报时说清「哪些跑过、哪些没跑」，不要用「全绿」掩盖没跑的部分。

当前 **221 个用例**，分三层：

| 层 | 位置 | 说明 |
|---|---|---|
| 单元 | `tests/test_plugin.py` | 过滤逻辑、配置对象、命令面、打包、迁移、坏配置保全、语言（188 条） |
| 端到端 | `tests/test_e2e.py` | 启动**真实 MCDR**，加载 `pack.py` 产出的 `.mcdr`，用假服务端跑完整生命周期（33 条） |
| 跨版本 | `tools/mcdr_matrix.py` | 同一个包在多个 MCDR 版本上逐项核对 |

另有两个工具：

```bash
python tools/mutation_check.py    # 故意改坏实现，确认测试会变红（33 个变异，33/33 应被抓住）
python benchmarks/bench_filter.py # 性能基准，README / CHANGELOG 里引用的数字都由它产出
python benchmarks/bench_versions.py 旧.mcdr 新.mcdr   # 两个版本逐项对比，见「版本间性能对比」
```

> **变异检查的判定看 pytest 的退出码：只有 `exit == 1` 才算「被抓住」。**
> `4`（用法错误）/ `5`（没收集到用例）都说明是脚本自己写错了，不是测试变红——
> 早期把 `4` 也当成命中，得到过一个假的满分。
>
> `MUTATIONS` 的条目是 `(名字, 目标文件, 变换函数, baseline 选择器)`，因此变异可以打在
> `i18n.py` 或两份语言 JSON 上，不只是 `__init__.py`；每个文件各缓存一份 baseline。
> 语言相关的 8 条包括：忽略 `language` 配置项、忽略 MCDR 语言、`pkgutil.get_data`
> 直接返回 `None`（模拟打包后读不到文案）、不报未知语言、不报坏目录、
> 坏配置时不从原文读语言、**语言文件少一个键**、**模板少一个占位符**。
> 后两条保证「文案本身」也在被测范围内，而不只是代码路径。

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
  **规则从配置删除后其统计立刻清除**（重载时即清，不必等服务端停止；
  仍写在配置里但被跳过的规则不受影响；配置刚被自动重置时不动状态文件）、
  可从配置关闭、`reset` 能清空、提醒内容不重复
- **正则安全检查**：4 类灾难性回溯模式被拦下，8 种真实写法全部放行
- **升级提示与迁移**：旧版本格式的配置在真实 MCDR 上自动补齐，并在日志里报告新增了哪些选项；
  每个配置项都必须写明加入版本与说明（有测试保证，漏写就会变红）
- **坏配置保全**：解析失败时原文件被备份为 `config.json.old`，新配置以默认值重建，
  并在日志里给出原因与出错行列；空文件、顶层非对象同样处理。**不是合法 UTF-8 的文件也算写坏**
  （中文 Windows 记事本存成「ANSI」就会这样）——它此前会让插件整个加载失败，见「实现注记」
- **三条提示的开关**（`announce_config_upgrade` / `announce_broken_config` /
  `warn_about_stale_rules`，默认全开）：关掉只影响**说不说**，不影响**做不做**——
  配置照补、坏文件照备份、统计照记。写坏配置时开关只能从**原文**里读（配置已解析失败），
  因此大小写、空格、写在校验错误之前或之后都有效；「连备份都失败」属数据丢失警告，
  不受开关影响。以上每条都有单测 + 真实 MCDR 端到端各验一遍

### 单元测试（语言支持）

- **`auto` 双向跟随**：MCDR 是 `zh_cn` → 中文；MCDR 是 `en_us` → 英文
- **显式值压过 MCDR**：配置写 `zh_cn`、MCDR 是英文，仍然说中文
- **宽松识别**：`EN_us` / `zh-CN` / `  en  ` / `zh` / `zh_TW` 都能认出来
- **没出货的语言**：`auto` 时**静默**回落（用户把选择委托给了 MCDR）；
  显式写一个不存在的语言时回落 + **一次**警告，不刷屏
- **目录坏掉不影响运行**：读不出文件 → 回落 + 报告一次；**两份同时坏掉也不崩**
- **打包后仍能读到文案**：子进程 import 真实 `.mcdr`，断言输出的是真句子（非裸键名），
  且语言列表来自 zip 内部
- **`reload` 立即切换语言**，不需要重启服务端
- **MCDR 语言 API 缺席 / 抛异常**都不会把插件带下去（退到 `get_mcdr_config()["language"]`，再退到回落语言）
- **坏配置播报用原文里的语言**（那一刻没有解析好的 Config）
- **英文真的到达了每一处输出**：零命中提醒、命令与运行摘要、删规则播报与规则报错、升级提示
- **不变式**（对**所有**出货语言同时成立，新增语言自动纳入）：
  - 每个语言文件都能解析；键集与回落语言完全一致；占位符集合一致（不能只翻一半）
  - **代码里 `_t("…")` 用到的键全部存在**，且**没有键是没人用的**（双向校验）
  - `CONFIG_DOC` 的键集合 == `Config` 的字段集合（所以「每个选项都有说明」对每种语言都成立）
  - `en_us` 里不含任何 CJK 字符（防止把中文原样复制过去）

> 键集合的提取靠 **AST**：遍历源码里所有 `_t(...)` / `say(...)` / `translate(...)`
> 调用的**第一个位置参数**并取字面量。这样「新增一处输出但忘了加文案」会直接变红，
> 不需要人工维护一份键名单。

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
插件 server_log_filter@<版本> 已加载
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

端到端组共有 **11 次真实 MCDR 启动**（正常一次、零命中提醒一次、坏配置一次、
关掉写坏提示一次、删规则前后各一次、**`auto` 跟随 MCDR 两个方向各一次、显式语言压过 MCDR 一次、
打包产物带语言文件一次、配置不是 UTF-8 一次**），整个 `tests` 目录约 33 秒，可用 `MCDR_SKIP_E2E=1` 跳过。

> **e2e 里的语言是分两处控制的**，因为 MCDR 自己的界面文案会被断言扫到：
> `_build_instance(..., mcdr_language="en_us", plugin_language="zh_cn")` ——
> 实例的 MCDR 用 `en_us`（否则 `Server process stopped with code 0` 这类用于判定
> 生命周期的**英文**标志行会消失，`grep` 证实该 key 只存在于 MCDR 的 `en_us.yml`），
> 而**插件**的语言单独 pin 成 `zh_cn`。要测跟随行为时把 `plugin_language` 传 `None`，
> 生成的 `config.json` 里就不写 `language` 键 → 插件走 `auto`。
>
> **`auto` 的两个方向都要测**，因为「跟随 MCDR 当前设置」和「跟随某个内置默认值」
> 在有配置 + 无配置时表现不同，只测一个方向会把它俩混淆：
>
> | MCDR 实例的 `language` | 生成的插件配置 | 期望输出 |
> |---|---|---|
> | `en_us` | 不写 `language` 键 | 英文（顺带覆盖 1.2.2 升级提示） |
> | `zh_cn` | 不写 `language` 键 | **中文** |
> | `zh_cn` | `"language": "en_us"` | 英文（显式值压过 MCDR） |
>
> 坏配置用例里的 `BROKEN_CONFIG` 会**自带** `"language": "zh_cn"`——
> 那才贴近真实场景（用户本来就是这么写的），也顺带证明原文可读。

## 实现注记（为什么这么写）

代码里的注释只留「改这里会踩什么坑」的一句话；原因、实测数字和历史教训集中在这节。
改动相关代码前先读对应条目。

### 为什么必须是 `hidden()` 而不是 `discarded()`

MCDR 在 `mcdreforged/info_reactor/impl/server_reactor.py` 里靠**日志行**判定启动完成：

```python
if handler.test_server_startup_done(info):
    self.mcdr_server.add_flag(MCDReforgedFlag.SERVER_STARTUP)
    self.mcdr_server.plugin_manager.dispatch_event(MCDRPluginEvents.SERVER_STARTUP, ())
```

`InfoActionFlag.hidden()` = `send_to_server | process`，只摘掉 `echo_to_console`；
而 `InfoFilter` 返回 `False`（或设成 `discarded()`）是把整条丢掉。**后者会让 MCDR
的启动 / 停止 / 玩家进出检测失效**——这不是理论风险，端到端组里有一条 canary 用例
专门守它（见 [tests/README.md](../tests/README.md) 的 canary 一节）。

服务端自己的 `server/logs/latest.log` 由服务端进程用 log4j 写入，与 MCDR 无关，
**一条都不会少**。

### 热路径的预算

`filter_server_info` 在 MCDR **主线程**上逐行调用（不是任务执行器线程），所以：

- 正则在载入时预编译（`Rule.regex`），逐行只做 `regex.search()`；
- 规则存 **tuple**，命中即 `return`，不命中就整体退化为 N 次 `search`；
- `hidden()` 这个不变常量在构造函数里算一次，命中路径上直接复用；
- 计数加锁，但**只在命中那一条路径上**取锁，不影响未命中的行；
- 只有开了 `log_matched_lines`（调试用）才碰语言查找——正常路径上一次 `_t()` 都不调。

实测（`benchmarks/bench_filter.py`，可复现）：

| 规则条数 | 每行开销 |
|---|---|
| 1 | ≈ 0.27 µs |
| 5 | ≈ 0.55 µs |
| 25 | ≈ 1.80 µs |
| 50 | ≈ 3.20 µs |

对照：MCDR 自己解析同一行约 **2.1 µs**。也就是说 1 条规则时本插件约占 MCDR
每行开销的 13%；即便 50 条规则、1000 行/秒的极端突发，也只占一个核心的 **0.3%**。

**所以「规则太多导致卡服」不是这个插件的性能问题**，也没有为它引入任何优化。
曾经试过的两个方向都实测无效，留在这里免得再试一遍：

- **把 N 条规则合并成一个 `(a)|(b)|(c)` 大正则**：1 条时略快，**2 条起反而更慢且超线性**
  （2 条 0.49 µs、5 条 1.16 µs、50 条 20 µs）。原因是 `re.search` 在每个起始位置都要
  逐支尝试整个 alternation，而分开的 N 次 `search` 各自能走字面量前缀的快速路径。
  加上命名组归属在嵌套捕获组下不可靠（`lastgroup` 可能是 `None`）、
  行内 `(?i)` 这类全局标志不允许出现在中段，这条路是净亏。
- **把 `match()` 内联进 `filter_server_info`**：省下的函数调用与测量噪声同量级
  （0.03 µs/行，约 MCDR 每行开销的 1.5%），不值得牺牲可读性。

真正会让主线程冻结的是**灾难性回溯**，见下条。

### 短探测串为什么能判断回溯风险

带嵌套量词的表达式（如 `(a+)+$`）匹配失败时按 2^n 回溯：实测 22 字符 182 ms、
27 字符 3.0 秒——**一行**就能冻住 MCDR。

载入时用 `PROBE_SUBJECTS`（22 字符左右）逐个试跑，超预算即拒绝。可行性来自
「耗时随长度指数增长」这一点：短串上已经明显超预算的模式，在真实长度的日志行上
只会更糟。标定（`benchmarks/bench_filter.py` 第 4 节）：

- 4 类危险模式在探测串上耗时 190~380 ms，全部超过 25 ms 预算 → 被拦下；
- 8 种真实写法最坏仅几微秒 → 余量约 **9000 倍**，不会误伤。

### 「零命中提醒」的价值与两个假提醒防护

它**不降低运行开销**（这一点被数据否定过：删规则省下的时间远低于测量噪声），
价值在配置卫生：尽早发现写错的规则、以及已经没必要再过滤的规则。
两个必须保留的防护，否则会给出**错误的**提醒：

1. **只统计「完成过启动」的周期**（`_session_reached_startup`）。否则服务端连续
   启动失败（mod 报错之类）会把所有规则刷成零命中。
2. **热重载要接续命中计数**，**并且**接续上面那个标记。真实重载会构造一个全新的
   模块，标记会归零；只接续计数不接续标记，整轮周期仍会被丢弃。

清理（`_forget_orphans`）的两条刻意保留的边界，防止「清理」变成「误删」：

- 孤立项按**配置原文**判，不按编译成功的规则。仍写在配置里但被跳过的规则
  （正则写错 / 被回溯探测拦下）**保留历史**——否则改好错别字后历史已经归零。
- 配置因写坏**被自动重置**时（`_config_was_reset`）两个清理点**都跳过**：那一刻的
  `patterns` 是默认值而不是用户配置。守卫必须放在 `_forget_orphans` 里（两个调用点
  共享），只放在 `_prune_state` 会漏掉关服那条路。

### 为什么要在 MCDR 之前自己检查配置文件

`load_config_simple` 默认 `failure_policy='regen'`：解析失败时**直接用默认值覆盖原文件**。
最常见的触发方式就是往 `patterns` 里加规则时漏一个逗号——用户辛苦写的规则会就此消失。

所以插件先自己读一遍原文：能解析成 dict 就交给 MCDR；否则先把文件改名成
`config.json.old`，再让 MCDR 重建。**备份动作永远执行**，`announce_broken_config`
只管说不说。两个细节：

- 那一刻没有解析好的 `Config`，所以开关与语言都只能用 `_raw_bool_option` /
  `_raw_str_option` 从**原文**里做文本查找，因此写在语法错误**之后**也照样有效。
- 「连 `os.replace` 都失败」意味着原文件真的会被覆盖掉，属数据丢失，**不受任何开关
  影响**，一律报出去。

#### 三种「写坏」，第三种曾经会让插件整个加载不了

「写坏」有三种：**JSON 语法错误**、**顶层不是对象**、以及**文件不是合法 UTF-8**。
第三种是 1.2.3 才补上的，而且它原来的表现远比前两种严重。

`config.json` 里可以写中文规则，所以编码错误是真实存在的：在中文 Windows 上用记事本
存成「ANSI」就是 GBK。MCDR 自己也解析不了它，因此它理应走同一条备份 + 重建的路。
但原来的 `_read_text_file` 只 `except OSError`，而 `open(..., encoding="utf-8").read()`
抛的是 **`UnicodeDecodeError`（`ValueError` 的子类）**——没被接住，一路冒到 `_load_config`
再到 `on_load`，结果是 **MCDR 里根本没有这个插件**。一个辅助函数的签名写着
「读不动就返回 None（不抛异常）」，而它其实会抛——**docstring 承诺与实现不符，是这类
bug 最好的藏身处**。

所以现在按字节读、显式解码：`_read_bytes()` 只管 I/O，`_decode_utf8()` 把「解码不了」
变成一个可以上报的返回值而不是异常。**推论值得记住：只要某个文件是「用户手写、插件去读」的，
读取路径就必须假设它可能不是 UTF-8。**

一个副产物：这种情况下读不出用户写的 `language`（那正是读不出来的东西），
所以播报只能退回 MCDR 自己的语言。这是当时唯一还能确定的语言，端到端用例断言的就是英文。

### 两个「取值写错」的坑

它们都属于「用户把某个值写成了不合法的东西，插件却报出与真实原因无关的错误」。

**`pattern_probe_timeout_ms <= 0` 会把所有规则拒掉。** 「预算 0 毫秒」意味着任何耗时都算
超支，于是每条规则都被判成「有灾难性回溯风险」——连纯字面量也不例外。过滤静默失效，
而日志把人往「规则写错了」的方向带。现在下限夹到 **1 ms**：真实规则在微秒级、
危险模式在百毫秒级，1 ms 两边都分得清（见「短探测串为什么能判断回溯风险」的标定数字）。

**`!!logfilter test` 会把锚定规则误报成「不命中」。** 过滤器匹配的是 MCDR **剥掉
`[时间] [线程/级别]:` 前缀之后**的正文（`info.content`），而命令拿用户粘贴的原文去匹配。
管理员习惯整行复制，于是 `^Player \w+ joined` 这种写法会被告知「不命中」——
而它其实工作得好好的，人反而会去改一条本来正确的规则。现在测试前先剥掉前缀
（`_LOG_LINE_PREFIX`），并在回复里说明它这么做了。前缀正则只匹配最常见的
`[..] [../LEVEL]:` 形态，认不出就不剥，行为与从前一致，不会有回归。

#### 「捕获的类型太窄」是一个反复出现的 bug 家族

上面那条编码问题属于这一类，1.3.0 复查时又找到两处**同族**的：

| 位置 | 只接了 | 实际会抛 | 后果 |
|---|---|---|---|
| `_read_text_file` | `OSError` | `UnicodeDecodeError`（`ValueError`） | 插件整个加载不了 |
| `re.compile(pattern)` | `re.error` | `OverflowError`（`a{999999999999}`）、`RecursionError`（上千层嵌套） | 插件整个加载不了 |
| 模板 `format(**kwargs)` | `KeyError`/`IndexError`/`ValueError` | `AttributeError`（译文写成 `{a.b}`） | 消息路径抛异常 |

共同点：**代码在同一条路径上明确承诺了「绝不抛异常 / 单条写错不影响其他」，而 except 只覆盖了
最常见的失败类型。** 写这类防御性代码时，问题不是「该接哪种异常」，而是
「这一步在什么条件下**不能**抛异常」——那就该按后者来写列表。三处现在分别是
`(re.error, OverflowError, RecursionError)`、`(KeyError, IndexError, ValueError,
AttributeError, TypeError)`、以及「按字节读 + 显式解码」。三条都有对应的变异锚点：
把 except 改窄，测试必须变红。

（顺带一个反例：`re.search` 在**匹配**阶段不会再抛这类异常——实测 400 层嵌套编译通过后
`search` 正常。所以只堵 `compile` 这一处就够了，不必给热路径加 try。）

### 语言必须在配置加载路径上最早定下来

坏配置的播报、缺选项的播报都要用它。配置写坏时读到的是默认值，所以那时改从原文里
取用户写的语言。`_quarantine_broken_config` 内部用 `warn=False` 解析一次，随后的
`_load_config` 用同一份原文再解析一次——**警告只在后者发一次**，不说两遍。

### 开关只控制「说不说」

`announce_config_upgrade` / `announce_broken_config` / `warn_about_stale_rules`
都**不控制「做不做」**：配置照补、坏文件照备份、统计照记。改动这类开关必须写测试
钉住这一条，否则实现很容易退化成「关掉提示 = 连安全动作一起跳过」。

### 语言：为什么不用 MCDR 自带的插件翻译机制

**MCDR 确实自带一套插件翻译**（`PackedPlugin(MultiFilePlugin)._on_ready` 会调
`__register_default_translation()`，自动把 `<插件>/lang/*.json|yml` 注册为插件翻译；
`server.tr(key, *args, _mcdr_tr_language=..., **kwargs)` 负责查表）。我们的目录名
（`plugin_constant.PLUGIN_TRANSLATION_FILES_PATH = 'lang'`）和扁平 JSON 格式**本来就和它一致**，
所以这不是巧合，而是可以随时切换的。**调研过、量过、决定不切**——记录在这里，
免得以后有人再从头查一遍。

先说**不会损失**的（逐条已核对 MCDR 源码）：回落链完全一致
（`LanguageFallbackHandler.auto()` 就是 `en_us` + `{'zh_tw': ['zh_cn'], 'zh_cn': ['zh_tw']}`，
我们的 `FALLBACKS` 本来就是照抄它）；键缺失同样返回键名本身；`lang/` 的读取用
`open_file()`，**在打包的 zip 里也读得到**（我们手写的 pkgutil / importlib 那套它已具备）；
显式语言能通过 `_mcdr_tr_language` 生效，且不传 `_mcdr_tr_fallback_language` 时默认仍是 `auto()`；
**键名不会撞车**（MCDR 自带翻译全部挂在唯一顶层键 `mcdreforged.*` 下，
我们的 `summary` / `status` / `broken` … 与它不相交）；时机也没问题（语言注册在
`plugin.ready()`，**早于** `on_load` 事件派发）；`tr()` / `rtr()` / `lang/` 自动注册都远早于
我们 2.15.0 的底线。

再说**会损失**的：

| 损失 | 说明 |
|---|---|
| **坏占位符会让插件崩** | MCDR 的 `TranslationManager.translate` 在 `formatter.format()` 失败时 `raise ValueError('Failed to apply args ...')`；本插件刻意是「返回原文、不抛」。同一个错误发生在 `on_load` 里就是**插件加载失败** |
| 键名缺失每次打一条 ERROR | MCDR 先 `logger.error('Error translate text ...')` 再返回键名；本插件是静默返回 |
| 宽松写入要自己保留 | MCDR 按**精确字符串**查表 + 固定回落表：不认 `zh-CN` / `EN_us`，也没有同基匹配（`zh` → `zh_cn`） |
| 「语言不支持」的警告要自己保留 | MCDR 没有「插件自己的 `language` 选项非法」这个概念 |
| 读不出的语言文件换了归属 | 变成 MCDR 自己打的异常堆栈（信息更多，但不受我们的开关控制） |
| `_t()` 不再是纯函数 | 需从 `_server` 取，测试里「替换 catalogue 读取函数」的手法失效 → 约 30 条语言测试 + 8 条变异要改写 |
| **省下的比想象中少** | 目录发现、`resolve`、宽松归一化、以及上面两条补偿都得留着：实际只能删约 **100 行**（`i18n.py` 238 → 约 140），压缩后包体只小 **约 1.1–1.3 KB** |

结论：**保持自研**。所有代价都落在「我们自己刻意加的那几条健壮性」上，尤其第一条——
官方机制外面永远得套一层 `try/except`，套了之后既不是「纯官方」也不是「纯自研」，
反而多一层壳。收益只有约 1.2 KB + 100 行，还要重写 30 条测试，不划算。

> 真要切换时的关键差异只有三处：给 `_t()` 换成 `server.tr(key, _mcdr_tr_language=_language)`
> 并包一层兜底；保留 `normalize()` 与同基匹配；保留「取值不支持」的警告。
> 目录名和文件格式都不用动。

### 一个测试必须覆盖「接线」而不只是函数

第 9 组（灾难性回溯防护）踩过：只在 `_build_rules` 层面测「危险模式被拦下」，
然后把 `on_load` 里的 `validate=_config.validate_patterns` 改成 `validate=False`，
测试**依然全绿**。函数正确 ≠ 它真的被挂上了。同类接线用例还有
`test_on_load_wires_the_migrator_into_config_loading`、`test_reload_command_also_migrates`。

### 版本间性能对比

`benchmarks/bench_versions.py` 回答「这次改动有没有变慢」。它把两个 `.mcdr`（或两个源码目录）
各自解到临时目录，**分别在独立子进程里 import 并测量**——两个版本永远不会共用同一个模块对象。

```bash
python benchmarks/bench_versions.py ServerLogFilter-v1.2.2.mcdr ServerLogFilter-v1.3.0.mcdr
```

**报告的是「多次分批里的最快一批」，不是平均值。** 这一点是实测调出来的：外部干扰
（别的进程、GC、调频）只会让某一批变慢、不会变快，所以最快的那批最接近代码的真实成本。
最初用平均值时，单条规则那一档在同一个版本上重复三次就能抖动 **约 20%**，足以把噪声当成回归；
改成取最小批次后，5 / 25 / 50 条规则三档的复跑差异都收敛到 **±1.5%** 以内。

1.2.2 → 1.3.0 的实测（同一台机器，三次复跑）：

| 规则数 | v1.2.2 | v1.3.0 | 差异 |
|---|---|---|---|
| 1 | 0.170 µs | 0.172 µs | +1.3% |
| 5 | 0.423 µs | 0.428 µs | +1.3% |
| 25 | 1.635 µs | 1.633 µs | −0.1% |
| 50 | 3.310 µs | 3.287 µs | −0.7% |

载入开销（编译 + 回溯探测，25 条规则）约 0.022 ms，两版相同；
模块冷导入约 0.54 s（其中绝大部分是 import `mcdreforged` 本身），两版相同。

**结论：1.3.0 在过滤热路径上没有可测量的变化。** 差异全部落在复跑噪声内，
而「最慢的一批」在绝对值上也只有微秒量级——对照 MCDR 自己解析一行要 ~2.1 µs，
这个插件在 1 条规则时约占其 8%。1.3.0 修的四处都是**错误路径**（写坏的配置、写错的取值），
那些路径本来就不在逐行循环里。

产物体积从 25,739 B 增到 27,526 B（+1.8 KB），来自新增的三条测试所需的代码、
新增的 6 条语言消息，以及 `!!logfilter` 多输出的三行。**这是功能与健壮性的代价，不是性能开销。**

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
| **2.15.0**（最低支持） | 加载、过滤、零命中提醒、状态文件、命令（含 `!!logfilter reset`）、语言跟随全部实测通过 |
| 2.15.7 | 同上，全部通过 |
| 2.16.0 | 同上，全部通过 |

全部功能在这三个版本上行为**完全一致**（实测：提醒出现时机、连续零命中计数推进、
状态文件写入、命令树注册、**`auto` 跟随 MCDR 语言**均相同，零异常）。
可用 `tools/mcdr_matrix.py` 自行复跑。

矩阵里与语言有关的判定是 **`spoke_chinese`**：生成的 `config.json` 首位写 `"language": "auto"`，
而矩阵实例的 MCDR 固定为 `language: zh_cn`，于是「输出里没有出现英文句子」同时证明了两件事——
插件确实读到了 MCDR 的语言，且 `auto` 是**默认行为**（不写这个键也一样）。

Minecraft 侧：过滤发生在 MCDR 侧（对服务端 stdout 逐行匹配），
因此**与 MC 版本无耦合**。1.16.5 / 1.19.4 / 1.20.1 / 1.20.6 / 1.21.8 / 1.21.11 /
26.1 / 26.2 / 26.3 均已用真实服务端实测通过。

唯一与 MC 版本相关的是**默认规则的目标日志**：`standing on air - force-sending blocks below`
仅由 **MC 26.3** 产生；装到更早版本上插件照常工作，只是默认规则不会命中任何内容。

## 发布流程

0. **提 PR 前统一跑一次完整清单**（开发过程中只跑相关子集）：
   ```bash
   PYTHONPATH=.testlibs python -m pytest tests -q      # 全量 221
   python tools/mutation_check.py                      # 33/33
   python tools/mcdr_matrix.py <mcdr2150> <mcdr215> <mcdr216>   # 跨版本矩阵
   ```
   出现问题就在这一轮集中修，修完再提 PR。**矩阵不通过就不发版。**
1. 改动合入 `main`（对外动作前先征得维护者同意）
2. 更新 `CHANGELOG.md` 与 `mcdreforged.plugin.json` 里的版本号；
   同时**把 `CHANGELOG.md` 裁成只剩本次版本一节**（旧条目删除，Releases 里已有全文）
3. 判定是否属于**实质更新**：只有用户拿到的东西变了（插件行为 / 配置项 / 命令 / 兼容性）
   才发新版本；纯测试、纯文档、纯打包脚本改动不发版本
4. `python pack.py` 构建产物，并记下**该文件**的 SHA256
5. 创建 Release 并上传资产；正文里写明改动与 SHA256

> 打 tag 可以交给 Release API 一并完成：创建 Release 时同时给 `tag_name` 与
> `target_commitish=<sha>` 即可，不必单独 push tag。
