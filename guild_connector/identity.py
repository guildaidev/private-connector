"""Private tunnel state and HTTPS enrollment/token renewal."""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import random
import ssl
import tempfile
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from unicodedata import category
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4

import httpx

from .models import ProtocolError

log = logging.getLogger(__name__)


def control_plane_api_base(url: str) -> str:
    try:
        base = urlsplit(url)
        port = base.port
        valid = (
            base.scheme == "https"
            and bool(base.hostname)
            and (port is None or port > 0)
            and base.username is None
            and base.password is None
            and not base.query
            and not base.fragment
            and "?" not in url
            and "#" not in url
            and not any(category(char) == "Cc" for char in url)
        )
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise ProtocolError("Control plane URL must be an HTTPS API base")
    return urlunsplit(
        (base.scheme, base.netloc, base.path.rstrip("/") or "/v1", "", "")
    )


def http_error_message(error: httpx.HTTPError, operation: str) -> str:
    if isinstance(error, httpx.HTTPStatusError):
        return f"{operation} failed with HTTP {error.response.status_code}"
    if isinstance(error, httpx.TimeoutException):
        return f"{operation} request timed out"
    cause: BaseException | None = error
    while cause is not None:
        if isinstance(cause, ssl.SSLError):
            return f"{operation} TLS handshake failed; check the CA and hostname"
        cause = cause.__cause__
    if isinstance(error, httpx.ConnectError):
        return (
            f"{operation} connection failed; check the service URL and network access"
        )
    if isinstance(error, httpx.RemoteProtocolError):
        return f"{operation} received an invalid HTTP response"
    return f"{operation} HTTP transfer failed; check network access"


