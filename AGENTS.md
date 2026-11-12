# Radxa deployment requirements

- The production checkout and persistent application data belong under `/armbian/rkmpp-maintainer`. Do not install this service in the user's home directory.
- Portainer owns the production stack. Deploy and update `portainer-stack.yml` from Portainer, using stack name `rkmpp-maintainer`.
- Do not use `docker compose up`, `down`, or `run` to manage the production stack. The documented, guarded one-time migration is the exception for removing the legacy CLI-owned project.
- Host image builds are allowed preparation steps. Run maintenance and test jobs from the Portainer maintainer container's Console while its action is `idle`.
- `compose.yml` is for isolated development and CI only.
- Preserve downloaded models, job evidence and builder cache during migration. Never move the workspace or stop its builder while a maintenance job is running.
- Keep automatic builds and production deployment disabled unless the user has explicitly requested enabling them. Candidate images are deployed through the existing application stacks in Portainer.
