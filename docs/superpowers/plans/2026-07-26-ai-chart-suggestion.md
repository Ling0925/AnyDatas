# AI Chart Suggestion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the AI Agent recommend a chart (from the 7 existing chart types) alongside its candidate SQL; the user previews it in chat and, on "应用并运行", the workbench renders the full result as that chart.

**Architecture:** The model co-emits a fenced ```chart JSON block in the same reply as its ```sql block. The Rust backend extracts and validates it into a `ChartSpec` (whitelisted type/aggregation, column names by string), persists it on the assistant message, and returns it in the message DTO. The Vue frontend renders a chart thumbnail on the candidate message and, on apply, feeds the spec (column-names → indices) into the existing `ResultChart`.

**Tech Stack:** Rust (Axum, SQLx, serde_json), Vue 3 + TypeScript, ECharts (existing ResultChart), Pinia.

## Global Constraints

- Chart types (exact): `bar`, `stacked-bar`, `line`, `area`, `pie`, `scatter`, `radar`.
- Aggregations (exact): `sum`, `average`, `max`, `min`.
- `values`: 1–4 items; `groups`: 0–1 effective; `title` ≤120 chars; `rationale` ≤240 chars.
- Chart spec references result columns by **name** (never index). Frontend maps names→indices.
- No free-form ECharts option anywhere. Invalid/missing chart spec must never affect SQL, reply, or execution — degrade silently.
- Rust gates: `cargo fmt --all --check`, `cargo clippy --all-targets --locked -- -D warnings`, `cargo test --locked` (run from `backend/`).
- Frontend has no unit-test runner; verification is `vue-tsc` + `vite build` (`npm run build` in `frontend/`) plus manual walkthrough.

---

## File Structure

- `backend/src/services/agent.rs` — `ChartSpec`, `validate_chart_spec`, `split_reply_sql_and_chart`, persist + DTO wiring, system-prompt line, tests.
- `backend/migrations/0009_ai_message_chart.sql` — add `chart_spec_json` column.
- `backend/src/models.rs` (or wherever `AiAgentMessage` DTO lives) — `chart` field.
- `frontend/src/types.ts` — `AgentChartSpec` + message field.
- `frontend/src/stores/workspace.ts` — `appliedChart` state.
- `frontend/src/components/ResultChart.vue` — `appliedConfig` prop + name→index mapping.
- `frontend/src/components/AiChartPreview.vue` — new thumbnail.
- `frontend/src/components/AiAssistantPanel.vue` — render preview + carry chart on apply/run.
- `frontend/src/views/AgentView.vue`, `frontend/src/views/WorkbenchView.vue` — wiring.

---

### Task 1: `ChartSpec` type + `validate_chart_spec`

**Files:**
- Modify: `backend/src/services/agent.rs` (add struct + fn near other agent types; tests in the existing `#[cfg(test)] mod tests`)

**Interfaces:**
- Produces: `struct ChartSpec { chart_type: String, category: String, values: Vec<String>, groups: Vec<String>, aggregation: String, title: Option<String>, rationale: Option<String> }` with `#[derive(Debug, Clone, Serialize, Deserialize)]` and `#[serde(rename_all = "camelCase")]`; the JSON key for `chart_type` is `type` via `#[serde(rename = "type")]`. `fn validate_chart_spec(spec: ChartSpec) -> Option<ChartSpec>` returns the normalized spec or `None` when invalid.

- [ ] **Step 1: Write the failing tests** (append to `mod tests`)

```rust
#[test]
fn validates_and_normalizes_chart_spec() {
    let ok = validate_chart_spec(ChartSpec {
        chart_type: "bar".into(), category: "月份".into(),
        values: vec!["销售额".into(), "利润".into()],
        groups: vec!["区域".into(), "多余".into()],
        aggregation: "sum".into(), title: None, rationale: None,
    }).unwrap();
    assert_eq!(ok.groups.len(), 1); // truncated to 1
    assert_eq!(ok.values.len(), 2);

    // unknown type -> None
    assert!(validate_chart_spec(ChartSpec {
        chart_type: "bubble".into(), category: "a".into(), values: vec!["b".into()],
        groups: vec![], aggregation: "sum".into(), title: None, rationale: None,
    }).is_none());
    // empty values -> None
    assert!(validate_chart_spec(ChartSpec {
        chart_type: "bar".into(), category: "a".into(), values: vec![],
        groups: vec![], aggregation: "sum".into(), title: None, rationale: None,
    }).is_none());
    // >4 values -> None
    assert!(validate_chart_spec(ChartSpec {
        chart_type: "bar".into(), category: "a".into(),
        values: (0..5).map(|i| i.to_string()).collect(),
        groups: vec![], aggregation: "sum".into(), title: None, rationale: None,
    }).is_none());
    // bad aggregation normalizes to sum
    let n = validate_chart_spec(ChartSpec {
        chart_type: "pie".into(), category: "a".into(), values: vec!["b".into()],
        groups: vec![], aggregation: "median".into(), title: None, rationale: None,
    }).unwrap();
    assert_eq!(n.aggregation, "sum");
}
```

