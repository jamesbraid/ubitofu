# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
FROM python@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de

RUN apt-get update \
    && apt-get install --yes --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

RUN python -m pip install --no-cache-dir \
    iniconfig==2.3.0 \
    lark==1.3.1 \
    packaging==26.2 \
    pluggy==1.6.0 \
    pygments==2.20.0 \
    pytest==9.1.1 \
    python-hcl2==8.1.2 \
    regex==2026.7.19

RUN python -c 'import hashlib, io, os, urllib.request, zipfile; url="https://github.com/opentofu/opentofu/releases/download/v1.12.0/tofu_1.12.0_linux_arm64.zip"; data=urllib.request.urlopen(url).read(); actual=hashlib.sha256(data).hexdigest(); expected="466bf912404b4ab0f0b3a043073d68ad34f11d55ad7a483957d94f0733169f8d"; assert actual == expected, f"OpenTofu checksum mismatch: {actual}"; zipfile.ZipFile(io.BytesIO(data)).extract("tofu", "/usr/local/bin"); os.chmod("/usr/local/bin/tofu", 0o755)'

WORKDIR /proof
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONPATH=/proof

COPY . /proof

CMD ["sleep", "infinity"]
