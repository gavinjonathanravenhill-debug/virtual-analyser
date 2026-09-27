"""Solana wallet tracker page (/solana) - built from the shared chain blueprint."""

import solana_client as sc
from chain_routes import make_chain_bp


def _profile(addr, depth):
    import solana_profiler
    return solana_profiler.profile(addr, depth)


solana_bp = make_chain_bp(sc, sc.start_solana, _profile, lambda a: 32 <= len(a or "") <= 44)
