# Dashboard quản trị simulator (`dashboard/`)

Giao diện web để **chạy, xem và giải thích** một lượt mô phỏng của simulator gọi xe: tài xế di chuyển trên lưới lục giác theo từng phút, chính sách π<sub>θ</sub> cắt voucher ở ô căng cung (ŝ < θ), tầng rider phát voucher theo điểm τ̂ ≥ κ trong ngân sách B, và các kết quả thực nghiệm đã lưu trong `runs/` (đường N(π<sub>θ</sub>), bảng chính sách dưới cùng B, throughput, GTE).

Stack: **FastAPI** (Python, cùng `.venv` với simulator) + **React 18 qua htm** (không cần Node, không cần build; thư viện vendor sẵn trong `static/vendor/`, chạy offline). Quyết định: `docs/decisions.md` T-38.

## Cài đặt và chạy

```powershell
.venv\Scripts\activate
pip install -e ".[dashboard]"          # fastapi, uvicorn, httpx
python -m dashboard                    # http://127.0.0.1:8050/  (--port, --host, --reload)
```

Lượt chạy từ dashboard ghi vào `runs/dashboard/<id>/` (không commit, như mọi `runs/`). Khi mở lại server, các lượt đã xong được nạp lại từ đĩa.

## Hai loại lượt chạy

- **Quét θ (tìm θ\*)**: việc chính của đề tài (spec §0 mục 3, D2). Ngưỡng θ không do người đặt: simulator chạy π<sub>θ</sub> với mọi θ trong lưới, cùng B (một pilot `all_on`), κ auto riêng từng θ (H-21), cùng seed, rồi θ\* = argmax<sub>θ</sub> N(π<sub>θ</sub>). Vì đường N(θ) thường phẳng quanh đỉnh, báo thêm **tập θ\***: các θ không khác biệt thống kê với θ tốt nhất (`analysis.metrics.theta_star_set`, so sánh ghép cặp theo seed, Bonferroni; T-31). "Cắt theo giờ và theo vùng" không phải là θ riêng cho từng (ô, slot): cùng một θ, nhưng ŝ của mỗi ô mỗi slot 15 phút khác nhau nên bản đồ tắt/bật khác nhau theo giờ và theo vùng (spec cố ý không rẽ nhánh θ theo từng (ô, slot)).
- **Một θ cố định**: một lượt `evaluate` một seed với `log_level = full` cộng frame từng tick, để xem bản đồ động, tầng ô, tầng rider của đúng một điểm trên đường N(θ) (ví dụ tại θ\* vừa tìm được).

## Các trang

