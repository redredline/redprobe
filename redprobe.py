#!/usr/bin/env python3
"""redprobe: a small volunteer measurement probe for Red Atlas and Red Node (RedLine Studio).

Self-contained (Python 3.8+, standard library only), runs as an ordinary user, opens no ports: it only makes outbound
HTTPS requests to the backend and the measurement traffic the jobs ask for.

Protocol (JSON over HTTPS, see README.md):
  POST {server}/probe/v1/register   {"enroll_token", "name", "region", "version", "kinds"} -> {"probe_id", "secret"}
  GET  {server}/probe/v1/jobs?max=N  signed                                                -> {"jobs": [...], "next_in": s}
  POST {server}/probe/v1/results     signed {"results": [...]}                              -> {"stored": n}
Signed = headers X-Probe-Id, X-Probe-Ts, X-Probe-Sig = hex HMAC-SHA256(secret, "<id>|<ts>|<body>").

Job kinds:
  atlas.ping     {"id", "kind", "target"}                 ICMP echo (unprivileged ping socket) or a TCP connect fallback
  atlas.tls      {"id", "kind", "target", "port", "mode", "sni"?, "kex"?}
                 mode "tls": TCP connect + TLS handshake (no certificate check, nothing sent after the handshake);
                 mode "ssh": TCP connect, the server banner, KEXINIT both ways and one key exchange message, then a
                 clean disconnect (like ssh-keyscan, never any login attempt); mode "tcp": a TCP connect only;
                 mode "discover" (control points only): which of 443, 22, 8443, 80 answers and how
  atlas.tls1620  {"id", "kind", "url", "max_bytes"?, "sni"?}  download up to 256 KB over TLS, report where it stalled
  rednode.tcp    {"id", "kind", "host", "port", "sni"?}   TCP connect and, with sni, a TLS handshake

Config: a file (default ~/.config/redprobe/redprobe.conf, KEY=VALUE) or environment variables with the same names:
  REDPROBE_SERVER (one URL or several, comma separated; default https://probe.redstudio.site:8444, then
  https://atlas.redstudio.site), REDPROBE_NAME, REDPROBE_REGION, REDPROBE_ENROLL_TOKEN (first run only),
  REDPROBE_ID and REDPROBE_SECRET (written after registration), REDPROBE_MAX_PPS (default 20), REDPROBE_WORKERS
  (default 16), REDPROBE_MAX_JOBS (default 500), REDPROBE_KINDS (default atlas.ping,atlas.tls,atlas.tls1620).
The probe runs only the kinds in REDPROBE_KINDS and skips any other job the server sends; rednode.* runs only where
it is listed there explicitly.

Through an HTTP proxy (a mobile proxy, for example): REDPROBE_PROXY=http://user:password@host:port, or
REDPROBE_PROXY_FILE (a KEY=VALUE file, e.g. a systemd credential) with REDPROBE_PROXY_KEY naming the line to use.
Every measurement connection then goes through HTTP CONNECT; the backend is still reached directly. There is no ICMP
through a proxy, so atlas.ping is skipped. The probe counts the bytes it sends and receives through the proxy and stops
for the rest of the day (UTC) at REDPROBE_PROXY_DAY_MB (default 40); REDPROBE_PROXY_TOTAL_MB is an optional cap over
all days. The proxy address and password are never written to the config file, the state file or the journal.

REDPROBE_LIVE=1 (our live-check point only, never through a proxy): every job starts the moment it arrives and its
answer goes back the moment it is ready, without waiting for the rest of the batch.

Run: redprobe.py (forever), redprobe.py --once (one batch), redprobe.py --register (register and exit),
redprobe.py --test <host> <port> <tls|ssh|tcp|discover> (one check, printed; nothing is sent to the server).
"""
import base64
import hashlib
import hmac
import json
import os
import socket
import ssl
import struct
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import unquote, urlparse

VERSION = "0.3.1"
CONF = os.environ.get("REDPROBE_CONF", os.path.expanduser("~/.config/redprobe/redprobe.conf"))
# The first address is a relay on a server in Russia: some Russian home ISPs cannot reach the Atlas server abroad.
# When an address does not answer, the probe moves on to the next one and stays there while it works.
SERVERS = ["https://probe.redstudio.site:8444", "https://atlas.redstudio.site"]
ALL_KINDS = ["atlas.ping", "atlas.tls", "atlas.tls1620", "rednode.tcp"]
DEFAULT_KINDS = ["atlas.ping", "atlas.tls", "atlas.tls1620"]
BANNER = b"SSH-2.0-RedAtlasProbe\r\n"
# never written back to the config file: secrets that come from the environment or a credential file
NO_SAVE = ("REDPROBE_PROXY", "REDPROBE_PROXY_FILE", "REDPROBE_PROXY_KEY", "REDPROBE_ENROLL_TOKEN", "REDPROBE_CONF")


# ---------------- config ----------------
def load_conf():
    cfg, from_file = {}, []
    if os.path.exists(CONF):
        with open(CONF) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    cfg[k.strip()] = v.strip()
                    from_file.append(k.strip())
    for k, v in os.environ.items():
        if k.startswith("REDPROBE_"):
            cfg[k] = v
    cfg["_file_keys"] = from_file
    return cfg


