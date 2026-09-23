# OpenAPI 接口测试与故障分析 Agent：执行方案草案

> 状态：需求与技术设计基线 v0.2
> 目标岗位：Agent 开发
> 模型：DeepSeek API
> 交互方式：CLI，无 Web UI
> 文档角色：本项目需求、范围、架构与验收标准的唯一事实源

## 1. 项目定位

本项目是一个面向 OpenAPI 服务的自主接口测试与故障分析 Agent，不是 Coding Agent。

它读取 OpenAPI 3.x 文档，在安全预算内选择测试目标、构造请求、调用真实 HTTP 接口、执行确定性校验，并根据观察结果决定继续原计划、追加诊断测试、重新规划或结束。最终输出测试报告、可复现的 curl、可执行 pytest 和完整轨迹。

运行时不是让 LLM 生成一段 Python/pytest 代码再执行。Agent 通过受控的 `send_http_request` 工具直接访问被测 HTTP 接口；探索结束后，程序才把已经验证过的请求与断言导出为 pytest 回归测试。

Agent 的核心闭环是：

```text
状态 → LLM 选择动作 → 代码校验动作 → 代码执行工具
     → 代码校验结果 → LLM 分析观察 → 更新状态 → 下一动作
```

设计原则：

- LLM 负责从自然语言描述和业务规则中提取语义约束、发现接口依赖、补充具体业务场景、选择下一项高信息量测试、提出故障假设和解释。
- 普通代码负责 OpenAPI 解析、安全策略、参数约束、HTTP 执行、Schema 校验、变量提取、去重、预算和持久化。
- OpenAPI 没有明确规定的业务期望，不得被报告为确定缺陷，只能标记为启发式风险或待确认项。
- 第一版使用单 Agent；不引入多 Agent、RAG、MCP 或 Web UI。

## 2. 被测演示服务

仓库内实现一个最小 Go 服务 `examples/buggy-shop`，使用内存存储。

第一版接口：

```text
POST /users
GET  /users/{id}
POST /orders
GET  /orders/{id}
DELETE /users/{id}
POST /test/reset
```

其中 `/test/reset` 只在测试模式启用，用于手动清空被测服务中的用户和订单数据。它与 Agent memory 无关：

- 环境重置：清除被测 Go 服务里由 POST 创建的数据。
- Agent memory/state 重置：清除当前运行的计划、变量、观察和假设。

`/test/reset` 是 Go 演示服务在测试模式下提供的辅助接口，不写入待测业务 OpenAPI，也不会被 Agent 当成业务接口测试。它完全由用户在 Go 演示服务侧手动触发，Agent 不提供 reset 工具，也不会调用该接口：

```bash
curl -X POST http://localhost:8080/test/reset
```

这个 reset 机制只是演示适配器，不是通用 Agent 对任意服务的强制要求。测试其他服务时，用户可以不配置 reset，或提供该服务自己的初始化脚本/命令。

演示服务包含可配置缺陷，但 Agent 的 prompt 和运行时不能读取缺陷清单。至少保留一组无缺陷配置，用于测试误报率。

## 3. Agent 接收的输入

典型命令：

```bash
python -m api_doctor run \
  --spec examples/buggy-shop/openapi.yaml \
  --base-url http://localhost:8080 \
  --max-tests 10 \
  --max-steps 20 \
  --auth-config examples/buggy-shop/auth.yaml \
  --output runs/demo-001
```

必填输入：

- OpenAPI 3.x YAML/JSON 文件；
- 被测服务 Base URL。

可选输入：

- 最大测试数 `max_tests`；
- 最大 Agent 决策步数 `max_steps`；
- tag/path/method 过滤条件；
- 鉴权配置；
- 额外业务规则文件；
- 服务日志路径；
- 随机种子；
- DELETE 等危险操作是否允许进入人工审批。

支持可选的业务规则文件。它用于表达 OpenAPI 无法完整描述的业务约束，也是第一版正式输入之一：

```yaml
rules:
  - id: unique-user-email
    operation_id: createUser
    description: 相同 email 不允许创建两个用户
    expected_status: [409]
```

断言必须记录来源：

- `contract`：来自 OpenAPI；
- `user_rule`：来自用户提供的规则；
- `heuristic`：来自 Agent 推测，不能单独判定为确定缺陷。

如果规则文件、OpenAPI 描述或实际行为之间存在歧义、缺失或冲突，最终报告增加“规格不明确与待确认事项”章节，列出原文、观察结果、Agent 的不同解释和建议向业务方确认的问题。

系统目标是测试任意满足支持范围的 OpenAPI 3.x 服务，而不是针对 `buggy-shop` 写死规则。`buggy-shop` 只承担本地演示、集成测试和固定评测样例的角色。所有测试生成逻辑必须以标准 OpenAPI 结构、可选规则文件和配置为输入，不能依赖示例服务的 operationId 或字段名。

## 4. 一个具体的完整执行例子

假设 OpenAPI 中有两个接口：

```yaml
paths:
  /users:
    post:
      operationId: createUser
      requestBody:
        required: true
        content:
          application/json:
            schema:
              type: object
              required: [name, email]
              properties:
                name: { type: string, minLength: 1 }
                email: { type: string, format: email }
      responses:
        "201":
          description: created
  /users/{id}:
    get:
      operationId: getUser
      parameters:
        - in: path
          name: id
          required: true
          schema: { type: integer, minimum: 1 }
      responses:
        "200": { description: found }
        "404": { description: not found }
```

被测服务隐藏了一个缺陷：`GET /users/999999` 对不存在用户返回 `200` 和空对象，而不是 OpenAPI 声明的 `404`。

### 4.1 第 0 步：建立运行状态

程序创建运行目录和状态：

