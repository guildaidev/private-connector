"""Customer-controlled HTTP method restrictions."""

import re

from .models import ProtocolError


def parse_blocked_methods(value: object) -> frozenset[str]:
    if not isinstance(value, (list, tuple)) or any(
        not isinstance(method, str) or not re.fullmatch(r"[A-Za-z]{1,16}", method)
        for method in value
    ):
        raise ProtocolError(
            "Setting blocked_methods must be a list of HTTP method names"
        )
    return frozenset(method.upper() for method in value)
