import os
from typing import Dict

from brownie import chain
from brownie import interface  # noqa
from prometheus_client import Gauge
from prometheus_client import start_http_server  # noqa
from web3 import Web3
from web3.exceptions import BlockNotFound

from scripts.addresses import ADDRESSES_FANTOM
from scripts.addresses import CHAIN_FANTOM
from scripts.addresses import checksum_address_dict
from scripts.data import get_json_request
from scripts.data import get_token_prices_in_usd
from scripts.data import get_wallet_balances_by_token
from scripts.logconf import log

PROMETHEUS_PORT = 8801

NETWORK = "Fantom"

# get all addresses
ADDRESSES = checksum_address_dict(ADDRESSES_FANTOM)
BADGER_WALLETS = ADDRESSES["badger_wallets"]
TREASURY_TOKENS = ADDRESSES["treasury_tokens"]
COINGECKO_TOKENS = ADDRESSES["coingecko_tokens"]
LP_TOKENS = ADDRESSES["lp_tokens"]

WEB3_INSTANCE = Web3(Web3.HTTPProvider(os.environ['FTMNODEURL']))


def get_token_prices(token_csv: str) -> Dict:
    log.info("Fetching token prices from CoinGecko ...")

    url = (
        f"https://api.coingecko.com/api/v3/simple/token_price/fantom?"
        f"contract_addresses={token_csv}&vs_currencies=usd"
    )

    token_prices = get_json_request(request_type="get", url=url)
    return token_prices


def update_wallets_gauge(
    wallets_gauge: Gauge,
    wallet_balances_by_token: Dict,
    token_name: str,
    token_address: str,
    token_prices: Dict,
    token_prices_from_api: Dict,
) -> None:
    """
    Function that updates wallets gauge;
    It updates both token balances and balances in USD.

    token_prices_from_api is used to obtain USD price for lp tokens such as BSMM_WBTC_RENBTC,
    because it's tricky to calculate them using contract and FTM RPC is slow
    """
    log.info(f"Processing wallet balances for [bold]{token_name}: {token_address} ...")

    wallet_info = wallet_balances_by_token[token_address]
    for wallet in wallet_info.describe():
        (
            token_name,
            token_address,
            token_balance,
            wallet_name,
            wallet_address,
        ) = wallet.values()

        ftm_balance = float(WEB3_INSTANCE.fromWei(
            WEB3_INSTANCE.eth.getBalance(wallet_address), "ether")
        )
        token_address = token_address.lower()
        wftm_address = COINGECKO_TOKENS['WFTM'].lower()
        # Amount of tokens in wallet
        wallets_gauge.labels(
            wallet_name, wallet_address, token_name, token_address, "balance"
        ).set(token_balance)
        # Amount of FTM in wallet
        wallets_gauge.labels(
            wallet_name, wallet_address, "FTM", "None", "balance"
        ).set(ftm_balance)
        # USD BALANCES
        # FTM balance in dollars
        wallets_gauge.labels(
            wallet_name, wallet_address, "FTM", "none", "usdBalance"
        ).set(ftm_balance * token_prices[wftm_address]['usd'])
        # Token balance in dollars
        if token_prices.get(token_address) is not None:
            wallets_gauge.labels(
                wallet_name, wallet_address, token_name, token_address, "usdBalance"
            ).set(token_balance * token_prices[token_address]['usd'])
        elif token_prices_from_api.get(token_address.lower()) is not None:
            wallets_gauge.labels(
                wallet_name, wallet_address, token_name, token_address, "usdBalance"
            ).set(token_balance * token_prices_from_api[token_address])


def main():
    block_gauge = Gauge(
        name="ftm_blocks",
        documentation="Info about blocks processed",
        labelnames=["chain"],
    )
    wallets_gauge = Gauge(
        name="ftm_wallets",
        documentation="Watched wallet balances",
        labelnames=["walletName", "walletAddress", "token", "tokenAddress", "param"],
    )
    start_http_server(PROMETHEUS_PORT)
    gecko_token_csv = ",".join(COINGECKO_TOKENS.values())
    # Hack to keep container alive, because FTM RPC raises exceptions very often
    while True:
        try:
            for step, block_data in enumerate(chain.new_blocks(height_buffer=1)):
                block_gauge.labels(NETWORK).set(block_data.number)
                log.info(f"Processing {block_data.number}")
                # Get token prices from coingecko
                token_prices = get_token_prices(gecko_token_csv)
                # Get token prices from API. Needed to fetch info about LP tokens in USD
                token_prices_from_api = get_token_prices_in_usd(CHAIN_FANTOM)
                log.warning(token_prices_from_api)
                wallet_balances_by_token = get_wallet_balances_by_token(
                    BADGER_WALLETS, TREASURY_TOKENS,
                )
                # process wallet balances for *one* treasury token
                for token_name, token_address in list(TREASURY_TOKENS.items()):
                    update_wallets_gauge(
                        wallets_gauge,
                        wallet_balances_by_token,
                        token_name,
                        token_address,
                        token_prices,
                        token_prices_from_api,
                    )
        except BlockNotFound:
            continue
