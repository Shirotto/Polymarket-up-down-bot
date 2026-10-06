"""On-chain redemption of winning shares held by a Polymarket proxy wallet.

Accounts created with an email login (signature type 2) keep their shares on
a Gnosis Safe whose only owner is the signer key. To cash in a won round the
Safe has to call ``redeemPositions`` on the Conditional Tokens contract, which
means wrapping that call in the Safe's ``execTransaction`` with an EIP-712
signature from the owner. The signer account broadcasts the transaction and
pays the gas in POL.

With ``dry=True`` everything is built, signed and simulated with eth_call but
nothing is broadcast.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from eth_abi import encode
from web3 import Web3

from .wallet import USDC_ADDRESS

log = logging.getLogger(__name__)

POLYGON_CHAIN_ID = 137
CTF_ADDRESS = Web3.to_checksum_address("0x4D97DCd97eC945f40cF65F87097ACe5EA0476045")
COLLATERAL = Web3.to_checksum_address(USDC_ADDRESS)
ZERO_ADDRESS = "0x" + "00" * 20

REDEEM_SELECTOR = Web3.keccak(text="redeemPositions(address,bytes32,bytes32,uint256[])")[:4]
EXEC_SELECTOR = Web3.keccak(
    text="execTransaction(address,uint256,bytes,uint8,uint256,uint256,uint256,address,address,bytes)"
)[:4]
NONCE_SELECTOR = Web3.keccak(text="nonce()")[:4]
DOMAIN_SEPARATOR_SELECTOR = Web3.keccak(text="domainSeparator()")[:4]

# Safe v1.3.0
SAFE_TX_TYPEHASH = Web3.keccak(
    text="SafeTx(address to,uint256 value,bytes data,uint8 operation,"
    "uint256 safeTxGas,uint256 baseGas,uint256 gasPrice,address gasToken,"
    "address refundReceiver,uint256 nonce)"
)

# Both outcomes of a binary market; the losing side redeems for nothing.
INDEX_SETS = [1, 2]

MAX_FAILED_ATTEMPTS = 3
MIN_PRIORITY_FEE_WEI = 30 * 10**9
FALLBACK_GAS = 250_000
# Kept below the main loop's watchdog timeout: the sweep runs on the loop thread.
RECEIPT_TIMEOUT_SEC = 120


@dataclass
class Pending:
    condition_id: str
    slug: str
    queued_ts: float = field(default_factory=time.time)
    attempts: int = 0


def redeem_calldata(condition_id: str) -> bytes:
    condition = bytes.fromhex(condition_id.removeprefix("0x"))
    if len(condition) != 32:
        raise ValueError(f"condition id must be 32 bytes, got {len(condition)}")
    args = encode(
        ["address", "bytes32", "bytes32", "uint256[]"],
        [COLLATERAL, b"\x00" * 32, condition, INDEX_SETS],
    )
    return REDEEM_SELECTOR + args


def safe_tx_digest(domain_separator: bytes, to: str, data: bytes, nonce: int) -> bytes:
    """EIP-712 hash of a zero-value, zero-refund Safe call."""
    struct_hash = Web3.keccak(
        encode(
            [
                "bytes32",
                "address",
                "uint256",
                "bytes32",
                "uint8",
                "uint256",
                "uint256",
                "uint256",
                "address",
                "address",
                "uint256",
            ],
            [
                SAFE_TX_TYPEHASH,
                to,
                0,
                Web3.keccak(data),
                0,
                0,
                0,
                0,
                ZERO_ADDRESS,
                ZERO_ADDRESS,
                nonce,
            ],
        )
    )
    return Web3.keccak(b"\x19\x01" + domain_separator + struct_hash)


def exec_calldata(to: str, data: bytes, signature: bytes) -> bytes:
    args = encode(
        [
            "address",
            "uint256",
            "bytes",
            "uint8",
            "uint256",
            "uint256",
            "uint256",
            "address",
            "address",
            "bytes",
        ],
        [to, 0, data, 0, 0, 0, 0, ZERO_ADDRESS, ZERO_ADDRESS, signature],
    )
    return EXEC_SELECTOR + args


class Redeemer:
    def __init__(
        self,
        rpc_url: str,
        private_key: str,
        safe_address: str,
        state_path: Path,
        dry: bool = True,
        min_interval_sec: int = 90,
    ):
        self.dry = dry
        self.min_interval_sec = min_interval_sec
        self.last_tx_hash: Optional[str] = None
        self.last_tx_ts: Optional[float] = None
        self._w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 15}))
        self._account = self._w3.eth.account.from_key(private_key)
        self._safe = Web3.to_checksum_address(safe_address)
        self._path = Path(state_path)
        self._pending: list[Pending] = []
        self._redeemed: set[str] = set()
        self._last_sweep_ts = 0.0
        self._load()

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def queue(self, condition_id: Optional[str], slug: str) -> None:
        if not condition_id:
            log.warning("cannot redeem %s: no condition id", slug)
            return
        cid = condition_id.lower()
        if cid in self._redeemed or any(p.condition_id == cid for p in self._pending):
            return
        self._pending.append(Pending(cid, slug))
        self._save()
        log.info("queued %s for redemption (%d pending)", slug, len(self._pending))

    def sweep(self, force: bool = False) -> Optional[str]:
        """Redeem at most one queued condition. Returns the tx hash if one confirmed."""
        now = time.time()
        if not self._pending or (not force and now - self._last_sweep_ts < self.min_interval_sec):
            return None
        self._last_sweep_ts = now

        try:
            base_nonce = self._safe_nonce()
            domain = self._domain_separator()
        except Exception as exc:
            log.warning("could not read Safe state: %s", exc)
            return None

        target = None
        for item in list(self._pending):
            data = self._signed_call(item, domain, base_nonce)
            if data is not None:
                target = item
                break
            # A call that reverts on its own is either not resolved yet or
            # already redeemed. Give it a few sweeps, then stop asking.
            item.attempts += 1
            if item.attempts >= MAX_FAILED_ATTEMPTS:
                log.info("giving up on %s after %d reverts", item.slug, item.attempts)
                self._pending.remove(item)
                self._redeemed.add(item.condition_id)
        self._save()
        if target is None:
            return None

        if self.dry:
            log.info("redemption of %s simulates fine; not broadcasting (dry)", target.slug)
            return None
        tx_hash = self._broadcast(target, data)
        if tx_hash is None:
            target.attempts += 1
        else:
            self._pending.remove(target)
            self._redeemed.add(target.condition_id)
            self.last_tx_hash = tx_hash
            self.last_tx_ts = time.time()
        self._save()
        return tx_hash

    def _safe_nonce(self) -> int:
        # Load-balanced RPC providers sometimes answer from a node that is a
        # few blocks behind. Ask several views and keep the highest nonce.
        seen = []
        for block in ("latest", "latest", "finalized", "pending"):
            try:
                raw = self._w3.eth.call({"to": self._safe, "data": NONCE_SELECTOR}, block)
                seen.append(int.from_bytes(raw, "big"))
            except Exception:
                continue
        if not seen:
            raise RuntimeError("no RPC view returned the Safe nonce")
        return max(seen)

    def _domain_separator(self) -> bytes:
        raw = bytes(self._w3.eth.call({"to": self._safe, "data": DOMAIN_SEPARATOR_SELECTOR}))
        if len(raw) != 32:
            raise RuntimeError(f"unexpected domain separator length {len(raw)}")
        return raw

    def _sign(self, digest: bytes) -> bytes:
        signed = self._account.unsafe_sign_hash(digest)
        # The Safe wants r || s || v with v in {27, 28}.
        v = signed.v if signed.v >= 27 else signed.v + 27
        return signed.r.to_bytes(32, "big") + signed.s.to_bytes(32, "big") + bytes([v])

    def _signed_call(self, item: Pending, domain: bytes, base_nonce: int) -> Optional[bytes]:
        """Return execTransaction calldata that simulates successfully, if any.

        Even the highest nonce read above can lag the chain, so a window of
        nonces around it is tried and the highest one that passes wins.
        """
        inner = redeem_calldata(item.condition_id)
        best = None
        for nonce in range(max(0, base_nonce - 2), base_nonce + 21):
            digest = safe_tx_digest(domain, CTF_ADDRESS, inner, nonce)
            data = exec_calldata(CTF_ADDRESS, inner, self._sign(digest))
            try:
                out = self._w3.eth.call(
                    {"from": self._account.address, "to": self._safe, "data": data}
                )
            except Exception:
                continue
            if len(out) >= 32 and int.from_bytes(bytes(out)[-32:], "big") == 1:
                best = data
        return best

    def _broadcast(self, item: Pending, data: bytes) -> Optional[str]:
        call = {"from": self._account.address, "to": self._safe, "value": 0, "data": data}
        try:
            gas_price = int(self._w3.eth.gas_price)
            priority = max(gas_price, MIN_PRIORITY_FEE_WEI)
            try:
                gas = int(self._w3.eth.estimate_gas(call) * 1.3)
            except Exception as exc:
                # The estimate occasionally fails for a call that eth_call
                # accepts. Redemptions use about 120k, so this is generous.
                log.warning("gas estimate failed (%s), using %d", exc, FALLBACK_GAS)
                gas = FALLBACK_GAS
            tx = {
                **call,
                "nonce": self._w3.eth.get_transaction_count(self._account.address),
                "chainId": POLYGON_CHAIN_ID,
                "gas": gas,
                "maxFeePerGas": max(gas_price * 2, priority + MIN_PRIORITY_FEE_WEI),
                "maxPriorityFeePerGas": priority,
            }
            signed = self._account.sign_transaction(tx)
            tx_hash = self._w3.eth.send_raw_transaction(signed.raw_transaction)
            log.info("redeeming %s: sent %s", item.slug, tx_hash.to_0x_hex())
            receipt = self._w3.eth.wait_for_transaction_receipt(
                tx_hash, timeout=RECEIPT_TIMEOUT_SEC
            )
        except Exception as exc:
            log.error("redemption of %s failed: %s", item.slug, exc)
            return None
        if receipt.status != 1:
            log.error("redemption of %s reverted (%s)", item.slug, tx_hash.to_0x_hex())
            return None
        log.info("redeemed %s in block %d", item.slug, receipt.blockNumber)
        return tx_hash.to_0x_hex()

    def _load(self) -> None:
        try:
            data = json.loads(self._path.read_text())
        except FileNotFoundError:
            return
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("redeemer state unreadable: %s", exc)
            return
        self._redeemed = {c.lower() for c in data.get("redeemed", [])}
        for raw in data.get("pending", []):
            cid = str(raw.get("condition_id", "")).lower()
            if cid and cid not in self._redeemed:
                self._pending.append(
                    Pending(
                        condition_id=cid,
                        slug=raw.get("slug", "?"),
                        queued_ts=float(raw.get("queued_ts", time.time())),
                        attempts=int(raw.get("attempts", 0)),
                    )
                )

    def _save(self) -> None:
        payload = {
            "redeemed": sorted(self._redeemed),
            "pending": [asdict(p) for p in self._pending],
        }
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_name(self._path.name + ".tmp")
            tmp.write_text(json.dumps(payload, indent=2))
            tmp.replace(self._path)
        except OSError as exc:
            log.warning("could not save redeemer state: %s", exc)
