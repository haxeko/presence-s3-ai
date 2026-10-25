// Dashboard tiếng Việt (HTML/JS thuần, không thư viện ngoài) dùng API của backend (/api/*).
"use strict";

const CLASSES = ["empty_quiet", "empty_disturbance", "occupied_still", "occupied_moving"];
const CLASS_VI = {
  empty_quiet: "Phòng trống, ít nhiễu",
  empty_disturbance: "Phòng trống, có quạt/rèm/nhiễu",
  occupied_still: "Có người ngồi/nằm yên",
  occupied_moving: "Có người di chuyển",
  unlabeled: "Chưa gán nhãn",
};
const STATE_VI = { occupied: "Có người", empty: "Trống", uncertain: "Chưa chắc" };
const DECISION_VI = { occupied: "có người", empty: "trống", unknown: "chưa có" };
const METHOD_VI = {
  model: "Mô hình TinyML",
  threshold_baseline: "Ngưỡng theo nền phòng",
  threshold_factory: "Ngưỡng mặc định LD2410",
  none: "—",
};
const RADAR_VI = {
  ok: ["Hoạt động", "ok"], init: ["Đang khởi động", "warn"], no_data: ["Radar không gửi dữ liệu", "bad"],
  not_engineering: ["Chưa bật Engineering Mode", "bad"], bad_frames: ["Khung lỗi (sai baud?)", "bad"],
  unsupported_layout: ["Biến thể không hỗ trợ", "bad"],
};

const state = { devices: [], device: localStorage.getItem("device") || "", timers: [], live: null };

