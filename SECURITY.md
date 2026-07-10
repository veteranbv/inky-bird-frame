# Security Policy

## Reporting a vulnerability

Do not open a public issue for a suspected vulnerability. Use GitHub's
[private vulnerability reporting](https://github.com/veteranbv/inky-bird-frame/security/advisories/new)
and include the affected revision, impact, reproduction steps, and any proposed
mitigation. Remove unrelated credentials, locations, and personal data.

The maintainer will acknowledge a report as soon as practical, investigate it,
and coordinate disclosure after a fix is available. Please do not disclose an
unresolved vulnerability publicly.

## Supported code

Security fixes target the current `main` branch. This project has not yet begun
publishing versioned releases with separate support windows.

## Trust boundaries

- Pull requests from forks run only deterministic checks on GitHub-hosted runners.
- External pull requests do not receive Codex, notification, deployment, or publishing credentials.
- Trusted controller workflows must never execute code from an untrusted pull request.
- Catalog submissions are treated as untrusted data until deterministic and independent review pass.