```json
{
  "run_id": "demo-001",
  "phase": "initialize",
  "remaining_test_budget": 10,
  "remaining_step_budget": 20,
  "variables": {},
  "executed_cases": [],
  "hypotheses": [],
  "pending_cases": []
}
```

这是普通代码，不调用 LLM。

### 4.2 第 1 步：解析 OpenAPI

代码执行：

```text
load_openapi(path)
resolve_local_refs()
normalize_operations()
validate_spec()
```

输出结构化 operation registry：

```json
{
  "operations": [
    {
      "operation_id": "createUser",
      "method": "POST",
      "path": "/users",
      "required_fields": ["name", "email"]
    },
    {
      "operation_id": "getUser",
      "method": "GET",
      "path": "/users/{id}",
      "path_parameters": ["id"],
      "declared_responses": [200, 404]
    }
  ]
}
```

LLM 不读取整份 YAML，也不负责解析 `$ref`。

### 4.3 第 2 步：代码生成机械候选，LLM 生成语义候选

规则代码根据 Schema 生成机械候选测试主题：

```json
[
  {"kind": "happy_path", "operation_id": "createUser"},
  {"kind": "missing_required", "operation_id": "createUser", "field": "email"},
  {"kind": "invalid_format", "operation_id": "createUser", "field": "email"},
  {"kind": "not_found", "operation_id": "getUser", "parameter": "id"},
  {"kind": "dependency_flow", "operations": ["createUser", "getUser"]}
]
```

这部分相当于软件测试中的通用模板库，适合由代码完成。它保证 required、enum、格式和边界等确定性约束不会因为 LLM 遗漏而消失。

随后调用 LLM 读取经过裁剪的 description、参数语义和业务规则，生成模板无法可靠得到的语义候选，例如：

- 从“结束时间必须晚于开始时间”生成字段间关系测试；
- 从“取消后的订单不可再次支付”生成状态机测试；
- 从创建接口和查询接口的描述推断跨接口依赖；
- 根据字段含义生成合法而非随机的 email、日期、SKU 等数据；
- 指出描述中没有明确失败状态码或边界定义的规则空洞。

LLM 输出的是受限 DSL 表达的 `SemanticTestCandidate`，不是任意代码，也不是让程序再次理解一段自然语言关系：

```json
{
  "operation_id": "createOrder",
  "purpose": "验证结束时间早于开始时间时的处理",
  "constraint_source": "user_rule",
  "mutations": [
    {
      "kind": "field_relation",
      "left": "$.end_time",
      "operator": "lt",
      "right": "$.start_time",
      "delta": {"value": 1, "unit": "hour"}
    }
  ],
  "expected": {"status_codes": [400, 422]},
  "evidence_refs": ["rule:order-time-range"]
}
```

程序校验 operationId、字段路径、操作符、字段类型和证据引用后，把语义候选编译成统一的 `TestCase`，才加入候选池。第一版只支持有限的、可验证的 mutation DSL：

- `field_relation`：`lt/lte/eq/ne/gte/gt`；
- `set_value`：设置经过 Schema 校验的具体值；
- `omit_field`：删除字段；
- `boundary`：选择最小值、最大值及其相邻值；
- `reuse_variable`：引用前序响应中经过校验的变量；
- `state_sequence`：按 operationId 执行有限的接口序列。

不能映射到 DSL 的自然语言规则不会自动执行：系统将其列入“暂不支持自动编译的规则/待人工确认”，而不是让 LLM 随意生成请求。

对于日期关系，编译器使用本次运行固定的时间锚点和 seed 生成可复现值。例如 `end_time lt start_time` 且 delta 为一小时时：

```text
anchor = 2026-10-02T10:00:00Z
start_time = anchor
end_time = anchor - 1 hour
```

编译后程序再次验证：两个值都符合字段的 JSON Schema `format: date-time`，并且 `end_time < start_time` 确实成立。如果任一条件失败，候选不会进入执行队列。

```json
{
  "case_id": "TC-SEM-001",
  "operation_id": "createOrder",
  "request_template": {
    "body": {
      "start_time": "2026-10-02T10:00:00Z",
      "end_time": "2026-10-02T09:00:00Z"
    }
  },
  "assertions": [
    {
      "kind": "status_in",
      "allowed": [400, 422],
      "source": "user_rule",
      "evidence_ref": "rule:order-time-range"
    }
  ],
  "dependencies": [],
  "risk": "safe"
}
```

这个规范化 `TestCase` 有两个消费者：运行期间交给 HTTP 执行器直接探索；运行结束后交给 pytest renderer 生成可维护的回归测试。两条路径使用同一份测试定义，避免探索行为与生成代码不一致。

具体值的来源分三级：

1. 确定性生成器优先：根据 type、format、enum、边界、字段关系、固定 seed 和运行锚点生成；
2. LLM 可以在 DSL 无法直接构造普通业务值时提议具体值，但必须通过 Schema 和 DSL 关系校验；
3. 无法机器验证的值或关系不执行，进入报告的规格歧义/人工补充清单。

### 4.4 第 3 步：首次规划

这是第一次调用 LLM。输入不是整个 OpenAPI，而是：

```json
{
  "goal": "在最多 10 个请求内寻找高价值的接口契约缺陷",
  "operation_summaries": ["..."],
  "candidate_tests": ["..."],
  "auth_available": true,
  "dangerous_methods": ["DELETE"],
  "remaining_test_budget": 10
}
```

LLM 必须返回符合 Pydantic/JSON Schema 的 JSON，例如：

