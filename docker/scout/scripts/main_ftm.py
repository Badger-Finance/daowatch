from brownie import chain
from prometheus_client import Gauge
from prometheus_client import start_http_server  # noqa
from web3.exceptions import BlockNotFound

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


def main():
    block_gauge = Gauge(
        name="ftm_blocks",
        documentation="Info about blocks processed",
        labelnames=["chain"],
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
                log.info(wallet_balances_by_token)
        except BlockNotFound:
            continue
