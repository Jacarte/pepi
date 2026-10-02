# Build context is the frozen run/build directory, never the whole Pepi checkout.
ARG NODE_IMAGE
ARG TASK_IMAGE
FROM ${NODE_IMAGE} AS agent
ARG PI_VERSION
ARG LITELLM_VERSION
ARG SUBAGENTS_VERSION
ENV PI_CODING_AGENT_DIR=/opt/pepi-agent
ENV PATH=/opt/pi/node_modules/.bin:$PATH
RUN npm install --prefix /opt/pi "@earendil-works/pi-coding-agent@${PI_VERSION}"
COPY profile/ /opt/pepi-agent/
RUN pi install "npm:pi-provider-litellm@${LITELLM_VERSION}" \
 && pi install "npm:pi-subagents@${SUBAGENTS_VERSION}"

FROM ${TASK_IMAGE}
USER root
COPY --from=agent /usr/local/ /usr/local/
COPY --from=agent /opt/pi/ /opt/pi/
COPY --from=agent /opt/pepi-agent/ /opt/pepi-agent/
COPY instructions.md /opt/pepi-eval/instructions.md
RUN apt-get update && apt-get install -y --no-install-recommends procps \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --uid 10101 --create-home --shell /bin/bash pepi-eval \
 && mkdir -p /output/tmp /input \
 && chown -R 10101:10101 /opt/pepi-agent /output /testbed
ENV TMPDIR=/output/tmp
ENV HOME=/home/pepi-eval
ENV PI_CODING_AGENT_DIR=/opt/pepi-agent
ENV PATH=/opt/pi/node_modules/.bin:/usr/local/bin:$PATH
WORKDIR /testbed
CMD ["sleep", "infinity"]
