# Controlled Agent Plan Layer runtime

## Rollout switches

```dotenv
AGENT_PLAN_LAYER_ENABLED=0
AGENT_PLAN_LAYER_SHADOW=1
AGENT_PLAN_MAX_STEPS=8
BOT_SMART_PROGRESS_ENABLED=1
EXECUTABLE_PLAN_V2_ENABLED=0
EXECUTABLE_PLAN_V2_SHADOW=1
EXECUTABLE_PLAN_DERIVE_ENABLED=0
EXECUTABLE_PLAN_GENERATED_ENABLED=0
UNIFIED_RESPONSE_STRATEGY_ENABLED=0
DYNAMIC_META_CAPABILITY_ENABLED=0
```

- Shadow compiles and validates plans, then writes `[agent_plan_shadow]` traces without changing execution or progress.
- Enabled writes `[agent_plan]` traces, attaches the plan to `result.meta.agent_plan`, and emits plan-aware progress.
- Smart progress is independent. Set it to `0` to restore the legacy generic Feishu wording.
- Existing Router and Entity Resolver switches still control entity and route rollout separately.
- Unified response strategy removes the user-facing report/follow-up split while
  retaining the existing route types as execution adapters. Dynamic Meta answers
  are generated from the capability registry, runtime flags, and latest report
  evidence; they never invoke business-data tools.

`EXECUTABLE_PLAN_V2_ENABLED=1` and `EXECUTABLE_PLAN_DERIVE_ENABLED=1`
activate the fixed `rank_then_drill` recipe. If compilation, validation, or a
required execution step fails, the request falls back to the V1 direct runner.
Generated plans remain disabled until their shadow traces are reviewed.

## V1 execution boundary

The plan compiler can only select registered executors. It validates step count,
unique IDs, dependency references, cycles, and executor names. Existing tested
chains remain the execution adapters; an LLM cannot create SQL, tools, brands,
periods, or unregistered actions.

Registered recipes include:

- fixed Tmall, Douyin, JD, three-platform, and BET report templates;
- EC + BET parallel branches followed by evidence synthesis;
- market Top-brand ranking followed by platform/product drill-down and synthesis;
- market Top-brand ranking followed by platform/product drill-down, then a
  controlled per-brand BET fan-out capped to each brand's latest available month;
- follow-up Skill planning followed by a registered Tool bundle and synthesis.

The complete original question is preserved across an explicit preflight reply,
so replying `确认` does not erase requested dimensions such as platform,
assortment, cadence, price, or promotion.

## V2 rank-then-drill execution

V2 executes the validated plan rather than using it only for progress copy:

1. query the requested Top brands;
2. build a brand-by-platform matrix from the same covered data;
3. run the allow-listed `per_group_argmax` derive operator;
4. pass each selected brand/platform pair to the matching existing template;
5. optionally fan out BET per selected brand;
6. synthesize one report and persist the active plan context for follow-ups.

Comparison wording maps deterministically to fields: scale uses `gmv_actual`,
absolute growth uses `gmv_growth`, growth rate uses `evol`, and contribution uses
`growth_contribution`. Missing values never participate as zero. The fan-out is
capped at five brands, and failed optional branches are reported without stopping
successful branches.

## Feishu message behavior

- Before routing: neutral acknowledgement (`先理解问题`), never an assumption that data is being queried.
- Data template: report-specific scope/check message, followed by existing chain progress.
- Complex recipe: explains the decomposition without exposing implementation details.
- Meta/guide/caliber: no data-query progress; completion says the answer was organized.
- Clarification: completion explicitly asks the user to confirm a scope.
- Unsupported scope: reports the checked data boundary rather than pretending to run an analysis.

## Verification

```bash
# Canonical entrypoint. It isolates unit tests from production .env rollout flags.
python tests/run.py

ROUTER_V2_ENABLED=1 \
ENTITY_RESOLVER_V2_ENABLED=1 \
AGENT_PLAN_LAYER_ENABLED=1 \
AGENT_PLAN_LAYER_SHADOW=0 \
EXECUTABLE_PLAN_V2_ENABLED=1 \
EXECUTABLE_PLAN_DERIVE_ENABLED=1 \
python tests/run.py \
  tests.test_entity_resolution_v2 \
  tests.test_router_v2 \
  tests.test_followup_v2 \
  tests.test_douyin_business_analysis \
  tests.test_jd_business_analysis \
  tests.test_agent_plan \
  tests.test_executable_plan
```
