import urllib.parse
import logging
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.by import By
from selenium.common.exceptions import TimeoutException
from bs4 import BeautifulSoup

from scrapper import MtgScrapper
from utils import init_selenium, get_fx

TIMEOUT_SECONDS = 10


class CKScrapper(MtgScrapper):
    def __init__(self):
        super().__init__()
        self.driver = init_selenium()
        self.fx = get_fx("jpy")
        self.logger = logging.getLogger('card_kingdom')

    def get_prices(self, card_name):
        def to_num(s): return float(s.replace('$', '').strip())

        try:
            results = self.search(card_name)
        except TimeoutException:
            self.logger.error("Loading took too much time, aborting!")
            return []

        cards = []
        for item in results.find_all("div", class_="itemContentWrapper"):
            card = item.find_next(class_="productDetailTitle").find_next("a").text
            price_usd = to_num(item.find_next(class_="stylePrice").text)
            stock = item.find_next(class_="styleQty")
            if stock:
                condition = item.find_next(class_="cardTypeList").find_next("li", class_="active").text
                if card.lower() == card_name.lower():
                    cards.append({
                        'shop': 'Card Kingdom',
                        'card': card,
                        'price_jpy': price_usd * self.fx,
                        'price_usd': price_usd,
                        'stock': stock.text,
                        'condition': condition
                    })
                    self.logger.info(f"Found with price {price_usd}")
                else:
                    self.logger.info(f"Card {card} doesn't match")
        return cards

    # needs selenium, otherwise throws 403
    def search(self, card_name):
        search_string = f"""
        https://www.cardkingdom.com/catalog/search?search=header&filter%5Bname%5D={urllib.parse.quote(card_name)}
        """
        self.logger.debug(f"Trying search to URL {search_string}")
        self.driver.get(search_string)
        (WebDriverWait(self.driver, TIMEOUT_SECONDS).
         until(EC.presence_of_element_located((By.CLASS_NAME, 'mtg-card'))))
        html = self.driver.page_source
        return BeautifulSoup(html, "html.parser")
