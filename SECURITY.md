# Security Policy

## Vulnerability scanning

Every image pushed to GHCR receives an SBOM (Software Bill of Materials) generated
by [Syft](https://github.com/anchore/syft) and attached as a signed attestation via
[cosign](https://github.com/sigstore/cosign) (keyless, OIDC-based).

A [Grype](https://github.com/anchore/grype) scan runs daily against that SBOM
(`.github/workflows/grype-scan.yml`). Results appear in the repository's
**Security → Code scanning** tab as SARIF findings. The scan never fails the
workflow — findings are triaged through VEX (see below).

## Triage process — VEX

[OpenVEX](https://openvex.dev/) statements in `security/vex.openvex.json` tell Grype
which CVEs are **not exploitable** in this specific deployment context, so they are
filtered out of scan results automatically. The same file is read by local `grype`
runs via `.grype.yaml`.

### Adding a `not_affected` statement

When a CVE is triaged and found not exploitable (wrong component, only affects
functionality not used, etc.), open a PR that adds an OpenVEX statement using
[`vexctl`](https://github.com/openvex/vexctl):

```sh
# Install vexctl
go install github.com/openvex/vexctl@latest

# Add a not_affected statement with a vocabulary justification
vexctl add \
  --in-place \
  --file security/vex.openvex.json \
  --product "pkg:oci/slskd-lidarr-bridge" \
  --vulnerability CVE-YYYY-NNNNN \
  --status not_affected \
  --justification vulnerable_code_not_in_execute_path \
  --impact-statement "Brief human-readable explanation of why this CVE does not affect this image"
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
