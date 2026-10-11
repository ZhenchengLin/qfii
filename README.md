# Qfii

UCSC AI Hackathon, Cisco track ("When AI Agents Disagree"). A team of LLM agents
that researches stocks like a university department writes papers, then argues
about them before any (paper) money moves.

| Agent | Role | Model tier | Status |
|---|---|---|---|
| Research | Undergrads: continuous discovery, cited facts only | mid | **built** |
| Assessment | TAs: thesis, arguments, critique, verdict | strong | waiting on Ben's spec |
| Risk & Validation | Independent reviewers: challenge the proposal | strong | waiting on Ben's spec |
| Committee: Aggressive | Weighs fundamental upside over near-term downside | strong | waiting on Ben's spec |
| Committee: Conservative | Weighs downside protection over upside | strong | waiting on Ben's spec |
| Committee: Neutral | Balances both vs the S&P 500; tracks cash, positions, costs, holding period, taxes | strongest | waiting on Ben's spec |

The three committee members replace a single Central Allocator. Each round they
argue and cast equal-weight ballots: invest, replace (sell a holding for this
one), pass, request more info from a team, or send the stock back for revision.
Trades need consensus on the action and the size; after 3 rounds without one,
the stock is shelved. Each member keeps its own private memory. Order execution
is deterministic code. Watch a debate with `qfii debate NVDA --as-of 2026-10-09`.

## Agentic stack

| Layer | Technology | In Qfii |
|---|---|---|
| Agent ↔ agent | **A2A** (a2a-sdk 1.x, JSON-RPC over HTTP) | each agent is its own A2A server with an Agent Card |
| Agent ↔ tools | **MCP** (mcp 2.x) | `qfii-tools` server: price history, indicators, data-quality gate |
| Workflow inside an agent | **LangGraph** | gather → write_notes ⇄ validate → assemble / fail |
| Models | any OpenAI-compatible endpoint, or `fake` | NVIDIA NIM on Brev, Ollama, vLLM, OpenAI; fake runs offline |
| Records | SQLite | papers, events, evidence, packets, LLM calls, ledger |
| Transport (later) | SLIM | swap A2A's HTTP binding for SLIM once a SLIM node is up |

Rules the code enforces: LLMs never compute numbers (MCP tools do); every
observation must cite an evidence id that exists; invalid LLM output is
retried once with the reason, then fails visibly; tools never return data after
the `as_of` cutoff; execution mode can only be `paper`.

## Run it

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest -q

.venv/bin/qfii research AAPL --as-of 2026-10-09          # one local run
.venv/bin/qfii research AAPL --json                      # full EvidencePacket

# real data (no API keys): Yahoo prices incl. SPY + FRED macro into data/
.venv/bin/qfii data fetch
printf '[data]\nsource = "csv"\n' > config.local.toml   # use it

# the undergrads, continuously: requests first, then the universe in batches
.venv/bin/qfii --config config.local.toml request JPM --question "rate sensitivity?"
.venv/bin/qfii --config config.local.toml discover --forever --interval 300
.venv/bin/qfii --config config.local.toml status

# watch the committee debate one stock
.venv/bin/qfii --config config.local.toml debate NVDA

# as separate services (three terminals)
.venv/bin/qfii serve tools                               # MCP on :9100/mcp
.venv/bin/qfii --config live.toml serve research --save  # A2A on :9101
.venv/bin/qfii ask MSFT                                  # A2A client
```

`live.toml` for the remote-tools setup:

```toml
[tools]
mcp_url = "http://127.0.0.1:9100/mcp"
```

To use a real model for Research, e.g. an NVIDIA NIM or Ollama endpoint:

```toml
[models.mid]
provider = "openai_compat"
model = "meta/llama-3.1-8b-instruct"
base_url = "http://localhost:8000/v1"
api_key_env = "NIM_API_KEY"   # name of the env var, never the key itself
```

Data: `qfii data fetch` downloads adjusted daily prices (Yahoo chart API,
unofficial), FRED macro series and, if `data.sec_user_agent` is set to a
contact string as the SEC requires, EDGAR company facts. Agents only read the
local files, always cut at `as_of`: macro figures respect publication lag and
fundamentals only count filings filed by `as_of`. The default `synthetic`
source is a deterministic random walk for offline development and is flagged
`synthetic_data` in every packet.

## Layout

```
qfii/
  contracts.py            ResearchRequest, EvidenceItem, Observation, EvidencePacket
  llm.py                  FakeLLM, OpenAICompatLLM, JSON reply parsing
  tools/                  MCP server + client, market data, indicators, quality gate
  agents/research/        LangGraph workflow, prompt, A2A server, fake model
  a2a_client.py           ask_agent(url, payload)
  committee/              protocol (vote counting), engine, members, prompts, memory
  papers.py               paper state machine
  discovery.py            continuous research loop
  data_fetch.py           Yahoo / FRED / SEC downloaders
  portfolio.py            snapshot: cash, lots, tax, liquidity
  store.py, db.py, schema.sql, config.py, default_config.toml, cli.py
docs/PLAN.md              milestones and open questions for Ben
```
