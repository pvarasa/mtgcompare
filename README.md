# Compare MTG

This script compares the prices of Magic The Gathering cards in different online shops, finding the cheapest.

## Usage

~~~~~~~
usage: compare [-h] (-c CARD | -f FILE)

Compares prices for Magic The Gathering cards

options:
  -h, --help            show this help message and exit
  -c CARD, --card CARD  name of the card to search for
  -f FILE, --file FILE  file with one card name per line
~~~~~~~

## Features TODO

* More shops (singlestar, toykomtg)
* Support shipping cost configuration
* Parse multiple card conditions for the same search
* Multithreaded search
* Set parsing for card kingdom
* Support alternative versions of cards, foils, etc.
* Allow to choose cards not in stock
* Better readme / setup instructions
* Better output formatting
* Currency preferences
* Some sort of SQLLite or other periodic DB save 