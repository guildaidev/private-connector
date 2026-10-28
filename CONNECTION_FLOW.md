# Connection flow

The private connector opens one outbound WSS connection to Guild. Guild sends
requests over that connection. Your network needs no inbound listener for the
connector.

## Open the tunnel

1. Create a connector and an enrollment token in Guild.
2. Start the connector with the token file. The connector uses HTTPS to enroll,
   stores its renewable identity, and removes the token from its private state.
3. The connector opens a WSS tunnel to the gateway address supplied by Guild.
   The connector verifies the gateway's TLS certificate and authenticates with
   its saved identity.
4. The connector keeps the tunnel open and reconnects after an interruption.
   A restart uses the saved identity; it does not need another enrollment token.

Keep the gateway URL free of credentials. The connector sends its credential in
an Authorization header. See [Enrollment](ENROLLMENT.md) for token handling.

## Forward a request

Assume the credential selects target `foo`, and the connector configuration has:

```json
{"destinations": {"foo": "https://foo.internal:8443"}}
```

1. Guild checks access to the integration credential. The credential's network
   settings select this connector and target `foo`.
2. Guild sends the HTTP method, encoded path, query, headers, and body through
   the tunnel. For a `GET /orders?limit=10` call, Guild sends the target name
   `foo` with the path `/orders` and query `limit=10`.
3. The connector looks up `foo` locally. It calls
   `https://foo.internal:8443/orders?limit=10`. The request cannot replace the
   configured scheme, host, or port. An unknown target or invalid path fails
   before the connector contacts a local service.
4. The connector returns the service's status, headers, and body through the
   tunnel. Ordinary calls buffer the response. A streaming call forwards chunks
   as they arrive, with a limit on bytes queued in the tunnel.

The connector preserves encoded paths and duplicate query keys. It does not
resolve integration secrets or run integration code. Credentials supplied by
Guild can appear in the forwarded headers or body.

## Timeouts and uncertain outcomes

The connector must acknowledge a request within the 25-second start window.
After acknowledgement, the integration's HTTP connection and read timeouts
apply. There is no connector-specific total execution timer.

Other calls and tunnel heartbeats can continue while a request waits on your
service. An acknowledgement confirms receipt, not API success. If the tunnel
fails after Guild sends a request, the service may have processed that request.
The connector does not replay it. A failed private route does not fall back to
direct access.