```json
{
  "plan_version": 1,
  "strategy": "先验证创建和查询主路径，再验证必填字段与不存在资源",
  "selected_cases": [
    {
      "case_id": "TC-001",
      "candidate_kind": "dependency_flow",
      "operation_id": "createUser",
      "purpose": "创建用户并提取 id",
      "priority": 100
    },
    {
      "case_id": "TC-002",
      "candidate_kind": "not_found",
      "operation_id": "getUser",
      "purpose": "验证不存在资源的响应",
      "priority": 90
    },
    {
      "case_id": "TC-003",
      "candidate_kind": "missing_required",
      "operation_id": "createUser",
      "purpose": "验证 email 必填约束",
      "priority": 80
    }
  ]
}
```

LLM 会选择测试意图和优先级，也会提供语义测试所需的字段关系、场景参数与依赖建议。最终请求仍由代码根据 Schema 组装并校验。

如果 LLM 输出无法通过结构校验，程序把校验错误反馈给模型修复一次；第二次仍失败则回退到规则候选的确定性排序。

### 4.5 第 4 步：执行创建用户

代码把 `TC-001` 具体化：

```json
{
  "method": "POST",
  "url": "http://localhost:8080/users",
  "headers": {"Content-Type": "application/json"},
  "body": {"name": "agent-test-demo001", "email": "demo001@example.test"}
}
```

发送前由代码检查：

- URL 是否在白名单；
- 是否发生跨域重定向；
- method 是否需要审批；
- body 是否满足本次 happy-path 的请求 Schema；
- Header 中是否有待脱敏字段；
- 超时、最大响应体、重定向次数是否符合限制。

实际响应：

```json
{
  "status": 201,
  "body": {"id": 42, "name": "agent-test-demo001", "email": "demo001@example.test"},
  "elapsed_ms": 18
}
```

代码校验状态码和响应 Schema，并按显式提取规则保存：

```json
{
  "variables": {
    "user_id": {
      "value": 42,
      "source_case": "TC-001",
      "source_path": "$.id",
      "validated_as": "integer"
    }
  }
}
```

请求执行、校验和变量写入都不由 LLM 完成。

### 4.6 第 5 步：执行依赖测试

代码将 `${variables.user_id}` 绑定到路径，执行：

```http
GET /users/42
```

返回 200 且 Schema 正确，记录为通过。这个结果会成为后续分析上下文，但完整响应不会反复塞进 LLM，只传摘要和必要证据。

### 4.7 第 6 步：发现契约失败

执行 `TC-002`：

```http
GET /users/999999
```

实际得到：

```json
{
  "status": 200,
  "body": {}
}
```

确定性校验代码发现：

```json
{
  "result": "fail",
  "violations": [
    {
      "type": "semantic_response_selection",
      "expected": "不存在资源应匹配已声明的 404 响应",
      "actual_status": 200,
      "evidence": "请求使用了未创建的大整数 ID"
    }
  ]
}
```

这里需要谨慎：OpenAPI 仅仅声明 200 和 404，并不能在所有情况下自动证明某个 ID 必然不存在。因此 Agent 必须记录该 ID 的生成方式，并最好用第二个探针确认。

### 4.8 第 7 步：观察分析与局部重新规划

代码触发 LLM 的 analyzer/replanner。输入：

```json
{
  "failed_case": {
    "operation_id": "getUser",
    "request": {"id": 999999},
    "actual_status": 200,
    "body_summary": "empty JSON object",
    "contract_responses": [200, 404]
  },
  "known_facts": [
    "本轮只创建了 user_id=42",
    "id=999999 未由任何成功创建请求产生"
  ],
  "remaining_test_budget": 7,
  "existing_case_fingerprints": ["..."]
}
```

LLM 返回：

```json
{
  "decision": "add_diagnostic_case",
  "hypothesis": {
    "statement": "查询接口可能把资源不存在错误映射为 200 空对象",
    "confidence": "medium",
    "supporting_evidence": ["未创建的 999999 返回 200 空对象"],
    "contradicting_evidence": []
  },
  "next_case": {
    "operation_id": "getUser",
    "mutation": {"path.id": 1000000},
    "purpose": "使用第二个未创建 ID 验证行为是否稳定",
    "expected_information_gain": "区分偶然数据碰撞和稳定的不存在资源处理错误"
  }
}
```

代码检查该测试不重复、ID 符合 Schema、预算充足，然后插入待执行队列首部。

这就是一次“响应驱动的局部重新规划”。它不需要把剩余计划全部推翻。

### 4.9 第 8 步：执行诊断测试并更新假设

第二个未创建 ID 仍返回 200 空对象。确定性代码记录事实；LLM 将假设置信等级从 `medium` 更新为 `high`，并建议停止围绕同一故障继续消耗预算。

Agent 随后回到原计划，执行缺失 email 的测试。

### 4.10 第 9 步：生成报告和 pytest

完成运行后：

1. 代码根据已执行请求和断言生成 pytest；
2. 代码运行 pytest，确认生成文件本身可执行；
3. 代码汇总事实和证据；
4. LLM 对失败聚类，生成故障假设与修复建议；
5. 报告器输出 Markdown、JSON、JUnit XML 和轨迹。

生成的 pytest 类似：

```python
def test_get_unknown_user_returns_404(api_client):
    response = api_client.get("/users/999999")
    assert response.status_code == 404
```

pytest 是可复现产物，不是 Agent 发 HTTP 的前提。Agent 本身可以直接通过 HTTP client 执行探索性测试；探索结束后再把有价值的失败固化成 pytest，用于以后回归和 CI。项目自身的解析器、校验器、安全策略等也使用 pytest 编写单元测试，这两类 pytest 要区分。

## 5. 哪些步骤使用 LLM

第一版在四个位置调用 LLM：

### 5.1 语义约束与场景提取

