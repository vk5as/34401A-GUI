"""Exceptions raised when talking to a Meter."""


class MeterError(Exception):
    """Base class for every failure talking to, or understanding, a Meter."""


class TransportError(MeterError):
    """The Transport to the Meter failed or is closed."""


class TransportTimeoutError(TransportError):
    """The Meter did not answer within the Transport's timeout."""


class MalformedReplyError(MeterError):
    """The Meter's reply could not be understood."""


class UnrecognisedIdentityError(MeterError):
    """The device that answered `*IDN?` is not a 34401A."""
