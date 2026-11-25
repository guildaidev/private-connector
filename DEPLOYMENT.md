# Deploy the private connector

The customer runs only the standalone `guild-private-connector` image. Guild
hosts the integration and tunnel services. The image does not contain those
services or test services.

Download the Compose bundle or Helm chart from the
[GitHub release](https://github.com/guildaidev/private-connector/releases/latest).
Each release includes a digest-pinned image reference, installation files,
checksums, and Linux amd64/arm64 support. No source checkout or local build is
required. Publication is separate from merging code; see [Releasing](RELEASING.md).

## Before installing

1. Create the account's connector in **Settings → Private connectors**.
2. Copy its ID and the control-plane URL supplied by the app. Do not guess the URL.
3. Choose a host or Kubernetes namespace with DNS and network access to the
   permitted internal HTTP services. Configure destination names and HTTP(S)
   origins. Paths and credentials do not belong in this map. The wizard fields
   only help prepare the initial JSON. You can skip them and edit the file on
   your host. Add one destination before startup; add other services when needed.
4. Permit outbound HTTPS to Guild's control plane and outbound WSS to its tunnel
   endpoint (normally port 443). No inbound listener, load balancer, or Kubernetes
   API access is required by the connector.
5. Prepare the deployment files in the app. The files have no expiry. Create a
   separate enrollment token when convenient; new tokens expire after three days.
   Existing tokens keep their original expiry. Revoke unused tokens when plans change.

Guild still resolves integration credentials. On each integration connection's
**Network access** settings, select this connector and the matching destination name.
The internal service URL belongs in the integration configuration itself.
Deploying the container alone does not route an
integration through it.

## Docker Compose on a VM

Use the released `compose.yaml` with Docker Compose 2.23.1 or later.
The file pins the image digest and mounts your `connector.json` read-only.
Copy the JSON file from Guild and the Compose file, plus the
separately downloaded `enrollment-token` into a private working folder on a
Linux host. From that folder:

```bash
chmod 600 connector.json enrollment-token
sudo chown 10001:10001 connector.json
sudo install -d -o 10001 -g 10001 -m 700 enrollment
sudo install -o 10001 -g 10001 -m 400 enrollment-token enrollment/token
docker compose up -d connector
docker compose logs --tail 50 connector
docker compose exec connector guild-private-connector-health --ready
```

Startup enrolls, saves the identity in the named volume, and opens the tunnel.
After the logs confirm enrollment, delete `enrollment-token` and
`enrollment/token`. Keep the empty `enrollment` directory: Compose mounts that
directory on later starts, but the connector does not read the removed token
when an identity already exists. Do not put tokens in arguments, environment
variables, Git, or image layers. Protect the initial browser download too.

For operators using the repository template instead, use
`deploy/compose/compose.yaml` with `connector.example.json` copied to
`connector.json`. Set `CONNECTOR_IMAGE` to Guild's released image digest and
fill in the connector ID, Guild URL, and destinations. The same startup commands
apply. Keep the configured identity subdirectory under `/var/lib/guild-connector`.

The configuration mount is read-only; the root filesystem is read-only; all
capabilities are dropped. Only the identity volume and small temporary filesystem
are writable. Logs are bounded. Keep the Compose project name stable: changing it
selects a different named volume. Do not use `docker compose down --volumes` for
an ordinary restart or upgrade. Podman can run the image too; Compose support
depends on the separately installed Compose provider.

## Helm in an existing Kubernetes cluster

The customer chart is `helm/private-connector`. It installs one single-replica
Deployment and an identity PVC, without RBAC, a Service, Ingress, database, or CA.
It uses `Recreate` updates to prevent old and new pods from sharing an identity.
Install a second release with its own token and PVC for another tunnel;
do not scale one release or reuse another tunnel's PVC.

Download `connector-values.yaml` from the app, or prepare equivalent values:

```yaml
image:
  digest: sha256:RELEASE_DIGEST
connector:
  id: CONNECTOR_ID_FROM_GUILD
  controlPlaneUrl: https://CONTROL_PLANE_FROM_GUILD
  destinations:
    foo: https://foo.internal:8443
enrollment:
  existingSecret: guild-connector-enrollment
# persistence:
#   storageClass: YOUR_ENCRYPTED_STORAGE_CLASS
```

Create the namespace and store the token in a Secret from a private local file,
then install the released OCI chart:

```bash
kubectl create namespace guild-connector --dry-run=client -o yaml | kubectl apply -f -
kubectl -n guild-connector create secret generic guild-connector-enrollment \
  --from-file=token=enrollment-token --dry-run=client -o yaml | kubectl apply -f -
helm upgrade --install tunnel-a \
  oci://ghcr.io/guildaidev/charts/private-connector \
  --version RELEASE_VERSION --namespace guild-connector \
  --values connector-values.yaml --wait --timeout 5m
```

After Helm finishes, confirm that Guild shows the tunnel as authenticated.
If the tunnel does not authenticate, check both containers:

```bash
kubectl -n guild-connector logs deployment/tunnel-a-connector -c enroll
kubectl -n guild-connector logs deployment/tunnel-a-connector -c connector
```

After authentication, delete the Secret and local token file:

```bash
kubectl -n guild-connector delete secret guild-connector-enrollment
rm enrollment-token
```

The chart references the Secret by name; it does
not store token contents in Helm release history. Only the init container mounts
it. After successful enrollment the Secret is optional, so restarts succeed
without recreating it. If the identity volume is lost, create a new enrollment
and revoke the old tunnel; do not expect an expired token to recreate it.

During development, replace the OCI chart argument with the local
`./helm/private-connector` directory and point `image` at the
locally built image available to the cluster. This does not publish anything.

The pod runs as UID/GID 10001. `fsGroup` grants access to the volume root; the
connector creates its own 0700 identity subdirectory. Use storage supporting
POSIX permissions and exclusive file locks. Protect PVCs and backups as bearer
credentials, with encryption and restricted administrative access. The connector does
not need a service-account token or Kubernetes API permissions.

Normal public HTTPS/WSS uses system CA trust and requires no CA override.
Enrollment does not distribute CA certificates. For a private control-plane CA,
mount its **public** PEM through `controlPlaneCA.existingConfigMap` and
`controlPlaneCA.key`. For a private tunnel-gateway CA, use
`gatewayCA.existingConfigMap` and `gatewayCA.key`. These may reference the same
ConfigMap if both endpoints use the same CA. These settings apply only to
connections to Guild. They do not configure HTTPS trust for a local destination.

For a direct or Compose tunnel, the equivalent JSON settings are
`control_plane_ca` and `gateway_ca`, each a local PEM-file path. Mount the files
read-only into the container and use their container paths in `connector.json`:

```json
{
  "control_plane_ca": "/etc/guild/private-ca/control-plane.pem",
  "gateway_ca": "/etc/guild/private-ca/gateway.pem"
}
```

These are optional additions to the complete configuration, not a replacement
for it. Supply CA files locally through a trusted administrator; never fetch
them from an enrollment response. Certificate and hostname verification remain
enabled with either system trust or an explicit CA file. Restart the connector
after changing a CA file.

## Health, upgrades, and removal

- **Liveness** checks an event-loop heartbeat in a temporary file. It stays healthy
  during reconnection, so a Guild/network outage does not cause restart storms.
- **Readiness** additionally requires a completed tunnel handshake. It means the
  tunnel is connected, not that every configured destination is reachable.
- There is no HTTP health port. Helm uses exec probes; Compose uses the image's
  health command. Docker reports unhealthy status but does not restart solely
  because a health check failed; the restart policy handles process exits.
- Neither probe reads identities or logs request data. A dead event loop becomes
  unhealthy after 30 seconds, plus the orchestrator's probe failure threshold.
- The operating limit is managed in Guild, with a 512 MiB default memory limit. Size
  memory for buffered bodies, encoding overhead, and concurrency; load-test your
  payloads before raising concurrency. These are not throughput guarantees.
- Upgrade by pinning the new digest and running `docker compose up -d` or
  `helm upgrade`. Identity storage must stay attached. Token renewal does
  not require reinstalling the image. Back up identity storage securely, but never
  run a restored copy concurrently with the original.
- A restart/update can interrupt in-flight calls; the connector does not promise
  transparent retry of side effects. Multiple tunnels still require the
  HA validation described in [ARCHITECTURE.md](ARCHITECTURE.md).
- To retire a tunnel, revoke it in Guild, stop/uninstall it, then explicitly
  remove its identity volume and backups. Helm deliberately retains the PVC on
  uninstall; reclaim it with `persistence.existingClaim` when reinstalling that
  same tunnel. Uninstall alone does not revoke credentials. Revocation
  blocks subsequent authorized requests/reconnections, but cannot undo requests
  already dispatched to the customer API.

## Build, test, and release (Guild)

```bash
docker build -t private-connector:local .
bash scripts/smoke-image.sh private-connector:local
uv sync --locked
uv run --frozen python -m unittest discover -s tests -v
uv run --frozen python -m unittest discover -s packaging_tests -v
```

Use `CONTAINER_ENGINE=podman` for the image smoke script; build with
`podman build --format docker` to retain the image-level Docker health check.
The Compose file also declares the health check explicitly for OCI runtimes.
The smoke test creates and removes its own temporary identity volume, runs with
the production non-root/read-only restrictions, checks persisted identity permissions,
and runs the HTTP/TLS test suite inside the built image. Its test TLS peers are
fixtures; that smoke test is not a real Guild enrollment or a production soak test.

The **Private connector release** workflow validates versions, tests, and the
packaged runtime before publishing a multi-architecture image, a pinned Helm
chart, and a GitHub release with installation downloads and checksums. The
release job verifies public image and chart access without registry credentials.
See [Releasing](RELEASING.md) for version changes, publication, and recovery.

## Concurrency

Each tunnel has a fixed safety ceiling of 4,096 concurrent requests. Guild controls
the operating limit within that ceiling. A lower operating limit lets active
calls finish before admitting more calls. Routine changes require no restart.

Before upgrading a connector with a custom concurrency setting, ask Guild to set
the intended operating limit. Legacy JSON `max_in_flight` and Helm
`connector.maxInFlight` values are accepted but ignored.

## Memory protection

The connector reads Linux cgroup v2 or v1 memory limits and usage, including
visible parent constraints. With no finite cgroup limit, the working limit is
capped at 512 MiB and bounded by host memory and available memory. If current
usage cannot be read, the connector refuses new work until readings recover.

Admission leaves 25% of the working limit, with a minimum of 32 MiB, as runtime
headroom. Another 64 MiB covers transport receive/parsing and control buffers.
The WebSocket receive queue holds one frame. Each admitted call reserves its
working memory until completion, cancellation, or disconnect:

- A buffered call reserves 64 MiB for the largest supported response and its
  byte-array, base64, JSON, and socket copies.
- A streaming call reserves 1 MiB for bounded chunks and encoding buffers.
- Both reserve another four times the incoming frame length for the decoded
  request and retained metadata.

The conservative reservations can admit fewer buffered calls than Guild’s
operating limit permits. The connector never increases its budget merely
because the host has more memory than the container was allocated.

When a reservation cannot fit, the connector returns `connector_busy` without
acknowledging the call or contacting the local service. Existing calls keep their
reservations and continue. Heartbeats, credentials renewal, target discovery,
stream credits, and cancellation remain available. Reservations are released
when their task ends. New calls resume automatically when capacity is available.


## Private CA for internal HTTPS services

Set `destination_ca` in `connector.json` to the path of a PEM CA bundle:

```json
"destination_ca": "/etc/guild/destination-ca/ca.pem"
```

The bundle adds trusted CA certificates for HTTPS destinations. The connector
still checks certificate chains and hostnames. Keep the service hostname in the
destination URL. This setting does not configure client certificates or change
trust for enrollment and the Guild gateway.

For Docker Compose, mount the bundle read-only in the connector service:

```yaml
volumes:
  - ./internal-ca.pem:/etc/guild/destination-ca/ca.pem:ro
```

For Helm, create a ConfigMap containing the CA bundle and set these values:

```sh
kubectl create configmap internal-api-ca --from-file=ca.pem=./internal-ca.pem
```

```yaml
destinationCA:
  existingConfigMap: internal-api-ca
  key: ca.pem
```

Use CA certificates only. The connector needs no CA private key. Replace the
bundle and restart the connector when the trusted roots change. With the setting
omitted, existing HTTPS behavior is unchanged.


## Block HTTP methods

Use a connector build that supports `blocked_methods`. Set the connector-wide
policy in `connector.json`:

```json
"blocked_methods": ["DELETE", "PATCH"]
```

For Helm, set:

```yaml
connector:
  blockedMethods: [DELETE, PATCH]
```

The connector returns HTTP 403 before contacting any destination for a blocked
method. The same policy applies to buffered and streamed calls. Method names are
case-insensitive in configuration. An omitted setting or an empty list preserves
existing behavior. Invalid method lists stop startup.

When a blocklist is active, the connector also refuses requests with
`X-HTTP-Method-Override`, `X-Method-Override`, or `X-HTTP-Method` headers. These
headers could otherwise change the method inside the destination service.

Apply the same policy to every replica. Changing the policy requires a connector
restart. Verify that a blocked call returns 403 after upgrading the image.

The policy checks the HTTP verb. It does not inspect tool names, request bodies,
or application actions. A permitted POST can still invoke a destructive action;
service authorization must restrict those actions.
