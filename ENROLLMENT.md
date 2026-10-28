# Enrollment and authentication

The private connector uses a short-lived enrollment token to create a renewable
identity. It connects to Guild over server-authenticated HTTPS and WSS. It does
not use a client certificate.

## First start

1. An account administrator creates a connector in Guild and downloads an
   enrollment token. A new token expires after three days and reserves one of
   the account's four tunnel slots. Prepare the deployment files before creating
   the token if you need more setup time; those files do not expire.
2. Give each installation its own token and persistent identity directory. Keep
   the token in a private file. Do not put it in Git, a container image, a
   command argument, or Helm values.
3. Start the connector with `--enrollment-token-file`. The connector saves the
   token and a request ID in its identity directory, then exchanges the token
   with Guild. It saves the returned identity atomically and removes the
   enrollment token from its private state.
4. After enrollment succeeds, delete the separate token file. Keep the identity
   directory for restarts and upgrades.

The identity directory uses mode 0700, and identity files use mode 0600. A file
lock prevents two processes from using one directory at the same time. Never
copy an identity directory to create another replica. Anyone who obtains the
saved identity or its backup can impersonate that tunnel.

If a response is lost during enrollment, the connector can retry with the same
request ID while the token remains valid. If the token expires before recovery,
revoke that tunnel in Guild and enroll a replacement.

## Renewal, revocation, and restart

The connector identity lasts 90 days. The connector begins renewal seven days
before expiry, with jitter. It saves the replacement identity and continues to
use the existing tunnel. A stopped connector can restart with its saved
identity while that identity is valid. An expired identity needs a new
enrollment.

Revoke a tunnel or disable the connector in Guild to block new calls and
reconnections. Requests already sent to your service may still finish.
Revocation cannot undo an HTTP side effect. Retiring a tunnel also requires you
to stop its process and delete its local identity and backups.

Pending and expired enrollments still use a tunnel slot until you revoke them.
Deleting a connector leaves credentials that selected it unable to use the
private route. Guild does not silently switch those calls to direct access.

## Control-plane address changes

The connector refuses to use an existing identity with a different
`control_plane_url`. This prevents an accidental configuration change from
sending its saved credential to another server. To change the address:

1. Stop the connector and verify the new HTTPS host and CA trust.
2. Change `control_plane_url` in the connector configuration.
3. Run the migration command as the identity directory's owner:

   ```sh
   guild-private-connector migrate-control-plane \
     --config connector.json \
     --from-control-plane-url https://old.example.com \
     --to-control-plane-url https://new.example.com/api/v2
   ```
4. Start the connector and check readiness.

The migration command updates the saved address without contacting either
server. It keeps the same tunnel identity. Use a new enrollment if you cannot
verify the new endpoint.

## TLS trust

The connector verifies Guild's certificate and hostname. Public Guild endpoints
use normal CA trust. If Guild supplies a private-CA endpoint, configure
`control_plane_ca` for HTTPS enrollment and renewal, and `gateway_ca` for the
WSS tunnel. Each setting points to a local public PEM bundle. These settings do
not supply client certificates and do not configure trust for your local API.

Enrollment does not distribute CA certificates. Obtain a private CA bundle from
a trusted administrator. Never disable certificate or hostname verification.
Do not place credentials in a gateway URL; the connector sends credentials in
Authorization headers.
