"""Task containers through the Docker API (Colima or Docker Desktop), with CPU and memory caps."""

from __future__ import annotations

import io
import math
import shlex
import tarfile
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath

import docker
import docker.errors

LABEL = "llm-second-opinion"
# The largest file any process in a task container may write (RLIMIT_FSIZE): a runaway command
# output log once filled the host disk (109 GB). The writer gets SIGXFSZ past it.
MAX_FILE_BYTES = 4 * 2**30
# `timeout` exits 124 in coreutils and 143 (SIGTERM) in BusyBox; 137 if it had to SIGKILL.
_TIMEOUT_EXITS = {124, 137, 143}


@dataclass
class ExecResult:
    exit_code: int
    output: str
    timed_out: bool = False


class Container:
    """A running container that idles until commands are exec'd into it."""

    def __init__(self, handle: docker.models.containers.Container):
        self._handle = handle

    def exec(
        self,
        command: str,
        workdir: str | None = None,
        timeout_s: float | None = None,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        """Run a shell command. Env is passed per exec, so secrets never enter the image config."""
        argv = ["sh", "-c", command]
        if timeout_s is not None:
            argv = ["timeout", str(max(1, math.ceil(timeout_s))), *argv]
        start = time.monotonic()
        code, output = self._handle.exec_run(argv, workdir=workdir, environment=env, demux=False)
        # The exit codes alone are ambiguous; a timeout also has to have used the full time.
        timed_out = (
            timeout_s is not None
            and code in _TIMEOUT_EXITS
            and time.monotonic() - start >= timeout_s
        )
        return ExecResult(code, (output or b"").decode(errors="replace"), timed_out)

    def write(self, path: str, data: str | bytes) -> None:
        data = data.encode() if isinstance(data, str) else data
        target = PurePosixPath(path)
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            info = tarfile.TarInfo(target.name)
            info.size = len(data)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))
        self.exec(f"mkdir -p {shlex.quote(str(target.parent))}")
        self._handle.put_archive(str(target.parent), buf.getvalue())

    def read(self, path: str) -> str | None:
        """File contents, or None if it does not exist."""
        try:
            stream, _ = self._handle.get_archive(path)
        except docker.errors.NotFound:
            return None
        with tarfile.open(fileobj=io.BytesIO(b"".join(stream))) as tar:
            member = tar.extractfile(tar.getmembers()[0])
            return member.read().decode(errors="replace") if member else None

    def remove(self) -> None:
        try:
            self._handle.remove(force=True)
        except docker.errors.NotFound:
            pass


class Runtime:
    def __init__(self, client: docker.DockerClient | None = None):
        self.client = client or docker.from_env()

    def start(
        self,
        image: str,
        cpus: float,
        memory_gb: float,
        name: str,
        volumes: Mapping[str, str] | None = None,
    ) -> Container:
        """A capped container idling on `sleep`; `volumes` maps volume names to read-only
        mount points."""
        self._ensure_image(image)
        mounts = {v: {"bind": path, "mode": "ro"} for v, path in (volumes or {}).items()}
        handle = self.client.containers.run(
            image,
            command=["sleep", "infinity"],
            entrypoint=[],
            detach=True,
            init=True,
            nano_cpus=int(cpus * 1e9),
            mem_limit=int(memory_gb * 2**30),
            ulimits=[docker.types.Ulimit(name="fsize", soft=MAX_FILE_BYTES, hard=MAX_FILE_BYTES)],
            labels={LABEL: name},
            extra_hosts={"host.docker.internal": "host-gateway"},
            volumes=mounts,
        )
        return Container(handle)

    def _ensure_image(self, image: str) -> None:
        try:
            self.client.images.get(image)
        except docker.errors.ImageNotFound:
            if image.startswith("sha256:"):
                raise RuntimeError(
                    f"image {image} not found: task images are local; a rebuilt image has a new "
                    "ID, so rebuild and validate again (`bench tasks build`, `bench tasks validate`)"
                ) from None
            self.client.images.pull(image)
