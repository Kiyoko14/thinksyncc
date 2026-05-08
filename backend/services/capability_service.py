import shutil

from services.ssh_service import SSHService

_CAPABILITY_CACHE: dict[str, dict] = {}


async def detect_capabilities(server):
    cache_key = f"{server.get('host')}|{server.get('ssh_user')}|{server.get('ssh_port')}"
    cached = _CAPABILITY_CACHE.get(cache_key)
    if isinstance(cached, dict) and cached:
        return dict(cached)

    commands = {
        "python": "python3 --version",
        "node": "node --version",
        "npm": "npm --version",
        "pm2": "pm2 -v",
        "docker": "docker --version",
        "git": "git --version",
        "nginx": "nginx -v",
    }

    capabilities = {}
    versions: dict[str, str] = {}

    for key, cmd in commands.items():
        try:
            res = await SSHService.execute(server=server, command=cmd)
            capabilities[key] = res.exit_code == 0
            versions[key] = (res.stdout or res.stderr or "").strip()
        except Exception:
            capabilities[key] = False
            versions[key] = ""
    capabilities["versions"] = versions

    system_info_cmds = {
        "which_python3": "which python3",
        "which_node": "which node",
        "free_m": "free -m",
        "df_h": "df -h",
    }
    system_info: dict[str, str] = {}
    for key, cmd in system_info_cmds.items():
        try:
            res = await SSHService.execute(server=server, command=cmd)
            system_info[key] = (res.stdout or res.stderr or "").strip()
        except Exception:
            system_info[key] = ""
    capabilities["system_info"] = system_info

    _CAPABILITY_CACHE[cache_key] = dict(capabilities)
    return capabilities
