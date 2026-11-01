# Pre-Open-Source Security Audit Report

**Repository:** AiS-Backend-API
**Audit type:** Read-only pre-open-source security audit
**Verdict:** **NOT READY FOR OPEN SOURCE**
**Changes made:** This report only. No source code, configuration, or Git history was changed.

## 1. Executive Summary

The audit found one confirmed medium-risk disclosure in deployment documentation: environment-specific infrastructure details are present in `docs/PORTAL_DEPLOYMENT.md` and `README.md`. No recognized credential-pattern matches were found in the accessible tracked text-file history, and no high-confidence exploitable application vulnerability was reported in the reviewed code paths. Neither result proves that secrets or vulnerabilities are absent.

Publication should wait until the deployment details and screenshots have been reviewed and the licensing and security-reporting gaps below are resolved. Production infrastructure and external dependency advisories could not be verified in this local, offline audit.

## 2. Critical Findings

No critical findings were identified in the accessible materials reviewed.

## 3. High Risk Findings

No high-risk findings were identified in the accessible materials reviewed.

## 4. Medium Risk Findings

### M-1. Deployment topology disclosed in documentation

- **Files/lines:** `docs/PORTAL_DEPLOYMENT.md:9-12,27,33`; `README.md:246`
- **Risk:** Medium — avoidable operational information disclosure.
- **Evidence:** The deployment guide contains an environment-specific internal host/address, checkout path, service/listener details, and tunnel-to-domain mapping. The README says a live deployment credential was used during smoke testing. No credential value was identified or is reproduced here.
- **Potential consequence:** The information does not itself provide Internet access or authentication, but could help an attacker with reconnaissance and expose operational practices.
- **Recommendation:** Replace hostnames, addresses, paths, and environment-specific routing with generic placeholders. Remove or generalize the live-credential-use statement. Review the referenced screenshots before release.
- **Confidence:** 9/10.

## 5. Low Risk Findings and Readiness Gaps

### L-1. Security-reporting policy is absent

- **File/line:** `SECURITY.md` — not present in the inspected repository; line not applicable.
- **Risk:** Low (open-source governance).
- **Potential consequence:** Reporters may disclose vulnerabilities publicly or have no clear way to contact maintainers.
- **Recommendation:** Add a security policy with a private reporting channel, supported versions, and coordinated disclosure expectations.

### L-2. Contribution guidance is absent

- **File/line:** `CONTRIBUTING.md` — not present in the inspected repository; line not applicable.
- **Risk:** Low (maintainability and contributor process).
- **Potential consequence:** Contributors may not know how to report bugs, run checks, or submit changes consistently.
- **Recommendation:** Add concise contribution and development instructions before inviting external contributions.

### L-3. Dependency reproducibility and advisory status are unresolved

- **Files:** `pyproject.toml`, `requirements.lock.txt`; specific lines were not recorded in the audit notes.
- **Risk:** Low to Medium (supply-chain assurance).
- **Evidence:** The project manifest uses version ranges for most dependencies; the lock file pins exact versions. Whether the documented installation process enforces the lock file and artifact hashes was not established. No local or network-backed vulnerability advisory check was available.
- **Potential consequence:** Different installations may resolve different dependency versions, and known vulnerable versions may go unnoticed.
- **Recommendation:** Make the supported, reproducible installation path explicit; verify the installed dependency set against an up-to-date advisory database and review licenses before release.

No specific exploitable application vulnerability was confirmed in the reviewed code paths. Controls observed included constant-time comparison for the admin bearer token, hashed high-entropy downstream API keys, authenticated inference routes, model permission checks, parameterized ORM queries, disabled provider redirects, restricted Mantle host construction, and rejection of remote image URLs. These observations do not certify every route or runtime configuration.

## 6. Secrets Inventory

Secret values are intentionally not included. Candidate values, if any, must remain masked in any follow-up materials.

| Category | Audit result |
|---|---|
| AWS access keys / Bedrock credentials | No recognized pattern match in accessible tracked text-file history. |
| Azure Foundry credentials | No recognized pattern match in accessible tracked text-file history. |
| OpenAI, Anthropic, OpenRouter keys | No recognized pattern match in accessible tracked text-file history. |
| JWT secrets / signing keys | No recognized pattern match in accessible tracked text-file history. |
| PostgreSQL passwords / credential-bearing URLs | No recognized pattern match in accessible tracked text-file history. |
| Telegram tokens | No recognized pattern match in accessible tracked text-file history. |
| SSH private keys / certificates | No recognized pattern match in accessible tracked text-file history. |
| Other passwords, tokens, credentials | No recognized pattern match in accessible tracked text-file history. |
| `.env.example` | Provider credentials, admin token, and database URL are empty placeholders. |
| Local `.env` / runtime secret files | None present in the inspected working tree. Untracked files and deployed copies are outside the evidence available. |

