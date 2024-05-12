import argparse
import logging.config
from scrappers.hareruya import HareruyaScrapper
from scrappers.card_kingdom import CKScrapper


def main():
    parser = argparse.ArgumentParser(
        prog='compare',
        description='Compares prices for Magic The Gathering cards')
    parser.add_argument('card_name')
    args = parser.parse_args()
    card_name = args.card_name

    scrappers = [HareruyaScrapper(), CKScrapper()]

    prices = []
    for scp in scrappers:
        prices = prices + scp.get_prices(card_name)

    cheapest = min(prices, key=lambda c: c['price_jpy'])
    print(f"Cheapest is {cheapest}")


if __name__ == "__main__":
    logging.config.fileConfig('logging.conf')
    main()
