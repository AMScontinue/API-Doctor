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

### 4.3.1 两条候选生成路径如何合并

Schema 规则引擎与 LLM 语义提取器都输出统一的 `SemanticTestCandidate`，但二者不是简单地“全部执行”。候选进入执行队列前依次经过：

1. **上下文校验**：确认 `rule_id`、证据引用、operationId、字段路径及 expected 与原始 OpenAPI/业务规则一致，防止模型把一条规则错误绑定到另一接口或篡改预期；
2. **规范化**：统一 JSONPath、状态码集合、mutation 顺序和默认值表示；
3. **指纹与去重**：根据 operationId、请求位置、mutation 和 assertion 生成稳定指纹；
4. **来源合并**：如果 Schema 路径和语义路径生成等价用例，只保留一个候选，但同时记录 `contract`、`user_rule`、`llm_inference` 等全部 provenance；
5. **冲突检测**：如果两个来源对同一场景给出不同期望，不自动任选一个，而是标记为规格冲突并请求人工确认；
6. **依赖、风险与价值评估**：分析前置接口、变量生产者、副作用、是否需审批，以及在剩余预算内的信息价值；
7. **选择执行集**：保留全部候选及其处理决定用于审计，只把通过校验且优先级足够的候选编译、执行。

例如，OpenAPI 的 `required: [email]` 与业务规则“创建用户时 email 必填”会产生等价的 `omit_field($.email)`。系统应合并为一个测试，并保留两个断言来源，而不是发送两次相同请求。

候选融合是确定性代码职责。LLM 可以提供语义候选、依赖建议和优先级理由，但不能决定跳过校验、覆盖来源冲突或突破执行预算。

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

### 6.5 什么才算有价值的 replan

replan 必须由新观察触发，并至少满足以下一项，否则继续固定队列，不调用 LLM：

- 前置接口失败或变量缺失，使一批依赖用例不可执行，需要取消或替换分支；
- 一个失败存在多个合理解释，需要追加控制变量实验来区分假设；
- 新响应产生了经过验证的 ID/token 等变量，解锁此前不可执行的跨接口用例；
- 鉴权失效、限流、服务版本不一致等系统性观察使原计划大面积失效；
- 人工拒绝危险操作后，需要保留安全分支并阻塞依赖该操作的后续流程；
- 候选数显著大于剩余请求预算，需要依据风险、依赖解锁能力和信息价值重新排序。

典型诊断例子：`GET /users/{unknown_id}` 违反契约返回 `200 {}` 后，Agent 不能直接断言根因，而应追加“创建真实用户后查询”和“查询另一个确定不存在 ID”等对照实验，用新观察区分“查询接口整体异常”和“不存在资源分支错误”。这种观察驱动的诊断选择是 M2 的核心 Agent 价值。

状态码明确、下一条用例独立、预算耗尽、重复指纹、Schema/安全策略确定性失败等情况由代码直接处理，不为展示 Agent 而调用 replanner。

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

本项目中的 memory 不是为了贴 Agent 标签而增加向量数据库，而是为决策闭环保存“下一步真正需要的状态”。分为三层：

- **Working Memory（MVP/M2）**：单次运行内的计划、候选、变量、观察、假设、预算和审批状态，由 LangGraph state 或等价状态机管理；
- **Episodic Memory（MVP/M2）**：本次运行的 checkpoint、轨迹、请求证据和报告，支持崩溃恢复、人工审计及按 `run_id` 恢复；
- **Long-term Semantic Memory（后续候选）**：跨运行复用已确认的接口依赖、稳定数据构造经验和故障模式。每条记忆必须绑定服务身份、OpenAPI 指纹、来源、置信度、创建时间和失效条件，且默认不自动写入下一次决策上下文。

LLM 上下文不等于完整 memory。上下文构建器只选择当前节点需要的 operation 摘要、最近观察、活跃假设、相关变量和剩余预算；大响应与旧轨迹只保存引用和摘要，避免上下文无限增长。

