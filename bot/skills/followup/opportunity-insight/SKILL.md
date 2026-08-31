---
name: opportunity-insight
description: Synthesize evidence already gathered by a completed EC/BET report (or a completed analysis-drill bundle) into a small set of grounded, clearly-labeled growth-opportunity suggestions, answering questions like "找出增长机会点" or "给出行动方案". Use when the user asks what to do next, where the opportunity is, or how to grow — not when they ask what happened (that is analysis-drill's job).
---

# Opportunity Insight

This Skill does not query new dimensions. It runs after `analysis_drill`/the
default EC or BET chain has already produced evidence, and its only job is to
turn that evidence plus the user's own question into a short list of
suggestions a business user can act on. It exists because every other Skill
in this project is descriptive (what happened) — this is the one place the
system is allowed to be prescriptive (what might be worth trying), and that
extra latitude needs its own explicit boundary.

1. Never invent a candidate. Only reason over rows the evidence bundle
   already contains (categories, key drivers, series, SKUs, and — if
   present — the same-period BET signals). If the evidence bundle is empty
   or the report failed, say there is not enough evidence instead of
   guessing.
2. A row only qualifies as an opportunity candidate if it clears the
   thresholds in `contract.json::candidate_rules` (share large enough to not
   be noise, and either growing share or growing YoY). Do not promote a row
   that fails both conditions just because it sounds interesting.
3. Every suggestion must name the specific number and table it is grounded
   on (e.g. "该系列份额同比提升6pp，GMV占比12%"). A suggestion with no
   attached number is not a suggestion — it is speculation and must be
   dropped.
4. State suggestions as hypotheses, not conclusions: "值得关注"、"可以考虑"、
   "建议优先配置资源到" — never "会带来增长"、"能提升GMV"、"证明了...策略有效".
   The forbidden-causal-word list in `bot/data/narrative_config.json` and
   this Skill's own `contract.json::forbidden_causal_words` both apply.
5. Internal KPI movement (share, YoY, growth contribution) can establish
   scale, direction, and momentum. It cannot establish why something is
   growing, whether a media investment caused it, or how a competitor,
   audience segment, or industry trend factors in. When the user's question
   implies one of those (e.g. "为什么增长"、"是不是因为投放"), say plainly that
   the current data does not support that claim instead of fabricating one.
6. Do not repeat `analysis_drill`'s descriptive bullets. If a fact was
   already stated as "what happened", this Skill's job is to add "so what
   might be worth doing about it", not to restate the fact.
7. Cap suggestions at `contract.json::output.max_bullets`. Fewer, well-
   grounded suggestions beat a padded list; do not manufacture a suggestion
   just to fill the quota.
8. If the evidence bundle includes BET signals alongside EC evidence, a
   suggestion may note that spend and share moved together, but must not
   claim the spend caused the share change (same invariant as
   `analysis-drill/SKILL.md` rule 10).
9. When series data is involved, keep the standard disclaimer: `产品系列由
   AI根据产品链接归纳总结，存在误差。`
