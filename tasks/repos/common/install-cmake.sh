#!/bin/sh
# Install a pinned CMake from Kitware's release tarballs, for arm64 or x86-64 alike.
# 3.31 is the last series that still configures projects asking for CMake < 3.5.
set -eu
version=3.31.12
case "$(uname -m)" in
  aarch64) arch=aarch64 sha=83f8fd91d2038a56556e1400390fcfe42f79602940c494f6c6f1cdae7f9e7f40 ;;
  x86_64) arch=x86_64 sha=0dc2e9a6860f06bf10bd8fadc03e35d9eeb4df46e33763a7e480e987758f385c ;;
  *) echo "no CMake tarball for $(uname -m)" >&2; exit 1 ;;
esac
file=cmake-$version-linux-$arch.tar.gz
curl -fsSLo /tmp/$file https://github.com/Kitware/CMake/releases/download/v$version/$file
echo "$sha  /tmp/$file" | sha256sum -c -
tar -xzf /tmp/$file -C /opt
ln -s /opt/cmake-$version-linux-$arch/bin/* /usr/local/bin/
rm /tmp/$file
