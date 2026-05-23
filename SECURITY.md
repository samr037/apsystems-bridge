# Security policy

## Reporting a vulnerability

If you find a security issue in this project — host code, firmware, web UI,
build pipeline, or documentation that could mislead an operator into an
insecure deployment — please report it before publicly disclosing.

**Preferred channel:** use GitHub's private vulnerability reporting —
the repository's **Security** tab → **Report a vulnerability**. This
opens a draft advisory visible only to the maintainers.

**Email:** if private reporting is unavailable to you, email the
maintainer (see the GitHub profile linked from the repository). PGP
encryption welcome; ask for the current key fingerprint by replying to
the first response.

Please include:

- A description of the issue and its impact.
- Step-by-step reproduction (or a proof-of-concept script).
- The affected version (git commit hash) and your environment
  (host OS, dongle model, firmware version).
- Whether you have already disclosed this to anyone else.

## Scope

In scope:

- Firmware on the CC2652P (`firmware/`).
- Host daemon, web UI, and protocol layer (`host/`).
- Build pipeline and CI configuration once present.
- Documentation that could lead a reader into an insecure deployment.

Out of scope (please report upstream):

- The third-party kadzsol firmware (Z-Stack Home 1.2 with NWK security
  disabled) — that is a separate project and its security posture is by
  design (APsystems inverters require unencrypted frames).
- The TI SimpleLink SDK we build against.
- The APsystems inverter firmware itself.

## Response

This is a hobbyist open-source project with no formal SLA. Best-effort
response within 7 days; mitigation timeline depends on severity. Critical
issues affecting deployed users will be prioritised.

## Hall of fame

Researchers who report valid issues will be credited in the corresponding
GitHub security advisory and in `THIRD_PARTY_NOTICES.md` unless they request otherwise.
