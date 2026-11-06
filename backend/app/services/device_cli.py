"""Interactive CLI sessions to devices over SSH or telnet, optionally through a jump host.

Commands are sent one at a time and each waits for the device prompt, so a rejected line stops the script
before the next one lands (`run(..., check=True)`). Host keys are trusted on first connect and enforced
afterwards (known_hosts file). Telnet is a small client of our own: telnetlib left the stdlib in 3.13.
"""

import logging
import re
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

import paramiko

from ..config import settings

log = logging.getLogger(__name__)

Transport = Literal["ssh", "telnet"]

PAGING_OFF: dict[str, list[str]] = {
    "vrp": ["screen-length 0 temporary"],
    "zte": ["terminal length 0"],
    "iosxr": ["terminal length 0", "terminal width 0"],
    "fiberhome": ["terminal length 0"],
}

# Device replies that mean the line was not accepted; the script stops there.
ERROR_PATTERNS = [re.compile(p, re.I | re.M) for p in (
    r"^\s*Error\s*:", r"^\s*%\s*(Invalid|Incomplete|Ambiguous|Unknown|Unrecognized|Error)", r"^%Error",
    r"Unrecognized command", r"Wrong parameter", r"Failed to commit", r"syntax error", r"Command rejected",
    r"^\s*\^\s*$", r"Invalid input", r"command not found", r"%\s*Failed",
)]
CONFIRM = re.compile(r"(\[\s*(Y/N|y/n|yes/no)\s*\]|\(y/n\)|continue\?)\s*:?\s*$", re.I)
MORE = re.compile(r"(--+\s*More\s*--+|<--- More --->|Press any key)", re.I)
PROMPT = re.compile(r"(?:^|\n)\S{1,160}[>#\]]\s?$")
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b[()][AB012]|\r")
USER_PROMPT = re.compile(r"(user ?name|login)\s*:\s*$", re.I)
PASS_PROMPT = re.compile(r"password\s*:\s*$", re.I)


class CliError(RuntimeError):
    pass


class CommandRejected(CliError):
    def __init__(self, command: str, reply: str):
        super().__init__(f"Perangkat menolak perintah: {command}")
        self.command, self.reply = command, reply


@dataclass
class Target:
    host: str
    protocol: Transport
    username: str
    password: str
    port: int | None = None
    dialect: str | None = None


@dataclass
class Jump:
    host: str
    port: int
    username: str
    password: str


class Stream(Protocol):
    def send(self, data: str) -> None: ...
    def read(self, timeout: float) -> str: ...
    def close(self) -> None: ...


# --------------------------------------------------------------------------- transports


class _RecordNewHostKey(paramiko.AutoAddPolicy):
    """Records an unknown key in known_hosts; a different key later raises BadHostKeyException. Nothing checks the
    first key, so it goes to the log with its fingerprint to be compared with the device if ever in doubt."""

    def missing_host_key(self, client, hostname, key) -> None:
        log.warning("SSH: host key baru dicatat tanpa verifikasi: %s %s %s", hostname, key.get_name(), key.fingerprint)
        super().missing_host_key(client, hostname, key)


def _ssh_client(known_hosts: Path) -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    known_hosts.parent.mkdir(parents=True, exist_ok=True)
    known_hosts.touch(exist_ok=True)
    client.load_host_keys(str(known_hosts))
    client.set_missing_host_key_policy(_RecordNewHostKey() if settings.device_ssh_trust_new_hosts
                                       else paramiko.RejectPolicy())
    return client


