# 灵活问数开发指导与验收基准

版本：v1.0，2026-09-21。状态：开发目标已记录，功能尚未据此验收。

本文件是本轮每天开工的主入口。排期以用户 9/21 提供的两张修改后截图为准；技术目标继承当天讨论的四层决策架构。旧架构建议中的“首批 10–20 个指标”不再作为交付范围。

## 1. 每天怎么使用

每天先读本文件和 [开发记录](flexible_query_worklog.md)，再查看当天相关代码与测试。先说明当天任务、前置条件、预计交付，再开始改代码。

- 所有任务用本文件的编号追踪；完成必须有代码位置、测试结果和遗留问题，不能只记录“已完成”。
- 开工先检查工作树和测试基线。当前仓库已有大量修改及未跟踪文件，不能覆盖或清理；本地版本不等于生产版本。
- 当天结束更新开发记录，写清下一天从哪里继续。未完成项保留，不因日期过去自动勾选。
- 若口径、范围或排期发生变化，先更新本文件的变更记录，再更新相关任务；不得为了赶进度静默缩成 8–10 个指标。
- 此文中的接口是目标合同，建议新增文件名不是已实现事实；可复用现有模块，但行为和验收不能缩水。
- 本轮交付测试环境可用版本。生产发布是另一个动作，不随“9/30 验收”自动执行。

建议每天给 Codex 的指令：

> 请先读 docs/flexible_query_implementation_guide.md 和 docs/flexible_query_worklog.md，核对当前代码和未完成项，执行今天的开发任务，按对应验收标准测试，并更新开发记录。

## 2. 目标与范围

用户可以在授权数据范围内，自由组合已定义指标、品牌、平台、期间、筛选、分组、排名和比较；Bot 按确认过的业务口径取数，回答数字并支持连续追问。

保留五份分析模板：天猫、京东、抖音、三平台、BET，以及现有简单回答和已可用的问数/追问能力。不要让单个数字问题误触发完整报告，也不要让新增问数吞掉原有报告能力。

全部 Obsidian《生意数据》指标均纳入知识和测试清单，当前基线为 22 张指标卡、27 张表卡，最终以准备阶段冻结的清单为准。每项分别记录知识是否整理、代码是否支持、真实数据是否验收。数据源存在缺陷可以标注受限；代码未实现不能包装为“数据受限”通过验收。

第一期使用一个对话入口和现有 worker，不增加子 Agent 或专门写代码的 Agent。Python/Pandas 用于已注册的计算算子；本轮不让模型任意运行 Python 脚本。SQL Tool 支持结构化查询组合，优先受控编译，任意模型 SQL 的灰度执行不在本轮默认范围。

## 3. 确认后的排期

|日期 / 任务编号|当天开发内容|当天完成的标准|
|---|---|---|
|9/21 · D21 准备|完成 Obsidian 指标与表说明初稿；Mac mini 测试环境、权限、测试应用、数据库连接；生产代码备份|测试应用能收消息、独立回复；数据库只读访问可用；Redis 与生产隔离；记录代码和配置基线；准备状态须实测，不能由排期推断|
|9/22 半天 · D22 最小链路基础|优化多轮记忆；确定四类问数 Skills 模板；选 3–5 个代表指标填样例|连续追问至少 3 轮保留未修改条件；品牌/时间变更正确；样例能通过合同检查。若半天不足，明确遗留，不声称已完成全部路由改造|
|9/23 · D23 最小可用链路|新版路由、Skills 加载、SQL Tool、结果输出；保留五模板；预留 Open Analysis 入口|3–5 个指标能从测试飞书取真实数据并返回数字、口径和来源；五模板分流正确；错误与澄清可用；部署可供 24 号远程测试的版本|
|9/24 · D24 全量知识|按模板整理全部指标与 27 张表；品牌、时间、渠道和质量规则；标准测试问题|全部知识格式与引用检查通过；每个指标有口径、来源和测试问题；无法对数者注明待 28 号验证。Windows 可做离线开发，通过飞书测试 Mac 已部署版本|
|9/28 · D28 全量接入和对数|加载全部 Skills；补齐所需查询与派生算子；核对字段、时间、品牌映射及结果|22 个指标逐项有可追踪测试结果；已实现且数据正常的指标对数通过；暂不可用项有具体原因、证据和责任人|
|9/29–9/30 · D29/30 飞书联调与测试环境上线测试|跑通问数、模板、简短回答；优化速度、超时、复用；回归五模板；测试并修复|数字准确、时间/品牌识别准确、多轮不丢上下文；模板正常响应；多人不串话；形成可演示测试版本及已知限制|

