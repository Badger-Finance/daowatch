import datetime
import itertools
import os
import re
import warnings
from collections import defaultdict
from typing import Dict
from typing import List
from typing import Optional

from brownie import chain
from brownie import interface  # noqa
from prometheus_client import Gauge
from prometheus_client import start_http_server  # noqa
from web3 import Web3

from scripts.addresses import ADDRESSES_ETH
from scripts.addresses import checksum_address_dict
from scripts.data import get_apr_from_convex
from scripts.data import get_badgertree_data
from scripts.data import get_digg_data
from scripts.data import get_ibbtc_data
from scripts.data import get_lp_data
from scripts.data import get_peak_composition_data
from scripts.data import get_peak_value_data
from scripts.data import get_sett_data
from scripts.data import get_token_by_address
from scripts.data import get_token_interfaces
from scripts.data import get_token_prices
from scripts.data import get_treasury_token_addr_by_pool_name
from scripts.data import get_yvault_data
from scripts.logconf import console
from scripts.logconf import log

warnings.simplefilter("ignore")

PROMETHEUS_PORT = 8801

NETWORK = "ETH"
ETHNODEURL = os.environ["ETHNODEURL"]
w3 = Web3(Web3.HTTPProvider(ETHNODEURL))

NATIVE_TOKENS = ["BADGER", "DIGG", "bBADGER", "bDIGG"]

# get all addresses
ADDRESSES = checksum_address_dict(ADDRESSES_ETH)
badger_wallets = ADDRESSES["badger_wallets"]
treasury_tokens = ADDRESSES["treasury_tokens"]
lp_tokens = ADDRESSES["lp_tokens"]
crv_pools = ADDRESSES["crv_pools"]
crv_stablecoin_pools = ADDRESSES["crv_stablecoin_pools"]
crv_meta_pools = ADDRESSES["crv_meta_pools"]
crv_3_pools = ADDRESSES["crv_3_pools"]
crv_factory_pools = ADDRESSES["crv_factory_pools"]
sett_vaults = ADDRESSES["sett_vaults"]
yearn_vaults = ADDRESSES["yearn_vaults"]
custodians = ADDRESSES["custodians"]
oracles = ADDRESSES["oracles"]
peaks = ADDRESSES["peaks"]

BALANCER_BPTS = ADDRESSES['balancer_bpt']
BALANCER = ADDRESSES['balancer']
BALANCER_VAULT = ADDRESSES['balancer_misc']['balancer_vault']
CVX_ADDRESSES = {
    **ADDRESSES['crv_pools'],
    **ADDRESSES['crv_3_pools'],
    **ADDRESSES['crv_stablecoin_pools'],
}

CRV_POOLS_WITH_CRV_STABLECOIN_POOLS = {**crv_pools, **crv_stablecoin_pools}

AMM_BALANCER = "balancer"
AMM_CURVE = "curve"
AMM_SUSHI = "sushi"
peak_sett_composition = {
    "badgerPeak": {
        "bcrvRenBTC": sett_vaults["bcrvRenBTC"],
        "bcrvSBTC": sett_vaults["bcrvSBTC"],
        "bcrvTBTC": sett_vaults["bcrvTBTC"],
    },
    "byvWbtcPeak": {"byvWBTC": yearn_vaults["byvWBTC"]},
}

usd_prices_by_token_address = {}


def update_price_gauge(
    coingecko_price_gauge,
    treasury_tokens,
    token_prices,
    token_name,
    token_address,
    countertoken_csv,
    network,
):
    lp_prefixes = ("uni", "slp", "b", "crv", "cake")

    # BSC token_names are coingecko_names, so lookup token symbol from treasury_tokens
    fetched_name = get_token_by_address(treasury_tokens, token_address)

    try:
        if not fetched_name.startswith(lp_prefixes):
            log.info(
                f"Processing CoinGecko price for [bold]{fetched_name}: {token_address} ..."
            )

            if network == "ETH":
                price_key = token_address.lower()
            elif network == "BSC":
                price_key = token_name
            else:
                log.error(
                    "Specify network in order to appropriately process CoinGecko prices"
                )

            price = token_prices[price_key]

            for countertoken in countertoken_csv.split(","):
                coingecko_price_gauge.labels(
                    "ETH"
                    if fetched_name == "WETH"
                    else "BNB"
                    if fetched_name == "WBNB"
                    else fetched_name,
                    token_address,
                    countertoken,
                ).set(price[countertoken])

            usd_prices_by_token_address[token_address] = price["usd"]
        else:
            log.info(
                f"Skipping CoinGecko price for [bold]{fetched_name}: {token_address} ..."
            )
    except Exception as e:
        log.warning(
            f"Error getting CoinGecko price for [bold]{fetched_name}: {token_address}"
        )
        log.warning(e)
        # log.warning(token_prices, token_name, fetched_name, token_address)


