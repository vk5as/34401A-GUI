"""Faults the socket Simulator can be told to inject, so the application can be shown a misbehaving Meter (ADR-0003).

A `Fault` describes one kind of misbehaviour and which lines it hits; a `FaultPlan` holds the faults that are armed and
decides, line by line, what the server does instead of answering normally. Everything is driven by counters and seeds
rather than chance, so a test that injects a fault gets the same behaviour on every run.
"""

import random
import re
import threading
from dataclasses import dataclass
from enum import Enum


class Effect(Enum):
    """What a fault does to the reply (or the Connection) when it hits."""

    SLOW = "slow"
    DROP = "drop"
    RESET = "reset"
    NOISE = "noise"
    TRUNCATED = "truncated"
    UNTERMINATED = "unterminated"
    WRONG_TYPE = "wrong-type"


@dataclass(frozen=True)
class Fault:
    """One kind of misbehaviour and the lines it hits.

    A line is *matched* when `command` (a regular expression, searched for anywhere in the line, ignoring case) is found
    in it. A fault with no `command` matches every query, which is every line the Meter would answer. A fault with
    one also matches commands that get no answer, which only a drop or reset can do anything about.
    The first `after` matches are left alone; after that the fault hits every `every`-th match, `times` times in all
    (`None` is without end). Matches are counted across every client of the server.
    """

    effect: Effect
    delay_s: float = 0.0
    command: str | None = None
    after: int = 0
    times: int | None = 1
    every: int = 1
    seed: int = 0

    def __post_init__(self) -> None:
        if self.delay_s < 0:
            message = f"A delay cannot be negative, got {self.delay_s}"
            raise ValueError(message)
        if self.after < 0 or self.every < 1 or (self.times is not None and self.times < 0):
            message = "A fault needs after >= 0, every >= 1 and times >= 0 (or None)"
            raise ValueError(message)
        if self.command is not None:
            re.compile(self.command)  # a pattern that cannot compile should fail here, not in the server's thread

    @classmethod
    def slow(
        cls, delay_s: float, *, command: str | None = None, after: int = 0, times: int | None = 1, every: int = 1
    ) -> "Fault":
        """Hold the reply back for `delay_s` seconds, as a Meter does that is busy or a link that is poor."""
        return cls(Effect.SLOW, delay_s=delay_s, command=command, after=after, times=times, every=every)

    @classmethod
    def drop(cls, *, command: str | None = None, after: int = 0, times: int | None = 1, every: int = 1) -> "Fault":
        """Hang up instead of answering: the Meter has acted on the line, then the Connection is closed."""
        return cls(Effect.DROP, command=command, after=after, times=times, every=every)

    @classmethod
    def reset(cls, *, command: str | None = None, after: int = 0, times: int | None = 1, every: int = 1) -> "Fault":
        """Like `drop`, but the Connection is reset rather than closed in an orderly way."""
        return cls(Effect.RESET, command=command, after=after, times=times, every=every)

    @classmethod
    def noise(
        cls, *, seed: int = 0, command: str | None = None, after: int = 0, times: int | None = 1, every: int = 1
    ) -> "Fault":
        """Answer with bytes that are not text; the same `seed` gives the same bytes every time."""
        return cls(Effect.NOISE, seed=seed, command=command, after=after, times=times, every=every)

    @classmethod
    def truncated(cls, *, command: str | None = None, after: int = 0, times: int | None = 1, every: int = 1) -> "Fault":
        """Answer with only the first half of the reply (the line still ends)."""
        return cls(Effect.TRUNCATED, command=command, after=after, times=times, every=every)

    @classmethod
    def unterminated(
        cls, *, command: str | None = None, after: int = 0, times: int | None = 1, every: int = 1
    ) -> "Fault":
        """Answer without ending the line, so the next reply runs on from this one."""
        return cls(Effect.UNTERMINATED, command=command, after=after, times=times, every=every)

    @classmethod
    def wrong_type(
        cls, *, command: str | None = None, after: int = 0, times: int | None = 1, every: int = 1
    ) -> "Fault":
        """Answer a query that has a number for its reply with text, and any other query with a number."""
        return cls(Effect.WRONG_TYPE, command=command, after=after, times=times, every=every)


@dataclass(frozen=True)
class Delivery:
    """What the server should do with one line's reply: wait, then send `payload` (if any), then maybe hang up."""

    payload: bytes | None
    delay_s: float = 0.0
    hang_up: bool = False
    reset: bool = False


_NOISE_LENGTH = 12
_NOISE_BYTES = bytes(byte for byte in range(0x80, 0x100))  # every one of them is invalid ASCII


def _wrong_type(reply: str) -> str:
    """Return a reply of the other type: text for a number, a number for anything else."""
    try:
        float(reply)
    except ValueError:
        return "+9.99000000E+00"
    return '"NOT A NUMBER"'


class _Armed:
    """A fault together with how often it has matched and hit so far."""

    def __init__(self, fault: Fault) -> None:
        self.fault = fault
        self._pattern = re.compile(fault.command, re.IGNORECASE) if fault.command is not None else None
        self._matches = 0
        self._hits = 0
        # Reproducible noise for tests, not security.
        self._random = random.Random(fault.seed)  # noqa: S311  # nosec B311

    def garble(self, reply: str) -> bytes:
        """Return the bytes this fault sends in place of `reply`."""
        match self.fault.effect:
            case Effect.NOISE:
                body = bytes(self._random.choice(_NOISE_BYTES) for _ in range(_NOISE_LENGTH))
                return body + b"\n"
            case Effect.TRUNCATED:
                return reply[: max(1, len(reply) // 2)].encode("ascii", errors="replace") + b"\n"
            case Effect.UNTERMINATED:
                return reply.encode("ascii", errors="replace")
            case _:
                return _wrong_type(reply).encode("ascii", errors="replace") + b"\n"

    def hits(self, line: str, *, is_query: bool) -> bool:
        """Count this line if the fault matches it, and say whether the fault strikes now."""
        if self._pattern is None:
            if not is_query:
                return False
        elif not self._pattern.search(line):
            return False
        self._matches += 1
        skipped = self._matches - self.fault.after
        if skipped < 1 or skipped % self.fault.every != 0:
            return False
        if self.fault.times is not None and self._hits >= self.fault.times:
            return False
        self._hits += 1
        return True


class FaultPlan:
    """The faults armed on a server. Safe to use from the server's many threads."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._armed: list[_Armed] = []

    def add(self, fault: Fault) -> None:
        with self._lock:
            self._armed.append(_Armed(fault))

    def clear(self) -> None:
        with self._lock:
            self._armed.clear()

    def deliver(self, line: str, reply: str | None) -> Delivery:
        """Decide how `reply` (None when `line` was not a query) reaches the client.

        Delays add up; if several faults garble the reply, the last one armed has the say.
        """
        payload = None if reply is None else f"{reply}\n".encode("ascii", errors="replace")
        delay_s = 0.0
        hang_up = False
        reset = False
        with self._lock:
            struck = [armed for armed in self._armed if armed.hits(line, is_query=reply is not None)]
            for armed in struck:
                effect = armed.fault.effect
                if effect is Effect.SLOW:
                    delay_s += armed.fault.delay_s
                elif effect is Effect.DROP:
                    hang_up = True
                elif effect is Effect.RESET:
                    hang_up = reset = True
                elif reply is not None:
                    payload = armed.garble(reply)
        return Delivery(None if hang_up else payload, delay_s, hang_up, reset)
