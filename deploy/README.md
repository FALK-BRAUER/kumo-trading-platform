# deploy/

Docker compose stacks and Dockerfiles. One stack per environment shape (`compose.paper.yml`).

- **Goes in:** compose files, Dockerfiles, a `README` per non-obvious knob
- **Stays out:** build logs, instance-specific values (those come from `instances/`)
