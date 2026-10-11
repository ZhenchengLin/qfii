# Qfii — build plan (hackathon, paper trading)

Ben owns the economics: what each agent decides, its inputs and outputs, decision
rules, and which objections block a trade. This plan covers engineering only.

## Architecture (Ben, 2026-10-10: committee replaces the Central Allocator)

```
Research (mid) ──EvidencePacket──▶ Assessment (strong) ──Proposal──┐
   │ MCP tools                                                     ▼
qfii-tools                                   Risk & Validation (strong) ──RiskReport──┐
                                                                                      ▼
                    ┌──────────── Investment committee (equal votes) ───────────────────┐
                    │  Aggressive (strong)   Conservative (strong)   Neutral (strongest)│
                    │  upside-first          downside-first          vs S&P 500 + whole │
                    │                                              portfolio, costs,   │
                    │        ⇄ argue each round, vote, repeat ⇄    holding period, tax │
                    └──────── may question Assessment / Risk for more evidence ─────────┘
                                              │ consensus decision
                                              ▼
                       deterministic code: sizing → hard limits → paper broker → ledger
```

Committee protocol (Ben, 2026-10-10), implemented in `qfii/committee/`:

- **Teams write reports; they don't chat.** Assessment writes a proposal and Risk writes a report. Members never message the teams directly, which saves compute.
- **Each round.** Every member reads the same view and casts one equal-weight ballot at the same time. The view is: both reports, the portfolio, all earlier rounds, any extra reports, and that member's own memory only. A ballot is one of:
  - `invest`: new position at a size.
  - `replace`: sell a named position and buy this stock. This is the rolling way positions get sold.
  - `pass`: don't invest.
  - `request_info`: ask Assessment, Risk or both for a report that all three members see next round.
  - `send_back`: return the stock to a team for a full revision, with feedback.
- **Thresholds (in config).** Trades need a unanimous vote. Info requests and send-backs need a majority.
- **Size is negotiated.** Sizes count as agreed when all are within 10% of each other, and the median is used. Otherwise the members argue again next round.
- **Three-round safety net.** No decision by round 3 means the stock is shelved and the committee moves to the next one. An info request in round 3 isn't fetched, since there's no round left to use it.
- **Failures.** A member that fails or returns an invalid ballot counts as `pass`, so it can never force a trade on its own.
- **Separate memories.** Each member has its own memory in `member_memory`: an automatic reflection after every debate, plus any `note_to_self` it writes. Its prompts include only its own memories.
- **Portfolio access.** Everyone except Research gets the `PortfolioSnapshot`: cash, tax lots, holding period, estimated tax if sold, liquidity as % of daily volume, limits, costs and the SPY benchmark.
- **The user watches.** `qfii debate SYMBOL` prints every round's arguments and ballots, and the dashboard (M9) will show the same.
- **Execution is code.** No LLM places orders. Code turns the agreed decision into an order and checks the hard limits last.

## Models and memory (Zhen, 2026-10-10)

| Tier | Model (Ollama tag) | Agents | Approx. size at Q4 |
|---|---|---|---|
| max | Qwen3.5 27B (`qwen3.5:27b`) | Neutral | ~17 GB |
| high | DeepSeek-R1-Distill-Qwen-14B (`deepseek-r1:14b`) | Assessment, Risk, Aggressive, Conservative | ~9 GB |
| mid | Qwen3.5 9B (`qwen3.5:9b`) | Research | ~6.6 GB |

- **One model in memory at a time** (`runtime.exclusive_models`). Every call goes through `qfii/scheduler.py`.
  - The loaded model works through *all* queued calls before it is unloaded and the next one loads.
  - It waits a short settle window so calls about to arrive can join the batch.
  - `max_batch` keeps any model from starving the others.
- **The three members don't need three models.** A member's "memory" is just its prompt, so they share a loaded model for free.
- **Debates run in lockstep across stocks.** Every stock's high-tier ballots for a round are served by one load. The max-tier model stays loaded into the next round when it can. Test: 3 stocks × 3 rounds = 27 calls with 3 model swaps.
- **This Mac (M1 Pro, 16 GB) cannot run the 27B.** Plan (Zhen, 2026-10-10): option B now, GPU later.
  - `configs/mac-local.toml`: Research on the 9B; every other agent on R1 14B with one shared context size, so a whole debate runs on one load.
  - `configs/team-gpu.toml`: the real 27B for Neutral on a Brev GPU (all three models loaded side by side).
  - `configs/dev-ollama.toml`: 9B only, for quick checks.

### Measured on the 16 GB M1 Pro (option B, 2026-10-10)

