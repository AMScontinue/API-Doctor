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

确定性校验并编译已保存的工作流提议（不调用 LLM，也不发送 HTTP）：

```bash
.venv/bin/api-doctor validate-workflows \
  --spec examples/buggy-shop/openapi.yaml \
  --input runs/m2/workflow-discovery.json \
  --output runs/m2/validated-workflows.json
```

校验器确认 operation、成功响应 JSON Pointer、下游参数位置和变量类型均来自当前 `OperationCatalog`，拒绝重复绑定及重复 workflow id。编译结果保存每个可提取响应状态及目标参数 Schema，供运行时再次校验真实值；包含 DELETE 的工作流只标记为需要批准，不会获得隐式执行权限。

在本地服务上执行一条已经发现的工作流：

```bash
.venv/bin/api-doctor run-workflow \
  --spec examples/buggy-shop/openapi.yaml \
  --input runs/m2/workflow-discovery.json \
  --workflow-id order_create_read \
  --base-url http://127.0.0.1:18080 \
  --output runs/m2/workflow-order-run
```

运行器会重新校验 discovery，生成每一步的合法请求，通过受限 HTTP 工具执行，从成功响应提取变量并分别用响应字段 Schema 和目标参数 Schema 校验，再注入下游请求。失败、提取错误、预算耗尽或未批准的 DELETE 会阻断剩余步骤。输出包含脱敏的 `run.json`、`report.md` 和 `trajectory.jsonl`；只有明确传入 `--approve-delete` 才允许执行 DELETE。

让 DeepSeek 在已经校验的测试与工作流中生成初始 Agent 计划（只规划，不发送 HTTP）：

```bash
.venv/bin/api-doctor plan-agent \
  --spec examples/buggy-shop/openapi.yaml \
  --test-plan runs/m2/final-test-plan.json \
  --workflows runs/m2/validated-workflows.json \
  --max-tests 8 \
  --max-steps 10 \
  --output runs/m2/initial-agent-plan.json
```

模型只能引用输入 inventory 中的 `item_id` 并声明优先级、理由和请求成本。代码重新计算真实成本，检查 ID、请求预算和 Agent step 预算；模型伪造 ID、谎报成本或超预算时使用确定性 fallback。没有 `--approve-delete` 时，包含 DELETE 的工作流不会进入 Planner inventory 或 fallback。输出是显式 `AgentState`，此命令当前尚不执行 pending actions。

执行已经校验的 Agent pending queue（不产生新的 LLM 调用）：

```bash
.venv/bin/api-doctor run-agent-actions \
  --spec examples/buggy-shop/openapi.yaml \
  --state runs/m2/initial-agent-plan.json \
  --test-plan runs/m2/final-test-plan.json \
  --workflows runs/m2/validated-workflows.json \
  --base-url http://127.0.0.1:18080 \
  --output runs/m2/agent-action-run
```

统一执行器从可信 registry 解析每个 action，重新核对真实成本和 OpenAPI 指纹，执行单接口 `CompiledTestCase` 或跨接口 `CompiledWorkflow`，再把 Observation、变量、请求指纹和预算写回 AgentState。失败或错误会把 phase 切到 `analyzing` 并保留剩余队列；安全阻断和重复请求不会消耗 HTTP 预算。每个 action 后原子更新 `checkpoint.json` 并追加脱敏 trajectory。

从 OpenAPI description 和可选业务规则中发现 Schema 机械规则无法生成的语义测试：

```bash
.venv/bin/api-doctor discover-tests \
  --spec examples/buggy-shop/openapi.yaml \
  --rules examples/buggy-shop/rules.yaml \
  --output runs/m2/semantic-tests.json
```

模型可以提出字段关系、条件必填和条件禁止等 `SemanticTestCandidate`，也应将无法用当前 DSL 表达的约束标记为 `not_compilable`。该命令只生成提议，不会直接执行。

确定性校验并编译已保存的模型候选（不调用 LLM，也不发送 HTTP）：

```bash
.venv/bin/api-doctor validate-tests \
  --spec examples/buggy-shop/openapi.yaml \
  --rules examples/buggy-shop/rules.yaml \
  --input runs/m2/semantic-tests.json \
  --output runs/m2/validated-semantic-tests.json
```

校验器逐条核对候选引用的规则或 description、operation、状态码、字段、Schema 合法性和字段关系类型，再调用正式编译器生成 `CompiledTestCase`。非法候选会带原因单独拒绝，不影响同批其他候选。

将机械测试与校验后的语义测试融合成最终计划（不调用 LLM，也不发送 HTTP）：

```bash
.venv/bin/api-doctor fuse-tests \
  --spec examples/buggy-shop/openapi.yaml \
  --rules examples/buggy-shop/rules.yaml \
  --input runs/m2/semantic-tests.json \
  --output runs/m2/final-test-plan.json
```

融合器根据实际 method、path、参数、请求体、依赖和风险计算稳定请求指纹。相同请求及相同期望只保留一次并合并证据；相同请求出现不同期望状态码时不会任选其一，而是移出执行集并记录为 `requires_confirmation`。被拒绝和暂不可编译的候选也保留在计划中供审计。

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