class _SshStream:
    def __init__(self, target: Target, known_hosts: Path, timeout: float, jump: Jump | None):
        self._clients: list[paramiko.SSHClient] = []
        sock = None
        if jump:
            hop = _ssh_client(known_hosts)
            self._clients.append(hop)
            hop.connect(jump.host, jump.port, jump.username, jump.password, timeout=timeout,
                        allow_agent=False, look_for_keys=False)
            sock = hop.get_transport().open_channel("direct-tcpip", (target.host, target.port or 22), ("127.0.0.1", 0))
        client = _ssh_client(known_hosts)
        self._clients.append(client)
        client.connect(target.host, target.port or 22, target.username, target.password, timeout=timeout, sock=sock,
                       allow_agent=False, look_for_keys=False, banner_timeout=timeout, auth_timeout=timeout)
        self._chan = client.invoke_shell(term="vt100", width=511, height=1000)

    def send(self, data: str) -> None:
        self._chan.sendall(data.encode())

    def read(self, timeout: float) -> str:
        self._chan.settimeout(timeout)
        try:
            data = self._chan.recv(65535)
        except socket.timeout:
            return ""
        if not data:
            raise CliError("Sesi SSH ditutup oleh perangkat.")
        return data.decode("utf-8", "replace")

    def close(self) -> None:
        for client in reversed(self._clients):
            client.close()


IAC, DONT, DO, WONT, WILL, SB, SE = 255, 254, 253, 252, 251, 250, 240
ECHO, SGA = 1, 3


class TelnetStream:
    """Telnet over a socket (or a jump host channel): refuses every option but echo/suppress-go-ahead."""

    def __init__(self, sock):
        self._sock = sock
        self._pending = b""
        self._hop: paramiko.SSHClient | None = None

    @classmethod
    def connect(cls, target: Target, known_hosts: Path, timeout: float, jump: Jump | None) -> "TelnetStream":
        if not jump:
            return cls(socket.create_connection((target.host, target.port or 23), timeout=timeout))
        hop = _ssh_client(known_hosts)
        hop.connect(jump.host, jump.port, jump.username, jump.password, timeout=timeout,
                    allow_agent=False, look_for_keys=False)
        stream = cls(hop.get_transport().open_channel("direct-tcpip", (target.host, target.port or 23), ("127.0.0.1", 0)))
        stream._hop = hop
        return stream

    def _negotiate(self, data: bytes) -> bytes:
        out, i, data = bytearray(), 0, self._pending + data
        self._pending = b""
        while i < len(data):
            byte = data[i]
            if byte != IAC:
                out.append(byte)
                i += 1
                continue
            if i + 1 >= len(data):
                self._pending = data[i:]
                break
            command = data[i + 1]
            if command == IAC:
                out.append(IAC)
                i += 2
            elif command in (DO, DONT, WILL, WONT):
                if i + 2 >= len(data):
                    self._pending = data[i:]
                    break
                option = data[i + 2]
                if command == DO:
                    self._sock.sendall(bytes([IAC, WILL if option == SGA else WONT, option]))
                elif command == WILL:
                    self._sock.sendall(bytes([IAC, DO if option in (ECHO, SGA) else DONT, option]))
                i += 3
            elif command == SB:
                end = data.find(bytes([IAC, SE]), i)
                if end < 0:
                    self._pending = data[i:]
                    break
                i = end + 2
            else:
                i += 2
        return bytes(out)

    def send(self, data: str) -> None:
        self._sock.sendall(data.replace("\n", "\r\n").encode())

    def read(self, timeout: float) -> str:
        self._sock.settimeout(timeout)
        try:
            data = self._sock.recv(65535)
        except socket.timeout:
            return ""
        if not data:
            raise CliError("Sesi telnet ditutup oleh perangkat.")
        return self._negotiate(data).decode("utf-8", "replace")

    def close(self) -> None:
        self._sock.close()
        if self._hop:
            self._hop.close()


def open_stream(target: Target, known_hosts: Path, timeout: float, jump: Jump | None = None) -> Stream:
    try:
        if target.protocol == "ssh":
            return _SshStream(target, known_hosts, timeout, jump)
        return TelnetStream.connect(target, known_hosts, timeout, jump)
    except paramiko.BadHostKeyException as exc:
        raise CliError(f"Host key {target.host} berbeda dari yang tercatat: koneksi dihentikan (cek perangkat, "
                       "lalu hapus entri lama di known_hosts bila memang diganti).") from exc
    except paramiko.AuthenticationException as exc:
        raise CliError("Login ditolak: username/password perangkat salah.") from exc
    except (OSError, paramiko.SSHException) as exc:
        raise CliError(f"Tidak bisa terhubung ke {target.host}: {exc}") from exc


