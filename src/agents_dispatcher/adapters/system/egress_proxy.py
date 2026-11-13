"""A loopback HTTP CONNECT proxy that tunnels only to allowlisted host names and ports.

The worker reaches the network through this proxy alone (the sandbox profile denies all other
network access). A destination is served only when its normalised host name equals a configured
name exactly and its port is a configured port. The two lists are independent: every allowed
host is reachable on every allowed port (a cross product, not a list of pairs). Plain HTTP is
refused, non-public addresses are refused unless a setting allows them, and every decision is
recorded. The tunnel ends when either side closes, after `idle_s` without traffic, or after
`lifetime_s`, whichever comes first; the close is the signal the client sees. Connection resets
end a tunnel the same way.
"""

import contextlib
import ipaddress
import select
import socket
import threading
import time
from dataclasses import dataclass

from agents_dispatcher.application.ports import EgressEndpoint
from agents_dispatcher.domain.run import RunId

LOOPBACK = "127.0.0.1"
DRAIN_LIMIT = 65536
POLL_S = 0.1
MAX_PORT = 65535
STATUS_METHOD_NOT_ALLOWED = 405
_PHRASES = {
    200: "Connection Established",
    400: "Bad Request",
    403: "Forbidden",
    405: "Method Not Allowed",
    408: "Request Timeout",
    431: "Request Header Fields Too Large",
    502: "Bad Gateway",
    503: "Service Unavailable",
}


@dataclass(frozen=True)
class ProxyConfig:
    """The proxy settings of ARCH section 3.2 that the proxy consumes."""

    hosts: tuple[str, ...]
    ports: tuple[int, ...]
    allow_nonpublic: bool
    header_bytes: int
    connections: int
    buffer_bytes: int
    idle_s: float
    lifetime_s: float
    connect_s: float


@dataclass(frozen=True)
class ProxyDecision:
    """One allow or refuse decision, with the destination as the client wrote it."""

    allowed: bool
    host: str
    port: int | None
    reason: str


class _Refusal(Exception):  # noqa: N818 - control flow inside this module, not a public error
    """A request that gets an HTTP status instead of a tunnel."""

    def __init__(self, status: int, reason: str) -> None:
        """Keep the status and the reason that goes to the decision log."""
        super().__init__(reason)
        self.status = status
        self.reason = reason


def _unmapped(address: str) -> str:
    """Return the IPv4 form of an IPv4-mapped IPv6 address; any other address is unchanged."""
    parsed = ipaddress.ip_address(address)
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
        return str(parsed.ipv4_mapped)
    return address


def _normalise(host: str) -> str:
    """Lower-case a host name and drop one trailing dot."""
    return host.lower().removesuffix(".")


