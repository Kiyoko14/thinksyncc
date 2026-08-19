# ThinkSync Deployment Reality Check

## A. Root cause
`ExecutionResultProjector` was treating any `workspace_url` as a verified deployment. That meant the job could finish with `success=true` and still present a deployment URL even when the upstream was not actually reachable. The real deployment contract in `backend/services/executor.py` also did not carry a verified deployment object forward, so the frontend had no reliable signal.

## B. Exact files changed
- `backend/models/agent.py`
- `backend/services/executor.py`
- `backend/services/execution_result_projector.py`
- `backend/services/agent_service.py`
- `backend/tests/test_deployment_result_projection.py`

## C. Exact code changes
- Added `deployment` to `ToolCallingLoopResult` so the executor can return a verified deployment payload.
- Updated the executor to set `deployment={"url": ..., "verified": True}` only after the deployment contract passes.
- Removed the fallback that invented deployment data from `workspace_url`.
- Added summary parsing in `to_forge_v2_response()` so an actual verified deployment URL is surfaced to the frontend.
- Added regression tests for “do not invent deployment from workspace URL” and “preserve explicit verified deployment”.

## D. Runtime verification performed
- `python3 -m py_compile backend/models/agent.py backend/services/executor.py backend/services/execution_result_projector.py backend/services/agent_service.py backend/tests/test_deployment_result_projection.py`
- Attempted backend test execution, but this sandbox is missing `pytest` and `pydantic`.
- Attempted frontend build, but this sandbox is missing `node` and `npm`.
- Live OS checks like `ss -lntp`, `systemctl status nginx`, and `nginx -t` could not be completed here because the sandbox blocks those binaries and nginx is not installed locally.

## E. Before / after deployment behavior
Before: a completed job could display a deployment URL even when the live upstream was dead.  
After: deployment is only surfaced when the executor has an explicit verified deployment result. Otherwise the frontend should not claim verification.

## F. Tests executed
- `python3 -m py_compile ...` on the touched Python files.
- Backend unit tests could not run in this sandbox because required dependencies are missing.

## G. `npm run build` result
Could not run in this sandbox. `node` and `npm` are not installed.

## H. Whether the URL is actually reachable
No. The production smoke test still returned:
`{"status":"error","error":"Upstream workspace is not reachable"}`

The fix prevents the UI from treating that as a verified deployment unless the backend has actually proven the endpoint is reachable.