| # | Trang | Trả lời câu hỏi | Dữ liệu |
|---|---|---|---|
| 1 | **Tổng quan** | lượt chạy cho N(π), V(π), chi/B, % (ô, slot) bị cắt bao nhiêu; cấu hình nào; chuỗi theo slot; với lượt quét: θ\*, tập θ\*, đường N(θ) | `/api/runs/{id}`, `/summary`, `/sweep` |
| 2 | **Tìm θ\* (quét θ)** | chọn lưới θ, số seed, số tiến trình song song, phạm vi ŝ, hàm điểm, B, thị trường; đường N(θ) ± SE **lớn dần theo từng seed xong**, vùng tập θ\*, chấm θ\*, đánh dấu θ̂ ước lượng từ dữ liệu quan sát (S9, T-45: θ̂_B 2,3 với `ring1`, 1,6 với `city` trên W0) để so; % (ô, slot) tắt và chi theo θ; bảng theo θ kèm κ; nút chạy một lượt chi tiết tại θ\* | `POST /api/sweeps`, `/sweep` |
| 3 | **Mô phỏng một θ** | đặt tham số (chính sách, θ, phạm vi ŝ, hàm điểm, κ, B, voucher, đội xe, cầu, cửa sổ, seed) rồi chạy; tiến độ theo giai đoạn: pilot B → κ auto → mô phỏng → ghi bảng | `POST /api/runs`, `/api/config/validate` |
| 4 | **Bản đồ động** | *animation*: xe rảnh / đi đón / chở khách / dịch chuyển, khách đang chờ, ô bật/tắt voucher (gạch chéo = bị cắt), hiệu ứng phát voucher, chặn ngân sách, ghép xe, hoàn thành, hủy; đồng hồ, tốc độ ×1…×120, thanh thời gian, theo dõi trực tiếp khi đang chạy; sổ ngân sách kỳ hiện tại; bấm ô để xem ŝ so với θ | `/frames` (từng chunk), `/slots` |
| 5 | **Vùng & ngưỡng θ** | ô nào bị cắt lúc nào: bản đồ nhiệt ô × slot (bật/tắt, ŝ dự báo, slack thực), chi tiết một ô, **cắt theo giờ trong ngày** (tỷ lệ ô tắt theo giờ), **cắt theo vùng** (bản đồ số slot tắt từng ô), nhật ký bật/tắt | `/slots`, `/summary`, hình học lưới |
| 6 | **Phân phát voucher** | ai nhận voucher: theo giờ (phát / bị chặn), phân khúc rider, cơ chế gán, phân bố điểm τ̂ và κ; ngân sách theo thời gian (chi + đặt + giữ ≤ B) | `/summary` |
| 7 | **Kết quả thực nghiệm** | đường N(π<sub>θ</sub>) ± SE của mọi sweep trong `runs/` kể cả sweep từ dashboard (chọn tối đa 4, chấm = θ\*), bảng N(π) dưới cùng B (`analysis.policy_table`, 18 dòng S10), throughput A1, GTE | `/api/results/*` |

Link thẳng một lượt: `#/map?run=<id>`, `#/sweep?run=<id>`; mở form một θ với θ điền sẵn: `#/run?theta=0.5&scope=ring1`.

## Kiến trúc

```
dashboard/
  trace.py      vòng lặp 10 bước giống sim.engine.run + ghi frame mỗi tick (xe, bộ đếm ô, sổ cái bằng đồng, sự kiện) và CellDecision/snapshot mỗi slot
  sweep.py      quét θ: cùng job với runner.sweep_theta (budget_for, resolve_kappas, seeds_for) chạy qua Pool spawn có báo tiến độ từng seed;
                tổng hợp theo θ, tập θ* (analysis.metrics.theta_star_set); ghi results/{policy_results,theta_sweep}, meta/run_metadata (mode sweep_theta)
  jobs.py       hàng đợi một luồng nền cho cả hai loại lượt: load_config → B → κ → mô phỏng → ghi bảng; nạp lại lượt đã xong từ đĩa
  summary.py    KPI, chuỗi theo slot, sự kiện bật/tắt, phân phát voucher (từ buffer + trace)
  geometry.py   tọa độ tâm ô, vector hiển thị trên torus (xe đi qua biên hiện ra ở cạnh đối diện)
  results.py    đọc results/*.parquet có sẵn trong runs/ (sweep, policy_table, throughput, gte)
  server.py     FastAPI: /api/* và static; create_app(root, runs_dir) cho test
  static/       index.html, css/app.css, js/{lib,common,charts,hexmap,sweep,pages,app}.js, vendor/ (React, ReactDOM, htm)
```

Lượt quét ghi `runs/dashboard/<id>/results/theta_sweep.parquet` đúng như `python -m sim run --mode sweep_theta`, cộng `dashboard/{meta.json, sweep.json}`. Pool tiến trình dùng `spawn` như runner; vì thế server phải chạy từ một module thật (`python -m dashboard`), không chạy từ `python -` hay REPL.

