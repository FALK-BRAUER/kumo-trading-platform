# docs/

Architecture and design docs for kumo-trading-platform. The source of truth for *why* the system is shaped this way.

Holds: architecture overview, the order/action catalog (evidence-graded tactics), multi-strategy lane design,
broker/data decisions. Does NOT hold: code, runtime config, secrets. Grew out of an earlier research codebase whose
order/action catalog seeded the first designs here.

`engineering-principles.md` is the public extract of the engineering rules in `CLAUDE.md` (#1046): same rules,
personal register and private identifiers removed. `CLAUDE.md` is the source; change both in one PR.
