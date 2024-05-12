import yfinance as yf
from selenium import webdriver
from selenium.webdriver import Chrome


def init_selenium():
    options = webdriver.ChromeOptions()
    options.add_argument("--headless")  # Set the Chrome webdriver to run in headless mode for scalability
    options.page_load_strategy = "none"
    driver = Chrome(options=options)
    driver.implicitly_wait(5)
    return driver


def get_fx(ccy: str) -> float:
    return yf.Ticker(f"{ccy.upper()}=X").info["previousClose"]
