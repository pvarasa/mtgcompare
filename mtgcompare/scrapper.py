from abc import ABC, abstractmethod


class MtgScrapper(ABC):
    @abstractmethod
    def get_prices(self, card_name):
        """
        Get the prices for the card in the shop represented by this scrapper

        :param card_name: string with the name of the card, ex: 'Force of Will'. Case-insensitive.
        :return: List of dicts with the fields (example)
            'shop': 'Hareruya',
            'card': 'Force of Will',
            'set': 'EMA',
            'price_jpy': 14800.0,
            'price_usd': 93.01,
            'stock': 4,             # None if unknown (e.g. aggregator sources)
            'condition': 'NM',
            'link': 'https://www.hareruyamtg.com/en/products/detail/14183?lang=EN'
        """
        pass
