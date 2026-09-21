# Production baseline snapshot — 2026-09-21

This baseline was captured from the running production directory
`/root/ai-business-qa-bot` on `cdo-internal-server-00`.

The snapshot intentionally excludes runtime and sensitive material:

- `.env` and all credentials;
- `.venv` and Python caches;
- `backups`, `output`, `tmp`, logs, locks, and local databases;
- imported or generated CSV, Excel, Parquet, and database files;
- `data_import/data` and `data_import/output`.

The three active production services at capture time were:

- `ai-bot-receiver.service`;
- `ai-bot-workers.service`;
- `ai-bot-document-workers.service`.

The live receiver unit requires Redis. The checked-in receiver template was
aligned with that live dependency while preparing this baseline. No service
was restarted or modified during capture.

The source archive used for review contained 268 files and had SHA-256:

`60a1819e2d09c4540b1436e3379613a930823691f30c806b999a2987ac003d00`

The archive itself is not tracked. Future production deployments should be
created from a reviewed Git commit rather than copied from a dirty working
directory.

## Verification at capture time

- A credential-pattern scan reported no findings in the sanitized source.
- The archive checksum matched after transfer from the production server.
- A credential-free full unit-test run discovered 435 tests and completed with
  131 failures and 24 errors. Failures include routes that require the model or
  database environment and tests whose expected feature-flag defaults differ
  from the running production configuration. This is recorded as the existing
  baseline; production behavior was not changed to make the snapshot pass.
