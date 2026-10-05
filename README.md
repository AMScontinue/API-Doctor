# API Doctor

API Doctor 是一个面向 OpenAPI 3.x 服务的自主接口测试与故障分析 Agent。它结合确定性 Schema 测试与 DeepSeek 语义分析，通过受控 HTTP 工具执行测试，根据真实响应追加诊断实验，并输出证据、复现步骤和 pytest 回归用例。

当前已完成 M0 TestSpec 技术验证，并开始实现 M1 确定性基线。M1-A 已提供真实 OpenAPI 3.x YAML/JSON 加载、本地 `$ref` 解析、规范化 Operation Catalog 和检查命令。DeepSeek 的 M0 小样本结果只用于技术验证，不代表产品准确率。

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

## 检查 OpenAPI 解析结果

重新执行 editable install 以安装最新 CLI，然后解析演示规范：

```bash
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/api-doctor inspect \
  --spec examples/buggy-shop/openapi.yaml \
  --output runs/inspect/buggy-shop.json
```

省略 `--output` 时，规范化结果直接输出到终端。当前支持 OpenAPI 3.x、YAML/JSON 和文档内 `$ref`；外部 `$ref` 会明确报错，递归引用会保留引用并产生带位置的 warning。

从请求体 Schema 生成确定性测试候选：

```bash
.venv/bin/api-doctor candidates \
  --spec examples/buggy-shop/openapi.yaml \
  --output runs/inspect/buggy-shop-candidates.json
```

当前生成正常路径、缺失必填字段、数值边界、非法枚举、格式、字符串长度、数组约束和参数边界候选。负向场景没有 OpenAPI 声明的可依据验证状态码时，候选会保留但标记为 `not_compilable`，不猜测 400/422。对声明 404 的 GET 资源接口会生成“不存在资源”探针，但因无法仅凭契约证明任意值一定不存在，其结果明确标记为 `heuristic`。

将可编译候选物化为确定性 HTTP 请求：

```bash
.venv/bin/api-doctor compile \
  --spec examples/buggy-shop/openapi.yaml \
  --seed 20260924 \
  --time-anchor 2026-10-02T10:00:00+00:00 \
  --output runs/inspect/buggy-shop-cases.json
```

编译器直接使用 `OperationCatalog`，生成 path/query/header/cookie/body 分离的 `CompiledTestCase`。合法基线会先通过 JSON Schema 反向校验；OpenAPI 负向候选必须在 mutation 后真正违反请求 Schema，否则拒绝编译。

启动 Go 服务后，运行完整确定性 baseline：

```bash
.venv/bin/api-doctor run \
  --spec examples/buggy-shop/openapi.yaml \
  --base-url http://127.0.0.1:18080 \
  --max-tests 30 \
  --output runs/m1/baseline
```

运行会生成 `run.json`、`report.md` 和 `trajectory.jsonl`。OpenAPI Links 可以把创建接口的响应值传给后续查询或删除接口，并在 run artifact 中保留变量来源。请求受 base URL 同源、重定向、超时、响应大小、方法和 DELETE 显式批准策略约束；响应会校验状态码、Content-Type 和对应 OpenAPI response Schema。

使用 DeepSeek 从接口语义中发现结构化跨接口工作流（只提议，不执行 HTTP）：

```bash
.venv/bin/api-doctor discover-workflows \
  --spec examples/buggy-shop/openapi.yaml \
  --output runs/m2/workflow-discovery.json
```

该命令从 `.env` 读取 `DEEPSEEK_API_KEY`。发给模型的 Catalog 上下文不包含 OpenAPI Links，因此模型需要根据 operation 描述、请求参数和响应 Schema 提出生产者—消费者关系。输出只是经 Pydantic 结构校验的 `WorkflowCandidate`；语义验证、编译和执行属于后续 M2 阶段。

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

运行与 Prompt 示例隔离的 M0 held-out 评测：

```bash
.venv/bin/api-doctor-m0 \
  --mode deepseek \
  --benchmark-dir benchmarks/m0/heldout \
  --log-level WARNING \
  --output runs/m0/heldout-result.json

jq '{metrics, ratios}' runs/m0/heldout-result.json
```

held-out 的 `expected_outcomes.json` 只供本地评测器评分，不会发送给模型。评分不仅比较 `compilable/not_compilable`，还核对关键 mutation，防止把“可以编译但测试语义错误”的候选算作成功。

### 查看 LLM 输入输出

M0 的 `INFO` 默认只显示精简后的 DeepSeek 输入/输出和最终聚合结果。普通解析、fixture、编译、HTTP 与单规则过程只在 `DEBUG` 显示。模型输出未通过 Pydantic 时，日志会显示失败输出以及一次 `structure_repair` 输入输出：

```bash
.venv/bin/api-doctor-m0 \
  --mode deepseek \
  --base-url http://127.0.0.1:18080 \
  --log-level INFO \
  --output runs/m0/deepseek-result.json
```

只有排查解析、编译或 HTTP 内部过程时才使用 `--log-level DEBUG`。

核心标签是 `[LLM INPUT]`、`[LLM OUTPUT]` 和 `[RESULT]`，其中 `attempt=initial|structure_repair` 区分首次抽取和结构修复。`not_compilable` 自检当前已关闭。

日志不会输出 DeepSeek API Key，常见 password、secret、token、Authorization 和 Cookie 字段会被替换为 `***REDACTED***`。

`benchmarks/m0/compiler_fixtures.json` 只用于开发测试“候选已经得到以后”的确定性链路，并故意包含非法候选来验证拒绝行为。真实用户不提供也不读取这个文件；真实模式的用户输入是 OpenAPI 文档和可选的 `rules.yaml`，语义候选由 DeepSeek 生成。
