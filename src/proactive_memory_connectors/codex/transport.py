"""Small JSON-RPC transport for the Codex app-server stdio protocol."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any


class CodexTransportError(RuntimeError):
    """Base error for Codex app-server process and transport failures."""


class CodexProtocolError(CodexTransportError):
    """Raised when app-server emits an invalid JSON-RPC message."""


class CodexRemoteError(CodexTransportError):
    """Raised when app-server returns a JSON-RPC error response."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(f"Codex app-server error {code}: {message}")
        self.code = code
        self.message = message
        self.data = data


NotificationHandler = Callable[[str, Mapping[str, Any]], bool | None]


class CodexAppServerTransport:
    """Exchange newline-delimited JSON-RPC messages with ``codex app-server``.

    Codex app-server documents JSON-RPC 2.0 with the ``jsonrpc`` header omitted
    on the wire.  The parser therefore accepts both the documented form and
    explicit ``"2.0"`` messages, which also makes deterministic fixtures easy
    to use.
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
            raise CodexTransportError("Codex app-server process has already exited")
        try:
            self._process = subprocess.Popen(
                self.command,
                cwd=None if self.cwd is None else str(self.cwd),
                env=self.env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=None,
                text=True,
                encoding="utf-8",
                bufsize=1,
            )
        except OSError as exc:
            raise CodexTransportError(
                f"failed to start Codex app-server: {exc}"
            ) from exc

    def request(
        self,
        method: str,
        params: Mapping[str, Any],
        *,
        on_notification: NotificationHandler | None = None,
        until_notification: Callable[[str, Mapping[str, Any]], bool] | None = None,
    ) -> Any:
        """Send a request and optionally keep reading until a terminal event.

        ``turn/start`` returns an initial turn object while lifecycle
        notifications continue on the same stream.  ``until_notification``
        lets the client wait for the matching ``turn/completed`` event instead
        of returning at the initial response.
        """

        if not isinstance(method, str) or not method.strip():
            raise ValueError("method must be a non-empty string")
        process = self._require_process()
        request_id = self._next_request_id
        self._next_request_id += 1
        self._write_message(
            {"id": request_id, "method": method, "params": dict(params)}
        )

        response: Any = _MISSING
        terminal = until_notification is None
        while True:
            message = self._read_message(process)
            if "method" in message:
                if "id" in message:
                    # App-server may ask its client for an approval or another
                    # host-side action.  Proactive Memory does not own those
                    # permissions, so fail closed instead of leaving Codex
                    # waiting forever.
                    self._handle_server_request(message)
                    continue
                notification_method, notification_params = self._notification(message)
                if on_notification is not None:
                    on_notification(notification_method, notification_params)
                if (
                    until_notification is not None
                    and until_notification(notification_method, notification_params)
                ):
                    terminal = True
                if response is not _MISSING and terminal:
                    return response
                continue

            if message.get("id") != request_id:
                raise CodexProtocolError(
                    "received a response for an unexpected request id"
                )
            if "error" in message:
                self._raise_remote_error(message["error"])
            if "result" not in message:
                raise CodexProtocolError("JSON-RPC response is missing result")
            response = message["result"]
            if terminal:
                return response

    def notify(self, method: str, params: Mapping[str, Any]) -> None:
        if not isinstance(method, str) or not method.strip():
            raise ValueError("method must be a non-empty string")
        self._require_process()
        self._write_message({"method": method, "params": dict(params)})

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

    def __enter__(self) -> CodexAppServerTransport:
        self.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _require_process(self) -> subprocess.Popen[str]:
        if self._process is None:
            self.start()
        assert self._process is not None
        if self._process.poll() is not None:
            raise CodexTransportError("Codex app-server is not running")
        return self._process

    def _write_message(self, message: Mapping[str, Any]) -> None:
        process = self._require_process()
        if process.stdin is None:
            raise CodexTransportError("Codex app-server stdin is unavailable")
        rendered = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        try:
            process.stdin.write(f"{rendered}\n")
            process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise CodexTransportError("failed to write to Codex app-server") from exc

    def _read_message(self, process: subprocess.Popen[str]) -> dict[str, Any]:
        if process.stdout is None:
            raise CodexTransportError("Codex app-server stdout is unavailable")
        line = process.stdout.readline()
        if line == "":
            raise CodexTransportError(
                "Codex app-server closed stdout before responding "
                f"(exit={process.poll()})"
            )
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            raise CodexProtocolError(
                "Codex app-server emitted invalid JSON on stdout"
            ) from exc
        if not isinstance(message, dict):
            raise CodexProtocolError("JSON-RPC message must be an object")
        version = message.get("jsonrpc")
        if version is not None and version != "2.0":
            raise CodexProtocolError("JSON-RPC version must be 2.0 when present")
        return message

    @staticmethod
    def _notification(message: Mapping[str, Any]) -> tuple[str, Mapping[str, Any]]:
        method = message.get("method")
        if not isinstance(method, str) or not method:
            raise CodexProtocolError("JSON-RPC method must be a non-empty string")
        params = message.get("params", {})
        if not isinstance(params, Mapping):
            raise CodexProtocolError("JSON-RPC params must be an object")
        return method, params

    def _handle_server_request(self, message: Mapping[str, Any]) -> None:
        request_id = message.get("id")
        method, _params = self._notification(message)
        self._write_message(
            {
                "id": request_id,
                "error": {
                    "code": -32601,
                    "message": f"client method is not implemented: {method}",
                },
            }
        )

    @staticmethod
    def _raise_remote_error(raw_error: Any) -> None:
        if not isinstance(raw_error, Mapping):
            raise CodexProtocolError("JSON-RPC error must be an object")
        code = raw_error.get("code")
        message = raw_error.get("message")
        if isinstance(code, bool) or not isinstance(code, int):
            raise CodexProtocolError("JSON-RPC error code must be an integer")
        if not isinstance(message, str) or not message:
            raise CodexProtocolError("JSON-RPC error message must be a string")
        raise CodexRemoteError(code, message, raw_error.get("data"))


class _Missing:
    pass


_MISSING = _Missing()
