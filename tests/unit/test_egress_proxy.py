"""Tests of the CONNECT allowlist proxy against a local fake upstream on loopback."""

import socket
import threading
import unittest
from collections.abc import Callable
from dataclasses import replace
from typing import Any
from unittest import mock

from agents_dispatcher.adapters.system.egress_proxy import ConnectAllowlistProxy, ProxyConfig
from agents_dispatcher.application.ports import EgressEndpoint
from tests.support.run_spies import RUN_ID

TIMEOUT_S = 5.0


class EchoUpstream:
    """A loopback TCP server that echoes bytes and counts the connections it accepted."""

    def __init__(self) -> None:
        """Listen on an ephemeral loopback port."""
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.bind(("127.0.0.1", 0))
        self.server.listen()
        self.port: int = self.server.getsockname()[1]
        self.accepted = 0
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        """Accept connections and echo each one in its own thread."""
        while True:
            try:
                connection, _ = self.server.accept()
            except OSError:
                return
            self.accepted += 1
            threading.Thread(target=self._echo, args=(connection,), daemon=True).start()

    @staticmethod
    def _echo(connection: socket.socket) -> None:
        """Echo until the peer closes."""
        with connection:
            while data := connection.recv(4096):
                connection.sendall(data)

    def close(self) -> None:
        """Stop listening."""
        self.server.close()


def request(endpoint: EgressEndpoint, head: bytes) -> tuple[socket.socket, str, bytes]:
    """Send a request head and return the socket, the status line and any bytes after the head."""
    client = socket.create_connection((endpoint.host, endpoint.port), timeout=TIMEOUT_S)
    client.sendall(head)
    received = b""
    while b"\r\n\r\n" not in received:
        chunk = client.recv(4096)
        if not chunk:
            break
        received += chunk
    status, _, rest = received.partition(b"\r\n")
    _, _, tail = rest.partition(b"\r\n\r\n")
    return client, status.decode("latin-1"), tail


class ProxyCase(unittest.TestCase):
    """A proxy that allows localhost on the upstream port, with a fake upstream."""

    def setUp(self) -> None:
        """Start the upstream and describe the default configuration."""
        self.upstream = EchoUpstream()
        self.addCleanup(self.upstream.close)
        self.config = ProxyConfig(
            hosts=("localhost",),
            ports=(self.upstream.port,),
            allow_nonpublic=True,
            header_bytes=16384,
            connections=8,
            buffer_bytes=65536,
            idle_s=30.0,
            lifetime_s=60.0,
            connect_s=5.0,
        )

    def start(
        self, config: ProxyConfig | None = None
    ) -> tuple[ConnectAllowlistProxy, EgressEndpoint]:
        """Start a proxy and register its cleanup."""
        proxy = ConnectAllowlistProxy(config or self.config)
        endpoint = proxy.start(RUN_ID)
        self.addCleanup(proxy.stop)
        return proxy, endpoint

    def connect_line(self, host: str, port: int | None = None) -> bytes:
        """Build a CONNECT request head."""
        target = f"{host}:{port if port is not None else self.upstream.port}"
        return f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode("latin-1")


class AllowedTests(ProxyCase):
    """What passes."""

    def test_an_allowed_destination_is_tunnelled_in_both_directions(self) -> None:
        """Bytes sent by the client reach the upstream and come back."""
        _, endpoint = self.start()
        client, status, _ = request(endpoint, self.connect_line("localhost"))
        self.addCleanup(client.close)
        self.assertIn(" 200 ", status, msg="connection established")
        client.sendall(b"ping")
        self.assertEqual(client.recv(16), b"ping", msg="echo through the tunnel")

    def test_the_host_comparison_is_exact_and_case_insensitive(self) -> None:
        """A different case of the same name passes; a trailing dot is normalised."""
        _, endpoint = self.start()
        client, status, _ = request(endpoint, self.connect_line("LocalHost."))
        self.addCleanup(client.close)
        self.assertIn(" 200 ", status, msg="normalised name allowed")

    def test_the_endpoint_is_on_loopback(self) -> None:
        """The proxy never listens on a public interface."""
        _, endpoint = self.start()
        self.assertEqual(endpoint.host, "127.0.0.1", msg="loopback address")
        self.assertGreater(endpoint.port, 0, msg="an ephemeral port")


