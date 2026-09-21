# Router V2

Router V2 uses one structured decision per user turn:

```text
deterministic entity extraction
→ semantic intent/question-mode planning
→ Capability Registry validation
→ execute / one clarification / unsupported-scope response
```

The structured contract and registry live in `bot/routing_contracts.py`. The
deterministic ontology and Douyin dual-identity rules live in
`bot/router_v2.py`. `RouteResult` remains the compatibility adapter consumed by
existing chains.

## Rollout

- `ROUTER_V2_ENABLED=0`: V1 executes (rollback/default during shadow).
- `ROUTER_V2_SHADOW=1`: V2 decisions are logged as `router_v2_shadow` but do
  not affect execution.
- `ROUTER_V2_ENABLED=1`: V2 handles recognized routes and V1 remains the
  compatibility fallback for routes not yet represented by the V2 contract.
- `ENTITY_RESOLVER_V2_SHADOW=1`: extract brand/time spans and log V1/V2
  differences without changing the route.
- `ENTITY_RESOLVER_V2_ENABLED=1`: Router V2 consumes the validated entity
  result. This flag only affects execution when Router V2 is enabled.

Shadow mode should run for at least five working days or 100 real requests.
Review action, brand, period, commerce platform, media mode/channel, market
scope, and clarification differences before enabling V2.

## Entity resolution

`bot/entity_resolution.py` is the single owner of brand and time extraction.
It preserves source offsets, validates calendar dates, assigns focus/comparison
roles, and returns a string-compatible `ResolvedPeriod` carrying exact start
and end dates. EC and BET period parsers consume those dates directly instead
of interpreting the user string again.

Yearless months, date ranges, and quarters default to the current system year;
the trace records `year_source=default_current_year` instead of creating a
confirmation turn. Campaign names such as 618, 520, and Double 11 still require
a user-provided date window, while the year defaults to the current system year.
Brand aliases are selected only from the local brand reference; fuzzy matching
can offer up to three candidates but cannot execute without confirmation.

## Non-causal boundary

BET reports describe observed changes and may use Tmall as the primary EC
business reference for overall BET. They must not state that BET caused a GMV
change or infer unobserved company actions. Strategy questions therefore carry
`causal_attribution` and `company_action_inference` as unsupported requests in
the route decision while still allowing observable data decomposition.
