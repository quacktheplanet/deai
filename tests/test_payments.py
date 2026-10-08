"""
Off-chain vouchers for PaymentChannels.sol, and chunked pay: what each side
can lose when the other stops.
"""

import pytest
from eth_account import Account

from protocol.payments import (
    Channel, Pricing, RequesterSide, Voucher, WorkerSide, sign_voucher, voucher_digest, voucher_signer,
)

# Hardhat's well-known test account #1 (never holds anything real).
PAYER_KEY = "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d"
PAYER = Account.from_key(PAYER_KEY).address
OTHER_KEY = "0x5de4111afa1a4b94908f83103eb1f1706367c2e68ca870fc3fb9a804cdab365a"
PAYEE = "0x3C44CdDdB6a900fa2b585dd299e03d12FA4293BC"
E18 = 10 ** 18

CH = Channel(chain_id=31337, contract="0x5FbDB2315678afecb367f032d93F642f64180aa3",
             channel_id="0x" + "11" * 32, payer=PAYER, payee=PAYEE, deposit=10 * E18)


def test_matches_the_contract_and_ethers():
    """Vector from ethers' TypedDataEncoder/signTypedData for the same domain; the
    Solidity test checks the contract's voucherDigest() equals ethers' hash."""
    v = sign_voucher(CH, 3 * E18 // 2, PAYER_KEY)
    assert voucher_digest(CH, 3 * E18 // 2).hex() == "7493c3d2441839e787f99ce612216f3ead61dfd0d46db8aaf4bbc1f683eec0b4"
    assert v.signature == ("0x54cfb072cd921cd3a563540048d5cc4ee7c440428ce1d829e45d9dbc41d20c8b"
                           "3eb464c573f4c3f5029bfba398efe4f2ce00c9bc388ead1d048912206b65e4731c")
    assert voucher_signer(CH, v) == PAYER


def test_voucher_beyond_the_deposit_is_refused():
    with pytest.raises(ValueError):
        sign_voucher(CH, 11 * E18, PAYER_KEY)


def test_worker_keeps_only_the_latest_valid_voucher():
    w = WorkerSide(CH, Pricing(per_token=E18 // 100))
    assert w.accept(sign_voucher(CH, 1 * E18, PAYER_KEY))
    assert w.accept(sign_voucher(CH, 2 * E18, PAYER_KEY))
    assert not w.accept(sign_voucher(CH, 1 * E18, PAYER_KEY))            # older
    assert not w.accept(sign_voucher(CH, 3 * E18, OTHER_KEY))            # not the payer
    other_channel = Channel(**{**CH.__dict__, "channel_id": "0x" + "22" * 32})
    assert not w.accept(sign_voucher(other_channel, 3 * E18, PAYER_KEY))  # another channel
    forged = Voucher(CH.channel_id, 9 * E18, w.best.signature)            # signature from 2.0
    assert not w.accept(forged)
    assert w.claim_args() == (CH.channel_id, 2 * E18, w.best.signature)


def _stream(total_tokens, requester_stops_after=None, step=16):
    """Run one answer through both sides; return (worker, requester)."""
    pricing = Pricing(per_token=E18 // 1000, chunk_tokens=64)
    req = RequesterSide(CH, PAYER_KEY, pricing)
    work = WorkerSide(CH, pricing)
    sent = 0
    while sent < total_tokens and work.may_produce(min(step, total_tokens - sent)):
        n = min(step, total_tokens - sent)
        work.produce(n)
        sent += n
        if requester_stops_after is None or req.received < requester_stops_after:
            work.accept(req.receive(n))
    return work, req


def test_honest_requester_pays_for_every_token():
    work, req = _stream(500)
    assert work.produced == 500
    assert work.paid == 500 * (E18 // 1000)
    assert work.unpaid_tokens() == 0


def test_requester_who_stops_paying_costs_the_worker_at_most_one_chunk():
    work, req = _stream(500, requester_stops_after=100)
    assert work.produced < 500                      # the worker stopped
    assert 0 < work.unpaid_tokens() <= 64           # and lost no more than a chunk


def test_worker_who_stops_early_is_paid_only_for_what_arrived():
    pricing = Pricing(per_token=E18 // 1000, chunk_tokens=64)
    req = RequesterSide(CH, PAYER_KEY, pricing)
    last = None
    for _ in range(3):
        last = req.receive(16)
    assert last.cumulative == 48 * (E18 // 1000)


def test_several_tasks_on_one_channel_add_up():
    pricing = Pricing(per_token=E18 // 1000, chunk_tokens=64)
    req, work = RequesterSide(CH, PAYER_KEY, pricing), WorkerSide(CH, pricing)
    for tokens in (100, 250):
        for _ in range(tokens // 10):
            assert work.may_produce(10)
            work.produce(10)
            work.accept(req.receive(10))
        req.finish_task()
        work.finish_task()
    assert work.paid == 350 * (E18 // 1000)
    assert work.unpaid_tokens() == 0
