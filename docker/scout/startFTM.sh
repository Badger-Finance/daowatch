#!/bin/sh
brownie networks add Ethereum ftm host=$FTMNODEURL chainid=250
exec brownie run main_ftm --network ftm
