import hashlib
import uuid
from urllib.parse import urlencode

import jwt


def _build_query_hash(params: dict | list[tuple[str, str]]) -> str:
    # Upbit validates the hash against the exact encoded query order.
    query_string = urlencode(params, doseq=True).encode()
    return hashlib.sha512(query_string).hexdigest()


def create_jwt_token(
    access_key: str, secret_key: str, params: dict | list[tuple[str, str]] | None = None
) -> str:
    payload: dict[str, str] = {
        "access_key": access_key,
        "nonce": str(uuid.uuid4()),
    }
    if params:
        payload["query_hash"] = _build_query_hash(params)
        payload["query_hash_alg"] = "SHA512"
    return jwt.encode(payload, secret_key)
