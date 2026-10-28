"""Response framing and byte limits shared by forwarding and WebSocket delivery."""

import json
from base64 import b64encode

from .models import ProxyResponse

MAX_FRAME_BYTES = 16 * 1024 * 1024
MAX_HEADER_BYTES = 128 * 1024
MAX_HEADER_COUNT = 256
MAX_STREAM_CHUNK_BYTES = 64 * 1024
MAX_STREAM_BYTES = 64 * 1024 * 1024
STREAM_WINDOW = 4


def _response_frame(response: ProxyResponse) -> dict[str, object]:
    return {
        "id": response.delivery_id,
        "type": "response",
        "response_status": response.status,
        "response_headers": response.headers,
        "response_body_base64": "",
    }


def response_body_limit(response: ProxyResponse) -> int:
    """Return the raw body budget, or a negative value if headers cannot fit."""
    if (
        len(response.headers) > MAX_HEADER_COUNT
        or sum(
            len(name.encode()) + len(value.encode())
            for name, value in response.headers.items()
        )
        > MAX_HEADER_BYTES
    ):
        return -1
    overhead = len(json.dumps(_response_frame(response)).encode())
    # Base64 requires four bytes for every three input bytes, rounded up.
    # The gateway's WebSocket reader rejects messages equal to its size limit.
    return ((MAX_FRAME_BYTES - 1 - overhead) // 4) * 3


def encode_response(response: ProxyResponse) -> str:
    """Encode a response, replacing oversized payloads with a delivery error."""
    error_code = response.error_code
    if error_code is None and len(response.body) > response_body_limit(response):
        error_code = "response_too_large"
    if error_code is not None:
        return json.dumps(
            {"id": response.delivery_id, "type": "error", "error_code": error_code}
        )
    frame = _response_frame(response)
    frame["response_body_base64"] = b64encode(response.body).decode()
    return json.dumps(frame)
