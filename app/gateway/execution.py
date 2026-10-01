"""Keep a request lease alive and finalize it on completion, failure or disconnect."""

import asyncio
import time
from contextlib import suppress

import anyio
from starlette.concurrency import run_in_threadpool
from starlette.responses import StreamingResponse

from app.core.config import get_settings
from app.gateway.quota import mark_dispatched, renew, settle
from app.providers.base import ChatEvent, ProviderError

END = object()


def next_event(iterator):
    return next(iterator, END)


def validate_event(event):
    if not isinstance(event, ChatEvent):
        raise ValueError("Invalid internal event")
    if event.finish_reason not in (None, "stop", "length", "tool_calls", "content_filter"):
        raise ValueError("Invalid finish reason")
    if event.usage is not None:
        counts = event.usage
        if set(counts) != {"prompt_tokens", "completion_tokens", "total_tokens"}:
            raise ValueError("Invalid token counts")
        if any(type(n) is not int or n < 0 for n in counts.values()):
            raise ValueError("Invalid token counts")
        if counts["total_tokens"] != counts["prompt_tokens"] + counts["completion_tokens"]:
            raise ValueError("Inconsistent token counts")
    if not isinstance(event.delta, dict) or set(event.delta) - {
        "role",
        "content",
        "refusal",
        "tool_calls",
    }:
        raise ValueError("Invalid delta")
    if "role" in event.delta and event.delta["role"] != "assistant":
        raise ValueError("Invalid role")
    for name in ("content", "refusal"):
        if name in event.delta and not isinstance(event.delta[name], str):
            raise ValueError("Invalid text delta")
    for tool in event.delta.get("tool_calls") or []:
        if type(tool.get("index")) is not int or not 0 <= tool["index"] < 50:
            raise ValueError("Invalid tool index")
        if set(tool) - {"index", "id", "type", "function"}:
            raise ValueError("Invalid tool delta")
        if tool.get("type", "function") != "function":
            raise ValueError("Invalid tool type")
        if "id" in tool and not isinstance(tool["id"], str):
            raise ValueError("Invalid tool ID")
        function = tool.get("function", {})
        if set(function) - {"name", "arguments"} or any(
            not isinstance(v, str) for v in function.values()
        ):
            raise ValueError("Invalid function delta")
    return event


class CompletionRun:
    def __init__(self, admission):
        self.admission = admission
        self.started = time.monotonic()
        self.iterator = None
        self.first = END
        self.usage = None
        self.has_finish = False
        self.completed = False
        self.error = None
        self.cancelled = False
        self.closed = False
        self.heartbeat = None
        self.lease_error = False

    async def keep_alive(self):
        try:
            while True:
                await asyncio.sleep(get_settings().reservation_heartbeat_seconds)
                if not await run_in_threadpool(renew, self.admission.id):
                    self.lease_error = True
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            # No raw DB/SDK messages in logs or public errors. The lease is a fallback
            # when finalization cannot reach PostgreSQL after a process/network failure.
            self.lease_error = True

    async def open(self):
        try:
            self.heartbeat = asyncio.create_task(self.keep_alive())
            from app.safety.service import enforce_chat

            if self.admission.key_id is not None:
                await run_in_threadpool(enforce_chat, self.admission)
            await run_in_threadpool(mark_dispatched, self.admission.id)
            self.iterator = iter(self.admission.adapter.events(self.admission.call))
            self.first = await self.next()
            if self.first is END:
                raise ProviderError("provider_error", message="The provider returned no response.")
        except BaseException as exc:
            self.record_error(exc)
            await self.finish()
            if isinstance(exc, (ProviderError, asyncio.CancelledError)):
                raise
            raise self.error from None

    def record_error(self, exc):
        if isinstance(exc, (asyncio.CancelledError, GeneratorExit)):
            self.cancelled = True
        elif isinstance(exc, ProviderError):
            self.error = exc
        else:
            self.error = ProviderError("provider_error", message="The provider request failed.")
        if self.error is not None:
            self.error.request_id = self.admission.id

    async def next(self):
        if self.lease_error:
            raise ProviderError(
                "provider_unavailable", 503, message="The request lease could not be renewed."
            )
        try:
            event = await run_in_threadpool(next_event, self.iterator)
            if event is END:
                missing_usage_allowed = getattr(
                    self.admission.adapter, "supports_missing_usage", False
                )
                if not self.has_finish or (self.usage is None and not missing_usage_allowed):
                    raise ProviderError(
                        "provider_error",
                        message="The provider response ended without final usage or finish reason.",
                    )
                self.completed = True
                return END
            validate_event(event)
            if event.usage is not None:
                self.usage = event.usage
            if event.finish_reason is not None:
                self.has_finish = True
            return event
        except ProviderError:
            raise
        except (ValueError, TypeError, KeyError, AttributeError):
            raise ProviderError(
                "provider_error", message="The provider returned an invalid response."
            ) from None
        except Exception:
            raise ProviderError("provider_error", message="The provider request failed.") from None

    async def events(self):
        try:
            event, self.first = self.first, END
            while event is not END:
                yield event
                event = await self.next()
        except BaseException as exc:
            self.record_error(exc)
            raise
        finally:
            await self.finish()

    async def finish(self):
        if self.closed:
            return
        with anyio.CancelScope(shield=True):
            self.closed = True
            if self.heartbeat is not None:
                self.heartbeat.cancel()
                with suppress(asyncio.CancelledError):
                    await self.heartbeat
            if self.iterator is not None:
                with suppress(Exception):
                    await run_in_threadpool(self.iterator.close)
            status = "completed" if self.completed and self.error is None else "failed"
            if self.cancelled or (not self.completed and self.error is None):
                status = "cancelled"
            metrics = self.admission.call.metrics
            usage = self.usage if self.usage is not None else metrics.get("usage")
            usage_unknown = self.error.usage_unknown if self.error else True
            if self.admission.route.provider == "amazon_bedrock" and not metrics.get("attempts"):
                usage_unknown = False  # A failed helper never charges selected-model tokens.
            await run_in_threadpool(
                settle,
                self.admission.id,
                usage,
                int((time.monotonic() - self.started) * 1000),
                status=status,
                error_code=self.error.code
                if self.error
                else ("client_disconnected" if status == "cancelled" else None),
                usage_unknown=usage_unknown,
                execution_metadata=metrics,
            )


class GatewayStreamingResponse(StreamingResponse):
    def __init__(self, content, run):
        self.run = run
        super().__init__(
            content,
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-store, no-transform",
                "X-Accel-Buffering": "no",
            },
        )

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            # Includes disconnects before the SSE generator is entered or while send()
            # is waiting on backpressure. Generator GC is not a resource cleanup policy.
            await self.run.finish()