def update_digg_gauge(digg_gauge, digg_prices, slpWbtcDigg, uniWbtcDigg):
    # process digg oracle price
    digg_oracle_price = digg_prices.describe()
    for param, value in digg_oracle_price.items():
        log.info(
            f"Processing Oracle param [bold]{param}[/] for [bold]DIGG: {digg_prices.oracle.address} ..."
        )
        digg_gauge.labels(param).set(value)

    # process digg AMM prices
    log.info(f"Processing SushiSwap price for [bold]DIGG: {uniWbtcDigg.address} ...")
    digg_uni_price = (uniWbtcDigg.getReserves()[0] / 1e8) / (
        uniWbtcDigg.getReserves()[1] / 1e9
    )
    digg_gauge.labels("uniswap").set(digg_uni_price)

    log.info(f"Processing Uniswap price for [bold]DIGG: {slpWbtcDigg.address} ...")
    digg_sushi_price = (slpWbtcDigg.getReserves()[0] / 1e8) / (
        slpWbtcDigg.getReserves()[1] / 1e9
    )
    digg_gauge.labels("sushiswap").set(digg_sushi_price)


def update_lp_tokens_gauge(
        lp_tokens_gauge: Gauge, amm_gauge: Gauge, lp_tokens: Dict, lp_token, token_interfaces: List
) -> None:
    lp_name = lp_token.name
    lp_address = lp_tokens[lp_name]

    log.info(f"Processing lpToken reserves for [bold]{lp_name}...")

    lp_info = lp_token.describe()
    lp_scale = 10 ** lp_info["decimals"]
    lp_supply = lp_info["totalSupply"]

    token0_address = lp_info["token0"]
    token1_address = lp_info["token1"]
    token0 = token_interfaces[token0_address]
    token1 = token_interfaces[token1_address]
    token0_reserve = lp_info["token0_reserve"]
    token1_reserve = lp_info["token1_reserve"]
    token0_scale = 10 ** token0.decimals()
    token1_scale = 10 ** token1.decimals()

    underlying_0_supply = token0_reserve / token0_scale
    underlying_1_supply = token1_reserve / token1_scale
    total_lp_token_supply = lp_supply / lp_scale
    lp_tokens_gauge.labels(lp_name, lp_address, f"{token0.symbol()}_supply").set(
        underlying_0_supply
    )
    amm_gauge.labels(
        lp_name, lp_address,
        token0.symbol(), token0.address,
        AMM_SUSHI, "totalSupply"
    ).set(underlying_0_supply)
    lp_tokens_gauge.labels(lp_name, lp_address, f"{token1.symbol()}_supply").set(
        underlying_1_supply
    )
    amm_gauge.labels(
        lp_name, lp_address,
        token1.symbol(), token1.address,
        AMM_SUSHI, "totalSupply"
    ).set(underlying_1_supply)
    lp_tokens_gauge.labels(lp_name, lp_address, "totalLpTokenSupply").set(total_lp_token_supply)
    amm_gauge.labels(
        lp_name, lp_address,
        None, None,
        AMM_SUSHI, "totalLpTokenSupply"
    ).set(total_lp_token_supply)
    try:
        price = (
            ((token1_reserve / token1_scale) / (lp_supply / lp_scale))
            * usd_prices_by_token_address[token1_address]
            * 2
        )
        usd_prices_by_token_address[lp_address] = price
        lp_tokens_gauge.labels(lp_name, "usdPricePerShare", lp_address).set(price)
        amm_gauge.labels(
            lp_name, lp_address,
            None, None,
            AMM_SUSHI, "usdPricePerShare"
        ).set(price)
    except Exception as e:
        log.warning(f"Error calculating USD price for lpToken [bold]{lp_name}")
        log.warning(e)


def update_crv_3_tokens_guage(gauge: Gauge, amm_gauge: Gauge, pool_name, pool_address) -> None:
    log.info(f"Processing crvToken data for [bold]{pool_name}...")
    pool_token_interface = interface.ERC20(treasury_tokens[pool_name])
    pool_token_symbol = pool_token_interface.symbol()
    pool = interface.tricryptoPool(pool_address)
    pool_divisor = 10 ** pool_token_interface.decimals()
    total_supply = pool_token_interface.totalSupply() / pool_divisor
    balance = pool_token_interface.balance() / pool_divisor

    tokenlist = []
    usd_balance = 0
    for i in range(3):
        tokenlist.append(interface.ERC20(pool.coins(i)))
    for underlying_token in tokenlist:
        underlying_price_per_share = (
            (underlying_token.balanceOf(pool_address) / 10 ** underlying_token.decimals())
            / (pool_token_interface.totalSupply() / pool_divisor)
        )
        underlying_balance = (
            underlying_token.balanceOf(pool_address) / 10 ** underlying_token.decimals()
        )
        underlying_token_symbol = underlying_token.symbol()
        gauge.labels(
            pool_name,
            pool_token_interface.address,
            f"{underlying_token.symbol()}_per_share"
        ).set(underlying_price_per_share)
        amm_gauge.labels(
            pool_token_symbol,
            pool_token_interface.address,
            underlying_token_symbol,
            underlying_token.address,
            AMM_CURVE,
            "pricePerShare",
        ).set(underlying_price_per_share)
        gauge.labels(
            pool_name,
            pool_token_interface.address,
            f"{underlying_token.symbol()}_balance"
        ).set(underlying_balance)
        amm_gauge.labels(
            pool_token_symbol,
            pool_token_interface.address,
            underlying_token_symbol,
            underlying_token.address,
            AMM_CURVE,
            "balance",
        ).set(underlying_balance)
        usd_balance += (
            (underlying_token.balanceOf(pool_address) / 10 ** underlying_token.decimals())
            * usd_prices_by_token_address[underlying_token.address]
        )
    usd_price = usd_balance / total_supply
    gauge.labels(pool_name, pool_token_interface.address, "usdPricePerShare").set(usd_price)
    amm_gauge.labels(
        pool_token_symbol,
        pool_token_interface.address,
        None, None,
        AMM_CURVE,
        "usdPricePerShare",
    ).set(usd_price)
    gauge.labels(pool_name, pool_token_interface.address, "totalSupply").set(total_supply)
    amm_gauge.labels(
        pool_token_symbol,
        pool_token_interface.address,
        None, None,
        AMM_CURVE,
        "totalSupply",
    ).set(total_supply)
    gauge.labels(pool_name, pool_token_interface.address, "balance").set(balance)
    amm_gauge.labels(
        pool_token_symbol,
        pool_token_interface.address,
        None, None,
        AMM_CURVE,
        "balance",
    ).set(balance)
    usd_prices_by_token_address[pool_token_interface.address] = usd_price


