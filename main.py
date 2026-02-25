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
    return keccak256_bytes(("Alchemist_Vessel_" + s).encode("utf-8"))


def transmute_id_raw(
    chain_id: int,
    block_number: int,
    sequence: int,
    beneficiary: str,
    vessel_id: bytes,
    recipe_id: int,
    reagent_wei: int,
    prevrandao: int,
) -> bytes:
    ben = bytes.fromhex(beneficiary.replace(HEX_PREFIX, "").zfill(40))
    data = b"Alchemist_Transmute"
    data += struct.pack(">Q", chain_id)
    data += struct.pack(">Q", block_number)
    data += struct.pack(">Q", sequence)
    data += ben
    data += vessel_id
    data += struct.pack(">Q", recipe_id)
    data += struct.pack(">Q", reagent_wei)
    data += struct.pack(">Q", prevrandao)
    return keccak256_bytes(data)


# -----------------------------------------------------------------------------
# In-memory lab state (simulator)
# -----------------------------------------------------------------------------

class AlchemistLabState:
    """In-memory simulation of Alchemist contract state and rules."""

    def __init__(self, config: Optional[LabConfig] = None):
        self.config = config or LabConfig()
        self.recipe_counter = 0
        self.transmute_sequence = 0
        self.lab_paused = False
        self._recipes: Dict[int, RecipeRecord] = {}
        self._recipe_ids: List[int] = []
        self._vessels: Dict[bytes, VesselRecord] = {}
        self._vessel_ids: List[bytes] = []
        self._transmutes: Dict[bytes, TransmuteSnapshot] = {}
        self._recipe_transmute_count: Dict[int, int] = {}
        self._recipe_volume_wei: Dict[int, int] = {}
        self._crucible_balance = 0
        self._block_number = self.config.deployed_block or 1
        self._prevrandao = 0x1234567890ABCDEF

    def set_block(self, block_number: int, prevrandao: Optional[int] = None) -> None:
        self._block_number = block_number
        if prevrandao is not None:
            self._prevrandao = prevrandao

    def inscribe_recipe(
        self,
        formula_hash: bytes,
        min_reagent_wei: int,
        yield_bps: int,
        caller: str,
    ) -> int:
        if formula_hash == bytes(32):
            raise ALCH_InvalidFormula()
        if not (ALCH_MIN_YIELD_BPS <= yield_bps <= ALCH_MAX_YIELD_BPS):
            raise ALCH_InvalidYieldBps()
        if self.recipe_counter >= ALCH_MAX_RECIPES:
            raise ALCH_MaxRecipesReached()
        self.recipe_counter += 1
        recipe_id = self.recipe_counter
        self._recipes[recipe_id] = RecipeRecord(
            formula_hash=formula_hash,
            min_reagent_wei=min_reagent_wei,
            yield_bps=yield_bps,
            inscribed_at_block=self._block_number,
            active=True,
            recipe_id=recipe_id,
        )
        self._recipe_ids.append(recipe_id)
        self._recipe_transmute_count[recipe_id] = 0
        self._recipe_volume_wei[recipe_id] = 0
        return recipe_id

    def toggle_recipe(self, recipe_id: int, active: bool, caller: str) -> None:
        if recipe_id not in self._recipes:
            raise ALCH_RecipeNotFound()
        self._recipes[recipe_id].active = active

    def deposit_reagent(
        self,
        vessel_id: bytes,
        amount_wei: int,
        label_hash: bytes,
        depositor: str,
    ) -> None:
        if amount_wei == 0:
            raise ALCH_ZeroAmount()
        if self.lab_paused:
            raise ALCH_LabPaused()
        if vessel_id not in self._vessels:
            self._vessels[vessel_id] = VesselRecord(
                vessel_id=vessel_id,
                balance_wei=0,
                label_hash=label_hash,
                created_at_block=self._block_number,
            )
            self._vessel_ids.append(vessel_id)
        self._vessels[vessel_id].balance_wei += amount_wei

    def resolve_transmutation(
        self,
        beneficiary: str,
        vessel_id: bytes,
        recipe_id: int,
        reagent_wei: int,
        keeper: str,
    ) -> Tuple[bytes, int, int]:
        if keeper != self.config.lab_keeper:
            raise ALCH_NotKeeper()
        if beneficiary == ZERO_ADDRESS or not beneficiary:
            raise ALCH_ZeroAddress()
        if recipe_id not in self._recipes:
            raise ALCH_RecipeNotFound()
        rec = self._recipes[recipe_id]
        if not rec.active:
            raise ALCH_RecipeInactive()
        if reagent_wei < rec.min_reagent_wei:
            raise ALCH_InsufficientReagent()
        if vessel_id not in self._vessels or self._vessels[vessel_id].balance_wei < reagent_wei:
            raise ALCH_InsufficientReagent()

        self._vessels[vessel_id].balance_wei -= reagent_wei
        yield_wei = (reagent_wei * rec.yield_bps) // ALCH_BPS_BASE
        fee_wei = (yield_wei * self.config.fee_bps) // ALCH_BPS_BASE
        transmute_id = transmute_id_raw(
            chain_id=1,
            block_number=self._block_number,
            sequence=self.transmute_sequence,
            beneficiary=address_from_hex(beneficiary),
            vessel_id=vessel_id,
            recipe_id=recipe_id,
            reagent_wei=reagent_wei,
            prevrandao=self._prevrandao,
        )
        self.transmute_sequence += 1
        self._transmutes[transmute_id] = TransmuteSnapshot(
            transmute_id=transmute_id,
            beneficiary=beneficiary,
            recipe_id=recipe_id,
            reagent_wei=reagent_wei,
            yield_wei=yield_wei,
            fee_wei=fee_wei,
            at_block=self._block_number,
        )
        self._recipe_transmute_count[recipe_id] += 1
        self._recipe_volume_wei[recipe_id] += reagent_wei
        return transmute_id, yield_wei, fee_wei

    def set_fee_bps(self, new_fee_bps: int) -> None:
        if new_fee_bps > ALCH_MAX_FEE_BPS:
            raise ALCH_InvalidFeeBps()
        self.config.fee_bps = new_fee_bps

    def set_lab_paused(self, paused: bool) -> None:
        self.lab_paused = paused

    def get_recipe(self, recipe_id: int) -> RecipeRecord:
        if recipe_id not in self._recipes:
            raise ALCH_RecipeNotFound()
        return self._recipes[recipe_id]

    def get_vessel(self, vessel_id: bytes) -> VesselRecord:
        if vessel_id not in self._vessels:
            raise ALCH_VesselNotFound()
        return self._vessels[vessel_id]

    def get_recipe_ids(self) -> List[int]:
        return list(self._recipe_ids)

    def get_vessel_ids(self) -> List[bytes]:
        return list(self._vessel_ids)

    def get_transmute(self, transmute_id: bytes) -> TransmuteSnapshot:
        if transmute_id not in self._transmutes:
            raise KeyError("Transmute not found")
        return self._transmutes[transmute_id]


