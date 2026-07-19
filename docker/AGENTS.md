# docker/ - Container Build And Compose Assets

## Purpose

The Docker image build and the two compose files: an end-user deployment example and the development support stack.

## Ownership

`Dockerfile`, `docker-compose.yml` (end-user example), `dev-services-compose.yml` (dev stack).

## Local Contracts

- `Dockerfile` installs the CI-built wheel (placed at `docker/dist` by the workflow, build context `./docker`) via `uv tool install`; `VOLUME /config`, `EXPOSE 8080`; env defaults include `DOCKER=true` and `AUTH=form`.
- The ENTRYPOINT is a restart loop around the `quasarr` binary: exit code 0 restarts after 2s, non-zero stops the container. `POST /api/restart` (which SIGINTs the process) relies on this contract - do not change the loop semantics without checking that endpoint.
- `dev-services-compose.yml` requires the `CONFIG_VOLUMES` env var; JDownloader, flaresolverr-next, and watchtower are active by default, while the *arr services and sponsorshelper exist only as commented examples (`uv run cli_tester.py` replaces them for most checks).
- Images are built by CI for amd64+arm64: `PullRequests.yml` pushes beta tags to ghcr (its docker jobs run only for dev→main PRs or dispatch/push on the dev branch); `Release.yml` pushes latest/version tags and also re-points the beta tags, on both ghcr and Docker Hub. `Release.yml`'s push trigger ignores `docker/**`, so a Dockerfile-only change merged to main does not produce new release images. There is no local build workflow to maintain.

## Work Guidance

(none beyond the contracts above)

## Verification

(none locally - image builds are exercised by the CI workflows)

## Child DOX Index

None.
