import pytest

from updown_bot.config import ConfigError, load_config, parse_multipliers

LIVE_ENV = {
    "DRY_RUN": "false",
    "POLY_PRIVATE_KEY": "0x" + "11" * 32,
    "POLY_API_KEY": "key",
    "POLY_API_SECRET": "secret",
    "POLY_API_PASSPHRASE": "pass",
    "POLY_FUNDER_ADDRESS": "0x" + "aa" * 20,
    "WALLET_ADDRESS": "0x" + "bb" * 20,
    "POLYGON_RPC_URL": "https://rpc.example",
}


def test_defaults_are_a_dry_run_without_credentials():
    cfg = load_config({})
    assert cfg.dry_run
    assert cfg.credentials is None
    assert cfg.assets == ("BTC", "ETH", "SOL", "XRP", "DOGE")


def test_live_mode_names_the_missing_credentials():
    env = dict(LIVE_ENV)
    del env["POLY_API_SECRET"], env["POLYGON_RPC_URL"]
    with pytest.raises(ConfigError) as err:
        load_config(env)
    assert "POLY_API_SECRET" in str(err.value)
    assert "POLYGON_RPC_URL" in str(err.value)
    assert "POLY_API_KEY" not in str(err.value)


def test_live_mode_loads_credentials():
    creds = load_config(LIVE_ENV).credentials
    assert creds.api_key == "key"
    assert creds.signature_type == 2


@pytest.mark.parametrize(
    "signature_type, expected", [("2", "0x" + "aa" * 20), ("0", "0x" + "bb" * 20)]
)
def test_funding_address_depends_on_wallet_type(signature_type, expected):
    creds = load_config({**LIVE_ENV, "POLY_SIGNATURE_TYPE": signature_type}).credentials
    assert creds.funding_address == expected


def test_assets_are_normalised():
    assert load_config({"ASSETS": " btc, eth ,"}).assets == ("BTC", "ETH")


@pytest.mark.parametrize(
    "env",
    [
        {"STAKE_USDC": "abc"},
        {"STAKE_USDC": "0"},
        {"DRY_RUN": "maybe"},
        {"MAX_ENTRIES_PER_WINDOW": "1.5"},
        {"LATE_MIN_PRICE": "0.96"},
        {"EARLY_MAX_PRICE": "1.0"},
        {"LATE_MIN_TTR_SEC": "90"},
        {"ASSETS": ","},
        {"ASSET_STAKE_MULT": "XRP=0.5"},
    ],
)
def test_bad_values_are_rejected(env):
    with pytest.raises(ConfigError):
        load_config(env)


def test_error_names_the_variable():
    with pytest.raises(ConfigError, match="STAKE_USDC"):
        load_config({"STAKE_USDC": "abc"})


def test_parse_multipliers():
    assert parse_multipliers("xrp:0.5, ETH:0.75") == {"XRP": 0.5, "ETH": 0.75}
    assert parse_multipliers("") == {}
