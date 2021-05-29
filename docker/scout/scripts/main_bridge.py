import datetime
import json
import os
import sys
import time
import warnings

from brownie.network.state import Chain
from prometheus_client import Counter, Gauge, start_http_server
from web3 import Web3
from web3.datastructures import AttributeDict

from scripts.addresses import ADDRESSES_BRIDGE, checksum_address_dict
from scripts.events import process_transaction
from scripts.logconf import log
from scripts.scanner import EventScanner, EventScannerState

PROMETHEUS_PORT = 8801
PROMETHEUS_PORT_FORWARDED = 8802

ETHNODEURL = os.environ["ETHNODEURL"]

ADDRESSES = checksum_address_dict(ADDRESSES_BRIDGE)

BLOCK_START = 12285143
CHAIN_REORG_SAFETY_BLOCKS = 20
POLL_INTERVAL = 150

warnings.simplefilter("ignore")

provider = Web3.HTTPProvider(ETHNODEURL)
# remove the default JSON-RPC retry middleware to enable eth_getLogs block range throttling
provider.middlewares.clear()
w3 = Web3(provider)


class BridgeScannerState(EventScannerState):
    """Store the state of scanned blocks and all events.

    All state is an in-memory dict.
    Simple load/store massive JSON on start up.

    Adapted from: https://web3py.readthedocs.io/en/stable/examples.html?highlight=newfilter
    """

    def __init__(self):
        self.state = None
        self.fname = "bridge-scanner_state.json"
        self.last_save = 0

    def reset(self):
        """Create initial state of nothing scanned."""
        self.state = {"last_scanned_block": 0, "blocks": {}}

    def restore(self):
        """Restore the last scan state from a file"""
        try:
            self.state = json.load(open(self.fname, "rt"))
            log.info(
                f"Restored previous state up to block {self.state['last_scanned_block']}"
            )
        except (IOError, json.decoder.JSONDecodeError):
            log.info("State JSON not found; starting from scratch")
            self.reset()

    def save(self):
        """Save everything we have scanned so far in a file."""
        with open(self.fname, "wt") as f:
            json.dump(self.state, f)
        self.last_save = time.time()

    def get_last_scanned_block(self):
        """The number of the last block we have stored."""
        return self.state["last_scanned_block"]

    def set_intended_end_block(self, block_number):
        """Save the intended end block when the scan is started.

        Dynamic chunk size throttling as implemented from the web3py docs has an issue
        where later chunks can overshoot the intended end block (and actual number of blocks mined).
        This becomes a problem when running a new scan as the new start block can be greater than
        the new calculated end block. Additionally, incorrect start/end blocks may lead to
        transactions being double-counted or missed when processed.

        To address, store the intended last block separately from the last chunk block.
        Then, at the end of a chunk or scan, check whether the last chunk block is larger than
        intended and reset it if it is.
        """
        self.state["intended_last_scanned_block"] = block_number

    def delete_data(self, since_block):
        """Remove potentially reorganised blocks from the scan data."""
        for block_num in range(since_block, self.get_last_scanned_block()):
            if block_num in self.state["blocks"]:
                del self.state["blocks"][block_num]

    def start_chunk(self, block_number, chunk_size):
        pass

    def end_chunk(self, block_number):
        """Save at the end of each block, so we can resume in the case of a crash or CTRL+C"""
        if block_number > self.state["intended_last_scanned_block"]:
            block_number = self.state["intended_last_scanned_block"]

        # next time the scanner is started we will resume from this block
        self.state["last_scanned_block"] = block_number

        # save the database file every minute
        if time.time() - self.last_save > 60:
            self.save()

    def process_event(self, block_when: datetime.datetime, event: AttributeDict) -> str:
        """Record an event in the JSON database."""
        # events are keyed by their transaction hash and log index
        # one transaction may contain multiple events and each one of those gets their own log index

        # event_name = event.event
        # log_index = event.logIndex  # log index within the block
        # transaction_index = event.transactionIndex  # transaction index within the block
        tx_hash = event.transactionHash.hex()
        block_number = event.blockNumber

        # save transaction hash only; will look up tx and tx events separately
        if block_number not in self.state["blocks"]:
            self.state["blocks"][block_number] = []

        if tx_hash not in self.state["blocks"][block_number]:
            self.state["blocks"][block_number].append(tx_hash)

        # return a label that allows us to look up this event later if needed
        return f"{block_number}-{tx_hash}"


