import asyncio
from types import SimpleNamespace

from services.deployment_service import DeploymentService
from services.runtime_detector import RuntimeType, detect_runtime


def test_static_multifile_fixture_is_static() -> None:
    runtime = detect_runtime(
        {
            "index.html": '<!doctype html><link rel="stylesheet" href="styles.css"><script src="app.js"></script>',
            "styles.css": "body { color: red; }",
            "app.js": "document.body.dataset.ready = 'yes';",
        }
    )
    assert runtime is RuntimeType.STATIC


def test_valid_python_is_python() -> None:
    assert detect_runtime({"main.py": "print('ok')"}) is RuntimeType.PYTHON


def test_real_node_manifest_is_node() -> None:
    assert detect_runtime(
        {"package.json": '{"scripts":{"start":"node server.js"}}', "server.js": ""}
    ) is RuntimeType.NODE


def test_html_main_py_does_not_override_static() -> None:
    assert detect_runtime(
        {"main.py": "<html></html>", "index.html": "<html></html>"}
    ) is RuntimeType.STATIC


def test_html_main_py_with_python_manifest_does_not_override_static() -> None:
    assert detect_runtime(
        {
            "main.py": "<html></html>",
            "requirements.txt": "not-a-python-entrypoint",
            "index.html": "<!doctype html><html></html>",
        }
    ) is RuntimeType.STATIC


def test_browser_app_js_alone_is_not_node() -> None:
    try:
        detect_runtime({"app.js": "document.body"})
    except ValueError:
        pass
    else:
        raise AssertionError("ambiguous browser-only project must fail safely")


def test_static_nginx_config_serves_workspace_without_upstream() -> None:
    config = DeploymentService._render_static_nginx(
        server_name="site.example.test",
        workspace_path="/root/workspaces/site",
    )
    assert "root /root/workspaces/site;" in config
    assert "try_files $uri $uri/ /index.html;" in config
    assert "proxy_pass" not in config
    assert "http.server" not in config


def test_static_cannot_become_process_command() -> None:
    try:
        DeploymentService._deployment_command("/root/workspaces/site", 8080, RuntimeType.STATIC)
    except ValueError as exc:
        assert "Nginx" in str(exc)
    else:
        raise AssertionError("STATIC must never produce a process command")


def test_static_deployment_uses_nginx_without_process_supervisor(monkeypatch) -> None:
    async def scenario() -> None:
        commands: list[str] = []

        async def fake_remote(*, server, command, step):
            commands.append(command)
            return SimpleNamespace(exit_code=0, stdout="", stderr="")

        class Query:
            async def execute(self):
                return SimpleNamespace(data=[{"id": "deployment-1"}])

        class Table:
            def update(self, payload):
                return Query()

            def insert(self, payload):
                return Query()

        class Supabase:
            def table(self, name):
                return Table()

        monkeypatch.setattr(DeploymentService, "_execute_remote", fake_remote)
        result = await DeploymentService._create_static_deployment(
            server={},
            workspace={"domain": "site.example.test", "slug": "site"},
            workspace_path="/root/workspaces/site",
            workspace_id="workspace-1",
            supabase=Supabase(),
            existing=None,
        )
        assert result["runtime"] == "static"
        assert result["port"] is None
        assert any("base64 -d" in command and "sites-available" in command for command in commands)
        assert any("curl -fsS" in command for command in commands)
        assert all("pm2" not in command and "http.server" not in command for command in commands)

    asyncio.run(scenario())