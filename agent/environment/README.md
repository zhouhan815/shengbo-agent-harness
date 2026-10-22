# SDK 安装与离线验证

准备日期：2026-10-15。项目：`D:\Codex project\shengbo-ops-agent`。

这是 SDK 准备工作。`tests/offline_sdk_probe.py` 仅是技术兼容性探针，未实现任何电商业务流程。

## 已安装版本

| 项目 | 版本 |
| --- | --- |
| Python | 3.12.14 / Windows AMD64 |
| deepagents | 0.7.25 |
| langchain | 1.4.6 |
| langchain-core | 1.6.10 |
| langgraph | 1.2.15 |
| langchain-deepseek | 1.1.1 |
| langchain-mcp-adapters | 0.3.2 |
| mcp | 1.30.0 |

5 项直接依赖见 `requirements.in`，全部 78 项运行依赖的精确版本见 `requirements.lock.txt`。安装环境共 79 个包，另 1 个是虚拟环境自带的 pip 25.0.1。Anthropic、Google、OpenAI 等客户端是指定包声明的必要传递依赖；未配置或调用这些服务。

基础 Python 为 Codex bundled runtime：`C:\Users\lizhe\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe`。环境通过 `python -m venv` 建立，所有包只安装到项目 `.venv`，未改全局 Python 包。

## 直接复用

无需激活环境，显式使用解释器即可：

```powershell
& 'D:\Codex project\shengbo-ops-agent\.venv\Scripts\python.exe' -I 'D:\Codex project\shengbo-ops-agent\environment\tests\offline_sdk_probe.py'
```

要同时重跑依赖一致性和探针，并刷新汇总报告：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File 'D:\Codex project\shengbo-ops-agent\environment\tools\run_offline_checks.ps1'
```

`-ExecutionPolicy Bypass` 仅作用于该次子进程。虚拟环境依赖上述基础 Python 路径；若基础 runtime 被迁移或清理，应重建虚拟环境。

## 重建环境

在新环境或需要重建时执行；下载包需要联网：

```powershell
Set-Location 'D:\Codex project\shengbo-ops-agent'
& 'C:\Users\lizhe\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m venv '.venv'
& '.\.venv\Scripts\python.exe' -m pip --isolated install --index-url 'https://pypi.org/simple' -r '.\environment\requirements.lock.txt'
& '.\.venv\Scripts\python.exe' -m pip --isolated check
```

`requirements.lock.txt` 锁定安装版本；`reports/pip-install-report.json` 还记录了最初安装来源、发行文件哈希和依赖元数据。未单独复制 wheel 仓库，当前 `.venv` 可以直接离线运行。

## 验证机制与结果

| 环节 | 验收标准 | 结果 |
| --- | --- | --- |
| 独立环境与安装 | Python >=3.11，5 项直接依赖已安装，路径在本项目 `.venv` | 通过 |
| 依赖一致性 | `pip check` 无冲突 | 通过：No broken requirements found. |
| 离线兼容性 | 探针 7/7 通过，真实模型调用 0 次 | 通过 |

探针检查独立虚拟环境、5 类 SDK 导入、`create_deep_agent` 实际签名、图编译、本地工具执行、图的模型/工具回环、网络连接及 DNS 尝试为零。加上 `pip check`，共有 8 项验证通过。

确定性模型替身先产生 `sdk_probe_add(17, 25)` 工具调用，工具在当前进程中计算 `42`，第二轮模型替身核对 ToolMessage 并返回 `SDK_OFFLINE_OK:42`。实际消息顺序是 `human -> ai -> tool -> ai`，本地工具 1 次，模型替身 2 次。

探针在 SDK 导入前清空继承环境，关闭 LangSmith/LangChain tracing，并通过 socket 替换和 Python audit hook 阻断连接及 DNS。网络阻断本身有自检。允许标准库的本地 IPv6 绑定能力检测；该操作不建立网络连接。未构造任何托管模型客户端。

## 官方接口核验

- [Deep Agents quickstart](https://docs.langchain.com/oss/python/deepagents/quickstart)：安装包为 `deepagents`，入口为 `create_deep_agent`，可以显式传入模型实例和工具，再调用编译图的 `invoke`。
- [Deep Agents API reference](https://reference.langchain.com/python/deepagents/graph/create_deep_agent)：在线索引曾显示 0.7.19；本次按官方 PyPI 实时元数据安装 0.7.25，并在本地记录实际接口签名和 `graph.py` SHA-256。
- [Deep Agents PyPI](https://pypi.org/project/deepagents/)：0.7.25 要求 Python >=3.11、LangChain >=1.4.5、langchain-core >=1.6.10；解析版本满足要求。实时官方元数据保存在 `reports/pypi-metadata.json`。
- [ChatDeepSeek 集成](https://docs.langchain.com/oss/python/integrations/chat/deepseek)：集成包为 `langchain-deepseek`，类为 `ChatDeepSeek`。本次仅验证导入，不读取密钥或测试真实推理。
- [LangChain MCP](https://docs.langchain.com/oss/python/langchain/mcp)：LangChain >=1.4 另有 beta 的 `langchain.mcp`，需要 `langchain[mcp]` / FastMCP。本次遵循指定范围安装 `langchain-mcp-adapters`，验证 `MultiServerMCPClient` 导入，未额外扩展到该 beta 路径。

当前官方 quickstart 和 0.7.25 源码中，任务规划 `TodoListMiddleware` 需要显式添加；本地 `deep-agents-core` 技能中“默认开启”的静态示例有版本差异。后续开发应以当前依赖的官方接口和实际源码为准，本次未修改用户技能。

## 证据文件

- `reports/installation-report.json`：结构化汇总，版本、验证数量、边界、源码与锁文件哈希。
- `reports/offline-probe.json`：7 项逐项断言、消息顺序、本地工具调用证据、实际工厂签名。
- `reports/pip-check.txt`：依赖一致性结果。
- `reports/pip-install-report.json`、`reports/pip-install.log`：安装来源、依赖解析和成功安装证据。
- `reports/installed-packages.json`、`reports/pypi-metadata.json`：本地包清单与官方发布元数据。

本次未验证真实 DeepSeek 推理、外部 MCP 服务连接、生产凭证或电商业务可用性；这些不属于此次 SDK 准备验收范围。
