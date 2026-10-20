# CLAUDE.md — Simulator gọi xe (Supply-Aware Promotion Uplift)

Đọc `README.md` trước: bài toán, mô hình, chính sách, các mode chạy, cấu hình, tái lập kết quả. Định dạng dữ liệu đầu ra và danh sách cột ẩn: `docs/schema.md`. Mọi tham số: `config/default.yaml`.

Chỉ tiêu chính là **N(π) = số chuyến hoàn thành** dưới cùng ngân sách voucher B; lợi nhuận V(π) là chỉ tiêu phụ.

## Stack và lệnh

- Python 3.11; `sim/` chỉ dùng numpy, pandas, pyarrow, pyyaml. `analysis/` thêm matplotlib, lightgbm; `dashboard/` thêm fastapi, uvicorn. Không thêm thư viện khác nếu chưa hỏi. `numba` chỉ dùng khi thật cần, và phải có fallback thuần numpy.
- Cài đặt: `py -3.11 -m venv .venv`, kích hoạt, rồi `pip install -e ".[dev]"` (thêm `analysis`, `dashboard` khi cần).
- Test nhanh: `pytest -q`. Test chậm (nghiệm thu): `pytest -q -m slow`. Trên máy này thư mục tạm mặc định của pytest bị khóa quyền: thêm `--basetemp="$LOCALAPPDATA/Temp/pytest-sim"`.
- Chạy: `python -m sim run --mode <mode> --config config/default.yaml [--config overlay.yaml] [--set a.b=c]`; thêm `--profile` để đo từng bước.

## Quy tắc cứng (không được vi phạm)

Code tham chiếu các quy tắc này theo số ("hard rule N"); giữ nguyên số thứ tự.

1. **Không hard-code tham số.** Mọi hằng số lấy từ `config/default.yaml`. Cần tham số mới thì thêm vào YAML kèm chú thích nguồn (`[data]`, `[paper]`, `[assume]`…).
2. **Không nhìn trước.** Quyết định ở slot k chỉ dùng snapshot của các slot < k, qua `SnapshotView`.
3. **Không rò biến ẩn.**
   - Chính sách và dữ liệu `observed/`, `market/` không được chứa cột nào trong **danh sách ẩn của `docs/schema.md`** (`u_latent`, alpha, beta_price, beta_eta, delta_promo, max_wait_min, propensity_true, p_request_*, direct_request_effect_fixed_market, và mọi số rút sẵn của session).
   - Riêng `LegacyPolicy` được dùng `u_latent` qua `LegacyHiddenView`; điểm `oracle` dùng hệ số thật qua `OracleHiddenView`. Các số rút sẵn `u_target`, `u_explore`, `u_explore_arm`, `u_score` được đưa vào `SessionBatch` cho chính sách dùng; chính sách không tự rút số.
4. **CRN.**
   - Mọi số ngẫu nhiên lấy qua `rng.rng_for(stream, *key)` với khóa ổn định (session_id, cell, tick, driver_id…).
   - Không dùng `np.random.*` toàn cục, không dùng `random`.
   - Đổi chính sách không được làm thay đổi số ngẫu nhiên của bất kỳ session nào.
5. **Cung độc lập với chính sách.** `supply.early_exit_enabled` mặc định false. Repositioning chỉ dùng trọng số tĩnh, không đọc cầu hay snapshot hiện tại.
6. **Không làm mất hiện tượng cần đo.** Không giảm `max_pickup_eta_min`, không giới hạn tìm xe ở vành 1, không bỏ hủy trong lúc xe đi đón để "cho nhanh" hoặc "cho đẹp".
7. **Hiệu năng.**
   - Trạng thái xe, order, session lưu dạng struct-of-arrays numpy. Không tạo object Python cho mỗi xe hay khách trong vòng lặp nóng.
   - Không lặp qua toàn bộ đội xe mỗi tick.
   - Mục tiêu ≤ 60 giây / ngày mô phỏng / một tiến trình trên cấu hình quy mô Hà Nội (kiểm A5). Mọi lệnh sinh dữ liệu và quét dùng `runner.n_procs` = số lõi.
8. **Ngân sách.** Bất biến `spent + committed + reserved ≤ B` phải luôn đúng khi `budget.enforce = true`.
9. **Không thêm tính năng ngoài phạm vi mô hình** (surge pricing, pooling, tài xế phản ứng theo thu nhập, nền tảng chủ động điều xe…). Học mô hình uplift chỉ làm trong `analysis/`, không bao giờ trong `sim/`.

## Cách làm việc

- Viết test trước hoặc cùng lúc với code; thay đổi chỉ xong khi `pytest -q` pass.
- **Khi test nghiệm thu không đạt** (đặc biệt A1, đường throughput) hoặc CAL lệch: báo số liệu thật. Không nới tiêu chí đạt, không sửa test cho pass.
- Khi yêu cầu mơ hồ hoặc mâu thuẫn với README / `docs/schema.md`: dừng lại và hỏi, không tự chọn một cách rồi đi tiếp.
- Đổi tên cột hay thêm cột thì sửa cả `docs/schema.md` (`tests/test_schema_contract.py` parse file này, nên giữ nguyên tiêu đề mục và dòng "Danh sách ẩn gồm").
- Đổi thứ tự bước trong `sim/engine.run` thì sửa `dashboard/trace.py` theo (`tests/test_dashboard.py` kiểm hai vòng lặp cho cùng kết quả).
- Hàm điểm truyền dạng `"module:function"` phải import được trong tiến trình con (`multiprocessing` kiểu `spawn`).
- `sim/` không import từ `analysis/` hay `dashboard/`. Notebook chỉ gọi hàm trong `analysis/`; phép tính mới viết vào `analysis/*.py` kèm test.
- Code comment và docstring bằng tiếng Anh; tài liệu (README, `docs/`, markdown notebook) bằng tiếng Việt.
- Tên biến khớp tên trong `docs/schema.md` và `config/default.yaml` (ví dụ `slack`, `cell_propensity`, `pickup_eta_min`).
- Nhánh: `develop` là nhánh chung, `develop1` / `develop2` là nhánh cá nhân; gộp vào `develop` qua PR, người kia review, merge commit (không squash).

## Cấu trúc thư mục

```
sim/            # simulator
analysis/       # đánh giá, ước lượng, hình; models/ = mô hình đã học (đọc trong tiến trình con)
dashboard/      # FastAPI + React (htm), không cần Node
notebooks/      # notebook kết quả, chỉ gọi analysis/
tests/          # pytest; fixtures/tiny.yaml cho test nhanh; fakes.py là đồ giả dùng chung
config/         # default.yaml (W0), world_w1.yaml, sweep_reference.yaml
docs/           # schema.md, figures/
outputs/        # báo cáo, slide
runs/           # output mô phỏng (không commit)
```