MVP 不实现 LLM compactor。运行受 `max_tests/max_steps` 硬预算约束，Observation 使用结构化摘要，完整响应和历史轨迹落盘后仅通过 evidence reference 引用。确定性的 Context Builder 按 operation 依赖、变量血缘、故障分组和当前假设选取模型上下文，比再次调用 LLM 压缩更可复现，也不会丢失关键证据。只有未来出现长时间运行、大量非结构化工具输出并确实逼近上下文窗口时，才重新评估 compaction。

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
    blocked_branches: list[BlockedBranch]
    pending_approval: ApprovalRequest | None
    active_context_refs: list[EvidenceRef]
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

### 9.4 Checkpoint、恢复与遗忘

- 每次工具执行后保存 checkpoint；恢复时不得重复发送已经完成的有副作用请求；
- checkpoint 保存 plan version、请求指纹、变量来源、预算消耗和待审批动作；
- “开始新运行”默认创建新的 Working Memory，不等于删除历史运行产物；
- 用户可手动删除或归档某个 run，这与手动调用 Go 服务 `/test/reset` 是两件独立的事；
- 长期记忆若以后启用，需要显式确认或满足可信写入策略，不能把一次模型猜测永久化。

### 9.5 Agent 化能力的实现优先级

以下能力作为 M2/M3 讨论时的候选清单，不表示全部都必须进入第一版：

1. **必须实现**：显式状态图、结构化 tool call、Working Memory、硬预算和终止条件、局部 replan、checkpoint/trajectory、人工审批、规则式回退；
2. **强烈建议**：上下文裁剪、错误恢复、变量血缘、假设的支持/反对证据、Agent 与确定性 baseline 对照评测；
3. **后续可选**：跨运行长期记忆、MCP 封装、历史故障 RAG；
4. **暂不建议为展示而加入**：多 Agent、无明确语料来源的向量库、让模型任意生成并执行代码。

简历展示的重点不是使用了多少 Agent 名词，而是能展示一次可审计闭环：模型基于真实观察选择诊断动作，宿主代码验证并执行工具，结果写回状态，触发局部重规划，最终在预算内停止。

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

