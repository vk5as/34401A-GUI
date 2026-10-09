"""Putting a Connection back in step after a reply went missing, came late or came back as noise.

A Meter answers every query in order, so one reply that is missed at the wrong moment leaves all the later ones off by
one: the next query would read the *previous* query's answer. A device clear cannot be relied on to fix that (a socket
has none, and a late reply may still be on its way). What can be relied on is a query whose answer is known: once that
answer has been read, everything the Meter sent before it has been read too, so nothing stale is left.
"""

import logging
from dataclasses import dataclass

from agilent34401a.errors import MalformedReplyError, ResyncFailedError, TransportTimeoutError
from agilent34401a.transport import Transport

_LOG = logging.getLogger(__name__)

DEFAULT_PATIENCE = 3
"""How many times reading may time out while waiting for the Meter to catch up before it is given up on."""
DEFAULT_SETTLE_S = 0.05
_MAX_STALE_REPLIES = 100  # a Meter that answers a hundred times and never with the sentinel is not catching up


@dataclass(frozen=True)
class Sentinel:
    """A query and the answer the Meter is known to give: the Meter's own identity does nicely."""

    command: str
    reply: str

    def matches(self, reply: str) -> bool:
        return reply.strip() == self.reply.strip()


def resynchronise(
    transport: Transport,
    sentinel: Sentinel,
    *,
    patience: int = DEFAULT_PATIENCE,
    settle_s: float = DEFAULT_SETTLE_S,
) -> None:
    """Bring `transport` back in step with the Meter, or raise `ResyncFailedError` if the Meter does not answer.

    The Transport is cleared, then `sentinel.command` is sent and replies are read and thrown away until
    `sentinel.reply` arrives. Each wait that times out counts against `patience`, because a late reply is worth waiting
    for. Afterwards the Connection is read once more for a moment, in case what was thrown away was a stale copy of the
    sentinel's own reply and the real one is still to come. Other errors from the Transport (a broken Connection, say)
    are not caught: they mean the Connection is lost. The Transport's timeout is restored whatever happens.
    """
    transport.clear()
    transport.write(sentinel.command)
    timeout = transport.timeout
    try:
        _await_sentinel(transport, sentinel, patience)
        transport.timeout = settle_s
        _discard_until_quiet(transport, sentinel)
    finally:
        transport.timeout = timeout


def _await_sentinel(transport: Transport, sentinel: Sentinel, patience: int) -> None:
    waits = 0
    stale = 0
    while waits < patience and stale <= _MAX_STALE_REPLIES:
        try:
            reply = transport.read()
        except TransportTimeoutError:
            waits += 1
            continue
        except MalformedReplyError:
            stale += 1  # noise, or a reply too mangled to read: it is stale all the same
            continue
        if sentinel.matches(reply):
            return
        _LOG.debug("Discarded a stale reply while resynchronising: %r", reply)
        stale += 1
    message = f"The Meter did not answer {sentinel.command} while the Connection was being resynchronised"
    raise ResyncFailedError(message)


def _discard_until_quiet(transport: Transport, sentinel: Sentinel) -> None:
    for _ in range(_MAX_STALE_REPLIES):
        try:
            transport.read()
        except TransportTimeoutError:
            return
        except MalformedReplyError:
            continue
        _LOG.debug("Discarded a late copy of %s's reply while resynchronising", sentinel.command)
