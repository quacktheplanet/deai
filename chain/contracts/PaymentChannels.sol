// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import "@openzeppelin/contracts/utils/cryptography/ECDSA.sol";

/**
 * @title PaymentChannels
 * @notice Requester-funded payment without a trusted publisher
 *         (docs/DECENTRALIZATION.md, "Payment: the hard problem").
 *
 * A requester (payer) opens a channel to a worker (payee) with a deposit.
 * As the worker's answer streams in, the requester signs vouchers off-chain:
 * "the payee of channel C may now claim up to X in total". Each voucher
 * supersedes the last, so only the latest one matters. The worker claims
 * once (or now and then) with the latest voucher; no orchestrator is
 * involved and nothing is minted.
 *
 * How this answers the design doc's open problems:
 *   - A requester refusing to pay: vouchers are signed per small chunk of
 *     output and the worker stops when payment stops, so the most it can
 *     lose is one chunk.
 *   - Self-dealing: payment comes from the requester's own deposit, so
 *     paying yourself for fake work only moves your own money.
 *   - On-chain cost: one claim per channel (or many channels in one
 *     claimMany), not one transaction per task.
 *
 * Expiry: after `expiresAt` the payer can take back what was not claimed.
 * Until then only the payee's claims move funds. A payee should claim before
 * expiry; a claim still works after expiry as long as the payer hasn't
 * reclaimed yet.
 *
 * No prompt content or IP address is ever stored here (SECURITY.md).
 */
contract PaymentChannels is ReentrancyGuard {
    using SafeERC20 for IERC20;

    IERC20 public immutable token;

    struct Channel {
        address payer;
        address payee;
        uint256 deposit;   // total ever deposited
        uint256 claimed;   // total paid out to the payee so far
        uint64 expiresAt;  // after this the payer may reclaim the rest
        bool closed;       // the payer reclaimed; no more claims
    }

    mapping(bytes32 => Channel) public channels;

    bytes32 public constant VOUCHER_TYPEHASH = keccak256("Voucher(bytes32 channelId,uint256 cumulative)");

    // EIP-712 domain, computed here rather than via OpenZeppelin's EIP712 (whose
    // dependencies need a newer EVM target than this repo builds for).
    bytes32 private constant DOMAIN_TYPEHASH =
        keccak256("EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)");
    bytes32 private constant NAME_HASH = keccak256("DAI PaymentChannels");
    bytes32 private constant VERSION_HASH = keccak256("1");
    uint256 private immutable _cachedChainId;
    bytes32 private immutable _cachedDomainSeparator;

    event ChannelOpened(bytes32 indexed id, address indexed payer, address indexed payee, uint256 deposit, uint64 expiresAt);
    event ChannelToppedUp(bytes32 indexed id, uint256 added, uint64 expiresAt);
    event Claimed(bytes32 indexed id, address indexed payee, uint256 amount, uint256 cumulative);
    event Reclaimed(bytes32 indexed id, address indexed payer, uint256 amount);

    constructor(address tokenAddress) {
        token = IERC20(tokenAddress);
        _cachedChainId = block.chainid;
        _cachedDomainSeparator = _buildDomainSeparator();
    }

    function _buildDomainSeparator() private view returns (bytes32) {
        return keccak256(abi.encode(DOMAIN_TYPEHASH, NAME_HASH, VERSION_HASH, block.chainid, address(this)));
    }

    function domainSeparator() public view returns (bytes32) {
        // Recomputed after a chain fork so vouchers can't be replayed across chains.
        return block.chainid == _cachedChainId ? _cachedDomainSeparator : _buildDomainSeparator();
    }

    function channelId(address payer, address payee, bytes32 salt) public pure returns (bytes32) {
        return keccak256(abi.encode(payer, payee, salt));
    }

    /// @notice The EIP-712 digest a payer signs for a voucher (for off-chain tooling).
    function voucherDigest(bytes32 id, uint256 cumulative) public view returns (bytes32) {
        bytes32 structHash = keccak256(abi.encode(VOUCHER_TYPEHASH, id, cumulative));
        return keccak256(abi.encodePacked("\x19\x01", domainSeparator(), structHash));
    }

    function open(address payee, uint256 amount, uint64 ttlSeconds, bytes32 salt)
        external nonReentrant returns (bytes32 id)
    {
        require(payee != address(0) && payee != msg.sender, "Invalid payee");
        require(amount > 0, "Amount must be > 0");
        id = channelId(msg.sender, payee, salt);
        require(channels[id].payer == address(0), "Channel exists");
        uint64 expiresAt = uint64(block.timestamp) + ttlSeconds;
        channels[id] = Channel(msg.sender, payee, amount, 0, expiresAt, false);
        token.safeTransferFrom(msg.sender, address(this), amount);
        emit ChannelOpened(id, msg.sender, payee, amount, expiresAt);
    }

    /// @notice Add funds and/or push the expiry later (never earlier).
    function topUp(bytes32 id, uint256 amount, uint64 newExpiresAt) external nonReentrant {
        Channel storage c = channels[id];
        require(c.payer == msg.sender, "Not your channel");
        require(!c.closed, "Channel closed");
        require(newExpiresAt >= c.expiresAt, "Expiry can only move later");
        c.deposit += amount;
        c.expiresAt = newExpiresAt;
        if (amount > 0) {
            token.safeTransferFrom(msg.sender, address(this), amount);
        }
        emit ChannelToppedUp(id, amount, newExpiresAt);
    }

    /// @notice Pay the payee up to `cumulative` in total, on the payer's signature.
    ///         Anyone may submit it (e.g. a relayer); the money always goes to the payee.
    function claim(bytes32 id, uint256 cumulative, bytes calldata signature) external nonReentrant {
        _claim(id, cumulative, signature);
    }

    /// @notice Several channels' latest vouchers in one transaction.
    function claimMany(bytes32[] calldata ids, uint256[] calldata cumulatives, bytes[] calldata signatures)
        external nonReentrant
    {
        require(ids.length == cumulatives.length && ids.length == signatures.length, "Length mismatch");
        for (uint256 i = 0; i < ids.length; i++) {
            _claim(ids[i], cumulatives[i], signatures[i]);
        }
    }

    function _claim(bytes32 id, uint256 cumulative, bytes calldata signature) internal {
        Channel storage c = channels[id];
        require(c.payer != address(0), "No such channel");
        require(!c.closed, "Channel closed");
        require(cumulative <= c.deposit, "Voucher exceeds deposit");
        require(cumulative > c.claimed, "Nothing new to claim");
        require(ECDSA.recover(voucherDigest(id, cumulative), signature) == c.payer, "Bad signature");
        uint256 amount = cumulative - c.claimed;
        c.claimed = cumulative;
        token.safeTransfer(c.payee, amount);
        emit Claimed(id, c.payee, amount, cumulative);
    }

    /// @notice After expiry, the payer takes back whatever wasn't claimed.
    function reclaim(bytes32 id) external nonReentrant {
        Channel storage c = channels[id];
        require(c.payer == msg.sender, "Not your channel");
        require(!c.closed, "Channel closed");
        require(block.timestamp > c.expiresAt, "Not expired");
        c.closed = true;
        uint256 rest = c.deposit - c.claimed;
        if (rest > 0) {
            token.safeTransfer(c.payer, rest);
        }
        emit Reclaimed(id, c.payer, rest);
    }
}
