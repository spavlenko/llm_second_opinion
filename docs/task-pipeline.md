# Task pipeline: notes

How the C++ task set is built, and what we learned building it. The design is in
[spec.md](spec.md#task-pipeline); this page is the practical side.

## Running it

```sh
cd harness
.venv/bin/bench tasks import --dataset mini                  # 50 candidates -> mswe-mini-cpp
.venv/bin/bench tasks build mswe-mini-cpp --parallel 2 --jobs 5
.venv/bin/bench tasks validate mswe-mini-cpp --parallel 2
.venv/bin/bench tasks freeze mswe-mini-cpp --version mswe-mini-cpp-v1 \
  --out ../tasks/manifests/mswe-mini-cpp-v1.yaml
```

Every stage resumes: a record per candidate goes in `runs/tasks/mswe-mini-cpp/`
(`builds.json`, `validation.json`), with logs per candidate (`build.log`, `before-N.log`,
`after-N.log`). Delete a record, or pass `--rebuild` / `--revalidate`, to redo it. Dataset
downloads and git mirrors are cached in `.cache/`.

Images are local to the machine that built them. The manifest pins each one by image ID;
rebuilding an image gives it a new ID, so the tasks have to be validated and frozen again.

## Dataset: Multi-SWE-bench mini, C++

- `ByteDance-Seed/Multi-SWE-bench_mini`, revision `d0fab3cc`, one JSONL file (checksum
  pinned). The C++ part has 50 instances from 4 repositories: nlohmann/json 21, fmtlib/fmt 17,
  simdjson 8, Catch2 4. All four build with CMake and test with ctest.
- `full` is registered too (revision `56ff018c`, one file per repository). It adds
  yhirose/cpp-httplib, which needs a recipe in `tasks/repos/` before it can be built.
- The problem statement is the text of the issues the pull request resolved. The pull request's
  own title and body are left out, because they describe the fix.

## Recipes

Each repository has `tasks/repos/<org>__<repo>/Dockerfile` and `recipe.yaml`, plus shared
scripts in `tasks/repos/common/`:

- `recipe.yaml` gives the git URL, the log parser, and the GCC version by PR number. The
  ranges follow the upstream harness; upstream's `gcc:latest` is pinned to 14. The official
  `gcc` images have arm64 variants and already include git, python3, and curl.
- `install-cmake.sh` installs CMake 3.31.12 from Kitware's release tarballs (arm64 or
  x86-64, checksum-verified). Upstream downloads an x86-64-only CMake 3.14 for old simdjson;
  3.31 is the last series that still configures projects asking for CMake < 3.5.
- `init-repo.sh` turns `/testbed` into a fresh one-commit repository, so an agent cannot
  find the fix in later history.
- `run-tests` is the tasks' eval command. It rebuilds `/build` incrementally and runs
  ctest.
- The project is configured and built at image build time. That puts a build of about 90 s
  in the image, so grading only rebuilds what a patch touches.

Measured on an M-series Mac with Docker Desktop (12 CPUs, 8 GB VM):
- **Image build:** 5 s to 2 min per image.
- **Image size:** each image adds about 400 MB of its own on top of shared layers (nlohmann/json).
- **Test suite:** about 2 min for the nlohmann/json suite.

## Problems found and fixes

1. **Upstream's test lists are too coarse.** Upstream runs its tests with `set -e`, so on its
   x86-64 runs, one test target that fails to compile with the test patch stops the whole
   suite. Every test then counts as "fixed" (`n2p`: absent before, passes after). For
   nlohmann/json 4536, upstream lists 98 fixed tests; only 1 actually fails before the fix.
   **Fix:** build with `make -k` so the other targets still build. Then re-derive the lists
   from our own runs: fail-to-pass = passes with the gold patch but not before; pass-to-pass =
   passes both times. Upstream's lists are kept as a cross-check: every upstream fixed test
   must pass with the gold patch.
2. **Stale binaries could make a broken patch look resolved.** With an incremental build,
   a test target that no longer compiles keeps the binary from the image's build, and ctest
   runs that. Catch2 2288 "passed" with the test patch alone this way. An agent patch that
   breaks the build would have been graded as resolved. **Fix:** `run-tests` deletes linked
   outputs (executables, `.a`, `.so`) before building and keeps object files. A target that
   fails to compile then shows as "Not Run", and the build stays incremental.
3. **Submodules.** simdjson 958's CMake runs `git submodule update` for its dependencies,
   including one it always needs (`cxxopts`). A plain checkout of the base commit has none.
   Turning off the options that use submodules was not enough. **Fix:** the builder checks
   out each submodule at its recorded commit, recursively, from local mirrors. `init-repo.sh`
   makes each one a nested one-commit repository, so scripts that look for its `.git` find
   one.
4. **Coverage tooling in old builds.** nlohmann/json 18 (2015) always sets up a coverage
   target that requires `lcov`. **Fix:** the recipe points `LCOV_PATH` and `GENHTML_PATH` at
   `/bin/true`; the tests do not use them.
5. **`git archive` can drop files.** `.gitattributes` `export-ignore` removes them from an
   archive. **Fix:** the builder checks out the tree with a separate index instead.
6. **Recipe changes mid-batch.** Images built before and after a recipe change differ, so
   when a recipe changes, all of that repository's images are rebuilt and validated again. The
   test script is copied into the image last, so changing it does not recompile the project.

7. **`char` is unsigned on arm64 Linux.** fmt 3271's gold patch fails `chrono-test`: the test
   formats `duration<char>(0x80)` and expects `-01.28`, which only holds where `char` is
   signed (x86-64). The task is dropped as `gold_does_not_pass`. Building with
   `-fsigned-char` would keep it; we have not done that, since it changes how every task's
   code compiles in order to save one.

## Results

`tasks/manifests/mswe-mini-cpp-v1.yaml` (frozen 2026-09-27): **49 of 50 kept**, 1 dropped
(fmt 3271; see problem 7). The split uses seed 0 and puts half of each repository in `test`:

| Repository | dev | test |
| --- | ---: | ---: |
| nlohmann/json | 10 | 11 |
| fmtlib/fmt | 8 | 8 |
| simdjson/simdjson | 4 | 4 |
| catchorg/Catch2 | 2 | 2 |
| **Total** | **24** | **25** |

- **Fail-to-pass tests per task:** median 1, maximum 24. The 24 are Catch2 2288's, where every
  test runs from one `SelfTest` binary.
- **Excluded pass-to-pass test:** one upstream pass-to-pass test fails here with the gold
  patch and is left out: fmt 3248 `chrono-test`, again because of `char` signedness.
- **Image builds:** median 35 s, longest 70 s (5 jobs, 2 in parallel). No candidate came near
  the 20-minute cap.
- **Test run with the gold patch:** median 39 s, longest 189 s. No candidate came near the
  10-minute cap.
- **Validation time:** about 3 hours for all 50 (4 runs each, 2 candidates in parallel).
- **Disk:** about 45 GB of images in Docker Desktop. This includes images superseded by
  rebuilds; `docker image prune` reclaims those.

Smoke subset (`mswe-mini-cpp-v1-smoke.yaml`): nlohmann/json 18, Catch2 1616, simdjson 524.
`experiments/mswe-smoke-gold.yaml` runs their gold patches through `bench run`, and all 3
resolve.
