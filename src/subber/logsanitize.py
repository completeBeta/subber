"""Log sanitizer — strips control characters and newlines from user input."""

import re

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def sanitize_log(value: str) -> str:
    """Strip newlines, carriage returns, and control characters from log input."""
    if not isinstance(value, str):
        return str(value)
    value = value.replace("\n", " ").replace("\r", " ")
    value = _CONTROL_CHARS.sub(" ", value)
    return value
