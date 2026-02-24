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


class ALCH_ZeroAmount(AlchemistError):
    pass


class ALCH_LabPaused(AlchemistError):
    pass


class ALCH_RecipeNotFound(AlchemistError):
    pass


class ALCH_RecipeInactive(AlchemistError):
    pass


class ALCH_InvalidFeeBps(AlchemistError):
    pass


class ALCH_TransferFailed(AlchemistError):
    pass


class ALCH_NotKeeper(AlchemistError):
    pass


class ALCH_MaxRecipesReached(AlchemistError):
    pass


class ALCH_RecipeAlreadyExists(AlchemistError):
    pass


class ALCH_InsufficientReagent(AlchemistError):
    pass


class ALCH_ArrayLengthMismatch(AlchemistError):
    pass


class ALCH_BatchTooLarge(AlchemistError):
    pass


class ALCH_ZeroRecipes(AlchemistError):
    pass


class ALCH_VesselNotFound(AlchemistError):
    pass


class ALCH_InvalidYieldBps(AlchemistError):
    pass


class ALCH_InvalidFormula(AlchemistError):
    pass


# -----------------------------------------------------------------------------
# Enums and data structures
# -----------------------------------------------------------------------------

class TransmuteStatus(IntEnum):
    PENDING = 0
    RESOLVED = 1
    FAILED = 2


@dataclass
class RecipeRecord:
    formula_hash: bytes
    min_reagent_wei: int
    yield_bps: int
    inscribed_at_block: int
    active: bool
    recipe_id: int = 0


@dataclass
class VesselRecord:
    vessel_id: bytes
    balance_wei: int
    label_hash: bytes
    created_at_block: int


@dataclass
class TransmuteSnapshot:
    transmute_id: bytes
    beneficiary: str
    recipe_id: int
    reagent_wei: int
    yield_wei: int
    fee_wei: int
    at_block: int


@dataclass
class LabConfig:
    crucible: str = CRUCIBLE_ADDRESS
    treasury: str = TREASURY_ADDRESS
    lab_keeper: str = LAB_KEEPER_ADDRESS
    fee_bps: int = 8
    deployed_block: int = 0


# -----------------------------------------------------------------------------
# Hashing and encoding (EVM-compatible)
# -----------------------------------------------------------------------------

def keccak256_hex(data: bytes) -> str:
    """Return keccak256 hash as 0x-prefixed hex. Use py-evm or eth_hash if available."""
    try:
        from eth_hash.auto import keccak
        h = keccak(data)
    except ImportError:
        h = hashlib.sha3_256(data).digest() if hashlib.sha3_256 else hashlib.sha256(data).digest()
    return HEX_PREFIX + h.hex()


def keccak256_bytes(data: bytes) -> bytes:
    try:
        from eth_hash.auto import keccak
        return keccak(data)
    except ImportError:
        h = hashlib.sha3_256(data).digest() if hashlib.sha3_256 else hashlib.sha256(data).digest()
        return h


def bytes32_from_hex(s: str) -> bytes:
    raw = s.replace(HEX_PREFIX, "").lower()
    if len(raw) != 64:
        raw = raw.zfill(64)[-64:]
    return bytes.fromhex(raw)


def bytes32_to_hex(b: bytes) -> str:
    if len(b) > 32:
        b = b[-32:]
    return HEX_PREFIX + b.hex().rjust(64, "0")


def address_from_hex(s: str) -> str:
    raw = s.replace(HEX_PREFIX, "").lower()
    if len(raw) != 40:
        raw = raw.zfill(40)[-40:]
    return HEX_PREFIX + raw


def formula_hash_from_string(s: str) -> bytes:
    return keccak256_bytes(s.encode("utf-8"))


def vessel_id_from_string(s: str) -> bytes:
