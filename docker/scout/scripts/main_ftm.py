from brownie import chain
from prometheus_client import Gauge
from prometheus_client import start_http_server  # noqa

from scripts.logconf import log

PROMETHEUS_PORT = 8801

NETWORK = "Fantom"


def main():
    block_gauge = Gauge(
        name="ftm_blocks",
        documentation="Info about blocks processed",
        labelnames=["chain"],
    )
    start_http_server(PROMETHEUS_PORT)
    for step, block_data in enumerate(chain.new_blocks(height_buffer=1)):
        log.info(block_data.number)
        block_gauge.labels(NETWORK).set(block_data.number)
