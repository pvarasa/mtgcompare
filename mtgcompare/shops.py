import re

from .scrappers.hareruya import HareruyaScrapper
from .scrappers.scryfall import ScryfallScrapper
from .scrappers.singlestar import SingleStarScrapper
from .scrappers.tokyomtg import TokyoMtgScrapper

SHOP_FLAGS = {
    "Hareruya": "\U0001F1EF\U0001F1F5",
    "SingleStar": "\U0001F1EF\U0001F1F5",
    "TokyoMTG": "\U0001F1EF\U0001F1F5",
    "TCGPlayer (Scryfall)": "\U0001F1FA\U0001F1F8",
}

# Default per-order shipping in JPY, assuming buyer is in Japan.
# JP shops: domestic tracked rates (ネコポス / クリックポスト equivalent, ~¥385).
# TCGPlayer: international to Japan via USPS First-Class Intl (~$8 ≈ ¥1,200).
SHIPPING_JPY: dict[str, int] = {
    "Hareruya":             385,
    "SingleStar":           385,
    "TokyoMTG":             385,
    "TCGPlayer (Scryfall)": 1200,
}


def shop_slug(name: str) -> str:
    """URL/form-field-safe identifier for a shop name."""
    return re.sub(r'[^a-z0-9]+', '_', name.lower()).strip('_')


def build_scrapers(fx: float) -> list:
    return [
        HareruyaScrapper(fx=fx),
        ScryfallScrapper(fx=fx),
        SingleStarScrapper(fx=fx),
        TokyoMtgScrapper(fx=fx),
    ]


def collect_prices(card_name: str, fx: float, logger=None) -> list[dict]:
    """Fetch and concatenate all shop results for a single card."""
    results: list[dict] = []
    for scraper in build_scrapers(fx):
        try:
            results.extend(scraper.get_prices(card_name))
        except Exception as exc:
            if logger is not None:
                logger.error(
                    "Scraper %s failed for %r: %s",
                    scraper.__class__.__name__,
                    card_name,
                    exc,
                )
    return results
