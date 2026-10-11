-- Qfii schema (current version: db.SCHEMA_VERSION). Timestamps are ISO-8601 UTC text.
-- Cash is stored in integer cents; JSON columns hold validated agent output.

-- One "paper" = one stock analysis moving through the pipeline.
CREATE TABLE papers (
    id          INTEGER PRIMARY KEY,
    symbol      TEXT NOT NULL,
    state       TEXT NOT NULL DEFAULT 'candidate' CHECK (state IN (
                    'candidate', 'packet_ready', 'needs_evidence', 'deferred', 'proposal',
                    'sent_back', 'decided', 'executed', 'dropped', 'rejected',
                    'passed', 'shelved', 'exec_rejected', 'closed')),
    origin      TEXT NOT NULL DEFAULT 'discovery' CHECK (origin IN ('discovery', 'request', 'user')),
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX papers_state ON papers(state);
CREATE INDEX papers_symbol ON papers(symbol);

-- Append-only log of every state transition.
CREATE TABLE events (
    id          INTEGER PRIMARY KEY,
    paper_id    INTEGER NOT NULL REFERENCES papers(id),
    from_state  TEXT,
    to_state    TEXT NOT NULL,
    actor       TEXT NOT NULL,
    note        TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX events_paper ON events(paper_id);

-- Every LLM call, for cost accounting and budgets.
CREATE TABLE llm_calls (
    id                 INTEGER PRIMARY KEY,
    agent              TEXT NOT NULL CHECK (agent IN ('research', 'assessment', 'risk', 'aggressive', 'conservative', 'neutral')),
    tier               TEXT NOT NULL CHECK (tier IN ('mid', 'strong', 'strongest')),
    provider           TEXT NOT NULL,
    model              TEXT NOT NULL,
    paper_id           INTEGER REFERENCES papers(id),
    prompt_tokens      INTEGER NOT NULL DEFAULT 0,
    completion_tokens  INTEGER NOT NULL DEFAULT 0,
    cost_usd           REAL NOT NULL DEFAULT 0,
    ok                 INTEGER NOT NULL,
    error              TEXT,
    created_at         TEXT NOT NULL
);
CREATE INDEX llm_calls_agent_day ON llm_calls(agent, created_at);

-- Raw facts from deterministic MCP tools. evidence_key = "ev_" + content hash, so the
-- same fact is stored once and every agent cites it by that key.
CREATE TABLE evidence (
    id             INTEGER PRIMARY KEY,
    symbol         TEXT,
    kind           TEXT NOT NULL CHECK (kind IN ('price', 'filing', 'macro', 'news', 'calc')),
    source         TEXT NOT NULL,
    source_url     TEXT,
    as_of          TEXT,
    retrieved_at   TEXT NOT NULL,
    evidence_key   TEXT NOT NULL UNIQUE,
    payload_json   TEXT NOT NULL,
    params_json    TEXT,
    quality_flags  TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX evidence_symbol ON evidence(symbol, kind);

-- Targeted follow-up work for Research, served before open discovery.
CREATE TABLE research_requests (
    id            INTEGER PRIMARY KEY,
    paper_id      INTEGER REFERENCES papers(id),
    symbol        TEXT NOT NULL,
    requested_by  TEXT NOT NULL CHECK (requested_by IN ('assessment', 'risk', 'aggressive', 'conservative',
                                                       'neutral', 'user')),
    question      TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'done', 'dropped')),
    created_at    TEXT NOT NULL,
    done_at       TEXT
);

-- Research output: factual packet citing evidence ids.
CREATE TABLE packets (
    id            INTEGER PRIMARY KEY,
    paper_id      INTEGER NOT NULL REFERENCES papers(id),
    body_json     TEXT NOT NULL,
    evidence_ids  TEXT NOT NULL,
    quality_flags TEXT NOT NULL DEFAULT '[]',
    llm_call_id   INTEGER REFERENCES llm_calls(id),
    created_at    TEXT NOT NULL
);

-- Assessment output. Revisions requested by the committee bump version.
CREATE TABLE proposals (
    id           INTEGER PRIMARY KEY,
    paper_id     INTEGER NOT NULL REFERENCES papers(id),
    version      INTEGER NOT NULL DEFAULT 1,
    verdict      TEXT NOT NULL CHECK (verdict IN ('submit', 'reject', 'defer', 'needs_evidence')),
    body_json    TEXT NOT NULL,
    llm_call_id  INTEGER REFERENCES llm_calls(id),
    created_at   TEXT NOT NULL,
    UNIQUE (paper_id, version)
);

-- Risk & Validation reports on a proposal.
CREATE TABLE reviews (
    id           INTEGER PRIMARY KEY,
    paper_id     INTEGER NOT NULL REFERENCES papers(id),
    proposal_id  INTEGER NOT NULL REFERENCES proposals(id),
    reviewer     TEXT NOT NULL CHECK (reviewer IN ('risk')),
    outcome      TEXT NOT NULL,
    body_json    TEXT NOT NULL,
    llm_call_id  INTEGER REFERENCES llm_calls(id),
    created_at   TEXT NOT NULL
);

-- One committee debate over one version of a paper's reports. A send-back to a
-- team ends the debate; the revised reports start a new one.
CREATE TABLE debates (
    id                INTEGER PRIMARY KEY,
    paper_id          INTEGER NOT NULL REFERENCES papers(id),
    proposal_id       INTEGER REFERENCES proposals(id),
    risk_report_id    INTEGER REFERENCES reviews(id),
    outcome           TEXT CHECK (outcome IN ('invest', 'replace', 'pass', 'send_back',
                                              'no_consensus')),
    rounds_used       INTEGER NOT NULL DEFAULT 0,
    started_at        TEXT NOT NULL,
    ended_at          TEXT,
    -- [{"result", "detail"}] per round, counted under the rules the debate ran with
    round_results_json TEXT
);

-- Each member's argument and ballot for one round. Ballots carry equal weight.
-- Actions: invest (new position at size_usd), replace (sell replace_symbol, buy
-- at size_usd), pass, request_info (from target team(s)), send_back (to target
-- team for full revision with feedback).
CREATE TABLE ballots (
    id              INTEGER PRIMARY KEY,
    debate_id       INTEGER NOT NULL REFERENCES debates(id),
    round           INTEGER NOT NULL CHECK (round BETWEEN 1 AND 50),
    member          TEXT NOT NULL CHECK (member IN ('aggressive', 'conservative', 'neutral')),
    action          TEXT NOT NULL CHECK (action IN ('invest', 'replace', 'pass', 'request_info',
                                                    'send_back', 'propose', 'support',
                                                    'abstain')),
    size_usd        REAL CHECK (size_usd IS NULL OR size_usd > 0),
    replace_symbol  TEXT,
    target          TEXT CHECK (target IN ('assessment', 'research', 'risk', 'both')),
    request         TEXT,
    argument        TEXT NOT NULL,
    llm_call_id     INTEGER REFERENCES llm_calls(id),
    created_at      TEXT NOT NULL,
    amendment_id    TEXT,            -- propose: the id it received; support: the id backed
    amendment_json  TEXT,            -- propose: the proposed terms
    changed_because TEXT,            -- why the member changed position (anti-herding audit)
    UNIQUE (debate_id, round, member),
    CHECK (action NOT IN ('invest', 'replace') OR size_usd IS NOT NULL),
    CHECK (action <> 'replace' OR replace_symbol IS NOT NULL),
    CHECK (action NOT IN ('request_info', 'send_back') OR (target IS NOT NULL AND request IS NOT NULL))
);

-- Extra information the committee voted for. The team answers with a report that
-- every member sees; members never chat with the teams directly.
CREATE TABLE info_requests (
    id            INTEGER PRIMARY KEY,
    debate_id     INTEGER NOT NULL REFERENCES debates(id),
    round         INTEGER NOT NULL,
    team          TEXT NOT NULL CHECK (team IN ('assessment', 'research', 'risk')),
    questions     TEXT NOT NULL,
    report_json   TEXT,
    llm_call_id   INTEGER REFERENCES llm_calls(id),
    created_at    TEXT NOT NULL,
    answered_at   TEXT
);

-- Final committee outcome for a paper that reached a trade decision.
CREATE TABLE decisions (
    id              INTEGER PRIMARY KEY,
    paper_id        INTEGER NOT NULL REFERENCES papers(id),
    debate_id       INTEGER NOT NULL UNIQUE REFERENCES debates(id),
    decision        TEXT NOT NULL CHECK (decision IN ('invest', 'replace', 'pass')),
    size_usd        REAL,
    replace_symbol  TEXT,
    rounds          INTEGER NOT NULL,
    created_at      TEXT NOT NULL,
    amendment_id    TEXT,            -- set when the committee agreed on a member's amendment
    terms_json      TEXT
);

-- Private memory per committee member. A member's prompt only ever includes its
-- own rows, so the three develop different experience over time.
CREATE TABLE member_memory (
    id          INTEGER PRIMARY KEY,
    member      TEXT NOT NULL CHECK (member IN ('aggressive', 'conservative', 'neutral')),
    paper_id    INTEGER REFERENCES papers(id),
    kind        TEXT NOT NULL CHECK (kind IN ('reflection', 'outcome', 'lesson')),
    text        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX member_memory_member ON member_memory(member, id);

CREATE TABLE orders (
    id                INTEGER PRIMARY KEY,
    decision_id       INTEGER REFERENCES decisions(id),
    symbol            TEXT NOT NULL,
    side              TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    qty               REAL NOT NULL CHECK (qty > 0),
    order_type        TEXT NOT NULL CHECK (order_type IN ('market', 'limit')),
    limit_price       REAL,
    status            TEXT NOT NULL CHECK (status IN ('rejected', 'submitted', 'filled', 'cancelled')),
    rejection_reason  TEXT,
    created_at        TEXT NOT NULL,
    CHECK (order_type = 'market' OR limit_price IS NOT NULL)
);

CREATE TABLE fills (
    id         INTEGER PRIMARY KEY,
    order_id   INTEGER NOT NULL REFERENCES orders(id),
    qty        REAL NOT NULL CHECK (qty > 0),
    price      REAL NOT NULL CHECK (price > 0),
    fee_cents  INTEGER NOT NULL DEFAULT 0,
    filled_at  TEXT NOT NULL
);

-- Open tax lots. Holdings, cost basis and holding period (short vs long-term
-- tax) are derived from these; qty_open drops as lots are sold (FIFO).
CREATE TABLE lots (
    id          INTEGER PRIMARY KEY,
    symbol      TEXT NOT NULL,
    fill_id     INTEGER REFERENCES fills(id),
    qty         REAL NOT NULL CHECK (qty > 0),
    qty_open    REAL NOT NULL CHECK (qty_open >= 0),
    cost_per_share REAL NOT NULL CHECK (cost_per_share > 0),
    opened_at   TEXT NOT NULL,
    CHECK (qty_open <= qty)
);
CREATE INDEX lots_symbol ON lots(symbol, opened_at);

-- Signed cash movements; balance = SUM(amount_cents).
CREATE TABLE cash_ledger (
    id            INTEGER PRIMARY KEY,
    amount_cents  INTEGER NOT NULL,
    reason        TEXT NOT NULL CHECK (reason IN ('deposit', 'buy', 'sell', 'fee', 'adjustment')),
    ref_type      TEXT,
    ref_id        INTEGER,
    created_at    TEXT NOT NULL
);

-- What the user tells us: capital to allocate and tax situation. One row.
CREATE TABLE IF NOT EXISTS investor_profile (
    id             INTEGER PRIMARY KEY CHECK (id = 1),
    capital_usd    REAL NOT NULL CHECK (capital_usd > 0),
    income_usd     REAL CHECK (income_usd IS NULL OR income_usd >= 0),
    filing_status  TEXT NOT NULL CHECK (filing_status IN ('single', 'married_joint')),
    state_tax_pct  REAL NOT NULL DEFAULT 0 CHECK (state_tax_pct BETWEEN 0 AND 20),
    updated_at     TEXT NOT NULL
);

-- Bottom-up week replay: one row per run, its progress, and what happened at
-- each 10-minute slot (research written or skipped, Assessment's screen, debates).
CREATE TABLE IF NOT EXISTS week_runs (
    id            INTEGER PRIMARY KEY,
    started_at    TEXT NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('running', 'done', 'stopped', 'failed')),
    symbols_json  TEXT NOT NULL,
    window_json   TEXT NOT NULL,
    last_slot     TEXT,
    state_json    TEXT,
    note          TEXT,
    updated_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS week_events (
    id            INTEGER PRIMARY KEY,
    run_id        INTEGER NOT NULL REFERENCES week_runs(id),
    slot          TEXT NOT NULL,
    symbol        TEXT,
    kind          TEXT NOT NULL,
    detail_json   TEXT NOT NULL DEFAULT '{}',
    paper_id      INTEGER REFERENCES papers(id),
    debate_id     INTEGER REFERENCES debates(id),
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS week_events_run ON week_events(run_id, id);
