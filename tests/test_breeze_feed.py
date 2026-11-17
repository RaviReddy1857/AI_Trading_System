"""BreezeFeed.start() against a fake SDK that reproduces breeze-connect 1.0.69's
get_stock_token_value(): it reads self.interval (only set by subscribe_feeds) and,
on any error, returns the exception object instead of raising."""
import pytest
import logging

from tests.fixture_settings import load_settings
from app.feeds.breeze_feed import BreezeFeed

TOKENS = {"NIFTY": "11", "CNXBAN": "12", "RELIND": "2885", "HDFBAN": "1333", "ICIBAN": "4963",
          "INFTEC": "1594", "TCS": "11536", "INDVIX": "13"}


class FakeBreezeSDK:
    """No 'interval' attribute until subscribe_feeds() runs, like the real SDK.
    Only the data calls BreezeFeed uses are implemented."""

    def __init__(self, tokens=TOKENS):
        self.tokens = tokens
        self.subscribed = []
        self.on_ticks = None

    def ws_connect(self):
        pass

    def get_stock_token_value(self, exchange_code="", stock_code="", product_type="",
                              expiry_date="", strike_price="", right="",
                              get_exchange_quotes=True, get_market_depth=True):
        try:
            if self.interval == "" or self.interval is None:   # AttributeError if never set
                pass
            token = self.tokens.get(stock_code, False)
            if token is False:
                raise Exception("Stock-Code not found.")
            return "4.1!" + token, False
        except Exception as e:
            return e

    def subscribe_feeds(self, stock_token="", interval="", **kw):
        self.interval = interval
        self.subscribed.append(stock_token)
        return {"message": f"Stock {stock_token} subscribed successfully"}


def feed_with(sdk):
    f = BreezeFeed(load_settings().watchlist)
    f._sdk = sdk                     # skip the real login; connect() keeps an existing session
    return f


def test_fake_sdk_reproduces_the_quirk():
    sdk = FakeBreezeSDK()
    out = sdk.get_stock_token_value(exchange_code="NSE", stock_code="NIFTY")
    assert isinstance(out, AttributeError)          # what the old code saw as "unknown code"


def test_start_subscribes_every_watchlist_symbol():
    sdk = FakeBreezeSDK()
    feed = feed_with(sdk)
    feed.start(lambda tick: None)
    watch = load_settings().watchlist
    assert sdk.subscribed == ["4.1!" + TOKENS[i.breeze_code] for i in watch]
    assert {i.symbol for i in feed._by_token.values()} == {i.symbol for i in watch}


def test_real_lookup_failure_logs_the_sdk_message(caplog):
    tokens = {k: v for k, v in TOKENS.items() if k != "INFTEC"}
    sdk = FakeBreezeSDK(tokens)
    feed = feed_with(sdk)
    with caplog.at_level(logging.ERROR, logger="breeze_feed"):
        feed.start(lambda tick: None)
    assert len(sdk.subscribed) == len(TOKENS) - 1
    msg = " ".join(r.getMessage() for r in caplog.records)
    assert "INFY" in msg and "INFTEC" in msg and "Stock-Code not found." in msg
    assert "AttributeError" not in msg              # the quirk no longer shows up


@pytest.mark.usefixtures("fixed_clock")
def test_ticks_from_subscribed_tokens_reach_the_handler():
    sdk = FakeBreezeSDK()
    got = []
    feed_with(sdk).start(got.append)
    sdk.on_ticks({"symbol": "4.1!2885", "quotes": "Quotes Data", "last": 1290.5,
                  "bPrice": 1290.4, "sPrice": 1290.6, "ttq": 100, "close": 1280.0})
    assert [t.symbol for t in got] == ["RELIANCE"]
