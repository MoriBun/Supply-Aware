// Small pieces shared by the pages (status pill, empty states, helpers) so page modules do not import each other.
import { html, cssVar, fmtNum } from "./lib.js";

export const isRunning = (r) => r && !["done", "error"].includes(r.status);
export const S = (i) => cssVar(`--s${i}`);
export const STATUS_VI = { queued: "xếp hàng", loading: "nạp config", budget: "pilot ngân sách B", kappa: "κ auto", simulating: "đang mô phỏng", writing: "ghi bảng", done: "xong", error: "lỗi" };

export function StatusPill({ status }) {
  const cls = status === "done" ? "done" : status === "error" ? "error" : "running";
  return html`<span className=${"status " + cls}>${STATUS_VI[status] || status}</span>`;
}

export function NoRun({ children }) {
  return html`<div className="card empty-state"><h2>Chưa có lượt mô phỏng để phân tích</h2><p>${children || "Chọn một lượt đã lưu ở thanh trên hoặc tạo thực nghiệm mới."}</p><a className="btn primary" href="#/run">Tạo lượt mô phỏng</a></div>`;
}

/** A sweep run opened on a page that needs frames: point to the sweep page instead. */
export function SweepOnlyNotice({ run }) {
  return html`<div className="card"><div className="empty">Lượt <b>${run.name}</b> là một lượt <b>quét θ</b> (nhiều θ × nhiều seed, chỉ ghi kết quả tổng hợp), nên không có frame để vẽ.
    Xem kết quả ở trang <a href=${"#/sweep?run=" + run.id}>Tìm θ*</a>, hoặc chạy một lượt chi tiết tại θ* ở trang <a href="#/run">Mô phỏng</a>.</div></div>`;
}

export function kappaText(run) {
  if (!run || run.policy !== "threshold") return "–";
  if (run.kappa == null) return "−∞ (không hạn chế)";
  return fmtNum(run.kappa, 3);
}

/** Contiguous true-runs of `flags` as x ranges (xs = slot start, w = slot width). */
export function bandsOf(flags, xs, w) {
  const out = [];
  let start = null;
  flags.forEach((f, i) => {
    if (f && start == null) start = i;
    if ((!f || i === flags.length - 1) && start != null) { const end = f ? i : i - 1; out.push({ x0: xs[start], x1: xs[end] + w }); start = null; }
  });
  return out;
}

/** Fractional position of `theta` on an ordinal grid axis (index space), by linear interpolation. */
export function gridIndex(grid, theta) {
  if (!grid.length) return 0;
  if (theta <= grid[0]) return 0;
  if (theta >= grid[grid.length - 1]) return grid.length - 1;
  for (let j = 0; j < grid.length - 1; j++) {
    if (theta >= grid[j] && theta <= grid[j + 1]) return j + (theta - grid[j]) / (grid[j + 1] - grid[j] || 1);
  }
  return 0;
}

export const parseNumbers = (text) => String(text || "").trim().split(/[\s,;]+/).filter(Boolean).map(Number).filter((v) => Number.isFinite(v) && v >= 0);

// Config layers of the worlds offered in the forms (W0 = config/default.yaml, W1 = + config/world_w1.yaml).
export const WORLDS = [["w0", "W0 Hà Nội (mặc định)", []], ["w1", "W1 Hà Nội + biến động (mưa, sự kiện, tài xế nghỉ)", ["config/world_w1.yaml"]],
  ["tiny", "Tiny (test, 7 ô)", ["tests/fixtures/tiny.yaml"]]];
export const worldLayers = (key) => (WORLDS.find(([k]) => k === key) || WORLDS[0])[2];