Quy tắc giữ nguyên với simulator:
- `sim/` **không** import từ `dashboard/`; dashboard chỉ dùng các factory công khai (`build_world`, `make_policy`, `runner.*`, `logger.*`).
- `trace.py` **không sửa** `engine.py` (file của Hoàng): nó chép vòng lặp và chỉ đọc trạng thái. `tests/test_dashboard.py::test_traced_run_matches_engine` kiểm hai vòng lặp cho cùng `RunResult` và cùng bảng session/order. Đổi thứ tự bước trong `engine.run` thì sửa `trace.py` theo.
- Mọi số ngẫu nhiên, ngân sách, CRN, không-nhìn-trước y như chạy bằng CLI: một lượt dashboard = `evaluate` một seed với `log_level=full`, cộng frame.
- Bảng ghi ra đúng `docs/schema.md` (`observed/`, `market/`, `hidden/`, `results/policy_results`, `meta/run_metadata`, `mode = dashboard`); thêm `dashboard/{trace.npz, slots.npz, meta.json, geometry.json, summary.json}`.

Frame (mỗi tick): `status`, `cell` (ô hiện tại hoặc ô đích khi đang đi), `origin` (ô xuất phát), `move_start`, `busy_until` của từng xe → front end nội suy vị trí giữa hai tâm ô; `idle/enroute/ontrip/waiting` theo ô; bộ đếm cộng dồn order theo trạng thái, session/offer/blocked/request; sổ cái kỳ hiện tại (cent); danh sách sự kiện `(loại, ô, ô2)`.

## API

| Method | Đường dẫn | Nội dung |
|---|---|---|
| GET | `/api/config/defaults` | `config/default.yaml`, danh sách hàm điểm, preset |
| POST | `/api/config/validate` | `{overrides:[...], layers:[...]}` → hash, số ô, cửa sổ hoặc lỗi `--set` |
| GET/POST | `/api/runs` | danh sách / xếp hàng một lượt một θ (`name, overrides, layers, budget_vnd?, kappa?`; `layers` = lớp config như `config/world_w1.yaml`) |
| POST | `/api/sweeps` | xếp hàng một lượt quét θ (`name, overrides, layers, theta_grid, n_seeds, n_procs?`) |
| GET | `/api/runs/{id}` | trạng thái, tiến độ, log, cấu hình, hình học lưới, KPI (hoặc tóm tắt θ\* với lượt quét) |
| GET | `/api/runs/{id}/sweep` | từng (θ, seed) đã xong, tổng hợp theo θ, θ\*, tập θ\*, κ từng θ (cập nhật khi đang chạy) |
| GET | `/api/runs/{id}/frames?start=&count=` | frame theo chunk (tối đa 2000) |
| GET | `/api/runs/{id}/slots` | ma trận ô × slot: `promo_on, s_hat, slack, idle_avg, …` |
| GET | `/api/runs/{id}/summary` | KPI, chuỗi theo slot, sự kiện bật/tắt, phân phát, doanh thu (`revenue`: cước gốc, voucher, khách trả, trả tài xế, lợi nhuận V; theo nhánh, theo giờ) |
| GET | `/api/runs/{id}/compare?ref=<id>` | hiệu ghép cặp với một lượt khác đã xong: ΔN, Δvoucher, ΔV, biên trước voucher, chuyến thêm / 100.000 đ, biên thêm / 1.000 đ voucher; cờ cùng seed / thế giới / B |
| GET | `/api/results/{sweeps,policy_tables,throughput,gte,evaluations}` | kết quả đã lưu trong `runs/` |

## Test

```powershell
pytest -q tests/test_dashboard.py
```

5 test: tracer trùng engine (RunResult, session, order), frame nhất quán với bộ đếm và bất biến ngân sách theo tick, vector torus, API chạy trọn một lượt tiny rồi nạp lại từ đĩa, API quét θ (3 θ × 2 seed, pool 2 tiến trình) ra đúng bảng `sweep_theta` và θ\*.

## Giới hạn và việc có thể làm tiếp