- `use_baseline`：使用确定性生成的 Schema 合法基线，用于 happy path；
- `set_value`：设置通过 Schema 校验的值；
- `omit_field`：删除字段；
- `boundary`：生成最小值、最大值及相邻无效值；
- `set_parameter` / `omit_parameter`：设置或删除 query/header/cookie/path 参数，path 参数不允许删除；
- `parameter_boundary`：生成参数 Schema 边界外的值；
- 不存在资源探针使用 `set_parameter` 表达，不增加一个行为完全相同的 mutation 类型；仅有 404 契约不能证明具体值必然不存在，因此其 source 标记为启发式证据；
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
│       │   ├── schema_validation.py
│       │   └── models.py
│       ├── testspec/
│       │   ├── models.py
│       │   ├── compiler.py
│       │   ├── openapi_compiler.py
│       │   ├── value_generator.py
│       │   └── validation.py
│       ├── generation/
│       │   ├── schema_candidates.py
│       │   ├── semantic_extractor.py
│       │   └── semantic_prompt.py
│       ├── execution/
│       │   ├── http.py
│       │   ├── models.py
│       │   ├── runner.py
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
│           ├── catalog.py
│           ├── cli.py
│           ├── models.py
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
│       └── compiler_fixtures.json
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
- M0 收尾重构后的 Python 测试实际结果：15 passed；离线 fixture 编译指标符合预期；Go 编译测试通过；
- 已完成 DeepSeek 实时抽取验证：加强 JSONPath Schema 后，10/10 规则返回合法结构，4 条成功编译；5 条确实不可执行、非法或模糊的规则均被正确拒绝；模型额外保守拒绝了 1 条可由 `set_value + omit_field` 表达的条件必填规则，因此可执行语义规则召回为 4/5（80%）。这是当前小样本技术验证结果，不外推为产品准确率。
- DeepSeek 少编译的具体规则是 `card-token-required`：“`payment_method` 为 `card` 时 `card_token` 必填”。人工 fixture 将其表示为先执行 `set_value($.payment_method, "card")`，再执行 `omit_field($.card_token)`；DeepSeek 却认为现有 mutation 无法表达条件依赖并返回 `not_compilable`。这说明当前短板是模型没有理解“多个原子 mutation 可以组合成一个条件场景”，不是编译器缺少执行能力。后续优先通过 prompt 中加入组合示例、few-shot 和一次针对 `unsupported_reason` 的自检/修复提高召回；最终仍由编译器验证组合后的请求确实满足前件并违反后件。
- M0 随后加入 mutation 可组合说明、条件必填 few-shot，以及模型首次返回 `not_compilable` 时的一次结构化自检。自检最多一次，不形成无界 LLM 循环；其输出仍须经过同一 Pydantic 与编译器校验。新的真实模型结果需重新运行后记录，不能用实现预期替代实测数据。
- 改进后的第三次 DeepSeek 实测结果：10/10 输出通过结构校验，5/5 条支持范围内规则全部成功编译，另外 5 条能力外、非法字段或语义模糊规则全部继续返回 `not_compilable`，没有误接受。本次仍为 compile-only，未发送 HTTP。`card-token-required` 在首次调用就正确生成 `set_value + omit_field`，因此本轮改善直接来自组合说明/few-shot；一次自检只对其余 5 条拒绝规则触发并维持原判断。自检提高保守决策的可复核性，但增加模型调用与时延，后续评测应单独记录初次召回、修复召回、误接受率和额外 token/时延。
- 第三次实验随后被认定存在评测泄漏：Prompt 直接给出了 `card-token-required` 的 mutation 组合，不能证明模型具备迁移能力。因此正式基准移除该具体 few-shot，并把规则替换为方向相反的新场景 `cash-card-token-forbidden`：“现金支付时主动提交 card_token 应返回 422”。Prompt 只保留“多个原子 mutation 可共同建立前置条件并构造违反”的抽象说明。
- 无直接答案的第四次 DeepSeek 实测中，新规则首次调用便自行生成 `set_value($.payment_method, "cash") + set_value($.card_token, <value>)`。整体结果仍为 10/10 结构合法、5/5 支持规则成功编译、5/5 不支持规则正确拒绝、0 误接受、0 误拒绝；compile-only 总计 15 次模型调用、5 次自检、0 次自检修复、模型调用耗时 17,266 ms。新的 fixture HTTP 验证为 5/5 通过。该单次小样本说明抽象组合提示具有初步迁移效果，但仍不能外推为稳定准确率。
- 已增加 Candidate Context Validator，确定性校验 `rule_id`、source、operationId、expected 状态码和字段路径没有偏离输入；结构合法但上下文不忠实的模型结果不会进入编译器。
- M0 评测分别记录正确接受/拒绝、误接受/拒绝和语义匹配；模型用量独立记录调用数、耗时、prompt/completion token 与结构修复次数。已删除关闭自检后失去意义的首次状态、自检次数和自检修复字段。
- M0 增加了独立 held-out 集合：3 个虚构 operation、16 条未出现在 Prompt 示例中的规则，按 8 条支持范围内和 8 条应拒绝规则平衡。覆盖中英文改写、日期/数字关系、必填字段、2 个 mutation 组合、相似联系方式字段、跨接口、请求头幂等、未知字段、未知 operation、缺少 expected oracle、嵌套路径、模糊概念和外部系统依赖。`expected_outcomes.json` 不进入模型上下文，并核对关键 mutation，而不只核对候选状态。
- held-out 初次运行暴露并修复了三项问题：日期字段错误使用 `delta.unit=number` 会导致编译器类型错误；拒绝不存在 operation 时允许候选清空 operationId；结构不合法输出需要一次有界修复。编译器与 Candidate Context Validator 现都会检查 relation 类型兼容，单条坏候选不再中断整轮。
- 修正后的 held-out 连续三轮结果均为 16/16 语义匹配、8/8 成功编译、8/8 正确拒绝、0 误接受、0 误拒绝。每轮均发生 1 次结构修复并成功，均发生 8 次 `not_compilable` 自检但修复数为 0。三轮合计 75 次模型调用、84,416 ms 模型调用耗时、157,373 prompt tokens 和 12,084 completion tokens。该结果说明结构修复在该样本中有直接价值，而全量 `not_compilable` 自检目前只有成本、没有观测到收益；进入 M1 前应考虑改为条件触发或暂时关闭。该结果仍是小型合成集，不作为通用准确率承诺。
- 根据上述三轮结果，当前关闭 `not_compilable` 自检并删除对应运行状态与指标字段，只保留一次有界结构修复。日志策略收敛为：LLM 输入/输出和最终 `[RESULT]` 使用 INFO；解析、fixture、编译、HTTP、单规则过程与结果落盘位置最多使用 DEBUG；重复的请求体、响应体和 mutation 逐项日志删除。敏感字段仍在任何日志输出前统一脱敏。
- M0 收尾时将 extractor 接口整理为显式 `ExtractionResult(candidate, telemetry)`，删除容易错配且不利于并发的 `last_trace` 可变旁路状态；Prompt 构建与安全日志视图位于 `generation/semantic_prompt.py`，DeepSeek 适配器只负责模型请求、Pydantic 校验和一次结构修复；运行指标、模型用量和 benchmark 评分分别建模，避免将评测字段混入 TestSpec 领域模型。
- 上述 DeepSeek 第二次验证只运行到结构校验与编译，没有提供 Base URL，因此准确表述是“4 条编译成功”，不是“4 条已执行”。5 条真实 HTTP 执行成功来自人工 fixture 路径，二者用于隔离评估不同组件，不能混作同一指标。
- `compiler_fixtures.json` 仅用于隔离测试 Pydantic、编译器和执行器，其中故意包含非法字段引用；它不是用户输入，也不是 DeepSeek 的预期答案或成功率依据。

