# Deploy the private connector

The customer runs only the standalone `guild-private-connector` image. Guild
hosts the integration and tunnel services. The image does not contain those
services or test services.

Packaging lives in this repository; no image or chart is published merely by
merging this code. Guild must perform the release steps below and supply an image
digest and chart version before a customer can pull them.

## Before installing

1. Create the account's connector in **Settings → Private connectors**.
2. Copy its ID and the control-plane URL supplied by the app. Do not guess the URL.
3. Choose a host or Kubernetes namespace with DNS and network access to the
   permitted internal HTTP services. Configure destination names and exact
   HTTP(S) origins that match the URLs used by the integrations. The target map
   is an allowlist, not host remapping; paths and credentials do not belong in it.
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

Use the app's downloaded `compose.yaml` with Docker Compose 2.23.1 or later.
The file includes the connector configuration and a pinned image digest.
No repository checkout or manual JSON edit is needed. Copy the file and the
separately downloaded `enrollment-token` into a private working folder on a
Linux host. From that folder:

```bash
chmod 600 enrollment-token
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
- The defaults allow four concurrent requests with a 512 MiB memory limit. Size
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

The separate **Private connector release** workflow:

1. Runs manually from `main`, with a numeric version matching `pyproject.toml`,
   `Chart.version`, and `Chart.appVersion`.
2. Validates tests, packaging, and a native container run before publication.
3. Authenticates to GHCR and checks both the image tag and OCI chart version
   before publishing either artifact. An existing artifact or an inconclusive
   lookup (including authentication and registry failures) stops the release.
4. Publishes `linux/amd64` and `linux/arm64` images to GHCR with SBOM/provenance,
   and the versioned chart to GHCR's OCI chart registry.
5. Reports the exact image digest and chart reference in the run summary. Supply
   those references to customers; do not instruct them to track `latest`.

Release runs are serialized. The existence check prevents accidental reuse of a
published version; it is not an atomic registry lock against other publishers.
Restrict publishing access to this workflow. If only one artifact was published
before a failure, a rerun stops too: investigate the partial release and use a
new version rather than overwrite it.

Set `PRIVATE_CONNECTOR_RELEASE_ENABLED=true` only after you configure required
reviewers on the `private-connector-release` GitHub environment. Configure
package visibility and access so customers can pull **only these artifacts**, not
Guild's source or backend image.
GitHub packages may initially be private; the workflow does not make the source
repository or packages public. Use new versions for releases and security fixes.
Publication is not deployment and does not imply production sign-off.