def update_crv_tokens_gauge(
        crv_tokens_gauge: Gauge, amm_gauge: Gauge,
        pool_name: str, pool_address: str) -> None:
    log.info(f"Processing crvToken data for [bold]{pool_name}...")

    pool_token_name = pool_name
    pool_token_address = treasury_tokens[pool_token_name]

    token_address = get_treasury_token_addr_by_pool_name(pool_name, treasury_tokens)
    # Fallback to WBTC
    if not token_address:
        token_address = treasury_tokens["WBTC"]
    if pool_name in ["crvRenBTC", "crvSBTC"]:
        crv_interface = interface.CRVswap(pool_address)
    else:
        crv_interface = interface.CRVswapUnderlying(pool_address)

    token_interface = interface.ERC20(treasury_tokens[pool_name])
    virtual_price = crv_interface.get_virtual_price() / 1e18
    usd_price = virtual_price * usd_prices_by_token_address[token_address]
    log.warning(f"CRV Token price: {pool_name}: virtual price {virtual_price} "
                f"* usd token price {usd_prices_by_token_address[token_address]} == {usd_price}USD")
    crv_tokens_gauge.labels(pool_name, token_address, "pricePerShare").set(virtual_price)
    amm_gauge.labels(
        token_interface.symbol(), token_interface.address, None, None, AMM_CURVE, "pricePerShare"
    ).set(virtual_price)
    crv_tokens_gauge.labels(pool_name, token_address, "usdPricePerShare").set(usd_price)
    amm_gauge.labels(
        token_interface.symbol(), token_interface.address,
        None, None, AMM_CURVE, "usdPricePerShare"
    ).set(usd_price)
    crv_tokens_gauge.labels(
        pool_name, token_address, "totalSupply").set(token_interface.totalSupply() / 1e18)
    amm_gauge.labels(
        token_interface.symbol(), token_interface.address,
        None, None, AMM_CURVE, "totalSupply"
    )

    usd_prices_by_token_address[pool_token_address] = usd_price


def update_crv_factory_tokens_gauge(
        crv_tokens_gauge: Gauge, amm_gauge: Gauge, pool_name: str, pool_address: str) -> None:
    log.info(f"Processing crvToken data for [bold] factory pool: {pool_name}...")
    pool_token_address = treasury_tokens[pool_name]
    crv_factory_interface = interface.CRVfactoryPool(pool_address)
    crv_token_interface = interface.ERC20(pool_token_address)
    crv_token_symbol = crv_token_interface.symbol()
    token_address = get_treasury_token_addr_by_pool_name(pool_name, treasury_tokens)

    pool_divisor = 10 ** crv_token_interface.decimals()
    total_supply = crv_token_interface.totalSupply() / pool_divisor

    virtual_price = crv_factory_interface.get_virtual_price() / 1e18
    crv_tokens_gauge.labels(pool_name, token_address, "pricePerShare").set(virtual_price)
    amm_gauge.labels(
        crv_token_symbol, crv_token_interface.address,
        None, None,
        AMM_CURVE, "pricePerShare"
    ).set(virtual_price)
    crv_tokens_gauge.labels(pool_name, token_address, "totalSupply").set(total_supply)
    amm_gauge.labels(
        crv_token_symbol, crv_token_interface.address,
        None, None,
        AMM_CURVE, "totalSupply"
    ).set(total_supply)
    token_list = []
    for i in itertools.count(start=0):
        try:
            token_list.append(interface.ERC20(crv_factory_interface.coins(i)))
        except ValueError:
            break
    for underlying_token in token_list:
        token_balance = underlying_token.balanceOf(pool_address) / 10 ** underlying_token.decimals()
        crv_tokens_gauge.labels(
            pool_name, underlying_token.address,
            f"{underlying_token.symbol()}_balance").set(token_balance)
        if underlying_token.address != treasury_tokens['pxCVX']:  # TODO remove when CG pricing
            usd_balance = (
                usd_prices_by_token_address[underlying_token.address] *
                underlying_token.balanceOf(pool_address) / 10 ** underlying_token.decimals()
            )
            amm_gauge.labels(
                crv_token_symbol, crv_token_interface.address,
                underlying_token.symbol(), underlying_token.address,
                AMM_CURVE, "balance"
            ).set(token_balance)
            crv_tokens_gauge.labels(
                pool_name, token_address,
                f"{underlying_token.symbol()}_usd_balance").set(usd_balance)
            amm_gauge.labels(
                crv_token_symbol, crv_token_interface.address,
                underlying_token.symbol(), underlying_token.address,
                AMM_CURVE, "usdBalance"
            ).set(usd_balance)


