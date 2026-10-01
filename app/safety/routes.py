import asyncio
import time

from fastapi import APIRouter, Depends
from starlette.concurrency import run_in_threadpool

from app.gateway.execution import CompletionRun
from app.gateway.quota import reserve, settle
from app.gateway.routes import client_key
from app.gateway.schemas import ChatRequest, Message
from app.safety.schemas import SafetyRequest
from app.safety.service import assess

router = APIRouter(prefix="/v1/safety", tags=["server-to-server Safety"])


@router.post("/assess")
async def safety(request: SafetyRequest, key_id: str = Depends(client_key)):
    # Normal key authentication plus explicit opt-in permission and public model
    # permission. No model, classifier instructions or arbitrary inference payload.
    admission = await run_in_threadpool(
        reserve,
        key_id,
        ChatRequest(
            model=request.selected_model,
            messages=[Message(role="user", content="Safety admission")],
            max_tokens=1,
        ),
        purpose="safety",
    )
    started = time.monotonic()
    run = CompletionRun(admission)
    run.heartbeat = asyncio.create_task(run.keep_alive())
    result, code = None, None
    try:
        result = await run_in_threadpool(
            assess, request.context, policy_version=request.policy_version, image=request.image
        )
        from app.safety.receipts import issue

        result["receipt"] = issue(
            key_id, admission.route.alias, request.context, request.image, result
        )
        # Public response never includes internal model/provider identifiers.
        return {k: v for k, v in result.items() if k != "internal_requests"}
    except BaseException as error:
        code = getattr(error, "code", "safety_unavailable")
        raise
    finally:
        run.heartbeat.cancel()
        await asyncio.gather(run.heartbeat, return_exceptions=True)
        await run_in_threadpool(
            settle,
            admission.id,
            None,
            int((time.monotonic() - started) * 1000),
            status="completed" if result else "failed",
            error_code=code,
            usage_unknown=False,
            execution_metadata={
                "safety": {
                    "policy_version": request.policy_version,
                    "internal_requests": result["internal_requests"] if result else [],
                }
            },
        )
