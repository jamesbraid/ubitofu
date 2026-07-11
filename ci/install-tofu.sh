#!/usr/bin/env bash
# Install the pinned OpenTofu binary into /usr/local/bin. Shared by every CI
# step whose Python test suite shells out to `tofu` (hcl_writer.tofu_fmt): the
# test step and both mutation gates, which run the suite under mutmut.
set -euo pipefail
TOFU_VERSION="1.12.0"
apt-get update && apt-get install -y --no-install-recommends unzip curl
curl -fsSL "https://github.com/opentofu/opentofu/releases/download/v${TOFU_VERSION}/tofu_${TOFU_VERSION}_linux_amd64.zip" -o /tmp/tofu.zip
unzip -o -d /usr/local/bin /tmp/tofu.zip tofu
tofu version