负责人沿用截图：9/21–9/28 为 Yuwei，9/29–9/30 为 Yuwei & Shuo；Codex 承担协作开发。29 号优先联调修复，30 号优先集中复测，二者属于同一验收阶段。

## 4. 四层决策架构

```text
飞书消息 + 接收时间 + 用户权限 + 当前 TaskFrame
  → ① Semantic Request：理解用户想做什么、这句话改了哪些条件
  → ② Resolver / Policy：合并上下文、确定口径与权限
       业务执行分类：TEMPLATE（固定模板）/ LOOKUP（灵活问数）/ ANALYSIS（开放分析）
       辅助处理分支：NO_DATA（无需取数）/ CLARIFY（澄清）/ REJECT（拒绝执行）
  → ③ Plan Compiler：编译可执行步骤并检查范围和预算
  → ④ Executor / Evidence：执行模板、SQL、派生计算并交付证据
  → Renderer：呈现结果（文字、表格或报告；不参与任务分类）
```

四层是代码职责边界，不是四次模型调用。普通问数默认最多一次语义理解调用，不调用复杂 Plan LLM，简单答案由确定性格式化输出。

执行分类与呈现形式是两个独立维度，不能互相替代：

|Policy 选择的业务执行分类|Renderer 可以采用的呈现形式|
|---|---|
|TEMPLATE：固定模板分析|原有五份完整报告|
|LOOKUP：灵活问数|单个数字的文字简答，或多品牌、多平台的对比表格|
|ANALYSIS：开放分析|分析结论、支撑表格或分析报告；本轮仅预留新增开放分析接口|

例如“上周天猫 GMV 多少”和“列出三平台上周 GMV”都属于 LOOKUP，分别适合文字和表格。要求“用表格展示”不能直接触发 ANALYSIS；要求“写成报告”也不能单凭格式要求触发五模板。Policy 根据业务目标及能力覆盖决定执行方式，Renderer 根据证据与输出偏好呈现结果。NO_DATA、CLARIFY、REJECT 是辅助处理分支，不是三种业务执行分类的替代名称。

### M1 用户理解与路由（22 号准备，23 号主体）

输入：`message_id/user_text/received_at/timezone`、权限范围摘要、版本化会话摘要、可用能力摘要。

输出 `SemanticRequest`：

|字段|要求|
|schema_version / request_id|固定版本和链路标识|
|task_relation|NEW_TASK / CONTINUE / MODIFY / FOLLOWUP / CANCEL；只提出关系，不自行清空状态|
|goals|保留用户全部目标，允许多个，不能只答其中容易的一项|
|slot_patch|每个字段的 KEEP / SET / CLEAR、原文、来源 turn、置信度和确认状态|
|metric_mentions / dimensions / filters|用户要求的指标、拆分方式及筛选；指标 ID 由 Resolver 确认|
|time_mentions / brand_mentions|保留原话和候选解释，不用来源表里的名字替代品牌身份|
|output_preference / ambiguities|简答、表格、完整报告偏好，以及需要澄清的具体问题|

模型输出不包含 chain 名、执行 Tool 名、数据库表名或 SQL。Schema 验证失败不能以半成品覆盖会话；在总时限内最多一次受控修复，否则给出可恢复的提示。

Policy 是执行路径的唯一裁决者：

|profile|适用场景|行为|
|NO_DATA|问用法、解释口径或不需要新数据的简答|走已有简答，保持业务任务上下文|
|TEMPLATE|明确需要五模板能覆盖的完整报告|固定 recipe 调用原 chain 和 formatter|
|LOOKUP|单个或多个可组合数字、排名、分组、同比、份额|目录匹配 → 固定查询/派生 recipe|
|ANALYSIS|需要开放探索的分析目标|本轮只保留接口；已支持的分析能力走现有受控适配，未支持部分明确说明|
|CLARIFY|缺必须字段或确有歧义|只问缺失/歧义项，保存其余已确认条件|
|REJECT|越权、禁止的组合或不支持的请求|说明原因，不调用数据库|