# -----------------------------------------------------------------------------
# Yield and fee calculations (pure)
# -----------------------------------------------------------------------------

def compute_yield_wei(reagent_wei: int, yield_bps: int) -> int:
    if not (ALCH_MIN_YIELD_BPS <= yield_bps <= ALCH_MAX_YIELD_BPS):
        raise ALCH_InvalidYieldBps()
    return (reagent_wei * yield_bps) // ALCH_BPS_BASE


def compute_fee_wei(yield_wei: int, fee_bps: int) -> int:
    if fee_bps > ALCH_MAX_FEE_BPS:
        raise ALCH_InvalidFeeBps()
    return (yield_wei * fee_bps) // ALCH_BPS_BASE


def compute_net_wei(yield_wei: int, fee_wei: int) -> int:
    return yield_wei - fee_wei


# -----------------------------------------------------------------------------
# ABI encoding helpers (minimal, for calldata construction)
# -----------------------------------------------------------------------------

def abi_encode_uint256(value: int) -> str:
    h = hex(value)[2:].replace("L", "").zfill(64)
    return HEX_PREFIX + h


def abi_encode_address(addr: str) -> str:
    raw = addr.replace(HEX_PREFIX, "").lower().zfill(40)[-40:]
    return HEX_PREFIX + raw.zfill(64)

def abi_encode_bytes32(b: bytes) -> str:
    if len(b) > 32:
        b = b[-32:]
    return HEX_PREFIX + b.hex().rjust(64, "0")


def abi_encode_bytes32_string(s: str) -> str:
    h = formula_hash_from_string(s)
    return abi_encode_bytes32(h)


# -----------------------------------------------------------------------------
# Contract ABI (minimal selectors and signatures)
# -----------------------------------------------------------------------------

