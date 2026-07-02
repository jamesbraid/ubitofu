# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
from pathlib import Path

import pytest


@pytest.fixture
def fixtures_dir() -> Path:
    return Path(__file__).parent / "fixtures"