### M1：确定性基线

- OpenAPI 解析和规范化；
- Schema 机械候选生成；
- HTTP 执行、安全策略和响应校验；
- Go 演示服务和可开关缺陷；
- 固定计划端到端运行；
- Markdown/JSON 报告和单元测试。

M1-A OpenAPI 解析当前实现状态（2026-09-26）：

- 已实现 OpenAPI 3.x YAML/JSON 加载、文档指纹和文档内 `$ref` 解析；
- 已实现 path/operation 参数合并、JSON 媒体类型选择、request/response/security scheme 规范化；
- 缺失 `operationId` 时生成稳定内部 ID 并警告，重复 ID、缺失引用和外部引用会明确报错，递归引用保留 `$ref` 并警告；
- 已增加 `api-doctor inspect --spec ...` 和 `examples/buggy-shop/openapi.yaml`；
- 该 Catalog 是后续 Schema 机械候选、LLM 语义上下文、TestSpec 编译和 Agent 工具查询的统一输入；M0 的 `operations.json` loader 仅作隔离 benchmark 兼容层保留。

M1-B Schema 机械候选当前实现状态（2026-10-04）：

- 已从 `OperationCatalog` 的顶层 JSON request body Schema 生成 happy path、required、数值边界、enum、format、字符串长度和数组约束候选；
- 规则引擎直接输出统一 `SemanticTestCandidate`，候选带 OpenAPI pointer 证据，不经过 LLM；
- TestSpec 增加 `use_baseline` mutation 表示正常路径使用确定性合法基线，不伪造字段修改；
- 负向候选只使用 OpenAPI 明确声明的 400/409/415/422 作为验证断言；无证据状态码时保留候选但标记 `not_compilable`；
- 当前边界是顶层 JSON request body；参数位置、嵌套 Schema、`oneOf/anyOf`、pattern 及合法值生成器在后续 M1 子阶段扩展。

M1-C 候选编译当前实现状态（2026-10-04）：

- 已实现基于 example/examples/default/enum/const/type/format/边界的确定性合法值生成器，支持嵌套 object、array、`allOf` 合并以及 `oneOf/anyOf` 的首个可选分支；
- 已实现 `compile_openapi_candidate`，直接使用 `OperationCatalog`，生成分离 path/query/header/cookie/body 的正式 `CompiledTestCase`；
- 编译前验证合法基线，编译后验证 happy path 仍合法，并确认 OpenAPI 负向 mutation 确实违反目标 request Schema；
- 无可靠生成策略的 `pattern` 字段会明确阻塞，需由 example/default/fixture 提供合法值，不猜测；
- M0 `compile_candidate` 仅作 benchmark 适配入口：将合成 operation 转为 `OperationCatalog` 后委托给正式 OpenAPI 编译器，不再维护第二套 mutation 编译逻辑。