# --------------------------------------------------------------------------- session


class CliSession:
    def __init__(self, stream: Stream, target: Target, log: Callable[[str], None], timeout: float):
        self._stream, self._target, self._timeout = stream, target, timeout
        self._log = log
        self._name: str | None = None  # device name inside its prompt, learned at login

    def _is_prompt(self, tail: str) -> bool:
        """A prompt line that also carries the device's own name, so "[OK]" in show output is not taken for one."""
        if not PROMPT.search(tail):
            return False
        return self._name is None or self._name in tail.rsplit("\n", 1)[-1]

    @staticmethod
    def prompt_name(line: str) -> str:
        """'<JKT-PE1>' / '[~JKT-PE1-vsi-x]' / 'RP/0/RSP0/CPU0:JKT-PE1#' / 'JKT-PE1(config)#' -> 'JKT-PE1'."""
        name = line.strip().rsplit(":", 1)[-1]
        name = re.sub(r"^[<\[~*]+|[>#\]]\s*$", "", name)
        return name.split("(", 1)[0][:40]

    def _emit(self, text: str) -> None:
        if self._target.password:
            text = text.replace(self._target.password, "********")
        self._log(text)

    def _read_until(self, done: Callable[[str], bool], timeout: float | None = None) -> str:
        deadline = time.monotonic() + (timeout or self._timeout)
        buffer = ""
        while time.monotonic() < deadline:
            chunk = self._stream.read(0.3)
            if chunk:
                chunk = ANSI.sub("", chunk)
                buffer += chunk
                self._emit(chunk)
                if MORE.search(buffer[-80:]):
                    buffer = MORE.sub("", buffer)
                    self._stream.send(" ")
                    continue
            tail = buffer.rstrip("\n")
            if tail and done(tail):
                return buffer
        raise CliError(f"Tidak ada prompt dari perangkat dalam {timeout or self._timeout:g} detik.")

    def login(self) -> None:
        """Telnet asks for credentials in-band; SSH has already authenticated, only the prompt is awaited."""
        if self._target.protocol == "telnet":
            self._read_until(lambda t: bool(USER_PROMPT.search(t) or PASS_PROMPT.search(t)))
            self._stream.send(self._target.username + "\n")
            self._read_until(lambda t: bool(PASS_PROMPT.search(t)))
            self._stream.send(self._target.password + "\n")
            reply = self._read_until(lambda t: bool(PROMPT.search(t) or USER_PROMPT.search(t)))
            if USER_PROMPT.search(reply.rstrip()):
                raise CliError("Login ditolak: username/password perangkat salah.")
        else:
            self._stream.send("\n")
            reply = self._read_until(lambda t: bool(PROMPT.search(t)))
        self._name = self.prompt_name(reply.rstrip("\n").rsplit("\n", 1)[-1]) or None
        for command in PAGING_OFF.get(self._target.dialect or "", []):
            self.run(command, check=False)

    def run(self, command: str, check: bool = True) -> str:
        self._stream.send(command + "\n")
        reply = self._read_until(lambda t: self._is_prompt(t) or bool(CONFIRM.search(t)))
        if CONFIRM.search(reply.rstrip()):
            raise CommandRejected(command, "Perangkat meminta konfirmasi; script dihentikan agar dijawab manual.")
        if check:
            body = reply.split("\n", 1)[1] if "\n" in reply else ""  # skip the echoed command
            if any(p.search(body) for p in ERROR_PATTERNS):
                raise CommandRejected(command, body)
        return reply

    def close(self) -> None:
        self._stream.close()
