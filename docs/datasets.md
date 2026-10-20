# Bộ dữ liệu sinh từ simulator

File này mô tả từng bộ dữ liệu trong `runs/` dùng cho phân tích tuần 5 (mốc P8, bàn giao B7). `runs/` không commit; mọi bộ **tái lập được** từ lệnh ghi ở đây, vì simulator tất định theo (config, seed).

Quy tắc dùng dữ liệu (`docs/schema.md`):
- `observed/` và `market/`: mô hình và chính sách được đọc.
- `hidden/`: ground truth, **chỉ** để đánh giá và debug; không nối vào dữ liệu huấn luyện. `analysis.io.load_run` không bao giờ trả bảng ẩn; muốn đọc phải gọi `analysis.io.load_hidden`.

---

## B7a (T4.1, sinh ngày 02/10/2026)

**Điều kiện chung**
- Code: commit `930875e` (`develop` sau Gate P3). Config: `config/default.yaml` đã hiệu chỉnh, `config_hash = cb27f5348011` (H-17).
- `world_seed = 20260930`, `run_seed = 0` cho mọi bộ `generate`: các thiết kế gặp **cùng 718.749 session** (CRN, T-29), nên so được độ chệch thiết kế trên cùng cầu.
- Cửa sổ: 28 ngày liên tục (`generate.days = 28`), warm-up 60 phút, mỗi ngày một kỳ ngân sách.
- Ngân sách: `B = 5.545,80 USD/kỳ` = 0,3 × chi tiêu của pilot `all_on` không ngân sách (seed 9000, 1 ngày: 18.486,00 USD). Chính sách cũ chịu B; thí nghiệm và GTE không áp ngân sách (D10).
- `config_hash` trong bảng là hash của config **sau khi áp override của lượt** (chính sách, thiết kế, 28 ngày), nên khác hash gốc; nó nằm trong `run_id` và `meta/run_metadata`.

| Thư mục (`runs/b7a/…`) | Chính sách | Ngân sách | Session | Order | N hoàn thành | Tỷ lệ phát | Chi voucher (USD) | V (USD) | MB | `config_hash` |
|---|---|---|---|---|---|---|---|---|---|---|
| `legacy_28d` | legacy (luật slack trễ + ε, nhắm rider theo `u_latent`, explore 5%) | B, chặn 0,1% | 718.749 | 115.232 | 108.410 | 23,2% | 152.342,5 | 350.507,6 | 56,4 | `56fe64fb463b` |
| `switchback_c1_28d` | experiment, `cluster_switchback` cụm 1, `p_on` 0,5, block 60 phút | không | 718.749 | 124.374 | 114.932 | 49,9% | 276.314,9 | 264.272,5 | 55,9 | `0a500f78bb05` |
| `switchback_c7_28d` | experiment, `cluster_switchback` cụm 7 | không | 718.749 | 123.841 | 114.040 | 49,6% | 268.964,4 | 267.106,1 | 55,6 | `bf5ccce8c655` |
| `switchback_all_28d` | experiment, `cluster_switchback` cụm all (toàn hệ) | không | 718.749 | 124.043 | 113.990 | 50,2% | 265.403,5 | 269.473,0 | 54,8 | `60fa66719536` |
| `rider_ab_28d` | experiment, `rider_ab`, `p_on` 0,5 | không | 718.749 | 124.557 | 115.116 | 50,3% | 279.152,0 | 263.081,5 | 55,3 | `1cd6d61dc7d7` |

Số session và order gồm cả warm-up (385 session); N, chi tiêu và V chỉ tính order đặt trong cửa sổ. Không bộ nào có order `Truncated`. Tổng dung lượng `runs/b7a`: 266 MB.

**GTE** (`runs/b7a/gte`, mode `gte`): `all_on` và `all_off` không ngân sách, 10 seed (0–9) × 1 ngày, ghép cặp theo seed.

| | N hoàn thành | Request | V (USD) | Chi voucher (USD) | ETA đón (phút) |
|---|---|---|---|---|---|
| `all_off` | 3.439,6 | 3.644,2 | 15.661,5 | 0 | 3,8 |
| `all_on` | 4.652,5 | 5.178,3 | 3.732,0 | 18.630,5 | 4,9 |
| **GTE = N(all_on) − N(all_off)** | **+1.212,9** (SE 14,2) | | | | |

**B** (`runs/b7a/calibrate_budget`, mode `calibrate_budget`): `budget_B_usd = 5545.80` trong `meta/run_metadata`.

### Mỗi thư mục `generate` có gì

```
observed/riders.parquet      observed/sessions.parquet    observed/orders.parquet
market/slot_snapshots.parquet
hidden/riders_hidden.parquet hidden/sessions_hidden.parquet
results/policy_results.parquet    meta/run_metadata.parquet
```

Ghi chú cho người dùng dữ liệu:
- `legacy_28d` là dữ liệu quan sát có confounding: session nhắm theo rider có `propensity = NaN` (xác suất thật ở `hidden/sessions_hidden.propensity_true`); lát explore (`assign_mechanism = explore`) có `propensity = 0,5` và là phần ngẫu nhiên hóa sạch; ô tắt có `propensity = 0` (T-24).
- Switchback: `cluster_id` 0–36 (cụm 1), 0–6 (cụm 7), 0 (all); `block` tính từ đầu lượt chạy, warm-up là block 0; `in_burnin` đánh dấu 15 phút đầu mỗi block; `propensity = cell_propensity = 0,5` (T-25).
- `rider_ab_28d`: mọi ô bật, `cluster_id = −1`, arm cố định theo rider suốt 28 ngày.
- Kết cục mặc định cho uplift là `completed`: `analysis.io.completed_outcome(sessions, orders)`.

### Lệnh sinh lại (PowerShell, từ thư mục gốc repo)

```powershell
$py = ".venv\Scripts\python.exe"
& $py -m sim run --mode calibrate_budget --config config/default.yaml --out runs/b7a/calibrate_budget
& $py -m sim run --mode generate --config config/default.yaml --set policy.name=legacy --out runs/b7a/legacy_28d
& $py -m sim run --mode generate --config config/default.yaml --set policy.name=experiment --set experiment.cluster_level=1 --out runs/b7a/switchback_c1_28d
& $py -m sim run --mode generate --config config/default.yaml --set policy.name=experiment --set experiment.cluster_level=7 --out runs/b7a/switchback_c7_28d
& $py -m sim run --mode generate --config config/default.yaml --set policy.name=experiment --set experiment.cluster_level=all --out runs/b7a/switchback_all_28d
& $py -m sim run --mode generate --config config/default.yaml --set policy.name=experiment --set experiment.design=rider_ab --out runs/b7a/rider_ab_28d
& $py -m sim run --mode gte --config config/default.yaml --out runs/b7a/gte
```

Mỗi bộ `generate` chạy 4–5 phút trên một lõi (đo: 240–272 giây); chạy song song được vì mỗi lệnh một tiến trình.

### Kiểm tra trước khi dùng (điều kiện nhận B7)

```powershell
& $py -m analysis.check_dataset runs/b7a/legacy_28d runs/b7a/switchback_c1_28d runs/b7a/switchback_c7_28d runs/b7a/switchback_all_28d runs/b7a/rider_ab_28d runs/b7a/gte runs/b7a/calibrate_budget
& $py -m analysis.check_budget runs/b7a/legacy_28d
```

- `check_dataset`: mọi bảng đúng cột và kiểu trên đĩa của `schema.md`; không cột ẩn nào trong `observed/`, `market/`; session, order và results khớp nhau; `config_hash` trong metadata đúng là hash của config đã lưu. Kết quả 02/10: cả 7 thư mục **OK**.
- `check_budget` trên `legacy_28d`: cả 28 kỳ chi ≤ B (lớn nhất 5.545,68 USD), warm-up 91,98 ≤ 231,07 USD; 450 session bị chặn đều có `arm = 0`, `voucher = 0`.
- Sinh lại trên máy khác phải ra đúng các con số trong bảng (session, order, N); khác là sai config hoặc sai phiên bản code.

---

## B7b (bản H-21, sinh ngày 05/10/2026): sweep θ tham chiếu với κ auto điểm bất động

Bản này thay bản ngày 02/10 (`runs/b7b`, κ từ một lượt pilot, seed pilot riêng cho từng θ). Theo H-21 và T-32: κ auto lặp pilot đến khi chi của pilot ≤ B, mọi θ dùng chung seed pilot 9000, κ giữ trọn nhóm điểm bằng nhau. B không đổi (5.545,80 USD/kỳ) nên **B7a không phải sinh lại**. T-31 (θ\* là tập, chấm θ̂ bằng regret) giữ nguyên; mentor chưa xác nhận T-31.

### Sweep tham chiếu (`runs/b7b_h21/sweep_theta_ref`)

- Chính sách `threshold`, hàm điểm `heuristic_low_freq`, dự báo `persistence`, không hysteresis.
- 16 mốc θ (lớp phủ `config/sweep_reference.yaml`) × 30 seed (0–29) × 1 ngày = 480 lượt; cùng B; κ auto mỗi θ lặp 1–6 lượt pilot (cột `kappa_pilots` của `meta/run_metadata`), không θ nào chạm trần `kappa_max_iter = 10`.
- `config_hash` của lượt: `19f46649271e`; `config_hash(default.yaml)` = `34f3aa436d16` (đổi từ `cb27f5348011` vì thêm khóa `policy.threshold.kappa_max_iter`, T-32); code `8467f83` + thay đổi H-21 (chưa commit lúc sinh, nay là commit `7b6a003`).
- Sinh lại sau khi thêm khóa `policy.threshold.scope` (T-35 f) cho `config_hash` của lượt `11358df900a6` thay vì `19f46649271e`; mọi số N theo θ, tập θ\* và regret trùng từng chữ số (Hoàng kiểm khi nhận, log 05/10). Hash lệch chỉ vì khóa mới có giá trị mặc định.
- Cột cuối so với bản 02/10, ghép cặp theo seed (cùng seed đánh giá nên chỉ khác ở κ).