M1-D 确定性执行基线当前实现状态（2026-10-04）：

- 已实现 base URL 同源限制、同源重定向复查、重定向次数、超时、响应大小、HTTP method 白名单和 DELETE 显式批准；
- 已实现状态码断言、OpenAPI response 选择、Content-Type 与 response JSON Schema 校验；
- 已实现固定顺序、`max_tests` 硬预算、单用例错误隔离和 passed/failed/error/blocked 统计；
- `api-doctor run` 输出脱敏的 `run.json`、`report.md` 和 `trajectory.jsonl`；
- 使用真实 Go 服务完成一次 21 请求端到端运行：19 passed、2 failed、0 error、0 blocked。失败为 email format 未被服务拒绝，以及 Schema 合法但不满足文字中时间先后关系的 order happy path；该数据仅对当前示例版本与本次 run 有效。

M1-E 演示服务扩展当前实现状态（2026-10-05）：

- Go 服务已按职责拆分为启动配置、线程安全内存 store 和 HTTP handler，支持用户创建/查询/删除与订单创建/查询；
- `/test/reset` 仅在 `BUGGY_SHOP_TEST_MODE=1` 时注册，不进入 OpenAPI，仍由用户手动调用；
- 默认为无缺陷参考行为；`BUGGY_SHOP_DEFECTS` 可显式开启 `invalid-email-accepted` 和 `unknown-user-200`，Agent 运行时不读取该配置；
- 正式 OpenAPI 已扩展为 5 个业务 operation，包含 path parameter、200/201/204/400/404/422 响应与完整请求/响应 Schema；
- Go 测试覆盖用户生命周期、订单生命周期、缺陷开关和 test-mode reset。跨接口变量提取与依赖执行留在下一子阶段，不写死演示服务 operationId。
- 真实对照运行中，默认无缺陷服务为 22/22 passed；开启两个缺陷后为 21 passed、1 failed，确定性 baseline 发现 `invalid-email-accepted`。`unknown-user-200` 尚未被当前只针对 request-body Schema 的候选触发，需要下一阶段的跨接口变量和不存在资源探针；两组数据仅对当前示例版本和对应 run 有效。

M1-F 参数与跨接口依赖当前实现状态（2026-10-05）：

- `OperationCatalog` 已解析 OpenAPI response Links，支持 `$response.body#/<pointer>` 形式的参数映射；
- Schema 规则已生成参数缺失、参数边界和 GET 不存在资源候选，编译器可将参数 mutation 物化为 path/query/header/cookie 具体值；
- 执行器从成功的上游响应提取变量，仅向 Links 指定的下游 happy path 注入，并在 `run.json` 保存生产者、消费者、表达式和值的来源；缺少变量时安全阻塞而不猜测；
- Links 的生产者会在消费者之前稳定执行，不依赖 OpenAPI paths 的书写顺序；循环依赖保持原始稳定顺序，缺少变量时仍按阻塞处理；
- 不存在资源值仅是高概率探针，不能单凭 OpenAPI 证明，因此证据类型标记为 `heuristic`，失败应解读为风险而非确定缺陷；
- M1 收尾重构后，M0 合成 benchmark 与正式 OpenAPI 候选共用同一编译器和 HTTP 执行器；JSON Schema 校验也使用同一适配层。M0 fixture、held-out 与 DeepSeek 指标仍作 LLM 回归资产保留，不进入正式 `run` 路径；
- 实际无缺陷对照运行为 30 candidates、27 compilable、26 executed、26 passed、4 blocked；两个缺陷开启时为 26 executed、24 passed、2 failed、0 error、4 blocked。四个 blocked 为三个缺少契约状态码依据的参数边界候选，以及未获 DELETE 显式批准的用例；数据仅对当前示例版本和对应保存 run 有效。

M1 完成后项目是“OpenAPI 确定性测试器 + LLM 语义 TestSpec 生成器”，具备 LLM 工程价值，但执行计划仍固定，不单独宣称已经形成完整 Agent。M1 的职责是为 M2 提供可信、可复用、受安全约束的工具层。

### M2：Agent 闭环

