import argparse
import datetime
import json
import logging.config
from scrappers.hareruya import HareruyaScrapper
from scrappers.card_kingdom import CKScrapper


def main():
    parser = argparse.ArgumentParser(
        prog='compare',
        description='Compares prices for Magic The Gathering cards')
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('-c', '--card', help='name of the card to search for')
    group.add_argument('-f', '--file', help='file with one card name per line')
    parser.add_argument('-e', '--export', help='file name to export all prices found in json')
    args = parser.parse_args()
    if args.file is None:
        cards = [args.card]
    else:
        with open(args.file) as file:
            cards = [line.rstrip() for line in file]

    all_prices = []
    for card_name in cards:
        logger.info(f"Processing {card_name}")
        scrappers = [HareruyaScrapper(), CKScrapper()]

        prices = []
        for scp in scrappers:
            prices = prices + scp.get_prices(card_name)

        cheapest = min(prices, key=lambda c: c['price_jpy'])
        print(f"For card {card_name} the cheapest is {json.dumps(cheapest, indent=2)}")
        all_prices = all_prices + prices

    if args.export:
        with open(args.export, 'w') as ef:
            now = datetime.datetime.now()
            for p in all_prices:
                p['timestamp'] = now.strftime("%Y/%m/%d, %H:%M:%S")
            json.dump(all_prices, ef)


if __name__ == "__main__":
    logging.config.fileConfig('logging.conf')
    logger = logging.getLogger('compare')
    main()