“追问”是任务关系，不是一个新的顶层执行器。报告后问具体数字仍可走 LOOKUP；“完整报告并补充一个数字”保留两个目标，按能力组合或说明受限部分。

测试：相同品牌+平台下，完整报告与单指标分流不同；“那天猫呢”继承范围；“为什么下降”不得直接冒充已实现的开放分析；缺条件只澄清一次所需信息；复杂问题不得丢目标。

### M2 多轮记忆与状态更新（22 号主体，23/29 号集成）

沿用 Redis 会话与现有 `TaskContextPatch`，升级为版本化 TaskFrame。所有影响业务槽位的写入和清空经过同一个 reducer；旧 domain/atomic/pending 状态只作兼容投影，不能各自成为另一份权威上下文。

输入：旧 TaskFrame、经过校验的 patch、事件 ID、预期 state_version。输出：新 TaskFrame、变更记录，或版本冲突/待澄清结果。

TaskFrame 至少保存 `schema_version/task_id/state_version/status`、品牌、期间、平台、品类、指标、维度、筛选、目标、待澄清项、证据引用、最近变更记录。每个业务字段记录值、来源 turn、原文、是否已确认；不依赖聊天历史完整存在。

必须满足的状态规则：

- 本轮未出现字段恒为 KEEP。`null`、模型漏字段、取数失败、完成报告、切换 Tool、普通简答都不能清空业务条件。
- “改看 8 月”仅 SET 时间，并使旧时间范围的证据不可复用；“那天猫呢”只 SET 平台。
- 更换品牌后重新检查品类/指标兼容性；不兼容条件标为待确认，禁止静默改成另一口径。
- CLEAR 仅由 reducer 执行，并要求明确原因。用户明确“重新开始/换个问题/取消”才创建新 frame 或关闭任务；模型低置信 NEW_TASK 不得直接清空。
- 完整独立新请求可提出新任务边界；有歧义则澄清。旧 frame 保留用于可定位的返回请求，不能直接丢弃全部历史。
- “刚才那份报告”优先按 task_id/evidence_id 找回；多个候选不能猜测。过期上下文不能伪装成仍被记住。
- 澄清是任务上的待补信息；用户只补时间时，不丢品牌等已确认字段。
- 同一会话先串行做上下文解析与提交，再冻结执行快照；后一个消息须基于前一个已提交状态。不能在第二条消息入队时就绑定尚未更新的旧状态。
- 执行 job 携带 resolved scope、task_id、state_version、权限和 catalog 版本；执行期间不重新读取可变活动槽位。旧 job 完成不得覆盖较新的任务条件。
- Redis CAS/乐观锁冲突需重新读状态并重新应用 patch；message_id 去重防止重复推进版本。
- 会话标识至少隔离环境、应用、用户；群聊再隔离 chat/thread。共享缓存仍需校验权限，不能让相同问句导致跨用户抑制或泄漏。

测试：连续 5 轮只分别改平台、时间、指标；中间问一句用法仍保留条件；失败后重试；明确换任务；返回旧报告；两条消息快速到达；重复消息；旧 job 晚完成；Redis 重启后的恢复/过期说明；两用户同问句互不干扰。

### M3 时间和品牌标准合同（22–23 号）

`TimeScope` 输入为时间原文、消息接收时间、继承值、业务时间规则。输出包含 `raw_text/timezone/start_inclusive/end_exclusive/granularity/resolution_rule/source_turn`，当前期与各比较期分开保存。统一 Asia/Shanghai；SQL 用左闭右开范围，给用户显示自然日期。

“上周”按接收日期锚定的上一完整自然周计算；“最近 7 天/本月”是否含当天、闰年同比和不足月处理必须由已确认规则定义，未定义则澄清。不得因数据不足偷偷缩短用户请求日期。晚排队、重试、跨午夜不重新解释相对时间。月表不能拆出不存在的日数据，日/月表不能重复求和。

