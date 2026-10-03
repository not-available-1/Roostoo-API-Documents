# base.py
from dataclasses import dataclass
from typing import Optional, List, Dict

@dataclass
class OrderIntent:
    pair: str
    side: str
    quantity: float
    price: Optional[float]
    order_type: str
    reason: str

@dataclass
class OrderResult:
    success: bool
    raw_response: Dict
    order_id: Optional[int]

class BaseBroker:
    def get_server_time(self) -> int:
        raise NotImplementedError

    def get_balance(self) -> Dict:
        raise NotImplementedError

    def place_order(self, intent: OrderIntent) -> OrderResult:
        raise NotImplementedError

    def cancel_all_orders(self, pair: Optional[str]=None) -> Dict:
        raise NotImplementedError

    def get_exchange_info(self) -> Dict:
        raise NotImplementedError

    def get_ticker(self, pair:str) -> Dict:
        raise NotImplementedError