ALCHEMIST_ABI_SELECTORS = {
    "inscribeRecipe(bytes32,uint256,uint256)": "0xa1b2c3d4",
    "toggleRecipe(uint256,bool)": "0xb2c3d4e5",
    "depositReagent(bytes32,bytes32)": "0xc3d4e5f6",
    "resolveTransmutation(address,bytes32,uint256,uint256)": "0xd4e5f6a7",
    "withdrawCrucible(uint256)": "0xe5f6a7b8",
    "setLabPaused(bool)": "0xf6a7b8c9",
    "setFeeBps(uint256)": "0xa7b8c9d0",
    "getRecipe(uint256)": "0xb8c9d0e1",
    "getVessel(bytes32)": "0xc9d0e1f2",
    "getRecipeIds()": "0xd0e1f2a3",
    "getVesselIds()": "0xe1f2a3b4",
}


def get_selector(signature: str) -> str:
    sig_bytes = signature.encode("utf-8")
    return keccak256_hex(sig_bytes)[:10]


# -----------------------------------------------------------------------------
# Event topic hashes (for log parsing)
# -----------------------------------------------------------------------------

def event_topic(event_signature: str) -> str:
    return keccak256_hex(event_signature.encode("utf-8"))


ALCHEMIST_EVENT_TOPICS = {
    "RecipeInscribed(uint256,bytes32,uint256,uint256,uint256)": event_topic("RecipeInscribed(uint256,bytes32,uint256,uint256,uint256)"),
    "RecipeToggled(uint256,bool,uint256)": event_topic("RecipeToggled(uint256,bool,uint256)"),
    "ReagentDeposited(address,bytes32,uint256,uint256)": event_topic("ReagentDeposited(address,bytes32,uint256,uint256)"),
    "TransmutationResolved(bytes32,address,uint256,uint256,uint256,uint256,uint256)": event_topic(
        "TransmutationResolved(bytes32,address,uint256,uint256,uint256,uint256,uint256)"
    ),
    "CrucibleWithdrawn(address,uint256,uint256)": event_topic("CrucibleWithdrawn(address,uint256,uint256)"),
    "LabPauseToggled(bool)": event_topic("LabPauseToggled(bool)"),
    "FeeBpsUpdated(uint256,uint256,uint256)": event_topic("FeeBpsUpdated(uint256,uint256,uint256)"),
}


# -----------------------------------------------------------------------------
# Batch inscribe simulation
# -----------------------------------------------------------------------------

def batch_inscribe_recipes(
    state: AlchemistLabState,
    formula_hashes: List[bytes],
    min_reagent_weis: List[int],
    yield_bps_list: List[int],
    caller: str,
) -> List[int]:
    n = len(formula_hashes)
    if n != len(min_reagent_weis) or n != len(yield_bps_list):
        raise ALCH_ArrayLengthMismatch()
    if n == 0:
        raise ALCH_ZeroRecipes()
    if n > ALCH_MAX_BATCH_INSCRIBE:
        raise ALCH_BatchTooLarge()
    if state.recipe_counter + n > ALCH_MAX_RECIPES:
        raise ALCH_MaxRecipesReached()
    recipe_ids = []
    for i in range(n):
        if formula_hashes[i] == bytes(32):
            raise ALCH_InvalidFormula()
        if not (ALCH_MIN_YIELD_BPS <= yield_bps_list[i] <= ALCH_MAX_YIELD_BPS):
            raise ALCH_InvalidYieldBps()
        rid = state.inscribe_recipe(
            formula_hash=formula_hashes[i],
            min_reagent_wei=min_reagent_weis[i],
            yield_bps=yield_bps_list[i],
            caller=caller,
        )
        recipe_ids.append(rid)
    return recipe_ids


# -----------------------------------------------------------------------------
# Deployment config (for script usage)
# -----------------------------------------------------------------------------

DEPLOYMENT_NETWORKS = {
    "mainnet": {"chain_id": 1, "rpc_env": "ETH_RPC_URL"},
    "sepolia": {"chain_id": 11155111, "rpc_env": "SEPOLIA_RPC_URL"},
    "base": {"chain_id": 8453, "rpc_env": "BASE_RPC_URL"},
    "arbitrum": {"chain_id": 42161, "rpc_env": "ARBITRUM_RPC_URL"},
    "optimism": {"chain_id": 10, "rpc_env": "OPTIMISM_RPC_URL"},
}


def get_rpc_url(network: str) -> str:
    info = DEPLOYMENT_NETWORKS.get(network, DEPLOYMENT_NETWORKS["mainnet"])
    return os.environ.get(info["rpc_env"], "https://eth.llamarpc.com")


