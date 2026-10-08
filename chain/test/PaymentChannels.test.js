import { expect } from "chai";
import hre from "hardhat";

// Asserts the call reverts with the given reason (this repo doesn't use the
// chai matchers plugin).
async function expectRevert(promise, reason) {
  try {
    await promise;
  } catch (e) {
    expect(e.message).to.include(reason);
    return;
  }
  expect.fail(`expected revert: ${reason}`);
}

describe("PaymentChannels", function () {
  this.timeout(60000);

  let conn, ethers, networkHelpers;
  let token, channels;
  let deployer, payer, payee, stranger;
  const TTL = 3600;
  const SALT = "0x" + "11".repeat(32);

  before(async function () {
    conn = await hre.network.connect();
    ethers = conn.ethers;
  });

  after(async function () {
    await conn.close();
  });

  async function domain() {
    const { chainId } = await ethers.provider.getNetwork();
    return { name: "DAI PaymentChannels", version: "1", chainId, verifyingContract: await channels.getAddress() };
  }

  const TYPES = { Voucher: [{ name: "channelId", type: "bytes32" }, { name: "cumulative", type: "uint256" }] };

  async function voucher(signer, id, cumulative) {
    return signer.signTypedData(await domain(), TYPES, { channelId: id, cumulative });
  }

  async function openChannel(amount = ethers.parseEther("10"), ttl = TTL, salt = SALT) {
    await token.connect(payer).approve(await channels.getAddress(), amount);
    await channels.connect(payer).open(payee.address, amount, ttl, salt);
    return channels.channelId(payer.address, payee.address, salt);
  }

  async function increaseTime(seconds) {
    await ethers.provider.send("evm_increaseTime", [seconds]);
    await ethers.provider.send("evm_mine", []);
  }

  beforeEach(async function () {
    [deployer, payer, payee, stranger] = await ethers.getSigners();
    const Token = await ethers.getContractFactory("DAIToken");
    token = await Token.deploy();
    const Channels = await ethers.getContractFactory("PaymentChannels");
    channels = await Channels.deploy(await token.getAddress());
    await token.grantRole(await token.MINTER_ROLE(), deployer.address);
    await token.mint(payer.address, ethers.parseEther("100"));
  });

  it("escrows the deposit when a channel opens", async function () {
    const id = await openChannel();
    const c = await channels.channels(id);
    expect(c.payer).to.equal(payer.address);
    expect(c.payee).to.equal(payee.address);
    expect(c.deposit).to.equal(ethers.parseEther("10"));
    expect(await token.balanceOf(await channels.getAddress())).to.equal(ethers.parseEther("10"));
  });

  it("the contract's digest is the standard EIP-712 hash (what off-chain signers compute)", async function () {
    const id = await openChannel();
    const v = { channelId: id, cumulative: ethers.parseEther("1.5") };
    expect(await channels.voucherDigest(id, v.cumulative))
      .to.equal(ethers.TypedDataEncoder.hash(await domain(), TYPES, v));
  });

  it("only the latest voucher matters: the payee gets the cumulative total once", async function () {
    const id = await openChannel();
    const vouchers = [];
    for (const amt of ["0.5", "1.0", "1.5", "2.0"]) {
      vouchers.push([ethers.parseEther(amt), await voucher(payer, id, ethers.parseEther(amt))]);
    }
    const [last, sig] = vouchers.at(-1);
    await channels.connect(payee).claim(id, last, sig);
    expect(await token.balanceOf(payee.address)).to.equal(ethers.parseEther("2.0"));
    // an older voucher can't be replayed for more
    const [older, oldSig] = vouchers[1];
    await expectRevert(channels.connect(payee).claim(id, older, oldSig), "Nothing new to claim");
  });

  it("claiming in steps pays only the difference each time", async function () {
    const id = await openChannel();
    await channels.connect(payee).claim(id, ethers.parseEther("1"), await voucher(payer, id, ethers.parseEther("1")));
    await channels.connect(payee).claim(id, ethers.parseEther("3"), await voucher(payer, id, ethers.parseEther("3")));
    expect(await token.balanceOf(payee.address)).to.equal(ethers.parseEther("3"));
    expect((await channels.channels(id)).claimed).to.equal(ethers.parseEther("3"));
  });

  it("anyone may submit a claim, but the money goes to the payee", async function () {
    const id = await openChannel();
    const amt = ethers.parseEther("2");
    await channels.connect(stranger).claim(id, amt, await voucher(payer, id, amt));
    expect(await token.balanceOf(payee.address)).to.equal(amt);
    expect(await token.balanceOf(stranger.address)).to.equal(0n);
  });

  it("rejects vouchers not signed by the payer", async function () {
    const id = await openChannel();
    const amt = ethers.parseEther("2");
    await expectRevert(channels.connect(payee).claim(id, amt, await voucher(payee, id, amt)), "Bad signature");
    await expectRevert(channels.connect(payee).claim(id, amt, await voucher(stranger, id, amt)), "Bad signature");
  });

  it("rejects a voucher for another channel", async function () {
    const id = await openChannel();
    const other = await openChannel(ethers.parseEther("5"), TTL, "0x" + "22".repeat(32));
    const amt = ethers.parseEther("1");
    await expectRevert(channels.connect(payee).claim(id, amt, await voucher(payer, other, amt)), "Bad signature");
  });

  it("can't pay out more than was deposited", async function () {
    const id = await openChannel();
    const amt = ethers.parseEther("11");
    await expectRevert(channels.connect(payee).claim(id, amt, await voucher(payer, id, amt)), "Voucher exceeds deposit");
  });

  it("after expiry the payer takes back the unclaimed rest, and claims stop", async function () {
    const id = await openChannel();
    const amt = ethers.parseEther("4");
    await channels.connect(payee).claim(id, amt, await voucher(payer, id, amt));
    await expectRevert(channels.connect(payer).reclaim(id), "Not expired");
    await increaseTime(TTL + 1);
    const later = await voucher(payer, id, ethers.parseEther("6"));
    await channels.connect(payer).reclaim(id);
    expect(await token.balanceOf(payer.address)).to.equal(ethers.parseEther("96"));
    await expectRevert(channels.connect(payee).claim(id, ethers.parseEther("6"), later), "Channel closed");
  });

  it("a payee can still claim after expiry until the payer reclaims", async function () {
    const id = await openChannel();
    await increaseTime(TTL + 1);
    const amt = ethers.parseEther("3");
    await channels.connect(payee).claim(id, amt, await voucher(payer, id, amt));
    expect(await token.balanceOf(payee.address)).to.equal(amt);
  });

  it("only the payer can reclaim or top up", async function () {
    const id = await openChannel();
    await increaseTime(TTL + 1);
    await expectRevert(channels.connect(payee).reclaim(id), "Not your channel");
    await expectRevert(channels.connect(stranger).topUp(id, 0, 0), "Not your channel");
  });

  it("top-ups add funds and can only push the expiry later", async function () {
    const id = await openChannel();
    const c = await channels.channels(id);
    await token.connect(payer).approve(await channels.getAddress(), ethers.parseEther("5"));
    await expectRevert(channels.connect(payer).topUp(id, ethers.parseEther("5"), c.expiresAt - 1n), "Expiry can only move later");
    await channels.connect(payer).topUp(id, ethers.parseEther("5"), c.expiresAt + 600n);
    const after = await channels.channels(id);
    expect(after.deposit).to.equal(ethers.parseEther("15"));
    expect(after.expiresAt).to.equal(c.expiresAt + 600n);
  });

  it("claimMany settles several channels in one transaction", async function () {
    const ids = [];
    const amts = [];
    const sigs = [];
    for (let i = 0; i < 5; i++) {
      const id = await openChannel(ethers.parseEther("2"), TTL, ethers.zeroPadValue(ethers.toBeHex(i + 1), 32));
      const amt = ethers.parseEther("1");
      ids.push(id); amts.push(amt); sigs.push(await voucher(payer, id, amt));
    }
    const tx = await channels.connect(payee).claimMany(ids, amts, sigs);
    const receipt = await tx.wait();
    expect(await token.balanceOf(payee.address)).to.equal(ethers.parseEther("5"));
    // Record what a batched claim costs per channel; a single claim for comparison.
    const id = await openChannel(ethers.parseEther("2"), TTL, "0x" + "33".repeat(32));
    const single = await (await channels.connect(payee).claim(id, ethers.parseEther("1"),
      await voucher(payer, id, ethers.parseEther("1")))).wait();
    console.log(`      gas: one claim ${single.gasUsed}, claimMany x5 ${receipt.gasUsed} (${receipt.gasUsed / 5n} each)`);
    expect(receipt.gasUsed / 5n).to.be.lessThan(single.gasUsed);
  });

  it("refuses a channel to yourself or to nobody, and duplicate ids", async function () {
    await token.connect(payer).approve(await channels.getAddress(), ethers.parseEther("10"));
    await expectRevert(channels.connect(payer).open(payer.address, 1n, TTL, SALT), "Invalid payee");
    await expectRevert(channels.connect(payer).open(ethers.ZeroAddress, 1n, TTL, SALT), "Invalid payee");
    await channels.connect(payer).open(payee.address, 1n, TTL, SALT);
    await expectRevert(channels.connect(payer).open(payee.address, 1n, TTL, SALT), "Channel exists");
  });
});
