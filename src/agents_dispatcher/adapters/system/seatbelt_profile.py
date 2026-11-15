"""Generates the Seatbelt profile of one run (digest section 2, ARCH layer 4).

The profile is text built from the run's inputs: nothing is read from a fixed file, so the
read-deny inventory and the proxy port come from settings and the run. Paths must already be
canonical (symbolic links resolved) because the kernel matches resolved paths.
"""

from dataclasses import dataclass

from agents_dispatcher.domain.routing import TaskMode

MAX_PORT = 65535
DEVICE_FILES = ("/dev/null", "/dev/zero", "/dev/dtracehelper", "/dev/tty")
SECURITY_SERVICE = "com.apple.SecurityServer"
# The five services PR3 measured a Codex worker cannot start without, besides the security service.
CODEX_MACH_SERVICES = (
    SECURITY_SERVICE,
    "com.apple.system.opendirectoryd.libinfo",
    "com.apple.SystemConfiguration.configd",
    "com.apple.trustd",
    "com.apple.trustd.agent",
    "com.apple.securityd.xpc",
)
VENDOR_WRITE_KINDS = ("literal", "subpath", "regex")
# The folders inside the sandbox that a read task may write (TMPDIR and ZDOTDIR of the worker).
SCRATCH = ("tmp", "zdot")


@dataclass(frozen=True)
class VendorWrite:
    """One write a vendor's own state needs: an exact file, a folder tree or a path pattern."""

    kind: str
    path: str


@dataclass(frozen=True)
class ProviderProfile:
    """What differs between worker CLIs: the Mach services they look up and the state they write."""

    mach_services: tuple[str, ...]
    vendor_writes: tuple[VendorWrite, ...] = ()


CLAUDE_CODE = ProviderProfile((SECURITY_SERVICE,))


def codex_profile(vendor_writes: tuple[VendorWrite, ...] = ()) -> ProviderProfile:
    """Return the Codex profile; its minimal vendor-state writes come from measurement."""
    return ProviderProfile(CODEX_MACH_SERVICES, vendor_writes)


@dataclass(frozen=True)
class ProfileInputs:
    """What varies between runs."""

    sandbox_dir: str
    proxy_port: int
    denied_reads: tuple[str, ...]
    mode: TaskMode
    worktree_dir: str
    provider: ProviderProfile


def render_profile(inputs: ProfileInputs) -> str:
    """Return the profile text, or raise `ValueError` for an input that cannot be quoted safely."""
    if not 1 <= inputs.proxy_port <= MAX_PORT:
        message = f"proxy port {inputs.proxy_port} is not a valid port"
        raise ValueError(message)
    sandbox = _path(inputs.sandbox_dir)
    devices = " ".join(f"(literal {_path(device)})" for device in DEVICE_FILES)
    if inputs.mode is TaskMode.READ:
        # Only the worker's own scratch folders; the worktree is also denied after every allowance
        # so that no rule added above it can reopen it (the later rule wins).
        scratch = " ".join(f"(subpath {_path(f'{inputs.sandbox_dir}/{name}')})" for name in SCRATCH)
        allowance = f"(allow file-write* {scratch} {devices})"
        closing = [f"(deny file-write* (subpath {_path(inputs.worktree_dir)}))"]
    else:
        allowance = f"(allow file-write* (subpath {sandbox}) {devices})"
        closing = []
    mach = " ".join(f"(global-name {_quote(name)})" for name in inputs.provider.mach_services)
    vendor = [_vendor_rule(write) for write in inputs.provider.vendor_writes]
    lines = [
        "(version 1)",
        "(allow default)",
        "(deny file-write*)",
        allowance,
        *vendor,
        *closing,
        *(f"(deny file-read* (subpath {_path(path)}))" for path in inputs.denied_reads),
        f"(allow file-read* (subpath {sandbox}))",
        "(deny signal (target others))",
        "(allow signal (target self))",
        "(allow signal (target same-sandbox))",
        "(deny mach-lookup)",
        f"(allow mach-lookup {mach})",
        "(deny network*)",
        f'(allow network-outbound (remote ip "localhost:{inputs.proxy_port}"))',
    ]
    return "\n".join(lines) + "\n"


def _vendor_rule(write: VendorWrite) -> str:
    """Render one vendor-state grant; an unknown kind or an unquotable path is refused."""
    if write.kind not in VENDOR_WRITE_KINDS:
        message = f"vendor write kind {write.kind!r} is not one of {VENDOR_WRITE_KINDS}"
        raise ValueError(message)
    if write.kind != "regex":
        return f"(allow file-write* ({write.kind} {_path(write.path)}))"
    if not write.path.startswith("^/") or '"' in write.path or not write.path.isprintable():
        message = f"vendor write pattern {write.path!r} must start with ^/ and be plain text"
        raise ValueError(message)
    return f'(allow file-write* (regex #"{write.path}"))'


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