def update_crv_meta_tokens_gauge(
        crv_tokens_gauge: Gauge, amm_gauge: Gauge,
        pool_name: str, pool_address: str) -> None:
    log.info(f"Processing crvToken data for [bold] meta pool: {pool_name}...")
    pool_token_address = treasury_tokens[pool_name]
    crv_meta_interface = interface.crvTransfer(pool_address)
    token_address = get_treasury_token_addr_by_pool_name(pool_name, treasury_tokens)
    # Fallback to WBTC
    if not token_address:
        token_address = treasury_tokens["WBTC"]
    token_interface = interface.ERC20(treasury_tokens[pool_name])
    pool_token_symbol = token_interface.symbol()
    pool_divisor = 10 ** crv_meta_interface.decimals()
    total_supply = crv_meta_interface.totalSupply() / pool_divisor
    balance = crv_meta_interface.balance() / pool_divisor

    virtual_price = crv_meta_interface.get_virtual_price() / 1e18
    usd_price = virtual_price * usd_prices_by_token_address[token_address]
    crv_tokens_gauge.labels(pool_name, token_address, "pricePerShare").set(virtual_price)
    crv_tokens_gauge.labels(pool_name, token_address, "usdPricePerShare").set(usd_price)
    crv_tokens_gauge.labels(pool_name, token_address, "balance").set(balance)
    crv_tokens_gauge.labels(pool_name, token_address, "totalSupply").set(total_supply)
    amm_gauge.labels(
        pool_token_symbol, token_interface.address,
        None, None, AMM_CURVE, "pricePerShare").set(virtual_price)
    amm_gauge.labels(
        pool_token_symbol, token_interface.address,
        None, None, AMM_CURVE, "usdPricePerShare").set(usd_price)
    amm_gauge.labels(
        pool_token_symbol, token_interface.address,
        None, None, AMM_CURVE, "balance").set(balance)
    amm_gauge.labels(
        pool_token_symbol, token_interface.address,
        None, None, AMM_CURVE, "totalSupply").set(total_supply)

    token_list = []
    for i in itertools.count(start=0):
        try:
            token_list.append(interface.ERC20(crv_meta_interface.coins(i)))
        except ValueError:
            break
    # Set balances for underlying tokens
    for underlying_token in token_list:
        token_balance = underlying_token.balanceOf(pool_address) / 10 ** underlying_token.decimals()
        crv_tokens_gauge.labels(
            pool_name, underlying_token.address,
            f"{underlying_token.symbol()}_balance").set(token_balance)
        amm_gauge.labels(
            pool_token_symbol, token_interface.address,
            underlying_token.symbol(), underlying_token.address,
            AMM_CURVE, "balance"
        ).set(token_balance)
    usd_prices_by_token_address[pool_token_address] = usd_price


def update_sett_gauge(sett_gauge, sett, sett_vaults, treasury_tokens):
    sett_name = sett.name
    sett_address = sett_vaults[sett_name]
    sett_token_name = re.sub("^b", "", sett.name)  # bveCVX
    sett_token_name = re.sub("^gravi", "", sett_token_name)  # remBADGER and DIGG
    sett_token_name = re.sub("^rem", "", sett_token_name)  # graviAURA
    sett_token_name = re.sub("harvest", "", sett_token_name)  # harvest sett
    sett_token_name = re.sub("bbveCVX-CVX-f", "CVX", sett_token_name)  # bveCVX LP
    sett_token_name = re.sub("^ve", "", sett_token_name)  # bveCVX
    try:
        sett_token_address = treasury_tokens[sett_token_name]
    except KeyError:
        log.warning(f"Cannot find {sett_token_name} in treasury tokens. Skipping")

    sett_info = sett.describe()

    log.info(f"Processing Sett data for [bold]{sett_name}")

    for param, value in sett_info.items():
        sett_gauge.labels(sett_name, sett_address, sett_token_name, param).set(value)

    try:
        usd_prices_by_token_address[sett_address] = (
            sett_info["pricePerShare"] * usd_prices_by_token_address[sett_token_address]
        )
        sett_gauge.labels(sett_name, sett_address, sett_token_name, "usdBalance").set(
            usd_prices_by_token_address[sett_address] * sett_info["totalSupply"]
        )
    except Exception as e:
        log.warning(f"Error calculating USD price for Sett [bold]{sett_name}")
        log.warning(e)


def update_crv_setts_roi_gauge(
    sett_gauge: Gauge, sett_data: List[Dict]
):
    if not sett_data:
        return
    for sett_name, sett_address in CVX_ADDRESSES.items():
        for cvx_item in sett_data:
            if Web3.toChecksumAddress(cvx_item['swap']) == sett_address:
                log.info(f"Updated AUM CVX pool {sett_name}")
                sett_gauge.labels(
                    sett_name, sett_address, sett_name, "cvxAUM"
                ).set(cvx_item['tvl'])


