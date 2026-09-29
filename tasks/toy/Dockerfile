# Toy task image for harness smoke tests: a tiny shell "library" with two bugs.
# Not part of any benchmark task set; real task images come from the task pipeline.
# Debian rather than Alpine: agent bundles (Node) need glibc.
FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /testbed
COPY repo/ .
RUN git init -q && git add -A \
 && git -c user.name=toy -c user.email=toy@localhost commit -qm base
