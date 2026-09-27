# Toy task image for harness smoke tests: a tiny shell "library" with two bugs.
# Not part of any benchmark task set; real task images come from the task pipeline.
FROM alpine:3.22
RUN apk add --no-cache git
WORKDIR /testbed
COPY repo/ .
RUN git init -q && git add -A \
 && git -c user.name=toy -c user.email=toy@localhost commit -qm base
