import logging.config

from mtgcompare.compare import LOGGING_CONF, main


if __name__ == "__main__":
    logging.config.fileConfig(LOGGING_CONF)
    main()
