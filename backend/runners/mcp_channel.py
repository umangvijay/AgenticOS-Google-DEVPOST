"""Bounded JSON-RPC stdio client over Docker's multiplexed exec stream.

This module runs only in the trusted supervisor. Generated programs receive
ordinary stdin/stdout, no Docker socket, validator code or signing key.
"""
import json
import socket
import struct
import time


class DockerMCPChannel:
    def __init__(self, stream, *, max_bytes, deadline):
        self.socket = getattr(stream, "_sock", stream)
        self.max_bytes = max_bytes
        self.deadline = deadline
        self.received = 0
        self.stdout = bytearray()
        self.sequence = 0
        self.server_request_handler = None

    def _read(self, size):
        chunks = bytearray()
        while len(chunks) < size:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("MCP response exceeded execution deadline")
            self.socket.settimeout(min(remaining, 10))
            chunk = self.socket.recv(size - len(chunks))
            if not chunk:
                raise EOFError("MCP server closed before replying")
            chunks.extend(chunk)
        return bytes(chunks)

    def receive(self):
        while b"\n" not in self.stdout:
            header = self._read(8)
            stream_id, size = header[0], struct.unpack(">I", header[4:])[0]
            if stream_id not in (1, 2) or header[1:4] != b"\0\0\0":
                raise ValueError("Invalid container stream frame")
            self.received += size
            if self.received > self.max_bytes:
                raise ValueError("MCP output limit exceeded")
            chunk = self._read(size)
            if stream_id == 1:
                self.stdout.extend(chunk)
        line, _, rest = self.stdout.partition(b"\n")
        self.stdout = bytearray(rest)
        message = json.loads(line)
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            raise ValueError("Invalid MCP JSON-RPC response")
        return message

    def send(self, message):
        encoded = (json.dumps(message, allow_nan=False, separators=(",", ":")) + "\n").encode()
        if len(encoded) > self.max_bytes:
            raise ValueError("MCP input limit exceeded")
        self.socket.settimeout(max(.01, min(10, self.deadline - time.monotonic())))
        self.socket.sendall(encoded)

    def notify(self, method):
        self.send({"jsonrpc": "2.0", "method": method})

    def request(self, method, params=None):
        self.sequence += 1
        self.send({"jsonrpc": "2.0", "id": self.sequence, "method": method, "params": params or {}})
        # Unnegotiated server requests cannot cause host-side actions.
        for _ in range(32):
            response = self.receive()
            if "method" in response:
                if "id" in response:
                    reply = {"jsonrpc": "2.0", "id": response["id"]}
                    if self.server_request_handler and method == "tools/call":
                        try:
                            reply["result"] = self.server_request_handler(response)
                        except Exception as exc:
                            reply["error"] = {"code": -32000, "message": str(exc)[:300]}
                    else:
                        reply["error"] = {"code": -32601, "message": "Client capability not available"}
                    self.send(reply)
                continue
            if response.get("id") != self.sequence or (("result" in response) == ("error" in response)):
                raise ValueError("MCP response identity or envelope mismatch")
            return response
        raise ValueError("MCP unsolicited message limit exceeded")

    def close_input(self):
        self.socket.shutdown(socket.SHUT_WR)
