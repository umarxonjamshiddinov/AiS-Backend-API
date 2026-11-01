# Security Review Summary

**Audit:** Pre-open-source security audit of AiS-Backend-API
**Detailed report:** [`SECURITY_AUDIT_REPORT.md`](SECURITY_AUDIT_REPORT.md)
**Initial verdict:** **NOT READY FOR OPEN SOURCE**
**Post-remediation verdict:** **NOT READY FOR OPEN SOURCE**

## Findings

| ID | Severity | Location | Finding | Status |
|---|---|---|---|---|
| M-1 | Medium | `docs/PORTAL_DEPLOYMENT.md`; `README.md` | Deployment details and a live-credential-use statement were disclosed. No credential value is reproduced. | Addressed in the requested files with generic deployment instructions; screenshots still need review. |
| L-1 | Low | `SECURITY.md` | Security reporting guidance was absent. | Addressed; private reporting is conditional on repository feature availability. |
| L-2 | Low | `CONTRIBUTING.md` | Contributor guidance was absent. | Addressed with setup and local check instructions. |
| M-2 | Medium | `requirements.lock.txt:7` | `certifi==2026.7.22` was affected by GHSA-6q48-6c7w-9gw2 / PYSEC-2026-4320. | Updated to `2026.10.19`; follow-up `uv audit` found no known vulnerabilities in the 50 pinned packages. |
| M-3 | Medium | `requirements.lock.txt:25` | `opentelemetry-api==1.45.0` was affected by GHSA-xcq7-p3pf-p88h / CVE-2026-114603. Application-level reachability was not established. | Updated to `1.45.1`; follow-up `uv audit` found no known vulnerabilities in the 50 pinned packages. |
| L-4 | Low–Medium | Project license; bundled font assets and dependencies | Project license and third-party redistribution terms remain unverified. | Review upstream terms and ownership; no license was invented or added. |

## Scope and limitations

The initial read-only review covered the working tree, accessible local refs/history (three commits; no tags), and 289 reachable text-file versions. No recognized credential-pattern matches were found. No high-confidence exploitable application vulnerability was reported in the reviewed paths. These results do not prove the absence of secrets or vulnerabilities.

The follow-up review redacted the previously identified deployment details in `README.md` and `docs/PORTAL_DEPLOYMENT.md`, added `SECURITY.md` and `CONTRIBUTING.md`, and updated two vulnerable exact pins in `requirements.lock.txt`. The installed `uv audit` queried OSV-backed advisories for the 50 pinned packages; the follow-up reported no known vulnerabilities or adverse statuses. The README and contributor guide now install the pinned requirements before the editable package. Installation was not run because no Python 3.13 environment is present. License metadata remains unavailable locally, so font and dependency redistribution terms are unresolved. Production infrastructure, deployed/untracked files, and other clones/remote history remain out of scope. Two JPEG screenshots could not be reliably OCR-reviewed. Refer to the detailed report for the full secrets inventory, limitations, and remediation status.

No source code, production configuration, or Git history was changed.
