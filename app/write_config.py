#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把向导收集的 SSH 配置合并写入 config.json。

由 cmd/install_callback、config_callback、upgrade_callback 调用。
放在 app/ 目录下（app/ 会被完整部署；cmd/ 下自定义脚本不会）。
只依赖标准库，写入失败也返回 0，绝不阻断安装流程。
"""

import json
import os
import sys

MAPPING = {
    "wizard_ssh_host": "host",
    "wizard_ssh_port": "port",
    "wizard_ssh_user": "user",
    "wizard_ssh_password": "password",
    "wizard_ssh_command": "command",
}

DEFAULTS = {
    "host": "127.0.0.1",
    "port": "22",
    "user": "admin",
    "password": "",
    "command": "",
}


def log_path():
    var = os.environ.get("TRIM_PKGVAR", "")
    if var:
        return os.path.join(var, "install.log")
    return None


def log(msg):
    p = log_path()
    if not p:
        return
    try:
        with open(p, "a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except OSError:
        pass


def target_dirs():
    dirs = []
    for key in ("TRIM_PKGETC", "TRIM_PKGVAR"):
        d = os.environ.get(key, "")
        if d and d not in dirs:
            dirs.append(d)
    if not dirs:
        here = os.path.dirname(os.path.abspath(__file__))
        dirs.append(here)
    return dirs


def merge(path):
    conf = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                conf = json.load(f)
        except Exception:
            conf = {}

    for env_key, conf_key in MAPPING.items():
        val = os.environ.get(env_key, "")
        if val != "":
            conf[conf_key] = val

    for k, v in DEFAULTS.items():
        conf.setdefault(k, v)

    # 先写临时文件再替换，避免写入中断留下半截 JSON
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(conf, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return conf


def main():
    log("write_config.py: PKGETC=[%s] PKGVAR=[%s] APPDEST=[%s]" % (
        os.environ.get("TRIM_PKGETC", ""),
        os.environ.get("TRIM_PKGVAR", ""),
        os.environ.get("TRIM_APPDEST", "")))
    log("write_config.py: wizard vars " + " ".join(
        "%s=%s" % (k, "SET" if os.environ.get(k) else "EMPTY") for k in MAPPING))

    ok = 0
    for d in target_dirs():
        try:
            os.makedirs(d, exist_ok=True)
            path = os.path.join(d, "config.json")
            conf = merge(path)
            ok += 1
            log("write_config.py: wrote %s => host=%s port=%s user=%s password=%s command=%s" % (
                path, conf.get("host"), conf.get("port"), conf.get("user"),
                "SET" if conf.get("password") else "EMPTY", conf.get("command", "")))
        except Exception as e:
            log("write_config.py: FAILED %s => %s" % (d, e))

    return 0 if ok else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        try:
            log("write_config.py: fatal %s" % e)
        except Exception:
            pass
        sys.exit(0)
