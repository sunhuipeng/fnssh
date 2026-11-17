#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SSH 终端 — 零第三方依赖后端（纯 Python 标准库）

链路：浏览器 <-> WebSocket(RFC6455, 自实现) <-> 系统 ssh 命令(pty)
- 不依赖 tornado / paramiko / pip，任何自带 python3 的系统可直接运行
- SSH 使用系统 openssh-client（fnOS/Debian 默认自带），密码经 pty 自动输入
- 双通道监听：
  1) Unix Socket（$TRIM_APPDEST/fnssh.sock）——飞牛统一网关转发，
     走 fnOS 登录态，支持飞牛 App / FN Connect 远程访问
  2) TCP 8899（可选）——局域网直连兜底
"""

import base64
import fcntl
import hashlib
import json
import os
import pty
import re
import select
import signal
import socketserver
import struct
import subprocess
import sys
import termios
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APPDIR = os.path.dirname(os.path.abspath(__file__))
WWW = os.path.join(APPDIR, "www")

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

RE_PASSWD_PROMPT = re.compile(r"password[:：]\s*$", re.I)
RE_SHELL_PROMPT = re.compile(r"[$#>]\s?$")
RE_DENIED = re.compile(r"permission denied", re.I)


def config_candidates():
    """依次查找配置文件（真机环境变量缺失时兜底）。"""
    dirs = [
        os.environ.get("TRIM_PKGETC", ""),
        os.environ.get("TRIM_PKGVAR", ""),
        APPDIR,
    ]
    return [os.path.join(d, "config.json") for d in dirs if d]


def config_path():
    cands = config_candidates()
    for p in cands:
        if os.path.isfile(p):
            return p
    return cands[0] if cands else os.path.join(APPDIR, "config.json")


def load_conf():
    """每次连接时读取配置，保证向导修改后立即生效。"""
    conf = {
        "host": "127.0.0.1",
        "port": "22",
        "user": "root",
        "password": "",
        "command": "",
    }
    path = config_path()
    loaded = False
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        loaded = True
        for k in conf:
            v = data.get(k)
            if isinstance(v, str) and v != "":
                conf[k] = v
    except Exception as e:
        sys.stderr.write("[fnssh] load config failed (%s): %s\n" % (path, e))
    if not re.match(r"^\d+$", str(conf["port"])):
        conf["port"] = "22"
    sys.stderr.write(
        "[fnssh] config file: %s | loaded: %s | target: %s@%s:%s | password: %s | cmd: %s\n"
        % (path, loaded, conf["user"], conf["host"], conf["port"],
           "yes" if conf["password"] else "NO", conf["command"] or "-")
    )
    conf["_path"] = path
    conf["_loaded"] = loaded
    return conf


# ---------------- WebSocket 帧协议（RFC6455 最小实现） ----------------

def ws_send(sock, payload, opcode):
    header = bytearray([0x80 | opcode])  # FIN=1
    n = len(payload)
    if n < 126:
        header.append(n)
    elif n < 65536:
        header.append(126)
        header += struct.pack(">H", n)
    else:
        header.append(127)
        header += struct.pack(">Q", n)
    sock.sendall(bytes(header) + payload)


def ws_recv_frame(rfile):
    """返回 (fin, opcode, payload)；连接关闭返回 None。"""
    b = rfile.read(2)
    if not b or len(b) < 2:
        return None
    fin = b[0] & 0x80
    opcode = b[0] & 0x0F
    masked = b[1] & 0x80
    length = b[1] & 0x7F
    if length == 126:
        ext = rfile.read(2)
        if len(ext) < 2:
            return None
        length = struct.unpack(">H", ext)[0]
    elif length == 127:
        ext = rfile.read(8)
        if len(ext) < 8:
            return None
        length = struct.unpack(">Q", ext)[0]
    mask = b""
    if masked:
        mask = rfile.read(4)
        if len(mask) < 4:
            return None
    payload = rfile.read(length) if length else b""
    if len(payload) < length:
        return None
    if masked:
        payload = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
    return fin, opcode, payload


# ---------------- SSH 会话（系统 ssh + pty） ----------------

class _Proc(object):
    """轻量进程句柄（配合 pty.fork 使用）。"""

    def __init__(self, pid):
        self.pid = pid
        self._rc = None

    def poll(self):
        if self._rc is None:
            try:
                wpid, status = os.waitpid(self.pid, os.WNOHANG)
                if wpid == self.pid:
                    try:
                        self._rc = os.waitstatus_to_exitcode(status)
                    except AttributeError:
                        self._rc = status
            except ChildProcessError:
                self._rc = 0
        return self._rc

    def terminate(self):
        for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGKILL):
            try:
                os.kill(self.pid, sig)
            except OSError:
                continue
            for _ in range(10):
                if self.poll() is not None:
                    return
                time.sleep(0.1)


class SSHSession(object):
    def __init__(self, conf, on_output, on_event):
        self.conf = conf
        self.on_output = on_output    # callable(bytes)
        self.on_event = on_event      # callable(event_str, detail_str)
        self.master = None
        self.proc = None
        self.alive = True
        self.lock = threading.Lock()

    def start(self, cols=120, rows=32):
        self.on_event("status", "connecting")
        argv = [
            "ssh", "-tt",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "LogLevel=ERROR",
            "-o", "ServerAliveInterval=30",
            "-o", "ConnectTimeout=10",
            "-o", "NumberOfPasswordPrompts=1",
            "-p", str(self.conf["port"]),
            "%s@%s" % (self.conf["user"], self.conf["host"]),
        ]
        # pty.fork：子进程以 slave 为控制终端（ssh 需要读取 /dev/tty 输入密码）
        pid, master = pty.fork()
        if pid == 0:  # 子进程
            try:
                fcntl.ioctl(0, termios.TIOCSWINSZ,
                            struct.pack("HHHH", max(2, rows), max(2, cols), 0, 0))
            except OSError:
                pass
            try:
                os.execvp(argv[0], argv)
            except Exception:
                pass
            os._exit(127)
        self.master = master
        self.proc = _Proc(pid)
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        """pty 输出 -> WS；同时负责自动输入密码与自动执行命令。"""
        master = self.master
        password = self.conf["password"]
        auto_cmd = self.conf["command"].strip()
        state = {"auth_sent": False, "auth_time": 0.0, "cmd_sent": False,
                 "start_time": time.time(), "denied": False}
        buf = b""
        def tick():
            """公共状态机：连上即回报 connected，并执行自动命令（prompt 识别 + 超时兜底）。"""
            ready = state["auth_sent"] or not password
            if not ready or state["cmd_sent"]:
                return
            tail = buf[-600:].decode("utf-8", "replace")
            base = state["auth_time"] or state["start_time"]
            if RE_SHELL_PROMPT.search(tail) or time.time() - base > 3.5:
                state["cmd_sent"] = True
                self.on_event("status", "connected")
                if auto_cmd:
                    time.sleep(0.3)
                    self._write_master(auto_cmd + "\n")

        try:
            while self.alive:
                r, _, _ = select.select([master], [], [], 0.3)
                if not r:
                    # 认证看门狗：密码提示挂起 20 秒仍未通过 → 报配置问题
                    if (not state["auth_sent"] and password
                            and RE_PASSWD_PROMPT.search(buf[-600:].decode("utf-8", "replace"))
                            and time.time() - state["start_time"] > 20):
                        self.on_event("error",
                                      "SSH 认证超时：密码未通过（检查配置的用户名/密码，注意连接目标是终端里显示的地址）")
                        break
                    tick()
                    if self.proc and self.proc.poll() is not None:
                        break
                    continue
                try:
                    data = os.read(master, 32768)
                except OSError:
                    break
                if not data:
                    break
                self.on_output(data)
                if len(buf) > 65536:
                    buf = buf[-8192:]
                buf += data
                tail = buf[-600:].decode("utf-8", "replace")

                if not state["auth_sent"] and password and RE_PASSWD_PROMPT.search(tail):
                    self._write_master(password + "\n")
                    state["auth_sent"] = True
                    state["auth_time"] = time.time()
                    continue

                if RE_DENIED.search(tail):
                    state["denied"] = True
                    self.on_event("error", "认证失败：请检查用户名或密码（应用设置中可修改）")
                    break

                tick()
        finally:
            if not state["denied"] and self.alive:
                if self.proc is not None and self.proc.poll() is None:
                    self.on_event("error", "SSH 连接失败：无法连接到目标主机（检查地址/端口/网络）")
                else:
                    self.on_event("error", "SSH 会话已结束")
            self.stop()

    def _write_master(self, s):
        try:
            os.write(self.master, s.encode("utf-8"))
        except OSError:
            pass

    def write(self, data):
        """用户键入 -> pty"""
        with self.lock:
            if self.master is not None:
                try:
                    os.write(self.master, data)
                except OSError:
                    pass

    def resize(self, cols, rows):
        with self.lock:
            if self.master is None:
                return
            try:
                fcntl.ioctl(self.master, termios.TIOCSWINSZ,
                            struct.pack("HHHH", max(2, rows), max(2, cols), 0, 0))
            except OSError:
                pass

    def stop(self):
        self.alive = False
        if self.proc:
            try:
                self.proc.terminate()
            except OSError:
                pass
        if self.master is not None:
            try:
                os.close(self.master)
            except OSError:
                pass
            self.master = None


# ---------------- WebSocket 会话 ----------------

def run_ws_session(conn, rfile):
    state = {"alive": True}
    lock = threading.Lock()

    def send_frame(payload, opcode):
        with lock:
            ws_send(conn, payload, opcode)

    def on_output(data):
        try:
            send_frame(data, 0x2)
        except Exception:
            state["alive"] = False

    def on_event(etype, detail):
        try:
            send_frame(json.dumps({"type": etype, "data": detail}).encode("utf-8"), 0x1)
        except Exception:
            pass

    conf = load_conf()
    if not conf.get("_loaded"):
        on_event("error",
                 "未读取到配置文件：%s\r\n请在应用的「运行设置」中保存一次配置，或重新安装本应用（安装向导会写入配置）。"
                 % conf.get("_path", ""))
        try:
            send_frame(b"", 0x8)
        except Exception:
            pass
        return

    sess = SSHSession(conf, on_output, on_event)
    on_event("notice", "正在连接 %s@%s:%s ...\r\n" % (conf["user"], conf["host"], conf["port"]))
    sess.start()

    frag = b""
    try:
        while state["alive"]:
            fr = ws_recv_frame(rfile)
            if fr is None:
                break
            fin, opcode, payload = fr
            if opcode == 0x8:  # close
                break
            if opcode == 0x9:  # ping -> pong
                send_frame(payload, 0xA)
                continue
            if opcode == 0xA:  # pong
                continue
            if opcode == 0x0:  # continuation
                frag += payload
                if fin:
                    payload, frag = frag, b""
                else:
                    continue
            elif not fin:
                frag = payload
                continue

            try:
                msg = json.loads(payload.decode("utf-8", "replace"))
            except ValueError:
                continue
            mtype = msg.get("type")
            if mtype == "input":
                sess.write(str(msg.get("data", "")).encode("utf-8"))
            elif mtype == "resize":
                try:
                    sess.resize(int(msg.get("cols", 80)), int(msg.get("rows", 24)))
                except (TypeError, ValueError):
                    pass
    except Exception:
        pass
    finally:
        state["alive"] = False
        sess.stop()
        try:
            send_frame(b"", 0x8)
        except Exception:
            pass


# ---------------- HTTP ----------------

MIMES = {
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "fnssh"

    def log_message(self, fmt, *args):
        pass  # 静默访问日志

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path != "/" and path.endswith("/"):
            path = path.rstrip("/")
        if path in ("", "/", "/index.html", "/app/fnssh"):
            return self._serve_file("index.html")
        m = re.match(r"^/(?:app/fnssh)?/?static/([A-Za-z0-9._-]+)$", path)
        if m:
            return self._serve_file("static/" + m.group(1))
        if path in ("/ws", "/app/fnssh/ws"):
            return self._websocket()
        self.send_error(404)

    def _serve_file(self, rel):
        fpath = os.path.normpath(os.path.join(WWW, rel))
        if not fpath.startswith(WWW) or not os.path.isfile(fpath):
            self.send_error(404)
            return
        try:
            with open(fpath, "rb") as f:
                body = f.read()
        except OSError:
            self.send_error(404)
            return
        ext = os.path.splitext(fpath)[1].lower()
        self.send_response(200)
        self.send_header("Content-Type", MIMES.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        self.wfile.write(body)

    def _websocket(self):
        key = self.headers.get("Sec-WebSocket-Key")
        upgrade = (self.headers.get("Upgrade") or "").lower()
        if not key or "websocket" not in upgrade:
            self.send_error(400)
            return
        accept = base64.b64encode(
            hashlib.sha1((key + WS_GUID).encode("ascii")).digest()
        ).decode("ascii")
        self.connection.sendall(
            ("HTTP/1.1 101 Switching Protocols\r\n"
             "Upgrade: websocket\r\n"
             "Connection: Upgrade\r\n"
             "Sec-WebSocket-Accept: %s\r\n\r\n" % accept).encode("ascii")
        )
        self.close_connection = True
        try:
            run_ws_session(self.connection, self.rfile)
        except Exception as e:
            sys.stderr.write("[fnssh] ws session error: %s\n" % e)


class ThreadingUnixHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    """Unix Socket 版 HTTP 服务（供飞牛统一网关转发）。"""
    daemon_threads = True


def main():
    servers = []

    # 1) 统一网关通道：Unix Socket（gatewaySocket=fnssh.sock）
    appdest = os.environ.get("TRIM_APPDEST") or "/tmp"
    sock_path = os.path.join(appdest, "fnssh.sock")
    try:
        if not os.path.isdir(appdest):
            os.makedirs(appdest, exist_ok=True)
        if os.path.exists(sock_path):
            os.unlink(sock_path)
        unix_srv = ThreadingUnixHTTPServer(sock_path, Handler)
        os.chmod(sock_path, 0o666)
        servers.append(unix_srv)
        print("[fnssh] unix socket: %s (gateway /app/fnssh)" % sock_path, flush=True)
    except OSError as e:
        print("[fnssh] unix socket bind failed: %s" % e, flush=True)

    # 2) 直连通道：TCP 8899（兜底，占用则跳过）
    try:
        port = int(os.environ.get("TRIM_SERVICE_PORT") or os.environ.get("PORT") or 8899)
    except ValueError:
        port = 8899
    try:
        tcp_srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
        tcp_srv.daemon_threads = True
        servers.append(tcp_srv)
        print("[fnssh] listening on 0.0.0.0:%d" % port, flush=True)
    except OSError as e:
        print("[fnssh] tcp port %d unavailable: %s (gateway only)" % (port, e), flush=True)

    if not servers:
        sys.exit(1)

    def shutdown(*_a):
        os._exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    import threading
    for srv in servers[1:]:
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    servers[0].serve_forever()


if __name__ == "__main__":
    main()
