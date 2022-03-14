import os

from brownie import chain
from prometheus_client import Gauge
from prometheus_client import start_http_server  # noqa
from web3.exceptions import BlockNotFound
from web3 import Web3
from scripts.addresses import ADDRESSES_FANTOM
from scripts.addresses import checksum_address_dict
from scripts.data import get_wallet_balances_by_token
from scripts.logconf import log

PROMETHEUS_PORT = 8801

NETWORK = "Fantom"

# get all addresses
ADDRESSES = checksum_address_dict(ADDRESSES_FANTOM)
BADGER_WALLETS = ADDRESSES["badger_wallets"]
TREASURY_TOKENS = ADDRESSES["treasury_tokens"]


WEB3_INSTANCE = Web3(Web3.HTTPProvider(os.environ['FTMNODEURL']))


def update_wallets_gauge(
    wallets_gauge,
    wallet_balances_by_token,
    token_name,
    token_address,
):
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

        eth_balance = float(WEB3_INSTANCE.fromWei(
            WEB3_INSTANCE.eth.getBalance(wallet_address), "ether")
        )

        wallets_gauge.labels(
            wallet_name, wallet_address, token_name, token_address, "balance"
        ).set(token_balance)
        wallets_gauge.labels(
            wallet_name, wallet_address, "ETH", "None", "balance"
        ).set(eth_balance)


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
                log.info(f"Processing {block_data.number}")
                block_gauge.labels(NETWORK).set(block_data.number)
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
                    )
        except BlockNotFound:
            continue
