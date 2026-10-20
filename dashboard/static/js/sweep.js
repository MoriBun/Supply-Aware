// "Tìm θ*": launch a theta sweep (the experiment of spec D2) and watch N(π_θ) build up seed by seed.
import { html, useState, useEffect, useMemo, api, useApi, useLocalStorage, cssVar, fmtInt, fmtNum, fmtVND, fmtPct,
  fmtSigned, fmtDur } from "./lib.js";
import { LineChart, BarChart, StatTile, Table } from "./charts.js";
import { isRunning, S, StatusPill, STATUS_VI, gridIndex, parseNumbers, WORLDS, worldLayers } from "./common.js";

const SWEEP_PRESETS = [
  { key: "fast", label: "Nhanh (demo): 6 θ × 2 seed, cửa sổ 4 giờ", grid: "0, 0.25, 0.5, 1, 2, 5", n_seeds: 2, window_min: 240 },
  { key: "std", label: "Chuẩn: lưới config × 10 seed, 1 ngày", grid: null, n_seeds: 10, window_min: "" },
  { key: "ref", label: "Tham chiếu S10: 16 θ × 30 seed, 1 ngày (sweep_reference.yaml)", grid: "0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.8, 1, 1.25, 1.5, 2, 3, 5, 10, 30", n_seeds: 30, window_min: "" },
];

function initialForm(cfg, cpu) {
  const th = cfg.policy.threshold;
  return {
    name: "", grid: cfg.sweep.theta_grid.join(", "), n_seeds: 3, n_procs: cpu || 1,
    scope: th.scope, forecast: th.forecast, hysteresis: th.hysteresis_h, score_fn: th.score_fn,
    enforce: cfg.budget.enforce, fraction: cfg.budget.fraction, pct: cfg.voucher.pct_of_fare,
    fleet: cfg.supply.fleet_size, demand_scale: cfg.demand.demand_scale, radius: cfg.space.grid_radius,
    window_min: cfg.time.window_min ?? "", warmup: cfg.time.warmup_min, seed: cfg.meta.run_seed, extra: "", world: "w0",
  };
}

function buildRequest(f) {
  const o = ["policy.name=threshold", `policy.threshold.scope=${f.scope}`, `policy.threshold.forecast=${f.forecast}`,
    `policy.threshold.hysteresis_h=${f.hysteresis}`, `policy.threshold.score_fn=${f.score_fn}`, "policy.threshold.kappa=auto",
    `budget.enforce=${f.enforce}`, `budget.fraction=${f.fraction}`, `voucher.pct_of_fare=${f.pct}`, `supply.fleet_size=${f.fleet}`,
    `demand.demand_scale=${f.demand_scale}`, `space.grid_radius=${f.radius}`, `time.warmup_min=${f.warmup}`, `meta.run_seed=${f.seed}`,
    `time.window_min=${f.window_min === "" || f.window_min == null ? "null" : f.window_min}`];
  for (const line of (f.extra || "").split("\n")) { const l = line.trim(); if (l && !l.startsWith("#")) o.push(l); }
  const grid = parseNumbers(f.grid);
  const name = f.name || `quét θ · ${f.scope} · ${f.score_fn} · ${f.n_seeds} seed`;
  return { name, overrides: o, layers: worldLayers(f.world), theta_grid: grid, n_seeds: Number(f.n_seeds), n_procs: Number(f.n_procs) || null };
}

function estimateSeconds(f) {
  const grid = parseNumbers(f.grid);
  const minutes = (f.window_min === "" || f.window_min == null ? 1440 : Number(f.window_min)) + Number(f.warmup || 0);
  const perRun = 16 * (minutes / 1500) * (Number(f.fleet) / 1150);   // measured: 15–16 s per day at Hanoi scale (W0)
  const procs = Math.max(1, Number(f.n_procs) || 1);
  const jobs = grid.length * Number(f.n_seeds);
  const sim = Math.ceil(jobs / procs) * perRun;
  const kappa = Math.ceil(grid.length / procs) * perRun * 3;
  return { jobs, total: perRun + kappa + sim };
}

