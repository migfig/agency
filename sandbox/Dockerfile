# agency-sandbox:v1 — sandbox image for tool-call execution (spec 006, contract §9).
# Small non-root runtime: bash (shell tool), coreutils (sleep/mkdir/cp),
# python3 (program tool + artifact copy-out scripts). Package caches are
# removed to keep the image at or under ~200 MB. Runtime user is uid/gid
# 1000 (SandboxPolicy.user); /scratch is the working directory (a bounded
# tmpfs at runtime).
FROM debian:bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        coreutils \
        python3 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*

RUN groupadd -g 1000 sandbox \
    && useradd -u 1000 -g 1000 -s /bin/bash sandbox

USER sandbox
WORKDIR /scratch
