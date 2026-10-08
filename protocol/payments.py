"""
DAI Payments — the off-chain side of PaymentChannels.sol
---------------------------------------------------------
Requester-funded payment with no trusted publisher (docs/DECENTRALIZATION.md,
"Payment: the hard problem"; the D3 step of its staircase).

The requester (payer) opens a channel to a worker (payee) on-chain with a
deposit. While the worker's answer streams in, the requester signs vouchers:
EIP-712 messages saying "the payee may claim up to X in total". Each one
supersedes the last. The worker keeps only the latest and claims it on-chain
when it likes (claimMany batches several channels).

Chunked pay bounds what either side can lose. The worker produces at most one
chunk of output beyond what it has been paid for; the requester signs for what
it actually received. If the requester stops signing, the worker stops after
at most one unpaid chunk. If the worker stops, the requester has paid only for
what arrived.

Nothing is minted: paying yourself through a channel moves your own money, so
the self-dealing exploit that minted rewards would invite doesn't exist here.

Not wired into the live request path yet: today the orchestrator sits between
requester and worker. This module is what both ends use once they talk directly
(D2, peer-to-peer transport).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_utils import keccak, to_checksum_address

DOMAIN_NAME = "DAI PaymentChannels"
DOMAIN_VERSION = "1"
VOUCHER_TYPES = {
    "EIP712Domain": [
        {"name": "name", "type": "string"},
        {"name": "version", "type": "string"},
        {"name": "chainId", "type": "uint256"},
        {"name": "verifyingContract", "type": "address"},
    ],
    "Voucher": [
        {"name": "channelId", "type": "bytes32"},
        {"name": "cumulative", "type": "uint256"},
    ],
}


@dataclass(frozen=True)
class Channel:
    chain_id: int
    contract: str      # PaymentChannels address
    channel_id: str    # 0x-prefixed bytes32
    payer: str
    payee: str
    deposit: int       # in token base units (wei)


@dataclass(frozen=True)
class Voucher:
    channel_id: str
    cumulative: int
    signature: str


def _typed(ch: Channel, cumulative: int) -> dict:
    return {
        "types": VOUCHER_TYPES,
        "primaryType": "Voucher",
        "domain": {"name": DOMAIN_NAME, "version": DOMAIN_VERSION, "chainId": ch.chain_id,
                   "verifyingContract": to_checksum_address(ch.contract)},
        "message": {"channelId": bytes.fromhex(ch.channel_id[2:]), "cumulative": cumulative},
    }


def voucher_digest(ch: Channel, cumulative: int) -> bytes:
    """The 32 bytes the contract's voucherDigest() returns and the payer signs."""
    m = encode_typed_data(full_message=_typed(ch, cumulative))
    return keccak(b"\x19" + m.version + m.header + m.body)


def sign_voucher(ch: Channel, cumulative: int, private_key: str) -> Voucher:
    if not 0 <= cumulative <= ch.deposit:
        raise ValueError("voucher must be between 0 and the channel's deposit")
    sig = Account.sign_message(encode_typed_data(full_message=_typed(ch, cumulative)), private_key)
    return Voucher(ch.channel_id, cumulative, "0x" + sig.signature.hex().removeprefix("0x"))


def voucher_signer(ch: Channel, v: Voucher) -> str:
    return Account.recover_message(encode_typed_data(full_message=_typed(ch, v.cumulative)),
                                   signature=v.signature)


# ── Chunked pay ───────────────────────────────────────────────────────────────

@dataclass
class Pricing:
    per_token: int          # base units per output token
    chunk_tokens: int = 64  # the most a worker produces beyond what's paid


@dataclass
class RequesterSide:
    """Signs for what actually arrived, never more, never past the deposit."""
    channel: Channel
    private_key: str
    pricing: Pricing
    spent_before: int = 0   # what earlier tasks on this channel already used
    received: int = 0       # output tokens received for the current task

    def receive(self, tokens: int) -> Voucher:
        self.received += tokens
        cumulative = min(self.spent_before + self.received * self.pricing.per_token, self.channel.deposit)
        return sign_voucher(self.channel, cumulative, self.private_key)

    def finish_task(self) -> None:
        self.spent_before = min(self.spent_before + self.received * self.pricing.per_token, self.channel.deposit)
        self.received = 0


@dataclass
class WorkerSide:
    """Keeps the latest valid voucher and decides whether to keep generating."""
    channel: Channel
    pricing: Pricing
    best: Voucher | None = None
    paid_before: int = 0    # cumulative at the start of the current task
    produced: int = 0       # output tokens produced for the current task
    rejected: list = field(default_factory=list)

    @property
    def paid(self) -> int:
        return self.best.cumulative if self.best else 0

    def accept(self, v: Voucher) -> bool:
        """Verify and keep a voucher. Rejects wrong channel, wrong signer, over
        the deposit, or not more than what's already held."""
        ok = (v.channel_id == self.channel.channel_id
              and v.cumulative <= self.channel.deposit
              and v.cumulative > self.paid
              and voucher_signer(self.channel, v).lower() == self.channel.payer.lower())
        if ok:
            self.best = v
        else:
            self.rejected.append(v)
        return ok

    def unpaid_tokens(self) -> int:
        paid_this_task = max(0, self.paid - self.paid_before)
        return self.produced - paid_this_task // self.pricing.per_token

    def may_produce(self, tokens: int) -> bool:
        """At most one chunk beyond what has been paid for."""
        return self.unpaid_tokens() + tokens <= self.pricing.chunk_tokens

    def produce(self, tokens: int) -> None:
        self.produced += tokens

    def finish_task(self) -> None:
        self.paid_before = self.paid
        self.produced = 0

    def claim_args(self) -> tuple[str, int, str] | None:
        """(channelId, cumulative, signature) for PaymentChannels.claim, or None."""
        return (self.best.channel_id, self.best.cumulative, self.best.signature) if self.best else None