def atomic_write(path: Path, value: str) -> None:
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".identity-")
    try:
        with os.fdopen(fd, "w") as output:
            output.write(value)
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextmanager
def identity_lock(directory: Path):
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink() or directory.stat().st_mode & 0o077:
        raise ProtocolError("Identity directory must be private (mode 0700)")
    fd = os.open(directory / ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ProtocolError(
                "Another connector is using this identity directory"
            ) from None
        yield
    finally:
        os.close(fd)


@dataclass(repr=False)
class Identity:
    connector_id: str
    tunnel_id: str | None
    control_plane_url: str
    request_id: str
    enrollment_token: str | None
    connector_token: str | None = None
    expires_at: str | None = None
    gateway_url: str | None = None

    @classmethod
    def create(
        cls, connector_id: str, control_plane_url: str, enrollment_token: str
    ) -> Identity:
        connector_id = str(UUID(connector_id))
        if (
            not isinstance(enrollment_token, str)
            or not enrollment_token.strip()
            or any(category(char) == "Cc" for char in enrollment_token)
        ):
            raise ProtocolError("Invalid enrollment token")
        return cls(
            connector_id,
            None,
            control_plane_url,
            str(uuid4()),
            enrollment_token,
        )

    def credential(self) -> tuple[str, datetime]:
        if not self.tunnel_id or not self.connector_token or not self.expires_at:
            raise ProtocolError("Run connector enrollment before starting the tunnel")
        return self.connector_token, datetime.fromisoformat(self.expires_at)

    def seconds_until_renewal(self) -> float:
        if not self.expires_at:
            return 0
        return max(
            0,
            (
                datetime.fromisoformat(self.expires_at) - datetime.now(UTC)
            ).total_seconds()
            - 7 * 24 * 60 * 60,
        )

    def save(self, directory: Path) -> None:
        atomic_write(directory / "identity.json", json.dumps(asdict(self)))

    async def issue(
        self, directory: Path, *, control_plane_ca: str | None = None
    ) -> bool:
        api_base = control_plane_api_base(self.control_plane_url)
        action = "enroll" if self.enrollment_token else "renew"
        credential = self.enrollment_token or self.connector_token
        if not credential:
            raise ProtocolError("Tunnel has not been enrolled")
        if not self.enrollment_token and (
            not self.expires_at
            or datetime.fromisoformat(self.expires_at) <= datetime.now(UTC)
        ):
            raise ProtocolError(
                "Connector credential expired; create a new enrollment in Guild"
            )
        async with httpx.AsyncClient(
            verify=ssl.create_default_context(cafile=control_plane_ca),
            trust_env=False,
            follow_redirects=False,
            timeout=15,
        ) as client:
            response = await client.post(
                f"{api_base}/customer_connectors/{action}",
                headers={"Authorization": f"Bearer {credential}"},
                json={"request_id": self.request_id} if action == "enroll" else None,
            )
        if response.status_code in {401, 403, 404, 409}:
            raise ProtocolError(
                f"Connector {action} request was rejected with HTTP {response.status_code}; "
                "check the tunnel in Guild"
            )
        response.raise_for_status()
        try:
            result = response.json()
        except ValueError:
            raise ProtocolError("Invalid enrollment response") from None
        if not isinstance(result, dict) or result.get("status") != "issued":
            raise ProtocolError("Invalid enrollment response")
        raw_tunnel_id = result.get("tunnel_id")
        if not isinstance(raw_tunnel_id, str):
            raise ProtocolError("Invalid tunnel ID in enrollment response")
        try:
            parsed_tunnel_id = UUID(raw_tunnel_id)
        except ValueError:
            raise ProtocolError("Invalid tunnel ID in enrollment response") from None
        if parsed_tunnel_id.int == 0:
            raise ProtocolError("Invalid tunnel ID in enrollment response")
        tunnel_id = str(parsed_tunnel_id)
        if self.tunnel_id is not None and tunnel_id != self.tunnel_id:
            raise ProtocolError("Enrollment response does not match this tunnel")
        value = result.get("connector_token")
        if (
            not isinstance(value, str)
            or not value
            or any(c.isspace() or category(c) == "Cc" for c in value)
        ):
            raise ProtocolError("Invalid connector token response")
        raw_expiry = result.get("expires_at")
        if not isinstance(raw_expiry, str):
            raise ProtocolError("Invalid expiry in enrollment response")
        try:
            expiry = datetime.fromisoformat(raw_expiry)
        except ValueError:
            raise ProtocolError("Invalid expiry in enrollment response") from None
        if expiry.utcoffset() is None or expiry <= datetime.now(UTC):
            raise ProtocolError("Expired enrollment token response")
        raw_gateway = result.get("gateway_url")
        if not isinstance(raw_gateway, str):
            raise ProtocolError("Invalid gateway URL in enrollment response")
        try:
            gateway = urlsplit(raw_gateway)
        except ValueError:
            raise ProtocolError("Invalid gateway URL in enrollment response") from None
        if (
            gateway.scheme not in {"https", "wss"}
            or not gateway.hostname
            or gateway.username is not None
            or gateway.password is not None
            or gateway.fragment
            or any(category(char) == "Cc" for char in raw_gateway)
        ):
            raise ProtocolError("Invalid gateway URL in enrollment response")
        # Persist the complete replacement before exposing it to the live tunnel.
        state = (
            asdict(self)
            | {
                name: result[name]
                for name in (
                    "connector_token",
                    "expires_at",
                    "gateway_url",
                )
            }
            | {"enrollment_token": None, "tunnel_id": tunnel_id}
        )
        atomic_write(directory / "identity.json", json.dumps(state))
        self.__dict__.update(state)
        return True


async def renew_identity(
    identity: Identity,
    directory: Path,
    stop: asyncio.Event,
    rotated: asyncio.Event,
    control_plane_ca: str | None = None,
    refresh_requested: asyncio.Event | None = None,
) -> None:
    retry = 2.0
    retrying = False
    while not stop.is_set():
        delay = 0 if retrying else identity.seconds_until_renewal()
        waits = [asyncio.create_task(stop.wait())]
        if refresh_requested is not None:
            waits.append(asyncio.create_task(refresh_requested.wait()))
        try:
            await asyncio.wait(
                waits,
                timeout=delay + random.uniform(0, 5) if delay > 0 else 0,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for wait in waits:
                wait.cancel()
            await asyncio.gather(*waits, return_exceptions=True)
        if stop.is_set():
            return
        if refresh_requested is not None:
            refresh_requested.clear()
        try:
            if await identity.issue(directory, control_plane_ca=control_plane_ca):
                rotated.set()
                retry = 2.0
                retrying = False
                log.info("Connector authorization renewed")
                continue
        except httpx.HTTPStatusError as error:
            if error.response.status_code not in {429, 500, 502, 503, 504}:
                raise ProtocolError(
                    http_error_message(error, "Token renewal")
                ) from None
            log.warning(
                "%s; renewal will retry", http_error_message(error, "Token renewal")
            )
        except httpx.TransportError as error:
            log.warning(
                "%s; renewal will retry", http_error_message(error, "Token renewal")
            )
        retrying = True
        try:
            await asyncio.wait_for(stop.wait(), retry + random.uniform(0, 1))
        except TimeoutError:
            pass
        retry = min(retry * 2, 30)
