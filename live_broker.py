import time
import hashlib
import hmac
import requests
from base import OrderIntent

class LiveBroker:
    def __init__(self, api_key, secret_key, base_url, max_rpm=30, tolerance=0.005):
        self.api_key = api_key
        self.secret_key = secret_key
        self.base_url = base_url.rstrip("/")
        self.max_rpm = max_rpm
        self.tolerance = tolerance
        self.session = requests.Session()

    def _sign(self, payload: dict):
        sorted_items = sorted(payload.items())
        raw = "&".join([f"{k}={v}" for k, v in sorted_items])
        return hmac.new(
            self.secret_key.encode("utf-8"),
            raw.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()

    def _get(self, path: str, params: dict = None, retry=3):
        """GET 请求允许重试"""
        if params is None:
            params = {}
        url = f"{self.base_url}{path}"
        for attempt in range(retry):
            try:
                resp = self.session.get(
                    url,
                    params=params,
                    headers={"X-API-Key": self.api_key},
                    timeout=15
                )
                try:
                    return resp.json()
                except ValueError:
                    print(f"💥 GET非JSON响应, status={resp.status_code}, text={resp.text[:200]}")
                    return None
            except Exception as e:
                print(f"⚠️ GET请求异常 attempt {attempt+1}/{retry}: {e}")
                time.sleep(1)
        return None

    def _post_no_retry(self, path: str, payload: dict):
        """POST下单接口：禁止自动重试！防止重复下单"""
        url = f"{self.base_url}{path}"
        payload["timestamp"] = int(time.time() * 1000)
        sign = self._sign(payload)
        headers = {
            "X-API-Key": self.api_key,
            "X-API-Sign": sign
        }
        try:
            resp = self.session.post(url, json=payload, headers=headers, timeout=15)
            try:
                return resp.json()
            except ValueError:
                print(f"💥 POST非JSON响应 status={resp.status_code}, text={resp.text[:200]}")
                return {"success": False, "raw_response": resp.text}
        except Exception as e:
            print(f"💥 POST请求异常：{e}")
            return {"success": False, "raw_response": str(e)}

    def get_ticker(self, pair: str):
        """获取行情，路径/v3/ticker，带上毫秒timestamp"""
        ts_ms = int(time.time() * 1000)
        return self._get(
            path="/v3/ticker",
            params={"pair": pair, "timestamp": ts_ms}
        )

    def get_balance(self):
        ts_ms = int(time.time() * 1000)
        return self._get(
            path="/v3/balance",
            params={"timestamp": ts_ms}
        )

    def place_order(self, intent: OrderIntent):
        payload = {
            "pair": intent.pair,
            "side": intent.side,
            "order_type": intent.order_type,
            "price": intent.price,
            "quantity": intent.quantity
        }
        resp_json = self._post_no_retry("/v3/place_order", payload)
        class OrderResp:
            def __init__(self, success, order_id, raw_response):
                self.success = success
                self.order_id = order_id
                self.raw_response = raw_response
        if resp_json is None:
            return OrderResp(False, None, "Empty response")
        success = bool(resp_json.get("Success", False))
        order_id = resp_json.get("orderId")
        raw = str(resp_json)
        return OrderResp(success, order_id, raw)