def update_sett_yvault_gauge(sett_gauge, yvault, yearn_vaults, treasury_tokens):
    yvault_name = yvault.name
    yvault_address = yearn_vaults[yvault_name]
    yvault_token_name = yvault_name[3:]
    yvault_token_address = treasury_tokens[yvault_token_name]

    yvault_info = yvault.describe()

    log.info(f"Processing Yearn Sett[bold] {yvault_name}...")

    for param, value in yvault_info.items():
        sett_gauge.labels(yvault_name, yvault_address, yvault_token_name, param).set(
            value
        )

    try:
        usd_prices_by_token_address[yvault_address] = (
            yvault_info["pricePerShare"]
            * usd_prices_by_token_address[yvault_token_address]
        )
        sett_gauge.labels(
            yvault_name,
            yvault_address,
            yvault_token_name,
            "usdBalance",
        ).set(usd_prices_by_token_address[yvault_address] * yvault_info["balance"])
    except Exception as e:
        log.warning(f"Error calculating USD price of Yearn Sett [bold]{yvault_name}")
        log.info(e)


def update_ibbtc_gauge(ibbtc_gauge, ibbtc):
    log.info("Processing ibBTC data")

    ibbtc_info = ibbtc.describe()

    for param, value in ibbtc_info.items():
        ibbtc_gauge.labels(param).set(value)


def update_peak_value_gauge(peak_value_gauge, peak, peaks):
    peak_name = peak.name
    peak_address = peaks[peak_name]

    peak_info = peak.describe()

    log.info(
        f"Processing Peak portfolio value for [bold]{peak_name} ..."
    )

    for param, value in peak_info.items():
        peak_value_gauge.labels(peak_name, peak_address, param).set(value)


def update_peak_composition_gauge(peak_composition_gauge, peak_sett_underlying):
    sett_name = peak_sett_underlying.sett_name
    sett_address = peak_sett_underlying.sett_address
    peak_name = peak_sett_underlying.peak_name
    peak_address = peak_sett_underlying.peak_address

    peak_sett_underlying_info = peak_sett_underlying.describe()

    log.info(
        f"Processing composition for Peak [bold]{peak_name}[/], Sett [bold]{sett_name}"
    )

    for param, value in peak_sett_underlying_info.items():
        peak_composition_gauge.labels(
            peak_name, peak_address, sett_name, sett_address, param
        ).set(value)

    peak_composition_gauge.labels(
        peak_name, peak_address, sett_name, sett_address, "usdBalance"
    ).set(
        usd_prices_by_token_address[sett_address] * peak_sett_underlying_info["balance"]
    )


WALLETS_TOKEN_BALANCES = defaultdict(dict)


def update_wallets_gauge(
    wallets_gauge,
    wallet_balances_by_token,
    token_address,
    step: Optional[int] = 0
):
    wallet_info = wallet_balances_by_token[token_address]
    dont_skip = step % 10 == 0
    log.info(f"Processing wallet balances for [bold]{wallet_info['name']}: {token_address} ...")
    for wallet_name, wallet_address in wallet_info['wallets'].items():
        if WALLETS_TOKEN_BALANCES.get(wallet_address, {}).get(token_address) == 0 and not dont_skip:
            continue
        token = interface.ERC20(wallet_info['token'])
        token_balance = token.balanceOf(wallet_address) / 10 ** token.decimals()

        WALLETS_TOKEN_BALANCES[wallet_address][token_address] = token_balance
        eth_name = "ETH"
        eth_balance = float(w3.fromWei(w3.eth.getBalance(wallet_address), "ether"))

        wallets_gauge.labels(
            wallet_name, wallet_address, wallet_info['name'], token_address, "balance"
        ).set(token_balance)
        wallets_gauge.labels(
            wallet_name, wallet_address, eth_name, "None", "balance"
        ).set(eth_balance)

        try:
            wallets_gauge.labels(
                wallet_name, wallet_address, wallet_info['name'], token_address, "usdBalance"
            ).set(token_balance * usd_prices_by_token_address[token_address])
            wallets_gauge.labels(
                wallet_name, wallet_address, eth_name, "none", "usdBalance"
            ).set(eth_balance * usd_prices_by_token_address[treasury_tokens[f"W{eth_name}"]])
        except Exception as e:
            log.warning(
                f"Error calculating USD balances for wallet "
                f"[bold]{wallet_name} token [bold]{wallet_info['name']}"
            )
            log.info(e)


def update_rewards_gauge(rewards_gauge, badgertree, badger, digg, treasury_tokens):
    log.info(f"Calculating Badgertree reward holdings ...")

    badger_rewards = badger.balanceOf(badgertree.address) / 1e18
    digg_rewards = digg.balanceOf(badgertree.address) / 1e9

    rewards_gauge.labels("BADGER", treasury_tokens["BADGER"]).set(badger_rewards)
    rewards_gauge.labels("DIGG", treasury_tokens["DIGG"]).set(digg_rewards)


def update_cycle_gauge(cycle_gauge, last_cycle_unixtime):
    for param, value in last_cycle_unixtime.items():
        log.info(f"Processing Badgertree [bold]{param} ...")
        cycle_gauge.labels(param).set(value)


