import pytest
from eth_abi import decode
from eth_account import Account
from web3 import Web3

from updown_bot import redeem

CONDITION = "0x" + "ab" * 32
SAFE_TX_TYPES = [
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
]


def test_redeem_calldata_decodes_back():
    data = redeem.redeem_calldata(CONDITION)
    assert data[:4] == Web3.keccak(text="redeemPositions(address,bytes32,bytes32,uint256[])")[:4]
    collateral, parent, condition, index_sets = decode(
        ["address", "bytes32", "bytes32", "uint256[]"], data[4:]
    )
    assert Web3.to_checksum_address(collateral) == redeem.COLLATERAL
    assert parent == b"\x00" * 32
    assert condition == bytes.fromhex("ab" * 32)
    assert list(index_sets) == [1, 2]


def test_redeem_calldata_rejects_a_short_condition_id():
    with pytest.raises(ValueError):
        redeem.redeem_calldata("0xabcd")


def test_exec_calldata_wraps_the_inner_call():
    inner = redeem.redeem_calldata(CONDITION)
    data = redeem.exec_calldata(redeem.CTF_ADDRESS, inner, b"\x01" * 65)
    to, value, payload, operation, *_gas, gas_token, refund, signature = decode(
        SAFE_TX_TYPES, data[4:]
    )
    assert Web3.to_checksum_address(to) == redeem.CTF_ADDRESS
    assert (value, operation) == (0, 0)
    assert payload == inner
    assert signature == b"\x01" * 65


def test_digest_depends_on_nonce_and_domain():
    inner = redeem.redeem_calldata(CONDITION)
    domain = b"\x11" * 32
    base = redeem.safe_tx_digest(domain, redeem.CTF_ADDRESS, inner, 5)
    assert len(base) == 32
    assert base == redeem.safe_tx_digest(domain, redeem.CTF_ADDRESS, inner, 5)
    assert base != redeem.safe_tx_digest(domain, redeem.CTF_ADDRESS, inner, 6)
    assert base != redeem.safe_tx_digest(b"\x22" * 32, redeem.CTF_ADDRESS, inner, 5)


def make_redeemer(tmp_path, **kwargs):
    return redeem.Redeemer(
        rpc_url="http://127.0.0.1:1",
        private_key="0x" + "11" * 32,
        safe_address="0x" + "aa" * 20,
        state_path=tmp_path / "redeemer.json",
        **kwargs,
    )


def test_signature_recovers_to_the_owner(tmp_path):
    redeemer = make_redeemer(tmp_path)
    digest = redeem.safe_tx_digest(b"\x11" * 32, redeem.CTF_ADDRESS, b"\x00", 1)
    signature = redeemer._sign(digest)
    assert len(signature) == 65
    assert signature[-1] in (27, 28)
    owner = Account.from_key("0x" + "11" * 32).address
    assert Account._recover_hash(digest, signature=signature) == owner


def test_queue_deduplicates_and_survives_a_restart(tmp_path):
    redeemer = make_redeemer(tmp_path)
    redeemer.queue(CONDITION, "btc-updown-5m-1000")
    redeemer.queue(CONDITION.upper().replace("0X", "0x"), "btc-updown-5m-1000")
    redeemer.queue(None, "eth-updown-5m-1000")
    assert redeemer.pending_count == 1
    assert make_redeemer(tmp_path).pending_count == 1


def test_sweep_with_an_empty_queue_does_nothing(tmp_path):
    assert make_redeemer(tmp_path).sweep(force=True) is None
