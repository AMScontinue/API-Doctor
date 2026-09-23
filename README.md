# API Doctor

API Doctor 是一个面向 OpenAPI 3.x 服务的自主接口测试与故障分析 Agent。它结合确定性 Schema 测试与 DeepSeek 语义分析，通过受控 HTTP 工具执行测试，根据真实响应追加诊断实验，并输出证据、复现步骤和 pytest 回归用例。

当前已完成 M0 TestSpec 技术验证的确定性链路：结构化候选校验、有限 IR 编译、非法引用拒绝和真实 HTTP 执行。DeepSeek 实时抽取入口已经实现，但需要在本项目配置 API Key 后才能记录真实模型指标。

## 核心设计

- 规则引擎生成 required、类型、枚举和边界等机械测试；
- LLM 从接口描述和业务规则中提取语义 TestSpec；
- 编译器将 TestSpec 校验并转换为可执行 TestCase；
- Agent 根据测试结果继续计划、追加诊断测试或结束；
- 确定性代码负责安全、HTTP、Schema 校验、预算和持久化；
- 生成 Markdown/JSON 报告、pytest、JUnit XML 和完整轨迹。

完整需求、架构、示例和验收标准见 [EXECUTION_DESIGN.md](./EXECUTION_DESIGN.md)。

## 计划中的本地运行方式

项目默认支持直接在 macOS/Linux 本地运行，不要求 Docker。以下命令将在实现阶段提供：

```bash
# 终端 1：启动 Go 演示服务
cd examples/buggy-shop
go run .

# 终端 2：运行 Agent
python -m api_doctor run \
  --spec examples/buggy-shop/openapi.yaml \
  --base-url http://localhost:8080
```

Docker Compose 仅作为后续可选的一键复现方式。

## M0 技术验证

安装开发依赖：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
```

运行离线编译测试：

```bash
.venv/bin/pytest
```

运行真实 HTTP 链路：

```bash
# 终端 1
cd examples/buggy-shop
go run .

# 终端 2
.venv/bin/api-doctor-m0 \
  --mode fixture \
  --base-url http://127.0.0.1:18080
```

在 shell 中设置 `DEEPSEEK_API_KEY` 后，可把 `--mode fixture` 改为 `--mode deepseek`。实时模式的模型结果必须单独记录，不能与 fixture 指标混用。