class RefusedTests(ProxyCase):
    """What is refused, and that the upstream never sees it."""

    def assert_refused(self, head: bytes, code: str, why: str) -> None:
        """Send `head`, expect the status `code`, and expect the upstream untouched."""
        proxy, endpoint = self.start()
        client, status, _ = request(endpoint, head)
        self.addCleanup(client.close)
        self.assertIn(f" {code} ", status, msg=f"{why}: status")
        self.assertEqual(self.upstream.accepted, 0, msg=f"{why}: upstream was not contacted")
        self.assertEqual([d.allowed for d in proxy.decisions()], [False], msg=f"{why}: logged")

    def test_a_host_outside_the_allowlist_is_refused(self) -> None:
        """Only the configured names pass."""
        self.assert_refused(self.connect_line("evil.example"), "403", "other host")

    def test_a_suffix_or_prefix_of_an_allowed_name_is_refused(self) -> None:
        """No substring or subdomain matching."""
        self.assert_refused(self.connect_line("sub.localhost"), "403", "subdomain")

    def test_a_userinfo_trick_is_refused(self) -> None:
        """The host is what follows the last at-sign check, never a prefix."""
        self.assert_refused(self.connect_line("localhost@evil.example"), "403", "userinfo")

    def test_a_port_outside_the_allowlist_is_refused(self) -> None:
        """The host alone is not enough."""
        self.assert_refused(self.connect_line("localhost", self.upstream.port + 1), "403", "port")

    def test_plain_http_is_refused(self) -> None:
        """Only CONNECT tunnels are served."""
        head = f"GET http://localhost:{self.upstream.port}/ HTTP/1.1\r\nHost: localhost\r\n\r\n"
        self.assert_refused(head.encode(), "405", "plain http")

    def test_a_malformed_request_line_is_refused(self) -> None:
        """Garbage gets 400."""
        self.assert_refused(b"CONNECT\r\n\r\n", "400", "malformed")

    def test_non_public_addresses_are_refused_by_default(self) -> None:
        """The destination resolves to loopback, which only an explicit setting allows."""
        config = replace(self.config, allow_nonpublic=False)
        proxy, endpoint = self.start(config)
        client, status, _ = request(endpoint, self.connect_line("localhost"))
        self.addCleanup(client.close)
        self.assertIn(" 403 ", status, msg="refused")
        self.assertEqual(self.upstream.accepted, 0, msg="upstream untouched")
        self.assertIn("non-public", proxy.decisions()[0].reason, msg="reason is logged")

    def test_an_oversized_header_is_refused(self) -> None:
        """The head is bounded by proxy.header_bytes."""
        config = replace(self.config, header_bytes=64)
        _, endpoint = self.start(config)
        padding = "X-Pad: " + "a" * 200 + "\r\n"
        head = f"CONNECT localhost:{self.upstream.port} HTTP/1.1\r\n{padding}\r\n".encode()
        client, status, _ = request(endpoint, head)
        self.addCleanup(client.close)
        self.assertIn(" 431 ", status, msg="request header fields too large")


