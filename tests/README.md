# 测试 / Tests

本目录是 Server Log Filter 的测试套件。**54 个用例**，覆盖过滤行为、配置、
命令面，以及本插件最核心的安全属性（被隐去的行仍保留 `process`，事件照常分发）。

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
54 passed
```

## 覆盖内容

| 分组 | 用例数 | 说明 |
|---|---|---|
| 过滤行为与安全属性 | 26 | 目标刷屏行命中（3）；15 类关键行逐一验证**不**被误伤；`content` 为 `""`/`None`/纯空白不崩溃（5）；`hidden()` 保留 `process` 且绝不等于 `discarded()` |
| 规则编译与容错 | 4 | 非法正则被跳过并告警，其余规则照常工作；空/空白规则静默丢弃 |
| 计数、重载与重置 | 7 | 多规则独立计数；`reload` 后归零；首条命中规则胜出且只计一次 |
| 配置对象 | 3 | 默认值与 README 文档一致；JSON 反序列化；往返稳定 |
| MCDR 契约 | 2 | `InfoActionFlag.hidden()` 的常量构成；`InfoFilter` 允许改写 `action_flag` |
| 命令面与元数据 | 12 | `on_load` 注册项；状态/测试/重载命令输出；`reload` 的 ADMIN 权限门禁；插件元数据与 `MIN_MCDR_VERSION` 同步 |
| **合计** | **54** | |

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
