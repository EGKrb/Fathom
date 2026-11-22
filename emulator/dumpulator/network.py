# Fathom addition to dumpulator: the network of the emulated program, at the level of its APIs
# (ws2_32, wininet, winhttp, urlmon). Boost Software License 1.0, like the rest of dumpulator
# (LICENSE.dumpulator).
#
# Two modes. "simulated" (the default, safe for a suspicious program): names resolve to addresses of
# the documentation range 192.0.2.0/24, one per name; connections succeed; what the program sends is
# recorded; an HTTP request gets a 200 response with a configurable body; other protocols get the
# connection closed by the peer. "real": the sockets and HTTP requests go out through the host
# (Python's socket and urllib), for a program one trusts. Every operation is recorded (events).

import hashlib
import socket as _socket
import struct
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .apihooks import read_string, set_last_error, to_signed

AF_INET, AF_INET6, SOCK_STREAM, SOCK_DGRAM = 2, 23, 1, 2
SOCKET_ERROR = 0xFFFFFFFF
INVALID_SOCKET = 0xFFFFFFFFFFFFFFFF
WSAEWOULDBLOCK, WSAENOTSOCK, WSAECONNREFUSED, WSAETIMEDOUT, WSAHOST_NOT_FOUND = 10035, 10038, 10061, 10060, 11001
ERROR_INTERNET_NAME_NOT_RESOLVED, ERROR_INTERNET_CANNOT_CONNECT, ERROR_INVALID_HANDLE = 12007, 12029, 6
DEFAULT_BODY = b"<html><head><title>Fathom</title></head><body>Simulated response of the Fathom emulator</body></html>\n"


@dataclass
class Socket:
    handle: int
    family: int
    kind: int
    peer: Optional[tuple] = None
    host: Optional[str] = None
    sent: bytearray = field(default_factory=bytearray)
    inbox: bytearray = field(default_factory=bytearray)
    answered: bool = False
    real: Optional[object] = None
    nonblocking: bool = False


@dataclass
class InternetHandle:
    handle: int
    kind: str                       # session, connection, request
    server: str = ""
    port: int = 80
    secure: bool = False
    method: str = "GET"
    path: str = "/"
    headers: str = ""
    response: Optional[bytes] = None
    status: int = 0
    response_headers: str = ""
    position: int = 0


