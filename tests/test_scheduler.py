import asyncio

import pytest

from qfii.scheduler import ModelScheduler


def make(sched, log, model, delay=0.001):
    async def fn():
        log.append(("start", model))
        await asyncio.sleep(delay)
        log.append(("end", model))
        return model
    return sched.run(model, fn)


async def test_one_model_resident_and_batches_drained_before_swap():
    sched = ModelScheduler(max_concurrent=1, settle_s=0.01)
    unloaded = []

    async def unload(key):
        unloaded.append(key)

    sched.register_unloader("A", unload)
    sched.register_unloader("B", unload)
    log = []
    # interleaved arrivals, like three debates each wanting high-tier then max-tier
    calls = [make(sched, log, m) for m in ["A", "B", "A", "B", "A", "B"]]
    assert await asyncio.gather(*calls) == ["A", "B", "A", "B", "A", "B"]
    assert sched.stats.order == ["A", "A", "A", "B", "B", "B"]
    assert sched.stats.swaps == 1 and unloaded == ["A"]
    # never two calls at once
    depth = 0
    for kind, _ in log:
        depth += 1 if kind == "start" else -1
        assert depth <= 1


async def test_settle_window_lets_late_calls_join_the_batch():
    sched = ModelScheduler(max_concurrent=1, settle_s=0.05)
    log = []

    async def late_a():
        await asyncio.sleep(0.01)  # arrives while B is already waiting
        return await make(sched, log, "A")

    await asyncio.gather(make(sched, log, "A"), make(sched, log, "B"), late_a())
    assert sched.stats.order == ["A", "A", "B"]


async def test_max_batch_prevents_starvation():
    sched = ModelScheduler(max_concurrent=1, settle_s=0.0, max_batch=2)

    async def stream_a():
        for _ in range(6):
            await make(sched, [], "A")

    await asyncio.gather(stream_a(), make(sched, [], "B", delay=0))
    first_b = sched.stats.order.index("B")
    assert first_b <= 3  # B got in after at most a couple of A batches


async def test_non_exclusive_mode_runs_models_side_by_side():
    sched = ModelScheduler(max_concurrent=2, settle_s=0.0, exclusive=False)
    log = []
    await asyncio.gather(make(sched, log, "A", 0.02), make(sched, log, "B", 0.02))
    assert log[:2] == [("start", "A"), ("start", "B")] and sched.stats.swaps == 0


async def test_failure_releases_the_slot():
    sched = ModelScheduler(max_concurrent=1, settle_s=0.0)

    async def boom():
        raise RuntimeError("model crashed")

    with pytest.raises(RuntimeError):
        await sched.run("A", boom)
    assert await make(sched, [], "A") == "A"


async def test_debates_in_lockstep_swap_once_per_round(cfg):
    """3 papers x 3 rounds: high-tier (aggressive, conservative) and max-tier (neutral).
    The resident model also carries over round boundaries: round 2's neutral ballots
    run on the max-tier model still loaded from round 1, so 27 calls need 3 swaps."""
    from dataclasses import replace

    from qfii.committee.engine import Dossier, run_debate
    from qfii.committee.members import build_members
    from qfii.db import connect, init_db
    from qfii.portfolio import snapshot
    from qfii.scheduler import scheduler_for_loop

    conn = connect(":memory:")
    init_db(conn, 100_000)
    snap = snapshot(conn, cfg, __import__("datetime").date(2026, 10, 9)).model_dump(mode="json")

    class Teams:
        async def report(self, team, requests, dossier):
            return {"team": team}

    cfg = replace(cfg, scheduler=replace(cfg.scheduler, settle_s=0.02))
    debates = [run_debate(Dossier(s, {}, {}, snap), build_members(cfg), Teams(), cfg.committee)
               for s in ("AAPL", "MSFT", "NVDA")]
    results = await asyncio.gather(*debates)
    assert all(r.outcome == "invest" for r in results)
    order = scheduler_for_loop(cfg.scheduler).stats.order
    runs = [m for i, m in enumerate(order) if i == 0 or order[i - 1] != m]
    assert len(order) == 27
    assert [r.split(":")[-1] for r in runs] == ["fake-strong", "fake-strongest", "fake-strong",
                                                "fake-strongest"]
