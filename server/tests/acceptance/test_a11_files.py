"""A11 Files (ART-01): traversal filenames cannot escape, stale versions are 409, scanned PDFs are a
clear unsupported_format result; plus versioning, preview safety, download authorization."""
import io
import os
from pathlib import Path

from httpx import ASGITransport
from pypdf import PdfReader

from tests.conftest import Client
from tests.test_m4_units import scanned_pdf


async def upload(client, name: str, data: bytes, ctype: str = "text/plain"):
    return await client.post("/api/artifacts", params={"filename": name}, content=data, headers={"content-type": ctype})


def files_under(root: str) -> set[Path]:
    return {Path(dp, f).resolve() for dp, _, fs in os.walk(root) for f in fs}


async def test_a11_traversal_filenames_cannot_escape(client, settings):
    root = Path(settings.artifact_dir).resolve()
    before_parent = set(os.listdir(root.parent))
    names = ["../../../../etc/passwd.txt", "..\\..\\evil.md", "/tmp/abs.csv", "sub/../../x.md", "..%2f..%2fenc.txt", "\u202etxt.md"]
    for i, n in enumerate(names):
        r = await upload(client, n, f"payload {i}".encode())
        assert r.status_code == 201, r.text
        body = r.json()
        assert "/" not in body["name"] and "\\" not in body["name"] and not body["name"].startswith(".")
    stored = files_under(root)
    assert len(stored) == len(names)
    for p in stored:  # every file is {root}/{owner}/{xx}/{sha256}; names never reach the filesystem
        rel = p.relative_to(root).parts
        assert len(rel) == 3 and len(rel[2]) == 64 and rel[1] == rel[2][:2]
    assert set(os.listdir(root.parent)) == before_parent
    assert not Path("/tmp/abs.csv").exists() or Path("/tmp/abs.csv").read_bytes() != b"payload 2"


async def test_a11_stale_version_is_a_409_and_history_is_kept(client):
    r = await client.post("/api/artifacts/markdown", json={"name": "Brief", "content": "# Brief\n\nFirst paragraph.\n\nSecond paragraph.\n"})
    assert r.status_code == 201, r.text
    art = r.json()
    assert art["name"] == "Brief.md" and art["current_version"] == 1
    r = await client.patch(f"/api/artifacts/{art['id']}", json={"expected_version": 1, "content": "# Brief\n\nFirst paragraph, revised.\n\nSecond paragraph.\n"})
    assert r.status_code == 200 and r.json()["current_version"] == 2
    stale = await client.patch(f"/api/artifacts/{art['id']}", json={"expected_version": 1, "content": "# lost update"})
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "version_conflict"
    versions = (await client.get(f"/api/artifacts/{art['id']}/versions")).json()
    assert [v["version"] for v in versions] == [2, 1]
    v1 = await client.get(f"/api/artifacts/{art['id']}/versions/1/download")
    v2 = await client.get(f"/api/artifacts/{art['id']}/versions/2/download")
    assert v1.content.decode().count("First paragraph.") == 1 and "revised" in v2.text and "lost update" not in v2.text
    assert v1.headers["content-disposition"].startswith('attachment; filename="Brief-v1.md"')
    assert v1.headers["x-content-type-options"] == "nosniff" and "sandbox" in v1.headers["content-security-policy"]


async def test_a11_scanned_pdf_is_unsupported_format(client, settings):
    r = await upload(client, "scan.pdf", scanned_pdf(), "application/pdf")
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "unsupported_format" and "OCR" in err["message"]
    assert (await client.get("/api/artifacts")).json() == []
    assert files_under(settings.artifact_dir) == set()  # rejected before any byte was stored


async def test_a11_type_mismatch_and_size_limit(client, settings):
    r = await upload(client, "report.pdf", b"not really a pdf", "application/pdf")
    assert r.status_code == 422 and r.json()["error"]["code"] == "type_mismatch"
    r = await upload(client, "photo.txt", b"\x89PNG\r\n\x1a\n\x00\x00", "text/plain")
    assert r.status_code == 422 and r.json()["error"]["code"] == "type_mismatch"
    settings.upload_max_bytes = 1024
    r = await upload(client, "big.txt", b"a" * 2048)
    assert r.status_code == 413 and r.json()["error"]["code"] == "too_large"


async def test_a11_text_pdf_upload_extracts_and_previews(client):
    from opa.artifacts.render import markdown_to_pdf

    pdf = markdown_to_pdf("# Q3 metrics\n\n" + "Revenue grew and churn fell across all regions this quarter. " * 5, "metrics")
    r = await upload(client, "metrics.pdf", pdf, "application/pdf")
    assert r.status_code == 201, r.text
    art = r.json()
    assert art["mime"] == "application/pdf" and art["current"]["note"]["extraction"] == "ok"
    p = (await client.get(f"/api/artifacts/{art['id']}/versions/1/preview")).json()
    assert p["kind"] == "pdf" and "Revenue grew" in p["text"]
    dl = await client.get(f"/api/artifacts/{art['id']}/versions/1/download")
    assert dl.headers["content-type"] == "application/pdf" and PdfReader(io.BytesIO(dl.content)).pages


async def test_markdown_preview_is_sanitized_and_render_pdf_contains_text(client):
    r = await client.post("/api/artifacts/markdown", json={"name": "x", "content": "# Hi\n\n<script>alert(1)</script>\n\n[bad](javascript:alert(1))\n"})
    art = r.json()
    p = (await client.get(f"/api/artifacts/{art['id']}/versions/1/preview")).json()
    assert p["kind"] == "markdown" and "<script" not in p["html"] and "&lt;script&gt;" in p["html"] and 'href="javascript' not in p["html"]
    pdf = await client.post(f"/api/artifacts/{art['id']}/render-pdf", json={"version": 1})
    assert pdf.status_code == 201
    dl = await client.get(f"/api/artifacts/{pdf.json()['id']}/versions/1/download")
    assert "Hi" in PdfReader(io.BytesIO(dl.content)).pages[0].extract_text()


async def test_files_are_owner_only(client, app, other_owner, anon):
    art = (await client.post("/api/artifacts/markdown", json={"name": "private", "content": "secret"})).json()
    paths = [f"/api/artifacts/{art['id']}", f"/api/artifacts/{art['id']}/versions", f"/api/artifacts/{art['id']}/versions/1/download",
             f"/api/artifacts/{art['id']}/versions/1/preview"]
    for p in paths:
        assert (await anon.get(p)).status_code == 401
    async with Client(transport=ASGITransport(app=app), base_url="http://test") as other:
        await other.sign_in("other@example.com", "another long password")
        for p in paths:
            r = await other.get(p)
            assert r.status_code == 404 and "secret" not in r.text
        assert (await other.patch(f"/api/artifacts/{art['id']}", json={"expected_version": 1, "content": "x"})).status_code == 404
        assert (await other.delete(f"/api/artifacts/{art['id']}")).status_code == 404
        assert (await other.get("/api/artifacts")).json() == []


async def test_delete_hides_file_and_gc_removes_bytes(client, settings, sessionmaker):
    from datetime import timedelta

    from opa.artifacts.service import gc_orphans

    art = (await client.post("/api/artifacts/markdown", json={"name": "gone", "content": "bye"})).json()
    assert len(files_under(settings.artifact_dir)) == 1
    assert (await client.delete(f"/api/artifacts/{art['id']}")).status_code == 204
    assert (await client.get(f"/api/artifacts/{art['id']}/versions/1/download")).status_code == 404
    assert await gc_orphans(sessionmaker, settings, timedelta(0)) == 1
    assert files_under(settings.artifact_dir) == set()