| θ | N trung bình | SE | V (USD) | Chi voucher (USD) | % (ô, slot) tắt | κ (cũ → mới) | lượt pilot | Kém θ tốt nhất (± SE ghép cặp) | Thuộc tập θ\* | N mới − cũ (± SE) |
|---|---|---|---|---|---|---|---|---|---|---|
| 0 | 3.860,3 | 11,0 | 12.378,0 | 5.523,3 | 0 | −2,349 → −2,236 | 4 | 39,5 ± 4,0 | không | 11,9 ± 3,2 |
| 0,1 | 3.884,3 | 11,3 | 12.477,5 | 5.520,7 | 25 | −2,584 → −2,425 | 3 | 15,4 ± 4,3 | không | 6,9 ± 3,5 |
| 0,2 | 3.887,0 | 10,8 | 12.479,8 | 5.523,4 | 27 | −2,589 → −2,469 | 3 | 12,7 ± 3,4 | không | 4,8 ± 3,5 |
| 0,3 | 3.893,2 | 11,0 | 12.522,7 | 5.514,6 | 30 | −2,708 → −2,512 | 6 | 6,5 ± 3,7 | **có** | 8,5 ± 4,4 |
| 0,4 | 3.890,8 | 11,3 | 12.531,5 | 5.495,4 | 31 | −2,708 → −2,563 | 3 | 9,0 ± 4,0 | **có** | 1,0 ± 4,4 |
| 0,5 | 3.894,9 | 10,3 | 12.580,8 | 5.462,6 | 33 | −2,767 → −2,574 | 3 | 4,9 ± 2,7 | **có** | 3,1 ± 4,0 |
| 0,6 | 3.892,2 | 11,0 | 12.573,4 | 5.447,7 | 35 | −2,821 → −2,622 | 5 | 7,5 ± 3,3 | **có** | −0,8 ± 3,6 |
| 0,8 | 3.889,3 | 10,4 | 12.607,6 | 5.390,9 | 37 | −2,951 → −2,687 | 5 | 10,4 ± 3,9 | **có** | −4,2 ± 3,6 |
| 1 | 3.889,9 | 11,5 | 12.581,8 | 5.414,6 | 39 | −2,995 → −2,741 | 3 | 9,9 ± 3,1 | không | −4,5 ± 4,0 |
| 1,25 | 3.890,4 | 11,6 | 12.583,3 | 5.421,9 | 42 | −3,206 → −2,894 | 5 | 9,4 ± 3,3 | **có** | 2,5 ± 4,5 |
| 1,5 | 3.891,8 | 11,6 | 12.579,3 | 5.431,2 | 45 | −3,101 → −2,986 | 4 | 7,9 ± 3,6 | **có** | −9,8 ± 3,7 |
| **2** | **3.899,7** | 10,6 | 12.562,3 | 5.484,6 | 47 | −3,404 → −3,172 | 4 | 0 (tốt nhất) | **có** | 2,3 ± 3,3 |
| 3 | 3.882,3 | 10,4 | 12.674,7 | 5.275,2 | 53 | −3,790 → −3,443 | 5 | 17,5 ± 3,9 | không | −7,3 ± 3,7 |
| 5 | 3.876,9 | 10,8 | 12.516,8 | 5.424,9 | 61 | −4,931 → −4,485 | 3 | 22,9 ± 3,9 | không | −6,4 ± 4,5 |
| 10 | 3.837,2 | 10,0 | 12.539,1 | 5.233,8 | 69 | −∞ → −∞ | 1 | 62,5 ± 4,6 | không | 0,0 ± 0,0 |
| 30 | 3.693,0 | 9,8 | 13.579,8 | 3.436,3 | 75 | −∞ → −∞ | 1 | 206,7 ± 3,8 | không | 0,0 ± 0,0 |

**Đọc bảng:**
- κ mới chặt hơn ở mọi θ có κ hữu hạn; chi tiêu còn 94–99,6% B thay vì luôn chạm 100% (ngân sách không còn cạn trước cuối ngày, Q26). N ở θ = 0 tăng 11,9 ± 3,2; ở đoạn phẳng thay đổi trong khoảng ±10, cùng cỡ nhiễu κ của Q24.
- Đường vẫn có ba đoạn tăng, phẳng, giảm. **Khoảng θ\* = [0,3; 2]**, θ tốt nhất 2 (bản cũ: [0,4; 2], tốt nhất 1,5). Mốc 1 bị loại khỏi tập (kém 9,9 ± 3,1): dùng chung seed pilot chưa xóa hết nhiễu riêng theo θ của κ.
- Regret theo N (± SE ghép cặp): θ = 0: 39,5 ± 4,0 (1,01%; bản cũ 53,3); θ mặc định 0,35 (nội suy): 7,8 ± 3,3 (0,20%); θ = 0,5: 4,9 ± 2,7; θ = 10: 62,5 ± 4,6; θ = 30: 206,7 ± 3,8 (5,30%). Lợi ích của tầng ô nhỏ hơn bản cũ: một phần lợi ích cũ đến từ việc κ cũ làm ngân sách cạn sớm ở θ nhỏ.
- Mốc so sánh (`runs/b7a/gte`, seed 0–9): `all_off` 3.439,6; `all_on` không ngân sách 4.652,5. `all_on` có B (30 seed, `runs/s5/policy_table`): 3.686,7.

File: `results/policy_results.parquet` (480 lượt), `results/theta_sweep.parquet`, `meta/run_metadata.parquet` (κ, số lượt pilot, B của từng lượt).

### Phân tích độ nhạy (`runs/b7b_h21/sens_*`, 16 θ × 10 seed mỗi bộ)

| Thư mục | Override | B (USD/kỳ) | N tại θ = 0 | N lớn nhất (θ) | N tại θ = 30 | Khoảng θ\* | Regret của θ = 0 | Lượt pilot (min–max) |
|---|---|---|---|---|---|---|---|---|
| `sens_fraction_0p1` | `budget.fraction=0.1` | 1.848,6 | 3.586 | 3.606,7 (0,6) | 3.581 | [0,1; 10] | +20,9 ± 5,4 (0,58%) | 3–5 |
| `sens_fraction_0p6` | `budget.fraction=0.6` | 11.091,6 | 4.240 | 4.298,3 (0,2) | 3.685 | [0,1; 1] | +58,8 ± 4,9 (1,37%) | 1–5 |
| `sens_voucher_0p3` | `voucher.pct_of_fare=0.3` | 9.076,3 | 3.972 | 4.033,4 (1,5) | 3.775 | [0,1; 3], loại 0,2 | +61,6 ± 8,5 (1,53%) | 1–7 |
| `sens_demand_1p5` | `demand.demand_scale=1.5` | 6.430,7 | 5.489 | 5.650,3 (1,25) | 5.347 | [0,3; 2], loại 0,4; 0,5 | +160,9 ± 15,6 (2,85%) | 1–9 |

- Cả bốn biến thể vẫn có đủ ba đoạn. θ = 0,6 thuộc tập θ\* ở cả năm kịch bản; θ = 0,4 thuộc tập ở bốn kịch bản (bị loại ở `sens_demand_1p5`).
- Lợi ích của tầng ô vẫn tăng theo độ căng (0,58% khi ngân sách rất chặt, 1,01% ở cấu hình chuẩn, 2,85% khi cầu gấp rưỡi) nhưng nhỏ hơn bản cũ (0,50%; 1,37%; 4,10%).

### Lệnh sinh lại (PowerShell)

```powershell
$py = ".venv\Scripts\python.exe"
$ref = @("--config", "config/default.yaml", "--config", "config/sweep_reference.yaml")
& $py -m sim run --mode sweep_theta @ref --set sweep.n_seeds=30 --out runs/b7b_h21/sweep_theta_ref
& $py -m sim run --mode sweep_theta @ref --set budget.fraction=0.1 --out runs/b7b_h21/sens_fraction_0p1
& $py -m sim run --mode sweep_theta @ref --set budget.fraction=0.6 --out runs/b7b_h21/sens_fraction_0p6
& $py -m sim run --mode sweep_theta @ref --set voucher.pct_of_fare=0.3 --out runs/b7b_h21/sens_voucher_0p3
& $py -m sim run --mode sweep_theta @ref --set demand.demand_scale=1.5 --out runs/b7b_h21/sens_demand_1p5
& $py -m analysis.check_dataset runs/b7b_h21/sweep_theta_ref runs/b7b_h21/sens_fraction_0p1 runs/b7b_h21/sens_fraction_0p6 runs/b7b_h21/sens_voucher_0p3 runs/b7b_h21/sens_demand_1p5
```

Thời gian (8 tiến trình, chạy cùng lúc với việc khác): sweep tham chiếu 1.613 giây; mỗi sweep độ nhạy 548–910 giây. `check_dataset` ngày 05/10: cả 5 thư mục OK.

### Dùng cho phân tích tuần 5

```python
import pandas as pd
from analysis.metrics import theta_star_set, theta_star_interval, sweep_regret
runs = pd.read_parquet("runs/b7b_h21/sweep_theta_ref/results/policy_results.parquet")
theta_star_interval(theta_star_set(runs))      # (2.0, 0.3, 2.0)
sweep_regret(runs, theta_hat=0.8)              # regret theo N của một ngưỡng ước lượng θ̂
```

Ước lượng θ̂ được chấm bằng regret theo N, không bằng khoảng cách tới θ\* (T-31).

**Bản cũ** (`runs/b7b`, 02/10, κ một lượt pilot): N lớn nhất 3.901,7 ở θ = 1,5; khoảng θ\* [0,4; 2]; regret θ = 0: 53,3 ± 4,8. Giữ để đối chiếu; xóa được sau khi Hoàng nhận bản H-21. `runs/p6/` cũng xóa được.

---

## B8 (H5.1, giao ngày 05/10/2026): hàm điểm τ̂ nền và parquet dự đoán

**Hàm điểm** (`analysis/scores.py`, bảng `analysis/models/tau_x_baseline.json`, có commit):
- `analysis.scores:tau_x_baseline`: hiệu ứng voucher lên `completed`, theo `x_segment` × tercile `x_freq`, ước lượng trên lát explore của `legacy_28d` (H4.2).
- `analysis.scores:tau_per_dollar_baseline`: cùng hiệu ứng chia cho chi phí voucher kỳ vọng của một lượt phát (số chuyến thêm trên 1 USD).
- Dùng: `--set policy.threshold.score_fn=analysis.scores:tau_per_dollar_baseline`. Chỉ đọc `x_freq`, `x_segment` của `SessionBatch`, bỏ qua ŝ, tất định.

**Parquet dự đoán** (`runs/b8/`, không commit): một file cho mỗi bộ B7a, `predictions_<bộ>.parquet`, 718.749 dòng; cột `session_id`, `rider_id`, `in_window`, `tau_x_baseline`, `tau_per_dollar_baseline`. Ghép với dữ liệu bằng `session_id`. Năm file giống nhau vì các bộ dùng chung session (CRN) và điểm chỉ phụ thuộc đặc trưng rider.

Lệnh sinh lại (PowerShell):

```powershell
foreach ($d in "legacy_28d","rider_ab_28d","switchback_c1_28d","switchback_c7_28d","switchback_all_28d") {
  & $py -m analysis.scores predict runs/b7a/$d runs/b8/predictions_$d.parquet
}
```

