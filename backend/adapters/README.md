# adapters/

Glue between the platform and the venue adapters it pins (Nautilus IB, kumo-nautilus-alpaca-adapter).

- **Goes in:** provider registry entries and thin wiring, each naming the package it wires
- **Stays out:** adapter implementations — a full adapter is its own package
