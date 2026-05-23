# Contributing

PRs and issues welcome. A few orientations before you start.

## Help especially welcome

- **Tested telemetry from other inverter variants.** YC600 and DS3 are
  validated against real hardware. QS1 / QS1A decode formulas exist but
  are untested. 3-phase variants (YC1000, DS3-D, QT2) need a new decoder.
  Captures + decoded readings get them supported quickly.
- **Bug fixes** in firmware (`firmware/`), daemon, web UI, decoder.
- **A CI pipeline** that builds firmware + runs decoder tests on push.
- **EFR32 / ESP32-C6 firmware port** — same protocol, different radio chip.
  Tracked as open issues.
- **Documentation, screenshots, install guides** for distros / setups
  not yet covered.

## What I'd rather you not PR

- **kadzsol firmware binaries.** License is CC BY-NC-SA 3.0; canonical
  distribution channel is Koenkk/zigbee2mqtt#4221 and the kadzsol Discord.
- **Inverter-control / curtailment features** that go beyond reading
  telemetry. APsystems inverters have undocumented commands and pushing
  power-limit changes blindly is a way to break units.
- **Rewrites to a "proper" framework** (Flask, FastAPI, React) for style.
  The whole point is zero-dep, runs on a Pi Zero, no Node build.
  Functional improvements are different.

## Style

- Code reads like the surrounding code: match its idiom, naming, density.
- Pure Python where possible — stdlib over package.
- Each commit should leave the daemon and web UI runnable.
- Test new decoder formulas against your own captured frames before opening a PR.

## Reporting protocol findings

The protocol is a moving target. If you find:
- An inverter that doesn't respond to our current pair sequence
- A different on-air opcode or cluster ID in the wild
- A firmware version mismatch (kadzsol revs)

… open an issue with the raw byte capture from any 802.15.4 analyzer, the
inverter model + S/N prefix (you can redact the trailing bytes), and what
you tried.

## Reporting security issues

See [`SECURITY.md`](SECURITY.md). Use a confidential issue for anything
that could compromise users running the daemon on an exposed network.

## Code of conduct

Be a good homelab citizen. We're all working with hardware we own, on
networks we run, learning a system the vendor doesn't document. Help when
you can; don't gatekeep.
