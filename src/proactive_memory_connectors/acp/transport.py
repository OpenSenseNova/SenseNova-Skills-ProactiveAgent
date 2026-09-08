"""Minimal synchronous ACP v1 transport over stdio and JSON-RPC 2.0."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any


class AcpTransportError(RuntimeError):
    """Base error for ACP process and transport failures."""


class AcpProtocolError(AcpTransportError):
    """Raised when the peer sends an invalid or unexpected JSON-RPC message."""


class AcpRemoteError(AcpTransportError):
    """Raised when the ACP Agent returns a JSON-RPC error response."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(f"ACP Agent error {code}: {message}")
        self.code = code
        self.message = message
        self.data = data


NotificationHandler = Callable[[str, Mapping[str, Any]], None]


class StdioJsonRpcTransport:
    """Launch one ACP Agent and exchange newline-delimited JSON-RPC messages.

    The first implementation intentionally supports one in-flight request. That
    is enough to prove one complete prompt turn without adding concurrency or
    Harness-specific behavior to the protocol layer.
    """

    def __init__(
        self,
        command: Sequence[str | Path],
        *,
        cwd: str | Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        if not command:
            raise ValueError("command must not be empty")
        self.command = tuple(str(part) for part in command)
        self.cwd = None if cwd is None else Path(cwd)
        self.env = None if env is None else dict(env)
        self._process: subprocess.Popen[str] | None = None
        self._next_request_id = 0

    def start(self) -> None:
        if self._process is not None:
            if self._process.poll() is None:
                return
            raise AcpTransportError("ACP Agent process has already exited")

        try:
            self._process = subprocess.Popen(
                self.command,
                cwd=None if self.cwd is None else str(self.cwd),
                env=self.env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                # ACP reserves stdout for protocol traffic and allows normal
                # diagnostic logging on stderr, so stderr remains visible.
                stderr=None,
                text=True,
                encoding="utf-8",
                bufsize=1,
            )
        except OSError as exc:
            raise AcpTransportError(f"failed to start ACP Agent: {exc}") from exc

    def request(
        self,
        method: str,
        params: Mapping[str, Any],
        *,
        on_notification: NotificationHandler | None = None,
    ) -> Any:
        if not method.strip():
            raise ValueError("method must be a non-empty string")
        process = self._require_process()
        request_id = self._next_request_id
        self._next_request_id += 1
        self._write_message(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": dict(params),
            }
        )

        while True:
            message = self._read_message(process)
            if "method" in message:
                self._handle_agent_message(message, on_notification)
                continue

            if message.get("id") != request_id:
                raise AcpProtocolError(
                    "received a response for an unexpected request id"
                )
            if "error" in message:
                self._raise_remote_error(message["error"])
            if "result" not in message:
                raise AcpProtocolError("JSON-RPC response is missing result")
            return message["result"]

    def notify(self, method: str, params: Mapping[str, Any]) -> None:
        if not method.strip():
            raise ValueError("method must be a non-empty string")
        self._require_process()
        self._write_message(
            {"jsonrpc": "2.0", "method": method, "params": dict(params)}
        )

    def close(self) -> None:
        process = self._process
        self._process = None
        if process is None:
            return
        try:
            if process.stdin is not None and not process.stdin.closed:
                try:
                    process.stdin.close()
                except OSError:
                    pass

            if process.poll() is None:
                try:
                    process.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    try:
                        process.wait(timeout=0.5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=0.5)
        finally:
            if process.stdout is not None and not process.stdout.closed:
                process.stdout.close()

    def __enter__(self) -> StdioJsonRpcTransport:
        self.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _require_process(self) -> subprocess.Popen[str]:
        if self._process is None:
            self.start()
        assert self._process is not None
        if self._process.poll() is not None:
            raise AcpTransportError("ACP Agent process is not running")
        return self._process

    def _write_message(self, message: Mapping[str, Any]) -> None:
        process = self._require_process()
        if process.stdin is None:
            raise AcpTransportError("ACP Agent stdin is unavailable")
        rendered = json.dumps(
            message,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        try:
            process.stdin.write(f"{rendered}\n")
            process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise AcpTransportError("failed to write to ACP Agent") from exc

    def _read_message(
        self,
        process: subprocess.Popen[str],
    ) -> dict[str, Any]:
        if process.stdout is None:
            raise AcpTransportError("ACP Agent stdout is unavailable")
        line = process.stdout.readline()
        if line == "":
            return_code = process.poll()
            raise AcpTransportError(
                f"ACP Agent closed stdout before responding (exit={return_code})"
            )
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            raise AcpProtocolError("ACP Agent emitted invalid JSON on stdout") from exc
        if not isinstance(message, dict):
            raise AcpProtocolError("JSON-RPC message must be an object")
        if message.get("jsonrpc") != "2.0":
            raise AcpProtocolError("JSON-RPC version must be 2.0")
        return message

    def _handle_agent_message(
        self,
        message: Mapping[str, Any],
        on_notification: NotificationHandler | None,
    ) -> None:
        method = message.get("method")
        if not isinstance(method, str) or not method:
            raise AcpProtocolError("JSON-RPC method must be a non-empty string")
        raw_params = message.get("params", {})
        if not isinstance(raw_params, Mapping):
            raise AcpProtocolError("JSON-RPC params must be an object")

        if "id" in message:
            # Client-side methods such as permission requests are deliberately
            # outside this first slice. Replying prevents the Agent from hanging.
            self._write_message(
                {
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "error": {
                        "code": -32601,
                        "message": f"client method is not implemented: {method}",
                    },
                }
            )
            return
        if on_notification is not None:
            on_notification(method, raw_params)

    @staticmethod
    def _raise_remote_error(raw_error: Any) -> None:
        if not isinstance(raw_error, Mapping):
            raise AcpProtocolError("JSON-RPC error must be an object")
        code = raw_error.get("code")
        message = raw_error.get("message")
        if isinstance(code, bool) or not isinstance(code, int):
            raise AcpProtocolError("JSON-RPC error code must be an integer")
        if not isinstance(message, str) or not message:
            raise AcpProtocolError("JSON-RPC error message must be a string")
        raise AcpRemoteError(code, message, raw_error.get("data"))