- DeepSeek 语义候选提取；
- TestSpec 编译器；
- LangGraph 状态循环；
- 跨接口变量提取与工作流；
- 失败后的诊断测试与局部 replan；
- 结构化轨迹和硬预算。

M2 是 Agent 简历价值的关键阶段：至少保存一条真实、非写死的轨迹，证明模型基于 HTTP Observation 形成或更新故障假设，选择受控诊断工具，新观察写回 Working Memory 后触发局部 replan，并由确定性预算和终止条件结束。LLM 调用次数不是目标；`state → decision → tool → observation → state` 的可审计闭环以及相对固定 baseline 的增益才是验收依据。

M2-A LLM Workflow 发现当前实现状态（2026-10-05）：

- 已实现 provider-independent `ModelGateway` 合约和 `DeepSeekGateway`，所有输出均先通过指定 Pydantic 模型校验，结构错误最多修复一次；
- M0 `DeepSeekExtractor` 已改为复用同一 Gateway，不再维护第二套模型 HTTP、telemetry 和结构修复逻辑；
- `build_catalog_context` 从 `OperationCatalog` 提取 operation 描述、参数、request/response Schema，明确不向模型提供 OpenAPI Links；
- 已实现受限 `WorkflowCandidate` DSL：2–4 个 step、响应 JSON Pointer 变量提取、path/query/header/cookie 参数绑定、置信度和歧义列表；结构层会拒绝重复 step/变量和先使用后提取的变量；
- `api-doctor discover-workflows` 只生成提议并记录模型用量，不发送任何被测 HTTP 请求，不把模型输出直接交给 executor；
- 一次真实 DeepSeek 验证中，模型在未看到 Links 时提出用户 `create → read → delete` 和订单 `create → read` 工作流，并指出 product_id、时间和 card_token 等无上游生产者的前置数据歧义。本次共 2 次模型调用，其中 1 次为结构修复，总模型耗时 6,902 ms、prompt 6,964 tokens、completion 1,602 tokens；数据仅对当前模型、Prompt、示例规范和本次保存运行有效；
- 该阶段仅证明“模型能提出结构化工作流”；其确定性语义校验与编译已在 M2-C 实现。工作流接入 Agent Runner 前仍不得执行。

M2-B1 LLM 语义测试发现当前实现状态（2026-10-05）：

- 已实现正式可选业务规则加载，`rules.yaml` 中每条规则包含稳定 id、operation 提示、自然语言描述和可选期望状态码；
- `api-doctor discover-tests` 将精简 Catalog、operation description、description 证据 pointer、用户规则和受支持 mutation 交给共享 `ModelGateway`，输出受 Pydantic 约束的 `SemanticDiscovery`；
- 当前语义 DSL 限定为顶层 request body 的 `set_value`、`omit_field` 和数字/日期 `field_relation`，允许多个 mutation 组合表达条件必填和条件禁止；
- 一次真实 DeepSeek 验证中，模型一次调用生成 11 个结构合法候选：包括时间先后、折扣与总额、cash 禁止 card token、card 条件必填；模型既从用户规则生成候选，也自主从 OpenAPI description 提出等价候选，并将当前 DSL 无法表达的 GET 404 场景标记为 `not_compilable`；
- 本次模型耗时 5,587 ms、prompt 4,347 tokens、completion 1,621 tokens，无结构修复；结果同时暴露用户规则与 description 会生成重复候选，且 description 对期望状态的证据强度需要代码复核，证明后续语义校验与候选融合是必要步骤；
- 当前候选只通过结构校验，不进入 executor。M2-B2 必须复核 source/rule/status、operation/字段引用、relation 类型和 mutation 组合；M2-B3 再与机械候选去重、合并 provenance 并报告冲突。

M2-B2 语义候选校验当前实现状态（2026-10-06）：