# -----------------------------------------------------------------------------
# Unit tests
# -----------------------------------------------------------------------------

class TestAlchemistLabState(unittest.TestCase):
    def setUp(self) -> None:
        self.config = LabConfig(
            crucible=CRUCIBLE_ADDRESS,
            treasury=TREASURY_ADDRESS,
            lab_keeper=LAB_KEEPER_ADDRESS,
            fee_bps=8,
            deployed_block=1000,
        )
        self.state = AlchemistLabState(self.config)
        self.state.set_block(1001, 0xABCD)

    def test_inscribe_recipe(self) -> None:
        fh = formula_hash_from_string("lead_to_gold")
        rid = self.state.inscribe_recipe(fh, 1_000_000, 8000, TREASURY_ADDRESS)
        self.assertEqual(rid, 1)
        rec = self.state.get_recipe(1)
        self.assertTrue(rec.active)
        self.assertEqual(rec.yield_bps, 8000)
        self.assertEqual(rec.min_reagent_wei, 1_000_000)

    def test_inscribe_recipe_invalid_yield(self) -> None:
        fh = formula_hash_from_string("bad")
        with self.assertRaises(ALCH_InvalidYieldBps):
            self.state.inscribe_recipe(fh, 0, 4000, TREASURY_ADDRESS)
        with self.assertRaises(ALCH_InvalidYieldBps):
            self.state.inscribe_recipe(fh, 0, 10001, TREASURY_ADDRESS)

    def test_deposit_reagent(self) -> None:
        vid = vessel_id_from_string("vessel_alpha")
        label = formula_hash_from_string("alpha")
        self.state.deposit_reagent(vid, 5_000_000, label, "0x1111111111111111111111111111111111111111")
        v = self.state.get_vessel(vid)
        self.assertEqual(v.balance_wei, 5_000_000)

    def test_deposit_zero_raises(self) -> None:
        vid = vessel_id_from_string("vessel_beta")
        label = bytes(32)
        with self.assertRaises(ALCH_ZeroAmount):
            self.state.deposit_reagent(vid, 0, label, TREASURY_ADDRESS)

    def test_resolve_transmutation(self) -> None:
        fh = formula_hash_from_string("silver")
        rid = self.state.inscribe_recipe(fh, 1_000_000, 8000, TREASURY_ADDRESS)
        vid = vessel_id_from_string("v1")
        self.state.deposit_reagent(vid, 10_000_000, bytes(32), TREASURY_ADDRESS)
        tid, yw, fw = self.state.resolve_transmutation(
            "0x2222222222222222222222222222222222222222",
            vid,
            rid,
            2_000_000,
            LAB_KEEPER_ADDRESS,
        )
        self.assertEqual(yw, 2_000_000 * 8000 // ALCH_BPS_BASE)
        self.assertEqual(fw, yw * 8 // ALCH_BPS_BASE)
        v = self.state.get_vessel(vid)
        self.assertEqual(v.balance_wei, 8_000_000)

    def test_resolve_transmutation_not_keeper(self) -> None:
        fh = formula_hash_from_string("x")
        rid = self.state.inscribe_recipe(fh, 100, 9000, TREASURY_ADDRESS)
        vid = vessel_id_from_string("v2")
        self.state.deposit_reagent(vid, 1000, bytes(32), TREASURY_ADDRESS)
        with self.assertRaises(ALCH_NotKeeper):
            self.state.resolve_transmutation(TREASURY_ADDRESS, vid, rid, 500, TREASURY_ADDRESS)

    def test_resolve_insufficient_reagent(self) -> None:
        fh = formula_hash_from_string("y")
        rid = self.state.inscribe_recipe(fh, 5_000_000, 7000, TREASURY_ADDRESS)
        vid = vessel_id_from_string("v3")
        self.state.deposit_reagent(vid, 1_000_000, bytes(32), TREASURY_ADDRESS)
        with self.assertRaises(ALCH_InsufficientReagent):
            self.state.resolve_transmutation(TREASURY_ADDRESS, vid, rid, 3_000_000, LAB_KEEPER_ADDRESS)

    def test_compute_yield_fee(self) -> None:
        y = compute_yield_wei(1_000_000, 8000)
        self.assertEqual(y, 800_000)
        f = compute_fee_wei(y, 8)
        self.assertEqual(f, 640)
        self.assertEqual(compute_net_wei(y, f), 799_360)

    def test_batch_inscribe(self) -> None:
        fhs = [formula_hash_from_string(f"r{i}") for i in range(3)]
        mins = [100, 200, 300]
        bps = [5000, 7500, 10000]
        rids = batch_inscribe_recipes(self.state, fhs, mins, bps, TREASURY_ADDRESS)
        self.assertEqual(rids, [1, 2, 3])
        self.assertEqual(self.state.recipe_counter, 3)

    def test_batch_inscribe_length_mismatch(self) -> None:
        with self.assertRaises(ALCH_ArrayLengthMismatch):
            batch_inscribe_recipes(
                self.state,
                [formula_hash_from_string("a")],
                [1, 2],
                [5000],
                TREASURY_ADDRESS,
            )


class TestEncoding(unittest.TestCase):
    def test_formula_hash_deterministic(self) -> None:
        h1 = formula_hash_from_string("lead_to_gold")
        h2 = formula_hash_from_string("lead_to_gold")
        self.assertEqual(h1, h2)

    def test_vessel_id_unique(self) -> None:
        v1 = vessel_id_from_string("alpha")
        v2 = vessel_id_from_string("beta")
        self.assertNotEqual(v1, v2)

    def test_transmute_id_unique(self) -> None:
        vid = vessel_id_from_string("v")
        t1 = transmute_id_raw(1, 100, 0, TREASURY_ADDRESS, vid, 1, 1000, 0xAAA)
        t2 = transmute_id_raw(1, 100, 1, TREASURY_ADDRESS, vid, 1, 1000, 0xAAA)
        self.assertNotEqual(t1, t2)


# -----------------------------------------------------------------------------
# Event log parsing (EVM log data to Python structs)
# -----------------------------------------------------------------------------

def parse_uint256_from_hex(hex_str: str) -> int:
    raw = hex_str.replace(HEX_PREFIX, "").strip()
    if not raw:
        return 0
    return int(raw, 16)


def parse_address_from_hex(hex_str: str) -> str:
    raw = hex_str.replace(HEX_PREFIX, "").strip()
    return HEX_PREFIX + raw.zfill(40)[-40:].lower()


def parse_bytes32_from_hex(hex_str: str) -> bytes:
    raw = hex_str.replace(HEX_PREFIX, "").strip()
    return bytes.fromhex(raw.zfill(64)[-64:])


def parse_recipe_inscribed_log(topics: List[str], data: str) -> Dict[str, Any]:
    if len(topics) < 2:
        return {}
    recipe_id = parse_uint256_from_hex(topics[1])
    if len(data) >= 128:
        formula_hash = parse_bytes32_from_hex(data[2:66])
        min_reagent_wei = parse_uint256_from_hex(data[66:130])
        yield_bps = parse_uint256_from_hex(data[130:194])
        at_block = parse_uint256_from_hex(data[194:258]) if len(data) >= 258 else 0
    else:
        formula_hash = b""
        min_reagent_wei = yield_bps = at_block = 0
    return {
        "recipeId": recipe_id,
        "formulaHash": formula_hash,
        "minReagentWei": min_reagent_wei,
        "yieldBps": yield_bps,
        "atBlock": at_block,
    }


def parse_reagent_deposited_log(topics: List[str], data: str) -> Dict[str, Any]:
    if len(topics) < 3:
        return {}
    depositor = parse_address_from_hex(topics[1])
    vessel_id = parse_bytes32_from_hex(topics[2])
    amount_wei = parse_uint256_from_hex(data[2:66]) if len(data) >= 66 else 0
    at_block = parse_uint256_from_hex(data[66:130]) if len(data) >= 130 else 0
    return {
        "depositor": depositor,
        "vesselId": vessel_id,
        "amountWei": amount_wei,
        "atBlock": at_block,
    }


def parse_transmutation_resolved_log(topics: List[str], data: str) -> Dict[str, Any]:
    if len(topics) < 2:
        return {}
    transmute_id = parse_bytes32_from_hex(topics[1])
    if len(data) >= 258:
        beneficiary = parse_address_from_hex(data[2:66])
        recipe_id = parse_uint256_from_hex(data[66:130])
        reagent_wei = parse_uint256_from_hex(data[130:194])
        yield_wei = parse_uint256_from_hex(data[194:258])
        fee_wei = parse_uint256_from_hex(data[258:322]) if len(data) >= 322 else 0
        at_block = parse_uint256_from_hex(data[322:386]) if len(data) >= 386 else 0
    else:
        beneficiary = ZERO_ADDRESS
        recipe_id = reagent_wei = yield_wei = fee_wei = at_block = 0
    return {
        "transmuteId": transmute_id,
        "beneficiary": beneficiary,
        "recipeId": recipe_id,
        "reagentWei": reagent_wei,
        "yieldWei": yield_wei,
        "feeWei": fee_wei,
        "atBlock": at_block,
    }


def parse_lab_pause_toggled(data: str) -> Dict[str, Any]:
    paused = False
    if len(data) >= 66:
        raw = data[2:66].strip()
        if raw:
            paused = parse_uint256_from_hex(raw) != 0
    return {"paused": paused}


def parse_fee_bps_updated(data: str) -> Dict[str, Any]:
    prev = new = at_block = 0
    if len(data) >= 194:
        prev = parse_uint256_from_hex(data[2:66])
        new = parse_uint256_from_hex(data[66:130])
        at_block = parse_uint256_from_hex(data[130:194])
    return {"previousBps": prev, "newBps": new, "atBlock": at_block}


# -----------------------------------------------------------------------------
# Full ABI JSON (stub for deployment tools)
# -----------------------------------------------------------------------------

ALCHEMIST_ABI_JSON = [
    {
        "type": "constructor",
        "inputs": [],
        "stateMutability": "nonpayable",
    },
    {
        "type": "function",
        "name": "inscribeRecipe",
        "inputs": [
            {"name": "formulaHash", "type": "bytes32", "internalType": "bytes32"},
            {"name": "minReagentWei", "type": "uint256", "internalType": "uint256"},
            {"name": "yieldBps", "type": "uint256", "internalType": "uint256"},
        ],
        "outputs": [{"name": "recipeId", "type": "uint256", "internalType": "uint256"}],
        "stateMutability": "nonpayable",
    },
    {
        "type": "function",
        "name": "toggleRecipe",
        "inputs": [
            {"name": "recipeId", "type": "uint256", "internalType": "uint256"},
            {"name": "active", "type": "bool", "internalType": "bool"},
        ],
        "outputs": [],
        "stateMutability": "nonpayable",
    },
    {
        "type": "function",
        "name": "depositReagent",
        "inputs": [
            {"name": "vesselId", "type": "bytes32", "internalType": "bytes32"},
            {"name": "labelHash", "type": "bytes32", "internalType": "bytes32"},
        ],
        "outputs": [],
        "stateMutability": "payable",
    },
    {
        "type": "function",
        "name": "resolveTransmutation",
        "inputs": [
            {"name": "beneficiary", "type": "address", "internalType": "address"},
            {"name": "vesselId", "type": "bytes32", "internalType": "bytes32"},
            {"name": "recipeId", "type": "uint256", "internalType": "uint256"},
            {"name": "reagentWei", "type": "uint256", "internalType": "uint256"},
        ],
        "outputs": [
            {"name": "transmuteId", "type": "bytes32", "internalType": "bytes32"},
            {"name": "yieldWei", "type": "uint256", "internalType": "uint256"},
            {"name": "feeWei", "type": "uint256", "internalType": "uint256"},
        ],
        "stateMutability": "nonpayable",
    },
    {
        "type": "function",
        "name": "withdrawCrucible",
        "inputs": [{"name": "amountWei", "type": "uint256", "internalType": "uint256"}],
        "outputs": [],
        "stateMutability": "nonpayable",
    },
    {
        "type": "function",
        "name": "setLabPaused",
        "inputs": [{"name": "paused", "type": "bool", "internalType": "bool"}],
        "outputs": [],
        "stateMutability": "nonpayable",
    },
    {
        "type": "function",
        "name": "setFeeBps",
        "inputs": [{"name": "newFeeBps", "type": "uint256", "internalType": "uint256"}],
        "outputs": [],
        "stateMutability": "nonpayable",
    },
    {
        "type": "function",
        "name": "getRecipe",
        "inputs": [{"name": "recipeId", "type": "uint256", "internalType": "uint256"}],
        "outputs": [
            {"name": "formulaHash", "type": "bytes32", "internalType": "bytes32"},
            {"name": "minReagentWei", "type": "uint256", "internalType": "uint256"},
            {"name": "yieldBps", "type": "uint256", "internalType": "uint256"},
            {"name": "inscribedAtBlock", "type": "uint256", "internalType": "uint256"},
            {"name": "active", "type": "bool", "internalType": "bool"},
        ],
        "stateMutability": "view",
    },
    {
        "type": "function",
        "name": "getVessel",
        "inputs": [{"name": "vesselId", "type": "bytes32", "internalType": "bytes32"}],
        "outputs": [
