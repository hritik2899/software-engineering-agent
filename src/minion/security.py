"""Control-plane API authentication helpers.

Authentication answers who may call the service; AuthorizationPolicy separately
answers which repositories a task may touch. WebSocket authentication mirrors HTTP
authentication before accepting a task event stream.
"""
from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import Header, HTTPException, WebSocket

from minion.config import get_settings


async def require_api_token(
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    expected = get_settings().api_token
    if not expected:
        return
    supplied = ""
    if authorization and authorization.lower().startswith("bearer "):
        supplied = authorization[7:]
    if not secrets.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="invalid API token")


def websocket_authorized(websocket: WebSocket) -> bool:
    expected = get_settings().api_token
    if not expected:
        return True
    authorization = websocket.headers.get("authorization", "")
    supplied = (
        authorization[7:]
        if authorization.lower().startswith("bearer ")
        else websocket.query_params.get("token", "")
    )
    return secrets.compare_digest(supplied, expected)
