"""
EVM chains tracked with the shared engine in evm_chain.py.

Each chain gets its own page (/ethereum, /base, /bsc, /robinhood) with the same features as
/solana: key wallet moves, wallet list, lookup, token screener, signal journal, price zones.

Add wallets from each page's Wallets tab, or per chain with a Railway variable such as
BASE_EXTRA_WALLETS="0xabc...:Label". Override RPCs with e.g. BSC_RPC_URL (comma-separated list).
"""

import importlib.util
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))

# Etherscan-labelled Wintermute wallets. Market makers reuse the same EOA on every EVM chain,
# so they are watched everywhere (cheap - it's one log filter).
_WM = [
    {"address": "0x0000006daea1723962647b7e189d311d757fb793", "label": "Wintermute 1 (trading bot)",
     "group": "Wintermute", "alert": False,
     "note": "Etherscan 'Wintermute 1', ~1M txs - fills customer/RFQ flow, so single trades aren't views. No alerts."},
    {"address": "0xf8191d98ae98d2f7abdfb63a9b0b812b93c873aa", "label": "Wintermute 4 (treasury)",
     "group": "Wintermute", "alert": True, "min_usd": 250000,
     "note": "Etherscan 'Wintermute 4' - largest ETH balance; exchange deposits/withdrawals are the signal"},
    {"address": "0xce84449a8ebec019ac110a4b6662e55d0fd9f228", "label": "Wintermute 5",
     "group": "Wintermute", "alert": True, "min_usd": 100000, "note": "Etherscan 'Wintermute 5'"},
    {"address": "0xdbf5e9c5206d0db70a90108bf936da60221dc080", "label": "Wintermute 0xdbf…080 (allocations)",
     "group": "Wintermute", "alert": True, "min_usd": 100000,
     "watch_new_tokens": True,   # first-ever receipt of a token = likely a new market-making deal
     "funder": True,             # it funded Wintermute 4 - new wallets it funds get tracked automatically
     "note": "Etherscan 'Wintermute: 0xdbf...080' - receives project market-making allocations; funded Wintermute 4"},
    {"address": "0x000002cba8dfb0a86a47a415592835e17fac080a", "label": "Wintermute 2",
     "group": "Wintermute", "alert": False, "note": "Etherscan 'Wintermute 2' - mostly dormant"},
]
# B2C2 - Robinhood's other big crypto market maker (12% of its transaction revenue, Q1 2025 10-Q)
_B2C2 = [
    {"address": "0xc333e80ef2dec2805f239e3f1e810612d294f771", "label": "B2C2 Group 1",
     "group": "B2C2", "alert": True, "min_usd": 250000,
     "note": "Etherscan 'B2C2 Group 1' - ~170k txs, holdings on 14 chains (largest on BSC)"},
]
_WM_ETH_ONLY = [
    {"address": "0x4f3a120e72c76c22ae802d129f599bfdbc31cb81", "label": "Wintermute multisig",
     "group": "Wintermute", "alert": True, "min_usd": 250000,
     "note": "Safe multisig on Ethereum only (same address on Optimism belongs to the 2022 exploiter)"},
]

# Well-known exchange hot wallets (Etherscan labels). Exchange deposits = likely sells.
_ETH_EXCHANGES = {
    "0x28c6c06298d514db089934071355e5743bf21d60": "Binance 14",
    "0xf977814e90da44bfa03b6295a0616a897441acec": "Binance 8",
    "0xdfd5293d8e347dfe59e90efd55b2956a1343963d": "Binance 16",
    "0x56eddb7aa87536c09ccc2793473599fd21a8b17f": "Binance 17",
    "0x9696f59e4d72e237be84ffd425dcad154bf96976": "Binance 18",
    "0x71660c4005ba85c37ccec55d0c4493e66fe775d3": "Coinbase 1",
    "0x503828976d22510aad0201ac7ec88293211d23da": "Coinbase 2",
    "0xa9d1e08c7793af67e9d92fe308d5697fb81d3e43": "Coinbase 10",
    "0x6cc5f688a315f3dc28a7781717a9a798a59fda7b": "OKX",
    "0x2910543af39aba0cd09dbb2d50200b3e800a63d2": "Kraken",
    "0xf89d7b9c864f589bbf53a82105107622b35eaa40": "Bybit",
    "0x40b38765696e3d5d8d9d834d8aad4bb6e418e489": "Robinhood",
}
_BSC_EXCHANGES = {
    "0xf977814e90da44bfa03b6295a0616a897441acec": "Binance 8",
    "0x8894e0a0c962cb723c1976a4421c95949be2d4e3": "Binance hot 6",
}

