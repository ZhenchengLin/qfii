"""One-model-at-a-time scheduler for local inference on limited memory.

Every LLM call goes through ModelScheduler.run(model_key, fn). Only one model is
resident at a time. The scheduler keeps serving the resident model while it has
work, and only when that model goes idle (after a short settle window, so calls
that are about to arrive can join the batch) does it unload it and switch to the
model with the most waiting calls.

Because each debater's "memory" is just its prompt, the three committee members
share a loaded model at no cost. Running many papers' debates concurrently lets
one load serve every paper's round: all high-tier ballots for round r, then one
swap, then all max-tier ballots for round r.
"""

from __future__ import annotations

import asyncio
import weakref
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TypeVar

T = TypeVar("T")
Unloader = Callable[[str], Awaitable[None]]


@dataclass
class SchedulerStats:
    swaps: int = 0
    calls: Counter = field(default_factory=Counter)
    order: list[str] = field(default_factory=list)   # model of each call, in start order


class ModelScheduler:
    def __init__(self, max_concurrent: int = 1, settle_s: float = 0.15, max_batch: int = 64,
                 exclusive: bool = True):
        self.max_concurrent = max_concurrent
        self.settle_s = settle_s
        self.max_batch = max_batch
        self.exclusive = exclusive
        self.current: str | None = None
        self.stats = SchedulerStats()
        self._unloaders: dict[str, Unloader] = {}
        self._waiting: Counter = Counter()
        self._active = 0
        self._batch = 0
        self._switching = False
        self._cond = asyncio.Condition()

    def register_unloader(self, model_key: str, unload: Unloader) -> None:
        self._unloaders[model_key] = unload

    def _others_waiting(self) -> bool:
        return any(n for m, n in self._waiting.items() if m != self.current)

    def _may_start(self, model: str) -> bool:
        if self._switching or self._active >= self.max_concurrent:
            return False
        if self.current is None or not self.exclusive:
            return True
        if model != self.current:
            return False
        # stop feeding the resident model once its batch is used up and others wait
        return not (self._batch >= self.max_batch and self._others_waiting())

    def _should_switch_to(self, model: str) -> bool:
        if self._switching or self._active or model == self.current:
            return False
        if self._waiting[self.current] and self._batch < self.max_batch:
            return False
        busiest = max((m for m in self._waiting if m != self.current and self._waiting[m]),
                      key=lambda m: self._waiting[m])
        return busiest == model

    async def run(self, model: str, fn: Callable[[], Awaitable[T]]) -> T:
        async with self._cond:
            self._waiting[model] += 1
            try:
                while not self._may_start(model):
                    if self.exclusive and self._should_switch_to(model):
                        try:  # settle: let more calls for the resident model arrive
                            await asyncio.wait_for(self._cond.wait(), self.settle_s)
                            continue
                        except TimeoutError:
                            if not self._should_switch_to(model):
                                continue
                        previous, self._switching = self.current, True
                        self._cond.release()
                        try:
                            if previous and previous in self._unloaders:
                                await self._unloaders[previous](previous)
                        finally:
                            await self._cond.acquire()
                            self.current, self._batch, self._switching = model, 0, False
                            self.stats.swaps += 1
                            self._cond.notify_all()
                        continue
                    await self._cond.wait()
            finally:
                self._waiting[model] -= 1
            if self.current is None:
                self.current = model
            self._active += 1
            self._batch += 1
            self.stats.calls[model] += 1
            self.stats.order.append(model)
        try:
            return await fn()
        finally:
            async with self._cond:
                self._active -= 1
                self._cond.notify_all()


@dataclass(frozen=True)
class SchedulerSettings:
    exclusive: bool = True
    max_concurrent: int = 1
    settle_s: float = 0.15
    max_batch: int = 64


# one scheduler per event loop (asyncio primitives cannot cross loops)
_SCHEDULERS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def scheduler_for_loop(settings: SchedulerSettings) -> ModelScheduler:
    loop = asyncio.get_running_loop()
    sched = _SCHEDULERS.get(loop)
    if sched is None:
        sched = ModelScheduler(settings.max_concurrent, settings.settle_s, settings.max_batch,
                               settings.exclusive)
        _SCHEDULERS[loop] = sched
    return sched
