"""Deterministic template/tool layer (pre-LLM).

Templates are plain Python/project scaffolds that can be rendered and executed
without calling the LLM. This reduces reliance on generation for common intents.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class TemplateSpec:
    name: str
    description: str
    keywords: list[str]
    files: dict[str, str]
    dependencies: list[str]


_TELEGRAM_BOT_MAIN = (
    "import os\n"
    "import asyncio\n"
    "from telegram import Update\n"
    "from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters\n"
    "\n"
    "\n"
    "def _resolve_token() -> str:\n"
    "    token = os.getenv(\"TELEGRAM_BOT_TOKEN\", \"\").strip()\n"
    "    if token:\n"
    "        return token\n"
    "    token = \"{TOKEN}\".strip()\n"
    "    if token and \"{\" not in token and \"}\" not in token:\n"
    "        return token\n"
    "    raise SystemExit(\"Missing Telegram token. Provide TELEGRAM_BOT_TOKEN env var or include token in prompt.\")\n"
    "\n"
    "\n"
    "async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:\n"
    "    _ = context\n"
    "    if update.message:\n"
    "        await update.message.reply_text(\"Hello! Send me any text and I'll echo it.\")\n"
    "\n"
    "\n"
    "async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:\n"
    "    _ = context\n"
    "    if update.message and update.message.text is not None:\n"
    "        await update.message.reply_text(update.message.text)\n"
    "\n"
    "\n"
    "def main() -> None:\n"
    "    token = _resolve_token()\n"
    "    app = ApplicationBuilder().token(token).build()\n"
    "    app.add_handler(CommandHandler(\"start\", start))\n"
    "    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))\n"
    "    app.run_polling(allowed_updates=Update.ALL_TYPES)\n"
    "\n"
    "\n"
    "if __name__ == \"__main__\":\n"
    "    main()\n"
)


_FASTAPI_MAIN = (
    "import os\n"
    "from fastapi import FastAPI\n"
    "\n"
    "\n"
    "def _parse_port(value: str) -> int:\n"
    "    try:\n"
    "        port = int(str(value).strip())\n"
    "        if 1 <= port <= 65535:\n"
    "            return port\n"
    "    except Exception:\n"
    "        pass\n"
    "    return 8000\n"
    "\n"
    "\n"
    "app = FastAPI(title=\"{APP_NAME}\")\n"
    "\n"
    "\n"
    "@app.get(\"/\")\n"
    "def root() -> dict:\n"
    "    return {\"status\": \"ok\"}\n"
    "\n"
    "\n"
    "@app.get(\"/health\")\n"
    "def health() -> dict:\n"
    "    return {\"ok\": True}\n"
    "\n"
    "\n"
    "if __name__ == \"__main__\":\n"
    "    import uvicorn\n"
    "\n"
    "    port = _parse_port(os.getenv(\"PORT\", \"{PORT}\"))\n"
    "    uvicorn.run(\"main:app\", host=\"0.0.0.0\", port=port, log_level=\"info\")\n"
)


_FLASK_MAIN = (
    "import os\n"
    "from flask import Flask, jsonify\n"
    "\n"
    "\n"
    "def _parse_port(value: str) -> int:\n"
    "    try:\n"
    "        port = int(str(value).strip())\n"
    "        if 1 <= port <= 65535:\n"
    "            return port\n"
    "    except Exception:\n"
    "        pass\n"
    "    return 5000\n"
    "\n"
    "\n"
    "app = Flask(__name__)\n"
    "\n"
    "\n"
    "@app.get(\"/\")\n"
    "def root():\n"
    "    return jsonify({\"status\": \"ok\"})\n"
    "\n"
    "\n"
    "@app.get(\"/health\")\n"
    "def health():\n"
    "    return jsonify({\"ok\": True})\n"
    "\n"
    "\n"
    "if __name__ == \"__main__\":\n"
    "    port = _parse_port(os.getenv(\"PORT\", \"{PORT}\"))\n"
    "    app.run(host=\"0.0.0.0\", port=port)\n"
)


_PYTHON_SCRIPT_MAIN = (
    "def main() -> None:\n"
    "    import json\n"
    "    import logging\n"
    "    from datetime import datetime, timezone\n"
    "    payload = {\n"
    "        \"timestamp\": datetime.now(timezone.utc).isoformat(),\n"
    "        \"level\": \"INFO\",\n"
    "        \"layer\": \"execution\",\n"
    "        \"message\": \"Hello from ThinkSync template script.\",\n"
    "        \"meta\": {},\n"
    "    }\n"
    "    logging.basicConfig(level=logging.INFO)\n"
    "    logging.getLogger(\"thinksync.template\").info(json.dumps(payload, ensure_ascii=False))\n"
    "\n"
    "\n"
    "if __name__ == \"__main__\":\n"
    "    main()\n"
)


def _requirements_txt(deps: list[str]) -> str:
    return ("\n".join(d.strip() for d in deps if (d or "").strip()) + "\n") if deps else ""


TEMPLATES: dict[str, TemplateSpec] = {
    "telegram_bot": TemplateSpec(
        name="telegram_bot",
        description="Python Telegram bot using python-telegram-bot v20+ (run_polling).",
        keywords=["telegram", "bot"],
        files={
            "main.py": _TELEGRAM_BOT_MAIN,
            "requirements.txt": _requirements_txt(["python-telegram-bot>=20,<22"]),
        },
        dependencies=["python-telegram-bot>=20,<22"],
    ),
    "fastapi_app": TemplateSpec(
        name="fastapi_app",
        description="FastAPI app with Uvicorn server.",
        keywords=["api", "fastapi", "backend"],
        files={
            "main.py": _FASTAPI_MAIN,
            "requirements.txt": _requirements_txt(["fastapi>=0.110", "uvicorn[standard]>=0.23"]),
        },
        dependencies=["fastapi>=0.110", "uvicorn[standard]>=0.23"],
    ),
    "flask_app": TemplateSpec(
        name="flask_app",
        description="Flask web app.",
        keywords=["flask", "web"],
        files={
            "main.py": _FLASK_MAIN,
            "requirements.txt": _requirements_txt(["flask>=2.3"]),
        },
        dependencies=["flask>=2.3"],
    ),
    "python_script": TemplateSpec(
        name="python_script",
        description="Minimal Python script (short task).",
        keywords=["python script", "script", "run python"],
        files={"main.py": _PYTHON_SCRIPT_MAIN},
        dependencies=[],
    ),
}


def match_template(text: str) -> TemplateSpec | None:
    lowered = (text or "").lower()
    for template in TEMPLATES.values():
        if any(keyword in lowered for keyword in template.keywords):
            return template
    return None


_TOKEN_RE = re.compile(r"\b\d{5,}:[A-Za-z0-9_-]{20,}\b")
_PORT_RE = re.compile(r"(?i)\bport\s*[:=]?\s*(\d{2,5})\b")
_APP_NAME_RE = re.compile(r"(?i)\bapp\s*name\s*[:=]\s*([^\n\r]{1,80})")


def extract_template_params(text: str) -> dict[str, str]:
    raw = text or ""
    params: dict[str, str] = {}

    token_match = _TOKEN_RE.search(raw)
    if token_match:
        params["TOKEN"] = token_match.group(0).strip()

    port_match = _PORT_RE.search(raw)
    if port_match:
        params["PORT"] = port_match.group(1).strip()

    app_name_match = _APP_NAME_RE.search(raw)
    if app_name_match:
        candidate = app_name_match.group(1).strip().strip("\"'`").strip()
        if candidate:
            params["APP_NAME"] = candidate

    return params


def render_template(template: TemplateSpec, params: dict[str, str] | None = None) -> dict[str, Any]:
    p = dict(params or {})
    if "PORT" not in p:
        p["PORT"] = "8000" if template.name == "fastapi_app" else "5000"
    if "APP_NAME" not in p:
        p["APP_NAME"] = "ThinkSync App"
    if "TOKEN" not in p:
        p["TOKEN"] = ""

    rendered_files: dict[str, str] = {}
    for path, content in template.files.items():
        rendered = (content or "")
        rendered = rendered.replace("{TOKEN}", str(p.get("TOKEN") or ""))
        rendered = rendered.replace("{PORT}", str(p.get("PORT") or ""))
        rendered = rendered.replace("{APP_NAME}", str(p.get("APP_NAME") or ""))
        rendered_files[path] = rendered

    return {
        "name": template.name,
        "files": rendered_files,
        "dependencies": list(template.dependencies),
        "params": p,
    }
