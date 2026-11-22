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
  atlas.tls1620  {"id", "kind", "url"}                    download up to 256 KB over TLS, report where it stalled
  rednode.tcp    {"id", "kind", "host", "port", "sni"?}   TCP connect and, with sni, a TLS handshake

Config: a file (default ~/.config/redprobe/redprobe.conf, KEY=VALUE) or environment variables with the same names:
  REDPROBE_SERVER, REDPROBE_NAME, REDPROBE_REGION, REDPROBE_ENROLL_TOKEN (first run only),
  REDPROBE_ID and REDPROBE_SECRET (written after registration), REDPROBE_MAX_PPS (default 20), REDPROBE_WORKERS
  (default 16), REDPROBE_MAX_JOBS (default 500), REDPROBE_KINDS (default atlas.ping,atlas.tls1620).
The probe runs only the kinds in REDPROBE_KINDS and skips any other job the server sends; rednode.* runs only where
it is listed there explicitly.

Run: redprobe.py (forever), redprobe.py --once (one batch), redprobe.py --register (register and exit).
"""
import hashlib
import hmac
import json
import os
import socket
import ssl
import struct
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

VERSION = "0.2.2"
CONF = os.environ.get("REDPROBE_CONF", os.path.expanduser("~/.config/redprobe/redprobe.conf"))
ALL_KINDS = ["atlas.ping", "atlas.tls1620", "rednode.tcp"]
DEFAULT_KINDS = ["atlas.ping", "atlas.tls1620"]


# ---------------- config ----------------
def load_conf():
    cfg = {}
    if os.path.exists(CONF):
        with open(CONF) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    cfg[k.strip()] = v.strip()
    for k, v in os.environ.items():
        if k.startswith("REDPROBE_"):
            cfg[k] = v
    return cfg


def save_conf(cfg):
    os.makedirs(os.path.dirname(CONF), exist_ok=True)
    tmp = CONF + ".tmp"
    with open(tmp, "w") as f:
        for k in sorted(cfg):
            if k.startswith("REDPROBE_") and k != "REDPROBE_CONF":
                f.write(f"{k}={cfg[k]}\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, CONF)


# ---------------- transport ----------------
def call(cfg, method, path, body=None, signed=True):
    data = json.dumps(body).encode() if body is not None else b""
    h = {"Content-Type": "application/json", "User-Agent": f"redprobe/{VERSION}"}
    if signed:
        ts = str(int(time.time()))
        pid = cfg["REDPROBE_ID"]
        h["X-Probe-Id"], h["X-Probe-Ts"] = pid, ts
        h["X-Probe-Sig"] = hmac.new(cfg["REDPROBE_SECRET"].encode(), f"{pid}|{ts}|".encode() + data, hashlib.sha256).hexdigest()
    req = urllib.request.Request(cfg["REDPROBE_SERVER"].rstrip("/") + path, data=data or None, method=method, headers=h)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def enabled_kinds(cfg):
    return [k.strip() for k in cfg.get("REDPROBE_KINDS", ",".join(DEFAULT_KINDS)).split(",") if k.strip() in ALL_KINDS]


def register(cfg):
    try:
        r = call(cfg, "POST", "/probe/v1/register", {"enroll_token": cfg["REDPROBE_ENROLL_TOKEN"], "name": cfg.get("REDPROBE_NAME", socket.gethostname()),
                                                      "region": cfg.get("REDPROBE_REGION", ""), "version": VERSION, "kinds": enabled_kinds(cfg)}, signed=False)
    except urllib.error.HTTPError as e:  # the server explains a refusal (bad token, probe outside Russia) in "message"
        try:
            msg = json.load(e).get("message") or ""
        except Exception:
            msg = ""
        sys.exit(f"registration refused (HTTP {e.code}): {msg}" if msg else f"registration refused: HTTP {e.code}")
    cfg["REDPROBE_ID"], cfg["REDPROBE_SECRET"] = r["probe_id"], r["secret"]
    cfg.pop("REDPROBE_ENROLL_TOKEN", None)  # needed only to register, not kept
    save_conf(cfg)
    return cfg


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


def tls1620(url, want=256 * 1024, stall=6.0, total=20.0):
    u = urlparse(url)
    host, port = u.hostname, u.port or 443
    path = (u.path or "/") + (("?" + u.query) if u.query else "")
    t0, got, stalled, err = time.time(), 0, False, None
    try:
        raw = socket.create_connection((host, port), timeout=8)
        s = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
        s.sendall(f"GET {path} HTTP/1.1\r\nHost: {host}\r\nRange: bytes=0-{want - 1}\r\nUser-Agent: redprobe/{VERSION}\r\nConnection: close\r\n\r\n".encode())
        s.settimeout(stall)
        while got < want and time.time() - t0 < total:
            try:
                b = s.recv(65536)
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
    return {"ok": got > 24 * 1024 and not err, "bytes": got, "stalled": stalled, "cut1620": cut, "secs": round(time.time() - t0, 2), "error": err}


def rednode_tcp(host, port, sni=None, timeout=5.0):
    t0 = time.time()
    try:
        raw = socket.create_connection((host, int(port)), timeout=timeout)
        stage = "tcp"
        if sni:
            ctx = ssl.create_default_context()
            ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE  # reachability, not certificate trust
            raw = ctx.wrap_socket(raw, server_hostname=sni)
            stage = "tls"
        raw.close()
        return {"ok": True, "stage": stage, "rtt": round((time.time() - t0) * 1000, 2)}
    except Exception as e:
        return {"ok": False, "error": str(e)[:120], "rtt": None}


def run_job(j):
    k = j.get("kind")
    if k == "atlas.ping":
        ok, rtt, how = ping(j["target"])
        return {"job_id": j["id"], "kind": k, "ok": ok, "rtt": rtt, "method": how}
    if k == "atlas.tls1620":
        return dict(job_id=j["id"], kind=k, **tls1620(j["url"]))
    if k == "rednode.tcp":
        return dict(job_id=j["id"], kind=k, **rednode_tcp(j["host"], j["port"], j.get("sni")))
    return {"job_id": j.get("id"), "kind": k, "ok": False, "error": "unknown kind"}


# ---------------- main loop ----------------
def safe_job(j):
    try:
        return run_job(j)
    except Exception as e:  # a malformed job must not stop the batch
        return {"job_id": j.get("id"), "kind": j.get("kind"), "ok": False, "error": str(e)[:120]}


def once(cfg):
    """One batch: jobs start at most REDPROBE_MAX_PPS per second and run in REDPROBE_WORKERS threads, so a dead
    address (timeouts) does not hold up the rest. Results go back in chunks of 200."""
    pps = max(1, int(cfg.get("REDPROBE_MAX_PPS", "20")))
    workers = max(1, min(64, int(cfg.get("REDPROBE_WORKERS", "16"))))
    t0 = time.time()
    r = call(cfg, "GET", "/probe/v1/jobs?max=" + str(int(cfg.get("REDPROBE_MAX_JOBS", "500"))))
    kinds = set(enabled_kinds(cfg))
    jobs = [j for j in r.get("jobs", []) if j.get("kind") in kinds]
    if len(jobs) < len(r.get("jobs", [])):
        print("skipped", len(r["jobs"]) - len(jobs), "jobs of kinds not enabled in REDPROBE_KINDS", file=sys.stderr, flush=True)
    out, ok = [], 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = []
        for j in jobs:
            futs.append(ex.submit(safe_job, j))
            time.sleep(1.0 / pps)
        for f in futs:
            out.append(f.result())
            ok += 1 if out[-1].get("ok") else 0
            if len(out) >= 200:
                call(cfg, "POST", "/probe/v1/results", {"results": out})
                out = []
    if out:
        call(cfg, "POST", "/probe/v1/results", {"results": out})
    print(f"batch: {len(jobs)} jobs, {ok} reachable, {round(time.time() - t0)} s", flush=True)
    return int(r.get("next_in", 60))


def main():
    cfg = load_conf()
    if "REDPROBE_SERVER" not in cfg:
        sys.exit("REDPROBE_SERVER is not set (config file or environment)")
    if "REDPROBE_ID" not in cfg:
        if "REDPROBE_ENROLL_TOKEN" not in cfg:
            sys.exit("first run needs REDPROBE_ENROLL_TOKEN")
        cfg = register(cfg)
        print("registered as", cfg["REDPROBE_ID"])
    if "--register" in sys.argv:
        print("probe id", cfg["REDPROBE_ID"], "server", cfg["REDPROBE_SERVER"])
        return
    if "--once" in sys.argv:
        once(cfg)
        return
    while True:
        try:
            wait = once(cfg)
        except Exception as e:  # keep running: the backend may be restarting
            print("cycle failed:", str(e)[:200], file=sys.stderr, flush=True)
            wait = 60
        time.sleep(max(10, min(600, wait)))


if __name__ == "__main__":
    main()
