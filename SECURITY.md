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

Security fixes target the current release and `main`. Older releases do not
have separate support windows yet.

## Trust boundaries

- Pull requests from forks run only deterministic checks on GitHub-hosted runners.
- External pull requests do not receive Codex, notification, deployment, or publishing credentials.
- Trusted controller workflows must never execute code from an untrusted pull request.
- Catalog submissions are treated as untrusted data until deterministic checks
  and an independent review pass.
- Container images publish only from trusted `main`, a repository release, or
  an owner-started workflow. Pull requests can build images but cannot publish
  them.