def update_aura_info_gauge(aura_gauge: Gauge, aura_token, aura_bal_token) -> None:
    # Amount of aura in Airdrop contract
    aura_gauge.labels("aura_airdrop_amount").set(
        aura_token.balanceOf(ADDRESSES['AuraMerkleDrop']) / 1e18
    )
    # Add vlAURA amount
    aura_locker = interface.AuraLocker(ADDRESSES['AuraLocker'])
    aura_gauge.labels("AURA_locked").set(aura_locker.lockedSupply() / 1e18)
    # Add auraBAL total supply
    aura_gauge.labels("AURA_locker_totalSupply").set(aura_locker.totalSupply() / 1e18)
    aura_gauge.labels("AURA_balance").set(aura_token.balanceOf(ADDRESSES['AuraLocker']) / 1e18)
    aura_gauge.labels("AURA_minted_per_bal_earned").set( ((500 - ((aura_token.totalSupply()/1e18) - 50000000) / 100000) * 2.5 + 700) / 500 )
    aura_gauge.labels("AURA_totalSupply").set(aura_token.totalSupply() / 1e18) ## TODO remove when in coingecko
    aura_gauge.labels("auraBAL_total_supply").set(aura_bal_token.totalSupply() / 1e18) ## TODO remove when in coingecko and update Aura locking dashboard


def update_convex_info_gauge(convex_gauge: Gauge, convex_token, cvxcrv_token) -> None:
    # Add vlAURA amount
    convex_locker = interface.ConvexLocker(ADDRESSES['convexLocker'])
    epoch = convex_locker.epochCount()
    px_cvx = interface.ConvexLocker(treasury_tokens['pxCVX'])
    convex_gauge.labels("CVX_locked").set(convex_locker.lockedSupply() / 1e18)
    convex_gauge.labels("CVX_locker_totalSupply").set(convex_locker.totalSupply() / 1e18)
    # Add auraBAL total supply
    convex_gauge.labels("cvxCRV_total_supply").set(cvxcrv_token.totalSupply() / 1e18) ## TODO remove when in coingecko and update convex locking dashboard
    convex_gauge.labels("CVX_total_supply").set(convex_token.totalSupply() / 1e18) ## TODO remove when in coingecko and update convex locking dashboard
    convex_gauge.labels("CVX_balance").set(convex_token.balanceOf(ADDRESSES['convexLocker']) / 1e18)
    convex_gauge.labels("pxCVX_total_supply").set(px_cvx.totalSupply() / 1e18) ## TODO remove when in coingecko and update convex locking dashboard
    convex_gauge.labels("CVX_epoch_total_supply_this_week").set(convex_locker.totalSupplyAtEpoch(epoch - 2) / 1e18)
    convex_gauge.labels("CVX_epoch_total_supply_next_week").set(convex_locker.totalSupplyAtEpoch(epoch - 1) / 1e18)
    convex_gauge.labels("pxCVX_total_supply").set(px_cvx.totalSupply() / 1e18)
    convex_gauge.labels(
        "CVX_epoch_total_supply_this_week").set(
        convex_locker.totalSupplyAtEpoch(epoch - 2) / 1e18
    )
    convex_gauge.labels(
        "CVX_epoch_total_supply_next_week"
    ).set(
        convex_locker.totalSupplyAtEpoch(epoch - 1) / 1e18
    )


def update_bpt_gauge(bpt_gauge: Gauge, amm_gauge: Gauge, bpt_name: str, bpt_address: str) -> None:
    log.info(f"Processing BPT info for BPT: {bpt_name}")
    # Main Balancer Vault contract that acts as pool controller
    balancer_vault_contract = interface.BalancerVault(BALANCER_VAULT)

    bpt_contract = interface.BPTWeighed(bpt_address)
    bpt_total_supply = bpt_contract.totalSupply() / 10 ** bpt_contract.decimals()
    bpt_gauge.labels(
        bpt_name,
        bpt_name,
        bpt_address,
        "total_supply"
    ).set(bpt_total_supply)
    amm_gauge.labels(
        bpt_name,
        bpt_address,
        None,
        None,
        AMM_BALANCER,
        "totalSupply"
    ).set(bpt_total_supply)
    tokens, balances, _ = balancer_vault_contract.getPoolTokens(bpt_contract.getPoolId())
    bpt_cummulative_price = 0
    # Iterate through all BPT underlying tokens and set params. Also adds up prices in $ for each
    # underlying token under BPT
    for index, token_address in enumerate(tokens):
        log.warning(f"Underlying token for BPT {bpt_name}: {token_address}")
        token_address_checksummed = Web3.toChecksumAddress(token_address)
        bpt_underlying_token = interface.ERC20(token_address_checksummed)
        token_symbol = bpt_underlying_token.symbol()
        token_balance = balances[index] / 10 ** bpt_underlying_token.decimals()
        bpt_gauge.labels(
            bpt_name,
            token_symbol,
            token_address_checksummed,
            "token_balance"
        ).set(token_balance)
        amm_gauge.labels(
            bpt_name,
            bpt_address,
            token_symbol,
            token_address_checksummed,
            AMM_BALANCER,
            "balance"
        ).set(token_balance)

        token_price_data = get_token_prices(token_address_checksummed, "usd", NETWORK)
        token_price = token_price_data[token_address_checksummed.lower()].get('usd', 0)
        # TODO: For now we skip stable pools price calculation. Impl in future if needed
        if not re.search('stable', bpt_name, re.IGNORECASE):
            bpt_cummulative_price += token_price * token_balance
        usd_prices_by_token_address[token_address] = token_price
        amm_gauge.labels(
            bpt_name,
            bpt_address,
            token_symbol,
            token_address_checksummed,
            AMM_BALANCER,
            "price"
        ).set(usd_prices_by_token_address[token_address])
        usd_token_balance = balances[index] / (
                10 ** bpt_underlying_token.decimals()) * usd_prices_by_token_address[token_address]
        amm_gauge.labels(
            bpt_name,
            bpt_address,
            token_symbol,
            token_address_checksummed,
            AMM_BALANCER,
            "usdBalance"
        ).set(usd_token_balance)
    # For stable pools we don't calc the price, hence cannot update price gauges for stable BPT
    if bpt_cummulative_price != 0:
        bpt_price = bpt_cummulative_price / bpt_total_supply
        bpt_gauge.labels(
            bpt_name,
            bpt_name,
            bpt_address,
            "mcap"
        ).set(bpt_cummulative_price)
        bpt_gauge.labels(
            bpt_name,
            bpt_name,
            bpt_address,
            "usd_balance"
        ).set(bpt_cummulative_price)
        bpt_gauge.labels(
            bpt_name,
            bpt_name,
            bpt_address,
            "price"
        ).set(bpt_price)
        amm_gauge.labels(
            bpt_name,
            bpt_address,
            None,
            None,
            AMM_BALANCER,
            "mcap"
        ).set(bpt_cummulative_price)
        amm_gauge.labels(
            bpt_name,
            bpt_address,
            None,
            None,
            AMM_BALANCER,
            "price"
        ).set(bpt_price)


