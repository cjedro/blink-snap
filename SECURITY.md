# Security Policy

## Supported Versions

| Version | Supported |
| ------- | --------- |
| latest tag | yes |

## Reporting a Vulnerability

Please **do not** open a public GitHub issue for security vulnerabilities.

Use [GitHub private vulnerability reporting](https://github.com/cjedro/blink-snap/security/advisories/new) for this repository.

Include:

- A clear description of the issue and potential impact
- Steps to reproduce
- Affected versions or commits, if known

We aim to acknowledge reports within a few days and will coordinate a fix and disclosure timeline with you.

## Scope

In scope:

- This repository's source code (CLI, export server, Docker image build)
- Accidental exposure of credentials via the app or documentation

Out of scope:

- Blink/Amazon cloud infrastructure
- Compromise of a user's own `.env`, `blink_session.json`, or host system

## Safe Usage

- Never commit `.env`, `blink_session.json`, or capture files
- Treat `blink_session.json` like a password — it grants camera access
- Use a strong `EXPORT_PASSWORD` if you expose the export web UI
