import re
import urllib.parse
import logging
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.by import By
from selenium.common.exceptions import TimeoutException
from bs4 import BeautifulSoup

from scrapper import MtgScrapper
from utils import init_selenium, get_fx

TIMEOUT_SECONDS = 20


class HareruyaScrapper(MtgScrapper):
    def __init__(self):
        super().__init__()
        self.driver = init_selenium()
        self.fx = get_fx("jpy")
        self.logger = logging.getLogger('hareruya')

    def get_prices(self, card_name):
        def to_num(s):
            return int(s.replace('¥', '').replace(',', '').strip())

        try:
            results = self.search(card_name)
        except TimeoutException:
            self.logger.error("Loading took too much time, aborting!")
            return []

        cards = []
        for item in results.find_all("div", class_="itemData"):
            name = item.find_next(class_="itemName")
            match = re.search(r"《(.*)》.*?\[(.*?)]", name.text)
            if match:
                card, mtg_set = match.group(1), match.group(2)
                if card.lower() == card_name.lower():
                    price = to_num(item.find_next(class_="itemDetail__price").text)
                    price_in_usd = '{:.2f}'.format(price / self.fx)
                    sstr = item.find_next(class_="itemDetail__stock").text
                    match = re.search(r"【(.*?) Stock:(\d+)】", sstr)
                    condition, stock = match.group(1), int(match.group(2))
                    if stock > 0:
                        cards.append({
                            'shop': 'Hareruya',
                            'card': card,
                            'set': mtg_set,
                            'price_jpy': price,
                            'price_usd': price_in_usd,
                            'stock': stock,
                            'condition': condition,
                            'link': self.link(name['href'])
                        })
                        self.logger.info(f"Found {card} from set {mtg_set} valued at ¥{price} (${price_in_usd})")
                else:
                    self.logger.info(f"Card {card} doesn't match")
        return cards

    def link(self, href):
        return f"https://www.hareruyamtg.com{href.strip()}"

    # too much Javascript magic for basic request, needs selenium
    def search(self, card_name):
        search_string = f"""
        https://www.hareruyamtg.com/en/products/search?\
        sort=&\
        order=&\
        cardId=&\
        product={urllib.parse.quote(card_name)}&\
        category=&\
        cardset=&\
        colorsType=0&\
        cardtypesType=0&\
        subtype=&\
        format=&\
        foilFlg%5B%5D=0&\
        illustrator=&\
        language%5B%5D=2&\
        stock=1\
        """.strip()
        self.logger.debug(f"Trying search to URL {search_string}")
        self.driver.get(search_string)
        (WebDriverWait(self.driver, TIMEOUT_SECONDS).
         until(EC.presence_of_element_located((By.CLASS_NAME, 'itemData'))))
        html = self.driver.page_source
        return BeautifulSoup(html, "html.parser")