输入：单个或一组相关 operation 的描述、Schema 摘要和可选业务规则。
输出：带来源证据的字段关系、状态转换、跨接口依赖、有效测试数据建议和规格歧义。

### 5.2 初始规划

输入：接口摘要、规则候选、预算和安全限制。
输出：测试意图、优先级、依赖方向和选择理由。

### 5.3 失败分析与下一动作选择

输入：失败摘要、确定性证据、已有假设、已执行指纹和剩余预算。
输出：继续原计划、追加诊断测试、调整优先级或停止当前故障分支。

### 5.4 最终故障解释

输入：已经确定的失败事实和精简轨迹。
输出：故障分组、可能原因、支持/反对证据、置信等级和修复建议。

LLM 不负责：

- 解析 OpenAPI；
- 判断 JSON Schema 是否满足；
- 强制执行安全策略；
- 真正发送 HTTP；
- 写入未经验证的变量；
- 决定是否绕过人工审批；
- 凭空创造业务断言。

如果关闭所有 LLM 能力，系统仍可作为基于 OpenAPI Schema 的确定性测试器运行，这是必须保留的 baseline。加入 LLM/Agent 后的价值必须通过对照评测证明：能否发现更多语义缺陷、构造更多有效跨接口流程，以及用更少请求定位故障。若这些指标没有改善，就不应声称 Agent 带来了实际收益。

## 6. 什么时候重新 Plan

不是每执行一个请求都重新生成完整计划。Plan 的价值只在测试空间大于预算，或用例之间存在依赖时成立；如果接口很少且预算足够，系统可以直接执行全部确定性候选，不为展示 Agent 而强行规划。区分三种情况：

### 6.1 不重新规划

- 测试通过且没有产生新变量；
- 失败类型已知且已有等价诊断测试；
- 下一条计划仍可执行。

此时直接执行队列中的下一条。

### 6.2 局部重新规划

- 出现新的失败；
- 得到了可供其他接口使用的新变量；
- 某个假设需要一个或两个对照测试；
- 原计划中的前置条件失败；
- 测试结果使某些待执行用例失去价值。

局部重新规划只新增、取消或调整少量用例。

### 6.3 全量重新规划

- 登录或鉴权状态变化导致大批接口不可执行；
- 关键依赖接口失败，原计划大面积失效；
- OpenAPI 与实际服务的基础路径或版本明显不一致；
- 连续出现相同类别错误，需要改变整体策略；
- 已消耗约一半预算但没有获得有效覆盖或信息。

每次 replan 都增加 `plan_version` 并记录触发原因。

## 6.4 LLM 每轮可以做出的决定

Analyzer/Decision 节点只能从受控枚举中选择下一动作：

- `continue_plan`：当前结果没有改变策略，执行队列中的下一条；
- `add_diagnostic_case`：追加一个用于区分故障假设的对照测试；
- `revise_pending_cases`：取消、替换或调整尚未执行的少量用例；
- `request_operation_context`：通过 `get_operation` 获取某个接口的详细约束；
- `request_human_input`：业务规则、凭据或危险操作需要人确认；
- `skip_blocked_branch`：前置接口失败，跳过依赖它的测试分支；
- `full_replan`：关键依赖或整体假设发生变化，重新规划剩余预算；
- `finish`：没有更高价值的测试，建议结束。

LLM 只能建议这些动作。宿主程序仍负责检查预算、重复用例、安全策略和硬终止条件，并可拒绝不合法的决定。

## 7. 循环与终止条件

建议的控制循环：

```python
while not should_stop(state):
    if needs_initial_plan(state) or needs_full_replan(state):
        state.plan = call_planner_llm(state)

    case = select_next_case(state)
    safe_request = materialize_and_validate(case, state)

    if safe_request.requires_approval:
        request_human_approval(safe_request)

    response = send_http_request(safe_request)
    validation = validate_response(case, response)
    update_variables_and_trace(state, response, validation)

    if should_analyze(validation, state):
        decision = call_analyzer_llm(state, validation)
        apply_validated_decision(state, decision)

finalize_report_and_regression_tests(state)
```

硬终止条件：

- `executed_request_count >= max_tests`；
- `agent_step_count >= max_steps`；
- 总运行时间超过限制；
- 目标服务连续两次不可达；
- 连续两次生成相同测试指纹；
- 用户拒绝关键危险操作且没有安全替代路径；
- LLM 连续输出无效结构且规则回退也无法继续。

正常终止条件：

- 待执行队列为空；
- 没有未验证的高价值假设；
- 所有剩余候选的信息价值低于阈值；
- 当前覆盖目标已经达到。

终止由确定性代码判断，LLM 可以建议结束，但不能突破硬预算。

## 8. 工具与代码边界

### 8.1 Agent 可选择的工具

- `list_operations(filter)`；
- `get_operation(operation_id)`；
- `execute_test(operation_id, purpose, parameters, body, expected)`；
- 可选 `read_service_logs(correlation_id, time_range)`。

`execute_test` 是高层受控工具：模型选择 OpenAPI 中的 `operation_id` 并提交结构化测试数据，不能提交任意完整 URL。工具内部依次执行请求物化、Schema 校验、安全策略、鉴权注入、HTTP 调用、响应校验和脱敏，再把结构化观察返回给模型。这样保留真实 tool-calling 闭环，同时不允许模型绕过安全边界。

### 8.2 工作流固定调用的内部能力

- `load_openapi`；
- `resolve_refs`；
- `generate_rule_candidates`；
- `materialize_request`；
- `enforce_safety_policy`；
- `send_http_request`；
- `validate_response`；
- `extract_variables`；
- `fingerprint_case`；
- `save_trajectory`；
- `generate_pytest`；
- `save_report`。