class LimitTests(ProxyCase):
    """Connection, idle and lifetime bounds, and stop."""

    def test_connections_beyond_the_limit_are_refused(self) -> None:
        """A second tunnel while the first is open gets 503."""
        _, endpoint = self.start(replace(self.config, connections=1))
        first, status, _ = request(endpoint, self.connect_line("localhost"))
        self.addCleanup(first.close)
        self.assertIn(" 200 ", status, msg="first tunnel")
        second, status, _ = request(endpoint, self.connect_line("localhost"))
        self.addCleanup(second.close)
        self.assertIn(" 503 ", status, msg="second tunnel refused")

    def test_an_idle_tunnel_is_closed(self) -> None:
        """No traffic for idle_s closes the tunnel."""
        _, endpoint = self.start(replace(self.config, idle_s=0.3))
        client, _, _ = request(endpoint, self.connect_line("localhost"))
        self.addCleanup(client.close)
        self.assertEqual(client.recv(16), b"", msg="closed by the proxy after the idle bound")

    def test_a_tunnel_is_closed_at_its_lifetime_even_when_busy(self) -> None:
        """Traffic does not extend the lifetime."""
        _, endpoint = self.start(replace(self.config, lifetime_s=0.5))
        client, _, _ = request(endpoint, self.connect_line("localhost"))
        self.addCleanup(client.close)
        closed = False
        for _ in range(60):
            try:
                client.sendall(b"x")
                if client.recv(16) == b"":
                    closed = True
                    break
            except OSError:
                closed = True
                break
            threading.Event().wait(0.05)
        self.assertTrue(closed, msg="the tunnel ended although traffic kept flowing")

    def test_stop_closes_the_listener_and_open_tunnels(self) -> None:
        """After stop nothing is served."""
        proxy, endpoint = self.start()
        client, _, _ = request(endpoint, self.connect_line("localhost"))
        self.addCleanup(client.close)
        proxy.stop()
        self.assertEqual(client.recv(16), b"", msg="open tunnel closed")
        with self.assertRaises(OSError, msg="listener closed"):
            socket.create_connection((endpoint.host, endpoint.port), timeout=TIMEOUT_S)


def closed_port() -> int:
    """Return a loopback port with nothing listening on it."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class UpstreamFailureTests(ProxyCase):
    """ER-3 (b): a failed upstream connect is explained, not dropped."""

    def test_the_connect_error_reaches_the_refusal_and_the_log(self) -> None:
        """A closed upstream port gives 502 and the operating system's reason in the log."""
        port = closed_port()
        proxy, endpoint = self.start(replace(self.config, ports=(port,)))
        client, status, _ = request(endpoint, self.connect_line("localhost", port))
        self.addCleanup(client.close)
        self.assertIn(" 502 ", status, msg="bad gateway")
        reason = proxy.decisions()[0].reason
        self.assertIn("refused", reason.lower(), msg=f"the connect error is in the log: {reason}")


class ConnectTimeoutTests(ProxyCase):
    """ER-3 (a): the connect timeout is a setting."""

    def test_the_configured_connect_timeout_is_used(self) -> None:
        """create_connection receives `connect_s`, not a constant."""
        seen: list[float | None] = []
        real = socket.create_connection

        def spy(
            address: tuple[str, int], timeout: float | None = None, **kwargs: object
        ) -> socket.socket:
            seen.append(timeout)
            return real(address, timeout=timeout, **kwargs)  # type: ignore[arg-type]

        _, endpoint = self.start(replace(self.config, connect_s=3.5))
        with mock.patch("socket.create_connection", spy):
            client, _, _ = request(endpoint, self.connect_line("localhost"))
        self.addCleanup(client.close)
        self.assertIn(3.5, seen, msg="the upstream connect used the configured timeout")


def resolving(
    table: dict[str, list[str]],
) -> Callable[..., list[tuple[int, int, int, str, tuple[str, int]]]]:
    """Build a getaddrinfo stand-in that answers for the names in `table` and defers otherwise."""
    real = socket.getaddrinfo

    def fake(host: str, port: int, *args: object, **kwargs: object) -> list[Any]:
        if host in table:
            return [
                (
                    socket.AF_INET6 if ":" in addr else socket.AF_INET,
                    socket.SOCK_STREAM,
                    6,
                    "",
                    (addr, port),
                )
                for addr in table[host]
            ]
        return real(host, port, *args, **kwargs)  # type: ignore[arg-type]

    return fake


