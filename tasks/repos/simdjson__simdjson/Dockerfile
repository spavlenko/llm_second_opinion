# simdjson/simdjson task image: the repo at one base commit in /testbed, configured and built in /build.
# Built by `bench tasks build`, which supplies src/ (the base commit's files) and common/.
# It sets GCC per task from recipe.yaml.
ARG GCC=14
FROM gcc:${GCC}
COPY common/install-cmake.sh /opt/lso/
RUN /opt/lso/install-cmake.sh
COPY common/init-repo.sh common/submodules /opt/lso/
COPY src/ /testbed/
WORKDIR /testbed
RUN /opt/lso/init-repo.sh
ARG JOBS=4
RUN cmake -S . -B /build -DSIMDJSON_DEVELOPER_MODE=ON && cmake --build /build --parallel ${JOBS}
# Last, so changing the test script does not rebuild the project.
COPY common/run-tests /opt/lso/
