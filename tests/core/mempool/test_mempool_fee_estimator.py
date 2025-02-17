from __future__ import annotations

from random import Random
from typing import Optional

import pytest

from chia.consensus.cost_calculator import NPCResult
from chia.full_node.bitcoin_fee_estimator import BitcoinFeeEstimator
from chia.full_node.coin_store import CoinStore
from chia.full_node.fee_estimate_store import FeeStore
from chia.full_node.fee_estimator import SmartFeeEstimator
from chia.full_node.fee_tracker import FeeTracker
from chia.full_node.mempool_manager import MempoolManager
from chia.simulator.wallet_tools import WalletTool
from chia.types.blockchain_format.coin import Coin
from chia.types.blockchain_format.sized_bytes import bytes32
from chia.types.coin_record import CoinRecord
from chia.types.condition_opcodes import ConditionOpcode
from chia.types.mempool_inclusion_status import MempoolInclusionStatus
from chia.types.mempool_item import MempoolItem
from chia.util.ints import uint32, uint64
from tests.core.consensus.test_pot_iterations import test_constants
from tests.core.mempool.test_mempool_manager import (
    IDENTITY_PUZZLE_HASH,
    generate_and_add_spendbundle,
    instantiate_mempool_manager,
)
from tests.util.db_connection import DBConnection


@pytest.mark.asyncio
async def test_basics() -> None:
    fee_store = FeeStore()
    fee_tracker = FeeTracker(fee_store)

    wallet_tool = WalletTool(test_constants)
    ph = wallet_tool.get_new_puzzlehash()
    coin = Coin(ph, ph, uint64(10000))
    spend_bundle = wallet_tool.generate_signed_transaction(uint64(10000), ph, coin)
    cost = uint64(5000000)
    for i in range(300, 700):
        i = uint32(i)
        items = []
        for _ in range(2, 100):
            fee = uint64(10000000)
            mempool_item = MempoolItem(
                spend_bundle,
                fee,
                NPCResult(None, None, cost),
                cost,
                spend_bundle.name(),
                [],
                uint32(i - 1),
            )
            items.append(mempool_item)

            fee1 = uint64(200000)
            mempool_item1 = MempoolItem(
                spend_bundle,
                fee1,
                NPCResult(None, None, cost),
                cost,
                spend_bundle.name(),
                [],
                uint32(i - 40),
            )
            items.append(mempool_item1)

            fee2 = uint64(0)
            mempool_item2 = MempoolItem(
                spend_bundle,
                fee2,
                NPCResult(None, None, cost),
                cost,
                spend_bundle.name(),
                [],
                uint32(i - 270),
            )
            items.append(mempool_item2)

        fee_tracker.process_block(i, items)

    short, med, long = fee_tracker.estimate_fees()

    assert short.median != -1
    assert med.median != -1
    assert long.median != -1


@pytest.mark.asyncio
async def test_fee_increase() -> None:

    async with DBConnection(db_version=2) as db_wrapper:
        coin_store = await CoinStore.create(db_wrapper)
        mempool_manager = MempoolManager(coin_store.get_coin_record, test_constants)
        assert test_constants.MAX_BLOCK_COST_CLVM == mempool_manager.constants.MAX_BLOCK_COST_CLVM
        btc_fee_estimator: BitcoinFeeEstimator = mempool_manager.mempool.fee_estimator  # type: ignore
        fee_tracker = btc_fee_estimator.get_tracker()
        estimator = SmartFeeEstimator(fee_tracker, uint64(test_constants.MAX_BLOCK_COST_CLVM))
        wallet_tool = WalletTool(test_constants)
        ph = wallet_tool.get_new_puzzlehash()
        coin = Coin(ph, ph, uint64(10000))
        spend_bundle = wallet_tool.generate_signed_transaction(uint64(10000), ph, coin)
        random = Random(x=1)
        for i in range(300, 700):
            i = uint32(i)
            items = []
            for _ in range(0, 20):
                fee = uint64(0)
                included_height = uint32(random.randint(i - 60, i - 1))
                cost = uint64(5000000)
                mempool_item = MempoolItem(
                    spend_bundle,
                    fee,
                    NPCResult(None, None, cost),
                    cost,
                    spend_bundle.name(),
                    [],
                    included_height,
                )
                items.append(mempool_item)

            fee_tracker.process_block(i, items)

        short, med, long = fee_tracker.estimate_fees()
        mempool_info = mempool_manager.mempool.fee_estimator.get_mempool_info()

        result = estimator.get_estimates(mempool_info, ignore_mempool=True)

        assert short.median == -1
        assert med.median == -1
        assert long.median == 0.0

        assert result.error is None
        short_estimate = result.estimates[0].estimated_fee_rate
        med_estimate = result.estimates[1].estimated_fee_rate
        long_estimate = result.estimates[2].estimated_fee_rate

        assert short_estimate.mojos_per_clvm_cost == uint64(fee_tracker.buckets[3] / 1000)
        assert med_estimate.mojos_per_clvm_cost == uint64(fee_tracker.buckets[3] / 1000)
        assert long_estimate.mojos_per_clvm_cost == uint64(0)


@pytest.mark.asyncio
async def test_total_mempool_fees() -> None:
    coin1 = Coin(IDENTITY_PUZZLE_HASH, IDENTITY_PUZZLE_HASH, uint64(0xFFFFFFFFFFFFFFFF))
    coin2 = Coin(IDENTITY_PUZZLE_HASH, IDENTITY_PUZZLE_HASH, uint64(3))

    async def get_coin_record(coin_id: bytes32) -> Optional[CoinRecord]:
        test_coin_records = {
            coin1.name(): CoinRecord(coin1, uint32(0), uint32(0), False, uint64(0)),
            coin2.name(): CoinRecord(coin2, uint32(0), uint32(0), False, uint64(0)),
        }
        return test_coin_records.get(coin_id)

    mempool_manager = await instantiate_mempool_manager(get_coin_record)
    conditions = [[ConditionOpcode.CREATE_COIN, IDENTITY_PUZZLE_HASH, 1]]
    _, _, result = await generate_and_add_spendbundle(mempool_manager, conditions, coin1)
    assert result[1] == MempoolInclusionStatus.SUCCESS
    _, _, result = await generate_and_add_spendbundle(mempool_manager, conditions, coin2)
    assert result[1] == MempoolInclusionStatus.SUCCESS
    # Total fees should be coin1's amount plus coin2's amount minus two mojos
    # for the created coins
    assert mempool_manager.mempool.total_mempool_fees == 0xFFFFFFFFFFFFFFFF + 3 - 2