class ConnectAllowlistProxy:
    """An `EgressProxy` serving CONNECT tunnels to an allowlist."""

    def __init__(self, config: ProxyConfig) -> None:
        """Keep the configuration; nothing listens until `start`."""
        self._config = config
        self._hosts = frozenset(_normalise(host) for host in config.hosts)
        self._lock = threading.Lock()
        self._decisions: list[ProxyDecision] = []
        self._open: set[socket.socket] = set()
        self._threads: list[threading.Thread] = []
        self._active = 0
        self._stopping = threading.Event()
        self._listener: socket.socket | None = None

    def start(self, run_id: RunId) -> EgressEndpoint:  # noqa: ARG002 - the port passes the run id
        """Listen on an ephemeral loopback port and return its address."""
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind((LOOPBACK, 0))
        listener.listen()
        self._listener = listener
        thread = threading.Thread(target=self._accept_loop, args=(listener,), daemon=True)
        self._threads.append(thread)
        thread.start()
        return EgressEndpoint(host=LOOPBACK, port=listener.getsockname()[1])

    def stop(self) -> None:
        """Stop listening and close every open tunnel."""
        self._stopping.set()
        if self._listener is not None:
            self._listener.close()
        with self._lock:
            sockets = list(self._open)
        for sock in sockets:
            _close(sock)
        for thread in list(self._threads):
            thread.join(timeout=2.0)

    def decisions(self) -> tuple[ProxyDecision, ...]:
        """Return every decision so far, in arrival order."""
        with self._lock:
            return tuple(self._decisions)

    def _accept_loop(self, listener: socket.socket) -> None:
        """Accept clients until stopped; each client is served on its own thread."""
        while not self._stopping.is_set():
            try:
                ready, _, _ = select.select([listener], [], [], POLL_S)
                if not ready:
                    continue
                client, _ = listener.accept()
            except (OSError, ValueError) as error:
                if self._stopping.is_set() or listener.fileno() == -1:
                    return  # the listener was closed on purpose
                # A transient failure (for example out of descriptors) must not kill the proxy
                # without a trace: record it, back off, and keep accepting.
                self._record(allowed=False, host="", port=None, reason=f"accept failed: {error}")
                time.sleep(POLL_S)
                continue
            with self._lock:
                over_limit = self._active >= self._config.connections
                if not over_limit:
                    self._active += 1
                self._open.add(client)
            thread = threading.Thread(
                target=self._serve, args=(client,), kwargs={"over_limit": over_limit}, daemon=True
            )
            with self._lock:
                self._threads.append(thread)
            thread.start()

    def _serve(self, client: socket.socket, *, over_limit: bool) -> None:
        """Handle one client; always release the slot and close the sockets."""
        upstream: socket.socket | None = None
        host, port = "", None
        try:
            client.settimeout(self._config.idle_s)
            head, leftover = self._read_head(client)
            method, target = self._request_line(head)
            host = target
            self._require(method == "CONNECT", STATUS_METHOD_NOT_ALLOWED, "only CONNECT is served")
            host, port = self._split_target(target)
            self._require(not over_limit, 503, "connection limit reached")
            upstream = self._open_upstream(host, port)
            with self._lock:
                self._open.add(upstream)
            self._record(allowed=True, host=host, port=port, reason="allowlisted")
            client.sendall(self._response(200))
            self._relay(client, upstream, leftover)
        except _Refusal as refusal:
            self._record(allowed=False, host=host, port=port, reason=refusal.reason)
            self._refuse(client, refusal.status)
        except TimeoutError:
            self._record(allowed=False, host=host, port=port, reason="timed out")
        except OSError as error:
            self._record(allowed=False, host=host, port=port, reason=f"i/o error: {error}")
        finally:
            for sock in (client, upstream):
                if sock is not None:
                    with self._lock:
                        self._open.discard(sock)
                    _close(sock)
            if not over_limit:
                with self._lock:
                    self._active -= 1

    def _read_head(self, client: socket.socket) -> tuple[bytes, bytes]:
        """Read up to the blank line; return the head and any bytes that followed it."""
        received = b""
        while b"\r\n\r\n" not in received:
            if len(received) > self._config.header_bytes:
                raise _Refusal(431, "request header too large")
            chunk = client.recv(4096)
            if not chunk:
                raise _Refusal(400, "connection closed before the request ended")
            received += chunk
        head, _, leftover = received.partition(b"\r\n\r\n")
        if len(head) > self._config.header_bytes:
            raise _Refusal(431, "request header too large")
        return head, leftover

    @staticmethod
    def _require(condition: bool, status: int, reason: str) -> None:  # noqa: FBT001
        """Refuse the request with `status` unless `condition` holds."""
        if not condition:
            raise _Refusal(status, reason)

    @staticmethod
    def _request_line(head: bytes) -> tuple[str, str]:
        """Return the method and target of the first request line."""
        parts = head.split(b"\r\n", 1)[0].decode("latin-1").split(" ")
        method_target_version = 3
        if len(parts) != method_target_version or not parts[2].startswith("HTTP/"):
            raise _Refusal(400, "malformed request line")
        return parts[0], parts[1]

    @staticmethod
    def _split_target(target: str) -> tuple[str, int]:
        """Split `host:port` or `[v6]:port` into a normalised host and a port."""
        host, _, raw_port = target.rpartition(":")
        if host.startswith("[") and host.endswith("]"):
            host = host[1:-1]
        if not host or not raw_port.isdigit() or not 1 <= int(raw_port) <= MAX_PORT:
            raise _Refusal(400, "malformed CONNECT target")
        return _normalise(host), int(raw_port)

    def _open_upstream(self, host: str, port: int) -> socket.socket:
        """Check the allowlist and the address class, then connect to a validated address."""
        if host not in self._hosts or port not in self._config.ports:
            raise _Refusal(403, "destination is not on the allowlist")
        try:
            infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except socket.gaierror as error:
            raise _Refusal(502, f"cannot resolve the host: {error}") from error
        addresses = [_unmapped(str(info[4][0])) for info in infos]
        if not self._config.allow_nonpublic and not all(
            ipaddress.ip_address(address).is_global for address in addresses
        ):
            raise _Refusal(403, "destination resolves to a non-public address")
        last_error = "no address to connect to"
        for address in addresses:
            try:
                upstream = socket.create_connection((address, port), timeout=self._config.connect_s)
            except OSError as error:
                last_error = f"{address}: {error}"
                continue
            if _unmapped(str(upstream.getpeername()[0])) == address:
                return upstream
            peer = upstream.getpeername()[0]
            _close(upstream)
            last_error = f"peer {peer} differs from the validated address {address}"
        raise _Refusal(502, f"the upstream connection failed: {last_error}")

    def _relay(self, client: socket.socket, upstream: socket.socket, leftover: bytes) -> None:
        """Copy bytes both ways until a side closes or a bound is reached."""
        started = last = time.monotonic()
        if leftover:
            upstream.sendall(leftover)
        sources = {client: upstream, upstream: client}
        while sources and not self._stopping.is_set():
            now = time.monotonic()
            idle_left = last + self._config.idle_s - now
            remaining = min(idle_left, started + self._config.lifetime_s - now)
            if remaining <= 0:
                return
            try:
                readable, _, _ = select.select(list(sources), [], [], min(remaining, POLL_S))
                for source in readable:
                    data = source.recv(self._config.buffer_bytes)
                    destination = sources[source]
                    if not data:
                        del sources[source]
                        with contextlib.suppress(OSError):  # the peer may already be gone
                            destination.shutdown(socket.SHUT_WR)
                        continue
                    destination.sendall(data)
                    last = time.monotonic()
            except (OSError, ValueError):
                return

    def _record(self, *, allowed: bool, host: str, port: int | None, reason: str) -> None:
        """Append one decision."""
        with self._lock:
            self._decisions.append(ProxyDecision(allowed, host, port, reason))

    @staticmethod
    def _response(status: int) -> bytes:
        """Build a bodiless response."""
        extra = "Allow: CONNECT\r\n" if status == STATUS_METHOD_NOT_ALLOWED else ""
        return f"HTTP/1.1 {status} {_PHRASES[status]}\r\n{extra}Content-Length: 0\r\n\r\n".encode()

    def _refuse(self, client: socket.socket, status: int) -> None:
        """Send a refusal, then drain what the client still sends so the reply is not reset."""
        with contextlib.suppress(OSError):  # the client may have gone; the decision is logged
            client.sendall(self._response(status))
            client.shutdown(socket.SHUT_WR)
            client.settimeout(0.2)
            drained = 0
            while drained < DRAIN_LIMIT and (chunk := client.recv(4096)):
                drained += len(chunk)


def _close(sock: socket.socket) -> None:
    """Shut a socket down and close it; an already closed socket is fine."""
    with contextlib.suppress(OSError):
        sock.shutdown(socket.SHUT_RDWR)
    with contextlib.suppress(OSError):
        sock.close()
