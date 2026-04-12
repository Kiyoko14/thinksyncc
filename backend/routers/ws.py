import asyncio
import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from core.security import decode_token
from services.agent_service import AgentService
from services.redis_service import RedisService

router = APIRouter(prefix="/v1/ws", tags=["ws"])


async def _send_history(job_id: str, websocket: WebSocket) -> bool:
    history = await AgentService.get_event_history(job_id)
    for event in history:
        await websocket.send_json(event)
        if event.get("type") == "completed":
            return True
    return False


async def _stream_live_events(job_id: str, websocket: WebSocket) -> None:
    redis = RedisService.get_async_client()
    if redis is not None:
        pubsub = redis.pubsub()
        await pubsub.subscribe(f"job_events:{job_id}:live")
        try:
            while True:
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=60.0)
                if message is None:
                    await websocket.send_json({"type": "ping"})
                    continue
                event = json.loads(message["data"])
                await websocket.send_json(event)
                if event.get("type") == "completed":
                    break
        finally:
            await pubsub.unsubscribe(f"job_events:{job_id}:live")
            await pubsub.aclose()
        return

    queue = AgentService.subscribe(job_id)
    try:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=60.0)
            except asyncio.TimeoutError:
                await websocket.send_json({"type": "ping"})
                continue
            await websocket.send_json(event)
            if event.get("type") == "completed":
                break
    finally:
        AgentService.unsubscribe(job_id, queue)


@router.websocket("/jobs/{job_id}")
async def job_ws(job_id: str, websocket: WebSocket) -> None:
    token = websocket.query_params.get("token", "").strip()
    if not token:
        await websocket.close(code=1008)
        return

    try:
        payload = decode_token(token)
        AgentService.get_job(job_id=job_id, user_id=payload["sub"])
    except Exception:
        await websocket.close(code=1008)
        return

    await websocket.accept()

    try:
        completed = await _send_history(job_id, websocket)
        if not completed:
            await _stream_live_events(job_id, websocket)
    except WebSocketDisconnect:
        pass
    finally:
        await websocket.close()
