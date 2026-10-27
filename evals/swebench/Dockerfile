# Build context is the frozen run/build directory, never the whole Pepi checkout.
ARG NODE_IMAGE
ARG TASK_IMAGE
FROM ${NODE_IMAGE} AS agent
ARG PI_VERSION
ARG LITELLM_VERSION=""
ARG SUBAGENTS_VERSION
ENV PI_CODING_AGENT_DIR=/opt/pepi-agent
ENV PATH=/opt/pi/node_modules/.bin:$PATH
RUN npm install --prefix /opt/pi "@earendil-works/pi-coding-agent@${PI_VERSION}"
COPY profile/ /opt/pepi-agent/
RUN if [ -n "$LITELLM_VERSION" ]; then pi install "npm:pi-provider-litellm@${LITELLM_VERSION}"; fi \
  && pi install "npm:pi-subagents@${SUBAGENTS_VERSION}"
# Package installation may persist declarations; restore the exact frozen settings.
COPY profile/settings.json /opt/pepi-agent/settings.json
COPY auth-audit.cjs /opt/pepi-eval/auth-audit.cjs
COPY mcp/ /opt/pepi-mcp/
COPY install-mcp.mjs /opt/pepi-eval/install-mcp.mjs
RUN node /opt/pepi-eval/install-mcp.mjs
# Node images link yarn into /usr/local/bin from /opt/yarn-*, which is not copied below: drop the dangling links so
# they cannot hide a task image's own yarn (facebook__docusaurus-10130 grades with `yarn test`).
RUN rm -f /usr/local/bin/yarn /usr/local/bin/yarnpkg

FROM ${TASK_IMAGE}
USER root
# Pi's Node gets its own prefix, searched after the task image's PATH: a task's own node/npm/npx/yarn (Node 20 in
# the Multilingual JS images) stays the one the agent's commands run; images without Node fall back to Pi's.
# Pi itself always starts on its own Node through /opt/pepi-eval/bin/pi (it needs Node >= 22).
COPY --from=agent /usr/local/ /opt/pepi-node/
COPY --from=agent /opt/pi/ /opt/pi/
COPY --from=agent /opt/pepi-agent/ /opt/pepi-agent/
COPY --from=agent /opt/pepi-mcp/ /opt/pepi-mcp/
COPY instructions.md /opt/pepi-eval/instructions.md
COPY --from=agent /opt/pepi-eval/auth-audit.cjs /opt/pepi-eval/auth-audit.cjs
# Debian/Ubuntu task images only (SWE-bench Python, Multilingual): fail clearly on anything else.
# CARGO_HOME (Rust images) and GEM_HOME (Ruby images) hold the caches the image's setup filled as root; the agent
# UID owns them so cargo/bundler can lock, update and add to them. Their locations, and RUSTUP_HOME, stay as set.
# A cache holding the controller's PATH (runner.CONTROLLER_ENV) or Pi's own files would hand those to the agent.
RUN command -v apt-get >/dev/null || { echo "pepi-eval: the task image needs apt-get (Debian/Ubuntu)" >&2; exit 1; } \
  && apt-get update && apt-get install -y --no-install-recommends procps \
  && rm -rf /var/lib/apt/lists/* \
  && useradd --uid 10101 --create-home --shell /bin/bash pepi-eval \
  && mkdir -p /output/tmp /input /opt/pepi-eval/bin \
  && printf '#!/bin/sh\nexec /opt/pepi-node/bin/node /opt/pi/node_modules/.bin/pi "$@"\n' > /opt/pepi-eval/bin/pi \
  && chmod 755 /opt/pepi-eval/bin/pi \
  && for cache in "${CARGO_HOME:-}" "${GEM_HOME:-}"; do \
       if [ -n "$cache" ] && [ -d "$cache" ]; then \
         for kept in /usr/sbin /usr/bin /sbin /bin /opt/pi /opt/pepi-node /opt/pepi-eval /opt/pepi-mcp; do \
           case "$kept/" in "${cache%/}"/*) echo "pepi-eval: refusing to give $cache (holds $kept) to the agent UID" >&2; exit 1;; esac; \
         done; \
         chown -R 10101:10101 "$cache"; \
       fi; \
     done \
  && chown -R 10101:10101 /opt/pepi-agent /output /testbed
ENV TMPDIR=/output/tmp
ENV HOME=/home/pepi-eval
ENV PI_CODING_AGENT_DIR=/opt/pepi-agent
ENV PATH=/opt/pepi-eval/bin:$PATH:/opt/pepi-node/bin
WORKDIR /testbed
# auth.json and mcp.json are never built in: `run --secrets-dir` streams them into a tmpfs at
# /run/pepi-secrets and symlinks them into /opt/pepi-agent at container start.
# The controller launches Pi as UID 10101 and kills that UID before patch export.
CMD ["sleep", "infinity"]
