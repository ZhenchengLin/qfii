# How Qfii agents talk without losing data

Ben asked which format lets LLMs talk to each other with no data loss. Short answer:

> **Schema-checked JSON, with every number passed by reference to a shared
> evidence store, never copied into prose.**

## Where data actually gets lost

| Loss | Example we saw | Guard in Qfii |
|---|---|---|
| A model retypes or relabels a number | Qwen3.5 9B called the 21-day return a "5-day return"; the number and citation were right, the label was invented | Numbers live in the evidence store under an id (`ev_…`). Agents cite ids, and every fact carries a fixed `label` the model must reuse. Downstream agents get the structured fact and the sentence together. |
| A model invents a source | A citation to `ev_made_up` | Code rejects any citation that is not in the store, then retries once with the reason. |
| Free text loses structure | "Size around 5–6k, maybe" | Ballots and reports are JSON with fixed fields (`size_usd: 5500`), validated by Pydantic. A reply that doesn't fit is rejected. |
| The prompt is silently cut off | Ollama drops the start of a prompt longer than `num_ctx` | Every call checks the token count Ollama reports. A prompt that fills the context window is refused, not truncated. |
| The model ignores the format | With `think=false`, Qwen3.5 ignored the JSON schema and invented its own field names | The schema is sent to the model (constrained decoding) *and* checked in code. Constrained decoding alone is not enough. |
| Paraphrasing across hops | Research → Assessment → committee, each restating the last | Each hop adds its own fields and cites the original evidence ids. Nobody re-summarizes upstream numbers. |

## Why not natural language, Markdown or YAML?

- **Prose:** loses numbers, labels and structure at every hop, and code can't check it.
- **Markdown tables:** easy to read but hard to validate. Models drift on columns.
- **YAML:** whitespace-sensitive and has type pitfalls ("NO" becomes false). It also has weaker support in constrained-decoding grammars.
- **JSON:** what Ollama, vLLM and OpenAI can constrain generation to, and what A2A data parts and MCP tool results already carry. Minified in prompts, it costs about the same tokens as YAML.

The free-text parts that matter (a member's `argument`, a proposal's `thesis`) are fields inside the JSON, so reasoning stays readable but is always attached to its structured vote or claim.

## The messages

Every message format is a Pydantic model in `qfii/contracts.py`. `qfii schemas` writes them as JSON Schema files to `docs/schemas/`.

| Message | From → to | Status |
|---|---|---|
| `ResearchRequest` | anyone → Research | in use |
| `EvidencePacket` | Research → Assessment | in use |
| `Proposal` | Assessment → committee | **draft, Ben to approve** |
| `RiskReport` | Risk & Validation → committee, only when commissioned | **draft, Ben to approve** |
| `Ballot` | each committee member, each round | in use |
| `PortfolioSnapshot` | code → Assessment, Risk and committee (not Research) | in use |

### Draft `Proposal` (the TAs' paper)

| Field | Meaning |
|---|---|
| `thesis` | one paragraph |
| `arguments[]` | `{claim, evidence_ids}` |
| `counterarguments[]` | `{claim, evidence_ids, rebuttal}` |
| `feasibility` | text |
| `uncertainties[]` | list |
| `invalidation_conditions[]` | what would prove the thesis wrong |
| `expected_holding_period_days` | sets short- vs long-term tax |
| `suggested_position_pct` | the TAs' starting point; the committee negotiates the real size |
| `verdict` | `submit`, `reject`, `defer` or `needs_evidence` |
| `research_questions[]` | used with `needs_evidence` |

### Draft `RiskReport` (only when the committee votes for it)

| Field | Meaning |
|---|---|
| `answers[]` | the committee's questions, answered with `{claim, evidence_ids}` |
| `findings[]` | `{issue, severity: low/medium/high/blocking, evidence_ids, detail}` |
| `assumption_checks[]` | the proposal's assumptions re-tested against real data with the Research tools: `{assumption, holds, evidence_ids, detail}` |
| `liquidity` | text |
| `portfolio_impact` | weight after the trade, cash left, tax |

**Decided (Ben, 2026-10-10): the committee always decides.** A `blocking`
finding is the strongest kind of objection, not a veto. Members who vote to
invest past one must say why in their argument. Hard limits (order size, cash,
position %) are separate and are always enforced by code.

**Question for Ben:** are these the right fields?
