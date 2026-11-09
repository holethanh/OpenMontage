"""Affiliate-marketing toolkit for Backlot.

Pure logic (no FastAPI): product hub, video templates, hook/CTA generator,
per-platform copy, compliance checks, batch project creation and result
tracking. Everything is deterministic and offline — no LLM or network calls.

Data lives in ``<AFFILIATE_DIR>/store.json`` (default ``.backlot/affiliate``,
override with ``OPENMONTAGE_AFFILIATE_DIR``). Video projects are created
through ``lib.checkpoint.init_project`` so they appear in the normal library.
"""

from __future__ import annotations

import json
import os
import re
import threading
import unicodedata
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator

from backlot import state as state_mod
from lib.paths import REPO_ROOT

AFFILIATE_DIR = Path(
    os.environ.get("OPENMONTAGE_AFFILIATE_DIR") or (REPO_ROOT / ".backlot" / "affiliate")
)
_LOCK = threading.RLock()

LANGUAGES = ("vi", "en")
NETWORKS = ("amazon", "shopee", "tiktok_shop", "lazada", "clickbank", "other")


class AffiliateError(ValueError):
    """User-facing validation problem (maps to HTTP 400)."""


class NotFoundError(AffiliateError):
    """Unknown product / result / project id (maps to HTTP 404)."""


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

def _clean_list(values: list[str]) -> list[str]:
    return [v.strip() for v in values if isinstance(v, str) and v.strip()][:12]


class ProductIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    affiliate_url: str = Field(default="", max_length=2000)
    price: Optional[float] = Field(default=None, ge=0)
    currency: str = Field(default="VND", max_length=8)
    image_url: str = Field(default="", max_length=2000)
    selling_points: list[str] = Field(default_factory=list)
    network: str = "other"
    commission_pct: Optional[float] = Field(default=None, ge=0, le=100)
    category: str = Field(default="", max_length=60)
    notes: str = Field(default="", max_length=2000)

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("name must not be blank")
        return v

    @field_validator("affiliate_url", "image_url")
    @classmethod
    def _http_only(cls, v: str) -> str:
        v = v.strip()
        if v and not re.match(r"^https?://", v, re.IGNORECASE):
            raise ValueError("URL must start with http:// or https://")
        return v

    @field_validator("network")
    @classmethod
    def _network(cls, v: str) -> str:
        v = v.strip().lower()
        return v if v in NETWORKS else "other"

    @field_validator("selling_points")
    @classmethod
    def _points(cls, v: list[str]) -> list[str]:
        return _clean_list(v)


class ResultIn(BaseModel):
    project_id: str = Field(min_length=1, max_length=120)
    platform: str = Field(default="tiktok", max_length=30)
    day: Optional[str] = None  # ISO date, defaults to today
    views: int = Field(default=0, ge=0)
    clicks: int = Field(default=0, ge=0)
    orders: int = Field(default=0, ge=0)
    commission: Optional[float] = Field(default=None, ge=0)
    cost: Optional[float] = Field(default=None, ge=0)
    note: str = Field(default="", max_length=500)

    @field_validator("day")
    @classmethod
    def _day(cls, v: Optional[str]) -> Optional[str]:
        if v in (None, ""):
            return None
        try:
            date.fromisoformat(v)
        except ValueError as exc:
            raise ValueError("day must be YYYY-MM-DD") from exc
        return v


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

def _store_path() -> Path:
    return AFFILIATE_DIR / "store.json"


