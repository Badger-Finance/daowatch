import os
from typing import Dict
from typing import Set
from typing import Tuple

from brownie import chain
from brownie import interface  # noqa
from prometheus_client import Gauge
from prometheus_client import start_http_server  # noqa
from web3 import Web3
from web3.exceptions import BlockNotFound

from scripts.addresses import ADDRESSES_FANTOM
from scripts.addresses import checksum_address_dict
from scripts.data import get_json_request
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


def parse_lp_tokens_to_underlying() -> Tuple[Dict, Set]:
    """
    Function that maps Sett token to underlying FTM tokens
    """
    lp_token_map = {}
    all_underlying_tokens = set()
    for pool_addr in LP_TOKENS.values():
        pool = interface.Sett(pool_addr)
        amm_interface = interface.UniV2Pair(pool.token())
        reserves = amm_interface.getReserves()
        token_0 = amm_interface.token0()
        token_1 = amm_interface.token1()
        token_0_interface = interface.ERC20(token_0)
        token_1_interface = interface.ERC20(token_1)

        lp_token_map[pool_addr] = {
            token_0: reserves[0] / 10 ** token_0_interface.decimals(),
            token_1: reserves[1] / 10 ** token_1_interface.decimals(),
        }
        all_underlying_tokens.add(token_0)
        all_underlying_tokens.add(token_1)
    return lp_token_map, all_underlying_tokens


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
) -> None:
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
        # Token balance in dollars
        if token_prices.get(token_address) is not None:
            wallets_gauge.labels(
                wallet_name, wallet_address, token_name, token_address, "usdBalance"
            ).set(token_balance * token_prices[token_address]['usd'])
        # FTM balance in dollars
        wallets_gauge.labels(
            wallet_name, wallet_address, "FTM", "none", "usdBalance"
        ).set(ftm_balance * token_prices[wftm_address]['usd'])


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
    # Hack to keep container alive, because FTM RPC raises exceptions very often
    while True:
        try:
            for step, block_data in enumerate(chain.new_blocks(height_buffer=1)):
                block_gauge.labels(NETWORK).set(block_data.number)
                log.info(f"Processing {block_data.number}")
                # Get all AMM underlying tokens
                lp_tokens_underlying_map, all_underlying_tokens = parse_lp_tokens_to_underlying()
                # Get all tokens, including underlying tokens from AMM that needs to be
                # fetched from coingecko
                all_tokens = [*COINGECKO_TOKENS.values(), *all_underlying_tokens]
                token_csv = ",".join(all_tokens)
                # Get token prices from coingecko
                token_prices = get_token_prices(token_csv)

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
                    )
        except BlockNotFound:
            continue
