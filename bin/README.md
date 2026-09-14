# bin/

Shell checks the Makefile runs before any deploy, and the public-tree guard.

- **Goes in:** one `check-*.sh` per property a deploy must satisfy, each with a test under `bin/tests/`
- **Stays out:** anything that is not executable code; secrets have never lived here