| What | Result |
|---|---|
| Memory | R1 14B at 12k context = 10.0 GB, fully on the GPU; the 9B = 5.6 GB |
| Speed | ~10 tokens/s generated on the 14B; prompts ~3.3–3.9k tokens; ~100–125 s per ballot |
| One debate (research + 3 rounds) | 32 min, including retries |
| Ollama + R1 + JSON schema + thinking | HTTP 500, so the schema is off for R1 (`json_schema = false`) |
| Live outcome, NVDA | Aggressive invest $10k, Conservative pass ×3, Neutral asked Assessment for fundamentals: no consensus, shelved |
| Failures fixed from the live runs | `size_usd: 0` and `"target": ""` on unused fields; runaway thinking (output cap); size over the limit (cap shown and enforced); 3 failures counted as a "pass" (now an abstention) |

For the demo, the GPU setup is needed: ~30 min per stock is too slow to show live.

## Ben's rules, 2026-10-10 (second batch)

| Rule | Status |
|---|---|
| Messages are schema-checked JSON with numbers passed by reference (`docs/FORMATS.md`) | done |
| User enters capital; code sets tax rates from income and filing status (`qfii setup`) | done |
| Risk & Validation runs only when the committee commissions it, with the Research tools | protocol done; agent next |
| Committee can vote to hold a cash reserve | next |
| Committee can vote to adjust a held position when new events arrive (EPS miss, Fed rate move) | next, with execution |

## Ben's review, 2026-10-11

| Concern | What changed |
|---|---|
| Research lacks fundamentals and news | EdgarTools (XBRL 10-K/10-Q statements, 8-K events) + Docling (press releases) as MCP tools; BM25 search over risk factors, MD&A and press releases; point in time by filing date |
| Risk & Validation "did no research" (`input_value=[]`) | Lab data showed it did; our validator rejected uncited missing-data findings and long answers. `data_gap` items are now allowed. Risk uses the same tools and searches filings with the committee's questions |
| Members may agree just to beat the 3-round limit | Members no longer see the round count or a final-round flag; the prompt says no consensus is legitimate; a changed vote must state what changed it (code-enforced). Open: A/B test hidden vs visible limit |

## Amendments (Zhen, 2026-10-11)

Members can put their own deal on the table (`propose`: size, holding period,
stop-loss, take-profit, conditions, summary) and back any amendment on the table
(`support A1`). All three backing one amendment is a decision on its terms;
the decision stores the amendment id and terms. Switching which deal you back
needs `changed_because`, like any vote change.

## Cisco track fit

| Level | Qfii |
|---|---|
| 1 AI team | Research, Assessment, Risk and the committee are separate A2A agents with distinct jobs |
| 2 Adaptive | Agents find each other through Agent Cards; Research flags stale or missing data; members request extra evidence mid-debate |
| 3 Collective | **Three autonomous agents with conflicting objectives deliberate to consensus with equal votes and no central decider.** Baselines to compare: Neutral alone, majority vote without debate, buy-and-hold SPY |

## Milestones

| # | Module | Status |
|---|---|---|
| M0 | Config, hard limits, SQLite schema, CLI | done |
| M1 | Research agent: MCP tools, LangGraph workflow, A2A server, cited EvidencePacket | done |
| M1b | Committee: protocol and vote counting, debate engine, persona prompts, private memory, portfolio snapshot, `qfii debate` (fake members, placeholder team reports) | done |
| M2 | Paper state machine: legal transitions only, event log, DB-level state check | done |
| M3 | Real data: `qfii data fetch` (Yahoo prices incl. SPY, FRED macro, SEC fundamentals); MCP tools for benchmark, macro (publication-lag aware), fundamentals (filed-by-as_of only) | done |
| M4 | Continuous discovery: requests first, oldest-first universe scan, dedup, cheap screen (data quality, liquidity), queue cap, daily $ budget; `qfii discover/request/status` | done |
| M5 | Assessment agent: proposal from the packet (draft format), answers committee questions, revises after a send-back | done (format still draft) |
| M6 | Risk & Validation agent: on demand, re-collects evidence with the Research tools, adds code-computed portfolio impact | done (format still draft) |
| M7 | Committee with real models; send-back loop with `max_send_backs`; members as A2A servers | after M5–M6 |
| M8 | Execution: sizing from the agreed size, hard limits, paper broker, ledger, reconciliation | after M7 |
| M9 | Dashboard: paper detail with the full debate transcript and votes per round; portfolio vs SPY | after M8 |
| M10 | Evaluation vs baselines; OpenTelemetry traces; SLIM transport | stretch |

## Open questions for Ben

1. **Send-backs.** How many times can a stock be sent back before it's shelved? The default is 1.
2. **Selling.** Should "replace" stay the only way to sell, or should the committee also review existing positions on a schedule (for example after an earnings drop)? Right now a holding is only sold when a new candidate replaces it.
3. **Tax and cost numbers.** The placeholders are 35% short-term, 15% long-term after 365 days, and 5 bps slippage.
4. **Models.** Which model for each tier? Aggressive and Conservative from different model families would give genuinely different views.
5. **Report formats.** We need the Proposal and RiskReport fields, plus three worked examples with the expected outcome.