第一版不使用 MCP。这些工具与 Agent 运行在同一 Python 应用内，直接函数调用更易测试。后续可把稳定能力包装为 MCP Server 作为加分项。

第一版不使用 RAG。OpenAPI 是结构化数据，按 operationId/path 精确查询比向量检索可靠。只有加入大量历史故障、业务文档或排障手册时再考虑 RAG。

## 9. 状态与 Memory

### 9.1 单次运行状态

```python
class AgentState(TypedDict):
    run_id: str
    spec_fingerprint: str
    operation_registry: dict[str, Operation]
    candidate_cases: list[TestCandidate]
    pending_cases: list[TestCase]
    executed_cases: list[ExecutedCase]
    variables: dict[str, ExtractedVariable]
    hypotheses: list[Hypothesis]
    plan_version: int
    remaining_test_budget: int
    remaining_step_budget: int
    duplicate_fingerprints: set[str]
    auth_context: AuthContext
    final_report: dict | None
```

### 9.2 持久化产物

```text
runs/<run_id>/
├── manifest.json
├── normalized-openapi.json
├── trajectory.jsonl
├── cases.json
├── generated_tests/
│   └── test_regressions.py
├── junit.xml
├── report.json
└── report.md
```

### 9.3 长期 Memory

MVP 不做跨运行的自动经验记忆。运行历史保留为文件，但不会未经审核自动注入下一次运行。后续若增加长期记忆，必须绑定服务、OpenAPI 指纹、来源、时间和失效条件。

## 10. DeepSeek 与 LangChain/LangGraph

DeepSeek 提供 OpenAI 兼容 API，并支持 JSON Output 和 tool calls，因此可通过 LangChain 接入；但“兼容”不代表所有模型和模式的行为完全相同。

第一版建议封装一个独立 `ModelGateway`，避免业务逻辑直接依赖具体 SDK：

```python
class ModelGateway(Protocol):
    def plan(self, request: PlanRequest) -> PlanDecision: ...
    def analyze(self, request: AnalysisRequest) -> AnalysisDecision: ...
    def summarize(self, request: ReportRequest) -> ReportAnalysis: ...
```

实现可以选择：

- LangChain 的 OpenAI-compatible chat model 配置 DeepSeek `base_url`；或
- 直接使用 DeepSeek/OpenAI-compatible SDK，在 LangGraph 节点内调用。

无论哪种方式，都必须：

- 对 LLM 输出做 Pydantic 校验；
- 对 tool arguments 再做一次业务校验；
- 允许一次结构修复重试；
- 提供规则式回退；
- 锁定并记录实际模型名；
- 为不同模型模式编写最小集成测试。

第一版优先使用非 thinking 模式完成严格结构化决策。如果以后使用 thinking mode，需要验证 DeepSeek 对多轮 tool calls 的消息历史要求。

## 11. 鉴权范围

这里的鉴权指被测 API 的鉴权，不是 DeepSeek API Key。

“主流鉴权全部支持”范围过大，因此分层实现：

### MVP 必须支持

- 无鉴权；
- OpenAPI `apiKey`：Header、Query、Cookie；
- 静态 Bearer Token；
- HTTP Basic；
- 可配置登录接口，通过 JSONPath 从响应提取 Bearer Token；
- OAuth2 Client Credentials。

### 后续支持

- OAuth2 Authorization Code；
- PKCE；
- OpenID Connect；
- 自动刷新复杂 token；
- 签名鉴权，如 AWS SigV4 或厂商私有签名。

Authorization Code/PKCE 通常需要浏览器和用户登录，不适合第一版无 Web UI 的 CLI 全自动流程。遇到此类 OpenAPI security scheme 时，MVP 允许用户预先提供 token。

所有凭据只能来自环境变量或独立配置引用，不能写入 OpenAPI、轨迹、报告或生成的 pytest。

## 12. DELETE 人工确认

默认规则：

- GET/HEAD/OPTIONS 自动允许；
- POST 自动允许，但仅针对用户指定的被测 Base URL；
- PUT/PATCH 与 POST 一样默认允许，但仍受域名白名单、请求预算和副作用记录约束；
- DELETE 每次执行前人工确认；
- CLI 非交互模式下没有显式 `--approve-delete` 就跳过 DELETE。

确认信息必须包含方法、完整路径、请求体摘要和潜在影响。Agent 无权自行批准。

## 13. pytest 的角色

pytest 有两种不同用途：

1. 项目自身测试：验证 OpenAPI 解析、安全策略、Schema 校验、变量提取等代码；这是开发项目必需的单元/集成测试。
2. Agent 生成的回归测试：把一次探索运行中有价值且可重复的用例固化下来；这是 MVP 的最终产物之一。

Agent 探索阶段不依赖 pytest，因为它需要按观察结果动态决定下一请求；固定 pytest 文件更适合探索完成后的复现和 CI 回归。

生成 pytest 时必须处理：

- Base URL 和凭据从 fixture/环境读取；
- 前置资源通过 fixture 创建；
- 动态 ID 不写死；
- 敏感 Header 不落盘；
- 测试之间尽量隔离；
- 生成后实际执行一次，不能只声称“可执行”。

## 14. 第一版验收标准

- 一条命令启动 Go 被测服务；
- 手动命令可以恢复被测服务初始状态；
- 解析演示服务全部 OpenAPI 接口；
- 支持至少 6 类规则候选；
- 完成创建后查询的依赖链和变量提取；
- 展示至少一次由失败触发的局部 replan；
- DELETE 经过人工确认；
- 支持 MVP 鉴权集合，并至少为静态 Bearer 和登录取 token 提供演示/测试；
- 输出 Markdown、JSON、trajectory.jsonl、pytest 和 JUnit XML；
- 生成的 pytest 实际运行通过或准确复现已发现缺陷；
- 在有缺陷和无缺陷配置上运行固定评测；
- 记录缺陷召回、误报、请求数、Agent 步数、时延和模型 token 使用量；
- 不使用伪造的准确率或效率数据。

