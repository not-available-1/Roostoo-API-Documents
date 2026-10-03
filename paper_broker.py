# paper_broker.py
from base import BaseBroker,OrderIntent,OrderResult

class PaperBroker(BaseBroker):
    def __init__(self):
        pass
    def get_server_time(self) -> int:
        raise NotImplementedError("paper broker stub")
    def get_balance(self) -> dict:
        raise NotImplementedError("paper broker stub")
    def place_order(self, intent: OrderIntent) -> OrderResult:
        raise NotImplementedError("paper broker stub")
    def cancel_all_orders(self, pair=None):
        raise NotImplementedError("paper broker stub")
    def get_exchange_info(self):
        raise NotImplementedError("paper broker stub")
    def get_ticker(self,pair):
        raise NotImplementedError("paper broker stub")