def run_scan(scanner, state, block_gauge, token_flow_counter, fees_counter):
    # discard last few blocks in case of chain reorgs
    scanner.delete_potentially_forked_block_data(
        state.get_last_scanned_block() - CHAIN_REORG_SAFETY_BLOCKS
    )

    # scan from last scanned block to latest block
    # min starting block is bridge contract creation block
    start_block = max(
        state.get_last_scanned_block() - CHAIN_REORG_SAFETY_BLOCKS, BLOCK_START
    )
    end_block = scanner.get_suggested_scan_end_block()
    state.set_intended_end_block(end_block)

    log.info(
        f"Scanning for bridge contract transactions from block {start_block} to {end_block}"
    )

    # run the scan
    result, total_chunks_scanned = scanner.scan(start_block, end_block)

    state.save()

    # process new mint/burn transactions
    tx_hashes = []
    for block_number, hash_list in state.state["blocks"].items():
        if (
            int(block_number) >= start_block
            and int(block_number) < end_block - CHAIN_REORG_SAFETY_BLOCKS
        ):
            tx_hashes.extend(hash_list)

    log.info(f"Processing transaction events from {start_block} to {end_block}")
    for tx_hash in tx_hashes:
        process_transaction(w3, tx_hash, block_gauge, token_flow_counter, fees_counter)

    log.info(f"Blocks {start_block} to {end_block} complete.")
    log.info(f"Sleeping for {POLL_INTERVAL} seconds before starting next block chunks")


def main():
    # set up prometheus
    log.info(
        f"Starting Prometheus events server at http://localhost:{PROMETHEUS_PORT_FORWARDED}"
    )

    block_gauge = Gauge(
        name="block_info", documentation="block_info", labelnames=["info"]
    )

    token_flow_counter = Counter(
        name="bridge_token_flow",
        documentation="token,event,direction",
        labelnames=["token", "event", "direction"],
    )
    fees_counter = Counter(
        name="bridge_fees",
        documentation="entity",
        labelnames=["entity"],
    )

    start_http_server(PROMETHEUS_PORT)

    # set up event filters
    # filter bridge contract events
    # bridge_abi = open("interfaces/Bridge.json", "r").read()
    # bridge = w3.eth.contract(address=ADDRESSES["bridge_v2"], abi=bridge_abi)
    # console.log(f"Read Badger BTC Bridge contract at address {ADDRESSES['bridge_v2']}")
    # filters = [
    #     bridge.events.Mint.createFilter(fromBlock=BLOCK_START, toBlock="latest"),
    #     bridge.events.Burn.createFilter(fromBlock=BLOCK_START, toBlock="latest"),
    # ]

    # watch events
    # chain = Chain()
    # console.log(
    #     f"Processing prior events from block {BLOCK_START} to {w3.eth.blockNumber}"
    # )
    # process_prior_events(chain, filters, block_gauge, token_flow_counter, fees_counter)

    # console.log("Listening for new events in latest blocks...")
    # listen_new_events(
    #     chain, filters, block_gauge, token_flow_counter, fees_counter, POLL_INTERVAL
    # )

    # ---------------------------------------------------------------------------------

    # set up scanner and scanner state
    # scan all blocks for Mint/Burn events with `eth_getLog`
    # works with nodes where `eth_newFilter` is not supported

    # erc20_abi = json.loads("interfaces/ERC20.json")
    # erc20 = web3.eth.contract(abi=abi)
    # wbtc = w3.eth.contract(address=ADDRESSES["WBTC"], abi=erc20_abi)
    # renbtc = w3.eth.contract(address=ADDRESSES["renBTC"], abi=erc20_abi)

    log.info(f"Reading Badger BTC Bridge contract at address {ADDRESSES['bridge_v2']}")
    bridge_abi = json.load(open("interfaces/Bridge.json", "r"))
    bridge = w3.eth.contract(address=ADDRESSES["bridge_v2"], abi=bridge_abi)

    state = BridgeScannerState()
    state.restore()

    scanner = EventScanner(
        web3=w3,
        contract=bridge,
        state=state,
        events=[bridge.events.Mint, bridge.events.Burn],
        filters={},
        num_blocks_rescan_for_forks=CHAIN_REORG_SAFETY_BLOCKS,
        max_chunk_scan_size=10000,
    )

    while True:
        run_scan(scanner, state, block_gauge, token_flow_counter, fees_counter)
        time.sleep(POLL_INTERVAL)