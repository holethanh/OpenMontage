import { el, getJSON } from "/ui/lib.js";

const panel = document.getElementById("panel");
const tabsEl = document.getElementById("tabs");
const toastEl = document.getElementById("toast");
const THEME_KEY = "backlot.theme";
document.documentElement.dataset.theme = localStorage.getItem(THEME_KEY) === "light" ? "light" : "dark";

const S = { meta: null, products: [], selected: null, tab: "products", editing: null };
const TABS = [
  ["products", "Sản phẩm"], ["create", "Tạo video"],
  ["compliance", "Tuân thủ"], ["results", "Kết quả"],
];

async function send(method, url, body) {
  const res = await fetch(url, {
    method,
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let data = null;
  try { data = await res.json(); } catch { /* empty body */ }
  if (!res.ok) {
    const d = data && data.detail;
    const msg = Array.isArray(d) ? d.map((e) => `${(e.loc || []).slice(1).join(".")}: ${e.msg}`).join("; ") : (d || res.statusText);
    throw new Error(msg);
  }
  return data;
}

function toast(msg, isError = false) {
  toastEl.textContent = msg;
  toastEl.className = `aff-toast show${isError ? " error" : ""}`;
  clearTimeout(toast.t);
  toast.t = setTimeout(() => { toastEl.className = "aff-toast"; }, isError ? 5000 : 2500);
}
const guard = (fn) => async (...a) => { try { await fn(...a); } catch (e) { toast(e.message, true); } };

function field(label, input) { return el("label", {}, label, input); }
const pct = (v) => (v == null ? "—" : `${(v * 100).toFixed(1)}%`);
const num = (v) => (v == null ? "—" : Number(v).toLocaleString("vi-VN", { maximumFractionDigits: 2 }));
const money = (v) => num(v);

async function copyText(text) {
  try { await navigator.clipboard.writeText(text); toast("Đã sao chép"); }
  catch { toast("Không sao chép được — hãy bôi đen và copy thủ công", true); }
}

function selectedProduct() { return S.products.find((p) => p.id === S.selected) || null; }

// ---------------------------------------------------------------- Products
function productForm() {
  const p = S.editing ? S.products.find((x) => x.id === S.editing) : null;
  const v = (k, d = "") => (p && p[k] != null ? p[k] : d);
  const f = {
    name: el("input", { required: "", maxlength: 120, value: v("name") }),
    url: el("input", { type: "url", placeholder: "https://…", value: v("affiliate_url") }),
    price: el("input", { type: "number", min: 0, step: "any", value: v("price") }),
    currency: el("input", { maxlength: 8, value: v("currency", "VND") }),
    image: el("input", { type: "url", placeholder: "https://…", value: v("image_url") }),
    network: el("select", {}, ...S.meta.networks.map((n) => el("option", { value: n }, n))),
    commission: el("input", { type: "number", min: 0, max: 100, step: "any", value: v("commission_pct") }),
    category: el("input", { maxlength: 60, value: v("category") }),
    points: el("textarea", { placeholder: "Mỗi dòng một điểm bán hàng" }),
  };
  f.network.value = v("network", "other");
  f.points.value = (p ? p.selling_points : []).join("\n");
  const body = () => ({
    name: f.name.value, affiliate_url: f.url.value,
    price: f.price.value === "" ? null : Number(f.price.value),
    currency: f.currency.value || "VND", image_url: f.image.value, network: f.network.value,
    commission_pct: f.commission.value === "" ? null : Number(f.commission.value),
    category: f.category.value,
    selling_points: f.points.value.split("\n").map((s) => s.trim()).filter(Boolean),
  });
  const form = el("form", { class: "aff-form", onsubmit: guard(async (e) => {
    e.preventDefault();
    const saved = p ? await send("PUT", `/api/affiliate/products/${p.id}`, body())
      : await send("POST", "/api/affiliate/products", body());
    S.selected = saved.id; S.editing = null;
    toast("Đã lưu sản phẩm");
    await load(); render();
  }) },
    field("Tên sản phẩm *", f.name), field("Link affiliate", f.url),
    el("div", { class: "aff-row" }, field("Giá", f.price), field("Tiền tệ", f.currency)),
    el("div", { class: "aff-row" }, field("Nền tảng affiliate", f.network), field("Hoa hồng (%)", f.commission)),
    field("Danh mục", f.category), field("Ảnh sản phẩm (URL)", f.image),
    field("Điểm bán hàng", f.points),
    el("div", { class: "aff-row" },
      el("button", { class: "aff-btn", type: "submit" }, p ? "Cập nhật" : "Thêm sản phẩm"),
      p ? el("button", { class: "aff-btn ghost", type: "button", onclick: () => { S.editing = null; render(); } }, "Hủy") : null),
  );
  return el("section", { class: "aff-card" }, el("h2", {}, p ? "Sửa sản phẩm" : "Thêm sản phẩm"), form);
}

function productList() {
  const list = el("div", { class: "aff-list" });
  if (!S.products.length) list.append(el("p", { class: "hint" }, "Chưa có sản phẩm — hãy thêm sản phẩm đầu tiên."));
  for (const p of S.products) {
    const price = p.price != null ? `${num(p.price)} ${p.currency}` : null;
    list.append(el("div", { class: `aff-item${p.id === S.selected ? " selected" : ""}` },
      el("h3", {}, p.name),
      el("div", { class: "aff-meta" },
        el("span", { class: "chip" }, p.network), price ? el("span", { class: "chip" }, price) : null,
        p.commission_pct != null ? el("span", { class: "chip" }, `${p.commission_pct}% hoa hồng`) : null,
        p.category ? el("span", { class: "chip" }, p.category) : null),
      p.selling_points.length ? el("p", { class: "aff-muted" }, p.selling_points.join(" · ")) : null,
      el("div", { class: "aff-row" },
        el("button", { class: "aff-btn", onclick: () => { S.selected = p.id; S.tab = "create"; render(); } }, "Tạo video"),
        el("button", { class: "aff-btn ghost", onclick: () => { S.editing = p.id; render(); } }, "Sửa"),
        el("button", { class: "aff-btn danger", onclick: guard(async () => {
          if (!confirm(`Xóa "${p.name}"?`)) return;
          await send("DELETE", `/api/affiliate/products/${p.id}`);
          if (S.selected === p.id) S.selected = null;
          await load(); render();
        }) }, "Xóa")),
    ));
  }
  return el("section", { class: "aff-card" }, el("h2", {}, `Sản phẩm (${S.products.length})`), list);
}

function productsTab() {
  return el("div", { class: "aff-grid" }, productForm(), productList());
}

// ------------------------------------------------------------ Create video
const C = { templates: new Set(["review"]), language: "vi", platform: "tiktok", perTemplate: 1, hooks: null, picked: 0, copy: null, series: "" };

function select(options, value, onchange) {
  const s = el("select", { onchange: (e) => onchange(e.target.value) }, ...options.map(([v, t]) => el("option", { value: v }, t)));
  s.value = value;
  return s;
}

function createTab() {
  const p = selectedProduct();
  const picker = select([["", "— chọn sản phẩm —"], ...S.products.map((x) => [x.id, x.name])], S.selected || "",
    (v) => { S.selected = v || null; C.hooks = null; C.copy = null; render(); });
  const head = el("section", { class: "aff-card" }, el("h2", {}, "1. Chọn sản phẩm"), picker);
  if (!p) return el("div", { class: "aff-list" }, head, el("p", { class: "hint" }, "Chọn (hoặc thêm) một sản phẩm để tiếp tục."));

  const tplBoxes = el("div", { class: "aff-checks" }, ...S.meta.templates.map((t) => el("label", {},
    el("input", { type: "checkbox", checked: C.templates.has(t.id) ? "" : null, onchange: (e) => {
      if (e.target.checked) C.templates.add(t.id); else C.templates.delete(t.id);
    } }),
    `${t.label.vi} · ~${t.duration_seconds}s · ${t.aspect_ratios.join("/")}`,
    t.needs_footage ? el("span", { class: "chip warn" }, "cần cảnh quay thật") : null)));
  const opts = el("div", { class: "aff-row" },
    field("Ngôn ngữ", select(S.meta.languages.map((l) => [l, l]), C.language, (v) => { C.language = v; C.hooks = null; })),
    field("Nền tảng đăng", select(S.meta.platforms.map((l) => [l, l]), C.platform, (v) => { C.platform = v; C.copy = null; })),
    field("Hook / mẫu", select([1, 2, 3, 4, 5].map((n) => [String(n), String(n)]), String(C.perTemplate), (v) => { C.perTemplate = Number(v); })));
  const series = el("input", { class: "aff-input", placeholder: `affiliate-${p.id}`, value: C.series, oninput: (e) => { C.series = e.target.value; } });
  const batch = el("button", { class: "aff-btn", onclick: guard(async (e) => {
    if (!C.templates.size) throw new Error("Chọn ít nhất một mẫu video");
    e.target.disabled = true;
    try {
      const r = await send("POST", "/api/affiliate/batch", {
        product_id: p.id, templates: [...C.templates], language: C.language, platform: C.platform,
        hooks_per_template: C.perTemplate, series: C.series || null });
      C.batchResult = r;
      toast(`Đã tạo ${r.count} dự án`);
      render();
    } finally { e.target.disabled = false; }
  }) }, "Tạo dự án hàng loạt");
  const step2 = el("section", { class: "aff-card" }, el("h2", {}, "2. Mẫu video & tùy chọn"), tplBoxes, opts,
    field("Tên series (tùy chọn)", series),
    el("div", { class: "aff-row" }, batch,
      el("span", { class: "aff-muted" }, `Tối đa ${S.meta.max_batch} biến thể mỗi lần`)));

  const out = [head, step2, hooksCard(p)];
  if (C.batchResult) out.push(batchCard());
  return el("div", { class: "aff-list" }, ...out);
}

function batchCard() {
  const r = C.batchResult;
  return el("section", { class: "aff-card" }, el("h2", {}, `Đã tạo ${r.count} dự án — series "${r.series}"`),
    el("p", { class: "aff-muted aff-links" }, el("a", { href: `/?series=${encodeURIComponent(r.series)}` }, "Mở series trong Library"),
      " · Mỗi dự án có artifacts/affiliate_brief.json để agent đọc và chạy pipeline."),
    el("div", { class: "aff-scroll" }, el("table", { class: "aff-table" },
      el("thead", {}, el("tr", {}, ...["Dự án", "Mẫu", "Pipeline", "Tỉ lệ", "Hook", "Tuân thủ"].map((h) => el("th", {}, h)))),
      el("tbody", {}, ...r.projects.map((x) => el("tr", {},
        el("td", {}, el("a", { href: `/p/${x.project_id}` }, x.project_id)),
        el("td", {}, x.template), el("td", {}, x.pipeline), el("td", {}, x.aspect_ratio),
        el("td", { style: "text-align:left" }, x.hook.text),
        el("td", {}, el("span", { class: `aff-status ${x.compliance}` }, x.compliance))))))));
}

function hooksCard(p) {
  const first = [...C.templates][0] || S.meta.templates[0].id;
  const gen = el("button", { class: "aff-btn", onclick: guard(async () => {
    C.hooks = await send("POST", `/api/affiliate/products/${p.id}/hooks`, { template: first, language: C.language, count: 8 });
    C.picked = 0; C.copy = null; render();
  }) }, C.hooks ? "Tạo lại hook" : "Tạo hook & CTA");
  const card = el("section", { class: "aff-card" }, el("h2", {}, "3. Hook, CTA & caption"),
    el("div", { class: "aff-row" }, gen, el("span", { class: "aff-muted" }, `Dùng mẫu "${first}" để chọn góc hook`)));
  if (!C.hooks) return card;
  C.hooks.hooks.forEach((h, i) => card.append(el("label", { class: "aff-hook" },
    el("input", { type: "radio", name: "hook", checked: i === C.picked ? "" : null, onchange: () => { C.picked = i; } }),
    el("span", { class: "chip" }, h.angle), el("span", {}, h.text))));
  const ctaSel = select(C.hooks.ctas.map((c, i) => [String(i), c]), "0", () => {});
  card.append(field("CTA", ctaSel), el("div", { class: "aff-row" },
    el("button", { class: "aff-btn", onclick: guard(async () => {
      C.copy = await send("POST", `/api/affiliate/products/${p.id}/copy`, {
        platform: C.platform, language: C.language,
        hook: C.hooks.hooks[C.picked].text, cta: C.hooks.ctas[Number(ctaSel.value)] });
      render();
    }) }, "Tạo caption cho " + C.platform)));
  if (C.copy) card.append(copyView(C.copy));
  return card;
}

function copyView(c) {
  return el("div", {},
    el("div", { class: "aff-pre" }, c.caption),
    el("p", { class: `aff-muted${c.within_limit ? "" : " aff-neg"}` }, `${c.char_count}/${c.char_limit} ký tự`),
    c.link_note ? el("p", { class: "aff-muted" }, c.link_note) : null,
    el("p", { class: "aff-muted" }, "☑ " + c.platform_toggle),
    el("div", { class: "aff-row" },
      el("button", { class: "aff-btn ghost", onclick: () => copyText(c.caption) }, "Sao chép caption"),
      el("span", { class: `aff-status ${c.compliance.status}` }, `Tuân thủ: ${c.compliance.status}`)));
}

// -------------------------------------------------------------- Compliance
const K = { text: "", platform: "tiktok", language: "vi", network: "other", link: true, result: null };

function complianceTab() {
  const ta = el("textarea", { class: "aff-input", style: "min-height:160px", placeholder: "Dán caption / kịch bản / mô tả video vào đây…", oninput: (e) => { K.text = e.target.value; } });
  ta.value = K.text;
  const link = el("input", { type: "checkbox", checked: K.link ? "" : null, onchange: (e) => { K.link = e.target.checked; } });
  const run = el("button", { class: "aff-btn", onclick: guard(async () => {
    K.result = await send("POST", "/api/affiliate/compliance", {
      text: K.text, platform: K.platform, language: K.language, network: K.network, has_affiliate_link: K.link });
    render();
  }) }, "Kiểm tra");
  const form = el("section", { class: "aff-card" }, el("h2", {}, "Kiểm tra tuân thủ"), ta,
    el("div", { class: "aff-row" },
      field("Nền tảng", select(S.meta.platforms.map((l) => [l, l]), K.platform, (v) => { K.platform = v; })),
      field("Affiliate", select(S.meta.networks.map((l) => [l, l]), K.network, (v) => { K.network = v; })),
      field("Ngôn ngữ", select(S.meta.languages.map((l) => [l, l]), K.language, (v) => { K.language = v; }))),
    el("label", { class: "aff-checks" }, el("span", {}, link, " Nội dung có link affiliate")), run);
  const r = K.result;
  const res = el("section", { class: "aff-card" }, el("h2", {}, "Kết quả"));
  if (!r) res.append(el("p", { class: "hint" }, "Chưa kiểm tra."));
  else {
    res.append(el("p", {}, el("span", { class: `aff-status ${r.status}` }, r.status.toUpperCase())));
    if (!r.issues.length) res.append(el("p", { class: "aff-muted" }, "Không phát hiện vấn đề nào theo các quy tắc hiện có."));
    for (const i of r.issues) res.append(el("div", { class: `aff-issue ${i.severity}` },
      el("strong", {}, i.match ? `"${i.match}" — ` : ""), i.message));
    res.append(el("h3", {}, "Checklist trước khi đăng"));
    for (const c of r.checklist) res.append(el("p", {}, (c.ok === true ? "✅ " : c.ok === false ? "❌ " : "☐ ") + c.item));
    res.append(el("p", { class: "aff-muted" }, r.disclaimer));
  }
  return el("div", { class: "aff-grid" }, form, res);
}

// ------------------------------------------------------------------ Results
const R = { data: null, projects: [] };

function metricRows(rows, key, label) {
  if (!rows.length) return el("p", { class: "hint" }, "Chưa có dữ liệu.");
  const cls = (v) => (v > 0 ? "aff-pos" : v < 0 ? "aff-neg" : "");
  return el("div", { class: "aff-scroll" }, el("table", { class: "aff-table" },
    el("thead", {}, el("tr", {}, ...[label, "View", "Click", "Đơn", "CTR", "CVR", "Hoa hồng", "Chi phí", "Lãi", "ROI"].map((h) => el("th", {}, h)))),
    el("tbody", {}, ...rows.map((r) => el("tr", {},
      el("td", {}, key === "project_id" ? el("a", { href: `/p/${r.project_id}` }, r.title || r.project_id) : r[key]),
      el("td", {}, num(r.views)), el("td", {}, num(r.clicks)), el("td", {}, num(r.orders)),
      el("td", {}, pct(r.ctr)), el("td", {}, pct(r.cvr)), el("td", {}, money(r.commission)),
      el("td", {}, money(r.cost)), el("td", { class: cls(r.profit) }, money(r.profit)),
      el("td", { class: cls(r.roi) }, pct(r.roi)))))));
}

function resultForm() {
  const f = {
    project: select([["", "— chọn dự án —"], ...R.projects.map((p) => [p.project_id, p.title || p.project_id])], "", () => {}),
    platform: select(S.meta.platforms.map((l) => [l, l]), "tiktok", () => {}),
    day: el("input", { type: "date", value: new Date().toISOString().slice(0, 10) }),
    views: el("input", { type: "number", min: 0, value: 0 }), clicks: el("input", { type: "number", min: 0, value: 0 }),
    orders: el("input", { type: "number", min: 0, value: 0 }),
    commission: el("input", { type: "number", min: 0, step: "any", placeholder: "tự tính nếu để trống" }),
    cost: el("input", { type: "number", min: 0, step: "any", placeholder: "mặc định: chi phí sinh asset" }),
  };
  const opt = (v) => (v.value === "" ? null : Number(v.value));
  return el("form", { class: "aff-form", onsubmit: guard(async (e) => {
    e.preventDefault();
    if (!f.project.value) throw new Error("Chọn dự án");
    await send("POST", "/api/affiliate/results", {
      project_id: f.project.value, platform: f.platform.value, day: f.day.value || null,
      views: Number(f.views.value) || 0, clicks: Number(f.clicks.value) || 0, orders: Number(f.orders.value) || 0,
      commission: opt(f.commission), cost: opt(f.cost) });
    toast("Đã lưu số liệu");
    await loadResults(); render();
  }) },
    field("Dự án / video", f.project),
    el("div", { class: "aff-row" }, field("Nền tảng", f.platform), field("Ngày", f.day)),
    el("div", { class: "aff-row" }, field("View", f.views), field("Click", f.clicks), field("Đơn", f.orders)),
    el("div", { class: "aff-row" }, field("Hoa hồng nhận được", f.commission), field("Chi phí", f.cost)),
    el("button", { class: "aff-btn", type: "submit" }, "Lưu số liệu"));
}

function resultsTab() {
  const d = R.data;
  const t = d.totals;
  const form = el("section", { class: "aff-card" }, el("h2", {}, "Nhập kết quả"), resultForm(),
    el("p", { class: "aff-muted" }, "Nhập hoa hồng và chi phí cùng một đơn vị tiền tệ để ROI có ý nghĩa."));
  const right = el("div", { class: "aff-list" },
    el("section", { class: "aff-card" }, el("h2", {}, "Tổng quan"),
      el("div", { class: "aff-meta" },
        ...[["View", num(t.views)], ["Click", num(t.clicks)], ["Đơn", num(t.orders)], ["Hoa hồng", money(t.commission)],
          ["Chi phí", money(t.cost)], ["Lãi", money(t.profit)], ["ROI", pct(t.roi)]].map(([k, v]) => el("span", { class: "chip" }, `${k}: ${v}`)))),
    el("section", { class: "aff-card" }, el("h2", {}, "Theo video"), metricRows(d.projects, "project_id", "Video")),
    el("section", { class: "aff-card" }, el("h2", {}, "Theo mẫu video"), metricRows(d.by_template, "template", "Mẫu")),
    el("section", { class: "aff-card" }, el("h2", {}, "Theo góc hook"), metricRows(d.by_hook_angle, "hook_angle", "Hook")),
    el("section", { class: "aff-card" }, el("h2", {}, "Lịch sử nhập"),
      d.entries.length ? el("div", { class: "aff-list" }, ...d.entries.slice(0, 20).map((e) => el("div", { class: "aff-row" },
        el("span", {}, `${e.day} · ${e.project_id} · ${e.platform}`),
        el("span", { class: "aff-muted" }, `${num(e.views)} view / ${num(e.clicks)} click / ${num(e.orders)} đơn`),
        el("button", { class: "aff-btn danger", onclick: guard(async () => {
          await send("DELETE", `/api/affiliate/results/${e.id}`); await loadResults(); render();
        }) }, "Xóa")))) : el("p", { class: "hint" }, "Chưa có dữ liệu.")));
  return el("div", { class: "aff-grid" }, form, right);
}

// -------------------------------------------------------------------- Boot
async function load() { S.products = await getJSON("/api/affiliate/products"); }
async function loadResults() {
  R.data = await getJSON("/api/affiliate/results");
  R.projects = (await getJSON("/api/projects")).filter((p) => p.project_id);
}

function render() {
  tabsEl.innerHTML = "";
  for (const [id, label] of TABS) {
    tabsEl.append(el("button", { class: "aff-tab", role: "tab", "aria-selected": String(S.tab === id),
      onclick: guard(async () => { S.tab = id; if (id === "results") await loadResults(); render(); }) }, label));
  }
  panel.innerHTML = "";
  const view = { products: productsTab, create: createTab, compliance: complianceTab, results: resultsTab }[S.tab];
  panel.append(view());
}

(async () => {
  try {
    S.meta = await getJSON("/api/affiliate/meta");
    await load();
    if (S.tab === "results") await loadResults();
    render();
  } catch (e) { toast(e.message, true); }
})();
