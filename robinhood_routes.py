"""Robinhood Chain wallet tracker page (/robinhood) - same page and features as /solana."""

import robinhood_client as rc
from chain_routes import make_chain_bp

robinhood_bp = make_chain_bp(rc, rc.start_robinhood, rc.profile, rc.is_addr)
