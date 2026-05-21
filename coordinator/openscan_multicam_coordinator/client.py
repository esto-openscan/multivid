from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from .config import NodeConfig


@dataclass(frozen=True)
class NodeResult:
    node: NodeConfig
    ok: bool
    status_code: int | None
    data: dict[str, Any] | None
    error: str | None


async def request_node(
    client: httpx.AsyncClient,
    node: NodeConfig,
    method: str,
    path: str,
    json_body: dict[str, Any] | None = None,
) -> NodeResult:
    try:
        response = await client.request(method, f"{node.base_url}{path}", json=json_body)
        response_data = _response_json(response)
        if response.is_success:
            return NodeResult(node=node, ok=True, status_code=response.status_code, data=response_data, error=None)

        detail = response_data.get("detail") if response_data else response.text
        return NodeResult(
            node=node,
            ok=False,
            status_code=response.status_code,
            data=response_data,
            error=str(detail) if detail else f"HTTP {response.status_code}",
        )
    except httpx.RequestError as exc:
        return NodeResult(node=node, ok=False, status_code=None, data=None, error=str(exc))


def _response_json(response: httpx.Response) -> dict[str, Any] | None:
    try:
        data = response.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else None
