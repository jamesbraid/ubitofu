#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
# Install the pinned OpenTofu binary into /usr/local/bin. Shared by every CI
# step whose Python test suite shells out to `tofu` (hcl_writer.tofu_fmt): the
# test step and both mutation gates, which run the suite under mutmut.
set -euo pipefail
TOFU_VERSION="1.12.0"

tofu_arch() {
    case "$1" in
        x86_64|amd64) printf '%s' "amd64" ;;
        aarch64|arm64) printf '%s' "arm64" ;;
        *)
            printf 'unsupported Linux architecture: %s\n' "$1" >&2
            return 1
            ;;
    esac
}

tofu_archive_url() {
    local archive_arch
    archive_arch="$(tofu_arch "$1")"
    printf '%s' "https://github.com/opentofu/opentofu/releases/download/v${TOFU_VERSION}/tofu_${TOFU_VERSION}_linux_${archive_arch}.zip"
}

install_tofu() {
    local archive_url
    archive_url="$(tofu_archive_url "$(uname -m)")"
    apt-get update
    apt-get install -y --no-install-recommends unzip curl
    curl -fsSL "$archive_url" -o /tmp/tofu.zip
    unzip -o -d /usr/local/bin /tmp/tofu.zip tofu
    tofu version
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    install_tofu
fi
