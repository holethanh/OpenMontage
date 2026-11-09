"""Tests for the Backlot affiliate toolkit (logic + API)."""

from __future__ import annotations

import io
import json

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from backlot import affiliate as aff
from backlot import server as server_mod
from backlot import state as state_mod


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    root.mkdir()
    monkeypatch.setattr(state_mod, "PROJECTS_DIR", root)
    monkeypatch.setattr(server_mod, "PROJECTS_DIR", root)
    monkeypatch.setattr(server_mod, "_summary_cache", {})
    monkeypatch.setattr(aff, "AFFILIATE_DIR", tmp_path / "affiliate")
    return root


@pytest.fixture
def client(env, monkeypatch):
    async def no_watch():
        return None

    monkeypatch.setattr(server_mod, "_watch_projects", no_watch)
    with TestClient(server_mod.create_app(), base_url="http://127.0.0.1:4750") as c:
        yield c


PRODUCT = {
    "name": "Máy xay cầm tay Đà Nẵng",
    "affiliate_url": "https://example.com/aff?id=1",
    "price": 499000,
    "currency": "VND",
    "description": "Nhỏ gọn, sạc USB-C. Pin dùng được 10 lần xay.",
    "network": "shopee",
    "commission_pct": 10,
}


def _png(size=(8, 8), fmt="PNG") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (200, 30, 30)).save(buf, fmt)
    return buf.getvalue()


def _product(client, **over):
    r = client.post("/api/affiliate/products", json={**PRODUCT, **over})
    assert r.status_code == 200, r.text
    return r.json()


# ---- product hub -----------------------------------------------------------

def test_slugify_strips_vietnamese():
    assert aff.slugify("Máy xay cầm tay Đà Nẵng") == "may-xay-cam-tay-da-nang"
    assert aff.slugify("!!!", "x") == "x"


def test_product_crud_and_unique_ids(client):
    a = _product(client)
    b = _product(client)
    assert a["id"] == "may-xay-cam-tay-da-nang" and b["id"].endswith("-2")
    assert len(client.get("/api/affiliate/products").json()) == 2
    r = client.put(f"/api/affiliate/products/{a['id']}", json={**PRODUCT, "price": 1})
    assert r.json()["price"] == 1 and r.json()["created_at"] == a["created_at"]
    assert client.delete(f"/api/affiliate/products/{a['id']}").json() == {"ok": True}
    assert client.delete(f"/api/affiliate/products/{a['id']}").status_code == 404
    assert client.put("/api/affiliate/products/nope", json=PRODUCT).status_code == 404


def test_product_validation(client):
    assert client.post("/api/affiliate/products", json={**PRODUCT, "name": "  "}).status_code == 422
    assert client.post("/api/affiliate/products", json={**PRODUCT, "affiliate_url": "javascript:alert(1)"}).status_code == 422
    assert client.post("/api/affiliate/products", json={**PRODUCT, "commission_pct": 101}).status_code == 422
    assert client.post("/api/affiliate/products", json={**PRODUCT, "price": -1}).status_code == 422
    assert _product(client, network="weird")["network"] == "other"


def test_corrupt_store_is_tolerated(env):
    aff.AFFILIATE_DIR.mkdir(parents=True)
    (aff.AFFILIATE_DIR / "store.json").write_text("{not json", encoding="utf-8")
    assert aff.list_products() == []


# ---- templates / hooks / copy ----------------------------------------------

def test_templates_reference_real_pipelines_and_styles():
    from lib.pipeline_loader import list_pipelines
    from styles.playbook_loader import list_playbooks

    pipelines, styles = set(list_pipelines()), set(list_playbooks())
    assert len(aff.TEMPLATES) == 5
    for tid, tpl in aff.TEMPLATES.items():
        assert tpl["pipeline"] in pipelines, tid
        assert tpl["style"] in styles, tid
        assert set(tpl["hook_angles"]) <= set(aff.HOOKS), tid


@pytest.mark.parametrize("lang", ["vi", "en"])
def test_hooks_distinct_and_counted(lang):
    product = {**PRODUCT, "id": "p"}
    for template in aff.TEMPLATES:
        out = aff.generate_hooks(product, template, lang, 8)
        texts = [h["text"] for h in out["hooks"]]
        assert len(texts) == 8 and len(set(texts)) == 8
        assert all("{" not in t for t in texts)
        assert out["ctas"]


