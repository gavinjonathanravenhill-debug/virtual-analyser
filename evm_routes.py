"""Wallet tracker pages for every EVM chain (/ethereum, /base, /bsc, /robinhood) - same page as /solana."""

from chain_routes import make_chain_bp
from evm_chains import CHAINS

EVM_BLUEPRINTS = {name: make_chain_bp(m, m.start, m.profile, m.is_addr) for name, m in CHAINS.items()}