def update_vebal_gauge(vebal_gauge: Gauge) -> None:
    log.info("Updating veBAL gauge")
    bpt_contract = interface.BPTWeighed(BALANCER_BPTS['B_80_BAL_20_WETH'])
    vebal_token_contract = interface.ERC20(BALANCER['veBAL'])
    # Amount of BPTs locked in veBAL
    vebal_gauge.labels("locked").set(
        bpt_contract.balanceOf(BALANCER['veBAL']) / 10 ** bpt_contract.decimals()
    )
    # Each veBAL == 1 vote
    vebal_gauge.labels("total_votes").set(
        vebal_token_contract.totalSupply() / 10 ** vebal_token_contract.decimals()
    )


def main():
    # set up prometheus
    log.info(
        f"Starting Prometheus scout-collector server at http://localhost:{PROMETHEUS_PORT}"
    )
    block_gauge = Gauge(
        name="blocks",
        documentation="Info about blocks processed",
    )
    bpt_gauge = Gauge(
        name="BPT",
        documentation="Info about balancer pool tokens",
        labelnames=["bpt", "token", "tokenAddress", "param"]
    )
    vebal_gauge = Gauge(
        name="veBAL",
        documentation="Info about veBAL token",
        labelnames=["param"]
    )
    coingecko_price_gauge = Gauge(
        name="coingecko_prices",
        documentation="Token price data from Coingecko",
        labelnames=["token", "tokenAddress", "countercurrency"],
    )
    digg_gauge = Gauge(
        name="digg_price",
        documentation="Digg price data from oracle and AMMs",
        labelnames=["value"],
    )
    lp_tokens_gauge = Gauge(
        name="lptokens",
        documentation="LP token data",
        labelnames=["token", "tokenAddress", "param"],
    )
    crv_tokens_gauge = Gauge(
        name="crvtokens",
        documentation="CRV token data",
        labelnames=["token", "tokenAddress", "param"],
    )
    crv_nonbtc_tokens_gauge = Gauge(
        name="nonbtcCrvTokens",
        documentation="CRV Tricrypto data",
        labelnames=["token", "tokenAddress", "param"],
    )
    sett_gauge = Gauge(
        name="sett",
        documentation="Badger Sett vaults data",
        labelnames=["sett", "tokenAddress", "token", "param"],
    )
    wallets_gauge = Gauge(
        name="wallets",
        documentation="Watched wallet balances",
        labelnames=["walletName", "walletAddress", "token", "tokenAddress", "param"],
    )
    rewards_gauge = Gauge(
        name="rewards",
        documentation="Badgertree reward holdings",
        labelnames=["token", "tokenAddress"],
    )
    cycle_gauge = Gauge(
        name="badgertree",
        documentation="Badgertree reward timestamp",
        labelnames=["lastCycleUnixtime"],
    )
    ibbtc_gauge = Gauge(
        name="ibBTC", documentation="Interest-bearing BTC", labelnames=["param"]
    )
    peak_value_gauge = Gauge(
        name="peak_value",
        documentation="Peak portfolio value",
        labelnames=["peakName", "peakAddress", "param"],
    )
    peak_composition_gauge = Gauge(
        name="peak_composition",
        documentation="Peak Sett composition",
        labelnames=["peakName", "peakAddress", "token", "tokenAddress", "param"],
    )
    aura_gauge = Gauge(
        name="aura_locker",
        documentation="Aura token data",
        labelnames=["param"],
    )
    convex_gauge = Gauge(
        name="convex_locker",
        documentation="convex token data",
        labelnames=["param"],
    )
    new_lp_token_gauge = Gauge(
        name="amm",
        documentation="Info about different AMM pools and their tokens",
        labelnames=["lptoken", "lpTokenAddress", "token", "tokenAddress", "amm", "param"]
    )
    start_http_server(PROMETHEUS_PORT)
    str_treasury_tokens = "".join(
        [
            f"\n\t[bold]{token_name}: {token_address}"
            for token_name, token_address in treasury_tokens.items()
        ]
    )

    log.info(f"Loading ERC20 interfaces for treasury tokens ... {str_treasury_tokens}")
    token_interfaces = get_token_interfaces(treasury_tokens)
    badger = token_interfaces[treasury_tokens["BADGER"]]
    digg = token_interfaces[treasury_tokens["DIGG"]]

    wallet_balances_by_token = {token_address: dict(
        wallets=badger_wallets, name=token_name,
        token=token_address,
    ) for token_name, token_address in treasury_tokens.items()}

    lp_data = get_lp_data(lp_tokens)

    sett_data = get_sett_data(sett_vaults)
    yvault_data = get_yvault_data(yearn_vaults)

    digg_prices = get_digg_data(oracles["oracle"], oracles["oracle_provider"])

    slp_wbtc_digg = interface.Pair(lp_tokens["slpWbtcDigg"])
    uni_wbtc_digg = interface.Pair(lp_tokens["uniWbtcDigg"])

    badgertree = interface.Badgertree(badger_wallets["badgertree"])
    badgertree_cycles = get_badgertree_data(badgertree)

    ibbtc_data = get_ibbtc_data(interface.ibBTC(treasury_tokens["ibBTC"]))
    peak_value_data = get_peak_value_data(peaks)
    peak_sett_underlyings = get_peak_composition_data(peaks, peak_sett_composition)

    # coingecko price query variables
    token_csv = ",".join(treasury_tokens.values())
    countertoken_csv = "usd"

    # scan new blocks and update gauges
    for step, block in enumerate(chain.new_blocks(height_buffer=1)):
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        console.print()
        console.rule(
            title=f"[green]{timestamp} step number {step}, block number {block.number}"
        )

        block_gauge.set(block.number)

        # process token prices
        token_prices = get_token_prices(token_csv, countertoken_csv, NETWORK)
        for token_name, token_address in treasury_tokens.items():
            update_price_gauge(
                coingecko_price_gauge,
                treasury_tokens,
                token_prices,
                token_name,
                token_address,
                countertoken_csv,
                NETWORK,
            )
        # process digg oracle prices
        update_digg_gauge(digg_gauge, digg_prices, slp_wbtc_digg, uni_wbtc_digg)

        # process rewards balances
        update_rewards_gauge(rewards_gauge, badgertree, badger, digg, treasury_tokens)

        # process badgertree cycles
        last_cycle_unixtime = badgertree_cycles.describe()
        update_cycle_gauge(cycle_gauge, last_cycle_unixtime)
        # process lp data
        for lp_token in lp_data:
            update_lp_tokens_gauge(
                lp_tokens_gauge, new_lp_token_gauge, lp_tokens, lp_token, token_interfaces
            )
        # General Aura data (lockers)
        aura_token = token_interfaces[treasury_tokens['AURA']]
        aura_bal_token = token_interfaces[treasury_tokens['auraBAL']]
        update_aura_info_gauge(aura_gauge, aura_token, aura_bal_token)

        # General Covex data (lockers)
        convex_token = token_interfaces[treasury_tokens['CVX']]
        cvxcrv_token = token_interfaces[treasury_tokens['cvxCRV']]
        update_convex_info_gauge(convex_gauge, convex_token, cvxcrv_token)

        # Process veBAL token data
        update_vebal_gauge(vebal_gauge)
        # Process balancer bpt data
        for bpt_name, bpt_address in BALANCER_BPTS.items():
            update_bpt_gauge(bpt_gauge, new_lp_token_gauge, bpt_name, bpt_address)
        # process curve pool data
        for pool_name, pool_address in CRV_POOLS_WITH_CRV_STABLECOIN_POOLS.items():
            update_crv_tokens_gauge(crv_tokens_gauge, new_lp_token_gauge, pool_name, pool_address)

        for meta_pool_name, meta_pool_address in crv_meta_pools.items():
            update_crv_meta_tokens_gauge(
                crv_tokens_gauge, new_lp_token_gauge, meta_pool_name, meta_pool_address
            )

        for factory_pool_name, factory_pool_address in crv_factory_pools.items():
            update_crv_factory_tokens_gauge(
                crv_tokens_gauge, new_lp_token_gauge, factory_pool_name, factory_pool_address
            )

        # process 3crv data data
        for pool_name, pool_address in crv_3_pools.items():
            update_crv_3_tokens_guage(
                crv_nonbtc_tokens_gauge, new_lp_token_gauge, pool_name, pool_address
            )

        for sett in sett_data:
            update_sett_gauge(sett_gauge, sett, sett_vaults, treasury_tokens)

        crvcvx_pools_data = get_apr_from_convex()
        if crvcvx_pools_data:
            update_crv_setts_roi_gauge(sett_gauge, crvcvx_pools_data)

        for yvault in yvault_data:
            update_sett_yvault_gauge(sett_gauge, yvault, yearn_vaults, treasury_tokens)

        # process ibBTC share price
        update_ibbtc_gauge(ibbtc_gauge, ibbtc_data)

        # process peak portfolio value
        for peak in peak_value_data:
            update_peak_value_gauge(peak_value_gauge, peak, peaks)

        # process peak sett underlying balance, share price
        for underlying in peak_sett_underlyings:
            update_peak_composition_gauge(peak_composition_gauge, underlying)

        # Get basic balances for all wallets on first run
        for token_name, token_address in treasury_tokens.items():
            update_wallets_gauge(
                wallets_gauge,
                wallet_balances_by_token,
                token_address,
                step,
            )