def test_hooks_skip_price_angle_without_price():
    out = aff.generate_hooks({"name": "X"}, "top_n", "vi", 6)
    assert all(h["angle"] != "price" for h in out["hooks"])
    assert all("{" not in h["text"] for h in out["hooks"])


def test_hooks_invalid_inputs():
    with pytest.raises(aff.AffiliateError):
        aff.generate_hooks({"name": "X"}, "nope", "vi")
    with pytest.raises(aff.AffiliateError):
        aff.generate_hooks({"name": "X"}, "review", "fr")


def test_copy_link_rules_and_disclosure():
    p = {**PRODUCT, "network": "amazon"}
    tiktok = aff.build_copy(p, "tiktok", "en", "Hook", "CTA")
    assert p["affiliate_url"] not in tiktok["caption"] and tiktok["link_note"]
    assert "Amazon Associate" in tiktok["caption"]
    youtube = aff.build_copy(p, "youtube", "en", "Hook", "CTA")
    assert p["affiliate_url"] in youtube["caption"] and not youtube["link_note"]
    assert len(tiktok["hashtags"]) <= 5 and len(youtube["hashtags"]) <= 3
    assert aff.check_compliance(tiktok["caption"], "tiktok", True, "amazon", "en")["status"] == "pass"


def test_copy_flags_over_limit():
    assert not aff.build_copy(PRODUCT, "tiktok", "vi", "x" * 2300, "c")["within_limit"]


# ---- compliance ------------------------------------------------------------

def test_compliance_missing_disclosure_fails():
    r = aff.check_compliance("Mua ngay nhé", "tiktok", True)
    assert r["status"] == "fail" and r["issues"][0]["code"] == "missing_disclosure"
    assert aff.check_compliance("Mua ngay nhé", "tiktok", False)["status"] == "pass"


@pytest.mark.parametrize("text,code", [
    ("#ad Guaranteed results", "guarantee"),
    ("#ad cam kết hiệu quả 100%", "guarantee"),
    ("#ad sản phẩm này chữa khỏi bệnh", "health_claim"),
    ("#ad no side effects", "no_side_effects"),
    ("#ad tốt nhất thế giới", "superlative"),
    ("#ad chỉ còn hôm nay", "fake_urgency"),
])
def test_compliance_risky_claims(text, code):
    assert code in {i["code"] for i in aff.check_compliance(text, "tiktok", True)["issues"]}


def test_compliance_no_false_positive_on_clean_text():
    assert aff.check_compliance("#ad Mình thấy máy nhỏ gọn, pin ổn.", "tiktok", True)["status"] == "pass"


def test_compliance_endpoint_validation(client):
    ok = client.post("/api/affiliate/compliance", json={"text": "hi", "has_affiliate_link": True})
    assert ok.status_code == 200 and ok.json()["status"] == "fail"
    assert client.post("/api/affiliate/compliance", json={"text": "hi", "platform": "myspace"}).status_code == 400


# ---- batch -----------------------------------------------------------------