- 已将 Schema 证据 `openapi` 与自然语言证据 `openapi_description` 分开：前者的负向 mutation 必须违反 JSON Schema，后者应保持 Schema 合法并只违反文字业务约束；
- `api-doctor validate-tests` 读取已保存的 LLM discovery，不再次调用模型，也不发送 HTTP；它逐条校验 source 证据、业务规则、operation、期望状态码、顶层字段、Schema 合法性以及数字/日期 relation 类型；
- 每条候选独立审查，非法候选记录明确原因而不会中断整批；通过者委托正式 `compile_openapi_candidate` 生成具体 `CompiledTestCase`，验证器不维护第二套编译逻辑；
- 校验层只依赖统一候选模型和 `OperationCatalog`，不依赖 LLM discovery 封装，保持“生成 → 校验/编译”的单向依赖；
- 一次真实 DeepSeek 产物包含 11 条候选，确定性校验接受 11 条，其中 9 条成功编译，2 条 GET 404 场景作为有依据但当前 DSL 无法表达的 `not_compilable` 保留；本次校验未执行被测 HTTP。该数据只对当前模型、Prompt、示例规范和保存产物有效；
- 当前产物仍包含用户规则与 OpenAPI description 的语义重复。M2-B3 将进行候选融合、provenance 合并和冲突显式化，不以简单“全部保留”进入执行器。

M2-B3 候选融合当前实现状态（2026-10-06）：

- 已实现确定性 `FinalTestPlan`：同时保存可执行用例、冲突、校验拒绝项和当前 DSL 不支持项，不让未执行候选从审计记录中消失；
- 融合发生在候选通过校验并编译之后，以 operation、实际 HTTP 请求、依赖和风险计算稳定指纹，不依赖模型生成的 purpose、case id 等自然语言标签；因此不同 DSL 表达只要物化为相同请求，也可以确定性去重；
- 请求与期望状态码均相同时只执行一次，并为每个输入保留 `case_id + rule_id + assertion provenance`；`rule_id` 允许一条规则派生多个候选，不能被误当成唯一候选 id。规范契约来源优先作为展示用例，但不会覆盖或删除其他证据；
- 相同请求对应不同期望状态码时整组不进入执行集，生成 `expected_status_conflict` 并标记 `requires_confirmation`，不由 LLM 或代码擅自选择；
- `api-doctor fuse-tests` 会重新校验保存的语义 discovery、生成并编译机械候选，再输出融合计划；该命令不调用 LLM，也不发送 HTTP；
- 使用真实 DeepSeek 保存产物与 buggy-shop 机械候选融合：36 条已编译输入形成 32 条可执行用例，4 条重复被合并，0 个冲突；另保留 5 条暂不可编译候选，其中 3 条来自缺少契约状态依据的机械候选，2 条来自 DSL 无法表达的语义场景。本次数据只对当前保存产物、规范和固定 seed/时间锚点有效；
- 单元测试不依赖 LLM prompt 或模型答案，覆盖不同 DSL 生成相同请求、证据合并、冲突隔离、不同请求保留、输入顺序稳定、元数据不影响指纹，以及拒绝/不支持项的审计保留。

M2-C 工作流校验与编译当前实现状态（2026-10-06）：

- `api-doctor validate-workflows` 读取已保存的 LLM workflow discovery，不再次调用模型，也不发送 HTTP；每条工作流独立校验，错误提议不会阻断同批其他工作流；
- 校验器确认所有 operation 存在、变量只从声明了对应 JSON Pointer 的 2xx 响应 Schema 提取、绑定目标参数的位置与名称真实存在，并验证所有可能的提取类型均可赋给目标参数；
- 支持穿过 response Schema 的 `allOf/anyOf/oneOf` 和数组索引查找字段；编译结果按响应状态保存提取 Schema，并保存目标参数 Schema，使运行时可以用真实响应值再次验证，而不是只信任静态类型；
- 重复 workflow id、同一步骤重复绑定同一参数、不存在的成功响应字段、错误参数位置及类型不兼容均会被拒绝；DELETE 步骤只产生 `requires_approval` 风险标记，不能授予执行权限；
- 之前保存的真实 DeepSeek 产物中，用户 `create → read → delete` 和订单 `create → read` 两条工作流均通过确定性校验并编译；其中用户工作流因 DELETE 标记为需要批准。该验证不代表工作流已执行；真实变量提取与注入属于下一阶段 Agent Runner；
- 当前单元测试使用手工构造的正确及对抗性提议，不依赖 prompt 示例，覆盖合法生命周期、批内错误隔离、未知 operation、只存在于错误响应的字段、错误参数位置、类型不兼容、重复绑定和重复 workflow id。