## 14.1 本地运行与可选 Docker

默认开发和演示路径不依赖 Docker：

```bash
# 终端 1：启动 Go 演示服务
cd examples/buggy-shop
go run .

# 终端 2：运行 Agent
python -m api_doctor run \
  --spec examples/buggy-shop/openapi.yaml \
  --base-url http://localhost:8080
```

Docker Compose 只作为后续可选的“一键复现环境”，不是开发前置条件。没有安装 Docker 的 macOS 环境可以完整开发和运行项目。

## 15. 当前已确认的产品决策

- 目标岗位：Agent 开发；
- 模型提供方：DeepSeek API；
- 编排：倾向 LangGraph，但通过 `ModelGateway` 隔离模型 SDK；
- 第一版：优先做可运行闭环，不做 Web UI；
- 测试范围：OpenAPI 描述的接口；
- POST：允许；
- DELETE：人工确认后允许；
- 被测后端：仓库内实现最小 Go 服务；
- 环境恢复：用户手动触发；
- 鉴权：按分层范围实现；
- pytest：既用于项目自身测试，也作为 Agent 生成的回归产物；
- MCP/RAG/多 Agent：不进入第一版。
- 业务规则文件：第一版支持；规则歧义和缺失进入最终报告。
- PUT/PATCH：与 POST 一样默认允许。
- reset：由 Go 演示服务在测试模式提供，不进入正式业务 OpenAPI；用户在 Go 服务侧手动触发，Agent 不调用。
- 运行环境：本地直接运行是默认路径，Docker Compose 只作为可选复现方式。

## 16. 仍需后续确认

当前没有阻塞实现的产品决策。已确认：

- DeepSeek 使用 `deepseek-chat` 非 thinking 模式；
- 业务规则文件使用 YAML；
- 自然语言规则没有明确 expected status 时只报告规格歧义，不擅自把 400/409/422 当成确定答案。

## 17. TestSpec IR 设计

### 17.1 定位

TestSpec IR 是 LLM、规则引擎、用户规则与执行器之间的结构化中间表示。它不是一门追求表达任意业务逻辑的通用编程语言，也不包含可直接执行的 Python 代码。

```text
OpenAPI 规则引擎 ─────┐
业务规则 + LLM ───────┼── TestSpec ── Validator/Compiler ── CompiledTestCase
失败分析 + LLM ───────┤                                  ├── HTTP Executor
用户手写用例 ─────────┘                                  └── pytest Renderer
```

它的作用是把 LLM 的开放式推理限制为可校验、可执行、可复现和可审计的动作。不能映射到受支持 IR 的规则必须拒绝自动执行，并进入报告中的“规格不明确或暂不支持自动化”章节。

### 17.2 三阶段模型

`SemanticTestCandidate` 描述测试意图，允许缺少部分具体值：

```json
{
  "operation_id": "createOrder",
  "purpose": "验证结束时间早于开始时间",
  "mutations": [
    {
      "kind": "field_relation",
      "left": "$.end_time",
      "operator": "lt",
      "right": "$.start_time",
      "delta": {"value": 1, "unit": "hour"}
    }
  ],
  "expected": {"status_codes": [400, 422]},
  "source": {"kind": "business_rule", "id": "order-time-range"}
}
```

`CompiledTestCase` 必须包含可以直接执行的请求、明确断言、来源证据和依赖：

```json
{
  "case_id": "TC-SEM-001",
  "operation_id": "createOrder",
  "request": {
    "body": {
      "product_id": 42,
      "quantity": 1,
      "start_time": "2026-10-02T10:00:00Z",
      "end_time": "2026-10-02T09:00:00Z"
    }
  },
  "assertions": [
    {
      "kind": "status_in",
      "allowed": [400, 422],
      "source": "user_rule",
      "evidence_ref": "rule:order-time-range"
    }
  ],
  "dependencies": [],
  "risk": "safe"
}
```

`TestResult` 保存事实，不混入未经证实的推断：

```json
{
  "case_id": "TC-SEM-001",
  "outcome": "failed",
  "actual_status": 201,
  "violations": ["status_not_in_expected_set"],
  "evidence": ["end_time 早于 start_time，但服务返回 201"]
}
```

### 17.3 MVP mutation 类型

- `set_value`：设置通过 Schema 校验的值；
- `omit_field`：删除字段；
- `boundary`：生成最小值、最大值及相邻无效值；
- `field_relation`：两个数字或日期字段之间的 `lt/lte/eq/ne/gte/gt`；
- `reuse_variable`：引用前序响应中经过验证的变量；
- `workflow`：执行两到四步 operation 序列；
- `repeat_request`：重复相同请求以验证幂等性或稳定性。

### 17.4 MVP assertion 类型

- `status_in`；
- `schema_valid`；
- `json_path_exists`；
- `json_path_equals`；
- `responses_same_value`；
- `header_present`；
- `latency_below`。

### 17.5 具体值生成优先级

1. OpenAPI `example/examples`；
2. OpenAPI `default`；
3. `enum` 中的合法值；
4. 前序接口响应中提取并验证的变量；
5. 用户提供的 fixture 或业务规则；
6. 基于 type、format、范围、固定 seed 和时间锚点的确定性生成器；
7. LLM 提议的具体值，必须再次通过 Schema 和语义关系校验；
8. 仍不能可靠生成时标记为 `blocked`，不得猜测后执行。