The pattern scan covered 289 reachable text-file versions and searched common credential formats, including cloud/provider keys, bearer tokens, GitHub tokens, Telegram token patterns, private-key markers, and credential-bearing PostgreSQL URLs. Pattern scans can miss encoded, transformed, uncommon, or application-specific secrets. A clean scan is not proof that no secret has ever been exposed.

## 7. Git History Findings

- The accessible local history contains three commits. The current branch and `main` point to the same commit; `origin/main` is available locally. No tags were present.
- The branch diff against `main` is empty. Relative to the initial release, the later commit adds files and does not delete tracked paths.
- The reachable history text scan covered 289 file versions and found no recognized credential-pattern matches.
- No unreachable Git objects were reported by the local check.
- This assessment covers only locally available refs and objects. It does not establish that all remote refs, deleted remote branches, other clones, or historical secrets outside the accessible object database are clean.
- No accidentally tracked `.env`, runtime secret file, log, database dump, or backup was identified in the inspected working tree. A complete inventory of deployed or untracked files was unavailable.

## 8. Infrastructure Exposure

### Confirmed disclosure

See **M-1**: `docs/PORTAL_DEPLOYMENT.md:9-12,27,33` and `README.md:246` disclose environment-specific deployment topology. Hostnames, addresses, and other operational values are deliberately omitted from this report.

### Not available for verification

- No Docker/Compose, nginx, systemd-unit, or `.github` configuration was present in the inspected repository.
- Documentation describes a Cloudflare Tunnel and systemd deployment, but the live host and actual service/tunnel configuration were unavailable.
- TLS enforcement, firewall rules, proxy headers, admin-route exposure, service-account permissions, and deployed secret handling therefore remain unverified.
- Two JPEG screenshots are present. OCR tooling was unavailable, so their visible contents could not be reliably inspected for sensitive information.
- No tracked logs, database dumps, or backup artifacts were identified. This does not cover external storage or deployment hosts.

## 9. Dependency Audit

- `requirements.lock.txt` specifies exact versions; `pyproject.toml` uses version ranges for most dependencies.
- The audit did not establish that the lock file is enforced by the documented install command or that dependency artifacts are hash-pinned.
- Vulnerability advisories were not checked: no local advisory database or network access was available. No claim that dependencies are vulnerability-free is made.
- **Required before release:** resolve the production install set reproducibly, run a dependency vulnerability/advisory scan, and retain its results with the release review.

## 10. License Compatibility

- **Project license:** No `LICENSE` file was present in the inspected repository.
- **Apache License 2.0 suitability:** It may be suitable for the project's original code, subject to the maintainers' ownership and policy choices. Compatibility cannot be concluded until dependency and bundled-asset licenses are reviewed and required notices are included.
- **Bundled assets:** Font assets are present without a locally verifiable license notice. Their redistribution terms must be established before publication.
- **Recommendation:** Select and add a license only after confirming rights to the code and assets; preserve third-party notices and verify dependency compatibility. This is not a legal opinion.

## 11. Required Remediation

1. Redact or generalize the environment-specific information in `docs/PORTAL_DEPLOYMENT.md:9-12,27,33` and `README.md:246`; inspect the JPEG screenshots for additional exposure.
2. Add a project license after confirming code ownership and compatibility of dependencies and bundled fonts/assets.
3. Add `SECURITY.md` with a private vulnerability-reporting channel and disclosure process.
4. Review the actual deployment configuration for TLS, firewalling, proxy behavior, admin access, service privileges, and secret storage; it was not available to this audit.
5. Run dependency vulnerability and license checks against the exact release dependency set; clarify how the lock file is enforced.
6. Recheck all Git refs and history available at publication time, and ensure the final public tree excludes local secrets, generated files, logs, dumps, and backups.

## 12. Open Source Readiness

| Area | Status |
|---|---|
| Source-code review | No high-confidence exploitable issue reported in reviewed paths; not a guarantee of absence. |
| Secrets | No recognized pattern matches in accessible tracked text history; deployed/untracked data not verified. |
| Git history | Three accessible commits reviewed; remote refs and other clones not covered. |
| Infrastructure | Deployment topology disclosure confirmed; live configuration unavailable. |
| Dependencies | Exact advisory status unknown; install reproducibility needs clarification. |
| License | No project `LICENSE`; bundled font terms unresolved. |
| README / deployment docs | Present, but environment-specific details require redaction and screenshots require review. |
| `SECURITY.md` | Absent. |
| `CONTRIBUTING.md` | Absent. |
| `.gitignore` | Coverage was not established by the available audit notes; verify that secrets, runtime state, logs, dumps, and backups are excluded. |
| Docker Compose / installation / deployment configuration | No Compose or infrastructure configuration was present; deployment details are documented but production setup was not inspected. |

**Final verdict: NOT READY FOR OPEN SOURCE.** The confirmed deployment-information disclosure and unresolved license, bundled-asset, and dependency checks should be addressed before publication. No source-code fixes or other changes have been made.
