# The pi agent bundle: Node and a pinned pi under /opt/lso-agent, copied onto each task image
# by `adapters/pi.py`. Official Node binaries need glibc 2.28, which every task image has.
# `bin/pi` runs the bundled Node directly, so Node is not put on the agent's shell PATH and
# the task environment stays as validated.
ARG NODE_VERSION=24
FROM node:${NODE_VERSION}-bookworm-slim
ARG PI_VERSION
RUN test -n "$PI_VERSION" \
 && mkdir -p /opt/lso-agent/bin \
 && cp /usr/local/bin/node /opt/lso-agent/bin/node \
 && npm install -g --ignore-scripts --no-audit --no-fund --prefix /opt/lso-agent/pi \
      "@earendil-works/pi-coding-agent@${PI_VERSION}" \
 && pkg=/opt/lso-agent/pi/lib/node_modules/@earendil-works/pi-coding-agent \
 && cli="$pkg/$(node -p "require('$pkg/package.json').bin.pi")" \
 && printf '#!/bin/sh\nexec /opt/lso-agent/bin/node %s "$@"\n' "$cli" > /opt/lso-agent/bin/pi \
 && chmod +x /opt/lso-agent/bin/pi
COPY limits.ts timeline.ts /opt/lso-agent/extensions/