`BrandRef` 输入为用户原话、平台/品类及品牌目录，输出 `surface/brand_id/display_name/candidates/resolution_status/mapping_version`。复用现有品牌映射、别名与 source binding；只在查询编译时按表绑定来源名称。无匹配和多匹配分别处理；用户纠正优先，不把一次模型猜测写回全局品牌库。

测试：固定 received_at 的上周/跨月/跨年/月底/闰日；自定义比较期；中文/英文/别名品牌等价；同名多候选；换品牌后不残留旧表绑定。

### M4 Skills 与指标合同（22 号模板，24 号全量，28 号核验）

Obsidian《生意数据》是业务定义来源；`docs/data-dictionary/` 是历史参考。运行时读取审核后的版本快照，不实时执行未审核笔记。表结构与实际数据验证另行完成；示例 SQL 不自动等于已验证的执行代码。

建议在 `bot/skills/` 下增加 flexible-query 知识目录，复用现有 loader；无需向量库。使用“精简说明 + 可校验的 JSON/YAML 合同 + SQL samples + 测试案例”。每次只加载相关指标及依赖的表/规则/质量卡。

|卡片|输入内容 / 必填合同|运行时输出|
|指标|metric_id、别名、定义、单位、基础/派生类型、必须条件、允许维度/筛选、依赖、聚合/分母/去重、表引用、时间规则、版本、状态、示例与测试|可编译的指标定义及限制|
|表|dataset_id、真实库表、用途、每行粒度、字段类型、主键/去重键、时间列/时区、金额单位、可加总范围、允许 join 及基数、更新和覆盖|授权字段与安全关联约束|
|业务规则|rule_id、适用范围、品牌/渠道/品类映射、强制过滤、排除条件、冲突优先级、版本、例子|查询前须满足的口径约束|
|质量规则|quality_id、更新时点、完整性检查、异常判定、缺数处理、阻断/警告级别|coverage、quality_flags、是否可回答|

每个 sample query 包含适用/不适用场景、参数、依赖表、grain、预期列与单位；至少一个正常和一个边界测试。示例中固定品牌/日期必须改成参数，演示数值不能当作生产结果。

发布流程：Obsidian 版本记录 → 四类卡片整理 → schema/引用检查 → 离线案例 → 真实 schema 与 SQL 对数 → 发布 catalog_version。发布状态为 `draft/verified/restricted/blocked`，记录原因；另设 implementation_status，区分未开发与数据问题。

首批 3–5 个指标至少覆盖直接取数、依赖计算（同比/份额）、有特殊限制的口径。选定后记入开发记录，不在这里捏造正式 metric_id。整体 Pure Mass、品类 Pure Mass、店铺 GMV、商品样本 GMV 必须有不同且明确的合同。

离线检查：重复 ID、断引用、依赖环、占位字段、缺单位、未知操作、缺来源均失败；待补表不得发布为 verified。24 号知识齐全不代表对数已完成。

### M5 Plan 编译与校验（23 号基础，28 号扩展）

输入 `ResolvedRequest`：已确认 scope、metric IDs、catalog_version、权限快照、输出偏好、可复用证据及预算。

输出 `ExecutionPlan`：`plan_id/task_id/state_version/locked_scope_hash/catalog_version/profile/steps/budget`；每个 step 有 `step_id/capability_id/inputs/depends_on/expected_output`。

capability registry 定义各能力输入输出 schema、授权要求、成本级别、超时和支持范围。模板使用固定 recipe；普通问数由指标依赖编译 query → derive → render。不要给每个用户句子新增 route。

Validator 必须拒绝：未知能力、缺依赖/依赖环、未确认品牌/时间、不可组合粒度、未授权表字段、未验证指标、超过步骤/查询预算，以及任何试图改变锁定 scope 的步骤。多目标可部分完成，但输出必须逐项解释未完成目标。

测试：单指标无需 Plan LLM；同比自动产生两期依赖；分母范围不一致拒绝；Planner 修改品牌/期间被拒绝；未知 capability 不执行；模板 recipe 和原 chain 一致。

### M6 SQL 取数 Tool（23 号最小版，28 号完整组合）

