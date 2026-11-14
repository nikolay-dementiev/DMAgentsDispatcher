"""Generates the Seatbelt profile of one run (digest section 2, ARCH layer 4).

The profile is text built from the run's inputs: nothing is read from a fixed file, so the
read-deny inventory and the proxy port come from settings and the run. Paths must already be
canonical (symbolic links resolved) because the kernel matches resolved paths.
"""

from dataclasses import dataclass

MAX_PORT = 65535
DEVICE_FILES = ("/dev/null", "/dev/zero", "/dev/dtracehelper", "/dev/tty")
SECURITY_SERVICE = "com.apple.SecurityServer"


@dataclass(frozen=True)
class ProfileInputs:
    """What varies between runs."""

    sandbox_dir: str
    proxy_port: int
    denied_reads: tuple[str, ...]


def render_profile(inputs: ProfileInputs) -> str:
    """Return the profile text, or raise `ValueError` for an input that cannot be quoted safely."""
    if not 1 <= inputs.proxy_port <= MAX_PORT:
        message = f"proxy port {inputs.proxy_port} is not a valid port"
        raise ValueError(message)
    sandbox = _path(inputs.sandbox_dir)
    devices = " ".join(f"(literal {_path(device)})" for device in DEVICE_FILES)
    lines = [
        "(version 1)",
        "(allow default)",
        "(deny file-write*)",
        f"(allow file-write* (subpath {sandbox}) {devices})",
        *(f"(deny file-read* (subpath {_path(path)}))" for path in inputs.denied_reads),
        f"(allow file-read* (subpath {sandbox}))",
        "(deny signal (target others))",
        "(allow signal (target self))",
        "(allow signal (target same-sandbox))",
        "(deny mach-lookup)",
        f"(allow mach-lookup (global-name {_quote(SECURITY_SERVICE)}))",
        "(deny network*)",
        f'(allow network-outbound (remote ip "localhost:{inputs.proxy_port}"))',
    ]
    return "\n".join(lines) + "\n"


def _path(path: str) -> str:
    """Quote an absolute path as a profile string literal."""
    if not path.startswith("/"):
        message = f"path {path!r} is not absolute"
        raise ValueError(message)
    return _quote(path)


def _quote(text: str) -> str:
    """Quote text as a profile string literal; control characters are refused."""
    if any(not character.isprintable() for character in text):
        message = f"{text!r} contains a control or unprintable character"
        raise ValueError(message)
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'
