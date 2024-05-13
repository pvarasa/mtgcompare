from abc import ABC, abstractmethod


class MtgScrapper(ABC):
    @abstractmethod
    def get_prices(self, card_name):
        """
        Get the prices for the card in the shop represented by this scrapper

        :param card_name: string with the name of the card, ex: 'Force of Will'. Case-insensitive.
        :return: Dictionary with the following fields (example)
            'shop': 'Card Kingdom',
            'card': 'Force of Will',
            'price_jpy': 10389,
            'price_usd': 71.42,
            'stock': 4,
            'condition': 'NM',
            'link': 'https://mtg_shop.com/product/2938472948'
        """
        pass
