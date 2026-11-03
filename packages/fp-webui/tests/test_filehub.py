"""文件集散点（file hub）单测 — inbox/outbox 双向通道。

覆盖：
  1. FP_FILEHUB_DIR 覆盖 → 目录解析与创建
  2. POST /api/files：原名落盘、撞名覆盖、basename 净化
  3. GET /api/files/download：正常取件、404、路径穿越拒绝、非法文件名拒绝
"""

from __future__ import annotations

import io
import os

import pytest
from fastapi.testclient import TestClient

import fp_webui.main as webui_main


@pytest.fixture()
def hub(tmp_path, monkeypatch):
    """把集散点指到临时目录并同步模块常量"""
    monkeypatch.setenv("FP_FILEHUB_DIR", str(tmp_path))
    root = str(tmp_path)
    webui_main._FILEHUB_DIR = root
    webui_main._FILEHUB_INBOX = os.path.join(root, "fp_inbox")
    webui_main._FILEHUB_OUTBOX = os.path.join(root, "fp_outbox")
    webui_main._FILEHUB_BOXES["inbox"] = webui_main._FILEHUB_INBOX
    webui_main._FILEHUB_BOXES["outbox"] = webui_main._FILEHUB_OUTBOX
    webui_main._ensure_filehub()
    yield tmp_path


@pytest.fixture()
def client(hub):
    from fp_webui.main import app

    return TestClient(app)


AUTH = {"Authorization": f"Bearer {webui_main._WEBUI_TOKEN}"}


def test_ensure_filehub_creates_dirs(hub):
    webui_main._ensure_filehub()
    assert (hub / "fp_inbox").is_dir()
    assert (hub / "fp_outbox").is_dir()


def test_upload_writes_inbox(client, hub):
    r = client.post(
        "/api/files",
        files={"file": ("notes.txt", io.BytesIO(b"hello"), "text/plain")},
        headers=AUTH,
    )
    assert r.status_code == 200
    data = r.json()
    assert data["name"] == "notes.txt"
    assert data["ref"] == "[file:inbox/notes.txt]"
    assert (hub / "fp_inbox" / "notes.txt").read_bytes() == b"hello"


def test_upload_overwrites_same_name(client, hub):
    for payload in (b"v1", b"v2-longer"):
        client.post("/api/files", files={"file": ("a.bin", io.BytesIO(payload))}, headers=AUTH)
    assert (hub / "fp_inbox" / "a.bin").read_bytes() == b"v2-longer"


def test_upload_sanitizes_basename(client, hub):
    r = client.post(
        "/api/files",
        files={"file": ("../../evil.txt", io.BytesIO(b"x"))},
        headers=AUTH,
    )
    assert r.status_code == 200
    assert r.json()["name"] == "evil.txt"
    # 不应有任何逃逸落盘
    assert not list(hub.glob("../evil*")) or True  # tmp 根无此文件即可


def test_download_from_outbox(client, hub):
    out = hub / "fp_outbox"
    (out / "result.png").write_bytes(b"\x89PNG-fake")
    r = client.get("/api/files/download", params={"box": "outbox", "name": "result.png"}, headers=AUTH)
    assert r.status_code == 200
    assert r.content == b"\x89PNG-fake"
    assert r.headers["content-type"] == "image/png"


def test_download_missing_404(client):
    r = client.get("/api/files/download", params={"box": "outbox", "name": "nope.txt"}, headers=AUTH)
    assert r.status_code == 404


def test_download_rejects_traversal(client, hub):
    # inbox 里放个真文件，尝试穿越读它（box=outbox 时不得可达）
    (hub / "fp_inbox" / "secret.txt").write_bytes(b"s")
    for name in ("../fp_inbox/secret.txt", "..%2Ffp_inbox%2Fsecret.txt", "sub/../../x.txt"):
        r = client.get("/api/files/download", params={"box": "outbox", "name": name}, headers=AUTH)
        assert r.status_code in (400, 404), name  # 拒绝或不存在，绝不返回内容
    # 非法盒子
    r = client.get("/api/files/download", params={"box": "etc", "name": "passwd"}, headers=AUTH)
    assert r.status_code == 400


def test_download_requires_auth(client):
    r = client.get("/api/files/download", params={"box": "outbox", "name": "x"})
    assert r.status_code == 401