M2-D 工作流运行时当前实现状态（2026-10-06）：

- `api-doctor run-workflow` 会针对当前 OpenAPI 重新校验保存的 workflow discovery，只允许选择通过校验的工作流；该命令自身不调用 LLM，但直接消费 LLM 发现产物；
- 每个 step 由正式 OpenAPI 编译器生成合法 baseline 请求，workflow binding 作为受控 parameter override 注入；请求仍通过同源 URL、重定向、超时、响应大小、method 和 DELETE 审批策略约束的同一 HTTP 工具执行；
- 只有通过状态码和完整 response Schema 校验的步骤才能提取变量；具体值先按对应响应状态的提取 Schema 校验，写入单次运行 Working Memory，注入下游前再按目标参数 Schema 校验；同一步的多个变量采用原子写入，任一提取失败不会留下部分状态；
- 步骤失败、编译错误、变量错误、预算耗尽或安全阻断会停止工作流后缀；未执行步骤保留明确 blocked 原因。DELETE 即使由 LLM 提议也必须由用户显式传入 `--approve-delete`；
- 运行产物包含脱敏 `run.json`、Markdown 报告和逐步 `trajectory.jsonl`，记录具体请求、Observation、变量来源和依赖 case id；
- 一次真实本地运行执行 DeepSeek 发现的 `createOrder → getOrder`：`createOrder` 返回 201 和 `id=1`，代码提取并校验 `order_id` 后注入 `getOrder.path.id`，实际请求 `/orders/1` 返回 200；2 步均通过，0 failed、0 error、0 blocked。该数据只对应当前保存 discovery、示例服务和本次运行；
- 这一步接通了“LLM 工作流发现 → 确定性校验/编译 → 真实 HTTP → 变量 Working Memory → 下游工具调用”，但尚不是完整 Agent 闭环；统一 Planner、语义测试计划执行、Observation 分析和 replan 仍待实现。

M2-E AgentState 与初始 Planner 当前实现状态（2026-10-06）：

- 已定义显式单次运行 `AgentState`，保存 phase、plan version、pending/completed actions、Observation、Hypothesis、请求预算、Agent step 预算、已执行指纹、DELETE 批准状态和终止原因；当前阶段只初始化状态，尚未执行统一队列；
- `api-doctor plan-agent` 将已融合的 `FinalTestPlan` 和已校验的 `CompiledWorkflow` 以精简 inventory 交给 DeepSeek；模型只能选择现有 `test_case/workflow item_id`、优先级、理由和估算请求数，不能生成 URL、请求体、operation 或断言；
- 初始规划本身消耗一个 Agent step。代码重新计算 workflow step 数和单用例成本，校验引用、成本、`max_tests` 与剩余 Agent step 预算；无效结构仍由 Gateway 最多修复一次，语义越权或超预算则不再次相信模型说明，而是启用确定性 fallback；
- 未显式批准 DELETE 时，危险工作流既不会发送给 Planner，也不会进入 fallback；批准状态写入 AgentState，但真正执行时仍需通过 HTTP safety policy 再检查一次；
- 单元测试使用 FakeGateway 和真实编译产物覆盖紧凑上下文、有效状态初始化、伪造 ID、成本谎报、预算溢出和未批准 DELETE 隔离，不在 prompt 中预写选择答案；
- 一次真实 DeepSeek Planner 在 8 请求、10 Agent step 预算下选择 1 个两步 `order_create_read` workflow 和 6 个可信单接口用例，总成本 8，引用与成本校验全部通过，无 fallback；调用 1 次、模型耗时 3,034 ms、prompt 2,827 tokens、completion 569 tokens。该数据仅对应当前保存 inventory、模型和本次运行；
- 开发验证中的另一次真实调用里，模型文字声称成本为 8、结构化选择实际成本为 9，确定性校验成功拒绝并触发 fallback；该次临时产物后来被最终验证覆盖，因此只作为开发观察，不作为保存运行指标；
- 下一阶段把 pending actions 接入统一执行循环，每个真实 HTTP Observation 更新状态并消耗预算；之后才加入失败触发的 Analyzer 与局部 replan。

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
