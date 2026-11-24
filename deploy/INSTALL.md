# Install the Guild private connector

Use a Linux host with Docker Compose 2.23.1 or later, or an existing Kubernetes
cluster with Helm 3. No source checkout or local build is required.

## Docker Compose

1. Download and extract the Compose bundle from the GitHub release.
2. Copy `connector.example.json` to `connector.json`. Enter the connector ID,
   control-plane URL from **Guild → Settings → Private connectors**. Add one
   destination when you are ready to test. The wizard's destination fields are
   only a helper for the initial file. You do not need to catalog every service
   or API endpoint. Add or change destinations in the JSON file on your host.
   The connector needs at least one destination before startup. Keep the
   identity directory as shown.
3. Create and download a fresh enrollment token from Guild. Save the token as
   `enrollment-token` beside `compose.yaml`. Tokens expire after three days.
4. Run these commands from that directory:

```sh
chmod 600 connector.json enrollment-token
sudo chown 10001:10001 connector.json
sudo install -d -o 10001 -g 10001 -m 700 enrollment
sudo install -o 10001 -g 10001 -m 400 enrollment-token enrollment/token
docker compose up -d
docker compose logs --tail 50 connector
docker compose exec connector guild-private-connector-health --ready
docker compose exec connector guild-private-connector --version
```

After Guild shows the tunnel as connected, remove `enrollment-token` and
`enrollment/token`. Keep the empty enrollment directory and the identity volume.
Set the integration's private route to this connector and its configured target.

For a private destination CA, mount the public CA bundle read-only and set
`destination_ca` to its container path in `connector.json`. Configure
`blocked_methods`, such as `["DELETE"]`, in the same JSON file when required.

## Kubernetes

Download the chart and `connector-values.yaml` from the same release. Enter your
connector ID, control-plane URL, and destinations in the values file. Store the
enrollment token in an existing Kubernetes Secret:

```sh
kubectl create namespace guild-connector --dry-run=client -o yaml | kubectl apply -f -
kubectl -n guild-connector create secret generic guild-connector-enrollment \
  --from-file=token=enrollment-token
helm upgrade --install tunnel-a ./private-connector-*.tgz \
  --namespace guild-connector --values connector-values.yaml --wait --timeout 5m
```

Use a directory containing one downloaded chart. Each replica needs its own Helm
release, enrollment, and identity volume. Do not scale a release or share its PVC.
After Guild shows the tunnel as connected, remove the enrollment Secret and local
token file. Keep the PVC.

## Verify and upgrade

Download `SHA256SUMS` with the release assets. From the download directory, run
`sha256sum --check --ignore-missing SHA256SUMS`.

For an upgrade, replace the Compose file or Helm chart with the new release.
Keep your configuration, Compose project name, and identity storage. Run
`docker compose up -d` or `helm upgrade` again. Updates can interrupt active calls.
Do not run `docker compose down --volumes` during an upgrade.