- [ ] **Step 2: Run to verify failure** — `cd backend && cargo test --locked validates_and_normalizes_chart_spec` → FAIL (unresolved `ChartSpec`/`validate_chart_spec`).

- [ ] **Step 3: Implement**

```rust
const CHART_TYPES: [&str; 7] = ["bar", "stacked-bar", "line", "area", "pie", "scatter", "radar"];
const CHART_AGGREGATIONS: [&str; 4] = ["sum", "average", "max", "min"];

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ChartSpec {
    #[serde(rename = "type")]
    pub chart_type: String,
    pub category: String,
    #[serde(default)]
    pub values: Vec<String>,
    #[serde(default)]
    pub groups: Vec<String>,
    #[serde(default)]
    pub aggregation: String,
    #[serde(default)]
    pub title: Option<String>,
    #[serde(default)]
    pub rationale: Option<String>,
}

fn truncate_chars_opt(value: Option<String>, max: usize) -> Option<String> {
    value.map(|text| text.chars().take(max).collect())
}

/// 返回归一化后的图表配置，任何白名单/条数不满足则返回 None（静默丢弃，不影响 SQL/回复）。
fn validate_chart_spec(spec: ChartSpec) -> Option<ChartSpec> {
    if !CHART_TYPES.contains(&spec.chart_type.as_str()) {
        return None;
    }
    if spec.category.trim().is_empty() {
        return None;
    }
    let values: Vec<String> = spec.values.into_iter().filter(|v| !v.trim().is_empty()).collect();
    if values.is_empty() || values.len() > 4 {
        return None;
    }
    let aggregation = if CHART_AGGREGATIONS.contains(&spec.aggregation.as_str()) {
        spec.aggregation
    } else {
        "sum".to_owned()
    };
    let groups: Vec<String> = spec.groups.into_iter().filter(|g| !g.trim().is_empty()).take(1).collect();
    Some(ChartSpec {
        chart_type: spec.chart_type,
        category: spec.category,
        values,
        groups,
        aggregation,
        title: truncate_chars_opt(spec.title, 120),
        rationale: truncate_chars_opt(spec.rationale, 240),
    })
}
```

- [ ] **Step 4: Run tests** — `cargo test --locked validates_and_normalizes_chart_spec` → PASS.
- [ ] **Step 5: Commit** — `git add backend/src/services/agent.rs && git commit -m "feat(agent): ChartSpec type and validation"`

---

### Task 2: Extract ```chart from the model reply