输入 `QueryRequest`：metric_id/version、dataset 引用、已解析期间、brand_id、允许的过滤/分组/排序、参数、用户权限、预算、scope_hash。模型不能直接把任意 SQL 传给通用连接函数。

服务端从已审核合同编译 SQL：允许自由组合合同支持的维度与筛选，不局限于 3–5 个固定问句。表名、列名、函数、聚合与 join 来自允许列表，值用参数绑定；所有执行 SQL 经过结构校验，复杂语句用 MySQL 方言 AST 验证。若使用 sqlglot 等解析器，实施时锁定版本。

输出 `QueryResult`：typed columns、有限 rows、单位、grain、scope_hash、来源、query fingerprint、查询时间、最新数据日、coverage、quality_flags、状态与失败原因。

限制：独立只读账户；禁止多语句、写入、DDL、文件输出、任意函数/存储过程、SELECT *、越权来源、未知 join、无日期界限和超预算查询。LIMIT 不能替代扫描成本约束。耗时查询须在数据库侧终止/限制，不能只让前端停止等待而后端继续占连接。

错误区分 `permission_denied/invalid_scope/unsupported_combination/missing_mapping/no_data/incomplete_coverage/quality_blocked/timeout/system_error`。返回空集不自动等于 0；只有覆盖检查和合同确认的真实零值才返回 0。

测试：正常查询与独立人工 SQL 对数；注入、多语句、越权表列、非法 join、无时间条件被阻断；缺日/部分平台不得展示为完整合计；超过行数/字节上限显式提示；超时释放连接；日志不包含密码和敏感明细。

### M7 Python/Pandas 派生 Tool（23 号首批所需，28 号补齐）

复用 `bot/derive_ops.py`，在现有 Python 环境安装/锁定 Pandas。输入为 `operation + evidence_refs + typed rows + parameters`；输出为派生数据、公式、分子分母来源、单位和质量状态。

首期按全部指标清单需要实现同比、份额、份额变化、排名、分组/透视等白名单算子。同比=(本期−同期)/同期；份额=分子/分母；份额差用百分点（pp），不能与同比百分比混淆。具体口径仍服从 metric contract；基期为 0、null 或不完整必须明确状态。

金额保留原始精度，展示时才四舍五入；比例统一内部小数，展示百分比。不同币种、单位、grain 或 scope 不能未经转换直接计算。测试包含零分母、负数、空值、并列排名、单位转换和舍入边界。

本轮不需要通用代码执行环境或 coding Agent；以后开放脚本须另做隔离进程、无网络/无数据库凭据及资源限制。

### M8 证据、输出与五模板兼容（23 号基础，29–30 号验收）

统一 `EvidenceEnvelope` 至少包含：`evidence_id/task_id/request_id/scope_hash/metric_id/metric_version/catalog_version/current_period/comparison_periods/filters/grain/rows/aggregates/unit/source_refs/query_fingerprint/queried_at/data_latest_at/coverage/quality_flags/derivations/limitations/status`。

Renderer 输入证据包和输出偏好，输出 `text/table/document_ref + answered_goals/unanswered_goals`。执行字段 `profile` 与呈现字段 `output_preference` 分开保存；Renderer 不重新选择 TEMPLATE / LOOKUP / ANALYSIS，也不改变查询范围。正常简答包含数字、单位、期间、关键范围和简短来源；口径有歧义时带口径说明。不能引用没有证据的数字或从报告 Markdown 反算指标。

缓存复用须同时匹配权限、metric/version、范围、期间、粒度、刷新版本/TTL 和质量；用户只改展示形式可复用，改范围必须重新判断。Session 仅保存有限摘要和引用，大结果不能无限进入 Redis。

五模板复用原 chain/formatter，经 adapter 输出证据。首轮可只适配追问需要的关键数；未结构化部分标记不可复用，不为统一接口全面重写五套报告。普通问数不创建长文档。

测试：同输入下旧/新入口报告关键数字和范围一致；品牌/时间变更不复用旧数；缺数据不编数字；失败说明可操作；一句“GMV 多少”不生成完整报告；现有简单回答继续可用；同一问数仅切换文字/表格时，profile 仍为 LOOKUP，取数范围和数字不变。

### M9 Redis、运行隔离、速度与观测（21 号配置，23–30 号验证）

