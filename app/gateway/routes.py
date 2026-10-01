import json
import time

from fastapi import APIRouter, Depends, Header
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from app.gateway.execution import CompletionRun, GatewayStreamingResponse
from app.gateway.quota import authenticate, reserve, visible_models
from app.gateway.schemas import ChatRequest
from app.providers.base import ProviderError

router = APIRouter(prefix="/v1", tags=["client API"])


def client_key(authorization: str | None = Header(default=None)):
    if not authorization:
        raise ProviderError("invalid_api_key", 401, usage_unknown=False)
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise ProviderError("invalid_api_key", 401, usage_unknown=False)
    return authenticate(token)


@router.get("/models")
def models(key_id: str = Depends(client_key)):
    return {
        "object": "list",
        "data": [
            {"id": alias, "object": "model", "created": 0, "owned_by": "myais"}
            for alias in visible_models(key_id)
        ],
    }


def sse_data(value):
    return "data: " + json.dumps(value, ensure_ascii=False) + "\n\n"


@router.post("/chat/completions")
async def chat(request: ChatRequest, key_id: str = Depends(client_key)):
    admission = await run_in_threadpool(reserve, key_id, request)
    run = CompletionRun(admission)
    await run.open()  # Upstream admission errors retain a normal HTTP error status.
    base = {
        "id": "chatcmpl-" + admission.id,
        "created": int(time.time()),
        "model": admission.route.alias,
    }
    if request.stream:

        async def sse():
            try:
                async for event in run.events():
                    chunk = {**base, "object": "chat.completion.chunk"}
                    include_usage = request.stream_options and request.stream_options.include_usage
                    if event.usage is not None:
                        if not include_usage:
                            continue
                        chunk.update(choices=[], usage=event.usage)
                    else:
                        chunk["choices"] = [
                            {"index": 0, "delta": event.delta, "finish_reason": event.finish_reason}
                        ]
                        if include_usage:
                            chunk["usage"] = None
                    yield sse_data(chunk)
            except ProviderError as exc:
                yield sse_data(exc.as_dict())
            except SQLAlchemyError:
                yield sse_data(ProviderError("database_unavailable", 503).as_dict())
            finally:
                await run.finish()
            yield "data: [DONE]\n\n"

        response = GatewayStreamingResponse(sse(), run)
        response.headers["X-Request-ID"] = admission.id
        return response
    text, refusals, tools, usage, reason = [], [], {}, None, None
    try:
        async for event in run.events():
            delta = event.delta
            if delta.get("content"):
                text.append(delta["content"])
            if delta.get("refusal"):
                refusals.append(delta["refusal"])
            for tool in delta.get("tool_calls") or []:
                index = tool["index"]
                stored = tools.setdefault(
                    index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
                )
                if tool.get("id"):
                    stored["id"] = tool["id"]
                for field in ("name", "arguments"):
                    stored["function"][field] += tool.get("function", {}).get(field, "")
            if event.finish_reason:
                reason = event.finish_reason
            if event.usage is not None:
                usage = event.usage
    finally:
        await run.finish()
    message = {"role": "assistant", "content": "".join(text) or None}
    if refusals:
        message["refusal"] = "".join(refusals)
    if tools:
        message["tool_calls"] = [tools[i] for i in sorted(tools)]
    return JSONResponse(
        {
            **base,
            "object": "chat.completion",
            "choices": [
                {"index": 0, "message": message, "finish_reason": reason},
            ],
            "usage": usage,
        },
        headers={"X-Request-ID": admission.id},
    )
