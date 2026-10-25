// Biểu đồ canvas đơn giản (không thư viện ngoài): cột năng lượng theo gate, heatmap theo thời gian,
// đường điểm + dải trạng thái, dòng thời gian nhãn người xác nhận / dự đoán thiết bị.
"use strict";

const Charts = (() => {
  const CLASS_COLORS = ["#74c0fc", "#ffd43b", "#ff8787", "#da77f2"];
  const STATE_COLORS = { 0: "#adb5bd", 1: "#2b8a3e", 2: "#d9480f" }; // chưa chắc, trống, có người

  function setup(canvas, height) {
    const dpr = window.devicePixelRatio || 1;
    const w = canvas.clientWidth || 600;
    canvas.style.height = height + "px";
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(height * dpr);
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, height);
    ctx.font = "11px system-ui, sans-serif";
    return { ctx, w, h: height };
  }

  function heatColor(v) {
    // 0 trắng -> 100 đỏ sẫm
    const t = Math.max(0, Math.min(1, v / 100));
    const r = Math.round(255 - 40 * t), g = Math.round(255 - 215 * t), b = Math.round(255 - 225 * t);
    return `rgb(${r},${g},${b})`;
  }

  // values: [m0..m8, s0..s8]; thr: {moving:[9], still:[9]} (100 = tắt); base: {p99:[18]} tùy chọn
  function gateBars(canvas, values, thr, base) {
    const { ctx, w, h } = setup(canvas, 190);
    const top = 14, bottom = h - 20, left = 28;
    const groupW = (w - left - 8) / 9;
    ctx.fillStyle = "#666";
    for (let y = 0; y <= 100; y += 25) {
      const py = bottom - (bottom - top) * y / 100;
      ctx.fillText(String(y), 2, py + 3);
      ctx.strokeStyle = "#eee"; ctx.beginPath(); ctx.moveTo(left, py); ctx.lineTo(w, py); ctx.stroke();
    }
    for (let g = 0; g < 9; g++) {
      const x0 = left + g * groupW + 3, bw = (groupW - 8) / 2;
      [[values ? values[g] : 0, "#4263eb", thr && thr.moving[g], base && base.p99 && base.p99[g]],
       [values ? values[9 + g] : 0, "#f08c00", thr && thr.still[g], base && base.p99 && base.p99[9 + g]]]
        .forEach(([v, color, t, p99], k) => {
          const x = x0 + k * (bw + 2);
          const hh = (bottom - top) * Math.min(100, v || 0) / 100;
          ctx.fillStyle = color; ctx.fillRect(x, bottom - hh, bw, hh);
          if (t !== undefined && t !== null && t < 100) {
            const py = bottom - (bottom - top) * t / 100;
            ctx.strokeStyle = "#c92a2a"; ctx.lineWidth = 2; ctx.beginPath(); ctx.moveTo(x - 1, py); ctx.lineTo(x + bw + 1, py); ctx.stroke();
          }
          if (p99 !== undefined && p99 !== null) {
            const py = bottom - (bottom - top) * p99 / 100;
            ctx.strokeStyle = "#212529"; ctx.setLineDash([3, 2]); ctx.lineWidth = 1;
            ctx.beginPath(); ctx.moveTo(x - 1, py); ctx.lineTo(x + bw + 1, py); ctx.stroke(); ctx.setLineDash([]);
          }
        });
      ctx.fillStyle = "#333"; ctx.fillText("g" + g, x0 + groupW / 2 - 8, h - 6);
    }
    ctx.lineWidth = 1;
  }

  // rows: mảng mẫu {energies:[18]} theo thời gian; kind: 0 chuyển động, 1 tĩnh
  function heatmap(canvas, rows, kind, label) {
    const { ctx, w, h } = setup(canvas, 120);
    const left = 24, top = 12, H = h - top - 4;
    ctx.fillStyle = "#333"; ctx.fillText(label, left, 10);
    const n = rows.length;
    if (!n) return;
    const cw = (w - left) / n, ch = H / 9;
    for (let i = 0; i < n; i++) {
      const e = rows[i];
      if (!e) continue;
      for (let g = 0; g < 9; g++) {
        ctx.fillStyle = heatColor(e[kind * 9 + g]);
        ctx.fillRect(left + i * cw, top + (8 - g) * ch, Math.ceil(cw), Math.ceil(ch));
      }
    }
    ctx.fillStyle = "#666";
    for (let g = 0; g < 9; g += 2) ctx.fillText("g" + g, 2, top + (8 - g) * ch + ch * 0.7);
  }

  // points: [{t, score|null, state, decision}], params {enter, exit}
  function scoreLine(canvas, points, params) {
    const { ctx, w, h } = setup(canvas, 130);
    const left = 28, top = 8, bottom = h - 22;
    if (!points.length) return;
    const t0 = points[0].t, t1 = points[points.length - 1].t || t0 + 1;
    const X = (t) => left + (w - left - 4) * (t - t0) / Math.max(1, t1 - t0);
    const Y = (v) => bottom - (bottom - top) * v;
    for (let i = 0; i < points.length; i++) {
      const p = points[i], q = points[i + 1];
      ctx.fillStyle = STATE_COLORS[p.state] || "#ccc";
      ctx.fillRect(X(p.t), h - 16, Math.max(1, (q ? X(q.t) : w) - X(p.t)), 12);
    }
    ctx.fillStyle = "#666"; ctx.fillText("1", 4, Y(1) + 4); ctx.fillText("0", 4, Y(0) + 4);
    if (params) {
      [["enter", "#c92a2a"], ["exit", "#2b8a3e"]].forEach(([k, c]) => {
        ctx.strokeStyle = c; ctx.setLineDash([4, 3]); ctx.beginPath();
        ctx.moveTo(left, Y(params[k])); ctx.lineTo(w, Y(params[k])); ctx.stroke(); ctx.setLineDash([]);
      });
    }
    ctx.strokeStyle = "#1c7ed6"; ctx.lineWidth = 1.5; ctx.beginPath();
    let pen = false;
    for (const p of points) {
      if (p.score === null || p.score === undefined) { pen = false; continue; }
      if (!pen) { ctx.moveTo(X(p.t), Y(p.score)); pen = true; } else ctx.lineTo(X(p.t), Y(p.score));
    }
    ctx.stroke(); ctx.lineWidth = 1;
  }

  // Dòng thời gian một phiên: heatmap (mỗi cột = một bước), nhãn người xác nhận, quyết định thiết bị
  function sessionTimeline(canvas, data, labels, sel) {
    const { ctx, w, h } = setup(canvas, 300);
    const left = 70, right = 6, W = w - left - right;
    const rows = data.rows, n = rows.length;
    if (!n) { ctx.fillText("Không có mẫu", left, 20); return null; }
    const c = data.columns, iTs = c.indexOf("ts_ms"), iM = c.indexOf("m0"), iD = c.indexOf("p_decision");
    const t0 = rows[0][iTs], t1 = rows[n - 1][iTs];
    const X = (t) => left + W * (t - t0) / Math.max(1, t1 - t0);
    const bands = [["Chuyển động", 0, 10, 90], ["Tĩnh", 9, 104, 90]];
    const step = Math.max(1, Math.floor(n / W));
    for (const [name, off, y0, hh] of bands) {
      ctx.fillStyle = "#333"; ctx.fillText(name, 2, y0 + 12);
      for (let i = 0; i < n; i += step) {
        const x = X(rows[i][iTs]), x2 = i + step < n ? X(rows[i + step][iTs]) : left + W;
        for (let g = 0; g < 9; g++) {
          ctx.fillStyle = heatColor(rows[i][iM + off + g]);
          ctx.fillRect(x, y0 + (8 - g) * hh / 9, Math.max(1, x2 - x), hh / 9 + 0.5);
        }
      }
    }
    // Nhãn người xác nhận
    ctx.fillStyle = "#333"; ctx.fillText("Nhãn (người)", 2, 212);
    for (const lb of labels) {
      const k = ["empty_quiet", "empty_disturbance", "occupied_still", "occupied_moving"].indexOf(lb.label);
      const a = X(Math.max(t0, lb.start_ms)), b = X(Math.min(t1, lb.end_ms || t1));
      ctx.fillStyle = CLASS_COLORS[k] || "#ccc"; ctx.fillRect(a, 200, Math.max(1, b - a), 18);
    }
    // Quyết định của thiết bị (dự đoán, không phải nhãn)
    ctx.fillStyle = "#333"; ctx.fillText("Thiết bị", 2, 244);
    if (iD >= 0) {
      for (let i = 0; i < n; i += step) {
        const x = X(rows[i][iTs]), x2 = i + step < n ? X(rows[i + step][iTs]) : left + W;
        ctx.fillStyle = STATE_COLORS[rows[i][iD]] || "#ccc";
        ctx.fillRect(x, 232, Math.max(1, x2 - x), 14);
      }
    }
    // Trục thời gian
    ctx.fillStyle = "#666";
    for (let k = 0; k <= 4; k++) {
      const t = t0 + (t1 - t0) * k / 4;
      ctx.fillText(new Date(t).toLocaleTimeString("vi-VN"), X(t) - (k === 4 ? 50 : 0), 268);
    }
    if (sel && sel.a !== null && sel.b !== null) {
      ctx.fillStyle = "rgba(47,111,222,0.18)";
      const a = Math.min(sel.a, sel.b), b = Math.max(sel.a, sel.b);
      ctx.fillRect(X(a), 6, Math.max(2, X(b) - X(a)), 246);
    }
    return { t0, t1, left, W, toTime: (px) => t0 + (t1 - t0) * Math.max(0, Math.min(1, (px - left) / W)) };
  }

  return { gateBars, heatmap, scoreLine, sessionTimeline, CLASS_COLORS, STATE_COLORS };
})();
