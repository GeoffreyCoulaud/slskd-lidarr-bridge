# Security Policy

## Vulnerability scanning

Every image pushed to GHCR gets three signed attestations (keyless, OIDC-based, via
[cosign](https://github.com/sigstore/cosign)):

- a **CycloneDX** SBOM — the standard, portable [Syft](https://github.com/anchore/syft)
  bill of materials for external consumers;
- a **Syft-JSON** SBOM — Syft's native format, consumed by the daily scan because it
  preserves the image source identity that image-scoped VEX matching needs (see below);
- an **[OpenVEX](https://openvex.dev/)** document (`security/vex.openvex.json`) — the
  triage marking non-exploitable CVEs as `not_affected`.

A [Grype](https://github.com/anchore/grype) scan runs daily against the attested
Syft-JSON SBOM (`.github/workflows/grype-scan.yml`), applying the VEX. Results appear in
the repository's **Security → Code scanning** tab as SARIF findings. The scan never
fails the workflow — findings are triaged through VEX (see below).

## Triage process — VEX

[OpenVEX](https://openvex.dev/) statements in `security/vex.openvex.json` tell Grype
which CVEs are **not exploitable** in this deployment context, so they are filtered out
of scan results automatically. The file is versioned here (the source of truth) and
attached to each released image as a signed OpenVEX attestation: the daily scan pulls it
**from the image** — nothing VEX-related is read from the repo checkout — and anyone who
scans the image directly inherits the triage. For a local run, point Grype at the file
explicitly with `--vex security/vex.openvex.json`.

### Statements are image-scoped, and the scan reads the Syft-JSON SBOM

Grype resolves a VEX statement in two passes: by the **image** identity (`pkg:oci/...`)
then by the vulnerable **package** PURL. We use the **image-scoped** form — product
`pkg:oci/slskd-lidarr-bridge` with the vulnerable package as a `subcomponent` — because
it is the only form that is safe to attach and redistribute: it is scoped to *this*
image, so a downstream consumer's unrelated `python`/`busybox` is never suppressed by
our statements. Use the subcomponent PURL **without a version** so a statement survives
package bumps (a specific CVE only matches the package it was filed against).

For that to work the scan must expose the image identity to Grype. A **CycloneDX** SBOM
drops it (the image-scoped product then matches nothing); Syft's native **Syft-JSON**
format preserves it (`.source` carries the image tags + repoDigests). That is why the
daily scan reads the attested Syft-JSON SBOM — and why local verification must use
Syft-JSON too, not CycloneDX.

Known limitation: an attached image-scoped VEX only helps consumers who scan **this
image directly**. Someone who bundles it into a larger image scans under a different
image identity, so the statements won't apply — cross-image VEX propagation is still
immature in the tooling.

### Adding a `not_affected` statement

When a CVE is triaged and found not exploitable (wrong component, only affects
functionality not used, etc.), open a PR that adds an OpenVEX statement using
[`vexctl`](https://github.com/openvex/vexctl):

```sh
# Install vexctl
go install github.com/openvex/vexctl@latest

# Add a not_affected statement. --product is THIS image; --subcomponents is the
# vulnerable package's PURL (find it in the Grype finding / SBOM), without a version.
# For a CVE that lands on several packages, pass them comma-separated.
vexctl add \
  --in-place \
  --file security/vex.openvex.json \
  --product "pkg:oci/slskd-lidarr-bridge" \
  --subcomponents "pkg:generic/python" \
  --vulnerability CVE-YYYY-NNNNN \
  --status not_affected \
  --justification vulnerable_code_not_in_execute_path \
  --impact-statement "Brief human-readable explanation of why this CVE does not affect this image"
```

After editing, verify the suppression actually applies before opening the PR. Use a
**Syft-JSON** SBOM — CycloneDX would silently no-op the image-scoped statements:

```sh
syft <image> -o syft-json=/tmp/sbom.syft.json
grype sbom:/tmp/sbom.syft.json --vex security/vex.openvex.json
grype sbom:/tmp/sbom.syft.json --vex security/vex.openvex.json --show-suppressed | grep <CVE>
```

Valid `--justification` values (OpenVEX vocabulary):
- `component_not_present`
- `vulnerable_code_not_present`
- `vulnerable_code_cannot_be_controlled_by_adversary`
- `vulnerable_code_not_in_execute_path`
- `inline_mitigations_already_exist`

The PR description must explain the triage rationale. At least one reviewer must
approve before merging. Do **not** add CVEs to ignore lists without a VEX statement —
this ensures all suppressions are auditable and signed.

## Reporting a security issue

To report a vulnerability privately, use GitHub's
[private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing/privately-reporting-a-security-vulnerability)
feature for this repository.
