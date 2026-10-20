// Research overview. All values come from the selected experiment's published data.
import { html, useState, useMemo, useEffect, useApi, fmtInt, fmtNum, fmtVND, fmtPct, fmtSigned, fmtClock, fmtDur, clamp } from "./lib.js";
import { LineChart, StatTile, Table, Meter } from "./charts.js";
import { Icon, SectionHead } from "./ui.js";
import { StatusPill, isRunning, S, kappaText } from "./common.js";
import { SweepResultLoader } from "./sweep.js";

export function OverviewPage({ run, runs = [], defaults, loading, onSelect }) {
  const [query, setQuery] = useState("");
  const [kind, setKind] = useState("all");
  const summaryQ = useApi(run?.status === "done" && run.kind !== "sweep" ? `/api/runs/${run.id}/summary` : null);
  // useApi retains old data while loading; never show it under another experiment's name.
  const summary = summaryQ.data;
  const k = run?.kind !== "sweep" ? run?.kpis : null;
  const cfg = run?.config || defaults?.config;
  const clock = run?.clock;
  const slotMin = cfg?.time?.slot_min;
  const filtered = useMemo(() => runs.filter((r) => (!query || `${r.name} ${r.policy || ""} ${r.id}`.toLocaleLowerCase("vi").includes(query.toLocaleLowerCase("vi"))) && (kind === "all" || (kind === "sweep" ? r.kind === "sweep" : r.kind !== "sweep"))), [runs, query, kind]);
  const requestRate = k && k.n_sessions > 0 ? k.n_requests / k.n_sessions : null;
  const completeRate = k && k.n_requests > 0 ? k.N_completed / k.n_requests : null;
  const totalBudget = k?.budget_B_vnd == null ? null : k.budget_B_vnd * (clock?.n_periods || 1);
  const series = summary?.slot_series;
  const rev = k && summary?.revenue ? summary.revenue : null;
  const x = series?.t_start.map((t) => t / 3600) || [];
  const bands = clock ? [{ x0: clock.window_start_s / 3600, x1: clock.window_end_s / 3600 }] : [];
  const stages = k ? [
    { label: "Phiên truy cập", value: k.n_sessions, color: S(1) },
    { label: "Yêu cầu đặt xe", value: k.n_requests, color: S(2) },
    { label: "Chuyến hoàn thành", value: k.N_completed, color: S(3) },
  ] : [];
  const rows = filtered.map((r) => ({ ...r, type: r.kind === "sweep" ? "Quét ngưỡng" : "Mô phỏng", completed: r.kind === "sweep" ? r.sweep_summary?.N_best : r.kpis?.N_completed, spend: r.kpis?.spent_share_of_budget }));
  return html`<div className="stack overview">
    <section className="research-banner"><div><span className="eyebrow">CÂU HỎI TRỌNG TÂM</span><h2>Khuyến mãi có tạo thêm chuyến đi<br className="desktop-break" /> khi thị trường thiếu tài xế?</h2><p>Đánh giá chính sách theo chuyến hoàn thành, trạng thái cung và ngân sách voucher.</p><div className="row"><a className="btn banner-primary" href="#/sweep">Tìm ngưỡng θ* <${Icon} name="arrow" size=${16} /></a><a className="banner-link" href="#/results">Đối chiếu kết quả <${Icon} name="arrow" size=${16} /></a></div></div><div className="research-equation"><span>MỤC TIÊU CHÍNH</span><strong>max N(π<sub>θ</sub>)</strong><div>cùng ngân sách <b>B</b> · nhiều seed</div><small>θ* = argmax<sub>θ</sub> N(π<sub>θ</sub>)</small></div></section>
    ${run ? html`<section className="run-context"><div><div className="eyebrow">${run.kind === "sweep" ? "QUÉT NGƯỠNG" : "MÔ PHỎNG ĐƯỢC CHỌN"}</div><h2>${run.name} <${StatusPill} status=${run.status} /></h2><div className="run-facts"><span>Chính sách <b>${run.policy || "threshold"}</b></span>${run.kind !== "sweep" && run.theta != null ? html`<span>θ cố định <b>${fmtNum(run.theta, 2)}</b></span>` : null}<span>Cung <b>${run.scope || "–"}</b></span>${cfg?.supply ? html`<span>Đội xe <b>${fmtInt(cfg.supply.fleet_size)}</b></span>` : null}<span>Seed <b>${run.kind === "sweep" ? run.sweep?.n_seeds : cfg?.meta?.run_seed ?? "–"}</b></span></div></div><a className="btn sm" href=${run.kind === "sweep" ? "#/sweep" : "#/map"}>${run.kind === "sweep" ? "Xem lượt quét" : "Khám phá bản đồ"}<${Icon} name="arrow" size=${15} /></a></section>` : null}
    ${run?.error ? html`<div className="card err" role="alert">${run.error}</div>` : null}
    ${isRunning(run) ? html`<div className="note row" role="status"><${Icon} name="clock" /><span>${run.message || "Thực nghiệm đang chạy. KPI sẽ hiển thị khi hoàn tất."}</span><a href=${run.kind === "sweep" ? "#/sweep" : "#/run"}>Theo dõi tiến độ →</a></div>` : null}
    ${!run ? html`<div className="card empty-state"><span className="empty-icon"><${Icon} name="run" size=${25} /></span><h2>${loading ? "Đang tải thực nghiệm…" : "Bắt đầu với một lượt mô phỏng"}</h2><p>${loading ? "Đang kết nối dữ liệu và cấu hình thị trường." : "Chọn cấu hình thử nghiệm để quan sát cung, cầu và hiệu quả phân bổ voucher."}</p>${!loading ? html`<a className="btn primary" href="#/run">Tạo lượt chạy đầu tiên <${Icon} name="arrow" size=${16} /></a>` : null}</div>` : null}
    ${k ? html`<section><${SectionHead} title="Hiệu quả trong cửa sổ đánh giá" description="Chuyến tính theo thời điểm yêu cầu đặt xe; theo dõi đến khi kết thúc hoặc drain hết." />
      <div className="grid cols-4 kpi-primary">
        <${StatTile} hero label="CHUYẾN HOÀN THÀNH · N(π)" value=${fmtInt(k.N_completed)} sub=${`${fmtNum(k.completed_per_h, 1)} chuyến / giờ · Chỉ tiêu chính`} />
        <${StatTile} label="TỶ LỆ HOÀN THÀNH / ĐẶT XE" value=${fmtPct(completeRate)} sub=${`${fmtInt(k.N_completed)} hoàn thành / ${fmtInt(k.n_requests)} yêu cầu`} />
        <${StatTile} label="NGÂN SÁCH VOUCHER ĐÃ CHI" value=${fmtVND(k.voucher_spent_vnd)} sub=${totalBudget == null ? "Không áp trần ngân sách" : `${fmtPct(k.spent_share_of_budget)} / tổng B ${fmtVND(totalBudget)}`} />
        <${StatTile} label="ETA ĐÓN TRUNG BÌNH" value=${k.mean_pickup_eta_min == null ? "–" : `${fmtNum(k.mean_pickup_eta_min, 2)} phút`} sub="Thời gian đón dự kiến của các lượt ghép xe" />
      </div><div className="secondary-kpis"><div><span>Lợi nhuận V(π) · phụ</span><b>${fmtVND(k.V_profit_vnd)}</b></div><div><span>Tỷ lệ (ô, slot) tắt voucher</span><b>${fmtPct(k.share_cells_off)}</b></div><div><span>Hủy khi xe đang đến</span><b>${fmtPct(k.cancel_rate)}</b></div><div><span>Bỏ chờ trước khi ghép</span><b>${fmtPct(k.abandon_rate)}</b></div></div></section>` : null}
    ${summaryQ.error ? html`<div className="card err" role="alert">Không tải được tổng hợp: ${summaryQ.error}<button className="btn sm" onClick=${summaryQ.reload}>Thử lại</button></div>` : null}
    ${series ? html`<section className="grid overview-charts">
      <div className="card"><${SectionHead} eyebrow="DIỄN BIẾN THỊ TRƯỜNG" title="Nhu cầu & chuyến hoàn thành" description=${`Số lượng / slot${slotMin == null ? "" : ` ${fmtNum(slotMin, 0)} phút`} · dải vàng: cửa sổ đánh giá`}><a className="text-link" href="#/map">Bản đồ <${Icon} name="arrow" size=${14} /></a><//><${LineChart} x=${x} height=${260} bands=${bands} xFormat=${(v) => fmtClock(v * 3600)} yLabel="số lượng / slot" yDomain=${[0, null]} series=${[{ name: "Yêu cầu đặt xe", color: S(2), y: series.n_requests, dash: true }, { name: "Chuyến hoàn thành", color: S(1), y: series.n_completed }, { name: "Voucher được phát", color: S(3), y: series.n_offers }]} /><p className="chart-foot">Hoàn thành theo slot ghi nhận thời điểm kết thúc chuyến; N(π) theo cửa sổ đặt xe.</p></div>
      <div className="card"><${SectionHead} eyebrow="ĐIỀU TIẾT THEO CUNG" title="Vùng tắt khuyến mãi" description="Số ô tắt voucher theo từng slot"><a className="text-link" href="#/cells">Chi tiết <${Icon} name="arrow" size=${14} /></a><//><${LineChart} x=${x} height=${260} bands=${bands} xFormat=${(v) => fmtClock(v * 3600)} yLabel="số ô" yDomain=${[0, run.geometry?.cells?.length || null]} legend=${false} series=${[{ name: "Ô tắt voucher", color: S(1), y: series.cells_off }]} /><p className="chart-foot">${run.policy === "threshold" ? `Ngưỡng θ = ${fmtNum(run.theta, 2)} cố định; trạng thái bật / tắt thay đổi theo cung dự báo của từng vùng.` : "Trạng thái bật / tắt theo chính sách đang chọn."}</p></div>
    </section>` : null}
    ${k ? html`<section className="grid cols-2"><div className="card"><${SectionHead} eyebrow="PHÂN RÃ HÀNH VI" title="Từ nhu cầu đến chuyến đi" description="Số liệu mô tả trong cùng cửa sổ đánh giá" /><div className="funnel">${stages.map((stage, i) => html`<div key=${stage.label} className="funnel-row"><div><span>${stage.label}</span><b>${fmtInt(stage.value)}</b></div><div className="funnel-track"><div style=${{ width: `${100 * clamp(stage.value / Math.max(1, k.n_sessions), 0, 1)}%`, background: stage.color }}></div></div><small>${i === 0 ? "Tổng số phiên" : i === 1 ? `${fmtPct(requestRate)} phiên chuyển thành yêu cầu` : `${fmtPct(completeRate)} yêu cầu được hoàn thành`}</small></div>`)}</div><p className="chart-foot">Phễu mô tả kết quả của lượt chạy, chưa phải ước lượng uplift hay tác động nhân quả.</p></div>
      <div className="card"><${SectionHead} eyebrow="PHÂN BỔ NGUỒN LỰC" title="Chi tiêu & cấu hình chính sách"><a className="text-link" href="#/distribution">Sổ ngân sách <${Icon} name="arrow" size=${14} /></a><//><div className="budget-amount"><strong>${fmtVND(k.voucher_spent_vnd)}</strong><span>${totalBudget == null ? "Không áp trần B" : `trên ${fmtVND(totalBudget)} cho ${clock?.n_periods || 1} kỳ`}</span></div><${Meter} max=${totalBudget} format=${fmtVND} segments=${[{ label: "Đã chi voucher", value: k.voucher_spent_vnd, color: S(1) }]} /><dl className="detail-list"><div><dt>Voucher / cước</dt><dd>${fmtPct(cfg?.voucher?.pct_of_fare, 0)}</dd></div><div><dt>κ · ngưỡng điểm rider</dt><dd>${kappaText(run)}</dd></div><div><dt>Cửa sổ đánh giá</dt><dd>${fmtDur((k.window_end_s || 0) - (k.window_start_s || 0))}</dd></div><div><dt>Đơn còn dang dở khi dừng</dt><dd>${fmtInt(k.n_truncated_orders)}</dd></div></dl><p className="chart-foot">Trần B áp cho từng kỳ: đã chi + đã cam kết + đang giữ ≤ B. Tổng chi không đại diện toàn bộ tiền đang giữ.</p></div></section>` : null}
    ${rev ? html`<section className="card"><${SectionHead} eyebrow="DOANH THU & LỢI NHUẬN" title="Tiền của các chuyến hoàn thành trong cửa sổ" description="Cước gốc − voucher = khách trả; khách trả − tiền tài xế = lợi nhuận nền tảng V(π) (phí nền tảng + hoa hồng − voucher, H-42)" />
      <div className="grid cols-5">
        <${StatTile} label="Cước gốc" value=${fmtVND(rev.total.gross_fare_vnd)} sub=${`${fmtInt(rev.total.trips)} chuyến · TB ${fmtVND(rev.total.gross_per_trip_vnd)}`} />
        <${StatTile} label="Voucher đã chi" value=${fmtVND(rev.total.voucher_value_vnd)} sub=${`${fmtPct(rev.total.gross_fare_vnd > 0 ? rev.total.voucher_value_vnd / rev.total.gross_fare_vnd : null)} cước gốc`} />
        <${StatTile} label="Khách trả" value=${fmtVND(rev.total.net_fare_vnd)} sub="cước sau voucher" />
        <${StatTile} label="Trả tài xế" value=${fmtVND(rev.total.driver_pay_vnd)} sub=${`${fmtPct(cfg?.pricing?.commission_rate, 0)} hoa hồng, phí nền tảng ${fmtVND(cfg?.pricing?.platform_fee_vnd)}`} />
        <${StatTile} hero label="Lợi nhuận nền tảng V(π)" value=${fmtVND(rev.total.platform_profit_vnd)} sub=${`TB ${fmtVND(rev.total.profit_per_trip_vnd)} / chuyến · trước voucher ${fmtVND(rev.total.margin_before_voucher_vnd)}`} />
      </div>
      <div className="grid cols-2" style=${{ marginTop: 12 }}>
        <div><${Table} rows=${rev.by_arm} columns=${[{ key: "arm", label: "Chuyến", fmt: (v) => (v === "voucher" ? "có voucher" : "không voucher") }, { key: "trips", label: "Số chuyến", num: true, fmt: fmtInt }, { key: "gross_per_trip_vnd", label: "Cước gốc / chuyến", num: true, fmt: (v) => fmtVND(v) }, { key: "voucher_per_trip_vnd", label: "Voucher / chuyến", num: true, fmt: (v) => fmtVND(v) }, { key: "profit_per_trip_vnd", label: "Lợi nhuận / chuyến", num: true, fmt: (v) => fmtVND(v) }, { key: "platform_profit_vnd", label: "Lợi nhuận", num: true, fmt: (v) => fmtVND(v) }]} />
          <p className="small muted">Bảng theo nhánh là <b>mô tả</b>: chuyến có voucher do chính sách chọn, không ngẫu nhiên, và voucher của người này đổi thị trường của người khác. Lợi nhuận <i>của việc phát voucher</i> chỉ đo được bằng cách so với một lượt cùng seed ở khối bên dưới.</p></div>
        <${LineChart} x=${rev.by_hour.map((r) => r.hour)} height=${220} xFormat=${(v) => `${v}h`} yFormat=${(v) => fmtVND(v, 0)} yLabel="đ / giờ" series=${[{ name: "Cước gốc", color: S(2), y: rev.by_hour.map((r) => r.gross_fare_vnd), dash: true }, { name: "Lợi nhuận V", color: S(1), y: rev.by_hour.map((r) => r.platform_profit_vnd) }, { name: "Voucher", color: S(3), y: rev.by_hour.map((r) => r.voucher_value_vnd) }]} />
      </div>
      <${CompareCard} run=${run} runs=${runs} />
    </section>` : null}
    ${run?.kind === "sweep" ? html`<${SweepResultLoader} run=${run} />` : null}
    <section className="card"><${SectionHead} eyebrow="LỊCH SỬ THỰC NGHIỆM" title="Danh sách lượt chạy" description="Chọn một dòng để xem dữ liệu của thực nghiệm đó"><span className="chip">${filtered.length} / ${runs.length} lượt</span><//><div className="table-toolbar"><label className="search-field"><${Icon} name="search" size=${16} /><input type="search" aria-label="Tìm lượt chạy" placeholder="Tìm tên, chính sách, mã lượt chạy…" value=${query} onChange=${(e) => setQuery(e.target.value)} /></label><div className="seg" role="group" aria-label="Lọc loại thực nghiệm">${[["all", "Tất cả"], ["run", "Mô phỏng"], ["sweep", "Quét θ"]].map(([key, label]) => html`<button key=${key} aria-pressed=${kind === key} className=${kind === key ? "on" : ""} onClick=${() => setKind(key)}>${label}</button>`)}</div></div>
      ${rows.length ? html`<${Table} rows=${rows} rowKey=${(r) => r.id} selectedKey=${run?.id} onRow=${(r) => onSelect(r.id)} columns=${[{ key: "name", label: "Thực nghiệm", fmt: (v, r) => html`<b>${v}</b><div className="small muted">${r.policy || "threshold"}${r.theta != null ? ` · θ = ${r.theta}` : ""}</div>` }, { key: "type", label: "Loại" }, { key: "status", label: "Trạng thái", fmt: (v) => html`<${StatusPill} status=${v} />` }, { key: "completed", label: "N(π)", num: true, fmt: (v, r) => html`${r.kind === "sweep" ? fmtNum(v, 1) : fmtInt(v)}${r.kind === "sweep" ? html`<div className="small muted">TB tại θ* = ${r.sweep_summary?.argmax_theta ?? "–"}</div>` : null}` }, { key: "spend", label: "Chi / tổng B", num: true, fmt: (v) => fmtPct(v) }]} />` : html`<div className="empty">${runs.length ? "Không tìm thấy lượt chạy phù hợp. Thử tên khác hoặc bộ lọc Tất cả." : "Chưa có dữ liệu. Tạo lượt chạy để bắt đầu."}</div>`}</section>
    <details className="card methodology"><summary><span><${Icon} name="info" /> Phương pháp & cách đọc dashboard</span><span className="small muted">Mở để xem</span></summary><div className="grid cols-3"><div className="method-step"><span>01</span><h3>So sánh dưới cùng B</h3><p>B có thể cố định hoặc hiệu chỉnh từ pilot all_on. So sánh chính sách phải giữ cùng ngân sách, cấu hình thị trường và seed.</p></div><div className="method-step"><span>02</span><h3>Quyết định dựa trên cung trễ</h3><p>Slack = xe rảnh / xe đang đi đón. Với threshold, ŝ được dự báo từ snapshot trước; ŝ thấp báo cung căng. Phạm vi cell hoặc ring1 theo cấu hình.</p></div><div className="method-step"><span>03</span><h3>Chọn θ* qua thực nghiệm</h3><p>Quét θ × nhiều seed để tìm argmax N(πθ). θ của một lượt đơn là giá trị đặt cố định. Dải ± SE và tập θ* giúp đọc độ bất định.</p></div></div><p className="chart-foot">Kết quả chỉ áp dụng trong mô phỏng này. Không dùng Qini hoặc so sánh hai nhánh voucher đơn lẻ thay cho giá trị chính sách ở cấp thị trường.</p></details>
  </div>`;
}

// Paired comparison with another finished run of the same seed (CRN): the extra trips, voucher spend and
// profit of this policy over the reference (all_off = no voucher, all_on_B = everyone within B, ...).
function CompareCard({ run, runs }) {
  const candidates = useMemo(() => (runs || []).filter((r) => r.kind !== "sweep" && r.status === "done" && r.id !== run.id && r.has_revenue)
    .sort((a, b) => (b.seed === run.seed) - (a.seed === run.seed)), [runs, run.id, run.seed]);
  const [ref, setRef] = useState("");
  useEffect(() => { if (!candidates.some((r) => r.id === ref)) setRef(candidates[0] ? candidates[0].id : ""); }, [candidates, ref]);
  const q = useApi(ref ? `/api/runs/${run.id}/compare?ref=${encodeURIComponent(ref)}` : null, [run.id, ref]);
  const c = q.data && q.data.run && q.data.run.id === run.id ? q.data : null;
  const d = c?.delta, r = c?.ratios;
  return html`<div className="compare-card" style=${{ marginTop: 14 }}>
    <div className="row between"><div><b>So với lượt cùng seed</b> <span className="small muted">· lợi nhuận của việc phát voucher = hiệu ghép cặp (CRN), không phải hiệu hai nhánh trong một lượt</span></div>
      <label className="field" style=${{ minWidth: 320 }}><span>Lượt tham chiếu</span><select value=${ref} onChange=${(e) => setRef(e.target.value)}>${candidates.map((x) => html`<option key=${x.id} value=${x.id}>${x.name} · ${x.policy || "?"}${x.theta != null ? ` θ=${x.theta}` : ""} · seed ${x.seed ?? "?"}${x.seed === run.seed ? "" : " (khác seed)"}</option>`)}</select></label></div>
    ${!candidates.length ? html`<div className="empty">Chưa có lượt nào khác đã xong để so. Chạy thêm <code>all_off</code> (không voucher) hoặc <code>all_on</code> có B với cùng seed.</div>` : null}
    ${q.error ? html`<div className="err small">${q.error}</div>` : null}
    ${c ? html`<div className="grid cols-5" style=${{ marginTop: 10 }}>
      <${StatTile} label="Chuyến thêm ΔN" value=${fmtSigned(d.N_completed, 0)} sub=${`${fmtInt(c.run.N_completed)} so với ${fmtInt(c.ref.N_completed)}`} />
      <${StatTile} label="Voucher chi thêm" value=${fmtSigned(d.voucher_value_vnd, 0) + " đ"} />
      <${StatTile} label="Lợi nhuận thêm ΔV" value=${fmtSigned(d.platform_profit_vnd, 0) + " đ"} sub=${`trước voucher ${fmtSigned(d.margin_before_voucher_vnd, 0)} đ`} />
      <${StatTile} label="Chuyến thêm / 100.000 đ voucher thêm" value=${r.extra_trips_per_100k_vnd == null ? "–" : fmtNum(r.extra_trips_per_100k_vnd, 2)} sub=${d.voucher_value_vnd > 0 ? "" : "lượt này chi ít voucher hơn tham chiếu"} />
      <${StatTile} label="Biên thêm / 1.000 đ voucher thêm" value=${r.extra_margin_per_1k_vnd_voucher == null ? "–" : fmtVND(r.extra_margin_per_1k_vnd_voucher, 0)} sub=${r.extra_margin_per_1k_vnd_voucher == null ? "" : r.extra_margin_per_1k_vnd_voucher >= 1000 ? "≥ 1.000 đ: voucher tự trả được tiền" : "< 1.000 đ: voucher không tự trả được tiền (ΔV < 0)"} />
    </div>
    <div className="small muted" style=${{ marginTop: 6 }}>${c.same_seed ? "✓ cùng seed" : "✗ khác seed: hiệu còn nhiễu seed"} · ${c.same_world ? "✓ cùng thế giới" : "✗ khác đội xe / cầu / cửa sổ / biến động"} · ${c.same_budget ? "✓ cùng B" : "B khác nhau (tham chiếu không ngân sách hoặc B khác)"}</div>` : null}
  </div>`;
}