CONFIGS = {
    "ethereum": {
        "chain": "ethereum", "name": "Ethereum", "native": "ETH", "wrapped": ["WETH", "ETH"],
        "rpcs": [u for u in [os.getenv("ETH_HTTP_URL", "").strip()] if u] + [
            "https://ethereum-rpc.publicnode.com", "https://eth.llamarpc.com", "https://eth.drpc.org",
            "https://1rpc.io/eth"],
        "block_seconds": 12, "poll": 12, "max_range": 2000, "start_hours": 3, "gap": 0.05,
        "gt": "eth", "bubblemaps": "eth", "dexscreener": "ethereum", "coingecko_native": "ethereum",
        "explorer": "https://etherscan.io", "explorer_name": "Etherscan",
        "default_wallets": _WM + _WM_ETH_ONLY + _B2C2, "default_exchanges": _ETH_EXCHANGES,
    },
    "base": {
        "chain": "base", "name": "Base", "native": "ETH", "wrapped": ["WETH", "ETH"],
        "rpcs": ["https://base-rpc.publicnode.com", "https://mainnet.base.org", "https://base.llamarpc.com",
                 "https://base.drpc.org"],
        "block_seconds": 2, "poll": 10, "max_range": 5000, "start_hours": 3, "gap": 0.05,
        "gt": "base", "bubblemaps": "base", "dexscreener": "base", "coingecko_native": "ethereum",
        "explorer": "https://basescan.org", "explorer_name": "Basescan",
        "default_wallets": _WM + _B2C2, "default_exchanges": {},
    },
    "bsc": {
        "chain": "bsc", "name": "BNB Chain", "native": "BNB", "wrapped": ["WBNB", "BNB"],
        "rpcs": ["https://bsc-rpc.publicnode.com", "https://bsc-dataseed.bnbchain.org", "https://bsc.drpc.org",
                 "https://1rpc.io/bnb"],
        "block_seconds": 0.75, "poll": 10, "max_range": 5000, "start_hours": 3, "gap": 0.05,
        "gt": "bsc", "bubblemaps": "bsc", "dexscreener": "bsc", "coingecko_native": "binancecoin",
        "explorer": "https://bscscan.com", "explorer_name": "BscScan",
        "default_wallets": _WM + _B2C2, "default_exchanges": _BSC_EXCHANGES,
    },
    "robinhood": {
        "chain": "robinhood", "name": "Robinhood Chain", "native": "ETH", "wrapped": ["WETH", "ETH"],
        "rpcs": ["https://rpc.mainnet.chain.robinhood.com"],
        "block_seconds": 0.1, "poll": 20, "max_range": 1_500_000, "start_hours": 6, "gap": 0.06,
        "gt": "robinhood", "bubblemaps": "robinhood", "dexscreener": "robinhood", "coingecko_native": "ethereum",
        "explorer": "https://robinhoodchain.blockscout.com", "explorer_name": "Blockscout",
        "holders_suffix": "?tab=holders", "portfolio_suffix": "?tab=tokens",
        "default_wallets": _WM + _B2C2, "default_exchanges": {},
    },
}

# EVM_CHAINS="ethereum,base" to run only some of them
ENABLED = [c.strip() for c in os.getenv("EVM_CHAINS", ",".join(CONFIGS)).split(",") if c.strip() in CONFIGS]


def load_chain(name):
    """Load evm_chain.py as its own module for one chain (own wallets, tracker and caches)."""
    mod_name = f"{name}_client"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, os.path.join(_HERE, "evm_chain.py"))
    mod = importlib.util.module_from_spec(spec)
    mod.CFG = CONFIGS[name]
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


CHAINS = {name: load_chain(name) for name in ENABLED}
