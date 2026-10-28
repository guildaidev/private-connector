"""Request and response objects exchanged with Guild."""

from __future__ import annotations

from base64 import b64decode
from dataclasses import dataclass, field
from datetime import UTC, datetime
from math import isfinite
from uuid import UUID

import httpx

from .urls import parse_origin


class ProtocolError(ValueError):
    """An invalid configuration or Guild response."""


@dataclass(frozen=True)
class Destination:
    id: str
    origin: str
    url: httpx.URL = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        try:
            if not isinstance(self.id, str) or not self.id or len(self.id) > 255:
                raise ValueError("Invalid target ID")
            object.__setattr__(self, "url", parse_origin(self.origin))
        except ValueError:
            raise ProtocolError(
                "A destination must have an ID and an HTTP(S) origin"
            ) from None


@dataclass(frozen=True)
class Delivery:
    id: str
    target_id: str
    method: str
    endpoint: str
    query: str | None
    headers: dict[str, str]
    body: bytes
    expires_at: datetime
    timeout: httpx.Timeout = field(default_factory=lambda: httpx.Timeout(30))
    stream_response: bool = False

    @classmethod
    def from_payload(cls, payload: dict[str, object]) -> Delivery:
        try:
            text_fields = (
                "id",
                "target_id",
                "method",
                "endpoint",
                "body_base64",
                "expires_at",
            )
            stream_response = payload.get("stream_response", False)
            if type(stream_response) is not bool:
                raise ValueError("Invalid streaming mode")
            if (
                set(payload)
                != {*text_fields, "type", "headers", "query", "timeout"}
                | ({"stream_response"} if "stream_response" in payload else set())
                or payload["type"] != "request"
            ):
                raise ValueError("Invalid request fields")
            values: dict[str, str] = {}
            for field in text_fields:
                value = payload[field]
                if not isinstance(value, str):
                    raise ValueError("Invalid field type")
                values[field] = value
            query = payload["query"]
            if query is not None and not isinstance(query, str):
                raise ValueError("Invalid query")
            headers = payload["headers"]
            if not isinstance(headers, dict) or not all(
                isinstance(key, str) and isinstance(value, str)
                for key, value in headers.items()
            ):
                raise ValueError("Invalid headers")
            expires_at = datetime.fromisoformat(values["expires_at"])
            if expires_at.utcoffset() is None:
                raise ValueError("Expiry must include a timezone")
            timeouts = payload["timeout"]
            if (
                not isinstance(timeouts, dict)
                or set(timeouts) != {"connect", "read", "write", "pool"}
                or any(
                    value is not None
                    and (
                        type(value) not in (int, float)
                        or not isfinite(value)
                        or value < 0
                    )
                    for value in timeouts.values()
                )
            ):
                raise ValueError("Invalid HTTP timeouts")
            return cls(
                id=str(UUID(values["id"])),
                target_id=values["target_id"],
                method=values["method"],
                endpoint=values["endpoint"],
                query=query,
                headers=dict(headers),
                body=b64decode(values["body_base64"], validate=True),
                expires_at=expires_at,
                timeout=httpx.Timeout(**timeouts),
                stream_response=stream_response,
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ProtocolError("Invalid delivery from Guild tunnel gateway") from error

    def is_expired(self) -> bool:
        return datetime.now(UTC) >= self.expires_at


@dataclass(frozen=True)
class ProxyResponse:
    delivery_id: str
    status: int
    headers: dict[str, str]
    body: bytes
    error_code: str | None = None