对于日期关系，运行 manifest 保存固定 `time_anchor`。例如 `end_time lt start_time` 且相差一小时：编译器设置 `start_time = anchor`、`end_time = anchor - 1 hour`，随后反向验证两个值满足 `date-time` 格式且关系成立。

### 17.6 编译与拒绝规则

编译器必须验证：

- operationId、字段路径和响应路径真实存在；
- mutation 类型与目标字段类型兼容；
- 断言有 OpenAPI、业务规则或用户输入作为依据；
- 依赖变量有明确生产者和提取规则；
- 所有具体值满足除本次刻意违反约束之外的其他 Schema 条件；
- 测试没有绕过安全策略、预算或审批；
- 编译后的请求确实满足目标 mutation；
- 用例指纹不与已执行或待执行测试重复。

以下情况不自动执行：

- 规则使用了 IR 不支持的语义；
- 字段映射存在多个无法区分的候选；
- 缺少构造合法基线所需的数据；
- 期望结果没有来源；
- 规则本身冲突或不可观察；
- 需要未配置的外部系统或身份。

### 17.7 扩展路线

成熟版本不扩张成一门巨型 DSL，而采用“核心 IR + 插件节点”：

- 内置 IR 覆盖高频、可验证场景；
- 插件支持分页、异步轮询、最终一致性、多角色权限、文件上传、签名鉴权等领域能力；
- 用户可注册受控 generator、extractor 和 assertion；
- 最后才考虑在隔离沙箱中运行经人工审批的 LLM 生成代码。

产品能力应表述为：对任意处于支持范围内的 OpenAPI 3.x 服务执行通用契约测试，并对可映射到内置 TestSpec IR 的描述和业务规则生成跨字段、跨接口及有限状态流程测试；无法可靠自动化的规则会被识别和报告。不承诺自动理解所有业务逻辑。

## 18. OpenAPI 功能需求与支持边界

MVP 必须解析：

- path、HTTP method、operationId、tag、summary、description；
- path/query/header/cookie 参数及序列化信息；
- requestBody 和 JSON Schema；
- response 状态码、媒体类型和 JSON Schema；
- required、enum、minimum、maximum、exclusiveMinimum、exclusiveMaximum；
- minLength、maxLength、pattern、format；
- array 的 minItems、maxItems、uniqueItems；
- securitySchemes 基本信息；
- 本文件内部 `$ref`。

解析失败或暂不支持的字段必须产生带位置的 warning，不能静默忽略。第一版重点支持 JSON request/response；multipart、二进制、callback、webhook、SSE、WebSocket、复杂 discriminator 及任意外部 `$ref` 可以报告为暂不支持。

## 19. 报告、轨迹与可观测性

最终报告必须包含：

- 测试环境、OpenAPI 指纹、模型名、seed、时间锚点和运行时间；
- 接口数、候选数、编译数、执行数、通过数、失败数、阻塞数；
- 每个失败的请求摘要、实际响应、期望、规则来源和可复现 curl；
- 状态码、Schema 差异、耗时和可选日志等确定性证据；
- 明确标记为“假设”的可能原因及支持/反对证据；
- 规格不明确、规则冲突、不可编译规则和待人工确认问题；
- 修复建议；
- 生成 pytest、JUnit XML 和完整轨迹的位置。

`trajectory.jsonl` 每行至少包含：

```json
{
  "timestamp": "2026-01-01T12:00:00Z",
  "run_id": "uuid",
  "step": 3,
  "type": "llm|tool|decision|error|final",
  "name": "execute_test",
  "input": {},
  "output_summary": "",
  "duration_ms": 126,
  "success": true
}
```

Authorization、Cookie、API Key、token 和业务敏感字段必须在落盘前统一脱敏。完整大响应不重复写入模型上下文；轨迹保存引用、哈希和受限摘要。

## 20. 推荐目录结构

```text
api-test-agent/
├── README.md
├── AGENTS.md
├── EXECUTION_DESIGN.md
├── pyproject.toml
├── .env.example
├── src/
│   └── api_doctor/
│       ├── __init__.py
│       ├── cli.py
│       ├── config.py
│       ├── openapi/
│       │   ├── loader.py
│       │   ├── resolver.py
│       │   ├── catalog.py
│       │   └── models.py
│       ├── testspec/
│       │   ├── models.py
│       │   ├── compiler.py
│       │   ├── generators.py
│       │   ├── mutations.py
│       │   └── validation.py
│       ├── generation/
│       │   ├── schema_candidates.py
│       │   ├── semantic_extractor.py
│       │   └── prompts/
│       ├── execution/
│       │   ├── http.py
│       │   ├── auth.py
│       │   ├── safety.py
│       │   ├── variables.py
│       │   └── response_validation.py
│       ├── agent/
│       │   ├── graph.py
│       │   ├── state.py
│       │   ├── planner.py
│       │   ├── analyzer.py
│       │   └── decisions.py
│       ├── reporting/
│       │   ├── report.py
│       │   ├── trace.py
│       │   ├── pytest_renderer.py
│       │   └── junit.py
│       ├── llm/
│       │   ├── gateway.py
│       │   └── deepseek.py
│       └── evaluation/
│           └── runner.py
├── examples/
│   └── buggy-shop/
│       ├── go.mod
│       ├── main.go
│       ├── openapi.yaml
│       ├── rules.yaml
│       └── README.md
├── benchmarks/
│   └── m0/
│       ├── operations.json
│       ├── rules.yaml
│       └── expected_candidates.json
├── tests/
│   ├── unit/
│   ├── integration/
│   └── e2e/
└── runs/
```

目录按职责而不是里程碑拆分。不会创建 `api_doctor/m1`、`m2` 或 `m3`；M0 验证通过的代码直接进入正式职责目录。`tools` 不作为混合目录使用：LLM 可见的高层工具由 Agent 层定义，HTTP、鉴权和安全等底层能力归 `execution`。`models` 也不设为全局杂物目录，数据模型与所属领域放在一起。