**Files:**
- Modify: `backend/src/services/agent.rs` — rename/extend `split_reply_and_sql` → `split_reply_sql_and_chart` (keep behavior for sql; also strip+parse a ```chart block). Update the single caller (~line 1022).

**Interfaces:**
- Produces: `fn split_reply_sql_and_chart(content: &str) -> (String, Option<String>, Option<ChartSpec>)` — reply text with both fences removed, candidate sql (unchanged semantics), and a validated chart spec.
- Consumes: `validate_chart_spec` (Task 1).

- [ ] **Step 1: Write failing tests**

```rust
#[test]
fn extracts_chart_block_alongside_sql() {
    let (reply, sql, chart) = split_reply_sql_and_chart(
        "先看结果：\n```sql\nSELECT 月份, SUM(额) AS 额 FROM data GROUP BY 月份\n```\n```chart\n{\"type\":\"bar\",\"category\":\"月份\",\"values\":[\"额\"],\"aggregation\":\"sum\"}\n```",
    );
    assert!(sql.is_some());
    let chart = chart.expect("chart parsed");
    assert_eq!(chart.chart_type, "bar");
    assert_eq!(chart.category, "月份");
    assert!(!reply.contains("```")); // both fences stripped
}

#[test]
fn ignores_invalid_or_missing_chart_block() {
    // invalid chart json -> chart None, sql still extracted
    let (_r, sql, chart) = split_reply_sql_and_chart(
        "```sql\nSELECT 1\n```\n```chart\n{not json}\n```",
    );
    assert!(sql.is_some());
    assert!(chart.is_none());
    // no chart block -> None, unchanged behavior
    let (_r2, sql2, chart2) = split_reply_sql_and_chart("```sql\nSELECT 1\n```");
    assert!(sql2.is_some());
    assert!(chart2.is_none());
}
```

- [ ] **Step 2: Run to verify failure** — `cargo test --locked chart_block` → FAIL.

- [ ] **Step 3: Implement** — add a helper that finds a ```chart fence, parses JSON → `ChartSpec` → `validate_chart_spec`, and removes that fence from the reply. Compose it with the existing sql extraction:

```rust
fn extract_chart_block(content: &str) -> (String, Option<ChartSpec>) {
    // find ```chart ... ``` ; parse+validate; strip from text.
    let mut cursor = 0usize;
    while let Some(rel) = content[cursor..].find("```") {
        let fence_start = cursor + rel;
        let lang_start = fence_start + 3;
        let Some(rel_body) = content[lang_start..].find('\n') else { break; };
        let body_start = lang_start + rel_body + 1;
        let language = content[lang_start..body_start - 1].trim().to_ascii_lowercase();
        let Some(rel_end) = content[body_start..].find("```") else { break; };
        let fence_end = body_start + rel_end;
        if language == "chart" {
            let body = content[body_start..fence_end].trim();
            let parsed = serde_json::from_str::<ChartSpec>(body).ok().and_then(validate_chart_spec);
            let reply = format!("{}{}", &content[..fence_start], &content[fence_end + 3..]);
            return (reply.trim().to_owned(), parsed);
        }
        cursor = fence_end + 3;
    }
    (content.to_owned(), None)
}

fn split_reply_sql_and_chart(content: &str) -> (String, Option<String>, Option<ChartSpec>) {
    let (reply, sql) = split_reply_and_sql(content);   // keep existing sql logic intact
    let (reply, chart) = extract_chart_block(&reply);
    (reply, sql, chart)
}
```

Update the caller (~1022) from `let (message, sql) = split_reply_and_sql(&content);` to `let (message, sql, chart) = split_reply_sql_and_chart(&content);` and thread `chart` to the insert (Task 3). Keep `split_reply_and_sql` as-is (reused + still unit-tested).

- [ ] **Step 4: Run tests** — `cargo test --locked chart` → PASS (new + existing `extracts_safe_sql_from_final_reply` still green).
- [ ] **Step 5: Commit** — `git commit -am "feat(agent): parse chart block from model reply"`

---

### Task 3: Persist + expose chart spec

**Files:**
- Create: `backend/migrations/0009_ai_message_chart.sql`
- Modify: `backend/src/services/agent.rs` — assistant-message INSERT (~1022 write path and ~2170), `AiMessageRow` (~219/364/1687 SELECT), DTO conversion (~2390).

**Interfaces:**
- Produces: `AiAgentMessage.chart: Option<ChartSpec>` serialized as `chart` (camelCase). DB column `ai_messages.chart_spec_json TEXT` (nullable).
- Consumes: `ChartSpec` (Task 1), `chart` from Task 2.

- [ ] **Step 1: Migration**

```sql
-- backend/migrations/0009_ai_message_chart.sql
ALTER TABLE ai_messages ADD COLUMN chart_spec_json TEXT;
```

- [ ] **Step 2: Row + SELECT** — add `chart_spec_json: Option<String>` to `AiMessageRow`; add `chart_spec_json` to every `SELECT ... FROM ai_messages` used to build messages (the two message-loading queries).

- [ ] **Step 3: INSERT** — when writing the assistant message, bind `chart.as_ref().map(|c| serde_json::to_string(c)).transpose()?` into the new column; add `chart_spec_json` to the column list + one `?`.

- [ ] **Step 4: DTO** — in the row→`AiAgentMessage` conversion (~2390), set `chart: row.chart_spec_json.as_deref().and_then(|s| serde_json::from_str::<ChartSpec>(s).ok())`. Add `pub chart: Option<ChartSpec>` to `AiAgentMessage` (with `#[serde(skip_serializing_if = "Option::is_none")]`).