def test_batch_creates_valid_projects(client, env):
    p = _product(client)
    r = client.post("/api/affiliate/batch", json={
        "product_id": p["id"], "templates": ["review", "top_n"], "hooks_per_template": 2, "platform": "tiktok"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 4 and body["series"] == "affiliate-may-xay-cam-tay-da-nang"
    ids = [x["project_id"] for x in body["projects"]]
    assert len(set(ids)) == 4
    for pid in ids:
        marker = json.loads((env / pid / "project.json").read_text(encoding="utf-8"))
        assert marker["series"] == body["series"] and marker["affiliate"]["product_id"] == p["id"]
        brief = json.loads((env / pid / "artifacts" / "affiliate_brief.json").read_text(encoding="utf-8"))
        assert brief["copy"]["disclosure"] and brief["hook"]["text"] in brief["agent_instructions"]
        assert client.get(f"/api/project/{pid}/state").status_code == 200
    assert {s["series"] for s in client.get("/api/projects").json()} == {body["series"]}
    # second batch must not collide with existing folders
    again = client.post("/api/affiliate/batch", json={"product_id": p["id"], "templates": ["review"]}).json()
    assert not set(x["project_id"] for x in again["projects"]) & set(ids)


def test_batch_errors(client):
    p = _product(client)
    assert client.post("/api/affiliate/batch", json={"product_id": "nope", "templates": ["review"]}).status_code == 404
    assert client.post("/api/affiliate/batch", json={"product_id": p["id"], "templates": ["zzz"]}).status_code == 400
    assert client.post("/api/affiliate/batch", json={"product_id": p["id"], "templates": []}).status_code == 422
    too_big = {"product_id": p["id"], "templates": list(aff.TEMPLATES), "hooks_per_template": 5,
               "aspect_ratios": ["9:16", "16:9"]}
    assert client.post("/api/affiliate/batch", json=too_big).status_code == 400


# ---- results ---------------------------------------------------------------

def test_results_metrics_and_aggregation(client, env):
    p = _product(client)
    ids = [x["project_id"] for x in client.post("/api/affiliate/batch", json={
        "product_id": p["id"], "templates": ["review", "top_n"]}).json()["projects"]]
    base = {"project_id": ids[0], "views": 1000, "clicks": 50, "orders": 5, "commission": 100.0, "cost": 40.0}
    assert client.post("/api/affiliate/results", json=base).status_code == 200
    assert client.post("/api/affiliate/results", json={**base, "views": 500, "clicks": 10, "orders": 1,
                                                     "commission": 20.0, "cost": 0}).status_code == 200
    # commission derived from price*commission_pct when omitted
    assert client.post("/api/affiliate/results", json={"project_id": ids[1], "views": 10, "clicks": 2, "orders": 2}).status_code == 200
    s = client.get("/api/affiliate/results").json()
    top = next(x for x in s["projects"] if x["project_id"] == ids[0])
    assert top["views"] == 1500 and top["commission"] == 120 and top["cost"] == 40 and top["profit"] == 80
    assert top["ctr"] == round(60 / 1500, 4) and top["cvr"] == round(6 / 60, 4) and top["roi"] == 2.0
    derived = next(x for x in s["projects"] if x["project_id"] == ids[1])
    assert derived["commission"] == round(2 * 499000 * 10 / 100, 2)
    assert {x["template"] for x in s["by_template"]} == {"review", "top_n"}
    assert s["totals"]["orders"] == 8
    rid = s["entries"][0]["id"]
    assert client.delete(f"/api/affiliate/results/{rid}").status_code == 200
    assert client.delete(f"/api/affiliate/results/{rid}").status_code == 404


def test_results_validation(client):
    assert client.post("/api/affiliate/results", json={"project_id": "ghost"}).status_code == 404
    assert client.post("/api/affiliate/results", json={"project_id": "../x"}).status_code == 404
    assert client.post("/api/affiliate/results", json={"project_id": "a", "views": -1}).status_code == 422
    assert client.post("/api/affiliate/results", json={"project_id": "a", "day": "31/12"}).status_code == 422
    empty = client.get("/api/affiliate/results").json()
    assert empty["totals"]["ctr"] is None and empty["projects"] == []


# ---- description / image upload ---------------------------------------------

def test_description_replaces_old_fields(client):
    p = _product(client)
    assert p["description"].startswith("Nhỏ gọn")
    assert "selling_points" not in p and "category" not in p and "image_url" not in p
    assert p["image_file"] == ""


def test_hooks_use_description_phrases():
    pts = aff.description_points("Nhỏ gọn, sạc USB-C. Pin dùng được 10 lần xay!")
    assert pts == ["Nhỏ gọn", "sạc USB-C", "Pin dùng được 10 lần xay"]
    out = aff.generate_hooks({"name": "X", "description": "Pin trâu. Nhẹ"}, "top_n", "vi", 8)
    assert any("pin trâu" in h["text"] for h in out["hooks"] if h["angle"] in ("benefit", "problem"))
    assert aff.description_points("") == [] and aff.description_points("x" * 200) == []


def test_legacy_selling_points_still_feed_description():
    legacy = {"name": "Old", "selling_points": ["Bền", "Rẻ"]}
    assert aff.product_description(legacy) == "Bền. Rẻ"
    assert aff.generate_hooks(legacy, "review", "vi", 3)["hooks"]


def test_image_upload_serve_replace_delete(client, env):
    p = _product(client)
    url = f"/api/affiliate/products/{p['id']}/image"
    assert client.get(url).status_code == 404
    r = client.put(url, content=_png(), headers={"Content-Type": "image/png"})
    assert r.status_code == 200 and r.json()["image_file"].endswith(".png")
    first = r.json()["image_file"]
    got = client.get(url)
    assert got.status_code == 200 and got.content == _png()
    # replacing removes the old file
    r2 = client.put(url, content=_png(fmt="JPEG"))
    assert r2.json()["image_file"].endswith(".jpg")
    assert not (aff.AFFILIATE_DIR / "images" / first).exists()
    # editing the product keeps the image
    kept = client.put(f"/api/affiliate/products/{p['id']}", json={**PRODUCT, "price": 5}).json()
    assert kept["image_file"] == r2.json()["image_file"]
    assert client.delete(url).json()["image_file"] == ""
    assert client.get(url).status_code == 404
    assert list((aff.AFFILIATE_DIR / "images").iterdir()) == []


@pytest.mark.parametrize("body", [b"", b"not an image", b"<svg xmlns='http://www.w3.org/2000/svg'/>", b"GIF89a" + b"\x00" * 4])
def test_image_upload_rejects_bad_files(client, body):
    p = _product(client)
    r = client.put(f"/api/affiliate/products/{p['id']}/image", content=body)
    assert r.status_code == 400
    assert not (aff.AFFILIATE_DIR / "images").exists() or not list((aff.AFFILIATE_DIR / "images").iterdir())


def test_image_upload_size_limit_and_unknown_product(client, monkeypatch):
    p = _product(client)
    monkeypatch.setattr(aff, "MAX_IMAGE_BYTES", 100)
    r = client.put(f"/api/affiliate/products/{p['id']}/image", content=_png((64, 64)))
    assert r.status_code == 400 and "quá lớn" in r.json()["detail"]
    monkeypatch.setattr(aff, "MAX_IMAGE_BYTES", 8 * 1024 * 1024)
    assert client.put("/api/affiliate/products/nope/image", content=_png()).status_code == 404
    assert client.get("/api/affiliate/products/nope/image").status_code == 404


def test_image_path_traversal_is_contained(env):
    assert aff._image_path("../store.json") is None
    assert aff._image_path("a/b.png") is None
    assert aff._image_path("") is None


def test_image_upload_cross_origin_blocked(client):
    p = _product(client)
    r = client.put(f"/api/affiliate/products/{p['id']}/image", content=_png(), headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


def test_deleting_product_removes_its_image(client):
    p = _product(client)
    client.put(f"/api/affiliate/products/{p['id']}/image", content=_png())
    assert len(list((aff.AFFILIATE_DIR / "images").iterdir())) == 1
    client.delete(f"/api/affiliate/products/{p['id']}")
    assert list((aff.AFFILIATE_DIR / "images").iterdir()) == []


def test_batch_copies_image_into_project(client, env):
    p = _product(client)
    client.put(f"/api/affiliate/products/{p['id']}/image", content=_png())
    body = client.post("/api/affiliate/batch", json={"product_id": p["id"], "templates": ["review"]}).json()
    pid = body["projects"][0]["project_id"]
    assert (env / pid / "assets" / "images" / "product.png").read_bytes() == _png()
    brief = json.loads((env / pid / "artifacts" / "affiliate_brief.json").read_text(encoding="utf-8"))
    assert brief["product"]["image_path"] == "assets/images/product.png"
    assert brief["product"]["description"].startswith("Nhỏ gọn")
    assert "assets/images/product.png" in brief["agent_instructions"]
    # no image -> no image path, nothing copied
    q = _product(client, name="Không ảnh")
    pid2 = client.post("/api/affiliate/batch", json={"product_id": q["id"], "templates": ["review"]}).json()["projects"][0]["project_id"]
    brief2 = json.loads((env / pid2 / "artifacts" / "affiliate_brief.json").read_text(encoding="utf-8"))
    assert brief2["product"]["image_path"] == "" and "product photo" not in brief2["agent_instructions"]


# ---- security / pages ------------------------------------------------------

def test_cross_origin_writes_blocked(client):
    r = client.post("/api/affiliate/products", json=PRODUCT, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    assert client.get("/api/affiliate/products").json() == []
    ok = client.post("/api/affiliate/products", json=PRODUCT, headers={"Origin": "http://localhost:4750"})
    assert ok.status_code == 200


def test_pages_and_assets_served(client):
    page = client.get("/affiliate")
    assert page.status_code == 200 and "/ui/affiliate.js?v=" in page.text
    assert client.get("/ui/affiliate.css").status_code == 200
    assert "/affiliate" in client.get("/").text
    assert client.get("/api/affiliate/meta").json()["max_batch"] == aff.MAX_BATCH