**Kiểm tra trước khi dùng (điều kiện nhận B8):** `pytest -q tests/test_estimate.py` (có test nạp hàm điểm trong tiến trình con `spawn`, test tất định và test chỉ đọc cột của batch).

**Lưu ý:** bảng τ̂ được ước lượng trên lát explore của `legacy_28d`. Các bộ khác cùng session nhưng khác cách gán voucher, nên kết quả kiểm định trên đó không độc lập hoàn toàn với dữ liệu huấn luyện.

**Bổ sung H5.2 (DR-learner, H-24):** 8 hàm điểm `analysis.uplift:tau_x_dr`, `tau_xs_dr`, `tau_x_dr_per_dollar`, `tau_xs_dr_per_dollar` (mẫu explore) và các bản `..._all` (toàn bộ legacy). Mô hình ở `analysis/models/dr_*` (có commit); fit lại bằng `python -m analysis.uplift fit runs/b7a/legacy_28d`. Cần `pip install -e ".[analysis]"` (LightGBM). Kiểm tra: `pytest -q tests/test_uplift.py`.

---

## Dữ liệu Sprint 5 (05/10/2026, `runs/s5`)

| Thư mục | Lệnh | Dùng cho |
|---|---|---|
| `policy_table` | `python -m analysis.policy_table --config config/default.yaml --n-seeds 30 --out runs/s5/policy_table` | T5.1: 15 chính sách (`DEFAULT_SPECS`, gồm 6 hàm điểm DR và 2 dòng π_θ̂ trên `ring1`) × 30 seed dưới cùng B (T-33, T-35d) |
| `sweep_ring1_heuristic` | `python -m sim run --mode sweep_theta --config config/default.yaml --config config/sweep_reference.yaml --set sweep.n_seeds=30 --set policy.threshold.scope=ring1 --out runs/s5/sweep_ring1_heuristic` | Sweep θ với ŝ `ring1` (H-25), `heuristic_low_freq`; so với B7b (ŝ theo ô) |
| `sweep_ring1_dr` | như trên, thêm `--set policy.threshold.score_fn=analysis.uplift:tau_x_dr_all_per_dollar` | Sweep θ với ŝ `ring1` và hàm điểm học được (H-22 ii); regret của θ̂ (A), (B) của H5.3 |
| `sweep_cell_dr` | như `sweep_ring1_dr` nhưng bỏ `--set policy.threshold.scope=ring1` | Sweep θ với ŝ theo ô và hàm điểm học được (H-22 ii) |
| `sweep_ring1_random` | như `sweep_ring1_dr` nhưng `--set policy.threshold.score_fn=random` (Hoàng, 05/10) | H5.3: regret của θ̂ (B) với tầng rider rải đều (H-26). θ tốt nhất 3 (3.884,5), tập θ* {3}; N(θ = 0) 3.807,0 trùng dòng `random` của T5.1; θ = 5 trùng `sweep_ring1_dr` (3.849,2, κ = −∞); 6 phút, 16 tiến trình |
| `qini_vs_value` | `python -m analysis.qini_vs_value --data runs/b7a/rider_ab_28d --table runs/s5/policy_table --outcome completed --out runs/s5/qini_vs_value` (và `--outcome requested`) | T5.2 (T-33). Từ 05/10 tối, `--outcome completed` ghi thêm `results/offer_efficiency.parquet` (T-36); chạy lại lệnh trên nếu thư mục thiếu file này (khoảng 6 phút) |
| `interference` | `python -m analysis.interference designs --gte runs/b7a/gte runs/b7a/rider_ab_28d runs/b7a/switchback_c1_28d runs/b7a/switchback_c7_28d runs/b7a/switchback_all_28d --out runs/s5/interference` (và `--outcome requested`); `python -m analysis.interference confounding runs/s5/legacy_gu_0_28d runs/s5/legacy_gu_0p5_28d runs/b7a/legacy_28d runs/s5/legacy_gu_2_28d --out runs/s5/interference` | T5.3 (T-34) |
| `legacy_gu_0_28d`, `legacy_gu_0p5_28d`, `legacy_gu_2_28d` | `python -m sim run --mode generate --config config/default.yaml --set policy.name=legacy --set policy.legacy.target_g_u=<0 / 0.5 / 2> --out runs/s5/legacy_gu_<0 / 0p5 / 2>_28d` | T5.3: độ nhạy theo `u_latent`; cùng `run_seed = 0` nên cùng 718.749 session với `legacy_28d` (`target_g_u` = 1) |

Ba bộ legacy mới (`config_hash` `2da8ea13c0e0` / `7306ebc20b78` / `5b6a3ba4079b`, code `8467f83` + H-21): N hoàn thành 105.941 / 106.911 / 108.177; tỷ lệ phát 22,0% / 22,2% / 22,2%; bị chặn ngân sách 0% / 0% / 2,9%; không order Truncated; `check_dataset` OK.

Máy 16 GB RAM (Hoàng, 05/10): `policy_table` với mặc định một tiến trình mỗi lõi (16) hết bộ nhớ ảo khi các tiến trình cùng nạp LightGBM; chạy với `--set runner.n_procs=6` (khoảng 10 phút) và `$env:PYTHONIOENCODING = "utf-8"` nếu chuyển hướng output (H-28 e). Bảng sinh lại trên máy Hoàng trùng từng số với bảng của Tình (N của 15 chính sách); ba bộ `legacy_gu_*` sinh lại trùng N hoàn thành 105.941 / 106.911 / 108.177.

### Sweep θ theo phạm vi ŝ và hàm điểm (vòng 2, code `ed64f62` + thay đổi chưa commit)

Cùng lưới 16 θ, cùng 30 seed, cùng B = 5.545,80 USD/kỳ với B7b (`runs/b7b_h21/sweep_theta_ref`, ŝ theo ô, `heuristic_low_freq`), nên ghép cặp được theo seed. `config_hash` của lượt: `89c0a9869ced` (`ring1`, heuristic), `c54d6db7563e` (`ring1`, DR), `0fa4fa60e36c` (ô, DR); `check_dataset` OK cả 3 và `policy_table`. Mỗi θ cần 1–6 lượt pilot κ, không chạm `kappa_max_iter`. Hàm điểm DR = `analysis.uplift:tau_x_dr_all_per_dollar`.

| Sweep | θ tốt nhất | N(θ tốt nhất) | Bao tập θ\* | θ trong bao bị loại | N(θ = 0) | Regret θ = 0 |
|---|---|---|---|---|---|---|
| ô, heuristic (B7b) | 2 | 3.899,7 | [0,3; 2] | 1 | 3.860,3 | 39,5 ± 4,0 (1,01%) |
| `ring1`, heuristic | 3 | 3.915,7 | [1,5; 3] | 2 | 3.860,3 | 55,4 ± 4,7 (1,42%) |
| ô, DR/USD | 0,4 | 4.006,6 | [0,1; 0,6] | không | 3.989,9 | 16,6 ± 5,7 (0,42%) |
| `ring1`, DR/USD | 1 | 4.017,4 | [0,2; 1,5] | không | 3.989,9 | 27,4 ± 4,1 (0,68%) |

N theo θ (chi / B, % (ô, slot) tắt); **đậm** = thuộc tập θ\*:

| θ | ô, heuristic | `ring1`, heuristic | ô, DR/USD | `ring1`, DR/USD |
|---|---|---|---|---|
| 0 | 3.860,3 (0,996; 0) | 3.860,3 (0,996; 0) | 3.989,9 (0,988; 0) | 3.989,9 (0,988; 0) |
| 0,1 | 3.884,3 (0,995; 25) | 3.871,4 (0,992; 6) | **3.999,2** (0,986; 25) | 4.002,1 (0,983; 6) |
| 0,2 | 3.887,0 (0,996; 27) | 3.881,2 (0,991; 8) | **4.004,6** (0,986; 27) | **4.010,3** (0,988; 9) |
| 0,3 | **3.893,2** (0,994; 30) | 3.889,2 (0,990; 11) | **4.005,3** (0,974; 30) | **4.014,2** (0,983; 11) |
| 0,4 | **3.890,8** (0,991; 31) | 3.894,6 (0,991; 13) | **4.006,6** (0,985; 32) | **4.012,9** (0,973; 13) |
| 0,5 | **3.894,9** (0,985; 33) | 3.893,0 (0,993; 14) | **4.000,3** (0,990; 33) | **4.014,0** (0,966; 15) |
| 0,6 | **3.892,2** (0,982; 35) | 3.896,4 (0,993; 16) | **3.997,4** (0,974; 35) | **4.014,1** (0,971; 17) |
| 0,8 | **3.889,3** (0,972; 37) | 3.895,8 (0,981; 20) | 3.990,2 (0,968; 38) | **4.015,5** (0,965; 20) |
| 1 | 3.889,9 (0,976; 39) | 3.902,8 (0,989; 23) | 3.989,1 (0,972; 39) | **4.017,4** (0,973; 23) |
| 1,25 | **3.890,4** (0,978; 42) | 3.900,0 (0,981; 27) | 3.980,5 (0,968; 43) | **4.009,3** (0,967; 28) |
| 1,5 | **3.891,8** (0,979; 45) | **3.908,0** (0,980; 31) | 3.976,2 (0,983; 45) | **4.015,7** (0,970; 32) |
| 2 | **3.899,7** (0,989; 47) | 3.901,9 (0,969; 38) | 3.964,1 (0,965; 48) | 4.006,0 (0,995; 40) |
| 3 | 3.882,3 (0,951; 53) | **3.915,7** (0,988; 52) | 3.940,6 (0,924; 54) | 3.972,4 (0,981; 53) |
| 5 | 3.876,9 (0,978; 61) | 3.849,2 (0,890; 70) | 3.919,4 (0,974; 61) | 3.849,2 (0,890; 70) |
| 10 | 3.837,2 (0,944; 69) | 3.597,2 (0,324; 86) | 3.837,2 (0,944; 69) | 3.597,2 (0,324; 86) |
| 30 | 3.693,0 (0,620; 75) | 3.443,5 (0; 100) | 3.693,0 (0,620; 75) | 3.443,5 (0; 100) |

