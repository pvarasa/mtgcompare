from abc import ABC, abstractmethod


class MtgScrapper(ABC):
    @abstractmethod
    def get_prices(self, card_name: str) -> list[dict]:
        """
        Get the prices for the card in the shop represented by this scrapper

        :param card_name: string with the name of the card, ex: 'Force of Will'. Case-insensitive.
        :return: List of dicts with the fields (example)
            'shop': 'Hareruya',
            'card': 'Force of Will',
            'set': 'EMA',
            'number': '49',         # normalised collector number, None if unknown
            'variant': '',          # comma-joined scrapers/variants.py tags;
                                    # '' = regular, None = the shop can't tell
            'price_jpy': 14800.0,
            'price_usd': 93.01,
            'stock': 4,             # None if unknown (e.g. aggregator sources)
            'condition': 'NM',
            'link': 'https://www.hareruyamtg.com/en/products/detail/14183?lang=EN'
            # marketplace shops only: 'ship_jpy' (the offer's own shipping)
        """
        ...
