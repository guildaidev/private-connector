# Versioning and releases

Connector releases use `major.minor.patch`, tagged as `v0.3.0`, for example.
Use patch versions for compatible fixes and minor versions for compatible
features. Document breaking configuration or behavior changes explicitly.
Before 1.0, a minor version may include breaking changes. Tunnel protocol
numbers are separate from the application release version.

The package version in `pyproject.toml` is checked against `uv.lock`, the Docker
image's default version, and the Helm chart's `version` and `appVersion`.
The installed command reports package metadata with
`guild-private-connector --version`. Images have OCI version and source labels.

## Prepare a version

1. Update `pyproject.toml`, `Dockerfile`, and `helm/private-connector/Chart.yaml`.
2. Run `uv lock` and `uv run python scripts/check-release.py`.
3. Submit a PR. Wait for review and the runtime and packaging checks, then merge.

Published versions are immutable. Do not move an existing tag or overwrite an
image, chart, or release. Keep version selection explicit; deployments pin the
image digest rather than a mutable `latest` tag.

## Publish

Run **Actions → Private connector release → Run workflow** from `main`, with
the package version. The workflow creates the matching Git tag and GitHub
release after publication succeeds. Alternatively, push a `vMAJOR.MINOR.PATCH`
tag pointing to a commit already on `main`. Other source commits are rejected.

The serialized workflow checks for an existing release, image, and chart before
publishing. It runs the tests and a non-root, read-only container smoke test,
then publishes:

- `ghcr.io/guildaidev/private-connector:VERSION` for Linux amd64 and arm64,
  with SBOM and build provenance.
- `oci://ghcr.io/guildaidev/charts/private-connector` at the same version.
- A GitHub release with a Compose bundle, standalone Compose/configuration
  templates, a digest-pinned chart, `INSTALL.md`, `LICENSE`, `VERSION`,
  `release.json`, and `SHA256SUMS`.

Customer-specific configuration and enrollment tokens are not release assets.
The wizard's destinations are only an initial JSON helper. Users can add more
destinations on their host as services become available.

The final job pulls both image platforms and reads the chart without registry
credentials before making the GitHub release available. On the first publish,
a package administrator may need to set both GHCR packages to public. GitHub
does not inherit package visibility from the repository; see
[GitHub package visibility](https://docs.github.com/en/packages/learn-github-packages/configuring-a-packages-access-control-and-visibility).

If publication succeeds but the public-access check fails, set package visibility
and rerun only the failed `release` job. The successful `publish` job's artifacts
are retained for 30 days. Do not rerun the publisher for an existing version.
If the publisher fails after creating only one registry artifact, inspect the
failure and prepare a new version. Restrict registry publishing to this workflow.

Publication does not update customer installations. Customers choose an upgrade,
keep their identity volume and configuration, and restart using the new release.