class Network:
    def __init__(self, dp, mode: str = "simulated", body: bytes = DEFAULT_BODY):
        self.dp = dp
        self.mode = mode
        self.body = body
        self.sockets: Dict[int, Socket] = {}
        self.internet: Dict[int, InternetHandle] = {}
        self.names: Dict[str, str] = {}      # address -> name (simulated DNS)
        self.next_handle = 0x40000
        self.events: List[dict] = []

    # ------------------------------------------------------------------ helpers
    def _handle(self) -> int:
        self.next_handle += 4
        return self.next_handle

    def _record(self, kind: str, **values):
        values["kind"] = kind
        values["thread"] = self.dp.thread_id
        self.events.append(values)
        self.dp.info(f"[net] {kind}: " + ", ".join(f"{k}={v!r}" for k, v in values.items() if k not in ("kind", "thread")))

    def _error(self, code: int, result: int = SOCKET_ERROR) -> int:
        set_last_error(self.dp, code)
        return result & self.dp.addr_mask if result == INVALID_SOCKET else result

    def resolve(self, name: str) -> Optional[str]:
        """The address of a name: simulated (one per name), or the host's resolver."""
        try:
            _socket.inet_aton(name)
            return name
        except OSError:
            pass
        if self.mode == "real":
            try:
                address = _socket.gethostbyname(name)
            except OSError:
                self._record("dns", name=name, address=None)
                return None
        else:
            digest = hashlib.sha256(name.lower().encode()).digest()
            address = f"192.0.2.{1 + digest[0] % 254}"
        self.names[address] = name
        self._record("dns", name=name, address=address)
        return address

    def _sockaddr(self, address: int, length: int):
        family = struct.unpack("<H", bytes(self.dp.read(address, 2)))[0]
        if family == AF_INET:
            port, ip = struct.unpack(">H4s", bytes(self.dp.read(address + 2, 6)))
            return family, _socket.inet_ntoa(ip), port
        if family == AF_INET6:
            port = struct.unpack(">H", bytes(self.dp.read(address + 2, 2)))[0]
            ip = _socket.inet_ntop(_socket.AF_INET6, bytes(self.dp.read(address + 8, 16)))
            return family, ip, port
        return family, None, 0

    def _write_sockaddr_in(self, address: int, ip: str, port: int):
        self.dp.write(address, struct.pack("<H", AF_INET) + struct.pack(">H", port) + _socket.inet_aton(ip) + bytes(8))

    # ------------------------------------------------------------------ Winsock
    def wsa_startup(self, dp, version, data):
        if dp._x64:
            blob = struct.pack("<HHHHQ", 0x202, 0x202, 0, 0, 0) + b"WinSock 2.0".ljust(257, b"\0") + b"Running".ljust(129, b"\0")
        else:
            blob = struct.pack("<HH", 0x202, 0x202) + b"WinSock 2.0".ljust(257, b"\0") + b"Running".ljust(129, b"\0") + struct.pack("<HHI", 0, 0, 0)
        dp.write(data, blob)
        self._record("wsa_startup", version=hex(version))
        return 0

    def socket(self, dp, family, kind, protocol, *rest):
        handle = self._handle()
        sock = Socket(handle, family & 0xFFFF, kind & 0xFFFF)
        if self.mode == "real":
            try:
                sock.real = _socket.socket(_socket.AF_INET6 if sock.family == AF_INET6 else _socket.AF_INET,
                                           _socket.SOCK_DGRAM if sock.kind == SOCK_DGRAM else _socket.SOCK_STREAM)
                sock.real.settimeout(20)
            except OSError:
                return self._error(10047, INVALID_SOCKET)
        self.sockets[handle] = sock
        self._record("socket", handle=hex(handle), family=family, type=kind)
        return handle

    def _get(self, handle) -> Optional[Socket]:
        sock = self.sockets.get(handle)
        if sock is None:
            set_last_error(self.dp, WSAENOTSOCK)
        return sock

    def connect(self, dp, handle, address, length, *rest):
        sock = self._get(handle)
        if sock is None:
            return SOCKET_ERROR
        family, ip, port = self._sockaddr(address, length)
        sock.peer, sock.host = (ip, port), self.names.get(ip, ip)
        self._record("connect", host=sock.host, address=ip, port=port)
        if sock.real is not None:
            try:
                sock.real.connect((ip, port))
            except OSError:
                return self._error(WSAECONNREFUSED)
        return 0

    def send(self, dp, handle, buffer, length, flags):
        sock = self._get(handle)
        if sock is None:
            return SOCKET_ERROR
        data = bytes(dp.read(buffer, length)) if length else b""
        sock.sent += data
        self._record("send", host=sock.host, port=sock.peer[1] if sock.peer else None, size=len(data), data=data[:256].decode("latin-1"))
        if sock.real is not None:
            try:
                sock.real.sendall(data)
            except OSError:
                return self._error(10054)
        return len(data)

    def _simulated_reply(self, sock: Socket):
        if sock.answered:
            return
        request = bytes(sock.sent)
        if request.split(b" ", 1)[0] in (b"GET", b"POST", b"HEAD", b"PUT", b"DELETE", b"OPTIONS", b"PATCH") and b"\r\n\r\n" in request:
            sock.answered = True
            body = b"" if request.startswith(b"HEAD") else self.body
            sock.inbox += (b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: %d\r\nConnection: close\r\n\r\n" % len(self.body)) + body

    def recv(self, dp, handle, buffer, length, flags):
        sock = self._get(handle)
        if sock is None:
            return SOCKET_ERROR
        if sock.real is not None:
            try:
                data = sock.real.recv(length)
            except _socket.timeout:
                return self._error(WSAETIMEDOUT)
            except OSError:
                return self._error(10054)
        else:
            self._simulated_reply(sock)
            data = bytes(sock.inbox[:length])
            del sock.inbox[:length]
        if data:
            dp.write(buffer, data)
        self._record("recv", host=sock.host, size=len(data))
        return len(data)

    def wsa_send(self, dp, handle, buffers, count, sent, flags, overlapped, routine):
        total = 0
        for index in range(count):
            length, pointer = self._wsabuf(buffers, index)
            result = self.send(dp, handle, pointer, length, 0)
            if result == SOCKET_ERROR:
                return SOCKET_ERROR
            total += result
        if sent:
            dp.write_ulong(sent, total)
        return 0

    def wsa_recv(self, dp, handle, buffers, count, received, flags, overlapped, routine):
        length, pointer = self._wsabuf(buffers, 0)
        result = self.recv(dp, handle, pointer, length, 0)
        if result == SOCKET_ERROR:
            return SOCKET_ERROR
        if received:
            dp.write_ulong(received, result)
        if flags:
            dp.write_ulong(flags, 0)
        return 0

    def _wsabuf(self, buffers, index):
        dp = self.dp
        if dp._x64:
            length, _, pointer = struct.unpack("<IIQ", bytes(dp.read(buffers + 16 * index, 16)))
        else:
            length, pointer = struct.unpack("<II", bytes(dp.read(buffers + 8 * index, 8)))
        return length, pointer

    def sendto(self, dp, handle, buffer, length, flags, address, address_length):
        sock = self._get(handle)
        if sock is None:
            return SOCKET_ERROR
        family, ip, port = self._sockaddr(address, address_length) if address else (0, None, 0)
        data = bytes(dp.read(buffer, length)) if length else b""
        self._record("sendto", address=ip, port=port, size=len(data), data=data[:256].hex())
        if sock.real is not None:
            try:
                sock.real.sendto(data, (ip, port))
            except OSError:
                return self._error(10054)
        return len(data)

    def recvfrom(self, dp, handle, buffer, length, flags, address, address_length):
        sock = self._get(handle)
        if sock is None:
            return SOCKET_ERROR
        if sock.real is None:
            self._record("recvfrom", size=0)
            return self._error(WSAETIMEDOUT)  # nobody answers on the simulated network
        try:
            data, (ip, port) = sock.real.recvfrom(length)
        except OSError:
            return self._error(WSAETIMEDOUT)
        dp.write(buffer, data)
        if address:
            self._write_sockaddr_in(address, ip, port)
        self._record("recvfrom", address=ip, port=port, size=len(data))
        return len(data)

    def close(self, dp, handle):
        sock = self.sockets.pop(handle, None)
        if sock is None:
            return self._error(WSAENOTSOCK)
        if sock.real is not None:
            sock.real.close()
        self._record("closesocket", handle=hex(handle), host=sock.host)
        return 0

    def select(self, dp, nfds, readfds, writefds, exceptfds, timeout):
        count = 0
        for fds in (readfds, writefds):
            if fds:
                count += dp.read_ulong(fds)
        return count

    def ioctl(self, dp, handle, command, argument):
        sock = self._get(handle)
        if sock is None:
            return SOCKET_ERROR
        command &= 0xFFFFFFFF
        if command == 0x8004667E and argument:  # FIONBIO
            sock.nonblocking = dp.read_ulong(argument) != 0
        elif command == 0x4004667F and argument:  # FIONREAD
            self._simulated_reply(sock) if sock.real is None else None
            dp.write_ulong(argument, len(sock.inbox))
        return 0

    def getaddrinfo(self, dp, node, service, hints, result, wide):
        name = read_string(dp, node, wide) if node else "localhost"
        port_text = read_string(dp, service, wide) if service else "0"
        try:
            port = int(port_text)
        except ValueError:
            port = {"http": 80, "https": 443, "ftp": 21, "smtp": 25, "dns": 53}.get(port_text.lower(), 0)
        address = self.resolve(name)
        if address is None:
            return WSAHOST_NOT_FOUND
        kind = dp.read_ulong(hints + 8) if hints else SOCK_STREAM
        sockaddr = dp.allocate(16)
        self._write_sockaddr_in(sockaddr, address, port)
        if dp._x64:
            info = struct.pack("<iiiiQQQQ", 0, AF_INET, kind or SOCK_STREAM, 0, 16, 0, sockaddr, 0)
        else:
            info = struct.pack("<iiiiIIII", 0, AF_INET, kind or SOCK_STREAM, 0, 16, 0, sockaddr, 0)
        pointer = dp.allocate(len(info))
        dp.write(pointer, info)
        dp.write_ptr(result, pointer)
        return 0

    def gethostbyname(self, dp, name_ptr):
        name = read_string(dp, name_ptr, False)
        address = self.resolve(name)
        if address is None:
            set_last_error(dp, WSAHOST_NOT_FOUND)
            return 0
        ptr = dp.ptr_size()
        block = dp.allocate(0x100)
        ip_ptr, list_ptr, aliases_ptr, name_copy = block + 0x40, block + 0x48, block + 0x58, block + 0x60
        dp.write(ip_ptr, _socket.inet_aton(address))
        dp.write_ptr(list_ptr, ip_ptr)
        dp.write_ptr(list_ptr + ptr, 0)
        dp.write_ptr(aliases_ptr, 0)
        dp.write(name_copy, name.encode()[:0x9F] + b"\0")
        # HOSTENT: h_name, h_aliases, h_addrtype (short), h_length (short), h_addr_list
        if dp._x64:
            dp.write(block, struct.pack("<QQhhIQ", name_copy, aliases_ptr, AF_INET, 4, 0, list_ptr))
        else:
            dp.write(block, struct.pack("<IIhhI", name_copy, aliases_ptr, AF_INET, 4, list_ptr))
        return block

    def gethostname(self, dp, buffer, length):
        dp.write(buffer, b"fathom-emu\0"[:length])
        return 0

    def peer_name(self, dp, handle, address, length, own):
        sock = self._get(handle)
        if sock is None:
            return SOCKET_ERROR
        ip, port = ("10.0.0.2", 49152) if own or not sock.peer else sock.peer
        self._write_sockaddr_in(address, ip, port)
        if length:
            dp.write_ulong(length, 16)
        return 0

    # ------------------------------------------------------------------ HTTP (WinINet, WinHTTP, urlmon)
    def http(self, method: str, url: str, headers: str = "", body: bytes = b""):
        """A request: (status, response headers, body), or None when the server cannot be reached."""
        self._record("http_request", method=method, url=url, headers=headers.strip(), body=body[:256].decode("latin-1"))
        if self.mode != "real":
            response = (200, "Content-Type: text/html\r\nContent-Length: %d\r\n" % len(self.body), self.body)
        else:
            request = urllib.request.Request(url, data=body or None, method=method)
            for line in headers.splitlines():
                if ":" in line:
                    key, value = line.split(":", 1)
                    request.add_header(key.strip(), value.strip())
            try:
                with urllib.request.urlopen(request, timeout=30) as reply:
                    response = (reply.status, "".join(f"{k}: {v}\r\n" for k, v in reply.headers.items()), reply.read())
            except urllib.error.HTTPError as error:
                response = (error.code, "".join(f"{k}: {v}\r\n" for k, v in error.headers.items()), error.read())
            except (urllib.error.URLError, OSError):
                self._record("http_failed", url=url)
                return None
        self._record("http_response", url=url, status=response[0], size=len(response[2]))
        return response

    def _internet(self, handle) -> Optional[InternetHandle]:
        return self.internet.get(handle)

    def _new_internet(self, kind, **values) -> int:
        handle = self._handle()
        self.internet[handle] = InternetHandle(handle, kind, **values)
        return handle

    def internet_open(self, dp, agent, access, proxy, bypass, flags, wide):
        self._record("internet_open", agent=read_string(dp, agent, wide) if agent else None)
        return self._new_internet("session")

    def internet_connect(self, dp, session, server, port, user, password, service, flags, context, wide):
        name = read_string(dp, server, wide) or ""
        port = port & 0xFFFF or 80
        return self._new_internet("connection", server=name, port=port, secure=port == 443)

    def _url(self, handle: InternetHandle) -> str:
        scheme = "https" if handle.secure else "http"
        default = 443 if handle.secure else 80
        host = handle.server if handle.port == default else f"{handle.server}:{handle.port}"
        return f"{scheme}://{host}{handle.path if handle.path.startswith('/') else '/' + handle.path}"

    def http_open_request(self, dp, connection, verb, path, version, referrer, accept, flags, context, wide):
        parent = self._internet(connection)
        if parent is None:
            return self._error(ERROR_INVALID_HANDLE, 0)
        return self._new_internet("request", server=parent.server, port=parent.port,
                                  secure=parent.secure or bool(flags & 0x00800000),  # INTERNET_FLAG_SECURE
                                  method=(read_string(dp, verb, wide) if verb else "GET") or "GET",
                                  path=(read_string(dp, path, wide) if path else "/") or "/")

    def _perform(self, handle: InternetHandle, url: str, body: bytes = b"") -> bool:
        response = self.http(handle.method, url, handle.headers, body)
        if response is None:
            set_last_error(self.dp, ERROR_INTERNET_CANNOT_CONNECT)
            return False
        handle.status, handle.response_headers, handle.response = response
        handle.position = 0
        return True

    def http_send_request(self, dp, request, headers, headers_length, optional, optional_length, wide):
        handle = self._internet(request)
        if handle is None:
            return self._error(ERROR_INVALID_HANDLE, 0)
        if headers:
            length = to_signed(headers_length)
            handle.headers = read_string(dp, headers, wide) if length < 0 else (
                bytes(dp.read(headers, 2 * length)).decode("utf-16-le", "replace") if wide else bytes(dp.read(headers, length)).decode("latin-1"))
        body = bytes(dp.read(optional, optional_length)) if optional and optional_length else b""
        return int(self._perform(handle, self._url(handle), body))

    def internet_open_url(self, dp, session, url, headers, headers_length, flags, context, wide):
        text = read_string(dp, url, wide) or ""
        handle = InternetHandle(self._handle(), "request", method="GET")
        self.internet[handle.handle] = handle
        if not self._perform(handle, text):
            del self.internet[handle.handle]
            return 0
        return handle.handle

    def internet_read_file(self, dp, handle_value, buffer, size, read):
        handle = self._internet(handle_value)
        if handle is None or handle.response is None:
            return self._error(ERROR_INVALID_HANDLE, 0)
        chunk = handle.response[handle.position:handle.position + size]
        handle.position += len(chunk)
        if chunk:
            dp.write(buffer, chunk)
        dp.write_ulong(read, len(chunk))
        return 1

    def query_data_available(self, dp, handle_value, available, *rest):
        handle = self._internet(handle_value)
        if handle is None or handle.response is None:
            return self._error(ERROR_INVALID_HANDLE, 0)
        if available:
            dp.write_ulong(available, len(handle.response) - handle.position)
        return 1

    def http_query_info(self, dp, handle_value, level, buffer, length, index, wide):
        handle = self._internet(handle_value)
        if handle is None or handle.response is None:
            return self._error(ERROR_INVALID_HANDLE, 0)
        what = level & 0xFFFF
        if what == 19:      # HTTP_QUERY_STATUS_CODE
            value = str(handle.status)
        elif what == 22:    # HTTP_QUERY_RAW_HEADERS_CRLF
            value = f"HTTP/1.1 {handle.status} OK\r\n" + handle.response_headers + "\r\n"
        elif what == 5:     # HTTP_QUERY_CONTENT_LENGTH
            value = str(len(handle.response))
        else:
            set_last_error(dp, 12150)  # ERROR_HTTP_HEADER_NOT_FOUND
            return 0
        if level & 0x20000000:  # HTTP_QUERY_FLAG_NUMBER
            dp.write_ulong(buffer, int(value) if value.isdigit() else 0)
            dp.write_ulong(length, 4)
            return 1
        data = value.encode("utf-16-le") + b"\0\0" if wide else value.encode() + b"\0"
        available = dp.read_ulong(length)
        if available < len(data):
            dp.write_ulong(length, len(data))
            set_last_error(dp, 122)  # ERROR_INSUFFICIENT_BUFFER
            return 0
        dp.write(buffer, data)
        dp.write_ulong(length, len(data) - (2 if wide else 1))
        return 1

    def close_internet(self, dp, handle):
        return int(self.internet.pop(handle, None) is not None)

    # WinHTTP
    def winhttp_connect(self, dp, session, server, port, reserved):
        port = port & 0xFFFF or 80
        return self._new_internet("connection", server=read_string(dp, server, True) or "", port=port, secure=port == 443)

    def winhttp_open_request(self, dp, connection, verb, path, version, referrer, accept, flags):
        parent = self._internet(connection)
        if parent is None:
            return self._error(ERROR_INVALID_HANDLE, 0)
        return self._new_internet("request", server=parent.server, port=parent.port,
                                  secure=parent.secure or bool(flags & 0x00800000),  # WINHTTP_FLAG_SECURE
                                  method=(read_string(dp, verb, True) if verb else "GET") or "GET",
                                  path=(read_string(dp, path, True) if path else "/") or "/")

    def winhttp_send_request(self, dp, request, headers, headers_length, optional, optional_length, total, context):
        handle = self._internet(request)
        if handle is None:
            return self._error(ERROR_INVALID_HANDLE, 0)
        if headers:
            length = to_signed(headers_length)
            handle.headers = read_string(dp, headers, True) if length < 0 else bytes(dp.read(headers, 2 * length)).decode("utf-16-le", "replace")
        body = bytes(dp.read(optional, optional_length)) if optional and optional_length else b""
        return int(self._perform(handle, self._url(handle), body))

    def winhttp_query_headers(self, dp, request, level, name, buffer, length, index):
        return self.http_query_info(dp, request, level, buffer, length, index, True)

    def url_download_to_file(self, dp, caller, url, file, reserved, callback, wide):
        text, path = read_string(dp, url, wide) or "", read_string(dp, file, wide) or ""
        response = self.http("GET", text)
        if response is None:
            return 0x800C0005  # INET_E_RESOURCE_NOT_FOUND
        from .handles import FileObject
        try:
            dp.handles.map_file("\\??\\" + path, FileObject(path, response[2]))
        except Exception:
            pass
        self._record("download", url=text, file=path, size=len(response[2]))
        return 0


def install(dp, mode: str = "simulated", body: bytes = DEFAULT_BODY):
    net = Network(dp, mode, body)
    dp.network = net
    hooks = dp.apihooks
    ws = "ws2_32.dll"
    hooks.register(ws, "WSAStartup", 2, net.wsa_startup)
    hooks.register(ws, "WSACleanup", 0, lambda dp: 0)
    hooks.register(ws, "socket", 3, net.socket)
    hooks.register(ws, "WSASocketW", 6, net.socket)
    hooks.register(ws, "WSASocketA", 6, net.socket)
    hooks.register(ws, "connect", 3, net.connect)
    hooks.register(ws, "WSAConnect", 7, net.connect)
    hooks.register(ws, "send", 4, net.send)
    hooks.register(ws, "recv", 4, net.recv)
    hooks.register(ws, "WSASend", 7, net.wsa_send)
    hooks.register(ws, "WSARecv", 7, net.wsa_recv)
    hooks.register(ws, "sendto", 6, net.sendto)
    hooks.register(ws, "recvfrom", 6, net.recvfrom)
    hooks.register(ws, "closesocket", 1, net.close)
    hooks.register(ws, "shutdown", 2, lambda dp, *a: 0)
    hooks.register(ws, "bind", 3, lambda dp, *a: 0)
    hooks.register(ws, "listen", 2, lambda dp, *a: 0)
    hooks.register(ws, "select", 5, net.select)
    hooks.register(ws, "ioctlsocket", 3, net.ioctl)
    hooks.register(ws, "setsockopt", 5, lambda dp, *a: 0)
    hooks.register(ws, "getsockopt", 5, lambda dp, s, level, name, value, length: (dp.write_ulong(value, 0), 0)[1])
    hooks.register(ws, "getaddrinfo", 4, lambda dp, *a: net.getaddrinfo(dp, *a, False))
    hooks.register(ws, "GetAddrInfoW", 4, lambda dp, *a: net.getaddrinfo(dp, *a, True))
    hooks.register(ws, "freeaddrinfo", 1, lambda dp, p: 0)
    hooks.register(ws, "FreeAddrInfoW", 1, lambda dp, p: 0)
    hooks.register(ws, "gethostbyname", 1, net.gethostbyname)
    hooks.register(ws, "gethostname", 2, net.gethostname)
    hooks.register(ws, "getpeername", 3, lambda dp, s, a, l: net.peer_name(dp, s, a, l, False))
    hooks.register(ws, "getsockname", 3, lambda dp, s, a, l: net.peer_name(dp, s, a, l, True))
    for wide, suffix in ((True, "W"), (False, "A")):
        w = wide
        hooks.register("wininet.dll", "InternetOpen" + suffix, 5, lambda dp, *a, w=w: net.internet_open(dp, *a, w))
        hooks.register("wininet.dll", "InternetConnect" + suffix, 8, lambda dp, *a, w=w: net.internet_connect(dp, *a, w))
        hooks.register("wininet.dll", "HttpOpenRequest" + suffix, 8, lambda dp, *a, w=w: net.http_open_request(dp, *a, w))
        hooks.register("wininet.dll", "HttpSendRequest" + suffix, 5, lambda dp, *a, w=w: net.http_send_request(dp, *a, w))
        hooks.register("wininet.dll", "InternetOpenUrl" + suffix, 6, lambda dp, *a, w=w: net.internet_open_url(dp, *a, w))
        hooks.register("wininet.dll", "HttpQueryInfo" + suffix, 5, lambda dp, *a, w=w: net.http_query_info(dp, *a, w))
        hooks.register("wininet.dll", "InternetSetOption" + suffix, 4, lambda dp, *a: 1)
        hooks.register("urlmon.dll", "URLDownloadToFile" + suffix, 5, lambda dp, *a, w=w: net.url_download_to_file(dp, *a, w))
    hooks.register("wininet.dll", "InternetReadFile", 4, net.internet_read_file)
    hooks.register("wininet.dll", "InternetQueryDataAvailable", 4, net.query_data_available)
    hooks.register("wininet.dll", "InternetCloseHandle", 1, net.close_internet)
    hooks.register("wininet.dll", "InternetGetConnectedState", 2, lambda dp, flags, reserved: (flags and dp.write_ulong(flags, 0x40), 1)[1])
    hooks.register("winhttp.dll", "WinHttpOpen", 5, lambda dp, *a: net._new_internet("session"))
    hooks.register("winhttp.dll", "WinHttpConnect", 4, net.winhttp_connect)
    hooks.register("winhttp.dll", "WinHttpOpenRequest", 7, net.winhttp_open_request)
    hooks.register("winhttp.dll", "WinHttpSendRequest", 7, net.winhttp_send_request)
    hooks.register("winhttp.dll", "WinHttpReceiveResponse", 2, lambda dp, *a: 1)
    hooks.register("winhttp.dll", "WinHttpQueryDataAvailable", 2, lambda dp, h, n: net.query_data_available(dp, h, n))
    hooks.register("winhttp.dll", "WinHttpReadData", 4, net.internet_read_file)
    hooks.register("winhttp.dll", "WinHttpQueryHeaders", 6, net.winhttp_query_headers)
    hooks.register("winhttp.dll", "WinHttpCloseHandle", 1, net.close_internet)
    hooks.register("winhttp.dll", "WinHttpSetOption", 4, lambda dp, *a: 1)
    hooks.register("winhttp.dll", "WinHttpSetTimeouts", 5, lambda dp, *a: 1)
    return net
