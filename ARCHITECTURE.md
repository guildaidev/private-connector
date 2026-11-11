# Private connector architecture

The private connector runs in your network. It opens outbound HTTPS and WSS
connections to Guild. Guild sends authorized HTTP requests over the tunnel. The
connector forwards each request to a configured service in your network.

| Component | Responsibility |
| --- | --- |
| Guild | Selects the integration credential and its connector target. Sends requests over the tunnel. |
| Private connector | Maintains the outbound tunnel. Maps target names to local HTTP(S) origins and forwards requests. |
| Your service | Handles the HTTP request and returns a response. |

The connector does not need an inbound port. It does not run agents, integration
code, OAuth exchanges, or LLM calls. See [Connection flow](CONNECTION_FLOW.md)
for a request example.

## Configuration and identity

One Guild account can register one connector and enroll up to four tunnels. Each
installation needs its own persistent identity directory. Never share or copy an
identity directory between installations. A reconnect uses the same identity.

An administrator creates an enrollment token in Guild. The connector exchanges
that token for a renewable connector identity and stores it in its private
directory. The token file can be removed after enrollment. The stored identity
is a bearer credential; protect the directory and its backups. See
[Enrollment](ENROLLMENT.md) for expiry, renewal, and revocation.

A Guild credential selects the connector and a target name. The connector's
configuration maps that name to one HTTP(S) origin. Guild supplies the request
path, query, headers, and body. A request cannot select another origin by
changing its path or headers. Configure target names and service addresses in
the [deployment files](DEPLOYMENT.md).

## Network and trust

- The connector verifies the TLS certificate and hostname when connecting to
  Guild. It also verifies HTTPS certificates for local destinations.
- The optional `control_plane_ca` and `gateway_ca` settings trust a private CA
  for Guild's enrollment endpoint or tunnel endpoint, respectively. Normal
  Guild endpoints need neither setting. These files contain public CA
  certificates, not client certificates or connector credentials. The settings
  do not configure trust for local destinations.
- Guild credentials may travel in forwarded request headers or bodies. The
  connector does not store them. Restrict access to the connector host and its
  logs, and give integration credentials only the access they need.
- The target map limits destination origins. It does not restrict API paths or
  operations within a destination. Apply your own service authorization and
  network egress rules where needed.
- The connector does not follow HTTP redirects or use environment proxies for
  forwarded requests. It does not retain response cookies between requests.

## Responses and failures

Ordinary responses are buffered. Guild can request streaming for a call. The
connector then forwards response bytes in chunks without parsing SSE events.
Streaming uses tunnel protocol 4. A streamed response allows up to 64 MiB total,
with 64 KiB chunks and at most four chunks in flight. A buffered response must
fit the 16 MiB encoded-frame limit. See [Supported behavior](README.md#supported-behavior)
for other request limits.

Guild requests configured target names over tunnel protocol 5. The connector
replies with names from its loaded configuration and does not send destination
URLs. Guild checks a target name before sending an HTTP call. The connector
still checks the name against its local destination map before forwarding.
Discovery is unavailable when the response exceeds 2,048 targets or 4 MiB
encoded. The connector sends a `targets_unavailable` error and logs the target
count and encoded size without names or URLs. The tunnel remains open, and
forwarding and streaming continue. Discovery is also unavailable with a
protocol-4 gateway.

An acknowledgement means the connector received a request. It does not mean
the service completed the request. If the connection drops after dispatch, the
outcome may be unknown. The connector does not replay such requests or switch to
direct access. Cancellation cannot undo a write that the service already made.

The readiness check requires a connected tunnel. The liveness check reports
whether the connector process is running. Neither check proves that every local
destination is reachable. Test each destination through its Guild integration.

## Code map

| Responsibility | Source |
| --- | --- |
| Enrollment and identity storage | [identity.py](guild_connector/identity.py) |
| Tunnel connection and request handling | [tunnel_client.py](guild_connector/tunnel_client.py) |
| Local HTTP forwarding | [http_forwarder.py](guild_connector/http_forwarder.py), [urls.py](guild_connector/urls.py) |
| Docker Compose and Helm packaging | [deploy/compose](deploy/compose/), [helm/private-connector](helm/private-connector/) |
