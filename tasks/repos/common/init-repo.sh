#!/bin/sh
# Make /testbed a fresh one-commit repository, so the fix is not reachable in history.
# Submodules (paths in /opt/lso/submodules, deepest first) become nested one-commit
# repositories first, so build scripts that look for their .git find one.
set -eu
commit() {
  git init -q
  git -c advice.addEmbeddedRepo=false add -Af
  git -c user.name=base -c user.email=base@localhost commit -qm base
}
cd /testbed
while read -r path; do
  (cd "$path" && commit)
done < /opt/lso/submodules
commit
