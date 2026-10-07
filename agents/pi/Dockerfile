# The pi agent bundle: Node and a pinned pi under /opt/lso-agent, copied onto each task image
# by `adapters/pi.py`. Official Node binaries need glibc 2.28, which every task image has.
# `bin/pi` runs the bundled Node directly, so Node is not put on the agent's shell PATH and
# the task environment stays as validated.
#
# The advisor plugin is bundled here from its sources into one extension file,
# extensions/lso-advisor.js (nothing is fetched at run time). Its sources come in as named
# build contexts, which adapters/pi.py passes:
#   --build-context advisor-core=plugin/advisor-core/src
#   --build-context pi-binding=plugin/pi-binding/src
#   --build-context plugin-scripts=plugin/scripts
ARG NODE_VERSION=24

FROM node:${NODE_VERSION}-bookworm-slim AS advisor
ARG ESBUILD_VERSION=0.28.2
WORKDIR /plugin
RUN npm install --no-audit --no-fund --no-save --no-package-lock "esbuild@${ESBUILD_VERSION}"
COPY --from=plugin-scripts bundle-pi.mjs scripts/
COPY --from=advisor-core . advisor-core/src/
COPY --from=pi-binding . pi-binding/src/
RUN node scripts/bundle-pi.mjs /out/lso-advisor.js

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
COPY limits.ts timeline.ts toolcall-nudge.ts /opt/lso-agent/extensions/
COPY --from=advisor /out/lso-advisor.js /opt/lso-agent/extensions/
COPY run-tests /opt/lso-agent/bin/run-tests
