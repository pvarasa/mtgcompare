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


def build_scrapers(fx: float) -> list:
    return [
        HareruyaScrapper(fx=fx),
        ScryfallScrapper(fx=fx),
        SingleStarScrapper(fx=fx),
        TokyoMtgScrapper(fx=fx),
    ]
