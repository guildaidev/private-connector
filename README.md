# Private connector

The connector runs in the customer's network. It opens an outbound WSS connection
to Guild and forwards HTTP requests to configured local services. The connector
does not run agents, LLM calls, or a secret store.

Guild keeps integration code, credential resolution, and access checks.
Integration credentials pass through the tunnel in request headers or bodies.

## Set up a connector

1. As an account admin, open **Settings → Private connectors**.
   Create the account's connector.
2. Choose Docker or Helm. Enter Guild's released image digest and your internal
   service addresses. Download the deployment file. This file has no expiry.
3. Create and download the enrollment token separately. New tokens last three
   days. Keep the token out of Git, deployment files, and Helm values.
4. Follow the host commands on the setup page. Startup enrolls automatically, saves
   the identity, and opens the tunnel. See [Deployment](DEPLOYMENT.md) for details.
5. Connect the integration's credentials in Guild. Open the credential's detail
   page and select **Network access → Private connector**. Choose the connector
   and enter a target name from the customer configuration, such as `foo`.

Example configuration; use the control-plane URL and connector ID from the app:

```json
{
  "control_plane_url": "https://api.example.com",
  "connector_id": "00000000-0000-0000-0000-000000000001",
  "identity_directory": "/var/lib/guild-connector",
  "max_in_flight": 16,
  "destinations": {
    "foo": "https://foo.internal:8443"
  }
}
```

The integration supplies the request path and query. The customer target map
supplies the local scheme, host, and port. Put API path prefixes in the integration,
not in the destination map.

One connector can serve many credentials, agents, and workspaces. Configure routing
once per credential. Existing credential grants and defaults control access.
Credential editors can select a connector; account admins manage connectors and
enrollment. **Enabled** is an administrative setting, not a live health check.

Each account permits one connector and up to four non-revoked tunnel identities.
Pending or expired enrollments still count until revoked. Enroll each replica
separately. Give each replica a private, persistent identity directory; never
share or clone that directory. Its saved token is a bearer credential.

For a source checkout, run `uv sync --locked` in the repository root.
Start with the command below. Keep the token file private. Remove
the token file after successful enrollment; keep the identity directory.

```sh
uv run guild-private-connector run --config connector.json \
  --enrollment-token-file /secure/path/token
```

## Supported behavior

- REST/OpenAPI, MCP HTTP, and Guild HTTP tools can use the connector when their
  credential selects a private route. Raw SQL/TCP and direct SDK connections
  cannot use the HTTP tunnel. The generic HTTP tool supports `fetch`, not
  background `fetch_async`.
- OAuth token exchange and LLM calls stay hosted.
- No redirects, environment proxies, or cookies retained between requests.
  Target names cannot override the customer's configured host.
- Ordinary responses are buffered. Guild can request streamed responses,
  including SSE, for a call. The connector forwards
  raw response bytes without parsing events. Repeated-header fidelity is not
  supported. Buffered responses use a 16 MiB encoded frame; streamed responses
  use 64 KiB chunks and a 64 MiB total byte limit.
- The 25-second start deadline covers dispatch and acknowledgement. After that,
  the integration's HTTPX phase timeouts apply, not a total execution timeout.
- Disabled, deleted, or unavailable private routes do not fall back to direct
  access. Uncertain requests are not replayed. Cancellation cannot undo a write.
- Streaming calls require tunnel protocol 4.
- Target discovery uses tunnel protocol 5. Guild requests target names over the
  existing connection. The connector never sends destination URLs in this reply.
  The connector negotiates protocol 5 with a compatible gateway. With a protocol-4
  gateway, forwarding and streaming continue, but target discovery is unavailable.

## Architecture and tests

- [Connection flow](CONNECTION_FLOW.md): follow one request.
- [Architecture](ARCHITECTURE.md): components, trust, and failure behavior.
- [Enrollment](ENROLLMENT.md): identity storage, renewal, and revocation.

Client tests, from the repository root:

```sh
uv sync --locked
uv run --frozen python -m unittest discover -s tests -v
```

Run packaging checks with:

```sh
uv run --frozen python -m unittest discover -s packaging_tests -v
```

Client tests alone do not verify live forwarding. Test a configured destination
through a Guild integration after deployment.

The `guild-customer-connector` and `guild-customer-connector-health` commands
remain as aliases for existing deployments. New deployment files use the private
connector commands.
