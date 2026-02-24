# -*- coding: utf-8 -*-
"""
Alchemist contract logic: simulation, encoding, deployment helpers, and tests.
Single-file module for the Alchemist EVM transmutation lab. Use with DaCauldron or headless.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import struct
import sys
import time
import unittest
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

# -----------------------------------------------------------------------------
# Constants (must match Alchemist.sol)
# -----------------------------------------------------------------------------

ALCH_BPS_BASE = 10000
ALCH_MAX_FEE_BPS = 250
ALCH_MAX_RECIPES = 72
ALCH_RECIPE_SALT = 0x5C9f2E8a4D1b7F0e3A6c9B2d5E8f1A4c7D0e3B6
ALCH_MAX_BATCH_INSCRIBE = 12
ALCH_MIN_YIELD_BPS = 5000
ALCH_MAX_YIELD_BPS = 10000

# Deployed immutable addresses (same as constructor in Alchemist.sol)
CRUCIBLE_ADDRESS = "0xE8f2A4C6b1D9e3F7a0B5c8E2d4F6A9b1C3e5D7"
TREASURY_ADDRESS = "0x9C1e5F3a7B0d2E6f8A4c1B7e9D3F5a0C2E6b8"
LAB_KEEPER_ADDRESS = "0x4F7b2D9e1A6c0E3f8B5d2A9c7E1F4b0D6e3A8"

# Hex prefixes and EVM defaults
HEX_PREFIX = "0x"
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
BYTES32_ZERO = "0x" + "00" * 32


# -----------------------------------------------------------------------------
# Errors (mirror contract custom errors)
# -----------------------------------------------------------------------------

class AlchemistError(Exception):
    """Base for Alchemist contract logic errors."""
    pass


class ALCH_ZeroAddress(AlchemistError):
    pass