def _load_store() -> dict[str, Any]:
    try:
        data = json.loads(_store_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("products", {})
    data.setdefault("results", {})
    return data


def _save_store(data: dict[str, Any]) -> None:
    AFFILIATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _store_path().with_suffix(f".{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(_store_path())


def slugify(text: str, fallback: str = "item") -> str:
    """ASCII slug; strips Vietnamese diacritics (đ -> d)."""
    text = text.replace("đ", "d").replace("Đ", "D")
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:60].strip("-") or fallback


# ---------------------------------------------------------------------------
# 1. Product hub
# ---------------------------------------------------------------------------

def list_products() -> list[dict]:
    with _LOCK:
        products = list(_load_store()["products"].values())
    return sorted(products, key=lambda p: p.get("updated_at", ""), reverse=True)


def get_product(product_id: str) -> dict:
    with _LOCK:
        product = _load_store()["products"].get(product_id)
    if product is None:
        raise NotFoundError(f"unknown product: {product_id}")
    return product


def save_product(payload: ProductIn, product_id: Optional[str] = None) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    with _LOCK:
        store = _load_store()
        if product_id is not None:
            existing = store["products"].get(product_id)
            if existing is None:
                raise NotFoundError(f"unknown product: {product_id}")
            created = existing.get("created_at", now)
        else:
            base = slugify(payload.name, "product")
            product_id, n = base, 2
            while product_id in store["products"]:
                product_id, n = f"{base}-{n}", n + 1
            created = now
        record = {
            **payload.model_dump(),
            "id": product_id,
            "created_at": created,
            "updated_at": now,
        }
        store["products"][product_id] = record
        _save_store(store)
    return record


def delete_product(product_id: str) -> None:
    with _LOCK:
        store = _load_store()
        if store["products"].pop(product_id, None) is None:
            raise NotFoundError(f"unknown product: {product_id}")
        _save_store(store)


# ---------------------------------------------------------------------------
# 2. Video templates
# ---------------------------------------------------------------------------

TEMPLATES: dict[str, dict[str, Any]] = {
    "review": {
        "label": {"vi": "Review sản phẩm", "en": "Product review"},
        "pipeline": "animated-explainer",
        "style": "clean-professional",
        "duration_seconds": 45,
        "aspect_ratios": ["9:16", "16:9"],
        "hook_angles": ["honest", "curiosity", "social_proof", "benefit"],
        "structure": [
            "Hook (0-3s): vấn đề hoặc câu hỏi", "Giới thiệu sản phẩm",
            "3 ưu điểm kèm hình minh họa", "1 nhược điểm trung thực",
            "Giá + ai nên mua", "CTA + disclosure",
        ],
        "needs_footage": False,
    },
    "top_n": {
        "label": {"vi": "Top 5 / Danh sách", "en": "Top 5 / listicle"},
        "pipeline": "animated-explainer",
        "style": "flat-motion-graphics",
        "duration_seconds": 50,
        "aspect_ratios": ["9:16"],
        "hook_angles": ["listicle", "curiosity", "price", "contrarian"],
        "structure": [
            "Hook: số lượng + lợi ích", "Mục 5 -> 2 (mỗi mục ~6s)",
            "Mục 1 = sản phẩm affiliate", "CTA + disclosure",
        ],
        "needs_footage": False,
    },
    "unboxing_demo": {
        "label": {"vi": "Unboxing / Demo", "en": "Unboxing / demo"},
        "pipeline": "hybrid",
        "style": "clean-professional",
        "duration_seconds": 40,
        "aspect_ratios": ["9:16", "1:1"],
        "hook_angles": ["curiosity", "honest", "benefit", "problem"],
        "structure": [
            "Hook: khoảnh khắc mở hộp / kết quả", "Cảnh thật cận cảnh",
            "Demo tính năng chính", "Giá + nơi mua", "CTA + disclosure",
        ],
        "needs_footage": True,
    },
    "comparison": {
        "label": {"vi": "So sánh sản phẩm", "en": "Comparison"},
        "pipeline": "animated-explainer",
        "style": "minimalist-diagram",
        "duration_seconds": 55,
        "aspect_ratios": ["16:9", "9:16"],
        "hook_angles": ["contrarian", "price", "problem", "curiosity"],
        "structure": [
            "Hook: A hay B?", "Bảng tiêu chí (giá, tính năng, độ bền)",
            "Điểm khác biệt chính", "Kết luận theo nhu cầu", "CTA + disclosure",
        ],
        "needs_footage": False,
    },
    "ugc_vertical": {
        "label": {"vi": "UGC dọc 9:16", "en": "Vertical UGC"},
        "pipeline": "avatar-spokesperson",
        "style": "flat-motion-graphics",
        "duration_seconds": 30,
        "aspect_ratios": ["9:16"],
        "hook_angles": ["problem", "social_proof", "price", "curiosity"],
        "structure": [
            "Hook nói thẳng vào camera (0-3s)", "Vấn đề -> giải pháp",
            "1-2 điểm bán hàng", "CTA + disclosure",
        ],
        "needs_footage": False,
    },
}


def list_templates() -> list[dict]:
    return [{"id": tid, **tpl} for tid, tpl in TEMPLATES.items()]


def _template(template_id: str) -> dict:
    tpl = TEMPLATES.get(template_id)
    if tpl is None:
        raise AffiliateError(f"unknown template: {template_id}")
    return tpl


# ---------------------------------------------------------------------------
# 3. Hook + CTA generator
# ---------------------------------------------------------------------------

# angle -> language -> formulas. Placeholders: {name} {point} {price}
HOOKS: dict[str, dict[str, list[str]]] = {
    "curiosity": {
        "vi": ["Mình đã thử {name} và đây là điều bất ngờ", "{name}: chuyện gì xảy ra sau 7 ngày dùng?"],
        "en": ["I tried {name} and this surprised me", "{name}: what happened after 7 days?"],
    },
    "social_proof": {
        "vi": ["Vì sao ai cũng nhắc đến {name}?", "{name} đang được tìm kiếm rất nhiều, có đáng không?"],
        "en": ["Why is everyone talking about {name}?", "{name} is everywhere, but is it worth it?"],
    },
    "benefit": {
        "vi": ["{point} — chỉ với {name}", "Muốn {point}? Xem {name} này"],
        "en": ["{point}, with just {name}", "Want {point}? Look at {name}"],
    },
    "price": {
        "vi": ["{name} giá chỉ {price}, có đáng mua không?", "Với {price}, {name} làm được gì?"],
        "en": ["{name} for only {price}: worth it?", "What does {price} get you with {name}?"],
    },
    "contrarian": {
        "vi": ["Đừng mua {name} trước khi xem video này", "Sự thật về {name} ít người nói"],
        "en": ["Don't buy {name} before watching this", "The truth about {name} nobody mentions"],
    },
    "listicle": {
        "vi": ["3 lý do mình chọn {name}", "Top lý do {name} đáng để thử"],
        "en": ["3 reasons I picked {name}", "Top reasons {name} is worth trying"],
    },
    "problem": {
        "vi": ["Mệt vì thiếu {point}? Thử {name}", "Vấn đề quen thuộc này, {name} xử lý thế nào?"],
        "en": ["Tired of lacking {point}? Try {name}", "A familiar problem. How does {name} handle it?"],
    },
    "honest": {
        "vi": ["Review thật lòng về {name}: ưu và nhược điểm", "{name} không hoàn hảo, nhưng đây là lý do mình vẫn dùng"],
        "en": ["Honest review of {name}: pros and cons", "{name} isn't perfect, but here's why I still use it"],
    },
}

CTAS: dict[str, list[str]] = {
    "vi": [
        "Link sản phẩm ở phần mô tả — xem giá hiện tại nhé.",
        "Muốn xem chi tiết, bấm link trong bio.",
        "Lưu video này để mua khi cần, link ở bên dưới.",
        "Comment 'LINK' nếu bạn muốn mình gửi link sản phẩm.",
        "Xem giá mới nhất qua link bên dưới, mình có thể nhận hoa hồng.",
    ],
    "en": [
        "Product link is in the description. Check the current price.",
        "Tap the link in my bio for details.",
        "Save this video and grab it when you need it. Link below.",
        "Comment 'LINK' and I'll send you the product link.",
        "See the latest price via the link below. I may earn a commission.",
    ],
}

_ANGLE_NEEDS_PRICE = {"price"}
_FALLBACK_POINT = {"vi": "kết quả tốt hơn", "en": "better results"}


def format_price(price: Optional[float], currency: str) -> str:
    if price is None:
        return ""
    return f"{price:,.0f} {currency}" if price == int(price) else f"{price:,.2f} {currency}"


def generate_hooks(product: dict, template_id: str, language: str = "vi", count: int = 6) -> dict:
    """Return ``count`` distinct hooks plus CTA variants for a product."""
    if language not in LANGUAGES:
        raise AffiliateError(f"language must be one of {LANGUAGES}")
    tpl = _template(template_id)
    count = max(1, min(int(count), 24))
    points = product.get("selling_points") or []
    price = format_price(product.get("price"), product.get("currency", ""))

    preferred = list(tpl["hook_angles"])
    angles = preferred + [a for a in HOOKS if a not in preferred]
    if not price:
        angles = [a for a in angles if a not in _ANGLE_NEEDS_PRICE]

    hooks: list[dict] = []
    seen: set[str] = set()
    for round_idx in range(2):  # each angle has 2 formulas
        for angle in angles:
            if len(hooks) >= count:
                break
            formulas = HOOKS[angle][language]
            point = points[len(hooks) % len(points)] if points else _FALLBACK_POINT[language]
            text = formulas[round_idx % len(formulas)].format(
                name=product["name"], point=point.rstrip(".").lower(), price=price)
            if text not in seen:
                seen.add(text)
                hooks.append({"angle": angle, "text": text})
    return {"hooks": hooks, "ctas": CTAS[language]}


# ---------------------------------------------------------------------------
# 4. Per-platform copy (caption / hashtags / description / disclosure)
# ---------------------------------------------------------------------------

_PAID_YT = {
    "vi": "Tick 'Có quảng cáo trả phí' (paid promotion) trong YouTube Studio.",
    "en": "Tick 'Includes paid promotion' in YouTube Studio.",
}
PLATFORMS: dict[str, dict[str, Any]] = {
    "tiktok": {"limit": 2200, "clickable_link": False, "hashtags": 5,
               "tags": ["fyp", "tiktokmademebuyit"], "toggle": {
                   "vi": "Bật 'Nội dung quảng cáo' (Promotional content) khi đăng.",
                   "en": "Switch on 'Promotional content' (disclose commercial content) when posting."}},
    "reels": {"limit": 2200, "clickable_link": False, "hashtags": 5,
              "tags": ["reels", "review"], "toggle": {
                  "vi": "Bật nhãn 'Hợp tác trả phí' (Paid partnership) nếu có.",
                  "en": "Use the 'Paid partnership' label when applicable."}},
    "shorts": {"limit": 5000, "clickable_link": True, "hashtags": 3,
               "tags": ["shorts"], "toggle": _PAID_YT},
    "facebook": {"limit": 5000, "clickable_link": True, "hashtags": 3,
                 "tags": ["review"], "toggle": {
                     "vi": "Dùng công cụ 'Nội dung có thương hiệu' nếu có.",
                     "en": "Use the 'Branded content' tool where it applies."}},
    "youtube": {"limit": 5000, "clickable_link": True, "hashtags": 3,
                "tags": ["review"], "toggle": _PAID_YT},
}

DISCLOSURE = {
    "vi": "#quangcao Link affiliate: mình có thể nhận hoa hồng nếu bạn mua qua link này, giá không đổi.",
    "en": "#ad Affiliate link: I may earn a commission if you buy through it, at no extra cost to you.",
}
AMAZON_STATEMENT = "As an Amazon Associate I earn from qualifying purchases."


def make_hashtags(product: dict, platform: str) -> list[str]:
    spec = PLATFORMS[platform]
    candidates: list[str] = []
    if product.get("category"):
        candidates.append(slugify(product["category"]).replace("-", ""))
    candidates.append(slugify(product["name"]).replace("-", "")[:30])
    candidates += ["affiliate"] + spec["tags"]
    tags: list[str] = []
    for tag in candidates:
        if tag and f"#{tag}" not in tags:
            tags.append(f"#{tag}")
    return tags[: spec["hashtags"]]


def build_copy(product: dict, platform: str, language: str, hook: str, cta: str) -> dict:
    if platform not in PLATFORMS:
        raise AffiliateError(f"platform must be one of {sorted(PLATFORMS)}")
    if language not in LANGUAGES:
        raise AffiliateError(f"language must be one of {LANGUAGES}")
    spec = PLATFORMS[platform]
    url = product.get("affiliate_url", "")
    disclosure = DISCLOSURE[language]
    if product.get("network") == "amazon":
        disclosure = f"{disclosure} {AMAZON_STATEMENT}"
    hashtags = make_hashtags(product, platform)

    lines = [hook.strip(), cta.strip()]
    link_note = ""
    if url and spec["clickable_link"]:
        lines.append(url)
    elif url:
        link_note = ("Link không bấm được trong caption: đặt vào bio / mục giỏ hàng."
                     if language == "vi" else
                     "Links aren't clickable in captions: put it in your bio / shop tab.")
    lines += [disclosure, " ".join(hashtags)]
    caption = "\n\n".join(line for line in lines if line)
    return {
        "platform": platform,
        "caption": caption,
        "hashtags": hashtags,
        "disclosure": disclosure,
        "link_note": link_note,
        "platform_toggle": spec["toggle"][language],
        "char_count": len(caption),
        "char_limit": spec["limit"],
        "within_limit": len(caption) <= spec["limit"],
    }


# ---------------------------------------------------------------------------
# 5. Compliance checklist
# ---------------------------------------------------------------------------

_DISCLOSURE_RE = re.compile(
    r"#ad\b|#quangcao|#sponsored|#affiliate|#paidpartnership|"
    r"affiliate|hoa hồng|hoa hong|quảng cáo|quang cao|sponsored|"
    r"paid partnership|commission",
    re.IGNORECASE,
)

# (code, severity, regex, message)
_RISKY: list[tuple[str, str, str, str]] = [
    ("guarantee", "fail", r"guarantee[sd]?|100\s*%|cam kết (hiệu quả|kết quả)|đảm bảo (hiệu quả|kết quả)",
     "Lời hứa tuyệt đối/đảm bảo kết quả có thể bị coi là quảng cáo sai sự thật."),
    ("health_claim", "fail", r"\bcures?\b|\bheals?\b|chữa (khỏi|bệnh|dứt điểm)|điều trị|trị (dứt|hết)",
     "Tuyên bố chữa bệnh/điều trị: chỉ được nêu khi có giấy phép, thường nên bỏ."),
    ("income_claim", "fail", r"làm giàu nhanh|get rich|passive income guaranteed",
     "Tuyên bố thu nhập không có căn cứ."),
    ("no_side_effects", "fail", r"không (có )?tác dụng phụ|no side effects|risk[- ]free|không rủi ro",
     "Tuyên bố 'không rủi ro/tác dụng phụ' khó chứng minh."),
    ("superlative", "warn", r"số 1\b|no\.?\s?1\b|best in the world|tốt nhất (thế giới|việt nam|thị trường)|duy nhất",
     "Từ so sánh tuyệt đối cần có bằng chứng."),
    ("fast_result", "warn", r"nhanh chóng|overnight|trong \d+ ngày|in \d+ days",
     "Hứa kết quả nhanh: nên nêu rõ kết quả có thể khác nhau."),
    ("fake_urgency", "warn", r"chỉ còn hôm nay|sắp hết hàng|last chance|only \d+ left|hết ngay",
     "Tạo khan hiếm giả có thể vi phạm quy định quảng cáo nếu không đúng sự thật."),
    ("unsourced_reviews", "warn", r"khách hàng (nói|phản hồi)|customers say|\d+\s*k?\s*(người|people) (đã )?(mua|dùng|bought)",
     "Dẫn số liệu/đánh giá cần nguồn thật."),
]


def check_compliance(
    text: str,
    platform: str = "tiktok",
    has_affiliate_link: bool = True,
    network: str = "other",
    language: str = "vi",
) -> dict:
    """Scan marketing text. Heuristic helper, not legal advice."""
    if platform not in PLATFORMS:
        raise AffiliateError(f"platform must be one of {sorted(PLATFORMS)}")
    if language not in LANGUAGES:
        raise AffiliateError(f"language must be one of {LANGUAGES}")
    issues: list[dict] = []
    has_disclosure = bool(_DISCLOSURE_RE.search(text))

    if has_affiliate_link and not has_disclosure:
        issues.append({
            "code": "missing_disclosure", "severity": "fail", "match": "",
            "message": "Thiếu công bố affiliate (vd #ad, #quangcao, 'mình có thể nhận hoa hồng'). "
                       "FTC và các nền tảng yêu cầu công bố rõ ràng, dễ thấy.",
        })
    if network == "amazon" and "amazon associate" not in text.lower():
        issues.append({
            "code": "amazon_statement", "severity": "warn", "match": "",
            "message": f"Amazon Associates yêu cầu câu: \"{AMAZON_STATEMENT}\"",
        })
    for code, severity, pattern, message in _RISKY:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            issues.append({"code": code, "severity": severity, "match": m.group(0), "message": message})

    fails = sum(1 for i in issues if i["severity"] == "fail")
    warns = sum(1 for i in issues if i["severity"] == "warn")
    return {
        "status": "fail" if fails else ("warn" if warns else "pass"),
        "has_disclosure": has_disclosure,
        "issues": issues,
        "checklist": [
            {"item": "Công bố affiliate trong caption/mô tả", "ok": has_disclosure or not has_affiliate_link},
            {"item": "Hiện dòng công bố ngay trên video (3 giây đầu hoặc cuối)", "ok": None},
            {"item": PLATFORMS[platform]["toggle"][language], "ok": None},
            {"item": "Không hứa kết quả tuyệt đối / không tuyên bố y tế", "ok": fails == 0},
            {"item": "Ưu/nhược điểm trung thực, số liệu có nguồn", "ok": None},
        ],
        "disclaimer": "Công cụ gợi ý dựa trên quy tắc đơn giản, không thay thế tư vấn pháp lý.",
    }


# ---------------------------------------------------------------------------
# 6. Batch project creation
# ---------------------------------------------------------------------------

MAX_BATCH = 30


def _unique_project_id(base: str, reserved: set[str]) -> str:
    root = state_mod.PROJECTS_DIR
    candidate, n = base, 2
    while (root / candidate).exists() or candidate in reserved:
        candidate, n = f"{base}-{n}", n + 1
    reserved.add(candidate)
    return candidate


def _agent_instructions(tpl: dict, hook: str, cta: str, aspect: str, language: str) -> str:
    footage = (" The user must supply real footage of the product before the assets stage."
               if tpl["needs_footage"] else "")
    return (
        "Read AGENT_GUIDE.md first, then skills/creative/affiliate-marketing.md. "
        f"Run the '{tpl['pipeline']}' pipeline with style '{tpl['style']}', aspect {aspect}, "
        f"~{tpl['duration_seconds']}s, language '{language}'. Open with this exact hook: \"{hook}\". "
        f"Close with this CTA: \"{cta}\". Show the disclosure text on screen. "
        "Do not add claims that are not in selling_points." + footage
    )


def create_batch(
    product_id: str,
    template_ids: list[str],
    *,
    language: str = "vi",
    platform: str = "tiktok",
    hooks_per_template: int = 1,
    aspect_ratios: Optional[list[str]] = None,
    series: Optional[str] = None,
) -> dict:
    """Create one Backlot project per (template x hook x aspect) variant."""
    from lib.checkpoint import init_project

    product = get_product(product_id)
    if not template_ids:
        raise AffiliateError("choose at least one template")
    if language not in LANGUAGES:
        raise AffiliateError(f"language must be one of {LANGUAGES}")
    if platform not in PLATFORMS:
        raise AffiliateError(f"platform must be one of {sorted(PLATFORMS)}")
    hooks_per_template = max(1, min(int(hooks_per_template), 5))
    series = (series or "").strip() or f"affiliate-{slugify(product['name'], 'product')}"

    plan: list[tuple[str, dict, dict, str]] = []
    for tid in dict.fromkeys(template_ids):
        tpl = _template(tid)
        aspects = [a for a in (aspect_ratios or tpl["aspect_ratios"][:1]) if a]
        for hook in generate_hooks(product, tid, language, hooks_per_template)["hooks"]:
            for aspect in aspects:
                plan.append((tid, tpl, hook, aspect))
    if len(plan) > MAX_BATCH:
        raise AffiliateError(f"batch too large ({len(plan)} variants, max {MAX_BATCH})")

    created: list[dict] = []
    reserved: set[str] = set()
    for index, (tid, tpl, hook, aspect) in enumerate(plan):
        cta = CTAS[language][index % len(CTAS[language])]
        copy = build_copy(product, platform, language, hook["text"], cta)
        compliance = check_compliance(
            copy["caption"], platform, bool(product.get("affiliate_url")),
            product.get("network", "other"), language,
        )
        base_id = f"{slugify(product['name'], 'product')}-{tid.replace('_', '-')}-{index + 1}"
        project_id = _unique_project_id(base_id, reserved)
        title = f"{product['name']} — {tpl['label'][language]} #{index + 1}"
        project_dir = init_project(
            project_id, title=title, pipeline_type=tpl["pipeline"],
            pipeline_dir=state_mod.PROJECTS_DIR, style_playbook=tpl["style"], series=series,
        )
        brief = {
            "version": "1.0",
            "kind": "affiliate_brief",
            "product": {k: product.get(k) for k in (
                "id", "name", "affiliate_url", "price", "currency", "image_url",
                "selling_points", "network", "commission_pct", "category")},
            "template": tid,
            "pipeline": tpl["pipeline"],
            "style": tpl["style"],
            "aspect_ratio": aspect,
            "duration_seconds": tpl["duration_seconds"],
            "structure": tpl["structure"],
            "needs_footage": tpl["needs_footage"],
            "language": language,
            "platform": platform,
            "hook": hook,
            "cta": cta,
            "copy": copy,
            "compliance": compliance,
            "agent_instructions": _agent_instructions(tpl, hook["text"], cta, aspect, language),
        }
        (project_dir / "artifacts" / "affiliate_brief.json").write_text(
            json.dumps(brief, indent=2, ensure_ascii=False), encoding="utf-8")
        marker_path = project_dir / "project.json"
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        marker["affiliate"] = {
            "product_id": product["id"], "template": tid, "hook_angle": hook["angle"],
            "platform": platform, "aspect_ratio": aspect,
        }
        marker_path.write_text(json.dumps(marker, indent=2, ensure_ascii=False), encoding="utf-8")
        created.append({
            "project_id": project_id, "title": title, "template": tid,
            "pipeline": tpl["pipeline"], "hook": hook, "aspect_ratio": aspect,
            "compliance": compliance["status"],
        })
    return {"series": series, "count": len(created), "projects": created}


# ---------------------------------------------------------------------------
# 7. Result tracking
# ---------------------------------------------------------------------------

def add_result(payload: ResultIn) -> dict:
    pid = payload.project_id
    if any(c in pid for c in "/\\:") or pid in (".", "..") or not (state_mod.PROJECTS_DIR / pid).is_dir():
        raise NotFoundError(f"unknown project: {pid}")
    record = {
        **payload.model_dump(),
        "id": uuid.uuid4().hex[:10],
        "day": payload.day or date.today().isoformat(),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    with _LOCK:
        store = _load_store()
        store["results"][record["id"]] = record
        _save_store(store)
    return record


def delete_result(result_id: str) -> None:
    with _LOCK:
        store = _load_store()
        if store["results"].pop(result_id, None) is None:
            raise NotFoundError(f"unknown result: {result_id}")
        _save_store(store)


def _ratio(num: float, den: float) -> Optional[float]:
    return round(num / den, 4) if den else None


def _metrics(views: int, clicks: int, orders: int, commission: float, cost: float) -> dict:
    return {
        "views": views, "clicks": clicks, "orders": orders,
        "commission": round(commission, 2), "cost": round(cost, 2),
        "profit": round(commission - cost, 2),
        "ctr": _ratio(clicks, views),       # clicks / views
        "cvr": _ratio(orders, clicks),      # orders / clicks
        "epc": _ratio(commission, clicks),  # earnings per click
        "roi": _ratio(commission - cost, cost),
    }


def _project_cost(project_id: str) -> float:
    try:
        cost = state_mod.load_board_state(state_mod.PROJECTS_DIR / project_id).get("cost") or {}
        return float(cost.get("total_spent_usd") or 0.0)
    except Exception:
        return 0.0


def _read_marker(project_id: str) -> dict:
    try:
        return json.loads((state_mod.PROJECTS_DIR / project_id / "project.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def results_summary() -> dict:
    """Per-project, per-template and per-hook-angle performance.

    Cost per project = sum of manually entered ``cost`` values, else the
    generation spend recorded by the pipeline (USD). Enter commission in the
    same currency as your costs for ROI to be meaningful.
    """
    with _LOCK:
        store = _load_store()
    results, products = list(store["results"].values()), store["products"]

    by_project: dict[str, list[dict]] = {}
    for r in results:
        by_project.setdefault(r["project_id"], []).append(r)

    rows: list[dict] = []
    for project_id, entries in by_project.items():
        marker = _read_marker(project_id)
        aff = marker.get("affiliate") or {}
        product = products.get(aff.get("product_id", ""), {})
        has_manual = any(e.get("cost") is not None for e in entries)
        cost = (sum(e["cost"] for e in entries if e.get("cost") is not None)
                if has_manual else _project_cost(project_id))
        commission = 0.0
        for e in entries:
            if e.get("commission") is not None:
                commission += e["commission"]
            elif product.get("price") and product.get("commission_pct"):
                commission += e["orders"] * product["price"] * product["commission_pct"] / 100
        rows.append({
            "project_id": project_id,
            "title": marker.get("title") or project_id,
            "product_id": aff.get("product_id"),
            "template": aff.get("template"),
            "hook_angle": aff.get("hook_angle"),
            "entries": len(entries),
            **_metrics(sum(e["views"] for e in entries), sum(e["clicks"] for e in entries),
                       sum(e["orders"] for e in entries), commission, cost),
        })

    def aggregate(key: str) -> list[dict]:
        groups: dict[str, list[dict]] = {}
        for row in rows:
            if row.get(key):
                groups.setdefault(row[key], []).append(row)
        out = [
            {key: name, "projects": len(m), **_metrics(
                sum(x["views"] for x in m), sum(x["clicks"] for x in m),
                sum(x["orders"] for x in m), sum(x["commission"] for x in m),
                sum(x["cost"] for x in m))}
            for name, m in groups.items()
        ]
        return sorted(out, key=lambda g: g["profit"], reverse=True)

    totals = _metrics(
        sum(r["views"] for r in rows), sum(r["clicks"] for r in rows),
        sum(r["orders"] for r in rows), sum(r["commission"] for r in rows),
        sum(r["cost"] for r in rows))
    return {
        "totals": totals,
        "projects": sorted(rows, key=lambda r: r["profit"], reverse=True),
        "by_template": aggregate("template"),
        "by_hook_angle": aggregate("hook_angle"),
        "entries": sorted(results, key=lambda r: (r["day"], r["created_at"]), reverse=True),
    }
