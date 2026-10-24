# Security Policy

Do not report a suspected vulnerability or exposed credential in a public issue. Email **security@sultangreen.com** with the minimum detail needed to investigate: what you found, where, and how to reproduce it. Do not include real credentials or private data. GitHub private vulnerability reporting is also enabled on this repository; either route is fine.

Do not send secrets through public discussions, pull requests, or issue attachments. If you accidentally publish a credential, revoke or rotate it immediately; deleting the visible text does not remove it from Git history or notifications.

Contributor and pull-request workflows use synthetic test data and no secrets. Production secrets exist only in the `production` environment (limited to `main`), read by `runtime.yml`, and hold a restricted database role's token (see docs/MAINTAINING.md). Please report any path that appears to let an untrusted pull request access credentials, private data, or production services.
