import logging
import time
from typing import Any

import requests
from tenacity import Retrying, retry_if_exception, stop_after_attempt, wait_exponential

from config import Settings
from upbit_auth import create_jwt_token


class UpbitAPIError(Exception):
    def __init__(self, status_code: int, payload: dict | None, response_text: str) -> None:
        super().__init__(f"Upbit API error {status_code}: {response_text}")
        self.status_code = status_code
        self.payload = payload
        self.response_text = response_text

    @property
    def retryable(self) -> bool:
        return self.status_code >= 500 or self.status_code == 429


class OrderNotFilledError(Exception):
    pass


def _should_retry(exc: BaseException) -> bool:
    if isinstance(exc, requests.RequestException):
        return True
    if isinstance(exc, UpbitAPIError):
        return exc.retryable
    return False


class UpbitClient:
    def __init__(self, settings: Settings, logger: logging.Logger | None = None) -> None:
        self._settings = settings
        self._logger = logger or logging.getLogger(__name__)
        self._session = requests.Session()

    def _log_rate_limit(self, response: requests.Response) -> None:
        remaining = response.headers.get("remaining-req") or response.headers.get(
            "x-ratelimit-remaining"
        )
        if remaining:
            self._logger.info("Rate limit remaining: %s", remaining)

    def _request(
        self, method: str, path: str, params: dict | None = None, auth: bool = True
    ) -> Any:
        url = f"{self._settings.upbit_base_url}{path}"
        headers = {}
        if auth:
            token = create_jwt_token(
                self._settings.upbit_access_key,
                self._settings.upbit_secret_key,
                params or {},
            )
            headers["Authorization"] = f"Bearer {token}"
        response = self._session.request(
            method=method,
            url=url,
            params=params,
            headers=headers,
            timeout=10,
        )
        self._log_rate_limit(response)
        self._logger.info("Upbit response: %s %s %s", method, path, response.text)
        if response.status_code >= 400:
            raise UpbitAPIError(response.status_code, params, response.text)
        try:
            return response.json()
        except ValueError:
            raise UpbitAPIError(response.status_code, params, response.text)

    def place_market_buy(self, market: str, amount_krw: float) -> dict:
        params = {
            "market": market,
            "side": "bid",
            "price": str(amount_krw),
            "ord_type": "price",
        }
        self._logger.info("Placing market buy: %s", params)
        retrying = Retrying(
            retry=retry_if_exception(_should_retry),
            stop=stop_after_attempt(self._settings.order_retry_attempts),
            wait=wait_exponential(
                multiplier=1,
                min=self._settings.order_retry_wait_min,
                max=self._settings.order_retry_wait_max,
            ),
            reraise=True,
        )
        for attempt in retrying:
            with attempt:
                return self._request("POST", "/v1/orders", params=params, auth=True)
        raise RuntimeError("Order retry loop exited unexpectedly.")

    def place_market_sell(self, market: str, volume: float) -> dict:
        params = {
            "market": market,
            "side": "ask",
            "volume": str(volume),
            "ord_type": "market",
        }
        self._logger.info("Placing market sell: %s", params)
        retrying = Retrying(
            retry=retry_if_exception(_should_retry),
            stop=stop_after_attempt(self._settings.order_retry_attempts),
            wait=wait_exponential(
                multiplier=1,
                min=self._settings.order_retry_wait_min,
                max=self._settings.order_retry_wait_max,
            ),
            reraise=True,
        )
        for attempt in retrying:
            with attempt:
                return self._request("POST", "/v1/orders", params=params, auth=True)
        raise RuntimeError("Order retry loop exited unexpectedly.")

    def cancel_order(self, order_uuid: str) -> dict:
        params = {"uuid": order_uuid}
        self._logger.info("Cancel order: %s", params)
        return self._request("DELETE", "/v1/order", params=params, auth=True)

    def get_order(self, order_uuid: str) -> dict:
        params = {"uuid": order_uuid}
        return self._request("GET", "/v1/order", params=params, auth=True)

    def wait_order_filled(self, order_uuid: str) -> dict:
        deadline = time.time() + self._settings.order_fill_timeout_sec
        while time.time() < deadline:
            order = self.get_order(order_uuid)
            state = order.get("state") or order.get("status")
            if state == "done":
                remaining_volume = float(order.get("remaining_volume") or 0.0)
                if remaining_volume > 0:
                    raise OrderNotFilledError("Order partially filled.")
                return order
            time.sleep(self._settings.order_fill_poll_sec)
        raise OrderNotFilledError("Order not filled within timeout.")

    def get_ticker(self, market: str) -> float:
        params = {"markets": market}
        data = self._request("GET", "/v1/ticker", params=params, auth=False)
        if not data:
            raise UpbitAPIError(500, params, "Empty ticker response.")
        return float(data[0]["trade_price"])

    def get_accounts(self) -> list[dict]:
        return self._request("GET", "/v1/accounts", params=None, auth=True)

    @staticmethod
    def extract_filled_volume(order: dict) -> float:
        if "executed_volume" in order and order["executed_volume"] is not None:
            return float(order["executed_volume"])
        trades = order.get("trades") or []
        total_volume = sum(float(trade["volume"]) for trade in trades)
        return total_volume

    @staticmethod
    def calculate_avg_price(order: dict) -> float:
        trades = order.get("trades") or []
        if trades:
            total = sum(float(trade["price"]) * float(trade["volume"]) for trade in trades)
            volume = sum(float(trade["volume"]) for trade in trades)
            return total / volume if volume else 0.0
        if "avg_price" in order and order["avg_price"] is not None:
            return float(order["avg_price"])
        if "price" in order and order["price"] is not None:
            return float(order["price"])
        return 0.0
