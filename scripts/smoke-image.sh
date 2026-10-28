#!/usr/bin/env bash
set -euo pipefail

engine="${CONTAINER_ENGINE:-docker}"
image="${1:?Usage: smoke-image.sh IMAGE}"
connector_root="$(cd -- "$(dirname -- "$0")/.." && pwd)"
volume="$($engine volume create "guild-connector-smoke-$$-$RANDOM")"
cleanup() { "$engine" volume rm "$volume" >/dev/null; }
trap cleanup EXIT
flags=(--rm --read-only --cap-drop=ALL --security-opt=no-new-privileges
       --tmpfs /tmp:rw,noexec,nosuid,size=32m --user 10001:10001)

"$engine" run "${flags[@]}" "$image" --help >/dev/null
"$engine" run "${flags[@]}" --entrypoint guild-private-connector-health "$image" --help >/dev/null
for mode in live ready; do
    args=(--state-file /tmp/missing-smoke-health.json)
    if [ "$mode" = ready ]; then
        args+=(--ready)
    fi
    status=0
    "$engine" run "${flags[@]}" --entrypoint guild-private-connector-health "$image" "${args[@]}" || status=$?
    if [ "$status" -ne 1 ]; then
        echo "Expected missing-state $mode probe to exit 1, got $status" >&2
        exit 1
    fi
done
"$engine" run "${flags[@]}" --mount "type=volume,source=$volume,target=/var/lib/guild-connector" \
    --entrypoint python "$image" -c '
import importlib.util, os
from pathlib import Path
from secrets import token_urlsafe
from uuid import uuid4
from guild_connector.identity import Identity, identity_lock
assert os.getuid() == 10001
assert importlib.util.find_spec("guildcore") is None
assert importlib.util.find_spec("ruff") is None
assert "CapEff:\t0000000000000000" in Path("/proc/self/status").read_text()
try:
    Path("/opt/should-not-write").touch()
except OSError:
    pass
else:
    raise AssertionError("Root filesystem is writable")
directory = Path("/var/lib/guild-connector/identity")
with identity_lock(directory):
    Identity.create(str(uuid4()), "https://control.example.test", f"gce:{uuid4()}:{token_urlsafe(32)}").save(directory)
assert directory.stat().st_mode & 0o777 == 0o700
assert (directory / "identity.json").stat().st_mode & 0o777 == 0o600
'
"$engine" run "${flags[@]}" --mount "type=volume,source=$volume,target=/var/lib/guild-connector" \
    --entrypoint python "$image" -c '
import json
from pathlib import Path
from guild_connector.identity import Identity, identity_lock
directory = Path("/var/lib/guild-connector/identity")
before = (directory / "identity.json").read_bytes()
with identity_lock(directory):
    identity = Identity(**json.loads(before))
    assert identity.request_id
    assert identity.enrollment_token.startswith("gce:")
assert (directory / "identity.json").read_bytes() == before
print("PASS: non-root, read-only image; private identity survives container replacement")
'
"$engine" run "${flags[@]}" --mount "type=bind,source=$connector_root/tests,target=/tests,readonly" \
    --entrypoint python "$image" -m unittest discover -s /tests -v