def save_conf(cfg):
    """Writes back what came from the file plus the probe id and secret. Settings that came only from the
    environment (the proxy above all) are never written."""
    os.makedirs(os.path.dirname(CONF), exist_ok=True)
    keep = set(cfg.get("_file_keys") or []) | {"REDPROBE_ID", "REDPROBE_SECRET"}
    tmp = CONF + ".tmp"
    with open(tmp, "w") as f:
        for k in sorted(cfg):
            if k in keep and k.startswith("REDPROBE_") and k not in NO_SAVE:
                f.write(f"{k}={cfg[k]}\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, CONF)


def proxy_url(cfg):
    """The proxy from REDPROBE_PROXY, or from the REDPROBE_PROXY_KEY line of REDPROBE_PROXY_FILE ('' = direct)."""
    if cfg.get("REDPROBE_PROXY"):
        return cfg["REDPROBE_PROXY"].strip()
    path, key = cfg.get("REDPROBE_PROXY_FILE", ""), cfg.get("REDPROBE_PROXY_KEY", "")
    if path and key:
        path = path.replace("$CREDENTIALS_DIRECTORY", os.environ.get("CREDENTIALS_DIRECTORY", ""))
        try:
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith(key + "="):
                        return line.split("=", 1)[1].strip().strip('"').strip("'")
        except OSError:
            pass
        sys.exit("REDPROBE_PROXY_FILE / REDPROBE_PROXY_KEY are set, but the proxy line could not be read")
    return ""


# ---------------- transport to the backend ----------------
_active = {"i": 0}


def servers(cfg):
    """REDPROBE_SERVER, and for the Atlas addresses the other Atlas address as a fallback."""
    out = [u.strip().rstrip("/") for u in cfg.get("REDPROBE_SERVER", "").replace(" ", ",").split(",") if u.strip()] or list(SERVERS)
    if any(u in SERVERS for u in out):
        out += [u for u in SERVERS if u not in out]
    return out


def call(cfg, method, path, body=None, signed=True):
    data = json.dumps(body).encode() if body is not None else b""
    h = {"Content-Type": "application/json", "User-Agent": f"redprobe/{VERSION}"}
    if signed:  # the signature covers id, time and body only, so it stays valid through the relay
        ts = str(int(time.time()))
        pid = cfg["REDPROBE_ID"]
        h["X-Probe-Id"], h["X-Probe-Ts"] = pid, ts
        h["X-Probe-Sig"] = hmac.new(cfg["REDPROBE_SECRET"].encode(), f"{pid}|{ts}|".encode() + data, hashlib.sha256).hexdigest()
    urls, err = servers(cfg), None
    for k in range(len(urls)):
        i = (_active["i"] + k) % len(urls)
        req = urllib.request.Request(urls[i] + path, data=data or None, method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                out = json.load(r)
        except urllib.error.HTTPError as e:
            try:
                e.payload = json.loads(e.read() or b"{}")
            except ValueError:
                e.payload = {}
            if e.code < 500 or e.payload.get("error"):
                raise  # the Atlas server answered; another address leads to the same server
            err = e  # a gateway error from the relay: try the next address
            if k < len(urls) - 1:
                print(f"{urls[i]} answered HTTP {e.code}, trying {urls[(i + 1) % len(urls)]}", file=sys.stderr, flush=True)
            continue
        except (urllib.error.URLError, OSError) as e:
            err = e
            if k < len(urls) - 1:
                print(f"{urls[i]} does not answer ({getattr(e, 'reason', e)}), trying {urls[(i + 1) % len(urls)]}",
                      file=sys.stderr, flush=True)
            continue
        _active["i"] = i
        return out
    raise err


def enabled_kinds(cfg):
    kinds = [k.strip() for k in cfg.get("REDPROBE_KINDS", ",".join(DEFAULT_KINDS)).split(",") if k.strip() in ALL_KINDS]
    if proxy_url(cfg):
        kinds = [k for k in kinds if k != "atlas.ping"]  # no ICMP through a proxy
    return kinds


def register(cfg):
    try:
        r = call(cfg, "POST", "/probe/v1/register", {"enroll_token": cfg["REDPROBE_ENROLL_TOKEN"], "name": cfg.get("REDPROBE_NAME", socket.gethostname()),
                                                      "region": cfg.get("REDPROBE_REGION", ""), "version": VERSION, "kinds": enabled_kinds(cfg)}, signed=False)
    except urllib.error.HTTPError as e:  # the server explains a refusal (bad token, probe outside Russia) in "message"
        msg = str(getattr(e, "payload", {}).get("message") or "")
        sys.exit(f"registration refused (HTTP {e.code}): {msg}" if msg else f"registration refused: HTTP {e.code}")
    cfg["REDPROBE_ID"], cfg["REDPROBE_SECRET"] = r["probe_id"], r["secret"]
    cfg.pop("REDPROBE_ENROLL_TOKEN", None)  # needed only to register, not kept
    save_conf(cfg)
    return cfg


# ---------------- traffic through a proxy ----------------
class Meter:
    """Bytes of one check. Wire overhead (TCP/IP headers, handshakes) is estimated on top of the payload so the daily
    budget errs on the safe side: +15 % and 600 bytes per connection."""

    def __init__(self):
        self.tx = self.rx = self.conns = 0

    def wire(self):
        return int((self.tx + self.rx) * 1.15) + 600 * self.conns


class Budget:
    """Bytes through the proxy per UTC day, kept in a small state file next to the config."""

    def __init__(self, cfg):
        self.path = cfg.get("REDPROBE_STATE") or (CONF + ".state")
        self.day_cap = int(float(cfg.get("REDPROBE_PROXY_DAY_MB", "40")) * 1e6)
        tot = cfg.get("REDPROBE_PROXY_TOTAL_MB", "")
        self.total_cap = int(float(tot) * 1e6) if tot else 0
        self.lock = threading.Lock()
        self.s = {"day": "", "bytes": 0, "total": 0}
        try:
            with open(self.path) as f:
                self.s.update(json.load(f))
        except (OSError, ValueError):
            pass
        self._roll()

    def _roll(self):
        d = time.strftime("%Y-%m-%d", time.gmtime())
        if self.s["day"] != d:
            self.s["day"], self.s["bytes"] = d, 0

    def room(self, need):
        with self.lock:
            self._roll()
            if self.s["bytes"] + need > self.day_cap:
                return False
            return not (self.total_cap and self.s["total"] + need > self.total_cap)

    def add(self, n):
        with self.lock:
            self._roll()
            self.s["bytes"] += n
            self.s["total"] += n

    def save(self):
        with self.lock:
            tmp = self.path + ".tmp"
            try:
                with open(tmp, "w") as f:
                    json.dump(self.s, f)
                os.chmod(tmp, 0o600)
                os.replace(tmp, self.path)
            except OSError:
                pass


class ProxyError(Exception):
    """The proxy itself failed (its gateway or device), so the check says nothing about the target."""


class Refused(Exception):
    """The target refused the connection (RST), or the proxy says so."""


def open_conn(host, port, timeout, proxy, meter):
    """A TCP connection to host:port, directly or through HTTP CONNECT. Raises socket.timeout / Refused / OSError
    for the target and ProxyError when the proxy cannot be used."""
    meter.conns += 1
    if not proxy:
        try:
            return socket.create_connection((host, int(port)), timeout=timeout)
        except ConnectionRefusedError:
            raise Refused("refused") from None
    u = urlparse(proxy if "://" in proxy else "http://" + proxy)
    try:
        s = socket.create_connection((u.hostname, u.port or 8080), timeout=10)
    except OSError as e:
        raise ProxyError("proxy unreachable: " + type(e).__name__) from None
    req = f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n"
    if u.username:
        tok = base64.b64encode(f"{unquote(u.username)}:{unquote(u.password or '')}".encode()).decode()
        req += f"Proxy-Authorization: Basic {tok}\r\n"
    req = (req + "\r\n").encode()
    s.sendall(req)
    meter.tx += len(req)
    s.settimeout(timeout + 4)
    buf, t0 = b"", time.time()
    try:
        while b"\r\n\r\n" not in buf and len(buf) < 4096:
            b = s.recv(4096)
            if not b:
                break
            buf += b
    except socket.timeout:
        meter.rx += len(buf)
        s.close()
        raise socket.timeout("no answer to CONNECT") from None
    meter.rx += len(buf)
    head, _, rest = buf.partition(b"\r\n\r\n")
    line = head.split(b"\r\n", 1)[0].decode(errors="replace")
    parts = line.split()
    code = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    if code == 200:
        if rest:  # data the target sent at once (an SSH banner) came in the same read
            s = _Prefixed(s, rest)
        s.settimeout(timeout)
        return s
    s.close()
    text = rest.decode(errors="replace").strip().lower()
    if code in (407, 403, 429) or "auth" in text:
        raise ProxyError(f"proxy said {code}")
    if code in (502, 503, 504):
        if "refused" in text and time.time() - t0 < 3:
            raise Refused("refused via proxy")
        if "unavailable" in text or "timeout" in text or "refused" in text:
            raise socket.timeout(f"proxy {code}: {text[:40]}")
    raise ProxyError(f"proxy said {code}")


class _Prefixed:
    """A socket with a few bytes already read (the CONNECT answer can carry the first bytes of the target)."""

    def __init__(self, s, data):
        self._s, self._buf = s, data

    def recv(self, n):
        if self._buf:
            b, self._buf = self._buf[:n], self._buf[n:]
            return b
        return self._s.recv(n)

    def __getattr__(self, a):
        return getattr(self._s, a)


# ---------------- measurements ----------------
def _csum(b):
    if len(b) % 2:
        b += b"\0"
    s = sum(struct.unpack(f"!{len(b) // 2}H", b))
    s = (s >> 16) + (s & 0xFFFF)
    s += s >> 16
    return ~s & 0xFFFF


def ping(target, timeout=1.5):
    """ICMP echo through an unprivileged ping socket (needs net.ipv4.ping_group_range); TCP 443/80 connect otherwise.
    -> (ok, rtt_ms, method)"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_ICMP)
    except (PermissionError, OSError):
        return tcp_ping(target, timeout)
    try:
        s.settimeout(timeout)
        payload = b"redprobe" + os.urandom(8)
        hdr = struct.pack("!BBHHH", 8, 0, 0, 0, 1)
        pkt = struct.pack("!BBHHH", 8, 0, _csum(hdr + payload), 0, 1) + payload
        t0 = time.time()
        s.sendto(pkt, (target, 0))
        while True:
            data, _ = s.recvfrom(1500)
            if data[-len(payload):] == payload or data[0] == 0:
                return True, round((time.time() - t0) * 1000, 2), "icmp"
    except socket.timeout:
        return False, None, "icmp"
    except OSError:
        return tcp_ping(target, timeout)
    finally:
        s.close()


def tcp_ping(target, timeout=1.5):
    for port in (443, 80, 22):
        t0 = time.time()
        try:
            with socket.create_connection((target, port), timeout=timeout):
                return True, round((time.time() - t0) * 1000, 2), f"tcp{port}"
        except ConnectionRefusedError:
            return True, round((time.time() - t0) * 1000, 2), f"tcp{port}-rst"  # a refusal still proves the path
        except OSError:
            continue
    return False, None, "tcp"


def _send(s, data, meter):
    s.sendall(data)
    meter.tx += len(data)


def _recv(s, n, meter):
    b = s.recv(n)
    meter.rx += len(b)
    return b


class BioTLS:
    """TLS over an already connected socket through memory BIOs, so every byte on the wire is counted and the same
    code works directly and through a proxy tunnel."""

    def __init__(self, s, sni, meter):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE  # reachability, not certificate trust
        try:
            ctx.set_ciphers("DEFAULT:@SECLEVEL=0")
        except ssl.SSLError:
            pass
        self.s, self.m = s, meter
        self.inc, self.out = ssl.MemoryBIO(), ssl.MemoryBIO()
        self.o = ctx.wrap_bio(self.inc, self.out, server_hostname=sni or None)
        self.got_tls = False
        self.first = b""

    def _flush(self):
        d = self.out.read()
        if d:
            _send(self.s, d, self.m)

    def _fill(self, deadline):
        self.s.settimeout(max(0.5, deadline - time.time()))
        b = _recv(self.s, 65536, self.m)
        if b and not self.first:
            self.first = b[:3]
            self.got_tls = len(b) >= 2 and b[0] in (0x14, 0x15, 0x16, 0x17) and b[1] == 0x03
        if b:
            self.inc.write(b)
        return b

    def handshake(self, timeout):
        """-> tls_ok (handshake done), tls_answer (the server answered in TLS, e.g. an alert: TLS passes both ways),
        tls_cut (the ClientHello went out, nothing came back), tls_reset, tls_eof, tls_other (not TLS)."""
        deadline = time.time() + timeout
        while True:
            try:
                self.o.do_handshake()
                self._flush()
                return "tls_ok"
            except ssl.SSLWantReadError:
                self._flush()
                try:
                    b = self._fill(deadline)
                except socket.timeout:
                    return "tls_answer" if self.got_tls else "tls_cut"
                except ConnectionResetError:
                    return "tls_answer" if self.got_tls else "tls_reset"
                except OSError:
                    return "tls_answer" if self.got_tls else "tls_reset"
                if not b:
                    return "tls_answer" if self.got_tls else "tls_eof"
                if not self.got_tls:
                    # a mobile proxy whose device could not connect leaks a SOCKS5 failure reply into the tunnel
                    # (05 01 00 01 ...) after its HTTP front said 200: that is «no connection», not a server reply
                    if self.first[:1] == b"\x05" and len(b) <= 16:
                        return "proxy_no_tcp"
                    return "tls_other"
            except ssl.SSLError:
                return "tls_answer" if self.got_tls else "tls_other"

    def send(self, data):
        self.o.write(data)
        self._flush()

    def recv(self, timeout):
        """Decrypted bytes, b'' at the end of the stream. Raises socket.timeout when nothing comes for `timeout`."""
        deadline = time.time() + timeout
        while True:
            try:
                return self.o.read(65536)
            except ssl.SSLWantReadError:
                self._flush()
                if not self._fill(deadline):
                    return b""
            except (ssl.SSLZeroReturnError, ssl.SSLEOFError):
                return b""


def tls_handshake(s, sni, timeout, meter):
    return BioTLS(s, sni, meter).handshake(timeout)


# SSH key exchange, plaintext until NEWKEYS: banner, KEXINIT both ways, one KEX init message and the server reply.
KEX_BIG = ["mlkem768x25519-sha256", "sntrup761x25519-sha512", "sntrup761x25519-sha512@openssh.com"]
KEX_SMALL = ["curve25519-sha256", "curve25519-sha256@libssh.org", "ecdh-sha2-nistp256",
             "diffie-hellman-group14-sha256", "diffie-hellman-group14-sha1"]
HOSTKEYS = "ssh-ed25519,ecdsa-sha2-nistp256,rsa-sha2-512,rsa-sha2-256,ssh-rsa"
CIPHERS = "chacha20-poly1305@openssh.com,aes128-ctr,aes256-ctr,aes128-gcm@openssh.com,aes256-gcm@openssh.com,aes128-cbc"
MACS = "hmac-sha2-256-etm@openssh.com,hmac-sha2-256,hmac-sha1,hmac-sha2-512"
# the NIST P-256 base point: a valid public value, so the server can finish its half of ECDH
P256_G = bytes.fromhex("046b17d1f2e12c4247f8bce6e563a440f277037d812deb33a0f4a13945d898c296"
                       "4fe342e2fe1a7f9b8ee7eb4a7c0f9e162bce33576b315ececbb6406837bf51f5")


def _sstr(b):
    return struct.pack("!I", len(b)) + b


def _ssh_packet(payload):
    pad = 8 - ((5 + len(payload)) % 8)
    if pad < 4:
        pad += 8
    return struct.pack("!IB", 1 + len(payload) + pad, pad) + payload + os.urandom(pad)


# a 2047-bit DH value with every bit set: inside (1, p-1) for group 14 and not rejected as weak (OpenSSH wants >4 bits)
DH_E = b"\x7f" + b"\xff" * 255


def _kex_value(alg):
    """Our public value for the chosen key exchange. We never finish the exchange, so these only have to be
    well-formed: random X25519, the P-256 base point, a valid DH value, an ML-KEM-768 key with zero coefficients."""
    x25519 = os.urandom(32)
    if alg.startswith("mlkem768x25519"):
        return _sstr(bytes(1152) + os.urandom(32) + x25519)
    if alg.startswith("sntrup761x25519"):
        return _sstr(os.urandom(1158) + x25519)
    if alg.startswith("curve25519"):
        return _sstr(x25519)
    if alg == "ecdh-sha2-nistp256":
        return _sstr(P256_G)
    return _sstr(DH_E)


class _Eof(Exception):
    pass


def _read_packet(s, buf, meter, deadline):
    """-> (payload, rest of the buffer). Raises socket.timeout, _Eof (the server closed cleanly), OSError."""
    while True:
        if len(buf) >= 5:
            n = struct.unpack("!I", buf[:4])[0]
            if n < 5 or n > 65000:
                raise ValueError("bad packet")
            if len(buf) >= 4 + n:
                pad = buf[4]
                return buf[5:4 + n - pad], buf[4 + n:]
        s.settimeout(max(0.5, deadline - time.time()))
        b = _recv(s, 16384, meter)
        if not b:
            raise _Eof()
        buf += b


def _namelists(payload, k):
    """The first k name-lists of a KEXINIT payload."""
    out, p = [], 17
    for _ in range(k):
        n = struct.unpack("!I", payload[p:p + 4])[0]
        out.append(payload[p + 4:p + 4 + n].decode(errors="replace").split(","))
        p += 4 + n
    return out


def ssh_kex(s, timeout, meter, big=True):
    """-> (outcome, info). Outcomes: tcp_no_banner, banner_no_kexinit, kex_cut (our KEX message went out, nothing
    came back), kex_reset (the connection was reset right there), kex_ok (the server's KEX reply came back, or it
    answered our value by closing or disconnecting: data passes both ways). Never authenticates."""
    info = {}
    deadline = time.time() + 5
    buf = b""
    try:
        while True:
            i = buf.find(b"\n")
            if i >= 0:
                line, buf = buf[:i + 1], buf[i + 1:]
                if line.startswith(b"SSH-"):
                    info["banner"] = line.strip()[:40].decode(errors="replace")
                    break
                continue
            if len(buf) > 8192:
                return "tcp_no_banner", info
            s.settimeout(max(0.5, deadline - time.time()))
            b = _recv(s, 4096, meter)
            if not b:
                return "tcp_no_banner", info
            if not buf and b[:1] == b"\x05" and len(b) <= 16:
                return "proxy_no_tcp", info  # the mobile proxy's device could not connect (SOCKS5 failure reply)
            buf += b
    except (socket.timeout, OSError):
        return "tcp_no_banner", info
    kex = (KEX_BIG if big else []) + KEX_SMALL
    payload = (b"\x14" + os.urandom(16) + _sstr(",".join(kex).encode()) + _sstr(HOSTKEYS.encode()) +
               _sstr(CIPHERS.encode()) * 2 + _sstr(MACS.encode()) * 2 + _sstr(b"none") * 2 + _sstr(b"") * 2 + b"\x00" +
               struct.pack("!I", 0))
    try:
        _send(s, BANNER + _ssh_packet(payload), meter)
        deadline = time.time() + timeout
        server = None
        for _ in range(4):
            p, buf = _read_packet(s, buf, meter, deadline)
            if p and p[0] == 20:
                server = p
                break
        if server is None:
            return "banner_no_kexinit", info
    except (socket.timeout, OSError, ValueError, _Eof):
        return "banner_no_kexinit", info
    try:
        offered = _namelists(server, 1)[0]
    except (struct.error, IndexError):
        return "banner_no_kexinit", info
    info["kex_big"] = any(a in offered for a in KEX_BIG)
    alg = next((a for a in kex if a in offered), None)
    if not alg:
        info["kex"] = "none"
        return "kex_ok", info  # KEXINIT came back after ours; no common algorithm, nothing more to send
    info["kex"] = alg
    try:
        _send(s, _ssh_packet(b"\x1e" + _kex_value(alg)), meter)
        deadline = time.time() + timeout
        for _ in range(4):
            p, buf = _read_packet(s, buf, meter, deadline)
            if p and p[0] in (31, 1):  # the reply, or a disconnect after reading our value: it got there
                break
        else:
            return "kex_cut", info
    except _Eof:
        info["closed"] = 1  # the server read our value and closed: the path works
        return "kex_ok", info
    except socket.timeout:
        return "kex_cut", info
    except (OSError, ValueError):
        return "kex_reset", info
    try:
        _send(s, _ssh_packet(b"\x01" + struct.pack("!I", 11) + _sstr(b"") + _sstr(b"")), meter)
    except OSError:
        pass
    return "kex_ok", info


OK_OUT = {"tls_ok", "tls_answer", "kex_ok", "tcp_ok"}


def check(host, port, mode, proxy=None, sni=None, timeout=8.0, big=True):
    """One protocol check. -> dict(outcome, ok, rtt, bytes, ...). ok is None when the check says nothing about the
    target (the proxy failed)."""
    m = Meter()
    t0 = time.time()
    res = {"port": int(port), "mode": mode}
    try:
        s = open_conn(host, port, timeout, proxy, m)
    except ProxyError as e:
        res.update(outcome="proxy_error", ok=None, error=str(e)[:80], bytes=m.wire())
        return res
    except Refused:
        res.update(outcome="tcp_reset", ok=False, bytes=m.wire())
        return res
    except (socket.timeout, OSError) as e:
        res.update(outcome="no_tcp", ok=False, error=str(e)[:80], bytes=m.wire())
        return res
    res["tcp_ms"] = round((time.time() - t0) * 1000, 1)
    try:
        if mode == "tls":
            out = tls_handshake(s, sni, timeout, m)
        elif mode == "ssh":
            out, info = ssh_kex(s, timeout, m, big=big)
            res.update(info)
        else:
            out = "tcp_ok"
    finally:
        try:
            s.close()
        except OSError:
            pass
    res.update(outcome=out, ok=out in OK_OUT, rtt=round((time.time() - t0) * 1000, 1), bytes=m.wire())
    return res


DISCOVER_ORDER = ((443, "tls"), (22, "ssh"), (8443, "tls"), (80, "tcp"))


def discover(host, proxy=None, timeout=4.0, parallel=False):
    """What a control point sees on an address: TLS on 443, else SSH on 22, else TLS on 8443, else TCP on 80.
    -> dict(svc tls|ssh|tcp|none, port, outcome, ok, seen). One connection per port, at most four. `parallel`
    (direct connections only, for live checks a visitor waits on) opens the four at once: a silent address costs one
    timeout instead of four."""
    if parallel and not proxy:
        with ThreadPoolExecutor(max_workers=len(DISCOVER_ORDER)) as ex:
            got = iter(list(ex.map(lambda pm: check(host, pm[0], pm[1], None, None, timeout, big=False), DISCOVER_ORDER)))
        probe = lambda port, mode: next(got)
    else:
        probe = lambda port, mode: check(host, port, mode, proxy, None, timeout, big=False)
    seen, tcp_port, total = [], 0, 0
    for port, mode in DISCOVER_ORDER:
        r = probe(port, mode)
        total += r.get("bytes", 0)
        seen.append(f"{port}:{r['outcome']}")
        if r["outcome"] == "proxy_error":
            r.update(svc="", seen=seen, bytes=total)
            return r
        if r["ok"]:
            r.update(svc=mode, seen=seen, bytes=total)
            return r
        if not tcp_port and r["outcome"] not in ("no_tcp", "tcp_reset"):
            tcp_port = port  # open, but the protocol did not finish: a TCP target if nothing better turns up
    if tcp_port:
        return {"svc": "tcp", "port": tcp_port, "mode": "tcp", "outcome": "tcp_ok", "ok": True, "seen": seen, "bytes": total}
    return {"svc": "none", "port": 0, "mode": "discover", "outcome": "no_tcp", "ok": False, "seen": seen, "bytes": total}


def tls1620(url, want=256 * 1024, stall=6.0, total=20.0, proxy=None, sni=True):
    """Download the first `want` bytes of a test file over TLS and note where the transfer stopped: a stop inside
    12-24 KB is the «16-20» cut. One connection, every byte counted."""
    u = urlparse(url)
    host, port = u.hostname, u.port or 443
    path = (u.path or "/") + (("?" + u.query) if u.query else "")
    t0, got, stalled, err, hs = time.time(), 0, False, None, ""
    m = Meter()
    try:
        s = open_conn(host, port, 8, proxy, m)
        t = BioTLS(s, host if sni else None, m)
        hs = t.handshake(8)
        if hs != "tls_ok":
            raise OSError("handshake " + hs)
        t.send(f"GET {path} HTTP/1.1\r\nHost: {host}\r\nRange: bytes=0-{want - 1}\r\nUser-Agent: redprobe/{VERSION}\r\n"
               f"Connection: close\r\n\r\n".encode())
        while got < want and time.time() - t0 < total:
            try:
                b = t.recv(stall)
            except socket.timeout:
                stalled = True
                break
            if not b:
                break
            got += len(b)
        s.close()
    except Exception as e:
        err = str(e)[:120]
    cut = stalled and 12 * 1024 <= got <= 24 * 1024
    return {"ok": got > 24 * 1024 and not err, "bytes_got": got, "bytes": m.wire(), "stalled": stalled, "cut1620": cut,
            "handshake": hs, "secs": round(time.time() - t0, 2), "error": err}


def rednode_tcp(host, port, sni=None, timeout=5.0, proxy=None):
    t0 = time.time()
    r = check(host, port, "tls" if sni else "tcp", proxy, sni, timeout)
    if r.get("ok"):
        return {"ok": True, "stage": "tls" if sni else "tcp", "rtt": round((time.time() - t0) * 1000, 2), "bytes": r["bytes"]}
    return {"ok": False if r.get("ok") is False else None, "error": r.get("outcome"), "rtt": None, "bytes": r["bytes"]}


# rough cost of a check through the proxy, to stop before the daily budget is crossed
COST = {"tls": 6000, "ssh": 9000, "tcp": 1200, "discover": 20000}


def run_job(j, proxy=None, budget=None, max1620=256 * 1024):
    k = j.get("kind")
    if k == "atlas.ping":
        if proxy:
            return {"job_id": j["id"], "kind": k, "ok": None, "error": "no icmp through a proxy", "uncounted": 1}
        ok, rtt, how = ping(j["target"])
        return {"job_id": j["id"], "kind": k, "ok": ok, "rtt": rtt, "method": how}
    if k == "atlas.tls":
        mode = j.get("mode", "tls")
        if mode not in COST:
            return {"job_id": j["id"], "kind": k, "ok": None, "error": "unknown mode"}
        if budget and not budget.room(COST[mode]):
            return {"job_id": j["id"], "kind": k, "ok": None, "outcome": "budget", "uncounted": 1}
        if mode == "discover":
            r = discover(j["target"], proxy, parallel=bool(j.get("parallel")))
        else:
            r = check(j["target"], int(j.get("port") or 443), mode, proxy, j.get("sni") or None,
                      big=j.get("kex", "big") != "small")
            if proxy and r.get("port") == 22 and r.get("outcome") == "tcp_reset":
                # a mobile network (or the proxy's device) that refuses port 22 to everybody, now and then: ask a known
                # SSH server once more before calling this address refused
                cr = check("github.com", 22, "tcp", proxy, timeout=8)
                r["bytes"] = r.get("bytes", 0) + cr.get("bytes", 0)
                if not cr.get("ok"):
                    r.update(outcome="port22_closed_here", ok=None, uncounted=1)
        if budget:
            budget.add(r.get("bytes", 0))
        return dict(job_id=j["id"], kind=k, proxy=1 if proxy else 0, **r)
    if k == "atlas.tls1620":
        want = min(int(j.get("max_bytes") or 256 * 1024), max1620)
        if budget and not budget.room(want + 15000):
            return {"job_id": j["id"], "kind": k, "ok": None, "outcome": "budget", "uncounted": 1}
        r = tls1620(j["url"], want=want, proxy=proxy, sni=j.get("sni", True) is not False)
        if budget:
            budget.add(r.get("bytes", 0))
        return dict(job_id=j["id"], kind=k, proxy=1 if proxy else 0, **r)
    if k == "rednode.tcp":
        r = rednode_tcp(j["host"], j["port"], j.get("sni"), proxy=proxy)
        if budget:
            budget.add(r.get("bytes", 0))
        return dict(job_id=j["id"], kind=k, **r)
    return {"job_id": j.get("id"), "kind": k, "ok": False, "error": "unknown kind"}


# ---------------- main loop ----------------
def safe_job(j, *a):
    try:
        return run_job(j, *a)
    except Exception as e:  # a malformed job must not stop the batch
        return {"job_id": j.get("id"), "kind": j.get("kind"), "ok": None, "error": str(e)[:120]}


def canary(cfg, proxy):
    """Through a proxy: is the proxy itself working right now? A TCP connect to REDPROBE_CANARY (default
    1.1.1.1:443), about 300 bytes. Without it a dead proxy would look like a blocked internet."""
    host, _, port = cfg.get("REDPROBE_CANARY", "1.1.1.1:443").rpartition(":")
    for _ in range(2):
        r = check(host, int(port or 443), "tcp", proxy, timeout=8)
        if r.get("ok"):
            return True, r.get("bytes", 0)
    return False, r.get("bytes", 0)


def once(cfg, budget=None):
    """One batch: jobs start at most REDPROBE_MAX_PPS per second and run in REDPROBE_WORKERS threads, so a dead
    address (timeouts) does not hold up the rest. Results go back in chunks of 200."""
    proxy = proxy_url(cfg)
    pps = max(1, int(cfg.get("REDPROBE_MAX_PPS", "5" if proxy else "20")))
    workers = max(1, min(64, int(cfg.get("REDPROBE_WORKERS", "6" if proxy else "16"))))
    max1620 = int(cfg.get("REDPROBE_1620_MAX_KB", "64" if proxy else "256")) * 1024
    t0 = time.time()
    q = "/probe/v1/jobs?max=" + str(int(cfg.get("REDPROBE_MAX_JOBS", "500")))
    if budget:
        q += f"&bytes_day={budget.s['bytes']}&budget={budget.day_cap}&bytes_total={budget.s['total']}"
        if not budget.room(COST["tls"]):
            q = q.replace("max=" + str(int(cfg.get("REDPROBE_MAX_JOBS", "500"))), "max=0")
    r = call(cfg, "GET", q)
    kinds = set(enabled_kinds(cfg))
    jobs = [j for j in r.get("jobs", []) if j.get("kind") in kinds]
    if len(jobs) < len(r.get("jobs", [])):
        print("skipped", len(r["jobs"]) - len(jobs), "jobs of kinds not enabled in REDPROBE_KINDS", file=sys.stderr, flush=True)
    out, ok = [], 0
    if proxy and jobs:
        alive, spent = canary(cfg, proxy)
        budget.add(spent)
        if not alive:  # the proxy is down: hand the jobs back unanswered rather than as «no connection»
            out = [{"job_id": j.get("id"), "kind": j.get("kind"), "ok": None, "outcome": "proxy_down", "uncounted": 1} for j in jobs]
            call(cfg, "POST", "/probe/v1/results", {"results": out})
            budget.save()
            print(f"batch: proxy down, {len(jobs)} jobs returned", flush=True)
            return 120
        if any(j.get("kind") == "atlas.tls" and int(j.get("port") or 0) == 22 for j in jobs):
            # some mobile networks (or the proxy's device) refuse port 22 to every address at times: then a refusal
            # says nothing about the target. A known SSH server (REDPROBE_CANARY_SSH) tells which case this is.
            host, _, port = cfg.get("REDPROBE_CANARY_SSH", "github.com:22").rpartition(":")
            r = check(host, int(port or 22), "tcp", proxy, timeout=8)
            budget.add(r.get("bytes", 0))
            if not r.get("ok"):
                rest = []
                for j in jobs:
                    if j.get("kind") == "atlas.tls" and int(j.get("port") or 0) == 22:
                        out.append({"job_id": j.get("id"), "kind": j.get("kind"), "ok": None, "outcome": "port22_closed_here",
                                    "uncounted": 1, "port": 22, "mode": j.get("mode")})
                    else:
                        rest.append(j)
                print(f"port 22 is closed through this proxy right now ({r.get('outcome')}): {len(out)} SSH checks returned",
                      flush=True)
                jobs = rest
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = []
        for j in jobs:
            futs.append(ex.submit(safe_job, j, proxy, budget, max1620))
            time.sleep(1.0 / pps)
        for f in futs:
            out.append(f.result())
            ok += 1 if out[-1].get("ok") else 0
            if len(out) >= 200:
                call(cfg, "POST", "/probe/v1/results", {"results": out})
                out = []
    if out:
        call(cfg, "POST", "/probe/v1/results", {"results": out})
    if budget:
        budget.save()
    extra = f", {round(budget.s['bytes'] / 1e6, 2)} MB through the proxy today" if budget else ""
    print(f"batch: {len(jobs)} jobs, {ok} reachable, {round(time.time() - t0)} s{extra}", flush=True)
    return int(r.get("next_in", 60))


def live_loop(cfg):
    """REDPROBE_LIVE=1, our own live-check point: a visitor waits on every answer, so a slow job (a silent address
    times out) must not hold up the others. Each job starts the moment it arrives and its answer goes back the moment
    it is ready; the next poll does not wait for the batch."""
    kinds = set(enabled_kinds(cfg))
    ex = ThreadPoolExecutor(max_workers=max(1, min(64, int(cfg.get("REDPROBE_WORKERS", "16")))))

    def run_and_send(j):
        r = safe_job(j)
        try:
            call(cfg, "POST", "/probe/v1/results", {"results": [r]})
        except Exception as e:
            print("result not sent:", str(e)[:120], file=sys.stderr, flush=True)

    print("live mode: every job answered on its own", flush=True)
    while True:
        try:
            r = call(cfg, "GET", "/probe/v1/jobs?max=" + str(int(cfg.get("REDPROBE_MAX_JOBS", "50"))))
            jobs = [j for j in r.get("jobs", []) if j.get("kind") in kinds]
            for j in jobs:
                ex.submit(run_and_send, j)
            if jobs:
                print(f"live: {len(jobs)} jobs started", flush=True)
            wait = int(r.get("next_in", 4))
        except Exception as e:  # keep running: the backend may be restarting
            print("cycle failed:", str(e)[:200], file=sys.stderr, flush=True)
            wait = 30
        time.sleep(max(float(cfg.get("REDPROBE_MIN_WAIT", "10")), min(600, wait)))


def main():
    cfg = load_conf()
    if "--test" in sys.argv:
        a = sys.argv[sys.argv.index("--test") + 1:]
        host, port, mode = a[0], int(a[1]) if len(a) > 1 else 443, a[2] if len(a) > 2 else "tls"
        proxy = proxy_url(cfg)
        r = discover(host, proxy) if mode == "discover" else check(host, port, mode, proxy, a[3] if len(a) > 3 else None)
        print(json.dumps(r, ensure_ascii=False))
        return
    if "REDPROBE_ID" not in cfg:
        if "REDPROBE_ENROLL_TOKEN" not in cfg:
            sys.exit("first run needs REDPROBE_ENROLL_TOKEN")
        cfg = register(cfg)
        print("registered as", cfg["REDPROBE_ID"])
    if "--register" in sys.argv:
        print("probe id", cfg["REDPROBE_ID"], "server", servers(cfg)[_active["i"]])
        return
    budget = Budget(cfg) if proxy_url(cfg) else None
    if budget:
        print(f"through a proxy, at most {budget.day_cap // 1000000} MB a day", flush=True)
    if "--once" in sys.argv:
        once(cfg, budget)
        return
    if cfg.get("REDPROBE_LIVE") == "1" and not budget:
        live_loop(cfg)
        return
    while True:
        try:
            wait = once(cfg, budget)
        except Exception as e:  # keep running: the backend may be restarting
            print("cycle failed:", str(e)[:200], file=sys.stderr, flush=True)
            wait = 60
        time.sleep(max(int(cfg.get("REDPROBE_MIN_WAIT", "10")), min(600, wait)))


if __name__ == "__main__":
    main()
