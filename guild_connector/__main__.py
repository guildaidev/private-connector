"""Connect services in your network to Guild."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import ssl
from datetime import UTC, datetime
from getpass import getpass
from pathlib import Path

import httpx

from .connector import PrivateConnector
from .health import report_health
from .identity import (
    Identity,
    control_plane_api_base,
    http_error_message,
    identity_lock,
    renew_identity,
)
from .models import Destination, ProtocolError
from .tunnel_client import TunnelClient


def _load_config(path: Path) -> tuple[dict, dict[str, Destination], Path]:
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        reason = os.strerror(error.errno) if error.errno else "I/O error"
        raise ProtocolError(f"Cannot read connector configuration: {reason}") from None
    except UnicodeError:
        raise ProtocolError("Connector configuration must use UTF-8 encoding") from None
    except json.JSONDecodeError as error:
        raise ProtocolError(
            f"Invalid JSON in connector configuration at line {error.lineno}, "
            f"column {error.colno}"
        ) from None
    if not isinstance(config, dict):
        raise ProtocolError("Connector configuration must be a JSON object")
    for field in (
        "connector_id",
        "control_plane_url",
        "identity_directory",
        "destinations",
    ):
        if field not in config:
            raise ProtocolError(f"Missing required setting: {field}")
    for field in ("connector_id", "control_plane_url", "identity_directory"):
        if not isinstance(config[field], str) or not config[field].strip():
            raise ProtocolError(f"Setting {field} must be a non-empty string")
    if not isinstance(config["destinations"], dict) or not config["destinations"]:
        raise ProtocolError("Setting destinations must be a non-empty JSON object")
    destinations = {}
    for index, (target_id, origin) in enumerate(config["destinations"].items(), 1):
        try:
            destinations[target_id] = Destination(target_id, origin)
        except ProtocolError:
            raise ProtocolError(
                f"Invalid destinations entry {index}: use a target ID of 1-255 "
                "characters and an HTTP(S) URL with a hostname and optional port; "
                "no credentials, path, query, or fragment"
            ) from None
    return config, destinations, Path(config["identity_directory"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=["run", "enroll", "migrate-control-plane"],
        nargs="?",
        default="run",
    )
    parser.add_argument("--enrollment-token-file", type=Path)
    parser.add_argument("--from-control-plane-url")
    parser.add_argument("--to-control-plane-url")
    parser.add_argument(
        "--if-needed", action="store_true", help="Reuse an enrolled, unexpired identity"
    )
    parser.add_argument("--health-file", type=Path)
    parser.add_argument(
        "--config", type=Path, required=True, help="Connector JSON configuration"
    )
    args = parser.parse_args()
    if args.if_needed and args.command != "enroll":
        parser.error("--if-needed is only supported with enroll")
    if args.command == "migrate-control-plane":
        if not args.from_control_plane_url or not args.to_control_plane_url:
            parser.error(
                "migrate-control-plane requires --from-control-plane-url and --to-control-plane-url"
            )
        if args.enrollment_token_file or args.health_file:
            parser.error(
                "migrate-control-plane does not accept enrollment or health files"
            )
    elif args.from_control_plane_url or args.to_control_plane_url:
        parser.error("control plane migration options require migrate-control-plane")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    # HTTPX's INFO request log includes the URL, which may contain credentials
    # or customer data in its path/query. Keep lifecycle logs, not request URLs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        config, destinations, directory = _load_config(args.config)
    except ProtocolError as error:
        parser.exit(2, f"{error}\n")

    operation = "Enrollment" if args.command == "enroll" else "Token renewal"
    try:
        with identity_lock(directory):
            identity_path = directory / "identity.json"
            if identity_path.exists():
                identity = Identity(**json.loads(identity_path.read_text()))
                if identity.connector_id != config["connector_id"]:
                    raise ProtocolError(
                        "Identity belongs to another connector or control plane"
                    )
                if args.command == "migrate-control-plane":
                    if identity.control_plane_url != args.from_control_plane_url:
                        raise ProtocolError(
                            "Saved control plane URL does not match --from-control-plane-url"
                        )
                    if config["control_plane_url"] != args.to_control_plane_url:
                        raise ProtocolError(
                            "Configured control plane URL does not match --to-control-plane-url"
                        )
                    control_plane_api_base(args.to_control_plane_url)
                    identity.control_plane_url = args.to_control_plane_url
                    identity.save(directory)
                    logging.info("Saved control plane URL updated")
                    return
                if identity.control_plane_url != config["control_plane_url"]:
                    raise ProtocolError(
                        "Saved control plane URL differs from config; "
                        "run migrate-control-plane to update it"
                    )
            elif args.command == "migrate-control-plane":
                raise ProtocolError("No saved identity to migrate")
            elif args.command == "enroll" or args.enrollment_token_file:
                token = (
                    args.enrollment_token_file.read_text()
                    .removesuffix("\n")
                    .removesuffix("\r")
                    if args.enrollment_token_file
                    else getpass("Enrollment token: ")
                )
                identity = Identity.create(
                    config["connector_id"], config["control_plane_url"], token
                )
                identity.save(directory)
            else:
                raise ProtocolError(
                    "No saved identity; supply --enrollment-token-file to enroll on startup"
                )
            if (
                args.if_needed
                and not identity.enrollment_token
                and identity.connector_token
                and identity.expires_at
                and datetime.fromisoformat(identity.expires_at) > datetime.now(UTC)
            ):
                logging.info("Reusing this tunnel's enrolled identity")
                return
            if identity.enrollment_token:
                operation = "Enrollment"
            asyncio.run(
                _run(
                    identity,
                    directory,
                    destinations,
                    config,
                    enroll=args.command == "enroll",
                    health_file=args.health_file,
                )
            )
    except ProtocolError as error:
        parser.exit(1, f"{error}\n")
    except httpx.HTTPError as error:
        parser.exit(1, f"{http_error_message(error, operation)}\n")
    except ssl.SSLError:
        parser.exit(
            1, "Cannot configure TLS; check the configured CA certificate files\n"
        )
    except TimeoutError:
        parser.exit(
            1, "Connector operation timed out; check the preceding connection errors\n"
        )
    except OSError as error:
        reason = os.strerror(error.errno) if error.errno else "I/O error"
        parser.exit(1, f"Cannot access connector files: {reason}\n")
    except EOFError:
        parser.exit(
            1,
            "No enrollment token supplied; use --enrollment-token-file or an interactive terminal\n",
        )
    except (ValueError, TypeError, KeyError):
        parser.exit(
            1,
            "Invalid connector identity or enrollment data; check identity.json, "
            "the enrollment token, and connector settings\n",
        )


async def _run(
    identity: Identity,
    directory: Path,
    destinations: dict[str, Destination],
    config: dict,
    *,
    enroll: bool,
    health_file: Path | None = None,
) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stop.set)
    if enroll or identity.enrollment_token:
        async with asyncio.timeout(120):
            while not stop.is_set():
                try:
                    if await identity.issue(
                        directory, control_plane_ca=config.get("control_plane_ca")
                    ):
                        logging.info(
                            "Connector enrolled; private identity saved on this host"
                        )
                        break
                except httpx.HTTPStatusError as error:
                    if error.response.status_code not in {429, 500, 502, 503, 504}:
                        raise
                    logging.warning(
                        "%s; enrollment will retry",
                        http_error_message(error, "Enrollment"),
                    )
                except httpx.TransportError as error:
                    logging.warning(
                        "%s; enrollment will retry",
                        http_error_message(error, "Enrollment"),
                    )
                try:
                    await asyncio.wait_for(stop.wait(), 2)
                except TimeoutError:
                    pass
        if identity.enrollment_token:
            raise ProtocolError("Enrollment interrupted before completion")
        if enroll:
            return
    if identity.enrollment_token:
        raise ProtocolError("Enrollment is not complete")
    rotated = asyncio.Event()
    refresh_requested = asyncio.Event()
    tunnel = TunnelClient(
        base_url=identity.gateway_url or "",
        ca_file=config.get("gateway_ca"),
        credential=identity.credential,
        max_in_flight=config.get("max_in_flight", 16),
        rotated=rotated,
        refresh_requested=refresh_requested,
    )
    connector = PrivateConnector(
        tunnel=tunnel,
        destinations=destinations,
    )
    logging.info("Private connector started; connecting to Guild")
    tasks = [
        asyncio.create_task(connector.run_forever(stop)),
        asyncio.create_task(
            renew_identity(
                identity,
                directory,
                stop,
                rotated,
                config.get("control_plane_ca"),
                refresh_requested,
            )
        ),
    ]
    if health_file:
        tasks.append(asyncio.create_task(report_health(health_file, tunnel, stop)))
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        stop.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    main()
