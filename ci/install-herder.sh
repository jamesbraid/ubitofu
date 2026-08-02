#!/usr/bin/env bash
# Install the pinned unifi-emu-herder into /usr/local/bin, for the controller
# scenarios that start an emulated device fleet.
#
# The version must equal tests/controllertest/pins.py EMU_VERSION; test_pins.py
# enforces that against the workflow that calls this. A release binary compiles
# in the synthetic image built from the same tag, so pinning the version pins
# the image too and the harness passes no --synthetic-image at all.
#
# A missing release stays honest-red — never fall back to building from source,
# which would test an unpinned tree.
set -euo pipefail
HERDER_VERSION="${HERDER_VERSION:?HERDER_VERSION must be set by the caller}"

os=$(uname -s | tr '[:upper:]' '[:lower:]')
case "$(uname -m)" in
  x86_64) arch=amd64 ;;
  aarch64 | arm64) arch=arm64 ;;
  *) echo "unsupported architecture: $(uname -m)" >&2; exit 1 ;;
esac

base="https://github.com/jamesbraid/unifi-emu/releases/download/v${HERDER_VERSION}"
archive="unifi-emu-herder_${HERDER_VERSION}_${os}_${arch}.tar.gz"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

curl -fsSL "$base/$archive" -o "$tmp/$archive"
curl -fsSL "$base/checksums.txt" -o "$tmp/checksums.txt"
# Verify before unpacking. --ignore-missing so the one archive we fetched is
# checked against the full release manifest rather than failing on the rest.
(cd "$tmp" && sha256sum --check --ignore-missing checksums.txt)

tar -xzf "$tmp/$archive" -C "$tmp" unifi-emu-herder
install -m 0755 "$tmp/unifi-emu-herder" /usr/local/bin/unifi-emu-herder
unifi-emu-herder --version
