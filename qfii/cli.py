"""Command line entry point: `qfii <command>`."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import date

from . import __version__
from .config import COMMITTEE, Config, ConfigError, load_config
from .db import cash_balance_cents, connect, init_db, schema_version, tables

TOOLS_PORT = 9100
RESEARCH_PORT = 9101


def _cmd_init(cfg: Config, args: argparse.Namespace) -> int:
    conn = connect(cfg.db_path)
    created = init_db(conn, cfg.limits.starting_cash_usd)
    verb = "initialised" if created else "already initialised"
    print(f"{cfg.db_path}: {verb} (schema v{schema_version(conn)})")
    print(f"cash: ${cash_balance_cents(conn) / 100:,.2f}")
    print("tables:", ", ".join(tables(conn)))
    return 0


def _cmd_config(cfg: Config, args: argparse.Namespace) -> int:
    print(f"db:         {cfg.db_path}")
    print(f"execution:  {cfg.execution_mode}")
    print(f"data:       {cfg.data_source}" + (f" ({cfg.csv_dir})" if cfg.data_source == "csv" else ""))
    print(f"tools:      {cfg.mcp_url or 'in-process MCP server'}")
    for name, agent in cfg.agents.items():
        m = cfg.models[agent.tier]
        print(f"agent {name:<11} tier={agent.tier:<9} model={m.provider}/{m.model} "
              f"budget=${agent.daily_budget_usd:.2f}/day")
    print(f"universe:   {len(cfg.symbols)} symbols ({', '.join(cfg.symbols)})")
    lim = cfg.limits
    print(f"limits:     cash=${lim.starting_cash_usd:,.0f} max_position={lim.max_position_pct}% "
          f"max_order=${lim.max_order_usd:,.0f} min_cash={lim.min_cash_pct}% "
          f"orders/day={lim.daily_order_cap}")
    c = cfg.committee
    print(f"committee:  {', '.join(COMMITTEE)} (equal votes); trade={c.invest_rule}, "
          f"info/send-back={c.procedural_rule}, size within {c.size_tolerance_pct}%, "
          f"max {c.max_rounds} rounds then shelve")
    pf = cfg.portfolio
    print(f"portfolio:  benchmark={pf.benchmark} costs={pf.commission_bps}+{pf.slippage_bps}bps "
          f"long-term after {pf.long_term_after_days}d; tax rates from `qfii setup`")
    return 0


def _print_packet(packet: dict, paper_id: int | None) -> None:
    head = f"{packet['symbol']} as of {packet['as_of']}  model={packet['model']}"
    print(head + (f"  paper #{paper_id}" if paper_id else ""))
    for obs in packet["observations"]:
        print(f"  [{obs['specialty']}] {obs['text']}  ({', '.join(obs['evidence_ids'])})")
    print(f"  flags: {', '.join(packet['quality_flags']) or 'none'}")
    for gap in packet["gaps"]:
        print(f"  gap: {gap}")


def _cmd_research(cfg: Config, args: argparse.Namespace) -> int:
    from .agents.research.agent import ResearchAgent, ResearchFailed
    from .contracts import ResearchRequest
    from .store import save_packet

    request = ResearchRequest(symbol=args.symbol, as_of=args.as_of, question=args.question)
    try:
        packet, calls = asyncio.run(ResearchAgent(cfg).run(request))
    except ResearchFailed as exc:
        print(f"research failed: {exc}", file=sys.stderr)
        return 1
    paper_id = None
    if args.save:
        conn = connect(cfg.db_path)
        init_db(conn, cfg.limits.starting_cash_usd)
        paper_id = save_packet(conn, request, packet, calls, cfg.agents["research"].tier)
    data = packet.model_dump(mode="json")
    if args.json:
        print(json.dumps(data, indent=2))
    else:
        _print_packet(data, paper_id)
    return 0


def _print_round(symbol: str, rec) -> None:
    print(f"\n-- {symbol} round {rec.round} --", flush=True)
    for b in rec.ballots:
        extra = ""
        if b.size_usd:
            extra = f" ${b.size_usd:,.0f}"
        if b.replace_symbol:
            extra += f" (sell {b.replace_symbol})"
        if b.target:
            extra = f" -> {b.target}: {b.request}"
        print(f"  {b.member:<12} {b.action}{extra}")
        print(f"  {'':<12} {b.argument}")
    print(f"  => {rec.outcome.kind}: {rec.outcome.detail}")
    for rep in rec.reports:
        r = rep["report"]
        print(f"  report from {rep['team']}:")
        for a in r.get("answers", []):
            print(f"    answer: {a['claim']}")
        for f in r.get("findings", []):
            print(f"    [{f['severity']}] {f['issue']}")
        if r.get("error") or r.get("note"):
            print(f"    {r.get('error') or r.get('note')}")
    print(flush=True)


def _print_proposal(proposal, version: int) -> None:
    label = "proposal" if version == 1 else f"revised proposal (v{version})"
    print(f"\nAssessment {label}: {proposal.verdict}, suggests "
          f"{proposal.suggested_position_pct}% for ~{proposal.expected_holding_period_days} days")
    print(f"  thesis: {proposal.thesis}", flush=True)


def _cmd_debate(cfg: Config, args: argparse.Namespace) -> int:
    from .committee.runner import run_paper

    conn = connect(cfg.db_path)
    init_db(conn, cfg.limits.starting_cash_usd)

    def on_research(packet) -> None:
        print(f"{packet.symbol} as of {packet.as_of}: {len(packet.observations)} research "
              f"observations, flags: {', '.join(packet.quality_flags)}", flush=True)

    run = asyncio.run(run_paper(cfg, conn, args.symbol, args.as_of, save=args.save,
                                on_research=on_research, on_round=_print_round,
                                on_proposal=_print_proposal))
    size = f" ${run.debate.size_usd:,.0f}" if run.debate and run.debate.size_usd else ""
    rounds = sum(len(d.rounds) for d in run.debates)
    print(f"\nOUTCOME: {run.outcome}{size} after {len(run.debates)} debate(s), {rounds} round(s)"
          + (f"  (paper #{run.paper_id})" if run.paper_id else ""))
    return 0


def _cmd_risk_check(cfg: Config, args: argparse.Namespace) -> int:
    """Ask Risk & Validation directly: research -> proposal -> risk review."""
    from .agents.research.agent import ResearchAgent
    from .agents.structured import AgentFailed
    from .agents.teams.agents import AssessmentAgent, RiskAgent
    from .contracts import ResearchRequest
    from .portfolio import snapshot

    async def run() -> int:
        conn = connect(cfg.db_path)
        init_db(conn, cfg.limits.starting_cash_usd)
        as_of = args.as_of or date.today()
        packet, _ = await ResearchAgent(cfg).run(ResearchRequest(symbol=args.symbol,
                                                                 as_of=as_of))
        portfolio = snapshot(conn, cfg, as_of).model_dump(mode="json")
        proposal = await AssessmentAgent(cfg).propose(packet, portfolio)
        print(f"proposal: {proposal.verdict}, {proposal.suggested_position_pct}% | "
              f"{proposal.thesis[:200]}")
        risk = RiskAgent(cfg)
        try:
            report = await risk.review(packet, portfolio, proposal, args.question)
        except AgentFailed as exc:
            print(f"RISK REPORT FAILED: {exc}", file=sys.stderr)
            return 1
        print(f"risk evidence collected: {len(risk.evidence)} items "
              f"({sum(e.payload.get('name') == 'filing_passage' for e in risk.evidence)} "
              f"filing passages)")
        for a in report.answers:
            print(f"  answer{' [data gap]' if a.data_gap else ''}: {a.claim}")
        for f in report.findings:
            print(f"  [{f.severity}]{' [data gap]' if f.data_gap else ''} {f.issue}")
        for c in report.assumption_checks:
            mark = {True: "holds", False: "FAILS", None: "unknown"}[c.holds]
            print(f"  assumption {mark}: {c.assumption[:150]}")
        return 0

    return asyncio.run(run())


def _cmd_bench(cfg: Config, args: argparse.Namespace) -> int:
    from pathlib import Path

    from .bench import bench_debate, format_table, save_report

    as_of = args.as_of or date.today()
    reports = []
    for i in range(args.runs):
        print(f"run {i + 1}/{args.runs}: {args.symbol} as of {as_of}", flush=True)
        report = asyncio.run(bench_debate(cfg, args.symbol.upper(), as_of,
                                          _print_round if args.verbose else None))
        path = save_report(report, Path(args.out), args.label or "bench")
        print(f"  saved {path}", flush=True)
        reports.append(report)
    print()
    print(format_table(reports))
    for r in reports:
        for err in r["errors"]:
            print(f"  error seen: {err}")
    return 0


def _cmd_ui(cfg: Config, args: argparse.Namespace) -> int:
    import uvicorn

    from .web.app import build_app

    print(f"Qfii debate viewer on http://{args.host}:{args.port}/", flush=True)
    uvicorn.run(build_app(cfg), host=args.host, port=args.port, log_level="warning")
    return 0


def _cmd_data(cfg: Config, args: argparse.Namespace) -> int:
    import httpx

    from .data_fetch import (
        FetchError,
        fetch_company_facts,
        fetch_fred_series,
        fetch_sec_ciks,
        fetch_yahoo_prices,
        write_facts,
        write_prices,
        write_series,
    )

    symbols = [s.upper() for s in args.symbols] or [*cfg.symbols, cfg.portfolio.benchmark]
    every = not (args.prices or args.macro or args.fundamentals)
    failures = 0
    with httpx.Client(timeout=30, follow_redirects=True) as http:
        if every or args.prices:
            for sym in symbols:
                try:
                    bars = fetch_yahoo_prices(http, sym, args.range)
                    path = write_prices(cfg.csv_dir, sym, bars)
                    print(f"prices  {sym:<6} {len(bars):>5} days  {bars[0].date}..{bars[-1].date}"
                          f"  -> {path}")
                except FetchError as exc:
                    failures += 1
                    print(f"prices  {sym:<6} FAILED: {exc}", file=sys.stderr)
        if every or args.macro:
            for sid in cfg.macro_series:
                try:
                    rows = fetch_fred_series(http, sid)
                    write_series(cfg.macro_dir, sid, rows)
                    print(f"macro   {sid:<13} {len(rows):>6} obs, latest {rows[-1][0]} = "
                          f"{rows[-1][1]}")
                except FetchError as exc:
                    failures += 1
                    print(f"macro   {sid} FAILED: {exc}", file=sys.stderr)
        if every or args.fundamentals:
            if not cfg.sec_identity:
                print("fundamentals skipped: set data.sec_user_agent (SEC requires a contact, "
                      'e.g. "Qfii research you@example.com")', file=sys.stderr)
            else:
                try:
                    ciks = fetch_sec_ciks(http, cfg.sec_identity)
                except FetchError as exc:
                    print(f"fundamentals FAILED: {exc}", file=sys.stderr)
                    return 1
                for sym in symbols:
                    if sym not in ciks:
                        print(f"fundamentals {sym:<6} no SEC filer (ETF or foreign?), skipped")
                        continue
                    try:
                        facts = fetch_company_facts(http, ciks[sym], cfg.sec_identity)
                        write_facts(cfg.fundamentals_dir, sym, facts)
                        print(f"fundamentals {sym:<6} {facts.get('entityName')}")
                    except FetchError as exc:
                        failures += 1
                        print(f"fundamentals {sym} FAILED: {exc}", file=sys.stderr)
    if every or args.prices:
        print("\nTo use these prices set [data] source = \"csv\" in your config.")
    return 1 if failures else 0


def _cmd_discover(cfg: Config, args: argparse.Namespace) -> int:
    from .discovery import run_loop

    conn = connect(cfg.db_path)
    init_db(conn, cfg.limits.starting_cash_usd)

    def show(n: int, report) -> None:
        print(f"cycle {n}: {report.summary()}", flush=True)

    cycles = None if args.forever else args.cycles
    try:
        asyncio.run(run_loop(cfg, conn, args.interval, cycles, args.as_of, show))
    except KeyboardInterrupt:
        print("stopped")
    return 0


def _cmd_request(cfg: Config, args: argparse.Namespace) -> int:
    from .discovery import enqueue_request

    conn = connect(cfg.db_path)
    init_db(conn, cfg.limits.starting_cash_usd)
    req_id = enqueue_request(conn, args.symbol, args.question)
    print(f"research request #{req_id} queued for {args.symbol.upper()}")
    return 0


def _cmd_schemas(cfg: Config, args: argparse.Namespace) -> int:
    from pathlib import Path

    from .contracts import MESSAGE_FORMATS

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for name, model in MESSAGE_FORMATS.items():
        (out / f"{name}.json").write_text(json.dumps(model.model_json_schema(), indent=2) + "\n")
        print(f"wrote {out / name}.json")
    return 0


def _cmd_setup(cfg: Config, args: argparse.Namespace) -> int:
    from .investor import SetupError, set_profile

    conn = connect(cfg.db_path)
    init_db(conn, cfg.limits.starting_cash_usd)
    try:
        profile = set_profile(conn, args.capital, args.income, args.filing, args.state_tax_pct)
    except (SetupError, ValueError) as exc:
        print(f"setup error: {exc}", file=sys.stderr)
        return 2
    t = profile.tax
    print(f"capital:    ${profile.capital_usd:,.2f} available to the committee")
    print(f"tax:        short-term {t.short_term_pct:.1f}%, long-term {t.long_term_pct:.1f}% "
          f"(federal {t.ordinary_bracket_pct:.0f}% / {t.ltcg_pct:.0f}% LTCG"
          f"{f' + {t.niit_pct}% NIIT' if t.niit_pct else ''}"
          f"{f' + {t.state_pct}% state' if t.state_pct else ''})")
    print(f"basis:      {t.basis}")
    return 0


def _cmd_status(cfg: Config, args: argparse.Namespace) -> int:
    from .discovery import queue_depth, spent_today_usd
    from .papers import count_by_state

    conn = connect(cfg.db_path)
    init_db(conn, cfg.limits.starting_cash_usd)
    counts = count_by_state(conn)
    print("papers:     " + (", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "none"))
    print(f"queue:      {queue_depth(conn)} packet(s) awaiting Assessment (cap {cfg.queue_cap})")
    open_req = conn.execute("SELECT COUNT(*) FROM research_requests WHERE status = 'open'"
                            ).fetchone()[0]
    print(f"requests:   {open_req} open")
    for agent, spec in cfg.agents.items():
        print(f"spend {agent:<12} ${spent_today_usd(conn, agent):.4f} of "
              f"${spec.daily_budget_usd:.2f} today")
    print(f"cash:       ${cash_balance_cents(conn) / 100:,.2f}")
    from .investor import get_profile

    profile = get_profile(conn)
    if profile:
        t = profile.tax
        print(f"investor:   capital ${profile.capital_usd:,.0f}, {t.filing_status}, tax "
              f"{t.short_term_pct:.1f}% short / {t.long_term_pct:.1f}% long"
              + (" (assumed)" if t.assumed else ""))
    else:
        print("investor:   not set up (run `qfii setup --capital ...`)")
    return 0


def _cmd_serve(cfg: Config, args: argparse.Namespace) -> int:
    import uvicorn

    if args.agent == "tools":
        from .tools.server import build_tool_server

        port = args.port or TOOLS_PORT
        print(f"Qfii MCP tools on http://{args.host}:{port}/mcp")
        build_tool_server(cfg).run("streamable-http", host=args.host, port=port)
        return 0

    from .agents.research.a2a import build_research_app
    from .agents.research.agent import ResearchAgent

    port = args.port or RESEARCH_PORT
    url = f"http://{args.host}:{port}/"
    app = build_research_app(ResearchAgent(cfg), url, cfg.db_path if args.save else None)
    print(f"Qfii Research agent (A2A) on {url}  card: {url}.well-known/agent-card.json")
    uvicorn.run(app, host=args.host, port=port, log_level="warning")
    return 0


def _cmd_ask(cfg: Config, args: argparse.Namespace) -> int:
    from .a2a_client import AgentError, ask_agent

    payload = {"symbol": args.symbol}
    if args.as_of:
        payload["as_of"] = args.as_of.isoformat()
    try:
        reply = asyncio.run(ask_agent(args.url, payload))
    except AgentError as exc:
        print(f"agent error: {exc}", file=sys.stderr)
        return 1
    _print_packet(reply["packet"], int(reply["paper_id"]) if reply.get("paper_id") else None)
    return 0


def main(argv: list[str] | None = None) -> int:
    import logging

    logging.getLogger("httpx").setLevel(logging.WARNING)
    parser = argparse.ArgumentParser(prog="qfii")
    parser.add_argument("--version", action="version", version=f"qfii {__version__}")
    parser.add_argument("--config", help="local TOML overriding the packaged defaults")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="create the database and seed starting cash")
    sub.add_parser("config", help="print the effective configuration")

    p = sub.add_parser("research", help="run the Research agent once, locally")
    p.add_argument("symbol")
    p.add_argument("--as-of", type=date.fromisoformat, help="information cutoff YYYY-MM-DD")
    p.add_argument("--question")
    p.add_argument("--save", action="store_true", help="store the packet in the database")
    p.add_argument("--json", action="store_true", help="print the full packet as JSON")

    p = sub.add_parser("debate", help="research a stock, then run the committee debate")
    p.add_argument("symbol")
    p.add_argument("--as-of", type=date.fromisoformat, help="information cutoff YYYY-MM-DD")
    p.add_argument("--save", action="store_true", help="store packet, debate and memories")

    p = sub.add_parser("risk-check", help="ask Risk & Validation directly about a stock")
    p.add_argument("symbol")
    p.add_argument("--question", action="append", required=True,
                   help="what Risk should check (repeatable)")
    p.add_argument("--as-of", type=date.fromisoformat)

    p = sub.add_parser("bench", help="benchmark the committee model on an identical debate")
    p.add_argument("symbol")
    p.add_argument("--as-of", type=date.fromisoformat)
    p.add_argument("--runs", type=int, default=1)
    p.add_argument("--label", help="report file prefix, e.g. luna or qwen27b")
    p.add_argument("--out", default="data/bench")
    p.add_argument("--verbose", action="store_true", help="print each round")

    p = sub.add_parser("ui", help="web page to start and watch committee debates")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=3000)

    p = sub.add_parser("data", help="download market data into local files")
    p.add_argument("action", choices=["fetch"])
    p.add_argument("--symbols", nargs="*", default=[],
                   help="default: the universe plus the benchmark")
    p.add_argument("--prices", action="store_true")
    p.add_argument("--macro", action="store_true")
    p.add_argument("--fundamentals", action="store_true")
    p.add_argument("--range", default="5y", help="Yahoo range, e.g. 2y, 5y, max")

    p = sub.add_parser("discover", help="run the continuous Research discovery loop")
    p.add_argument("--cycles", type=int, default=1, help="cycles to run (default 1)")
    p.add_argument("--forever", action="store_true", help="run until Ctrl-C")
    p.add_argument("--interval", type=float, default=300, help="seconds between cycles")
    p.add_argument("--as-of", type=date.fromisoformat, help="information cutoff YYYY-MM-DD")

    p = sub.add_parser("request", help="queue a targeted research request")
    p.add_argument("symbol")
    p.add_argument("--question", required=True)

    sub.add_parser("status", help="pipeline, queue and budget overview")

    p = sub.add_parser("schemas", help="write JSON schemas of every agent message")
    p.add_argument("--out", default="docs/schemas")

    p = sub.add_parser("setup", help="set the capital to invest and the tax situation")
    p.add_argument("--capital", type=float, required=True, help="dollars the committee may use")
    p.add_argument("--income", type=float, help="annual income, sets the tax bracket")
    p.add_argument("--filing", choices=["single", "married_joint"], default="single")
    p.add_argument("--state-tax-pct", type=float, default=0.0)

    p = sub.add_parser("serve", help="run an agent or the tool server")
    p.add_argument("agent", choices=["research", "tools"])
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int)
    p.add_argument("--save", action="store_true", help="store packets in the database")

    p = sub.add_parser("ask", help="send a research request to an A2A agent")
    p.add_argument("symbol")
    p.add_argument("--url", default=f"http://127.0.0.1:{RESEARCH_PORT}/")
    p.add_argument("--as-of", type=date.fromisoformat)

    args = parser.parse_args(argv)
    handlers = {"init": _cmd_init, "config": _cmd_config, "research": _cmd_research, "debate": _cmd_debate,
                "data": _cmd_data, "discover": _cmd_discover, "request": _cmd_request,
                "status": _cmd_status, "setup": _cmd_setup,
                "schemas": _cmd_schemas, "bench": _cmd_bench, "risk-check": _cmd_risk_check,
                "ui": _cmd_ui,
                "serve": _cmd_serve, "ask": _cmd_ask}
    try:
        cfg = load_config(args.config)
        return handlers[args.command](cfg, args)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