class AddressValidationTests(ProxyCase):
    """ER-3 (c): mapped addresses are judged as their IPv4 form and the peer is verified."""

    def test_an_ipv4_mapped_loopback_address_is_non_public(self) -> None:
        """::ffff:127.0.0.1 must not slip past the address-class check."""
        config = replace(self.config, hosts=("mapped.test",), allow_nonpublic=False)
        proxy, endpoint = self.start(config)
        fake = resolving({"mapped.test": ["::ffff:127.0.0.1"]})
        with mock.patch("socket.getaddrinfo", fake):
            client, status, _ = request(endpoint, self.connect_line("mapped.test"))
        self.addCleanup(client.close)
        self.assertIn(" 403 ", status, msg="refused as non-public")
        self.assertIn("non-public", proxy.decisions()[0].reason, msg="reason is logged")
        self.assertEqual(self.upstream.accepted, 0, msg="upstream untouched")

    def test_a_mapped_address_is_connected_in_its_ipv4_form(self) -> None:
        """The validated and the connected address are the same unmapped IPv4 address."""
        config = replace(self.config, hosts=("mapped.test",), allow_nonpublic=False)
        _, endpoint = self.start(config)
        fake = resolving({"mapped.test": ["::ffff:93.184.216.34"]})
        seen: list[str] = []

        real = socket.create_connection

        def refuse(address: tuple[str, int], *args: object, **kwargs: object) -> socket.socket:
            if address[0] == endpoint.host:
                return real(address, *args, **kwargs)  # type: ignore[arg-type]
            seen.append(address[0])
            raise ConnectionRefusedError

        with mock.patch("socket.getaddrinfo", fake), mock.patch("socket.create_connection", refuse):
            client, _, _ = request(endpoint, self.connect_line("mapped.test"))
        self.addCleanup(client.close)
        self.assertEqual(seen, ["93.184.216.34"], msg="the proxy connected to the IPv4 form")

    def test_a_connection_to_a_different_peer_than_validated_is_refused(self) -> None:
        """The connected peer address must equal the address that was validated."""
        config = replace(self.config, hosts=("peer.test",), allow_nonpublic=False)
        proxy, endpoint = self.start(config)
        fake = resolving({"peer.test": ["93.184.216.34"]})
        real = socket.create_connection

        def redirect(
            address: tuple[str, int], timeout: float | None = None, **kwargs: object
        ) -> socket.socket:
            target = ("127.0.0.1", address[1]) if address[0] == "93.184.216.34" else address
            return real(target, timeout=timeout, **kwargs)  # type: ignore[arg-type]

        with (
            mock.patch("socket.getaddrinfo", fake),
            mock.patch("socket.create_connection", redirect),
        ):
            client, status, _ = request(endpoint, self.connect_line("peer.test"))
        self.addCleanup(client.close)
        self.assertNotIn(" 200 ", status, msg="no tunnel to an unvalidated peer")
        self.assertIn("peer", proxy.decisions()[0].reason, msg="the mismatch is logged")


class DecisionLogTests(ProxyCase):
    """The log of allowed and refused hosts."""

    def test_allowed_and_refused_destinations_are_both_recorded(self) -> None:
        """The evidence for the manual run lists hosts and verdicts."""
        proxy, endpoint = self.start()
        ok, _, _ = request(endpoint, self.connect_line("localhost"))
        self.addCleanup(ok.close)
        bad, _, _ = request(endpoint, self.connect_line("evil.example"))
        self.addCleanup(bad.close)
        decisions = proxy.decisions()
        self.assertEqual(
            [(d.host, d.allowed) for d in decisions],
            [("localhost", True), ("evil.example", False)],
            msg="verdicts in arrival order",
        )


if __name__ == "__main__":
    unittest.main()