依赖方向保持为：

```text
agent ──→ generation ──→ llm
  │           │
  ├────────→ testspec ←── openapi
  ├────────→ execution ──→ testspec
  └────────→ reporting
```

`testspec` 和 `openapi` 不依赖 LLM、Agent 或 HTTP；`execution` 不决定下一步测什么；`agent` 只负责状态与决策循环。目录按实际功能逐步创建，不为尚未实现的能力预建空文件。

## 21. 开发里程碑

### M0：技术验证

- 准备十条覆盖字段关系、条件必填、跨接口依赖、幂等性及模糊规则的样例；
- 打通“规则 → LLM TestSpec → Pydantic 校验 → 编译 → HTTP 执行”；
- 记录首次结构化成功率、编译成功率、非法字段引用率和拒绝率；
- 若技术验证不稳定，先缩小 IR，不扩展功能。

M0 当前实现状态（2026-09-24）：

- 已实现 Pydantic `SemanticTestCandidate`、`CompiledTestCase`、`TestResult`；
- 已实现 `set_value`、`omit_field`、`boundary`、数字/日期 `field_relation` 的有限编译器；
- 已实现固定 seed/时间锚点、operation/字段引用校验和拒绝路径；
- 已准备 10 条规则，其中包含 5 条可执行语义规则、1 条非法字段引用和 4 条应拒绝/暂不支持规则；
- 已实现 DeepSeek JSON Output 抽取入口以及独立 fixture 抽取器；
- 已实现最小 Go HTTP 服务，并以 fixture 候选完成真实 HTTP 验证；
- 实际 fixture 结果：10/10 通过结构校验，6 条声明可编译，5 条成功编译并执行，5/5 HTTP 结果符合预期，1 条非法字段引用被拒绝，4 条能力外规则被明确拒绝；
- Python 测试实际结果：5 passed；Go 编译测试通过；
- 尚未记录 DeepSeek 实时生成指标：当前项目环境未配置 `DEEPSEEK_API_KEY`，fixture 结果不得作为模型成功率。

### M1：确定性基线

- OpenAPI 解析和规范化；
- Schema 机械候选生成；
- HTTP 执行、安全策略和响应校验；
- Go 演示服务和可开关缺陷；
- 固定计划端到端运行；
- Markdown/JSON 报告和单元测试。

### M2：Agent 闭环

- DeepSeek 语义候选提取；
- TestSpec 编译器；
- LangGraph 状态循环；
- 跨接口变量提取与工作流；
- 失败后的诊断测试与局部 replan；
- 结构化轨迹和硬预算。

### M3：可交付性

- pytest renderer 和 JUnit XML；
- 有缺陷/无缺陷对照评测；
- 鉴权、审批、脱敏和错误恢复；
- README、演示命令、示例报告和 GIF；
- 可选 Docker Compose，不阻塞本地运行。

时间不足时优先删除 Web UI、跨运行长期记忆、多 Agent、RAG、MCP、复杂 OAuth 浏览器流程、性能测试和自动修复业务代码。

## 22. 评测与简历数据

至少比较“规则引擎 baseline”和“Agent 模式”：

- Schema 缺陷发现数；
- 语义缺陷发现数；
- 植入缺陷召回率；
- 正常服务误报率；
- TestSpec 首次生成成功率；
- TestSpec 编译成功率；
- 非法字段/operation 引用率；
- 无法编译规则的正确拒绝率；
- 跨接口工作流成功率；
- 每发现一个有效缺陷所需请求数；
- 生成 pytest 的实际可运行率；
- 平均 Agent 步数、耗时、token 和费用；
- 相同 seed 下结果稳定性；
- 危险请求拦截与审批正确率。

只使用真实运行数据。10～30 个固定样例足以支撑项目阶段的面试讨论，不预先编造“准确率 90%”或“效率提升 80%”。

## 23. 非目标与未来加分项

MVP 非目标：

- 完整替代 Postman、JMeter 或商业 API 测试平台；
- 高并发压力测试；
- 扫描互联网上任意服务；
- 自动理解所有业务规则；
- 自动修复被测业务代码；
- 多用户平台、数据库管理后台或复杂 Web UI；
- 在宿主机直接执行任意 LLM 生成代码。

未来加分项：

- 服务日志与请求 correlation ID 关联；
- 修复前后回归对比；
- CI 集成和历史趋势；
- 分页、异步轮询、最终一致性和多角色权限插件；
- TestSpec 插件 SDK；
- 将稳定测试能力包装为 MCP Server；
- 在严格沙箱和人工审批下生成自定义测试代码。

## 24. 文档治理

- `EXECUTION_DESIGN.md`：唯一需求与架构事实源，保存范围、决策、数据模型、流程、验收和路线图；旧实施规格的有效内容均已合并到此处。
- `README.md`：面向用户和面试官，只保存项目价值、架构摘要、快速开始、演示效果、当前状态和文档入口，不复制完整设计。
- `AGENTS.md`：面向 Codex 等编码 Agent，规定先读哪些文档、实现边界、测试命令、安全约束和交付纪律，不承载产品需求。
- `examples/buggy-shop/README.md`：说明演示服务、缺陷开关和手动 reset。
- 后续只有当文档明显过长影响维护时，才从本文件拆出 `docs/testspec-ir.md`、`docs/security.md` 或 ADR；拆分后必须在本文件保留索引和关键决策摘要。

旧的 `OpenAPI接口测试与故障分析Agent-实施规格.md` 在确认本文件内容完整后可以删除，删除前不再向旧文档写入新决策。