- [ ] **Step 5: Extend the existing run test** — in `persists_a_complete_native_tool_run` (or add a focused `#[tokio::test]`), seed a reply containing a chart block and assert the reloaded conversation's assistant message has `chart.is_some()` with `chart_type == "bar"`. (Reuse `seeded_agent_state` + the mock model server pattern.)

- [ ] **Step 6: Run** — `cargo test --locked` (all backend tests) + `cargo clippy --all-targets --locked -- -D warnings` + `cargo fmt --all` → all green.
- [ ] **Step 7: Commit** — `git commit -am "feat(agent): persist and expose chart suggestion on messages"`

---

### Task 4: Instruct the model to emit charts

**Files:**
- Modify: `backend/src/services/agent.rs` — the system-prompt / rules builder (grep for the fixed system rules string).

- [ ] **Step 1** — Add one rule line to the system prompt, only meaningful when data tools are available: instruct that, when the result is suitable for visualization, append after the ```sql block a ```chart block containing `{type, category, values, aggregation, groups?, title?, rationale?}`, using **only the SQL's output column names**, `type` from the 7 allowed, `values` 1–4; if unsure, omit the chart. State the chart block is a suggestion and is never executed.
- [ ] **Step 2** — `cargo test --locked` (context/prompt tests still pass) + fmt/clippy.
- [ ] **Step 3: Commit** — `git commit -am "feat(agent): prompt the model to suggest a chart with its SQL"`

---

### Task 5: Frontend types

**Files:**
- Modify: `frontend/src/types.ts`

- [ ] **Step 1** — Add:

```ts
export type AgentChartType = 'bar' | 'stacked-bar' | 'line' | 'area' | 'pie' | 'scatter' | 'radar'
export type AgentChartAggregation = 'sum' | 'average' | 'max' | 'min'
export interface AgentChartSpec {
  type: AgentChartType
  category: string
  values: string[]
  groups?: string[]
  aggregation?: AgentChartAggregation
  title?: string
  rationale?: string
}
```
Add `chart?: AgentChartSpec` to the `AiAgentMessage` interface.

- [ ] **Step 2** — `cd frontend && npm run build` → passes.
- [ ] **Step 3: Commit** — `git commit -am "feat(ui): AgentChartSpec type"`

---

### Task 6: Store `appliedChart`

**Files:**
- Modify: `frontend/src/stores/workspace.ts`

- [ ] **Step 1** — Add `const appliedChart = ref<AgentChartSpec | null>(null)` and `function setAppliedChart(spec: AgentChartSpec | null) { appliedChart.value = spec }`; export both. In `selectSavedQuery` and `insertFormula` (manual context changes) set `appliedChart.value = null`. Do NOT clear it inside `runQuery` (so an AI-applied chart survives the run it triggers).
- [ ] **Step 2** — `npm run build` → passes.
- [ ] **Step 3: Commit** — `git commit -am "feat(ui): workspace appliedChart state"`

---

### Task 7: `ResultChart` accepts an applied config

**Files:**
- Modify: `frontend/src/components/ResultChart.vue`

**Interfaces:**
- Consumes: `AgentChartSpec` (Task 5). Column names map to `props.columns[].name`.

- [ ] **Step 1** — Add optional prop `appliedConfig?: AgentChartSpec | null`. Add a helper `nameToIndex(name) => props.columns.findIndex(c => c.name === name)`. Add a `watch(() => props.appliedConfig, applyExternalConfig, { immediate: true })` that, when a spec is present:
  - sets `chartType.value = spec.type`;
  - `categoryIndex.value = max(0, nameToIndex(spec.category))`;
  - `valueIndexes.value = spec.values.map(nameToIndex).filter(i => i >= 0).slice(0, 4)` — if empty, keep the existing default inference;
  - `groupIndexes.value = (spec.groups ?? []).map(nameToIndex).filter(i => i >= 0)`;
  - `aggregation.value = spec.aggregation ?? 'sum'`.
  If `category` and all `values` fail to map, do nothing (keep existing default behavior). Controls remain user-editable.
- [ ] **Step 2** — `npm run build` → passes (type-check confirms prop wiring). Manual: with a matching spec the chart renders as specified; with a non-matching spec it falls back.
- [ ] **Step 3: Commit** — `git commit -am "feat(ui): ResultChart applies an external chart config by column name"`

---

### Task 8: `AiChartPreview` thumbnail

**Files:**
- Create: `frontend/src/components/AiChartPreview.vue`