- Mô phỏng chạy trong một luồng nền của tiến trình server (GIL): trong lúc chạy, API phản hồi chậm hơn một chút; các lượt xếp hàng chạy tuần tự. W0 (1.150 xe, 61 ô): 1 ngày ≈ 16 giây mô phỏng có trace (engine 15 giây), cộng pilot B và κ auto ≈ 1–2 phút.
- Frame giữ trong bộ nhớ của server trong phiên; lượt dài nhiều ngày sẽ nặng (≈ 27 MB/ngày với 1.150 xe).
- Giao diện viết bằng htm (template literal) để không cần Node. Muốn JSX/TypeScript: tạo dự án Vite trong `dashboard/web/`, chuyển từng file trong `static/js/` (mỗi file đã là một module ES), build ra `static/`.

## Giao diện nghiên cứu (07/10/2026)

- Điều hướng theo ba nhóm: nghiên cứu, thiết kế thực nghiệm, phân tích vận hành.
- Tổng quan: N(π), tỷ lệ hoàn thành/đặt, chi voucher, ETA; phễu session → request → completed; diễn biến theo slot; tìm kiếm và lọc lượt chạy.
- Kết quả: liên kết RQ1–RQ3 với các bảng đã lưu trong `runs/`. Nhãn “Có dữ liệu” chỉ xác nhận có artifact, không xác nhận đạt tiêu chí nghiên cứu.
- Bảng có thể chọn bằng Enter hoặc Space, chế độ sáng/tối/tự động và bố cục responsive; lỗi API có nút thử lại.
- GTE chỉ dùng all_on/all_off không ngân sách, ghép seed chung; bỏ bảng có trùng (policy, seed) vì không đủ khóa xác định một đối chiếu.
- Giao diện tải lại tài nguyên bằng HTTP revalidation. Nếu đang mở bản cũ, dùng Ctrl+F5 một lần.
- Rà soát nội dung và các phần nghiên cứu cần bổ sung: `docs/dashboard_review.md`.

## Cập nhật 15/10/2026 (S10, quyết định T-49)

- Simulator quy mô Hà Nội (S7–S8): preset **W0** (61 ô bán kính 4, 1.150 xe, VND) và **W1** (`config/world_w1.yaml`: 1.278 xe, mưa, sự kiện, cầu theo ngày, tài xế nghỉ). Ô "Thế giới" trong hai form chọn lớp config (`layers` của API); biến động của W1 tác động lên động lực (tốc độ, cầu, xe nghỉ) và được ghi ở `meta/shocks` của lượt chạy, chưa vẽ riêng trên bản đồ.
- Phạm vi đo ŝ thêm `city` (T-40); hàm điểm thêm các mô hình S9 theo 1.000đ (`analysis.uplift:*_per_kvnd`, bản `_w1`) và luật tắt theo giờ (`analysis.hour_rule`). Trang Kết quả chọn sẵn hai sweep `runs/s10/<w>/sweep_ring1_dr`; θ̂ mặc định để đánh dấu là θ̂_B = 2,3 (S9).
- **Bỏ phần lưu và hiển thị ảnh thí nghiệm** (`/api/results/figures`, mount `/figures`, thư viện hình): hình nghiên cứu nằm ở `docs/figures/` và notebook 01–06, không phải việc của dashboard.
- `trace.py` vẫn trùng `engine.run` sau S7 (kiểm trên cấu hình tiny + W1 có biến động: cùng mọi trường kết quả, cùng bảng shocks).
- Trang Tổng quan thêm khối **Doanh thu & lợi nhuận** (từ `observed/orders` của chuyến hoàn thành trong cửa sổ; tổng lợi nhuận bằng đúng V(π)) và khối **So với lượt cùng seed**: lợi nhuận của việc phát voucher là hiệu ghép cặp CRN với lượt tham chiếu (`all_off`, `all_on` có B, hay chính sách khác), không phải hiệu hai nhánh trong một lượt.