// ------------------------------------------------------------------ tiện ích
function $(sel, el = document) { return el.querySelector(sel); }
function esc(s) { return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }
function fmtTime(ms) { return ms ? new Date(ms).toLocaleString("vi-VN") : "—"; }
function fmtDur(s) { if (s === null || s === undefined) return "—"; s = Math.round(s); return s >= 3600 ? `${(s / 3600).toFixed(1)} giờ` : s >= 60 ? `${Math.floor(s / 60)} phút ${s % 60} s` : `${s} s`; }
function num(v, d = 3) { return v === null || v === undefined || Number.isNaN(v) ? "—" : (typeof v === "number" ? +v.toFixed(d) : v); }
function toast(msg, err = false) {
  const t = $("#toast"); t.textContent = msg; t.className = "toast" + (err ? " err" : "");
  clearTimeout(toast.h); toast.h = setTimeout(() => t.classList.add("hidden"), err ? 7000 : 3500);
}
async function api(path, opts = {}) {
  const headers = Object.assign({ "Content-Type": "application/json" }, opts.headers || {});
  const tok = localStorage.getItem("adminToken");
  if (tok) headers.Authorization = "Bearer " + tok;
  const r = await fetch("/api" + path, Object.assign({}, opts, { headers, body: opts.json !== undefined ? JSON.stringify(opts.json) : opts.body }));
  const text = await r.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  if (!r.ok) {
    const msg = data && data.detail ? (typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail)) : r.statusText;
    if (r.status === 401) toast("Cần ADMIN_TOKEN: nhập ở tab Cài đặt", true);
    throw new Error(msg);
  }
  return data;
}
function every(ms, fn) { fn(); state.timers.push(setInterval(fn, ms)); }
function clearTimers() { state.timers.forEach(clearInterval); state.timers = []; }
function deviceSelect(id = "dev") {
  const opts = state.devices.map((d) => `<option value="${esc(d.device_id)}" ${d.device_id === state.device ? "selected" : ""}>${esc(d.name || d.device_id)} (${esc(d.device_id)})</option>`).join("");
  return `<select id="${id}">${opts || '<option value="">(chưa có thiết bị)</option>'}</select>`;
}
function bindDeviceSelect(rerender, id = "dev") {
  const s = $("#" + id);
  if (s) s.onchange = () => { state.device = s.value; localStorage.setItem("device", s.value); rerender(); };
}
async function loadDevices() {
  try { state.devices = await api("/devices"); } catch { state.devices = []; }
  if (!state.device && state.devices.length) state.device = state.devices[0].device_id;
  return state.devices;
}
function cur() { return state.devices.find((d) => d.device_id === state.device); }
function simpleMarkdown(md) {
  const lines = md.split("\n"); let html = "", inTable = false, inList = false;
  const inline = (s) => esc(s).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>").replace(/`(.+?)`/g, "<code>$1</code>");
  for (const ln of lines) {
    if (/^\|.*\|$/.test(ln.trim())) {
      if (/^\|[\s|:-]+\|$/.test(ln.trim())) continue;
      if (!inTable) { html += "<table>"; inTable = true; }
      html += "<tr>" + ln.trim().slice(1, -1).split("|").map((c) => `<td>${inline(c.trim())}</td>`).join("") + "</tr>";
      continue;
    } else if (inTable) { html += "</table>"; inTable = false; }
    if (/^\s*- /.test(ln)) { if (!inList) { html += "<ul>"; inList = true; } html += `<li>${inline(ln.replace(/^\s*- /, ""))}</li>`; continue; }
    else if (inList) { html += "</ul>"; inList = false; }
    if (/^> ?/.test(ln)) { html += `<blockquote>${inline(ln.replace(/^> ?(- )?/, ""))}</blockquote>`; continue; }
    const m = ln.match(/^(#{1,4}) (.*)/);
    if (m) { html += `<h${m[1].length}>${inline(m[2])}</h${m[1].length}>`; continue; }
    if (ln.trim()) html += `<p>${inline(ln)}</p>`;
  }
  return html + (inTable ? "</table>" : "") + (inList ? "</ul>" : "");
}

// ------------------------------------------------------------------ Tổng quan + biểu đồ trực tiếp
async function renderOverview(main) {
  await loadDevices();
  main.innerHTML = `
    <h2>Tổng quan</h2>
    <div class="grid" id="cards"></div>
    <div class="card" style="margin-top:12px">
      <div class="row"><h3 style="margin:0">Dữ liệu trực tiếp</h3> ${deviceSelect()}
        <span class="muted small">Đỏ: ngưỡng phương pháp ngưỡng, nét đứt: p99 phòng trống (nếu đang dùng nền)</span></div>
      <div class="legend"><span><i style="background:#4263eb"></i>chuyển động</span><span><i style="background:#f08c00"></i>tĩnh</span>
        <span><i style="background:#d9480f"></i>có người</span><span><i style="background:#2b8a3e"></i>trống</span><span><i style="background:#adb5bd"></i>chưa chắc</span></div>
      <canvas id="bars"></canvas>
      <div class="row small muted">Năng lượng theo gate (gate 0 gần nhất, mỗi gate 0,75 m) trong 2 phút gần nhất:</div>
      <canvas id="hm"></canvas><canvas id="hs" style="margin-top:4px"></canvas>
      <div class="row small muted">Điểm có người (điểm mô hình hoặc tỉ lệ mẫu vượt ngưỡng — KHÔNG phải xác suất) và trạng thái:</div>
      <canvas id="score"></canvas>
      <div class="row">
        <button id="fa" class="danger">Đánh dấu báo giả (đang báo có người nhưng phòng trống)</button>
        <button id="miss" class="danger">Đánh dấu bỏ sót (có người nhưng báo trống)</button>
        <span class="muted small">Lưu sự kiện và trích 10 phút dữ liệu gần nhất thành phiên chưa gán nhãn để gán sau.</span>
      </div>
    </div>`;
  bindDeviceSelect(() => render());
  const buf = []; let cursor = 0;
  async function tick() {
    await loadDevices();
    $("#cards").innerHTML = state.devices.map(deviceCard).join("") || `<div class="card">Chưa có thiết bị nào gửi dữ liệu. Kiểm tra Wi-Fi/MQTT của ESP32 hoặc chạy <code>tools/fake_device.py</code>.</div>`;
    const d = cur();
    if (!d) return;
    const r = await api(`/devices/${encodeURIComponent(d.device_id)}/live?cursor=${cursor}`);
    cursor = r.cursor;
    for (const row of r.rows) buf.push(row);
    while (buf.length > 600) buf.shift();
    const last = buf[buf.length - 1];
    const energies = (row) => [...Array(9).keys()].map((g) => row["m" + g]).concat([...Array(9).keys()].map((g) => row["s" + g]));
    const cfg = d.config || {};
    const base = cfg.baseline ? (await baselineStats(cfg.baseline.id)) : null;
    Charts.gateBars($("#bars"), last ? energies(last) : null, cfg.thresholds, base);
    Charts.heatmap($("#hm"), buf.map(energies), 0, "Chuyển động");
    Charts.heatmap($("#hs"), buf.map(energies), 1, "Tĩnh");
    const pts = buf.filter((x) => x.ev === 1).map((x) => ({ t: x.ts_ms, score: x.occ_pm >= 0 ? x.occ_pm / 1000 : null, state: x.p_state }));
    const st = d.status && d.status.presence;
    Charts.scoreLine($("#score"), pts, st && st.params);
  }
  every(1000, () => tick().catch((e) => toast(e.message, true)));
  $("#fa").onclick = () => markEvent("false_alarm");
  $("#miss").onclick = () => markEvent("missed_detection");
}
const baselineCache = {};
async function baselineStats(id) {
  if (!baselineCache[id]) {
    const all = await api("/baselines");
    for (const b of all) baselineCache[b.baseline_id] = b.stats;
  }
  return baselineCache[id];
}
async function markEvent(kind) {
  const d = cur(); if (!d) return;
  const note = prompt(kind === "false_alarm" ? "Ghi chú báo giả (ví dụ: quạt chạy, rèm bay):" : "Ghi chú bỏ sót (ví dụ: ngồi đọc sách ở góc phòng):", "");
  if (note === null) return;
  const r = await api("/events", { method: "POST", json: { device_id: d.device_id, kind, note } });
  toast(r.snapshot_session ? `Đã lưu và trích dữ liệu thành phiên ${r.snapshot_session} (chưa gán nhãn)` : "Đã lưu sự kiện vào phiên đang thu");
}
function deviceCard(d) {
  const st = d.status || {}, p = st.presence || {}, r = st.radar || {}, m = st.model || {}, b = st.buffer || {};
  const [radarText, radarCls] = RADAR_VI[r.status] || [r.status || "—", "warn"];
  const act = m.active;
  return `<div class="card">
    <div class="row"><h3 style="margin:0;flex:1">${esc(d.name || d.device_id)}</h3>
      <span class="pill ${d.online ? "ok" : "bad"}">${d.online ? "trực tuyến" : "mất kết nối"}</span></div>
    <div class="row"><span class="state ${esc(p.state || "uncertain")}">${STATE_VI[p.state] || "Chưa chắc"}</span>
      <span class="muted">quyết định: ${DECISION_VI[p.decision] || "—"}</span></div>
    <div class="kv">
      <div>Phương pháp</div><div>${METHOD_VI[p.method] || METHOD_VI[m.method] || "—"}${p.method === "model" ? "" : " <span class='pill warn'>chưa dùng mô hình</span>"}${p.model_blocked === "radar_gate_mismatch" ? ` <span class='pill bad'>mô hình bị chặn: radar ${esc(r.gate_cm)} cm/gate, mô hình ${esc(act ? act.gate_cm : "?")} cm/gate</span>` : p.model_blocked === "radar_gate_unknown" ? " <span class='pill warn'>mô hình tạm dừng: chưa đọc được độ phân giải gate của radar</span>" : ""}</div>
      <div>Điểm mô hình</div><div>${num(p.score)} ${p.calibrated_probability !== undefined ? `<span class="muted small">(xác suất hiệu chuẩn ước lượng ${num(p.calibrated_probability, 2)})</span>` : ""}</div>
      <div>Radar</div><div><span class="pill ${radarCls}">${esc(radarText)}</span> ${esc(r.version || "")} gate ${esc(r.gate_cm || "?")} cm</div>
      <div>Khung radar/s</div><div>${num(st.sampler && st.sampler.radar_reports_per_s, 1)} · mẫu ${num(st.sampler && st.sampler.sample_rate_hz, 2)} Hz</div>
      <div>Một lần quyết định</div><div>${p.eval_us ? `${esc(p.eval_us.last)} µs (lớn nhất ${esc(p.eval_us.max)} µs)` : "—"}</div>
      <div>Mô hình</div><div>${act ? `${esc(act.model_id)} v${esc(act.version)}${act.simulated_data ? " <span class='pill bad'>dữ liệu mô phỏng</span>" : ""}` : "không có"}</div>
      <div>Cập nhật</div><div>${esc(m.update ? m.update.phase : "—")} ${m.update && m.update.error ? `<span class="pill bad">${esc(m.update.error)}</span>` : ""}</div>
      <div>Bộ đệm</div><div>${esc(b.used ?? "—")}/${esc(b.capacity ?? "—")} mẫu, đã bỏ ${esc(b.dropped ?? 0)}</div>
      <div>Mạng</div><div>${esc(st.ip || "")} RSSI ${esc(st.rssi ?? "—")} · ${d.last_seen_ms ? fmtTime(d.last_seen_ms) : "—"}</div>
      <div>Firmware</div><div>${esc(d.fw_version || "—")} · heap tối thiểu ${esc(st.heap_min ?? "—")} B</div>
    </div>
    ${d.recording ? `<div class="notice info">Đang thu phiên ${esc(d.recording.session_id)} (${esc(d.recording.kind)})</div>` : ""}
  </div>`;
}

// ------------------------------------------------------------------ Thu dữ liệu
async function renderCollect(main) {
  await loadDevices();
  const rooms = await api("/rooms").catch(() => []);
  main.innerHTML = `
    <h2>Thu dữ liệu có gán nhãn</h2>
    <div class="card">
      <div class="row">Thiết bị ${deviceSelect()} Phòng <input id="room" list="rooms" placeholder="phong-khach">
        <datalist id="rooms">${rooms.map((r) => `<option value="${esc(r.room_id)}">${esc(r.name)}</option>`).join("")}</datalist></div>
      <div class="row">Ghi chú môi trường <input id="notes" style="flex:1" placeholder="quạt trần số 2, cửa sổ mở, rèm vải mỏng…"></div>
      <div class="row"><button class="primary" id="start">Bắt đầu phiên</button><button id="stop" class="danger">Dừng phiên</button>
        <span id="recinfo" class="muted"></span></div>
    </div>
    <div class="card" style="margin-top:12px">
      <h3>Nhãn từ lúc này (người xác nhận)</h3>
      <div class="row">${CLASSES.map((c, i) => `<button class="big label-${i}" data-label="${c}">${CLASS_VI[c]}</button>`).join("")}
        <button class="big" data-label="unlabeled">Chưa gán nhãn (dừng nhãn)</button></div>
      <div class="muted small">Mỗi lần bấm đóng khoảng nhãn trước và mở khoảng mới. Dữ liệu chưa gán nhãn KHÔNG được coi là phòng trống và không dùng để huấn luyện. Sửa nhãn chính xác ở tab "Phiên &amp; nhãn".</div>
    </div>
    <div class="card" style="margin-top:12px">
      <h3>Tình huống khó nên thu (mỗi tình huống vài phút, nhiều lần, nhiều ngày)</h3>
      <ul>
        <li>Người ngồi yên (đọc sách, xem điện thoại) <b>khi quạt đang chạy</b> → nhãn "Có người ngồi/nằm yên".</li>
        <li>Người ngồi yên <b>cùng khoảng cách với quạt</b> (cùng gate) → "Có người ngồi/nằm yên".</li>
        <li>Người ở <b>xa</b> (gate 6–8), nằm ngủ → "Có người ngồi/nằm yên".</li>
        <li>Người <b>ra khỏi phòng nhưng quạt vẫn chạy</b>, rèm bay → "Phòng trống, có quạt/rèm/nhiễu".</li>
        <li>Phòng trống yên tĩnh ban đêm và ban ngày → "Phòng trống, ít nhiễu".</li>
        <li>Đi lại, vào/ra phòng nhiều lần → "Có người di chuyển" (giúp đo độ trễ vào/ra).</li>
      </ul>
      <div class="muted small">Gán nhãn ngay khi trạng thái thay đổi; vài giây quanh lúc đổi nhãn được tự loại khi huấn luyện.</div>
    </div>`;
  bindDeviceSelect(() => render());
  async function refresh() {
    await loadDevices();
    const d = cur();
    const rec = d && d.recording;
    $("#start").disabled = !d || !!rec; $("#stop").disabled = !rec;
    if (rec) {
      const s = await api(`/sessions/${rec.session_id}`);
      const open = s.labels.find((l) => l.end_ms === null);
      $("#recinfo").innerHTML = `Đang thu <b>${esc(s.session_id)}</b> từ ${fmtTime(s.started_at_ms)} · ${s.samples} mẫu · nhãn hiện tại: <b>${open ? CLASS_VI[open.label] : "chưa gán nhãn"}</b>`;
      document.querySelectorAll("[data-label]").forEach((b) => b.classList.toggle("current", open ? b.dataset.label === open.label : b.dataset.label === "unlabeled"));
    } else { $("#recinfo").textContent = d ? "Không có phiên đang thu" : ""; }
    document.querySelectorAll("[data-label]").forEach((b) => (b.disabled = !rec));
  }
  every(2000, () => refresh().catch((e) => toast(e.message, true)));
  $("#start").onclick = async () => {
    const d = cur(); if (!d) return;
    await api("/sessions", { method: "POST", json: { device_id: d.device_id, room_id: $("#room").value || null, notes: $("#notes").value } }).then(() => toast("Đã bắt đầu phiên")).catch((e) => toast(e.message, true));
    refresh();
  };
  $("#stop").onclick = async () => {
    const d = cur(); if (!d || !d.recording) return;
    await api(`/sessions/${encodeURIComponent(d.recording.session_id)}/stop`, { method: "POST" }).then(() => toast("Đã dừng phiên")).catch((e) => toast(e.message, true));
    refresh();
  };
  document.querySelectorAll("[data-label]").forEach((b) => (b.onclick = async () => {
    const d = cur(); if (!d || !d.recording) return;
    await api(`/sessions/${encodeURIComponent(d.recording.session_id)}/label-now`, { method: "POST", json: { label: b.dataset.label } }).catch((e) => toast(e.message, true));
    refresh();
  }));
}

// ------------------------------------------------------------------ Phiên & nhãn
async function renderSessions(main, sessionId) {
  if (sessionId) return renderSessionDetail(main, sessionId);
  const list = await api("/sessions");
  main.innerHTML = `<h2>Phiên thu</h2><div class="card"><table>
    <tr><th>Phiên</th><th>Thiết bị</th><th>Loại</th><th>Nguồn</th><th>Bắt đầu</th><th>Thời lượng</th><th>Mẫu</th><th>Nhãn</th><th>Tập</th><th>Ghi chú</th></tr>
    ${list.map((s) => `<tr><td><a href="#/sessions/${encodeURIComponent(s.session_id)}">${esc(s.session_id)}</a></td><td>${esc(s.device_id)}</td>
      <td>${esc(s.kind)}</td><td>${s.source === "simulated" ? '<span class="pill bad">mô phỏng</span>' : esc(s.source)}</td>
      <td>${fmtTime(s.started_at_ms)}</td><td>${s.ended_at_ms ? fmtDur((s.ended_at_ms - s.started_at_ms) / 1000) : '<span class="pill warn">đang thu</span>'}</td>
      <td>${s.samples}</td><td>${s.label_count || '<span class="pill warn">chưa có</span>'}</td><td>${esc(s.split || "tự động")}</td><td class="small">${esc(s.notes)}</td></tr>`).join("")}
  </table></div>`;
}

function radarSummary(r) {
  if (!r || !Object.keys(r).length) return "—";
  const t = r.thresholds ? ` · ngưỡng radar chuyển động [${esc(r.thresholds.moving)}] tĩnh [${esc(r.thresholds.still)}]` : "";
  return `${esc(r.version || r.variant || "?")} · gate ${esc(r.gate_cm || r.gate_resolution_cm || "?")} cm · gate xa nhất ${esc(r.max_moving_gate ?? "?")}/${esc(r.max_still_gate ?? "?")}${t}`;
}

async function renderSessionDetail(main, sid) {
  const s = await api(`/sessions/${encodeURIComponent(sid)}`);
  const data = await api(`/sessions/${encodeURIComponent(sid)}/samples?step=1`);
  const sel = { a: null, b: null };
  main.innerHTML = `
    <h2><a href="#/sessions">Phiên</a> / ${esc(s.session_id)}</h2>
    <div class="card">
      <div class="kv"><div>Thiết bị</div><div>${esc(s.device_id)} · phòng ${esc(s.room_id || "—")}</div>
        <div>Thời gian</div><div>${fmtTime(s.started_at_ms)} → ${fmtTime(s.ended_at_ms)}</div>
        <div>Radar</div><div class="small">${esc(radarSummary(s.radar))}
          <details><summary>chi tiết</summary><pre class="log" style="white-space:pre-wrap">${esc(JSON.stringify(s.radar, null, 1))}</pre></details></div></div>
      <div class="row">Ghi chú <input id="notes" style="flex:1" value="${esc(s.notes)}">
        Tập <select id="split">${["auto", "train", "val", "test"].map((x) => `<option ${((s.split || "auto") === x) ? "selected" : ""}>${x}</option>`).join("")}</select>
        <button id="save">Lưu</button><button id="del" class="danger">Xóa phiên</button></div>
    </div>
    <div class="card" style="margin-top:12px">
      <div class="legend">${CLASSES.map((c, i) => `<span><i style="background:${Charts.CLASS_COLORS[i]}"></i>${CLASS_VI[c]}</span>`).join("")}
        <span class="muted">Hàng "Thiết bị" là quyết định của thiết bị (dự đoán), không phải nhãn.</span></div>
      <canvas id="tl"></canvas>
      <div class="row">Kéo chuột trên biểu đồ để chọn khoảng: <span id="selinfo" class="muted">chưa chọn</span></div>
      <div class="row">${CLASSES.map((c, i) => `<button class="label-${i}" data-set="${c}">${CLASS_VI[c]}</button>`).join("")}
        <button data-set="unlabeled">Xóa nhãn khoảng này</button></div>
    </div>
    <div class="card" style="margin-top:12px"><h3>Các khoảng nhãn (người xác nhận)</h3><div id="labels"></div></div>`;
  let geom = null;
  function draw() { geom = Charts.sessionTimeline($("#tl"), data, s.labels, sel); }
  function drawLabels() {
    $("#labels").innerHTML = `<table><tr><th>Từ</th><th>Đến</th><th>Nhãn</th><th>Ghi chú</th><th></th></tr>${s.labels.map((l) => `<tr><td>${fmtTime(l.start_ms)}</td><td>${fmtTime(l.end_ms)}</td><td>${CLASS_VI[l.label]}</td><td class="small">${esc(l.note)}</td><td><button data-del="${l.label_id}" class="danger">Xóa</button></td></tr>`).join("")}</table>`;
    document.querySelectorAll("[data-del]").forEach((b) => (b.onclick = async () => {
      await api(`/labels/${b.dataset.del}`, { method: "DELETE" }); Object.assign(s, await api(`/sessions/${encodeURIComponent(sid)}`)); draw(); drawLabels();
    }));
  }
  draw(); drawLabels();
  const cv = $("#tl"); let dragging = false;
  const tAt = (e) => geom ? geom.toTime(e.offsetX) : null;
  cv.onmousedown = (e) => { dragging = true; sel.a = sel.b = tAt(e); draw(); };
  cv.onmousemove = (e) => { if (dragging) { sel.b = tAt(e); draw(); } };
  window.onmouseup = () => {
    if (!dragging) return; dragging = false;
    if (sel.a !== null) $("#selinfo").textContent = `${fmtTime(Math.min(sel.a, sel.b))} → ${fmtTime(Math.max(sel.a, sel.b))} (${fmtDur(Math.abs(sel.b - sel.a) / 1000)})`;
  };
  document.querySelectorAll("[data-set]").forEach((b) => (b.onclick = async () => {
    if (sel.a === null || sel.a === sel.b) return toast("Hãy kéo chọn một khoảng thời gian trước", true);
    const r = await api(`/sessions/${encodeURIComponent(sid)}/labels`, { method: "POST", json: { start_ms: Math.round(Math.min(sel.a, sel.b)), end_ms: Math.round(Math.max(sel.a, sel.b)), label: b.dataset.set } }).catch((e) => toast(e.message, true));
    if (r) { Object.assign(s, r); draw(); drawLabels(); toast("Đã cập nhật nhãn"); }
  }));
  $("#save").onclick = () => api(`/sessions/${encodeURIComponent(sid)}`, { method: "PATCH", json: { notes: $("#notes").value, split: $("#split").value } }).then(() => toast("Đã lưu")).catch((e) => toast(e.message, true));
  $("#del").onclick = async () => { if (confirm("Xóa phiên và dữ liệu của nó?")) { await api(`/sessions/${encodeURIComponent(sid)}`, { method: "DELETE" }); location.hash = "#/sessions"; } };
}

// ------------------------------------------------------------------ Học phòng trống
async function renderBaseline(main) {
  await loadDevices();
  main.innerHTML = `
    <h2>Học phòng trống</h2>
    <div class="notice">Chỉ chạy khi bạn <b>chắc chắn không có ai trong phòng</b> (kể cả người ngủ, ngồi yên). Thiết bị không tự học nền.
      Nền dùng để quan sát và cho phương pháp ngưỡng đối chứng; không thay đổi 72 đặc trưng của mô hình.</div>
    <div class="card">
      <div class="row">Thiết bị ${deviceSelect()} Tên <input id="bname" value="Phòng trống"> Điều kiện <input id="bcond" style="flex:1" placeholder="quạt tắt, đêm, cửa đóng"></div>
      <div class="row"><label><input type="checkbox" id="bdist"> Có nguồn nhiễu đang chạy (quạt, rèm, máy lạnh)</label>
        Thời lượng <input type="number" id="bdur" value="120" min="30" max="3600"> giây</div>
      <div class="row"><label><input type="checkbox" id="bconfirm"> Tôi xác nhận trong phòng KHÔNG có người</label>
        <button class="primary" id="bstart">Bắt đầu học</button></div>
    </div>
    <div class="card" style="margin-top:12px"><h3>Các phiên nền</h3><div id="blist"></div></div>`;
  bindDeviceSelect(() => render());
  async function list() {
    const bl = await api("/baselines" + (state.device ? `?device_id=${encodeURIComponent(state.device)}` : ""));
    $("#blist").innerHTML = bl.length ? bl.map((b) => `<div class="card" style="margin:6px 0">
      <div class="row"><b>${esc(b.name)}</b> <span class="muted">${esc(b.condition)}</span> ${b.disturbance ? '<span class="pill warn">có nhiễu</span>' : ""}
        <span class="pill ${b.status === "ok" ? "ok" : b.status === "review" ? "warn" : b.status === "recording" ? "" : "bad"}">${{ ok: "tốt", review: "cần xem lại", recording: "đang ghi", failed: "thất bại" }[b.status] || b.status}</span>
        ${b.active ? '<span class="pill ok">đang dùng</span>' : ""} <span class="muted small">${fmtTime(b.created_at)}</span></div>
      ${(b.warnings || []).map((w) => `<div class="notice bad">${esc(w)}</div>`).join("")}
      ${b.stats ? `<canvas id="bc${b.baseline_id}"></canvas><div class="muted small">Cột: trung bình; vạch đen: p99; vạch đỏ: ngưỡng tính từ nền. ${b.stats.n_samples} mẫu.</div>` : ""}
      <div class="row">${b.status === "recording" ? `<button data-fin="${b.baseline_id}">Kết thúc ngay</button>` : ""}
        ${b.stats && !b.active ? `<button class="primary" data-act="${b.baseline_id}" data-review="${b.status === "review" ? 1 : 0}">Dùng làm nền</button>` : ""}
        ${b.active ? `<button data-deact="${b.baseline_id}">Thôi dùng</button>` : ""}<button class="danger" data-bdel="${b.baseline_id}">Xóa</button></div></div>`).join("") : '<span class="muted">Chưa có</span>';
    for (const b of bl) if (b.stats && $("#bc" + b.baseline_id)) Charts.gateBars($("#bc" + b.baseline_id), b.stats.mean, b.thresholds, { p99: b.stats.p99 });
    document.querySelectorAll("[data-fin]").forEach((x) => (x.onclick = () => api(`/baselines/${x.dataset.fin}/finish`, { method: "POST" }).then(list)));
    document.querySelectorAll("[data-act]").forEach((x) => (x.onclick = async () => {
      let ack = false;
      if (x.dataset.review === "1") { ack = confirm("Phiên nền có cảnh báo (có thể có người trong phòng). Vẫn dùng?"); if (!ack) return; }
      await api(`/baselines/${x.dataset.act}/activate`, { method: "POST", json: { active: true, acknowledge_warnings: ack } }).then(() => toast("Đã gửi ngưỡng nền xuống thiết bị")).catch((e) => toast(e.message, true));
      list();
    }));
    document.querySelectorAll("[data-deact]").forEach((x) => (x.onclick = () => api(`/baselines/${x.dataset.deact}/activate`, { method: "POST", json: { active: false } }).then(list)));
    document.querySelectorAll("[data-bdel]").forEach((x) => (x.onclick = () => confirm("Xóa phiên nền?") && api(`/baselines/${x.dataset.bdel}`, { method: "DELETE" }).then(list)));
  }
  every(3000, () => list().catch((e) => toast(e.message, true)));
  $("#bstart").onclick = () => {
    if (!$("#bconfirm").checked) return toast("Hãy xác nhận phòng không có người", true);
    api("/baselines", { method: "POST", json: { device_id: state.device, name: $("#bname").value, condition: $("#bcond").value, disturbance: $("#bdist").checked, duration_s: +$("#bdur").value, confirm_empty: true } })
      .then(() => { toast("Đang học nền: hãy rời khỏi phòng"); list(); }).catch((e) => toast(e.message, true));
  };
}

// ------------------------------------------------------------------ Huấn luyện
async function renderTrain(main) {
  main.innerHTML = `
    <h2>Huấn luyện</h2>
    <div class="card">
      <div class="row">Chia tập theo <select id="split"><option value="session">phiên</option><option value="day">ngày</option></select>
        Epoch tối đa <input type="number" id="epochs" value="300"> Patience <input type="number" id="pat" value="25"></div>
      <div class="row"><label><input type="checkbox" id="sim"> Cho phép dữ liệu MÔ PHỎNG (chỉ để kiểm tra pipeline)</label>
        <button class="primary" id="go">Bắt đầu huấn luyện</button></div>
      <div class="muted small">Dùng mọi phiên đã kết thúc có nhãn người xác nhận. Huấn luyện chạy nền trên CPU của máy chủ; xong sẽ có báo cáo đánh giá ở tab Mô hình. Không phát hành tự động.</div>
    </div>
    <div class="card" style="margin-top:12px"><h3>Tác vụ</h3><div id="jobs"></div></div>
    <div class="card" style="margin-top:12px"><h3>Log</h3><pre class="log" id="log">Chọn một tác vụ</pre></div>`;
  let shown = null;
  async function list() {
    const js = await api("/jobs");
    $("#jobs").innerHTML = `<table><tr><th>#</th><th>Trạng thái</th><th>Tiến trình</th><th>Mô hình</th><th>Bắt đầu</th><th>Lỗi</th><th></th></tr>${js.map((j) => {
      const p = j.progress || {};
      const prog = p.stage === "train" ? `epoch ${p.epoch}/${p.epochs} · loss ${num(p.loss)} · val_loss ${num(p.val_loss)}` : esc(p.stage || "");
      return `<tr><td>${j.job_id}</td><td><span class="pill ${j.status === "done" ? "ok" : j.status === "failed" ? "bad" : "warn"}">${esc(j.status)}</span></td><td>${prog}</td>
        <td>${j.model_id ? `<a href="#/models">${esc(j.model_id)}</a>` : ""}</td><td>${fmtTime(j.started_at)}</td><td class="small">${esc(j.error || "")}</td>
        <td><button data-log="${j.job_id}">Log</button>${j.status === "running" ? `<button data-cancel="${j.job_id}" class="danger">Dừng</button>` : ""}</td></tr>`;
    }).join("")}</table>`;
    document.querySelectorAll("[data-log]").forEach((b) => (b.onclick = () => { shown = b.dataset.log; showLog(); }));
    document.querySelectorAll("[data-cancel]").forEach((b) => (b.onclick = () => api(`/jobs/${b.dataset.cancel}/cancel`, { method: "POST" })));
    if (shown) showLog();
  }
  async function showLog() { const t = await api(`/jobs/${shown}/log?lines=300`); const el = $("#log"); el.textContent = t || "(trống)"; el.scrollTop = el.scrollHeight; }
  every(2000, () => list().catch((e) => toast(e.message, true)));
  $("#go").onclick = () => {
    if ($("#sim").checked && !confirm("Mô hình huấn luyện bằng dữ liệu mô phỏng KHÔNG phản ánh khả năng phát hiện thực tế. Tiếp tục để kiểm tra pipeline?")) return;
    api("/jobs", { method: "POST", json: { include_simulated: $("#sim").checked, overrides: { data: { split_mode: $("#split").value }, model: { epochs: +$("#epochs").value, patience: +$("#pat").value } } } })
      .then((j) => { shown = j.job_id; toast("Đã bắt đầu tác vụ #" + j.job_id); list(); }).catch((e) => toast(e.message, true));
  };
}

// ------------------------------------------------------------------ Mô hình
function tlRow(tl, key, d = 2) { return tl ? num(tl[key], d) : "—"; }
async function renderModels(main) {
  await loadDevices();
  const [models, releases] = await Promise.all([api("/models"), api("/releases")]);
  const d = cur(); const st = d && d.status && d.status.model;
  main.innerHTML = `
    <h2>Mô hình</h2>
    <div class="card"><div class="row">Thiết bị ${deviceSelect()}
      <span>Đang chạy: <b>${st && st.active ? `${esc(st.active.model_id)} v${esc(st.active.version)}` : "không có (phương pháp ngưỡng)"}</b></span>
      <span class="muted">${st && st.previous ? `bản trước: ${esc(st.previous.model_id)} v${esc(st.previous.version)}` : ""}</span>
      <button id="rollback" ${st && st.previous ? "" : "disabled"}>Khôi phục bản trước</button></div></div>
    <div class="card" style="margin-top:12px"><table>
      <tr><th>Phiên bản</th><th>Mã</th><th>Trạng thái</th><th>Dữ liệu</th><th>Báo giả/giờ (INT8)</th><th>Bỏ sót (s)</th><th>Bỏ sót dài nhất (s)</th><th>Ngưỡng LD2410: báo giả/giờ · bỏ sót</th><th>Độ chính xác cửa sổ test</th><th></th></tr>
      ${models.map((m) => {
        const tl = m.evaluation_summary.timeline || {}, i8 = tl.int8, th = tl.threshold_factory;
        return `<tr><td>${m.model_version}</td><td>${esc(m.model_id)}</td><td><span class="pill ${m.status === "approved" ? "ok" : m.status === "rejected" ? "bad" : "warn"}">${esc(m.status)}</span></td>
          <td>${m.simulated ? '<span class="pill bad">mô phỏng</span>' : ""} ${m.conclusive ? '<span class="pill ok">đủ để kết luận</span>' : '<span class="pill warn">chưa đủ dữ liệu</span>'}${m.signed ? "" : ' <span class="pill bad" title="Firmware mặc định từ chối gói chưa ký: đặt MODEL_SIGNING_KEY rồi huấn luyện lại">chưa ký</span>'}</td>
          <td>${tlRow(i8, "false_alarms_per_hour")}</td><td>${tlRow(i8, "missed_s", 0)}</td><td>${tlRow(i8, "longest_miss_s", 0)}</td>
          <td>${th ? `${num(th.false_alarms_per_hour, 2)} · ${num(th.missed_s, 0)} s` : "—"}</td><td>${num(m.evaluation_summary.test_windows)}</td>
          <td><button data-rep="${esc(m.model_id)}">Báo cáo</button> <label><input type="checkbox" data-cmp="${esc(m.model_id)}"> so sánh</label></td></tr>`;
      }).join("")}
    </table><div class="row"><button id="cmp">So sánh 2 mô hình đã chọn</button></div></div>
    <div id="detail"></div>
    <div class="card" style="margin-top:12px"><h3>Lịch sử phát hành</h3><table><tr><th>#</th><th>Mô hình</th><th>Thiết bị</th><th>Loại</th><th>Trạng thái</th><th>Lỗi</th><th>Cập nhật</th></tr>
      ${releases.map((r) => `<tr><td>${r.release_id}</td><td>${esc(r.model_id)}</td><td>${esc(r.device_id)}</td><td>${esc(r.kind)}</td>
        <td><span class="pill ${r.status === "active" ? "ok" : r.status === "failed" ? "bad" : "warn"}">${esc(r.status)}</span></td><td class="small">${esc(r.error || "")}</td><td>${fmtTime(r.updated_at)}</td></tr>`).join("")}</table></div>`;
  bindDeviceSelect(() => render());
  $("#rollback").onclick = () => confirm("Khôi phục mô hình trước trên thiết bị?") && api(`/devices/${encodeURIComponent(state.device)}/rollback`, { method: "POST" }).then(() => toast("Đã gửi yêu cầu khôi phục")).catch((e) => toast(e.message, true));
  document.querySelectorAll("[data-rep]").forEach((b) => (b.onclick = () => showModel(b.dataset.rep)));
  $("#cmp").onclick = async () => {
    const ids = [...document.querySelectorAll("[data-cmp]:checked")].map((x) => x.dataset.cmp);
    if (ids.length !== 2) return toast("Chọn đúng 2 mô hình", true);
    const r = await api(`/models/compare?a=${ids[0]}&b=${ids[1]}`);
    const rows = [["Báo giả/giờ", "false_alarms_per_hour"], ["Tổng thời gian báo giả (s)", "false_alarm_s"], ["Bỏ sót (s)", "missed_s"], ["Bỏ sót dài nhất (s)", "longest_miss_s"], ["Chưa chắc (s)", "uncertain_s"], ["Thời gian có nhãn (s)", "labeled_s"]];
    const cell = (m, k, meth) => { const t = m.evaluation && m.evaluation.timeline && m.evaluation.timeline[meth]; return t ? num(t[k], 2) : "—"; };
    $("#detail").innerHTML = `<div class="card" style="margin-top:12px"><h3>So sánh (phát lại tập test của từng mô hình)</h3>
      <div class="notice">Hai mô hình có thể có tập test khác nhau; chỉ so trực tiếp khi cùng dữ liệu. Xem mục "Dữ liệu" trong báo cáo.</div>
      <table><tr><th>Chỉ số</th>${ids.map((i) => `<th>${esc(i)} INT8</th><th>${esc(i)} ngưỡng LD2410</th>`).join("")}</tr>
      ${rows.map(([t, k]) => `<tr><td>${t}</td>${ids.map((i) => `<td>${cell(r[i], k, "int8")}</td><td>${cell(r[i], k, "threshold_factory")}</td>`).join("")}</tr>`).join("")}
      <tr><td>Chưa đủ dữ liệu</td>${ids.map((i) => `<td colspan="2" class="small">${esc((r[i].insufficient_data || []).join("; ") || "không")}</td>`).join("")}</tr></table></div>`;
  };
}
async function showModel(id) {
  const [m, md] = await Promise.all([api(`/models/${id}`), api(`/models/${id}/report.md`).catch(() => "")]);
  const insufficient = (m.summary.insufficient_data || []).length > 0;
  $("#detail").innerHTML = `<div class="card" style="margin-top:12px">
    <div class="row"><h3 style="margin:0;flex:1">${esc(m.model_id)} (v${m.model_version})</h3>
      ${m.status !== "approved" ? `<button class="primary" id="approve">Duyệt sau khi đã xem báo cáo</button><button class="danger" id="reject">Không dùng</button>` : ""}
      ${m.status === "approved" ? `Phát hành xuống ${deviceSelect("reldev")} <button class="primary" id="release">Phát hành</button>` : ""}</div>
    ${insufficient ? `<div class="notice bad">Báo cáo ghi chưa đủ dữ liệu để kết luận${m.simulated ? " (dữ liệu mô phỏng)" : ""}. Chỉ duyệt để thử nghiệm.</div>` : ""}
    <div class="report">${simpleMarkdown(md)}</div></div>`;
  $("#detail").scrollIntoView({ behavior: "smooth" });
  const ap = $("#approve");
  if (ap) ap.onclick = async () => {
    const note = prompt("Ghi chú duyệt (đã xem báo cáo, so với mô hình hiện tại…):", "");
    if (note === null) return;
    let ack = false;
    if (insufficient) { ack = confirm("Mô hình chưa đủ dữ liệu kết luận / dữ liệu mô phỏng. Vẫn duyệt để THỬ NGHIỆM?"); if (!ack) return; }
    await api(`/models/${id}/approve`, { method: "POST", json: { note, acknowledge_insufficient: ack } }).then(() => toast("Đã duyệt")).catch((e) => toast(e.message, true));
    render();
  };
  const rj = $("#reject");
  if (rj) rj.onclick = () => api(`/models/${id}/reject`, { method: "POST", json: { note: "" } }).then(render);
  const rl = $("#release");
  if (rl) rl.onclick = () => {
    const dev = $("#reldev").value;
    if (!confirm(`Phát hành ${id} xuống ${dev}? Thiết bị sẽ tải, kiểm tra chữ ký, chạy thử rồi mới kích hoạt; mô hình hiện tại được giữ để khôi phục.`)) return;
    api("/releases", { method: "POST", json: { model_id: id, device_id: dev } }).then(() => toast("Đã gửi lệnh phát hành qua MQTT")).catch((e) => toast(e.message, true));
  };
}

// ------------------------------------------------------------------ Cài đặt
async function renderSettings(main) {
  await loadDevices();
  const d = cur(); const cfg = (d && d.config) || {};
  const pm = cfg.presence_model, pt = cfg.presence_threshold || {};
  const rec = d && d.status && d.status.model && d.status.model.active && d.status.model.active.recommended;
  const p = (pfx, v) => `<div class="row">Ngưỡng xác nhận (enter) <input type="number" step="0.05" id="${pfx}en" value="${v.enter ?? ""}">
    Ngưỡng nhả (exit) <input type="number" step="0.05" id="${pfx}ex" value="${v.exit ?? ""}">
    Xác nhận có người <input type="number" id="${pfx}co" value="${v.confirm_occupied_ms ? v.confirm_occupied_ms / 1000 : ""}"> s
    Xác nhận trống <input type="number" id="${pfx}ce" value="${v.confirm_empty_ms ? v.confirm_empty_ms / 1000 : ""}"> s</div>`;
  main.innerHTML = `
    <h2>Cài đặt</h2>
    <div class="card"><h3>Truy cập</h3><div class="row">ADMIN_TOKEN <input id="tok" type="password" value="${esc(localStorage.getItem("adminToken") || "")}" style="width:280px"><button id="savetok">Lưu trên trình duyệt này</button></div></div>
    <div class="card" style="margin-top:12px"><h3>Thiết bị ${deviceSelect()}</h3>
      ${d ? `<div class="row">Tên <input id="dname" value="${esc(d.name || "")}"> Phòng <input id="droom" value="${esc(d.room_id || "")}"><button id="savedev">Lưu</button>
        <button id="reboot">Khởi động lại</button><button id="reinit">Khởi tạo lại radar</button></div>
      <h3>Logic giữ trạng thái — mô hình</h3>
      <div class="muted small">Điểm mô hình = tổng softmax của 2 lớp có người (không phải xác suất). Mặc định dùng tham số đề xuất trong gói mô hình${rec ? `: enter ${rec.enter}, exit ${rec.exit}, ${rec.confirm_occupied_ms / 1000} s / ${rec.confirm_empty_ms / 1000} s` : ""}.</div>
      ${p("m", pm || rec || {})}
      <div class="row"><button id="savem">Lưu cho mô hình</button><button id="resetm">Dùng tham số trong gói mô hình</button> ${pm ? '<span class="pill warn">đang ghi đè</span>' : ""}</div>
      <h3>Logic giữ trạng thái — phương pháp ngưỡng</h3>
      <div class="muted small">Điểm = tỉ lệ mẫu trong cửa sổ 4 s có gate vượt ngưỡng.</div>
      ${p("t", pt)}<div class="row"><button id="savet">Lưu cho phương pháp ngưỡng</button>
        <label><input type="checkbox" id="pref" ${cfg.prefer_threshold ? "checked" : ""}> Dùng phương pháp ngưỡng kể cả khi có mô hình</label><button id="savepref">Lưu</button></div>
      <div class="muted small">Khi chưa chắc, thiết bị giữ quyết định trước đó. Gợi ý: xác nhận có người 1–2 s, xác nhận trống 30–120 s.</div>` : "Chưa có thiết bị"}
    </div>`;
  bindDeviceSelect(() => render());
  $("#savetok").onclick = () => { localStorage.setItem("adminToken", $("#tok").value); toast("Đã lưu token"); };
  if (!d) return;
  const read = (pfx) => ({ enter: +$("#" + pfx + "en").value, exit: +$("#" + pfx + "ex").value, confirm_occupied_ms: Math.round(+$("#" + pfx + "co").value * 1000), confirm_empty_ms: Math.round(+$("#" + pfx + "ce").value * 1000) });
  const put = (body) => api(`/devices/${encodeURIComponent(d.device_id)}/config`, { method: "PUT", json: body }).then(() => { toast("Đã gửi cấu hình xuống thiết bị"); render(); }).catch((e) => toast(e.message, true));
  $("#savem").onclick = () => put({ presence_model: read("m") });
  $("#resetm").onclick = () => put({ reset_model_params: true });
  $("#savet").onclick = () => put({ presence_threshold: read("t") });
  $("#savepref").onclick = () => put({ prefer_threshold: $("#pref").checked });
  $("#savedev").onclick = () => api(`/devices/${encodeURIComponent(d.device_id)}`, { method: "PATCH", json: { name: $("#dname").value, room_id: $("#droom").value || null } }).then(() => toast("Đã lưu")).catch((e) => toast(e.message, true));
  $("#reboot").onclick = () => confirm("Khởi động lại thiết bị?") && api(`/devices/${encodeURIComponent(d.device_id)}/command`, { method: "POST", json: { cmd: "reboot" } }).then(() => toast("Đã gửi lệnh"));
  $("#reinit").onclick = () => api(`/devices/${encodeURIComponent(d.device_id)}/command`, { method: "POST", json: { cmd: "radar_reinit" } }).then(() => toast("Đã gửi lệnh"));
}

// ------------------------------------------------------------------ điều hướng
async function render() {
  clearTimers();
  const main = $("#main");
  const parts = (location.hash || "#/").slice(2).split("/");
  const tab = parts[0] || "overview";
  document.querySelectorAll("#nav a").forEach((a) => a.classList.toggle("active", a.dataset.tab === (tab === "" ? "overview" : tab)));
  try {
    if (tab === "collect") await renderCollect(main);
    else if (tab === "sessions") await renderSessions(main, parts[1] ? decodeURIComponent(parts[1]) : null);
    else if (tab === "baseline") await renderBaseline(main);
    else if (tab === "train") await renderTrain(main);
    else if (tab === "models") await renderModels(main);
    else if (tab === "settings") await renderSettings(main);
    else await renderOverview(main);
  } catch (e) { main.innerHTML = `<div class="notice bad">Lỗi: ${esc(e.message)}</div>`; }
}
async function health() {
  try {
    const h = await api("/health");
    $("#conn").textContent = h.mqtt_enabled ? (h.mqtt_connected ? "MQTT: đã kết nối" : "MQTT: mất kết nối") : "MQTT: tắt";
    $("#conn").className = "pill " + (h.mqtt_connected ? "ok" : "bad");
  } catch { $("#conn").textContent = "Backend: lỗi"; $("#conn").className = "pill bad"; }
}
window.addEventListener("hashchange", render);
setInterval(health, 5000); health();
render();