**Interfaces:**
- Props: `spec: AgentChartSpec`, `result: QueryResponse`. Renders a compact chart if columns map, else a one-line textual suggestion.

- [ ] **Step 1** — Implement by reusing `ResultChart` with `:columns="result.columns" :rows="result.rows" :applied-config="spec"` inside a fixed-height wrapper, hiding ResultChart's own control bar via a `compact` prop on ResultChart (add a `compact?: boolean` prop to ResultChart that hides `.chart-controls` when true — small addition folded here). If `spec.category` and none of `spec.values` exist in `result.columns`, render instead: `图表建议：{typeLabel} · 维度 {category} · 度量 {values.join('、')}`.
- [ ] **Step 2** — `npm run build` → passes.
- [ ] **Step 3: Commit** — `git commit -am "feat(ui): AiChartPreview thumbnail"`

---

### Task 9: Render preview + carry chart in AiAssistantPanel

**Files:**
- Modify: `frontend/src/components/AiAssistantPanel.vue`

**Interfaces:**
- Emits: change `apply-sql` / `run-sql` events to also pass the chart: `emit('apply-sql', { sql, chart })` (define a payload type) — OR add a second arg. Choose an object payload `{ sql: string; chart?: AgentChartSpec }` and update `defineEmits`.

- [ ] **Step 1** — Under the SQL-proposal block, when `message.chart`, render `<AiChartPreview :spec="message.chart" :result="messagePreview(message)!" v-if="messagePreview(message)" />` (only once a preview sample exists). Update `applySql`/`runSql` to accept the message and emit `{ sql, chart: message.chart }`.
- [ ] **Step 2** — `npm run build` → passes.
- [ ] **Step 3: Commit** — `git commit -am "feat(ui): show AI chart preview and carry it on apply"`

---

### Task 10: Wire AgentView + WorkbenchView

**Files:**
- Modify: `frontend/src/views/AgentView.vue`, `frontend/src/views/WorkbenchView.vue`

- [ ] **Step 1 (AgentView)** — `applyAgentSql`/`runAgentSql` accept `{ sql, chart }`; call `store.setAppliedChart(chart ?? null)` before setting `currentSql` / running.
- [ ] **Step 2 (WorkbenchView)** — pass `:applied-config="store.appliedChart"` to `<ResultChart>`; when `store.appliedChart` is set after a run, default `resultMode` to `'chart'`.
- [ ] **Step 3** — `npm run build` → passes.
- [ ] **Step 4: Commit** — `git commit -am "feat(ui): apply AI-suggested chart into the workbench result"`

---

### Task 11: Verify end-to-end + deploy

- [ ] **Step 1** — Backend gates: `cd backend && cargo fmt --all --check && cargo clippy --all-targets --locked -- -D warnings && cargo test --locked` → all green.
- [ ] **Step 2** — Frontend: `cd frontend && npm run build` → green.
- [ ] **Step 3** — Manual/browser: in `/agent`, a chart-suitable question yields a ```chart suggestion; the thumbnail renders; "应用并运行" opens the workbench with the result shown as the suggested chart; user can still hand-tune. Invalid/missing chart never breaks SQL or chat.
- [ ] **Step 4** — Deploy to 192.168.8.108 via the established safe flow (backup → sync changed files → rollback tag → build → tag → up → verify health/functional). Note: this task touches the backend (migration + agent), so the Rust layer rebuilds (slower than frontend-only).
- [ ] **Step 5: Commit** any doc updates; mark plan complete.

---

## Self-Review

- **Spec coverage:** ChartSpec/validation (T1), reply extraction (T2), persistence+DTO (T3), prompt (T4), types (T5), store (T6), ResultChart mapping (T7), thumbnail (T8), panel preview+carry (T9), workbench wiring (T10), verify+deploy (T11). All spec sections covered.
- **Placeholder scan:** none — each step has concrete code or an exact action + command.
- **Type consistency:** `ChartSpec` (Rust, `type`/`category`/`values`/`groups`/`aggregation`/`title`/`rationale`) mirrors `AgentChartSpec` (TS). `appliedConfig` prop name used consistently in T7/T8/T10. `setAppliedChart`/`appliedChart` consistent T6/T10. Emit payload `{ sql, chart }` consistent T9/T10.
- **Known follow-ups:** ResultChart `compact` prop is introduced in T8 (folded into that task). Frontend has no unit-test runner, so frontend tasks verify via `vue-tsc`+build+manual (documented in Global Constraints).