保留 Redis/RQ、同会话顺序、跨用户并行。测试应用、代码配置、Redis 实例或命名空间、队列、session、dedupe、cache、日志和输出目录必须隔离。独立测试账户访问同一事实库仍消耗数据库资源，因此限制测试并发。

建议首轮 staging 1 worker、1 内部查询并行、DB pool=1、overflow=0；并发验收再按连接预算调整。绝不直接套用或修改生产配置。

建议初始配置（23 号实测后调整并记录，属于工程预算而非已达成指标）：LOOKUP 最多 6 SQL/10 steps；单次最多 5,000 rows/5 MB；SQL deadline 8 秒、问数总 deadline 20 秒；失败最多一次可重试且受总时限约束。指标确有更多依赖时需明确扩充预算，不能截掉目标。服务端数据库限时的可用机制按实际 MySQL 版本核验。

普通问数体验目标 p95 ≤10 秒，以飞书收消息到最终答案计算，另报排队耗时；至少采样 30 次代表请求，分别列冷/热缓存。五模板与改造前同输入基线比较，性能退化超过 20% 要定位和说明。2–3 用户并发是功能验收，不等同正式容量压测。

Trace 记录每层耗时、模型调用次数、relation、槽位前后值及来源、profile、plan、metric/catalog 版本、查询指纹、失败原因、证据引用。脱敏与保留期沿用项目规范。新增功能开关必须同时约束入口与执行器；关闭后既有能力可恢复，新问数要明确不可用，不能误转成别的报告。

### M10 Open Analysis 预留（23 号接口，后续实现）

本轮完成：ANALYSIS profile、goals 列表、capability registry、锁定 scope、EvidenceEnvelope、Plan 校验/预算/trace、事实与解释分离的扩展字段。现有已可用分析路径保留兼容适配。

以后接入：AnalysisBrief → 证据计划 → query/derive → 证据缺口检查 → 有上限的补查 → 总结。事实标 evidence_ref，解释标 supported/hypothesis/unknown。假设不能变成事实或因果结论。

本轮不实现自主循环、假设树、多 Agent 协作和任意 SQL/Python；新分析请求超出现有能力时如实说明，不能只留一个路由名称却对用户宣称可用。

## 5. 现有代码的改造落点

以下为 9/21 本地代码已存在的落点，开发前还需核对当天版本：

|模块|优先修改/复用的代码|改造边界|
|M1/M3|bot/llm_router.py、router.py、router_v2.py、routing_contracts.py、entity_resolution.py、brand_mapping.py、brand_source_binding.py|统一语义合同，复用映射，清理重复裁决；旧 route 用适配器兼容|
|M2|bot/session.py、task_context.py、conversation_control.py、task_queue.py、jobs.py|现已有 reducer 和 KEEP/SET/CLEAR；补来源、清空门禁、快照、版本与隔离；排查所有绕过 reducer 的写状态入口|
|M4|bot/skills/loader.py、bot/skills/|增加四类知识合同与发布快照；原 Skills 保持可用|
|M5|bot/execution_policy.py、agent_plan.py、dynamic_plan.py、executable_plan.py|Policy 唯一分流，简单 recipe 不调用动态 planner|
|M6/M7|bot/db/connection.py、bot/tools/、derive_ops.py|新增受控查询入口，fetch_df 不直接对模型暴露；派生白名单复用|
|M8|bot/ec_report_evidence.py、inline_answer.py、grounded_summary.py、chains/、各 formatter|证据 adapter 与输出契约，不整体重写模板|
|M9|bot/runtime_config.py、redis_client.py、worker_pool.py、task_queue.py、request_audit.py、timing.py、app.py/main.py|从真实消息入口接通；环境隔离、超时与 trace|

新模块可放在 `bot/flexible_query/`（contracts/catalog/compiler/query/evidence），避免再造一整套独立 Router/Session。采用现有 dataclass 或验证库即可，不为文档强制换框架。

## 6. 测试矩阵与交付门槛

