import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from services.agent_service import AgentService

router = APIRouter(prefix="/ws", tags=["ws"])


@router.websocket("/jobs/{job_id}")
async def job_ws(job_id: str, websocket: WebSocket) -> None:
    """Real-time event stream for a running job.

    Connect after submitting via ``POST /jobs``.
    Each message is a JSON object with a ``type`` field:
    - ``status``      — job status changed
    - ``plan``        — LLM execution plan ready
    - ``step_start``  — a step is about to run
    - ``step_result`` — step execution finished
    - ``decision``    — LLM evaluated the step
    - ``abort``       — step aborted
    - ``completed``   — job finished (success or failure)
    - ``error``       — unrecoverable error
    - ``ping``        — keepalive (no action needed)

    The connection closes automatically after ``completed``, ``abort``, or ``error``.
    """
    await websocket.accept()

    try:
        events_queue = AgentService.get_events_queue(job_id)
    except Exception:
        await websocket.close(code=1008)
        return

    try:
        while True:
            try:
                event = await asyncio.wait_for(events_queue.get(), timeout=60.0)
            except asyncio.TimeoutError:
                await websocket.send_json({"type": "ping"})
                continue

            await websocket.send_json(event)

            if event.get("type") in ("completed", "error", "abort"):
                break
    except WebSocketDisconnect:
        pass
    finally:
        await websocket.close()
