"""What the parts of the Simulator share: reading a number the way the Meter does, and writing one the way it replies."""

import math


def parse_number(word: str) -> float | None:
    """Return `word` as a finite number, or None when it is not one."""
    try:
        value = float(word)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def number_reply(value: float) -> str:
    """Return `value` as the Meter writes a number: a sign, eight decimals and an exponent."""
    return f"{value + 0.0:+.8E}"
