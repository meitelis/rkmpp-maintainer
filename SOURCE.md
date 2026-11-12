# Source history

Extracted on 12 November 2026 from `automation/rkmpp-maintainer/` in
[meitelis/memories-rkmpp PR #26](https://github.com/meitelis/memories-rkmpp/pull/26),
at source commit `b79d827d262c5e451bad80f4e4dfe307773d418e`.

The original source commits, authored by `meitelis`, are:

- [75cf157](https://github.com/meitelis/memories-rkmpp/commit/75cf1575202c7ab1157422d0031ad355804f6ff9): Prototype local RKMPP release maintainer with isolated builds and local model smoke.
- [b79d827](https://github.com/meitelis/memories-rkmpp/commit/b79d827d262c5e451bad80f4e4dfe307773d418e): Use production 7B model for real agent smoke and improve file-tool feedback.

The standalone repository imports the final source snapshot. The original
commits and discussion remain available through the closed source PR; unrelated
Memories history is not imported here.

The existing ARM64 CI workflow moved from
`.github/workflows/maintainer-prototype.yml` in Memories to
`.github/workflows/maintainer.yml` here. Its commands now run from this repository's
root. The source repository's Apache-2.0 license is retained in [LICENSE](LICENSE).

The extraction changes repository layout and documentation; it does not change
the Python controller, model configuration, tests or Docker Compose services.
The earlier prototype's validation is recorded in PR #26. Consult this
repository's own Actions results for validation after the move.
