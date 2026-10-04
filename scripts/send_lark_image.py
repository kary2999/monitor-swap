#!/usr/bin/env python3
"""Upload an image to the configured Feishu/Lark home channel and verify it."""

from __future__ import annotations

import json
import mimetypes
import os
import re
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

import yaml

HOME = Path.home() / ".hermes"


def latest_feishu_dm_chat_id():
    """Return the newest real Feishu DM route recorded by Hermes."""
    db = sqlite3.connect(HOME / "state.db")
    try:
        row = db.execute(
            "SELECT chat_id FROM sessions "
            "WHERE lower(source) = 'feishu' AND chat_type = 'dm' "
            "AND chat_id IS NOT NULL AND chat_id <> '' "
            "ORDER BY last_activity_at DESC LIMIT 1"
        ).fetchone()
        return row[0] if row else None
    finally:
        db.close()


def latest_feishu_open_id():
    """Recover the latest exact inbound Lark open_id from gateway logs."""
    candidates = sorted((HOME / "logs").glob("gateway.log*"), key=lambda p: p.stat().st_mtime)
    pattern = re.compile(r"sender=user:(ou_[A-Za-z0-9]+)")
    found = None
    for log in candidates:
        for match in pattern.finditer(log.read_text(errors="ignore")):
            found = match.group(1)
    return found


def load_env():
    data = {}
    p = HOME / ".env"
    for raw in p.read_text(errors="ignore").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            data[k.strip()] = v.strip().strip('"').strip("'")
    data.update(os.environ)
    return data


def request_json(method, url, body=None, token=None, headers=None):
    hdr = dict(headers or {})
    if token:
        hdr["Authorization"] = "Bearer " + token
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        hdr["Content-Type"] = "application/json; charset=utf-8"
    req = urllib.request.Request(url, data=data, headers=hdr, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            payload = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            payload = {"code": -1, "msg": "non-JSON HTTP error"}
        return exc.code, payload


def multipart_image(path):
    boundary = "----Hermes" + uuid.uuid4().hex
    raw = path.read_bytes()
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    parts = [
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"image_type\"\r\n\r\nmessage\r\n".encode(),
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"{path.name}\"\r\nContent-Type: {mime}\r\n\r\n".encode(),
        raw,
        f"\r\n--{boundary}--\r\n".encode(),
    ]
    return boundary, b"".join(parts)


def main():
    image = Path(sys.argv[1]).expanduser().resolve()
    env = load_env()
    cfg = yaml.safe_load((HOME / "config.yaml").read_text()) or {}
    feishu = (((cfg.get("gateway") or {}).get("platforms") or {}).get("feishu") or {})
    home = feishu.get("home_channel") or {}
    # “发给我”优先走 Hermes 最近实际收过消息的 Feishu 私聊，
    # 避免 home_channel 指向 Bot 已退出的旧群聊。
    chat_id = latest_feishu_dm_chat_id() or home.get("chat_id") or env.get("FEISHU_HOME_CHANNEL")
    app_id = env.get("FEISHU_APP_ID")
    secret = env.get("FEISHU_APP_SECRET")
    domain = env.get("FEISHU_DOMAIN", "lark").lower()
    base = "https://open.larksuite.com" if "lark" in domain else "https://open.feishu.cn"
    if not image.is_file() or not chat_id or not app_id or not secret:
        raise SystemExit("missing image or configured Feishu home channel credentials")

    _, auth = request_json("POST", base + "/open-apis/auth/v3/tenant_access_token/internal", {"app_id": app_id, "app_secret": secret})
    if auth.get("code", 0) != 0:
        raise SystemExit("Feishu authentication failed")
    token = auth["tenant_access_token"]

    boundary, payload = multipart_image(image)
    req = urllib.request.Request(
        base + "/open-apis/im/v1/images",
        data=payload,
        headers={"Authorization": "Bearer " + token, "Content-Type": "multipart/form-data; boundary=" + boundary},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        uploaded = json.loads(resp.read())
    image_key = uploaded.get("data", {}).get("image_key")
    if uploaded.get("code") != 0 or not image_key:
        raise SystemExit("Feishu image upload failed")

    content = json.dumps({"image_key": image_key})
    url = base + "/open-apis/im/v1/messages?" + urllib.parse.urlencode({"receive_id_type": "chat_id"})
    send_status, sent = request_json("POST", url, {"receive_id": chat_id, "msg_type": "image", "content": content}, token)
    if sent.get("code") == 230002:
        open_id = latest_feishu_open_id()
        if open_id:
            url = base + "/open-apis/im/v1/messages?" + urllib.parse.urlencode({"receive_id_type": "open_id"})
            send_status, sent = request_json("POST", url, {"receive_id": open_id, "msg_type": "image", "content": content}, token)
    message_id = sent.get("data", {}).get("message_id")
    if sent.get("code") != 0 or not message_id:
        print(json.dumps({"stage": "send", "http_status": send_status, "code": sent.get("code"), "msg": sent.get("msg")}, ensure_ascii=False))
        raise SystemExit("Feishu image send failed")

    status, checked = request_json("GET", base + "/open-apis/im/v1/messages/" + message_id, token=token)
    items = checked.get("data", {}).get("items", [])
    verified = status == 200 and checked.get("code") == 0 and any(x.get("msg_type") == "image" for x in items)
    print(json.dumps({"uploaded": True, "sent": True, "verified": verified, "msg_type": "image"}, ensure_ascii=False))
    if not verified:
        raise SystemExit(4)


if __name__ == "__main__":
    main()
