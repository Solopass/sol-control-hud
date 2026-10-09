import pytest
from sol_control_hud import launcher


def test_bulk_add_solang_empty():
    r = launcher.bulk_add_solang("")
    assert not r["ok"]
    assert "please paste" in r["why"]


def test_bulk_add_solang_with_links(monkeypatch):
    class FakePopen:
        calls = []
        def __init__(self, cmd, **kw):
            FakePopen.calls.append(cmd)

    # Mock urllib.request
    import urllib.request
    import io
    import json

    class FakeResp:
        status = 200
        def __init__(self, data=b"{}"):
            self._data = data
        def read(self):
            return self._data
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass

    def fake_urlopen(req, timeout=1.0):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if "/api/health" in url:
            return FakeResp(b'{"status":"ok"}')
        if "/api/youtube/bulk-ingest" in url:
            res = {"count": 2, "items": [{"videoId": "vid1", "status": "ingested", "title": "Song 1"}]}
            return FakeResp(json.dumps(res).encode("utf-8"))
        return FakeResp(b"{}")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    r = launcher.bulk_add_solang("https://youtube.com/watch?v=11cta61wi0g", popen=FakePopen)
    assert r["ok"]
    assert "Processed 2 track(s)" in r["why"]
    assert len(r["items"]) == 1
