# markets_utils.py
import re
import difflib
from typing import Dict, Tuple, Optional, List
from x10.perpetual.trading_client import PerpetualTradingClient
from x10.perpetual.markets import MarketModel
from x10.errors import X10Error

async def build_markets_cache(trading_client: PerpetualTradingClient) -> Dict[str, MarketModel]:
    """Télécharge la liste des marchés et renvoie un dict {name -> MarketModel}."""
    res = await trading_client.markets_info.get_markets()
    if not res or not res.data:
        raise X10Error("Impossible de récupérer la liste des marchés (réponse vide).")
    return {m.name: m for m in res.data}

def _normalize(name: str) -> str:
    """Normalise un symbole (casse, séparateurs, etc.)."""
    name = name.strip().upper()
    name = re.sub(r'[\s_]+', '-', name)            # espaces/underscores -> '-'
    name = re.sub(r'[^A-Z0-9\-]', '', name)        # enlève caractères exotiques
    return name

async def verify_market(
    trading_client: PerpetualTradingClient,
    market_name: str,
    cache: Optional[Dict[str, MarketModel]] = None,
) -> Tuple[Optional[MarketModel], List[str]]:
    """
    Renvoie (MarketModel, suggestions). Si le marché n'existe pas: (None, suggestions).
    """
    if cache is None:
        cache = await build_markets_cache(trading_client)

    # 1) essai direct
    if market_name in cache:
        return cache[market_name], []

    # 2) essai normalisé
    norm_map = {_normalize(k): k for k in cache.keys()}
    norm = _normalize(market_name)
    if norm in norm_map:
        return cache[norm_map[norm]], []

    # 3) suggestions proches
    candidates = list(norm_map.keys())
    suggestions_norm = difflib.get_close_matches(norm, candidates, n=5, cutoff=0.6)
    suggestions = [norm_map[s] for s in suggestions_norm]
    return None, suggestions

async def ensure_market(
    trading_client: PerpetualTradingClient,
    market_name: str,
    cache: Optional[Dict[str, MarketModel]] = None,
) -> MarketModel:
    """
    Valide qu’un marché existe. Renvoie MarketModel ou lève une erreur avec suggestions.
    """
    market, suggestions = await verify_market(trading_client, market_name, cache)
    if market:
        return market
    hint = f" (voulez-vous dire : {', '.join(suggestions)})" if suggestions else ""
    raise X10Error(f"Marché inconnu: '{market_name}'{hint}")
