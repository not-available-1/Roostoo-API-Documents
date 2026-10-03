# live_broker.py
import requests
import time
import hmac
import hashlib
from base import BaseBroker, OrderIntent, OrderResult

class LiveBroker(BaseBroker):
    def __init__(self, api_key, secret_key, base_url, max_rpm, tolerance):
        self.api_key = api_key
        self.secret_key = secret_key
        self.base_url = base_url
        self.max_rpm = max_rpm
        self.tolerance_ms = tolerance * 1000
        self.request_times = []

    def _get_timestamp(self):
        return str(int(time.time() * 1000))

    def _rate_limit_check(self):
        now = time.time()
        self.request_times = [t for t in self.request_times if now - t < 60]
        if len(self.request_times) >= self.max_rpm:
            sleep_sec = 61 - (now - self.request_times[0])
            time.sleep(max(0, sleep_sec))
        self.request_times.append(time.time())

    def _sign_request(self, payload:dict):
        payload["timestamp"] = self._get_timestamp()
        sorted_keys = sorted(payload.keys())
        total_params = "&".join(f"{k}={payload[k]}" for k in sorted_keys)
        sig = hmac.new(
            self.secret_key.encode("utf-8"),
            total_params.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()
        headers = {
            "RST-API-KEY": self.api_key,
            "MSG-SIGNATURE": sig
        }
        return headers, total_params

    def get_server_time(self) -> int:
        self._rate_limit_check()
        resp = requests.get(f"{self.base_url}/api/v1/serverTime")
        resp.raise_for_status()
        j = resp.json()
        return j["ServerTime"]

    def get_balance(self):
        self._rate_limit_check()
        headers, payload_str = self._sign_request({})
        resp = requests.get(f"{self.base_url}/api/v1/account", headers=headers, params=payload_str)
        resp.raise_for_status()
        return resp.json()

    def get_exchange_info(self):
        self._rate_limit_check()
        resp = requests.get(f"{self.base_url}/api/v1/exchangeInfo")
        resp.raise_for_status()
        return resp.json()

    def get_ticker(self, pair:str):
        self._rate_limit_check()
        params = {"pair":pair}
        resp = requests.get(f"{self.base_url}/api/v1/ticker/price", params=params)
        resp.raise_for_status()
        return resp.json()

    def place_order(self, intent:OrderIntent) -> OrderResult:
        self._rate_limit_check()
        payload = {
            "pair": intent.pair,
            "side": intent.side.upper(),
            "type": intent.order_type.upper(),
            "quantity": str(intent.quantity)
        }
        if intent.price is not None:
            payload["price"] = str(intent.price)
        headers, data_raw = self._sign_request(payload)
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        resp = requests.post(f"{self.base_url}/api/v1/order", headers=headers, data=data_raw)
        resp.raise_for_status()
        j = resp.json()
        ok = j.get("Success", False)
        oid = j.get("OrderDetail",{}).get("OrderID")
        return OrderResult(success=ok, raw_response=j, order_id=oid)

    def cancel_all_orders(self, pair=None):
        self._rate_limit_check()
        payload = {}
        if pair:
            payload["pair"] = pair
        headers, data_raw = self._sign_request(payload)
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        resp = requests.post(f"{self.base_url}/api/v1/order/cancel", headers=headers, data=data_raw)
        resp.raise_for_status()
        return resp.json()
