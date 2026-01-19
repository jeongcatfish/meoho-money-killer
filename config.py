import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _get_env_float(key: str, default: float) -> float:
    value = os.getenv(key)
    if value is None or value == "":
        return default
    return float(value)


def _get_env_int(key: str, default: int) -> int:
    value = os.getenv(key)
    if value is None or value == "":
        return default
    return int(value)


def _get_env_bool(key: str, default: bool) -> bool:
    value = os.getenv(key)
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


@dataclass(frozen=True)
class Settings:
    upbit_access_key: str
    upbit_secret_key: str
    upbit_base_url: str
    min_order_krw: float
    order_retry_attempts: int
    order_retry_wait_min: float
    order_retry_wait_max: float
    order_fill_timeout_sec: int
    order_fill_poll_sec: float
    price_poll_sec: float
    price_retry_attempts: int
    price_retry_wait_min: float
    price_retry_wait_max: float
    signal_ttl_sec: int
    log_level: str
    recovery_skip: bool
    recovery_market: str | None
    recovery_tp: float
    recovery_sl: float


def get_settings() -> Settings:
    access_key = os.getenv("UP_BIT_ACCESS_KEY") or os.getenv("UPBIT_ACCESS_KEY")
    secret_key = os.getenv("UP_BIT_SECRET_KEY") or os.getenv("UPBIT_SECRET_KEY")
    if not access_key or not secret_key:
        raise ValueError("Missing UP_BIT_ACCESS_KEY or UP_BIT_SECRET_KEY in environment.")

    recovery_market = os.getenv("RECOVERY_MARKET")
    if recovery_market:
        recovery_market = recovery_market.strip().upper()

    return Settings(
        upbit_access_key=access_key,
        upbit_secret_key=secret_key,
        upbit_base_url=os.getenv("UPBIT_BASE_URL", "https://api.upbit.com"),
        min_order_krw=_get_env_float("MIN_ORDER_KRW", 5000.0),
        order_retry_attempts=_get_env_int("ORDER_RETRY_ATTEMPTS", 3),
        order_retry_wait_min=_get_env_float("ORDER_RETRY_WAIT_MIN", 1.0),
        order_retry_wait_max=_get_env_float("ORDER_RETRY_WAIT_MAX", 4.0),
        order_fill_timeout_sec=_get_env_int("ORDER_FILL_TIMEOUT_SEC", 10),
        order_fill_poll_sec=_get_env_float("ORDER_FILL_POLL_SEC", 1.0),
        price_poll_sec=_get_env_float("PRICE_POLL_SEC", 1.0),
        price_retry_attempts=_get_env_int("PRICE_RETRY_ATTEMPTS", 3),
        price_retry_wait_min=_get_env_float("PRICE_RETRY_WAIT_MIN", 0.5),
        price_retry_wait_max=_get_env_float("PRICE_RETRY_WAIT_MAX", 2.0),
        signal_ttl_sec=_get_env_int("SIGNAL_TTL_SEC", 86400),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        recovery_skip=_get_env_bool("RECOVERY_SKIP", False),
        recovery_market=recovery_market,
        recovery_tp=_get_env_float("RECOVERY_TP", 0.0),
        recovery_sl=_get_env_float("RECOVERY_SL", 0.0),
    )