|编号|必须通过的验收|
|T01 路由|五模板、普通问数、追问、简答、歧义、越权、多目标、分析入口均有固定案例；核对 profile 和 scope|
|T02 记忆|3–5 轮继承/局部修改；新任务/取消；澄清恢复；失败/简答不清空；同用户连续消息、旧 job 回写和重复消息|
|T03 实体|品牌别名与歧义；固定接收时间的日期边界；比较期保留；换表不改变品牌身份|
|T04 知识|22 指标、27 表以及所依赖规则全量清单；结构、引用、依赖、单位、发布状态检查|
|T05 对数|每个可执行指标至少正常取数、范围变更、缺失/异常三类案例；固定输入、独立核验 SQL/结果、容差和证据|
|T06 查询安全|越权、注入、多语句、非法表列/关联、无界扫描、超时、结果过大均不能绕过限制|
|T07 计算|同比/份额/pp、零值/空值、单位、精度、并列排名；不混口径与粒度|
|T08 兼容|五模板关键数、结构、入口和后续追问；旧简单回答及现有可用分析功能回归|
|T09 多用户|2–3 用户并发；同问句不串话/不相互去重；不同权限不能共享越权结果；慢任务不永久阻塞|
|T10 体验|至少 20 个真实业务问题、30 次延迟采样；短回答、澄清、进度、超时与部分结果均可理解|
|T11 回退|测试环境开关关闭、重启、旧 session 兼容或明确过期；恢复旧能力；记录恢复步骤|

数值比较：计数精确相等；金额按合同精度比较；比例按预先记录的绝对/相对容差比较，不能为了通过临时放宽。对数 SQL 不可只是调用同一被测函数，否则不能作为独立证据。

已有测试入口为 `python tests/run.py`；先用环境 venv，并选择当天相关测试模块。已有可复用用例包括 `tests.test_task_context`、`tests.test_brand_multiturn_regression`、`tests.test_entity_resolution_v2`、`tests.test_execution_policy`、`tests.test_executable_plan`、`tests.test_queue_runtime`。新用例按 M/T 编号可追踪；数据库/飞书端到端测试单独标记，Windows 离线运行不能误连生产。

每个指标验收行需记录：metric_id、源卡版本、实现状态、数据状态、支持平台/维度、测试输入、预期和实际结果、误差、query fingerprint、测试时间、通过/失败、限制原因、负责人。全部指标纳入不等于全部未经验证即可上线。

最终门槛：关键路由/记忆/权限/对数案例全部通过；所有指标有明确状态；没有未说明的回归；多人使用隔离正确；性能有实测；未完成开发与受限数据项分开列出。未达门槛时交付问题清单和可演示范围，不能标记“全量验收完成”。

## 7. 环境和依赖清单

- 现有受支持 Python + 项目独立 venv，按 requirements 锁定依赖；SQLAlchemy/PyMySQL、Pandas、Redis/RQ 沿用项目环境。
- 测试飞书 App ID/secret 和消息接收配置；模型 API 及结构化输出能力；凭据只进测试配置，不进 Git/Skills。
- MySQL 只读账户、授权表/视图、访问白名单/安全连接；真实 schema、索引、覆盖与查询限时机制的核验记录。
- Mac mini 测试 Redis、进程启动/重启方式、日志轮转、可用磁盘与不休眠设置；24 号远程测试前确认服务持续在线。
- GitHub 私有仓库/分支和 Windows 同步方式；无需 Windows 远程操控 Mac 才能整理 Skills。自动部署或 SSH 未配置时，24 号只能测试 23 号已部署版本，新提交待 Mac 更新后生效。
- 新增依赖仅在需要时加入：MySQL SQL AST 解析器、选用 YAML 时的 YAML 解析器、合同校验工具。优先复用已有库并固定版本。

## 8. 变更记录

|日期|决定|影响|
|2026-09-21|采纳用户两张截图排期：22 号半天、3–5 个样例、29–30 号合并联调测试|替代旧排期；全部指标范围保持|
|2026-09-21|四层决策、统一 reducer、受控 query/derive、证据合同；保留 Redis 和五模板|本轮开发和验收基准；尚不代表实现完成|
|2026-09-21|明确执行分类 TEMPLATE / LOOKUP / ANALYSIS 与呈现形式文字/表格/报告分离|更新架构图、输出合同及测试；不改变既定业务分类或开发范围|