const Field = ({ label, children, hint }) => html`<label className="field"><span>${label}${hint ? html` <span className="muted">· ${hint}</span>` : null}</span>${children}</label>`;

export function SweepPage({ defaults, runs, run, onStarted, onSelect }) {
  const [form, setForm] = useState(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  const [marks, setMarks] = useLocalStorage("sweep.marks", "2.3");
  useEffect(() => { if (defaults && !form) setForm(initialForm(defaults.config, defaults.cpu_count)); }, [defaults, form]);
  const active = run && run.kind === "sweep" ? run
    : (runs || []).find((r) => r.kind === "sweep" && isRunning(r)) || (runs || []).find((r) => r.kind === "sweep") || null;
  const sweepQ = useApi(active ? `/api/runs/${active.id}/sweep` : null, [active && active.status, active && active.sweep && active.sweep.n_rows],
    { poll: active && isRunning(active) ? 2500 : 0 });
  if (!defaults || !form) return html`<div className="card"><div className="empty">Đang nạp config mặc định…</div></div>`;
  const set = (k) => (e) => { const v = e && e.target ? (e.target.type === "checkbox" ? e.target.checked : e.target.value) : e; setForm((f) => ({ ...f, [k]: v })); };
  const num = (k) => (e) => setForm((f) => ({ ...f, [k]: e.target.value === "" ? "" : Number(e.target.value) }));
  const preset = (p) => setForm((f) => ({ ...f, grid: p.grid == null ? defaults.config.sweep.theta_grid.join(", ") : p.grid, n_seeds: p.n_seeds, window_min: p.window_min }));
  const start = async () => {
    setBusy(true); setErr(null);
    try { const r = await api("/api/sweeps", { method: "POST", body: buildRequest(form) }); onStarted(r.id); }
    catch (e) { setErr(String(e.message)); } finally { setBusy(false); }
  };
  const est = estimateSeconds(form);
  const sweeps = (runs || []).filter((r) => r.kind === "sweep");
  return html`<div className="stack">
    <div className="card">
      <div className="card-head"><h2>θ* không phải tham số đặt tay: simulator quét θ rồi chọn argmax N(π<sub>θ</sub>)</h2><span className="hint">spec §0 (3), D2; T-31</span></div>
      <div className="grid cols-3">
        <div className="note"><b>Một ngưỡng θ cho cả hệ:</b> ŝ của mỗi ô mỗi slot ${defaults.config.time.slot_min} phút khác nhau, nên cùng θ cho ra bản đồ tắt/bật khác nhau theo giờ và theo vùng. Không đặt θ riêng cho từng (ô, slot).</div>
        <div className="note"><b>Cách tìm θ*:</b> chạy π<sub>θ</sub> với mọi θ trong lưới, cùng B (một pilot), κ auto riêng cho từng θ, cùng seed; θ* = θ có N trung bình cao nhất. Vì đường thường phẳng quanh đỉnh, báo <b>tập θ*</b>: các θ không khác biệt thống kê với θ tốt nhất (so sánh ghép cặp theo seed, Bonferroni).</div>
        <div className="note"><b>Trang này chạy đúng việc đó</b> rồi vẽ đường N(θ) lớn dần theo từng seed xong. Kết quả cũng ghi <code>results/theta_sweep.parquet</code> như lệnh <code>sweep_theta</code>, nên xuất hiện ở trang Kết quả. Có thể đánh dấu θ̂ ước lượng từ dữ liệu quan sát (S9, T-45: θ̂_B = 2,3 với <code>ring1</code>, 1,6 với <code>city</code> trên W0; 2,15 / 2,1 trên W1) để so với θ*.</div>
      </div>
    </div>
    <div className="grid cols-2 sweep-layout">
      <div className="stack">
        <div className="card">
          <div className="card-head"><h2>Cấu hình quét θ</h2></div>
          <div className="row" style=${{ marginBottom: 10 }}>${SWEEP_PRESETS.map((p) => html`<button key=${p.key} className="btn sm" onClick=${() => preset(p)}>${p.label}</button>`)}</div>
          <div className="stack">
            <${Field} label="Tên"><input type="text" value=${form.name} onChange=${set("name")} placeholder="ví dụ: quét θ ring1 DR/đ" /><//>
            <${Field} label="Lưới θ" hint="phân cách bằng dấu phẩy"><input type="text" value=${form.grid} onChange=${set("grid")} /><//>
            <div className="form-grid">
              <${Field} label="Seed mỗi θ" hint="báo cáo: 10; B7b: 30"><input type="number" min="1" max="100" value=${form.n_seeds} onChange=${num("n_seeds")} /><//>
              <${Field} label="Tiến trình song song" hint=${`máy có ${defaults.cpu_count} lõi`}><input type="number" min="1" max="64" value=${form.n_procs} onChange=${num("n_procs")} /><//>
              <${Field} label="Thế giới" hint="lớp config"><select value=${form.world} onChange=${(e) => { const w = e.target.value; setForm((f) => ({ ...f, world: w, fleet: w === "w1" ? 1278 : w === "tiny" ? 10 : 1150, radius: w === "tiny" ? 1 : 4 })); }}>${WORLDS.map(([k, label]) => html`<option key=${k} value=${k}>${label}</option>`)}</select><//>
              <${Field} label="Phạm vi đo ŝ"><select value=${form.scope} onChange=${set("scope")}><option value="cell">cell</option><option value="ring1">ring1 (ô + 6 ô kề)</option><option value="city">city (cả thành phố)</option></select><//>
              <${Field} label="Dự báo ŝ"><select value=${form.forecast} onChange=${set("forecast")}><option value="persistence">persistence</option><option value="ar">ar</option></select><//>
              <${Field} label="Trễ bật lại h"><input type="number" step="0.05" min="0" value=${form.hysteresis} onChange=${num("hysteresis")} /><//>
              <${Field} label="Hàm điểm rider τ̂"><select value=${form.score_fn} onChange=${set("score_fn")}>${defaults.score_functions.map((s) => html`<option key=${s} value=${s}>${s}</option>`)}</select><//>
            </div>
            <fieldset><legend>Ngân sách, thị trường, thời gian (chung cho mọi θ)</legend><div className="form-grid">
              <label className="check"><input type="checkbox" checked=${form.enforce} onChange=${set("enforce")} /> Áp ngân sách B</label>
              <${Field} label="fraction (B = fraction × chi all_on)"><input type="number" step="0.05" min="0" value=${form.fraction} onChange=${num("fraction")} disabled=${!form.enforce} /><//>
              <${Field} label="Voucher = % cước"><input type="number" step="0.05" min="0" max="1" value=${form.pct} onChange=${num("pct")} /><//>
              <${Field} label="Đội xe"><input type="number" step="10" min="1" value=${form.fleet} onChange=${num("fleet")} /><//>
              <${Field} label="Hệ số cầu"><input type="number" step="0.05" min="0.05" value=${form.demand_scale} onChange=${num("demand_scale")} /><//>
              <${Field} label="Bán kính lưới"><input type="number" step="1" min="1" max="6" value=${form.radius} onChange=${num("radius")} /><//>
              <${Field} label="Cửa sổ (phút)" hint="trống = 1 ngày"><input type="number" step="15" min="15" value=${form.window_min} onChange=${set("window_min")} /><//>
              <${Field} label="Warm-up (phút)"><input type="number" step="15" min="0" value=${form.warmup} onChange=${num("warmup")} /><//>
              <${Field} label="run_seed đầu"><input type="number" step="1" value=${form.seed} onChange=${num("seed")} /><//>
            </div></fieldset>
            <${Field} label="Override thêm (key=value mỗi dòng)"><textarea value=${form.extra} onChange=${set("extra")}></textarea><//>
            <div className="row">
              <button className="btn primary" onClick=${start} disabled=${busy || !parseNumbers(form.grid).length}>▶ Quét θ</button>
              <span className="small ink2">${est.jobs} lượt mô phỏng · ước tính ≈ ${fmtDur(est.total)} (gồm pilot B và κ auto)</span>
              ${err ? html`<span className="err small">${err}</span>` : null}
            </div>
          </div>
        </div>
        <div className="card">
          <div className="card-head"><h2>Các lượt quét θ</h2></div>
          ${sweeps.length ? html`<${Table} rowKey=${(r) => r.id} selectedKey=${active && active.id} onRow=${(r) => onSelect(r.id)} rows=${sweeps} columns=${[
            { key: "name", label: "Tên" }, { key: "status", label: "Trạng thái", fmt: (v) => html`<${StatusPill} status=${v} />` },
            { key: "sweep", label: "Tiến độ", num: true, fmt: (v) => (v ? `${v.n_rows} / ${v.total}` : "–") },
            { key: "sweep_summary", label: "θ*", num: true, fmt: (v) => (v && v.argmax_theta != null ? `${v.argmax_theta}${v.star_lo != null ? ` [${v.star_lo}; ${v.star_hi}]` : ""}` : "–") },
            { key: "sweep_summary", label: "N(θ*)", num: true, fmt: (v) => (v && v.N_best != null ? fmtNum(v.N_best, 1) : "–") }]} />` : html`<div className="empty">chưa có lượt quét nào</div>`}
        </div>
      </div>
      <div className="stack">
        ${active ? html`<${SweepProgress} run=${active} />` : null}
        ${active && sweepQ.data ? html`<${SweepResult} run=${active} data=${sweepQ.data} marksText=${marks} onMarks=${setMarks} />`
          : html`<div className="card"><div className="empty">${active ? "Đang tải…" : "Chưa có lượt quét nào. Chọn preset \"Nhanh (demo)\" rồi bấm Quét θ."}</div></div>`}
      </div>
    </div>
  </div>`;
}

export function SweepProgress({ run }) {
  const indeterminate = ["loading", "budget", "kappa", "writing", "queued"].includes(run.status);
  const pct = run.status === "simulating" && run.sweep ? 100 * run.sweep.n_rows / Math.max(1, run.sweep.total) : (run.status === "done" ? 100 : 0);
  return html`<div className="card">
    <div className="card-head"><h2>${run.name} <${StatusPill} status=${run.status} /></h2>${run.stage_elapsed_s != null ? html`<span className="hint">giai đoạn này ${fmtDur(run.stage_elapsed_s)}</span>` : null}</div>
    <div className=${"progress" + (indeterminate ? " indeterminate" : "")}><div style=${{ width: `${pct}%` }}></div></div>
    <p className="ink2" style=${{ marginTop: 8 }}>${run.message}</p>
    ${run.error ? html`<p className="err">${run.error}</p>` : null}
    ${run.status !== "done" ? html`<div className="log">${(run.log || []).slice(-6).join("\n")}</div>` : null}
  </div>`;
}

/** Charts and tables of a sweep payload (/api/runs/{id}/sweep); also used by the overview. */
export function SweepResult({ run, data, marksText = "", onMarks = null }) {
  const agg = data.aggregate || {};
  const grid = data.theta_grid || [];
  const per = agg.per_theta || [];
  const x = grid.map((_, i) => i);
  const xLabels = grid.map((t) => String(t));
  const star = agg.star;
  const best = per.find((p) => p.theta === agg.argmax_theta);
  const marks = useMemo(() => parseNumbers(marksText), [marksText]);
  const vlines = marks.map((t, i) => ({ x: gridIndex(grid, t), label: `θ̂ = ${t}`, color: S(2 + (i % 3)) }));
  const bands = star && star.lo != null ? [{ x0: gridIndex(grid, star.lo), x1: gridIndex(grid, star.hi), color: "rgba(42, 120, 214, 0.10)" }] : [];
  const nSeries = { name: "N(π_θ): chuyến hoàn thành (trung bình theo seed)", color: S(1), y: per.map((p) => p.N_mean ?? null), dots: true, format: (v) => fmtNum(v, 1),
    band: { lo: per.map((p) => (p.N_mean == null ? null : p.N_mean - (p.N_se || 0))), hi: per.map((p) => (p.N_mean == null ? null : p.N_mean + (p.N_se || 0))) } };
  const argMark = best && best.N_mean != null ? [{ x: grid.indexOf(agg.argmax_theta), y: best.N_mean, color: S(1), label: `θ* = ${agg.argmax_theta}` }] : [];
  const gain = agg.gain_vs_zero;
  const done = run.status === "done";
  return html`<div className="stack">
    <div className="grid cols-4">
      <${StatTile} hero label="θ* = argmax N(π_θ)" value=${agg.argmax_theta == null ? "–" : String(agg.argmax_theta)} sub=${done ? `${data.n_seeds} seed × ${grid.length} θ · ŝ theo ${data.scope} · ${data.score_fn}` : `tạm tính trên ${agg.n_rows || 0} / ${data.total || "?"} lượt`} />
      <${StatTile} label="Tập θ* (không khác θ tốt nhất)" value=${star ? `[${star.lo}; ${star.hi}]` : "–"} sub=${star ? `${star.rows.filter((r) => r.in_set).length} / ${grid.length} θ trong tập · ${star.n_seeds} seed ghép cặp · α = ${star.alpha}` : "cần ≥ 2 seed đủ mọi θ"} />
      <${StatTile} label="N tại θ*" value=${best && best.N_mean != null ? fmtNum(best.N_mean, 1) : "–"} sub=${best && best.N_se != null ? `± ${fmtNum(best.N_se, 1)} SE · chi ${fmtVND(best.spent_mean, 0)} · ${fmtPct(best.share_off_mean)} (ô, slot) tắt` : ""} />
      <${StatTile} label="Lợi ích của tầng ô: N(θ*) − N(θ = 0)" value=${gain ? fmtSigned(gain.dN, 1) : (agg.argmax_theta === 0 ? "0" : "–")} sub=${gain ? `± ${fmtNum(gain.dN_se, 1)} SE, ghép cặp ${gain.n_seeds} seed · B = ${fmtVND(data.budget_vnd, 0)}/kỳ` : (data.budget_vnd ? `B = ${fmtVND(data.budget_vnd, 0)}/kỳ` : "")} />
    </div>
    <div className="card">
      <div className="card-head"><h2>Đường N(π<sub>θ</sub>) theo θ</h2><span className="hint">dải = ± 1 SE theo seed · vùng xanh = tập θ* · trục θ cách đều theo lưới</span></div>
      <${LineChart} x=${x} xLabels=${xLabels} height=${300} series=${[nSeries]} marks=${argMark} vlines=${vlines} bands=${bands} yFormat=${(v) => fmtInt(v)} yLabel="chuyến / cửa sổ" legend=${false} />
      ${onMarks ? html`<div className="row small" style=${{ marginTop: 6 }}><span className="ink2">Đánh dấu θ̂ ước lượng từ dữ liệu quan sát (S9, T-45: θ̂_B ring1 2,3 / city 1,6 trên W0; 2,15 / 2,1 trên W1) để so với θ* của simulator:</span><input type="text" value=${marksText} onChange=${(e) => onMarks(e.target.value)} style=${{ width: 160 }} placeholder="2.3, 1.6" /></div>` : null}
    </div>
    <div className="grid cols-2">
      <div className="card">
        <div className="card-head"><h2>Tỷ lệ vùng tắt voucher theo θ</h2><span className="hint">tỷ lệ (ô, slot) tắt trong cửa sổ</span></div>
        <${BarChart} height=${200} categories=${xLabels} yFormat=${(v) => fmtPct(v, 0)} legend=${false}
          series=${[{ name: "% (ô, slot) bị cắt", color: S(7), values: per.map((p) => p.share_off_mean || 0), format: (v) => fmtPct(v, 1) }]} />
      </div>
      <div className="card">
        <div className="card-head"><h2>Chi voucher theo θ</h2><span className="hint">cùng B; cắt nhiều thì chi không hết</span></div>
        <${LineChart} x=${x} xLabels=${xLabels} height=${200} legend=${false} yFormat=${(v) => fmtVND(v, 0)}
          hlines=${data.budget_vnd ? [{ y: data.budget_vnd * (run.clock ? run.clock.n_periods : 1), label: "B", color: S(8) }] : []}
          series=${[{ name: "chi voucher trung bình", color: S(3), y: per.map((p) => p.spent_mean ?? null), dots: true, format: (v) => fmtVND(v, 0) }]} />
      </div>
    </div>
    <div className="card">
      <div className="card-head"><h2>Bảng theo θ</h2><span className="hint">κ auto riêng từng θ (H-21)</span></div>
      <${Table} rows=${per.map((p) => ({ ...p, in_set: star ? (star.rows.find((r) => r.theta === p.theta) || {}).in_set : null, kappa_n: (data.kappas || []).find((k) => k.theta === p.theta) }))} columns=${[
        { key: "theta", label: "θ", num: true }, { key: "n", label: "seed xong", num: true },
        { key: "N_mean", label: "N ± SE", num: true, fmt: (v, r) => (v == null ? "–" : `${fmtNum(v, 1)}${r.N_se != null ? ` ± ${fmtNum(r.N_se, 1)}` : ""}`) },
        { key: "in_set", label: "tập θ*", fmt: (v) => (v == null ? "–" : v ? "✓" : "") },
        { key: "V_mean", label: "V (đ)", num: true, fmt: (v) => (v == null ? "–" : fmtNum(v, 0)) },
        { key: "spent_mean", label: "Chi (đ)", num: true, fmt: (v) => (v == null ? "–" : fmtNum(v, 0)) },
        { key: "share_off_mean", label: "% ô tắt", num: true, fmt: (v) => (v == null ? "–" : fmtPct(v)) },
        { key: "eta_mean", label: "ETA (ph)", num: true, fmt: (v) => (v == null ? "–" : fmtNum(v, 2)) },
        { key: "kappa_n", label: "κ (pilot)", num: true, fmt: (v) => (v ? `${v.kappa == null ? "−∞" : fmtNum(v.kappa, 3)} (${v.n_pilots})` : "–") }]} />
      ${done && agg.argmax_theta != null ? html`<div className="row" style=${{ marginTop: 10 }}>
        <a className="btn sm primary" href=${`#/run?theta=${agg.argmax_theta}&scope=${encodeURIComponent(data.scope || "")}&score_fn=${encodeURIComponent(data.score_fn || "")}`}>Chạy một lượt chi tiết tại θ* để xem bản đồ động</a>
        <span className="small muted">config ${data.config_hash}</span></div>` : null}
    </div>
  </div>`;
}

/** Overview helper: fetch and show a finished sweep. */
export function SweepResultLoader({ run }) {
  const q = useApi(run ? `/api/runs/${run.id}/sweep` : null, [run && run.status, run && run.sweep && run.sweep.n_rows], { poll: isRunning(run) ? 3000 : 0 });
  if (!q.data) return html`<div className="card"><div className="empty">Đang tải kết quả quét…</div></div>`;
  return html`<${SweepResult} run=${run} data=${q.data} />`;
}
