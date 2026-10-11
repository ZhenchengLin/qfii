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
          f"tax={pf.short_term_tax_pct}%/{pf.long_term_tax_pct}% "
          f"(long-term after {pf.long_term_after_days}d)")
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


def _cmd_debate(cfg: Config, args: argparse.Namespace) -> int:
    from .committee.runner import run_paper

    conn = connect(cfg.db_path)
    init_db(conn, cfg.limits.starting_cash_usd)
    run = asyncio.run(run_paper(cfg, conn, args.symbol, args.as_of, save=args.save))
    print(f"{run.packet.symbol} as of {run.packet.as_of}: {len(run.packet.observations)} "
          f"research observations, flags: {', '.join(run.packet.quality_flags)}")
    for rec in run.debate.rounds:
        print(f"\n-- round {rec.round} --")
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
            print(f"  report from {rep['team']}: {rep['report'].get('note', rep['report'])}")
    size = f" ${run.debate.size_usd:,.0f}" if run.debate.size_usd else ""
    print(f"\nOUTCOME: {run.debate.outcome}{size} after {len(run.debate.rounds)} round(s)"
          + (f"  (paper #{run.paper_id}, debate #{run.debate_id})" if run.paper_id else ""))
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
                "serve": _cmd_serve, "ask": _cmd_ask}
    try:
        cfg = load_config(args.config)
        return handlers[args.command](cfg, args)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