- **Hai cách đo ŝ, so ở θ tốt nhất của mỗi sweep (ghép cặp):** `ring1` − ô: heuristic +16,0 ± 3,2; DR/USD +10,8 ± 4,4. Cả hai phía đều chọn "tốt nhất" trên chính các seed này (winner's curse, báo cáo §10.3), nên hiệu này chỉ là chỉ dấu. Cùng θ thì không so được, vì θ đo trên hai thang khác nhau: `ring1` tắt ít (ô, slot) hơn ở θ nhỏ và nhiều hơn ở θ lớn.
- **H-22 (ii), dự đoán "điểm càng tốt thì θ\* càng gần 0": đúng trên cả hai cách đo.** Với DR/USD thay heuristic, bao θ\* dời từ [0,3; 2] về [0,1; 0,6] (ô) và từ [1,5; 3] về [0,2; 1,5] (`ring1`); regret của θ = 0 giảm từ 39,5 xuống 16,6 (ô) và từ 55,4 xuống 27,4 (`ring1`). θ = 0 vẫn chưa thuộc tập θ\* ở cả bốn sweep. DR/USD hơn heuristic ở θ tốt nhất +106,8 ± 4,7 (ô).
- **Regret của θ̂ (H5.3) trên các sweep `ring1`:** θ̂ (A) = 0,25: 5,1 ± 3,1 [−1,3; 11,5] (0,13%) với DR/USD; 30,5 ± 4,2 (0,78%) với heuristic. θ̂ (B) = 5: 168,2 ± 5,3 [157,4; 178,9] (4,19%) với DR/USD; 66,5 ± 3,9 (1,70%) với heuristic. Ở θ = 5 trên `ring1`, 70% (ô, slot) tắt, κ = −∞ (phát cho mọi session của ô còn bật) mà vẫn chỉ chi 0,890 × B: tầng ô cắt nhiều hơn mức ngân sách cần.
- **Với `ring1`, θ > `monitor.slack_cap` (10) là `all_off`.** `ring1` cắt +∞ ở `slack_cap` (H-25a), còn ŝ theo ô với `forecast: persistence` giữ +∞ (ô bật, T-23b). Vì vậy ở θ = 30, `ring1` tắt 100% và N = 3.443,5 (≈ `all_off` 3.440,1), còn ô vẫn bật 25% (ô có xe rảnh mà không xe nào đi đón). Chỉ điểm θ = 30 bị ảnh hưởng; tập θ\* không đổi.
- Kiểm chéo: π_θ θ = 0,5 heuristic của `policy_table` = 3.894,9, trùng điểm θ = 0,5 của B7b; π_θ̂ (B) `ring1` = 3.849,2, trùng θ = 5 của `sweep_ring1_dr`. Lượt chạy ~15–18 phút mỗi sweep (480 lượt + pilot κ).

## Dữ liệu H5.3 (05/10/2026, `runs/h53`)

Dùng cho chọn ŝ (H-25) và tác hại theo giờ; notebook `04_uoc_luong_va_theta` đọc cả ba. Đều là `evaluate` 5 seed (0–4) với `--log-level full` (cần bảng session, order, snapshot của từng lượt).

| Thư mục | Lệnh | Dùng cho |
|---|---|---|
| `all_off_3d` | `python -m sim run --mode evaluate --config config/default.yaml --set policy.name=all_off --set time.window_min=4320 --set sweep.n_seeds=5 --log-level full --out runs/h53/all_off_3d` | Chọn ŝ: `python -m analysis.tension runs/h53/all_off_3d` (bảng 7 ứng viên); cửa sổ 3 ngày để `cell_ar` có lag ngày |
| `hourly_all_on`, `hourly_all_off` | `python -m sim run --mode evaluate --config config/default.yaml --set policy.name=<all_on / all_off> --set budget.enforce=false --set sweep.n_seeds=5 --log-level full --out runs/h53/hourly_<all_on / all_off>` | Tác hại theo giờ: phát voucher cho mọi khách trừ không phát, không ngân sách, ghép cặp theo seed (`analysis.theta.on_off_by_hour_runs`) |

Kiểm tra: `on_off_by_hour_runs` cho cả ngày +1.198,8 chuyến, 7h −15,4 ± 2,1 (log H5.3).

---

## Dữ liệu Sprint 6 (05/10/2026, `runs/s6`): kiểm định cho notebook 01

Notebook kết quả chỉ đọc dữ liệu, không chạy mô phỏng (H-27). Các thư mục dưới đây cho notebook `01_simulator_kiem_dinh` các số A1, A3, A5 và CAL; A2 (b) dùng `runs/s5/policy_table` và `runs/b7b_h21/sweep_theta_ref`, A4 dùng `runs/b7a/switchback_c7_28d`. Sinh bằng code `52f99a5` (`sim/` không đổi trong S6), `config_hash(default.yaml)` = `92b7d13adc39`.

| Thư mục | Lệnh (`python -m sim run --config config/default.yaml ...`) | Dùng cho | `config_hash` | Thời gian |
|---|---|---|---|---|
| `a5_evaluate_1proc` | `--mode evaluate --set sweep.n_seeds=3 --set runner.n_procs=1 --out runs/s6/a5_evaluate_1proc` | A5: thời gian engine 1 ngày, 1 tiến trình, log tối thiểu (chạy riêng, máy không bận) | `dca3cafc1f50` | 44 s |
| `throughput` | `--mode throughput_curve --out runs/s6/throughput` | A1: 16 mức cầu × 3 seed | — (mode chỉ ghi `results/`, spec §7) | 152 s |
| `gte_full_5` | `--mode gte --set gte.n_seeds=5 --log-level full --out runs/s6/gte_full_5` | CAL: log đầy đủ của all_off 5 seed (cùng các job của test chậm CAL); kèm all_on | `06a9d1599ace` | 25 s |
| `a3_h0` | `--mode evaluate --set sweep.n_seeds=5 --out runs/s6/a3_h0` | A3: π_θ θ = 0,35, h = 0 | `feb7327a82e5` | 36 s |
| `a3_h01` | như trên, thêm `--set policy.threshold.hysteresis_h=0.1`, `--out runs/s6/a3_h01` | A3: h = 0,1 | `a37ada11e9eb` | 42 s |

`check_dataset`: OK cả 4 thư mục có `meta/`; `throughput` báo thiếu `meta/run_metadata` vì mode `throughput_curve` chỉ ghi `results/throughput_curve.parquet` (đúng spec §7), không phải lỗi dữ liệu.

Số chính (chi tiết trong notebook 01):
- A1 đạt 4/4: đỉnh 781,2 chuyến/giờ ở mức 3,25 (trùng gate P3, log H3.2); mức cầu lớn nhất 0,845 × đỉnh; slack lớn nhất sau đỉnh 0,095; bước ETA lớn nhất 1,871 phút.
- CAL 5/5 trong khoảng: P(đặt) 0,1535; tăng request +50,6%; giá trung bình 19,03 USD; (ô, slot) căng 27,9%, dư 63,3%. `tests/test_validation.py` kiểm rằng tính từ bảng đã lưu bằng đúng số test chậm tính trong bộ nhớ (cùng seed).
- A2 (b): Var(hiệu, CRN) / Var(hiệu, seed độc lập) = 0,171 trên 30 seed, 0,135 trên 20 seed (ngưỡng 0,5; T-37).
- A3: trung vị 25,6 lần/ô/ngày (h = 0), 25,0 (h = 0,1); N không đổi (3.884,6 và 3.883,0).
- A4: 0 lần đổi dấu (trùng H4.2).
- A5: trung vị 5,75 giây / ngày mô phỏng (ngưỡng 30).

---

## Dữ liệu Sprint 8 (T8.4, sinh ngày 14/10/2026, `runs/s8`): W0 và W1 trên quy mô Hà Nội

**Điều kiện chung**
- Code: `develop` sau PR #28 (S7 của Hoàng) + T-42 trên `develop1`. W0: `config/default.yaml` đã hiệu chỉnh, `config_hash = ae9b7b9a1b94` (T-42). Sau T-43 (cùng ngày, chỉ đổi lưới A1 `throughput.demand_scale_grid`) hash của `default.yaml` là `514d0b41c378`; hai cấu hình khác đúng khóa đó, mà không mode sinh dữ liệu nào đọc, nên các bộ dưới đây vẫn là dữ liệu của cấu hình hiện hành. W1: thêm lớp phủ `config/world_w1.yaml` (bật `shocks`, `fleet_size` 1.278 = 1.150 / 0,90), `config_hash = e1730a6d2fe0`.
- Tiền tính bằng **đồng** (B9, H-33); cột `*_vnd`. `world_seed = 20260930`, `run_seed = 0` cho mọi bộ `generate` (cùng session, CRN); GTE và bật/tắt ghép cặp theo seed.
- Cửa sổ `generate`: 28 ngày, warm-up 60 phút, mỗi ngày một kỳ ngân sách; B = 0,3 × chi tiêu pilot `all_on` không ngân sách (`budget.fraction`). Chính sách cũ chịu B; thí nghiệm và GTE không áp ngân sách (D10).
- Hai quy ước chốt khi sinh (ghi ở đây vì kế hoạch v3 §2.1 không nêu số): (1) "4 mức nhắm theo `u`" = `policy.legacy.target_g_u` ∈ {0; 0,5; 1,0 (bộ `legacy_28d`); 2}, theo tiền lệ S5 (`runs/s5/legacy_gu_*`); (2) "GTE với đội xe rất lớn" = `supply.fleet_size` 6.000 (W0, ≈ 5,2 × đội xe chuẩn) và 6.667 (W1, cùng quy ước /0,90), đủ để không còn (ô, slot) căng, tách phần "công suất" ở kế hoạch §4.4.
- Thư mục: `runs/s8/w0/…` và `runs/s8/w1/…` cùng tên bộ; thời gian chạy thật ở `runs/s8/times.txt` và bảng dưới.

### Lệnh sinh lại (bash, từ thư mục gốc repo; mỗi `generate` một tiến trình, 8 lượt song song)

```bash
PY=.venv/Scripts/python.exe
W0="--config config/default.yaml"
W1="--config config/default.yaml --config config/world_w1.yaml"
for w in w0 w1; do
  if [ "$w" = w0 ]; then C="$W0"; else C="$W1"; fi
  $PY -m sim run --mode generate $C --set policy.name=legacy --out runs/s8/$w/legacy_28d
  for gu in 0 0.5 2; do   # thư mục: legacy_gu_0 / legacy_gu_0p5 / legacy_gu_2
    $PY -m sim run --mode generate $C --set policy.name=legacy --set policy.legacy.target_g_u=$gu --out runs/s8/$w/legacy_gu_$(echo $gu | tr . p)_28d
  done
  for lvl in 1 7 all; do
    $PY -m sim run --mode generate $C --set policy.name=experiment --set experiment.cluster_level=$lvl --out runs/s8/$w/switchback_c${lvl}_28d
  done
  $PY -m sim run --mode generate $C --set policy.name=experiment --set experiment.design=rider_ab --out runs/s8/$w/rider_ab_28d
  $PY -m sim run --mode calibrate_budget $C --out runs/s8/$w/calibrate_budget
  $PY -m sim run --mode gte $C --out runs/s8/$w/gte
  for pol in all_on all_off; do
    $PY -m sim run --mode evaluate $C --set policy.name=$pol --set budget.enforce=false --set sweep.n_seeds=5 --log-level full --out runs/s8/$w/${pol}_5seeds_full
  done
done
$PY -m sim run --mode gte $W0 --set supply.fleet_size=6000 --out runs/s8/w0/gte_bigfleet
$PY -m sim run --mode gte $W1 --set supply.fleet_size=6667 --out runs/s8/w1/gte_bigfleet
```

### Kiểm tra trước khi dùng (điều kiện nhận B11)

```bash
$PY -m analysis.check_dataset runs/s8/w0/* runs/s8/w1/* --markdown
$PY -m analysis.check_budget runs/s8/w0/legacy_28d
$PY -m analysis.check_budget runs/s8/w1/legacy_28d
```

### Kết quả (14/10/2026; bảng từ `analysis.check_dataset --markdown`, cả 26 thư mục **OK**)

**W0** (`runs/s8/w0`, B = 113.760.488 đ/kỳ = 0,3 × chi tiêu pilot `all_on` không ngân sách seed 9000, 1 ngày: 379.201.628 đ)

| Thư mục | Mode | Chính sách | Ngày | Seed | Session | Order | N hoàn thành | Tỷ lệ phát | Tỷ lệ chặn | MB | `config_hash` |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `legacy_28d` | generate | legacy (`target_g_u` 1,0), chịu B | 28 | 0 | 4.195.499 | 637.948 | 588.644 | 22,3% | 1,1% | 304,9 | `62605caf8130` |
| `legacy_gu_0_28d` | generate | legacy, `target_g_u` 0 | 28 | 0 | 4.195.499 | 629.288 | 582.590 | 22,3% | 0,0% | 304,8 | `872fac608c3b` |
| `legacy_gu_0p5_28d` | generate | legacy, `target_g_u` 0,5 | 28 | 0 | 4.195.499 | 635.190 | 587.123 | 22,5% | 0,0% | 304,9 | `a31b6e59532e` |
| `legacy_gu_2_28d` | generate | legacy, `target_g_u` 2 | 28 | 0 | 4.195.499 | 638.024 | 588.299 | 20,7% | 5,1% | 304,7 | `70bc6ba2f9bf` |
| `switchback_c1_28d` | generate | experiment, cụm 1, `p_on` 0,5, block 60 phút | 28 | 0 | 4.195.499 | 682.837 | 608.525 | 50,0% | 0,0% | 298,1 | `eef351b8d1e4` |
| `switchback_c7_28d` | generate | experiment, cụm 7 | 28 | 0 | 4.195.499 | 681.828 | 605.994 | 49,7% | 0,0% | 296,5 | `1fa408c7ff88` |
| `switchback_call_28d` | generate | experiment, cụm all | 28 | 0 | 4.195.499 | 680.419 | 598.212 | 50,2% | 0,0% | 293,4 | `be14a9d1a228` |
| `rider_ab_28d` | generate | experiment, `rider_ab`, `p_on` 0,5 | 28 | 0 | 4.195.499 | 685.239 | 612.331 | 49,9% | 0,0% | 295,4 | `f7947c059f8e` |
| `gte` | gte | all_on / all_off, không B | 1 | 0–9 | – | – | 23.235,7 / 19.371,0 | – | – | 0,04 | `ad3c146cbfa1` |
| `gte_bigfleet` | gte | như trên, `fleet_size` 6.000 | 1 | 0–9 | – | – | 37.459,0 / 25.274,1 | – | – | 0,04 | `ad4d4627d39c` |
| `all_on_5seeds_full` | evaluate | all_on, không B, log đầy đủ | 1 | 0–4 | – | – | 23.164,6 | – | – | 105,3 | `ff89b939e9db` |
| `all_off_5seeds_full` | evaluate | all_off, log đầy đủ | 1 | 0–4 | – | – | 19.336,0 | – | – | 103,6 | `79572b38077c` |
| `calibrate_budget` | calibrate_budget | all_on | 1 | 9000 | – | – | – | – | – | 0,0 | `ad3c146cbfa1` |

**W1** (`runs/s8/w1`, B = 116.125.778 đ/kỳ; pilot 387.085.927 đ)

| Thư mục | Mode | Chính sách | Ngày | Seed | Session | Order | N hoàn thành | Tỷ lệ phát | Tỷ lệ chặn | MB | `config_hash` |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `legacy_28d` | generate | legacy (`target_g_u` 1,0), chịu B | 28 | 0 | 4.237.888 | 638.473 | 585.126 | 22,3% | 0,4% | 308,3 | `0fa5835e3571` |
| `legacy_gu_0_28d` | generate | legacy, `target_g_u` 0 | 28 | 0 | 4.237.888 | 627.678 | 577.078 | 21,7% | 0,0% | 308,0 | `980547d490e4` |
| `legacy_gu_0p5_28d` | generate | legacy, `target_g_u` 0,5 | 28 | 0 | 4.237.888 | 633.525 | 581.671 | 22,0% | 0,0% | 308,2 | `07987c600105` |
| `legacy_gu_2_28d` | generate | legacy, `target_g_u` 2 | 28 | 0 | 4.237.888 | 638.623 | 584.200 | 21,1% | 3,7% | 308,0 | `16f08f2f7c71` |
| `switchback_c1_28d` | generate | experiment, cụm 1 | 28 | 0 | 4.237.888 | 682.686 | 601.126 | 49,9% | 0,0% | 301,5 | `e82de1bdebb6` |
| `switchback_c7_28d` | generate | experiment, cụm 7 | 28 | 0 | 4.237.888 | 681.136 | 599.328 | 49,5% | 0,0% | 299,9 | `14a920d6b421` |
| `switchback_call_28d` | generate | experiment, cụm all | 28 | 0 | 4.237.888 | 681.205 | 592.311 | 50,2% | 0,0% | 296,8 | `b22e7faa46b1` |
| `rider_ab_28d` | generate | experiment, `rider_ab` | 28 | 0 | 4.237.888 | 685.252 | 605.306 | 49,9% | 0,0% | 298,8 | `014daa142964` |
| `gte` | gte | all_on / all_off, không B | 1 | 0–9 | – | – | 22.944,5 / 19.248,7 | – | – | 0,04 | `00c1624c58df` |
| `gte_bigfleet` | gte | như trên, `fleet_size` 6.667 | 1 | 0–9 | – | – | 38.014,4 / 25.668,7 | – | – | 0,04 | `82f33ec317be` |
| `all_on_5seeds_full` | evaluate | all_on, không B, log đầy đủ | 1 | 0–4 | – | – | 22.817,8 | – | – | 107,7 | `cac62bb0df8d` |
| `all_off_5seeds_full` | evaluate | all_off, log đầy đủ | 1 | 0–4 | – | – | 19.306,2 | – | – | 106,0 | `ec258b2699a8` |
| `calibrate_budget` | calibrate_budget | all_on | 1 | 9000 | – | – | – | – | – | 0,0 | `00c1624c58df` |

Session và order gồm cả warm-up; N chỉ tính order đặt trong cửa sổ. Mọi bộ `generate` của một thế giới gặp cùng tập session (CRN); W1 nhiều hơn W0 1,0% session vì hệ số cầu theo ngày của 28 ngày đó trung bình > 1. Dung lượng: 2,5 GB mỗi thế giới, 4,9 GB cả hai.

**GTE** (ghép cặp theo seed 0–9, 1 ngày, không ngân sách):

| | N(all_on) | N(all_off) | GTE | SE | ETA đón on / off (phút) |
|---|---|---|---|---|---|
| W0, đội xe chuẩn (1.150) | 23.235,7 | 19.371,0 | **+3.864,7** (+20,0%) | 40,9 | 6,78 / 4,00 |
| W0, đội xe rất lớn (6.000) | 37.459,0 | 25.274,1 | **+12.184,9** (+48,2%) | 22,2 | 1,22 / 1,20 |
| W1, đội xe chuẩn (1.278) | 22.944,5 | 19.248,7 | **+3.695,8** (+19,2%) | 294,6 | 6,93 / 4,20 |
| W1, đội xe rất lớn (6.667) | 38.014,4 | 25.668,7 | **+12.345,7** (+48,1%) | 493,2 | 1,25 / 1,22 |

Phần "công suất" (kế hoạch §4.4): ở đội xe chuẩn chỉ 32% mức tăng cầu thành chuyến thêm (3.865 / 12.185); SE của W1 lớn gấp 7–20 lần W0 vì biến động theo ngày.

**Kiểm G8 "biến động có tác dụng"** (trên `market/slot_snapshots` của `legacy_28d`, 163.968 (ô, slot) trong cửa sổ mỗi thế giới): tỷ lệ (ô, slot) căng (slack < 0,35) W0 16,6% → W1 17,7%; **ngoài 5–8h W0 14,7% → W1 15,8%**; theo ngày W0 chỉ dao động 16,5–17,6% (trừ ngày đầu 1,6%), W1 2,5–28,0%; trên W1 theo loại ngày: không mưa, không sự kiện 11,6% (7 ngày), chỉ sự kiện 17,8% (14), chỉ mưa 18,4% (1), mưa + sự kiện 21,3% (7). Biến động tạo căng rõ ở ngày có mưa/sự kiện; mức tăng trung bình 28 ngày là +1,1 điểm vì W0 đã hiệu chỉnh không còn dồn căng vào 5–8h (T-42). Lịch biến động 28 ngày của W1: 8 ngày mưa, 22 sự kiện.

**Thêm cho T10.6 (14/10, W0):** `all_on_oldshift_5seeds_full`, `all_off_oldshift_5seeds_full` = hai lệnh `evaluate` ở trên với `--set "supply.shift_start_mixture=[[6.0,8.5,0.30],[10.0,14.0,0.20],[15.0,19.0,0.35],[19.0,23.0,0.15]]"` (lịch ca trước T-42, mọi thứ khác giữ nguyên); N `all_on` 21.413,2, `all_off` 18.348,2. Dùng với `analysis.theta.on_off_by_hour_runs` để so tác hại theo giờ trước/sau khi sửa lịch ca (số trong log T10.6).

**`check_budget`** trên `legacy_28d`: W0 mọi kỳ chi ≤ B, 47.285 session bị chặn (1,7% session ở ô bật), đều `arm = 0`, `voucher = 0`; W1 mọi kỳ ≤ B, 17.853 bị chặn (0,7%). Bảng đầy đủ: `runs/s8/check_budget.txt`, `runs/s8/check_dataset.md`.

**Thời gian chạy** (8 lõi, `runs/s8/times.txt`): mỗi `generate` 28 ngày 935–1.183 s khi chạy 8 lượt song song (33–42 s/ngày/tiến trình); `gte` 85–88 s; `gte_bigfleet` 128–134 s; `evaluate` 5 seed log đầy đủ 32–37 s; `calibrate_budget` 19 s. Tổng từ lệnh đầu tới lệnh cuối: **46,8 phút** (ĐX mục E ước 1,1 giờ).

---

## Dữ liệu Sprint 9 (T-44: Tình làm S9; 14–15/10/2026, `runs/s9`): phương pháp trên W0 và W1

Mọi bước đọc `runs/s8` (B11) và chỉ `observed/`, `market/`, `results/`; không sinh dữ liệu mới. Mô hình và luật được commit trong `analysis/models/` (để chính sách nạp trong tiến trình con); bảng kết quả trong `runs/s9/<world>/` (không commit). W1 dùng `config/default.yaml` + `config/world_w1.yaml`.

### Lệnh sinh lại (bash, từ thư mục gốc repo)

```bash
PY=.venv/Scripts/python.exe
# H9.1 — DR-learner theo 1.000đ, fit lại trên luật cũ 28 ngày của từng thế giới (mô hình cũ trước B9 là USD)
$PY -m analysis.uplift fit runs/s8/w0/legacy_28d                                                    # dr_explore, dr_all
$PY -m analysis.uplift fit runs/s8/w1/legacy_28d config/default.yaml config/world_w1.yaml --suffix w1   # dr_explore_w1, dr_all_w1
# H9.2 — chấm 7 chỉ số độ căng (H-22) trên all_off 5 seed log đầy đủ
$PY -m analysis.tension runs/s8/w0/all_off_5seeds_full                                 > runs/s9/tension_w0.txt
$PY -m analysis.tension runs/s8/w1/all_off_5seeds_full config/default.yaml config/world_w1.yaml > runs/s9/tension_w1.txt
# H9.3 — đường tác dụng theo độ căng trước khối, ngưỡng theo độ căng so với theo giờ (A/B), giờ có hại, phân bố độ căng
for w in w0 w1; do
  $PY -m analysis.tension_curve --world $w --switchback runs/s8/$w/switchback_call_28d --budget runs/s8/$w/calibrate_budget \
      --all-off runs/s8/$w/all_off_5seeds_full --all-on runs/s8/$w/all_on_5seeds_full --legacy runs/s8/$w/legacy_28d --out runs/s9/$w
  # H9.4 — luật tắt theo giờ: giờ có hại của H9.3 vào analysis/models/hour_rule_<w>.json (commit)
  $PY -m analysis.hour_rule set $w --from runs/s9/$w/summary.json
  # H9.5 — thác nước ba nguồn sai số; H9.6 — burn-in so với ảnh hưởng kéo dài
  $PY -m analysis.interference waterfall --legacy runs/s8/$w/legacy_28d --rider-ab runs/s8/$w/rider_ab_28d \
      --gte runs/s8/$w/gte --gte-big runs/s8/$w/gte_bigfleet --out runs/s9/$w
  $PY -m analysis.interference burnin --switchback-all runs/s8/$w/switchback_call_28d --gte runs/s8/$w/gte --out runs/s9/$w
done
# H9.7 — kiểm gói B12 (nạp hàm điểm trong tiến trình con hai lần, file giờ có hại, thác nước cộng khớp)
$PY -m analysis.b12_check --runs runs/s9
```

### Kết quả (14/10/2026 tối; chi tiết ở log H9.1–H9.7, decisions T-45, T-46)

| Bước | W0 | W1 | Thời gian |
|---|---|---|---|
| H9.1 DR-learner (ATE DR lên hoàn thành, lát explore / toàn bộ; chi/lượt phát) | 0,0584 ± 0,0015 / 0,0930 ± 0,0003 | 0,0593 ± 0,0015 / 0,0907 ± 0,0003; 2.989đ / 3.413đ | 786 s / 1.239 s |
| H9.2 chỉ số độ căng tốt nhất (R² ETA / bắt ca tệ) | `hour` 0,213 / 0,308; `system` 0,188 / 0,316; `ring1` 0,187 / 0,308 | `system` 0,269 / 0,488; `ring1` 0,255 / 0,478; `hour` 0,248 / 0,394 | < 1 phút |
| H9.3 θ̂_tension `ring1` [CI]; θ̂_B `ring1`; θ̂_B `city` | 0,00 [0; 0,32]; 2,3; 1,6 | 0,00 [0; 0,18]; 2,15; 2,1 | 44 s / 39 s |
| H9.3 slot toàn thành phố căng (< 0,35): `all_off` / `legacy` / `all_on` | 0,4% / 4,2% / 32,1% | 5,4% / 6,5% / 34,0% | |
| H9.4 giờ có hại (τ̂(h) cận trên CI < 0) | [8, 17, 18, 19] | [18] | |
| H9.5 thác nước: so thô → mức mỗi khách → GTE → GTE đội xe rất lớn (chuyến/ngày) | +13.453 → +9.217 → +3.865 → +12.185 | +14.106 → +9.120 → +3.696 → +12.346 | 499 s / 427 s |
| H9.5 gây nhiễu; tranh chấp xe; đội xe giữ lại | +4.237 [3.885; 4.557]; +5.352 [5.087; 5.642]; 32% | +4.986 [4.665; 5.340]; +5.424 [4.812; 6.052]; 30% | |
| H9.6 burn-in bias; ảnh hưởng kéo dài (chuyến/ngày) | +594 [483; 700]; +121 [−896; +1.266] | +590 [466; 714]; +228 [−1.044; +1.645] | 466 s / 482 s |
| H9.7 `b12_check` | OK (12 hàm điểm tất định, 8 file) | | |

Thư mục: `runs/s9/<w>/summary.{md,json}`, `curve_by_tension.parquet`, `thresholds.parquet`, `hour_effects.parquet`, `tension_distribution.parquet`, `results/waterfall_completed.parquet` (+ `_attrs.json`), `results/burnin_completed.parquet`; `runs/s9/tension_w0_w1.txt`, `tension_w1.txt`; `runs/s9/b12_manifest.json`. Commit: `analysis/models/dr_*`, `dr_*_w1`, `hour_rule_w0.json`, `hour_rule_w1.json`.

---

## Dữ liệu Sprint 10 (T10.1–T10.8, đêm 14→15/10/2026, `runs/s10`): thí nghiệm đánh giá trên W0 và W1

Chạy tách rời qua đêm (`runs/s10/times.txt`, log từng lệnh `runs/s10/logs/`), `runner.n_procs = 8`. Mỗi bảng chính sách trả thêm ~25 phút pilot κ-auto chạy tuần tự (một pilot mỗi dòng ngưỡng). Hàm điểm theo 1.000đ và ngưỡng S9 (`policy_table.S9_THRESHOLDS`, T-45); W1 dùng mô hình `_w1`.

```bash
PY=.venv/Scripts/python.exe; W0="--config config/default.yaml"; W1="$W0 --config config/world_w1.yaml"
DR0=analysis.uplift:tau_x_dr_all_per_kvnd; DR1=analysis.uplift:tau_x_dr_all_w1_per_kvnd
# T10.1 — bảng 18 dòng × 30 seed (specs_for_world: tầng rider S5 theo 1.000đ, θ̂_tension ring1, θ̂_B ring1/city, luật giờ, oracle)
$PY -m analysis.policy_table $W0 --world w0 --n-seeds 30 --out runs/s10/w0/policy_table
$PY -m analysis.policy_table $W1 --world w1 --n-seeds 30 --out runs/s10/w1/policy_table
# T10.2 — quét θ 16 mức (sweep_reference.yaml) × 30 seed, ring1 + DR/1.000đ
$PY -m sim run --mode sweep_theta $W0 --config config/sweep_reference.yaml --set sweep.n_seeds=30 --set policy.threshold.scope=ring1 --set policy.threshold.score_fn=$DR0 --out runs/s10/w0/sweep_ring1_dr
$PY -m sim run --mode sweep_theta $W1 --config config/sweep_reference.yaml --set sweep.n_seeds=30 --set policy.threshold.scope=ring1 --set policy.threshold.score_fn=$DR1 --out runs/s10/w1/sweep_ring1_dr
# T10.5 — độ vững W1: B ∈ {0,3; 0,6; 1,0} × đội xe {1.278; 1.061; 856} (100 / 83 / 67%), 3 dòng, 30 seed; và bảng 18 dòng trên 2 world_seed khác
for frac in 0.3 0.6 1.0; do for fleet in 1278 1061 856; do
  $PY -m analysis.policy_table $W1 --set budget.fraction=$frac --set supply.fleet_size=$fleet --n-seeds 30 \
     --spec label=all_on_B,policy=all_on --spec label=tier_dr_kvnd,policy=threshold,theta=0,score_fn=$DR1 \
     --spec label=pi_hourB_ring1,policy=threshold,theta=2.15,score_fn=$DR1,scope=ring1 --out runs/s10/w1/robust_B${frac}_fleet${fleet}
done; done
for ws in 20260931 20260932; do $PY -m analysis.policy_table $W1 --set meta.world_seed=$ws --world w1 --n-seeds 30 --out runs/s10/w1/policy_table_ws$ws; done
# T10.8 — độ nhạy W0, 10 seed, 5 dòng (all_on_B, random, tầng DR, θ̂_B ring1 2,3, luật giờ DR): hazard hủy ×0,5 / ×2,
#   dest_decay_rings 1,9 (~5,2 km) / 6,0, hình cầu phẳng hơn kiểu NYC (độ lệch co 50%, xấp xỉ vì không có dữ liệu TLC thô)
SPECS="--spec label=all_on_B,policy=all_on --spec label=random,policy=threshold,theta=0,score_fn=random --spec label=tier_dr_kvnd,policy=threshold,theta=0,score_fn=$DR0 --spec label=pi_hourB_ring1,policy=threshold,theta=2.3,score_fn=$DR0,scope=ring1 --spec label=hour_off_dr,policy=threshold,theta=0,score_fn=analysis.hour_rule:off_hours_dr_w0"
$PY -m analysis.policy_table $W0 --n-seeds 10 $SPECS --out runs/s10/w0/sens_base
$PY -m analysis.policy_table $W0 --set cancel.base_per_min=0.0025 --set cancel.slope_per_min2=0.002 --n-seeds 10 $SPECS --out runs/s10/w0/sens_hazard_0p5
$PY -m analysis.policy_table $W0 --set cancel.base_per_min=0.01 --set cancel.slope_per_min2=0.008 --n-seeds 10 $SPECS --out runs/s10/w0/sens_hazard_2
$PY -m analysis.policy_table $W0 --set demand.dest_decay_rings=1.9 --n-seeds 10 $SPECS --out runs/s10/w0/sens_trip_L1p9
$PY -m analysis.policy_table $W0 --set demand.dest_decay_rings=6.0 --n-seeds 10 $SPECS --out runs/s10/w0/sens_trip_L6
$PY -m analysis.policy_table $W0 --set "demand.hour_profile=[0.685,0.634,0.609,0.609,0.634,0.736,0.913,1.218,1.370,1.167,1.015,1.015,1.066,1.040,1.015,1.066,1.192,1.395,1.446,1.268,1.116,1.040,0.939,0.812]" --n-seeds 10 $SPECS --out runs/s10/w0/sens_profile_flat
# T10.7 — gây nhiễu theo u trên dữ liệu mới
for w in w0 w1; do $PY -m analysis.interference confounding runs/s8/$w/legacy_gu_0_28d runs/s8/$w/legacy_gu_0p5_28d runs/s8/$w/legacy_28d runs/s8/$w/legacy_gu_2_28d --out runs/s10/$w/confounding; done
# T10.4 — Qini trên A/B theo rider (28 ngày) so với N của bảng T10.1; oracle bỏ qua (2.187–3.372 s mỗi lượt)
for w in w0 w1; do for oc in completed requested; do $PY -m analysis.qini_vs_value --data runs/s8/$w/rider_ab_28d --table runs/s10/$w/policy_table --outcome $oc --out runs/s10/$w/qini_vs_value; done; done
# G10 / H2 vế hai — hai chính sách W1 với log đầy đủ (10 seed, cùng B và κ pilot), rồi tách lợi theo slot có mưa / sự kiện (428 s)
$PY -m analysis.policy_table $W1 --n-seeds 10 --log-level full --spec label=all_on_B,policy=all_on \
   --spec label=pi_hourB_ring1,policy=threshold,theta=2.15,score_fn=$DR1,scope=ring1 \
   --spec label=hour_off_dr,policy=threshold,theta=0,score_fn=analysis.hour_rule:off_hours_dr_w1 --out runs/s10/w1/h2_slots_full
$PY -m analysis.s10_eval shocks --table runs/s10/w1/h2_slots_full --a pi_hourB_ring1 --b hour_off_dr --out runs/s10/w1/eval_h2   # --kinds rain | event
# T11.1 — animation bản cuối (T-48): cặp W0 log đầy đủ 10 seed (238 s); W1 dùng runs/s10/w1/h2_slots_full ở trên; GIF vào docs/figures
$PY -m analysis.policy_table $W0 --n-seeds 10 --log-level full --spec label=all_on_B,policy=all_on \
   --spec label=pi_hourB_city,policy=threshold,theta=1.6,score_fn=$DR0,scope=city --out runs/s10/w0/anim_pair_full
$PY -m analysis.check_dataset runs/s10/w0/anim_pair_full/runs/* runs/s10/w1/h2_slots_full/runs/*
$PY -m analysis.animation --old runs/s10/w0/anim_pair_full/runs/*-all_on-nan-* --new runs/s10/w0/anim_pair_full/runs/*-threshold-1.6-* \
   --hours 5 9 --out docs/figures/06_anim_w0_5_9h.gif --title "W0 · tầng vùng city θ̂_B = 1,6 + τ̂(x)/1.000đ DR so với all_on có B = 113,76 triệu đ/kỳ · 10 seed, cùng seed · trong mô phỏng này"
$PY -m analysis.animation --old runs/s10/w1/h2_slots_full/runs/*-all_on-nan-* --new runs/s10/w1/h2_slots_full/runs/*-threshold-2.15-* \
   --hours 5 9 --out docs/figures/06_anim_w1_5_9h.gif --title "W1 · tầng vùng ring1 θ̂_B = 2,15 + τ̂(x)/1.000đ DR so với all_on có B = 116,13 triệu đ/kỳ · 10 seed, cùng seed · trong mô phỏng này"
# khung sự kiện: 3 seed có sự kiện trong cửa sổ theo meta/shocks (seed 1, 2, 5), vẽ slack của seed 2 (--display-seed 1)
$PY -m analysis.animation --old runs/s10/w1/h2_slots_full/runs/*-all_on-nan-{1,2,5} --new runs/s10/w1/h2_slots_full/runs/*-threshold-2.15-{1,2,5} \
   --hours 21 24 --display-seed 1 --out docs/figures/06_anim_w1_su_kien_21_24h.gif --title "W1, 3 ngày có sự kiện (meta/shocks, 21–23h) · tầng vùng ring1 θ̂_B = 2,15 so với all_on có B = 116,13 triệu đ/kỳ · 3 seed · trong mô phỏng này"
# A5-HN cho notebook 01 (máy rảnh, 1 tiến trình, 3 seed; 74 s cả pilot)
$PY -m sim run --mode evaluate $W0 --set policy.name=all_on --set sweep.n_seeds=3 --set runner.n_procs=1 --out runs/s10/w0/a5_evaluate_1proc
# Đọc kết quả (analysis/s10_eval.py): khoảng cách all_on → oracle đã lấp; regret chọn θ trên nửa seed; số lần bật/tắt; xếp chồng bảng độ vững / độ nhạy
$PY -m analysis.s10_eval gap --table runs/s10/w0/policy_table;  $PY -m analysis.s10_eval sweep --sweep runs/s10/w0/sweep_ring1_dr --theta-hat 2.3 0
$PY -m analysis.s10_eval switch --table runs/s10/w0/policy_table; $PY -m analysis.s10_eval stack --dirs "runs/s10/w1/robust_*"
```

### Kết quả (15/10/2026; thời gian thật ở `runs/s10/times.txt`; chi tiết log T10.1–T10.9, G10; kết luận T-47)

Đêm 14→15/10: T10.1 4.170 s (W0) + 4.139 s (W1); T10.2 3.126 + 3.544 s; T10.5 B × đội xe 9 bảng 568–754 s; `world_seed` 4.864 + 4.800 s; T10.8 6 bảng 691–861 s (tổng ≈ 9,3 giờ máy, phiên bị khởi động lại một lần, chạy tiếp bằng `runs/s10/run_s10_rest.sh`). Sáng 15/10: Qini 4 lượt (≈ 2,9 giờ, 2 lượt song song), `h2_slots_full` 428 s, A5 74 s. Tóm tắt dạng Markdown: `runs/s10/t10_1_t10_3_summary.md`, `t10_1_pairs.md`, `t10_2_t10_5_summary.md`, `t10_5_robust_summary.md`, `t10_5_worldseed_summary.md`, `w0/t10_8_summary.md`, `<w>/qini_<outcome>.log`, `w1/eval_h2/*.json`.

| Thí nghiệm | Thư mục | Kết quả chính (N − `all_on_B` ± SE, ghép cặp theo seed) |
|---|---|---|
| T10.1 bảng 18 dòng × 30 seed | `w0/policy_table`, `w1/policy_table` | W0 (B 113,76 triệu): tầng rider DR/1.000đ +1.190 / +1.298 (x / xs); oracle +1.279; tầng vùng `ring1` θ̂_B 2,3 +1.351; `city` 1,6 **+1.469 ± 13**; luật giờ + DR +1.447. W1 (B 116,13 triệu): +1.037 / +1.217; oracle +1.135; `ring1` 2,15 **+1.249 ± 29**; `city` 2,1 +1.251; luật giờ + DR +1.107. H1: `city` − luật giờ +22 ± 11 (W0). H2 vế một: `ring1` − luật giờ +142 ± 16 (W1) |
| T10.2 quét θ 16 mức × 30 seed | `<w>/sweep_ring1_dr`, `<w>/eval` | đỉnh θ = 1,5 (W0, +281 so với θ = 0) / 1,25 (W1, +311); tập θ\* {1,25; 1,5} / {0,8…1,5}; chọn trên nửa seed → regret 0–14; θ̂_B S9 lấy lại 44% / 62% |
| T10.3 bật/tắt | `<w>/eval/switch.parquet` | `ring1` θ̂_B 23,7 lần/ô/ngày (W0), 21,1 (W1); `city` 13,4 / 19,5; đề xuất giữ ≥ 180 phút |
| T10.4 Qini | `<w>/qini_vs_value` | Spearman(Qini, N) −0,48 / −0,55; (chuyến thêm / 100.000đ, N) 0,80 / 0,86; phần do cung (xs − x, cùng /1.000đ) +108 ± 10 / +180 ± 17 |
| T10.5 độ vững | `w1/robust_B*_fleet*`, `w1/policy_table_ws*` | B = 0,3: tầng vùng thắng tầng rider ở 3 cỡ đội xe (+212 / +69 / +56); B = 0,6: thua (−418 / −389 / −24); B = 1,0: −952 / −383 / +386. Ba `world_seed`: `ring1` − luật giờ +142 / +86 / +110 (SE 16) |
| T10.6, T10.7 | `<w>/confounding` | chệch sau điều chỉnh theo X +0,048 so với so thô +0,035 (g_u = 1); tỷ lệ bỏ chờ 0,01%; tác hại 7–8h trước/sau sửa lịch ca (log 14/10) |
| T10.8 độ nhạy (W0, 10 seed) | `w0/sens_*` | tầng vùng − tầng rider: gốc +159; hazard ×0,5 +259; ×2 +140; quãng 1,9 vành +226; 6,0 vành +152; hình cầu phẳng **−237**; luật giờ + DR cao nhất ở cả 6 |
| G10 H2 vế hai | `w1/h2_slots_full`, `w1/eval_h2` | lợi ở slot mưa / sự kiện −24 ± 17, slot yên +165 ± 54; hiệu mỗi slot −5,9 ± 3,3 (CI [−14,9; +3,2]) → không dồn vào slot có biến động |
| A5-HN (notebook 01) | `w0/a5_evaluate_1proc` | 18,5 / 18,0 / 16,8 s mỗi ngày mô phỏng, 1 tiến trình (ngưỡng 60) |
| T11.1 animation (notebook 06) | `w0/anim_pair_full`, `w1/h2_slots_full`; `docs/figures/06_anim_*.gif` | ΔN cả cửa sổ W0 +1.478,5 ± 49,2, W1 +1.208,0 ± 59,1 (10 seed); trong khung 5h–9h −33,7 / −62,1; khung sự kiện W1 (3 seed) +179,7; tổng Δ theo slot = N(mới) − N(cũ) ở mọi seed |

---

## Thống kê mô tả dữ liệu lịch sử 28 ngày (W0, W1) (20/10/2026, `docs/figures/07_*`)

Theo hướng dẫn `huong_dan_thong_ke_du_lieu.md` của Hoàng (cho 3 slide "Dữ liệu sinh ra" và mục 4.1.2 của báo cáo). Không chạy lại simulator: chỉ đọc `observed/`, `market/`, `meta/` của `runs/s8/w0/legacy_28d` và `runs/s8/w1/legacy_28d` (không đọc `hidden/`). `check_dataset` OK cả hai thư mục, số khớp mục Sprint 8 (4.195.499 / 4.237.888 session; 637.948 / 638.473 order; N 588.644 / 585.126; phát 22,3%).

### Lệnh

```bash
PY=.venv/Scripts/python.exe
$PY -m analysis.check_dataset runs/s8/w0/legacy_28d runs/s8/w1/legacy_28d --markdown
$PY -m analysis.data_stats --w0 runs/s8/w0/legacy_28d --w1 runs/s8/w1/legacy_28d --out docs/figures   # 8,4 s
```

Quy ước: mọi chỉ tiêu trong cửa sổ đánh giá (bỏ 60 phút khởi động); "mỗi ngày" = tổng / 28. Ngày của một order là **ngày cửa sổ**: từ 1h (đầu cửa sổ) đến 1h hôm sau, nên cả 28 ngày đủ 24 giờ (order không có cột `day`; theo ngày lịch thì ngày đầu thiếu 1 giờ và có thêm "ngày 29" dài 1 giờ). (ô, slot) của cửa sổ: slot bắt đầu trong cửa sổ, cùng bộ lọc với kiểm G8. Số thô chưa làm tròn: `docs/figures/07_du_lieu_thong_ke.csv` (cột `chi_tieu, w0, w1`; hai chữ số đầu của `chi_tieu` là số thứ tự chỉ tiêu).

### Bảng (W0 | W1)

| # | Chỉ tiêu | W0 | W1 |
|---|---|---|---|
| 1 | Số ô · số khách · số xe | 61 · 120.000 · 1.150 | 61 · 120.000 · 1.278 |
| 2 | Khách có ít nhất 1 phiên trong cửa sổ | 119.648 (99,7%) | 119.662 (99,7%) |
| 3 | Phiên: mỗi ngày (tổng) | 149.758 (4.193.232) | 151.275 (4.235.713) |
| 4 | Lượt đặt: mỗi ngày (tổng); tỷ lệ đặt | 22.769 (637.537); 15,2% | 22.788 (638.076); 15,1% |
| 5 | Chuyến hoàn thành: mỗi ngày (tổng = N); thấp nhất–cao nhất theo ngày | 21.023 (588.644); 20.818–21.198 | 20.897 (585.126); 16.907–23.106 |
| 6 | Lượt đặt: hoàn thành / hủy khi xe đang tới / bỏ chờ | 92,3% / 7,7% / 0,00% | 91,7% / 8,3% / 0,00% |
| 7 | Phiên được phát voucher; bị chặn vì hết ngân sách | 22,3%; 1,1% | 22,3%; 0,4% |
| 8 | Phiên thuộc lát phát ngẫu nhiên | 5,0% | 5,0% |
| 9 | Voucher đã chi mỗi ngày (triệu đ); so với B | 113,8 = 100,00% B (B = 113,8) | 115,1 = 99,14% B (B = 116,1) |
| 10 | Giá cước chuyến hoàn thành (đ) | TB 80.618 · trung vị 85.925 · p90 126.936 | TB 80.658 · trung vị 85.925 · p90 126.936 |
| 10 | Quãng đường (km) | TB 5,64 · trung vị 6,35 · p90 9,52 | TB 5,65 · trung vị 6,35 · p90 9,52 |
| 10 | Thời gian chuyến (phút) | TB 19,6 · trung vị 16,1 · p90 38,1 | TB 19,7 · trung vị 16,1 · p90 38,2 |
| 11 | ETA đón, order đã ghép (phút) | TB 4,4 · trung vị 3,3 · p90 10,6 | TB 4,6 · trung vị 3,3 · p90 10,6 |
| 12 | Chuyến có voucher chạm trần 20.000đ (trong chuyến có voucher); chuyến có voucher | 54,8%; 33,0% chuyến hoàn thành | 54,8%; 33,6% chuyến hoàn thành |
| 13 | Chuyến hoàn thành / xe / ngày | 18,3 | 16,4 |
| 14 | (ô, slot) trong cửa sổ; căng (slack < 0,35) · dư (slack > 1) | 163.968; 16,6% · 65,8% | 163.968; 17,7% · 65,3% |
| 15 | Chuyến hoàn thành / khách trong cửa sổ (gồm khách 0 chuyến) | TB 4,9 · trung vị 2,0 · p90 12,0 | TB 4,9 · trung vị 2,0 · p90 12,0 |
| 16 | Biến động: ngày mưa · sự kiện (sân vận động) · xe nghỉ/ngày · hệ số cầu ngày | không bật | 8 · 22 (4) · 111–141 · ×0,71–1,18 |
| 17 | observed/riders (dòng × cột) | 120.000 × 7 | 120.000 × 7 |
| 17 | observed/sessions | 4.195.499 × 30 | 4.237.888 × 30 |
| 17 | observed/orders | 637.948 × 24 | 638.473 × 24 |
| 17 | market/slot_snapshots | 164.334 × 31 | 164.334 × 31 |

Bỏ chờ: W0 0 lượt, W1 9 lượt / 638.076. Chỉ tiêu 17 là kích thước trên đĩa, gồm cả 60 phút khởi động và phần cool-down (164.334 = 61 ô × 2.694 slot).

### Hình (`docs/figures/`, 220 dpi; hình chính dùng W0, panel (d) có cả W1)

| File | Nội dung |
|---|---|
| `07_du_lieu_tong_quan.png` | hình 2 × 2 (12 × 8,6 inch): (a) theo giờ, (b) bản đồ, (c) giá cước, (d) theo ngày |
| `07_du_lieu_theo_gio.png` | (a) ngày trung bình: phiên/giờ; lượt đặt và chuyến hoàn thành/giờ (theo giờ đặt); tỷ lệ (ô, slot) căng theo giờ. Ba hàng chung trục giờ |
| `07_du_lieu_ban_do.png` | (b) phiên/ô/ngày theo ô đón trên 61 ô; ▲ 2 sân vận động, ■ 1 phố đi bộ (ô có sự kiện ở W1, lấy từ `meta/shocks`) |
| `07_du_lieu_gia_cuoc.png` | (c) phân bố giá cước chuyến hoàn thành (cắt ở p99 = 162 nghìn đ), vạch TB và mốc 80.000đ |
| `07_du_lieu_theo_ngay.png` | (d) chuyến hoàn thành/ngày: W0 đường xám, W1 điểm theo loại ngày (không biến động / chỉ sự kiện / chỉ mưa / mưa + sự kiện) |
| `07_du_lieu_quang_duong_eta.png` | phụ: quãng đường chuyến hoàn thành và ETA đón lúc ghép, có vạch TB |
| `07_du_lieu_gay_nhieu.png` | phụ: tỷ lệ đặt có / không voucher, phiên do luật cũ quyết định so với lát phát ngẫu nhiên (chỉ cột quan sát) |

### Nhận xét

- Kiểm: chỉ tiêu 5 bằng N của `results/policy_results` (tổng theo giờ = tổng theo ngày = N); chỉ tiêu 14 trùng kiểm G8 (163.968 (ô, slot); căng 16,6% / 17,7%); chỉ tiêu 16 trùng mục Sprint 8 (8 ngày mưa, 22 sự kiện, 111–141 xe nghỉ, ×0,71–1,18).
- Theo giờ (W0): phiên đỉnh sáng 8h (10.930/giờ), đỉnh chiều 18h (11.894/giờ); (ô, slot) căng đỉnh 8h 53,7%, 18h 52,1%, 19h 43,1%. Ở 8h có 1.327 lượt đặt/giờ nhưng chỉ 1.099 chuyến hoàn thành, ở 18h 1.359 và 1.096: hủy dồn vào giờ căng.
- Quãng đường và giá cước **rời rạc**: chuyến đi từ tâm ô đón tới tâm ô đích (lưới nối vòng), nên `trip_km` chỉ có 5 giá trị 1,48 / 3,17 / 6,35 / 9,52 / 12,70 km (cùng ô, cách 1–4 vành); chuyến trong ô (13,8%) trả giá tối thiểu 38.473đ. Vì vậy TB (80.618đ, 5,64 km) thấp hơn trung vị (85.925đ, 6,35 km). 51,9% chuyến hoàn thành có giá ≥ 80.000đ; trong chuyến có voucher, **54,8% chạm trần 20.000đ**: khoảng một nửa, không phải "phần lớn nhận đủ 25%" như hướng dẫn dự kiến. Quãng đường TB 5,64 km cao hơn mốc 5,56 km của CAL vì CAL tính trên lượt không voucher.
- Theo ngày: W0 20.818–21.198 chuyến/ngày, ngày 1 không khác thường (21.198, cao nhất, +0,8% so với TB). W1 16.907–23.106; tương quan với hệ số cầu ngày r = 0,98, với số xe nghỉ r = −0,37. TB theo loại ngày: không biến động 19.742 (6 ngày), chỉ sự kiện 21.261 (14), chỉ mưa 21.718 (1), mưa + sự kiện 21.043 (7). Ngày thấp nhất (ngày 15, 16.907 chuyến) không có mưa hay sự kiện mà có hệ số cầu 0,71. Kiểm G8 đếm theo ngày lịch nên có 7 ngày không biến động (gồm "ngày 29" dài 1 giờ); ở đây là 6.
- Gây nhiễu (W0, chỉ cột quan sát): phiên do luật cũ quyết định đặt 22,5% khi có voucher, 13,2% khi không (chênh 9,4 điểm); ở lát ngẫu nhiên là 20,1% và 13,9% (chênh 6,2 điểm). So thô trên phiên của luật cũ lớn gấp 1,5 lần chênh lệch không gây nhiễu, vì luật cũ chọn khách vốn hay đặt xe (W1: 9,6 so với 6,4 điểm).
- Chuyến/xe/ngày: W0 18,3; W1 16,4 vì chia cho cả đội 1.278 xe, gồm trung bình 126,7 xe nghỉ mỗi ngày. Mốc CAL 16,8 tính trên lượt không voucher.
- Lệch hướng dẫn: (1) panel (a) dùng ba hàng chung trục giờ thay cho trục phụ bên phải (quy ước `analysis/plots.py`: không hai thang y trên một trục); (2) panel (a) tô màu cam cho tỷ lệ căng, chuyến hoàn thành màu xanh, phiên và lượt đặt màu xám; (3) ngày tính theo cửa sổ như trên; (4) `analysis.plots.save_figure` thêm tham số `dpi` (mặc định giữ 150) để lưu 220 dpi.
