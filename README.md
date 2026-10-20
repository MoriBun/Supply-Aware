# Supply-Aware Promotion Uplift

Simulator agent-based cho thị trường gọi xe (chuyến solo, không đi chung) trên lưới ô lục giác quy mô Hà Nội, kèm bộ phân tích để trả lời một câu hỏi vận hành: **khi nào nên tắt voucher vì thiếu xe?**

Voucher kéo thêm lượt đặt, nhưng ở ô và giờ đang thiếu xe thì lượt đặt thêm làm ETA đón dài ra, khách hủy nhiều hơn, và tiền voucher không đổi thành chuyến. Mô hình uplift chuẩn đo tác dụng trên từng khách trong một thị trường cố định nên bỏ qua hiệu ứng tranh chấp xe này. Repo này:

- sinh dữ liệu quan sát có **confounding kiểm soát được** (chính sách cũ nhắm khách theo một biến ẩn) và dữ liệu thí nghiệm switchback / A/B;
- tính **giá trị thật** của một chính sách voucher: **N(π) = số chuyến hoàn thành** (chỉ tiêu chính) và V(π) = lợi nhuận nền tảng (chỉ tiêu phụ), mọi chính sách chịu **cùng ngân sách B**;
- tìm **ngưỡng căng cung θ\*** để tắt khuyến mãi theo ô và theo slot 15 phút, rồi so với ngưỡng θ̂ ước lượng từ dữ liệu quan sát.

Simulator tất định theo (config, seed): cùng `config_hash` và cùng seed cho cùng kết quả; đổi chính sách không làm đổi số ngẫu nhiên của bất kỳ session nào (CRN), nên hiệu giữa hai chính sách đo được trên cùng một "ngày".

## Mục lục

- [Cài đặt](#cài-đặt)
- [Chạy nhanh](#chạy-nhanh)
- [Mô hình](#mô-hình)
- [Chính sách](#chính-sách)
- [Các mode chạy](#các-mode-chạy)
- [Cấu hình](#cấu-hình)
- [Dữ liệu đầu ra](#dữ-liệu-đầu-ra)
- [Kết quả chính](#kết-quả-chính)
- [Tái lập kết quả](#tái-lập-kết-quả)
- [Notebook và phân tích](#notebook-và-phân-tích)
- [Dashboard](#dashboard)
- [Kiểm thử](#kiểm-thử)
- [Cấu trúc thư mục](#cấu-trúc-thư-mục)

## Cài đặt

Yêu cầu Python **3.11**. Phụ thuộc lõi của `sim/`: numpy, pandas, pyarrow, pyyaml.

```powershell
git clone <url-repo> Supply-Aware-Promotion-Uplift
cd Supply-Aware-Promotion-Uplift
py -3.11 -m venv .venv
.venv\Scripts\activate                 # Git Bash: source .venv/Scripts/activate; Linux/macOS: source .venv/bin/activate
python -m pip install --upgrade pip
pip install -e ".[dev]"                # simulator + pytest
```

| Extra | Gói thêm | Dùng cho |
|---|---|---|
| `dev` | pytest | chạy test |
| `analysis` | matplotlib, lightgbm, ipykernel | `analysis/`, `notebooks/` |
| `dashboard` | fastapi, uvicorn, httpx | `dashboard/` |

Cài đủ: `pip install -e ".[dev,analysis,dashboard]"`. Dashboard dùng React qua `htm`, thư viện đã vendor sẵn nên không cần Node. `sim/` không bao giờ import từ `analysis/` hay `dashboard/`.

## Chạy nhanh

```bash
# Một lượt nhỏ (7 ô, 10 xe, cửa sổ 2 giờ), vài giây; in N_mean, V_mean, spent_mean
python -m sim run --mode evaluate --config config/default.yaml --config tests/fixtures/tiny.yaml --out runs/smoke

# Test nhanh
pytest -q
```

Một ngày mô phỏng quy mô Hà Nội (61 ô, 1.150 xe, 120.000 khách) mất khoảng 17–19 giây trên một tiến trình.

## Mô hình

### Không gian và thời gian

- **Lưới:** lục giác bán kính 4 → 61 ô, mỗi ô cỡ ô H3 độ phân giải 7 (cạnh 1,41 km, tổng ≈ 315 km²), nối biên kiểu **torus** để không có ô rìa. Ma trận khoảng cách vành `D[a, b]` và thời gian đi `T[a, b, h]` (phút) tính trước theo tốc độ từng giờ `v_h = base_speed_kmh × speed_factor_by_hour[h]` (18 km/h nhân hệ số giờ):
  `T[a, b, h] = detour_factor × D[a, b] × √3·edge_km / v_h × 60`.
- **ETA đón trong ô có I ≥ 1 xe rảnh** (khoảng cách kỳ vọng tới xe gần nhất trong trường Poisson):
  `ETA_in(I, h) = max(eta_floor_min, detour_factor × nn_const × (A / I)^γ / v_h × 60)`, với A là diện tích ô và γ = 0,5.
- **Thời gian:** tick 60 giây, slot 15 phút (96 slot/ngày). Một lượt gồm warm-up 60 phút, **cửa sổ đánh giá** (mặc định 1 ngày) và cool-down tối đa 120 phút. N(π) và V(π) chỉ đếm order có thời điểm đặt nằm trong cửa sổ.

### Vòng lặp một tick

```
1. trips.advance       xe tới điểm đón / trả; xe điều chuyển tới nơi
2. supply.update       vào ca, hết ca (chỉ rời khi rảnh); biến động W1
3. cancel.expire       order chờ quá max_wait -> Abandoned
4. demand.spawn        session mới (dừng sau cửa sổ)
5. pricing.quote       giá, ETA báo, quyết định voucher, giữ ngân sách
6. choice.decide       đặt xe -> order Waiting; không đặt -> nhả ngân sách
7. matching.match      ghép FIFO, lập lịch đón
8. cancel.en_route     hủy trong lúc xe đang tới
9. reposition.step     xe rảnh lâu dịch sang ô kề
10. monitor.accumulate cộng dồn chỉ số ô; hết slot thì công bố snapshot
```

### Cầu và khách

- **Session** (khách mở app) mỗi (ô z, tick) ~ Poisson với `λ_z(t) = base_sessions_per_cell_h × demand_scale × w_z × hour_profile[h] × tick_s / 3600`, trọng số ô `w_z ~ LogNormal` cố định theo `world_seed`.
- **Khách:** 120.000 người, sinh một lần theo `world_seed`. Đặc trưng quan sát được: `x_freq`, `x_tenure`, `x_segment`, `home_cell`. Biến ẩn: `u_latent ~ N(0, 1)`. Hệ số hành vi riêng từng khách:
  `α_i = α0 + α_freq·z(x_freq) + α_u·u_latent + ε`, `β_price,i < 0` (theo phân khúc), `β_eta,i < 0`, `δ_i = δ0 + δ_u·u_latent + δ_seg + ε` (tác dụng voucher), `max_wait_i ~ LogNormal` (mode 5 phút).
- **Ô đích:** `P(d | o) ∝ w_d × exp(−D[o, d] / dest_decay_rings)`.
- Mọi số ngẫu nhiên của một session (`u_book`, `u_target`, `u_explore`, `u_explore_arm`, `trip_noise`, `e_cancel`, `u_score`) được **rút sẵn, luôn đủ, theo thứ tự cố định**, bất kể chính sách.

### Giá, voucher và ngân sách

- **Giá gốc** (bảng giá GrabCar Hà Nội, tiền VND): `p = open_fare + per_km × max(0, km − 2) + per_min × T_sau_2km + platform_fee`.
- **Voucher:** 25% giá gốc, trần 20.000đ. Khách trả `p − v`.
- **Thanh toán khi hoàn thành:** tài xế nhận `(1 − commission_rate) × (p − platform_fee)`; lợi nhuận nền tảng = `p − v − phần tài xế`.
- **Sổ ngân sách:** `reserved` (giữ khi phát) → `committed` (khách đặt) → `spent` (chuyến hoàn thành); nhả khi khách không đặt, bỏ chờ hoặc hủy. Chỉ phát khi `spent + committed + reserved + v ≤ B`; bất biến `spent + committed + reserved ≤ B` luôn đúng. Mỗi ngày của cửa sổ là một kỳ ngân sách với cùng mức B.
- **Mức B** (`budget.mode = fraction_of_all_on`): `B = 0,3 ×` chi tiêu voucher mỗi kỳ của một lượt pilot `all_on` không ngân sách. B tính một lần cho mỗi thế giới và dùng chung cho mọi chính sách, θ và seed.

### Quyết định đặt xe

```
logit = α_i + β_price,i × (p − v) + β_eta,i × ETA_báo + δ_i × 1[v > 0]
P(đặt) = 1 / (1 + exp(−logit)),   đặt nếu u_book < P
```

Bảng ẩn ghi thêm ground truth trong cùng bối cảnh: `p_request_treat`, `p_request_control` và hiệu `direct_request_effect_fixed_market`.

### Ghép xe, chuyến và hủy

- **Ghép:** duyệt order chờ theo thời điểm đặt (FIFO). Ô khách còn xe rảnh thì lấy xe rảnh lâu nhất, ETA = `ETA_in`. Ô trống thì mở rộng dần từng vành; ở vành gần nhất còn xe, lấy ô có `T` nhỏ nhất (hòa thì ô nhiều xe rảnh nhất). Không ghép nếu ETA > `max_pickup_eta_min` = 30 phút; order chờ tiếp. Giới hạn này cố ý đặt cao để giữ hiện tượng **wild goose chase** (xe đi đón xa, càng thiếu xe).
- **Chuyến:** thời gian chuyến = `T[đón, trả, giờ đón] × trip_noise`.
- **Bỏ chờ:** order chưa ghép sau `max_wait_i` → `Abandoned`.
- **Hủy khi xe đang tới:** hazard `h = base_per_min + slope_per_min2 × max(0, ETA − eta_free_min)`; khách hủy đúng lúc `h × (t − matched_time) / 60` vượt ngưỡng rút sẵn `e_cancel ~ Exp(1)`. Xe hủy dở dừng lại tại chỗ (ô xuất phát hoặc ô khách theo luật nửa ETA).

### Cung và điều chuyển

- **Đội xe** W0: 1.150 xe. Giờ vào ca rút từ hỗn hợp nhiều khung (sáng sớm, trưa, chiều, tối), độ dài ca theo phân phối chuẩn có chặn, lặp lại mỗi ngày. Xe hết ca chỉ rời khi đang rảnh.
- **Cung độc lập với chính sách:** rời sớm theo thu nhập (`supply.early_exit_enabled`) mặc định tắt; điều chuyển chỉ dùng trọng số tĩnh `w_z` (xe rảnh quá `max_idle_min` dịch sang ô kề), không đọc cầu hay snapshot.

### Theo dõi thị trường

Mỗi (ô, slot), monitor lấy trung bình theo tick: I (xe rảnh), E (xe đang đi đón khách của ô), O (xe đang chở), W (order chờ), rồi tính

- **slack = I / E** (Castillo et al.); E = 0 thì `+inf` nếu I > 0 và **0 nếu I = 0** (ô không có xe rảnh là ô căng);
- `utilization = (E + O) / (I + E + O)`; các bộ đếm session, offer, request, matched, hủy, hoàn thành, voucher.

Snapshot của slot k chỉ được công bố khi slot k kết thúc; chính sách ở slot k chỉ thấy snapshot của các slot < k (`SnapshotView`, có assert).

### Hai thế giới: W0 và W1

- **W0** (`config/default.yaml`): thế giới đã hiệu chỉnh, không có biến động ngoài.
- **W1** (`config/default.yaml` + `config/world_w1.yaml`): W0 thêm **biến động tự nhiên** rút trước theo `run_seed`: hệ số cầu theo ngày, mưa (cầu tăng, xe chạy chậm, một phần tài xế nghỉ), sự kiện tại sân vận động / phố đi bộ (cầu tăng ở ô địa điểm và 6 ô kề sau giờ tan), tài xế nghỉ theo ngày và lệch giờ vào ca. Đội xe 1.278 = 1.150 / 0,90 để số xe đi làm trung bình bằng W0. Chính sách không đọc được lịch biến động; lịch ghi ở `meta/shocks`.

### Ngẫu nhiên chung (CRN)

Mọi phép rút đi qua `rng.rng_for(stream, *key)`: một `Generator(Philox)` khóa theo hash của `run_seed`, luồng và một khóa ổn định. Không dùng `np.random.*` toàn cục hay `random`.

| Luồng | Khóa | Dùng cho |
|---|---|---|
| WORLD | `world_seed` | trọng số ô, khách, tài xế, ô địa điểm (không phụ thuộc `run_seed`) |
| DEMAND | `(day, tick_of_day, cell)` | số session Poisson |
| SESSION | `session_id` | khách, ô đích và các số rút sẵn của session |
| CELLSLOT | `(kind, cell/cluster, slot/block)` | ε của chính sách cũ, switchback |
| RIDER | `rider_id` | nhánh A/B theo khách |
| DRIVER | `(driver_id, counter)` | điều chuyển |
| SHOCK | `(kind, day, …)` | lịch biến động W1 |

## Chính sách

Chọn bằng `policy.name`. Chính sách chỉ đọc cột quan sát được qua `SessionBatch` và snapshot quá khứ qua `SnapshotView`; ngân sách được áp ở lớp voucher của `pricing.py` theo thứ tự `session_id`, như nhau cho mọi chính sách.

| Tên | Mô tả |
|---|---|
| `all_on`, `all_off` | phát cho mọi session / không phát. `all_on` có B là mốc so sánh chính; không B dùng cho GTE |
| `legacy` | chính sách cũ sinh dữ liệu quan sát. Cấp ô: bật khi slack slot trước ≥ `slack_on`, với xác suất ε thay bằng tung đồng xu. Cấp khách: phát với xác suất `σ(g0 + g_freq·z(x_freq) + g_u·u_latent)`, tức **nhắm theo biến ẩn** → confounding. Lát **explore** 5% session phát ngẫu nhiên với propensity biết trước. Có áp ngân sách |
| `threshold` | **π<sub>θ</sub> hai tầng.** Tầng ô: dự báo độ căng ŝ (persistence = slack slot trước, hoặc AR với slack cùng slot hôm trước), phạm vi `cell` / `ring1` (ô và 6 ô kề) / `city` (cả thành phố); ô bật khi **không** `ŝ < θ` (θ = 0: không cắt ô nào; có thể thêm trễ `hysteresis_h`). Tầng khách: phát khi ô bật và `score ≥ κ`, trong ngân sách |
| `experiment` | thí nghiệm không ngân sách: `cluster_switchback` (cụm 1 ô / 7 ô / toàn thành phố, block 60 phút, `p_on` 0,5), `global_switchback`, `rider_ab` (nhánh cố định theo khách). Ghi cờ `in_burnin` cho đầu block |

**Hàm điểm** (`policy.threshold.score_fn`): `random` (số rút sẵn `u_score`), `heuristic_low_freq` (`−x_freq`), `oracle` (mốc trên: lượt đặt thêm trên mỗi 1.000đ voucher tính từ hệ số thật, chỉ simulator dùng được), hoặc chuỗi `"module:function"` với chữ ký `f(batch, s_hat) -> ndarray`, ví dụ `analysis.uplift:tau_x_dr_all_per_kvnd` (DR-learner đã học, mô hình trong `analysis/models/`) và `analysis.hour_rule:off_hours_dr_w0` (luật tắt voucher theo giờ có hại).

**κ = auto:** chạy pilot không ngân sách (vẫn cắt ô theo θ), sắp session được phát theo điểm giảm dần, chọn κ nhỏ nhất để tổng chi ≤ B (cùng điểm thì giữ hoặc bỏ cùng nhau), lặp lại pilot tại κ mới đến khi chi ≤ B hoặc hết `kappa_max_iter`. Mọi θ dùng chung seed pilot.

## Các mode chạy

```bash
python -m sim run --mode <mode> --config config/default.yaml [--config overlay.yaml ...] \
    [--set key.sub=value ...] [--out DIR] [--log-level minimal|full] [--profile]
```

| Mode | Làm gì | Bảng kết quả chính |
|---|---|---|
| `generate` | chạy `generate.days` ngày liên tục với một chính sách (thường `legacy` hoặc `experiment`), ghi đủ dữ liệu | `observed/`, `market/`, `hidden/`, `results/policy_results` |
| `evaluate` | một chính sách × `sweep.n_seeds` seed: N(π), V(π), chi voucher | `results/policy_results` |
| `sweep_theta` | π<sub>θ</sub> với mọi θ trong `sweep.theta_grid`, cùng B và cùng seed; θ\* = argmax N(π<sub>θ</sub>) | `results/theta_sweep` |
| `gte` | GTE = N(all_on) − N(all_off), không ngân sách, ghép cặp theo seed | `results/policy_results` |
| `calibrate_budget` | pilot `all_on` để tính B | `meta/run_metadata` (`budget_B_vnd`) |
| `throughput_curve` | quét `throughput.demand_scale_grid` với mọi xe online suốt lượt, cầu và tốc độ hằng số tại giờ tham chiếu (kiểm A1) | `results/throughput_curve` |

| Tham số | Ý nghĩa |
|---|---|
| `--config` | file YAML; lặp lại để chồng lớp, file sau ghi đè file trước; file đầu luôn là `config/default.yaml` |
| `--set a.b=c` | ghi đè một khóa, giá trị parse như YAML |
| `--out` | thư mục kết quả; mặc định `runs/<mode>-<config_hash>` |
| `--log-level full` | `evaluate`, `sweep_theta`, `gte` ghi thêm bảng session / order |
| `--profile` | in thời gian từng bước của engine |

Các seed chạy song song với `runner.n_procs` tiến trình (mặc định = số lõi), khởi động kiểu `spawn`, nên hàm điểm phải import được trong tiến trình con.

```bash
# pi_theta, theta = 0.4, 5 seed, giữ bảng session/order
python -m sim run --mode evaluate --config config/default.yaml --set policy.name=threshold \
    --set policy.threshold.theta=0.4 --set sweep.n_seeds=5 --log-level full --out runs/eval_theta04

# Chính sách cũ dưới ngân sách B trên thế giới W1
python -m sim run --mode evaluate --config config/default.yaml --config config/world_w1.yaml \
    --set policy.name=legacy --out runs/eval_legacy_w1

# Quét theta trên lưới tham chiếu (0 … 30), phạm vi ring1, điểm DR
python -m sim run --mode sweep_theta --config config/default.yaml --config config/sweep_reference.yaml \
    --set policy.threshold.scope=ring1 --set policy.threshold.score_fn=analysis.uplift:tau_x_dr_all_per_kvnd \
    --out runs/sweep_ring1

# 28 ngày dữ liệu switchback cụm 7 ô
python -m sim run --mode generate --config config/default.yaml --set policy.name=experiment \
    --set experiment.cluster_level=7 --out runs/switchback_c7_28d
```

## Cấu hình

- `config/default.yaml` chứa **mọi** tham số (thế giới W0); mỗi khóa có chú thích nguồn: `[design]` lựa chọn thiết kế của mô hình, `[data]` số liệu thực tế (bảng giá Grab, TomTom, NYC TLC…), `[paper]` bài báo, `[suy]` tính ra từ số liệu công bố, `[assume]` giả định của nhóm, `[hiệu chỉnh]` chỉnh để đạt mục tiêu CAL. Code không hard-code hằng số nào.
- Lớp phủ: `config/world_w1.yaml` (thế giới W1), `config/sweep_reference.yaml` (lưới θ tới 30), `tests/fixtures/tiny.yaml` (7 ô, 10 xe, cửa sổ 2 giờ cho test nhanh).
- Khóa lạ trong YAML hoặc `--set` gây lỗi ngay. `config_hash` = 12 ký tự đầu sha1 của cấu hình đã chuẩn hóa, ghi vào mọi output; W0 hiện hành có `config_hash = 514d0b41c378`, W1 `c75e4b8c004a`.
- Ràng buộc: `warmup_min` là bội của `experiment.block_min`; cửa sổ ≤ 1 ngày hoặc là bội của 1 ngày. Độ dài cửa sổ và kỳ ngân sách lấy qua `config.eval_window_min(cfg)` và `config.budget_period_min(cfg)`.
- Công cụ hiệu chỉnh một vòng: `python -m analysis.calibrate --config config/default.yaml [--set núm=giá_trị] --seeds 3` in các chỉ tiêu CAL (xem [Kiểm thử](#kiểm-thử)).

## Dữ liệu đầu ra

Mỗi lượt ghi một thư mục Parquet. Định nghĩa từng cột và **danh sách cột ẩn**: [`docs/schema.md`](docs/schema.md).

```
runs/<out>/
  meta/run_metadata.parquet         config đầy đủ, config_hash, seed, mode, budget_B_vnd, git_sha, thời gian
  meta/shocks.parquet               lịch biến động (W1)
  observed/riders.parquet           đặc trưng quan sát được của khách
  observed/sessions.parquet         một dòng mỗi session: giá, ETA báo, voucher, cơ chế gán, propensity
  observed/orders.parquet           một dòng mỗi order: trạng thái, ETA đón, thời điểm, tiền
  market/slot_snapshots.parquet     I, E, O, W, slack, utilization, bộ đếm theo (ô, slot)
  hidden/riders_hidden.parquet      ground truth: u_latent, hệ số hành vi
  hidden/sessions_hidden.parquet    propensity thật, p_request_*, số rút sẵn
  results/policy_results.parquet    một dòng mỗi (chính sách, seed): N_completed, V_profit_vnd, voucher_spent_vnd
  results/theta_sweep.parquet       chỉ sweep_theta
  results/throughput_curve.parquet  chỉ throughput_curve
```

`hidden/` chỉ để **đánh giá**: chính sách và mọi ước lượng trong `analysis/` không được đọc nó (`analysis.io.load_run` không bao giờ trả bảng ẩn). `runs/` không commit. Kiểm tra một bộ vừa sinh:

```bash
python -m analysis.check_dataset runs/<out> [--markdown]   # schema, rò cột ẩn, tổng hợp
python -m analysis.check_budget runs/<out>                 # bất biến spent + committed + reserved <= B theo kỳ
```

## Kết quả chính

Số dưới đây là kết quả **trong mô phỏng này**, mỗi ngày một kỳ ngân sách, B = 0,3 × chi tiêu pilot `all_on` (W0: 113,76 triệu đ/kỳ; W1: 116,13 triệu đ/kỳ), 30 seed ghép cặp theo seed (± là sai số chuẩn của hiệu).

| | W0 | W1 |
|---|---|---|
| GTE: phát cho mọi người, không B (chuyến/ngày) | +3.865 (+20,0%) | +3.696 (+19,2%) |
| GTE với đội xe rất lớn (không còn ô căng) | +12.185 (+48,2%) | +12.346 (+48,1%) |
| Chỉ tầng khách, điểm DR τ̂(x)/1.000đ, so với `all_on` có B | +1.190 | +1.037 |
| Mốc trên theo τ cá nhân (oracle) | +1.279 | +1.135 |
| π<sub>θ</sub> tầng vùng + DR, θ̂_B ước lượng từ dữ liệu | **+1.469 ± 13** (`city`, θ̂ = 1,6) | **+1.249 ± 29** (`ring1`, θ̂ = 2,15) |
| Luật tắt theo giờ có hại + DR | +1.447 | +1.107 |

- **Ràng buộc cung quyết định:** ở đội xe chuẩn chỉ khoảng 32% lượt cầu tăng thêm thành chuyến. Cắt voucher ở vùng căng thắng cả mốc oracle của chính sách theo từng khách, vì oracle chỉ tối ưu tác dụng trong một thị trường cố định.
- **H1** (W0: chính sách theo độ căng ngang luật tắt theo giờ): đúng, hiệu +22 ± 11. **H2** (W1: chính sách theo độ căng thắng luật giờ và lợi tập trung ở slot có mưa / sự kiện): chỉ đúng vế đầu (+142 ± 16, giữ trên ba `world_seed`); lợi lại nằm ở slot yên (+165 ± 54) chứ không ở slot có biến động (−24 ± 17).
- **Qini không dự báo được N:** hệ số Spearman giữa Qini và N(π) là −0,48 (W0) / −0,55 (W1); chỉ tiêu "chuyến thêm trên 100.000đ voucher" tương quan 0,80 / 0,86.
- **θ̂ phụ thuộc B:** trên W1 với B = 0,6 × pilot, tầng vùng thua tầng khách ở cả ba cỡ đội xe; trên W0 với hình cầu theo giờ phẳng kiểu NYC cũng thua (−237). Ngưỡng phải chọn lại cho đúng B vận hành (quét θ trên nửa seed, kiểm trên nửa còn lại).
- **Bật/tắt nhiều:** trên W0 khoảng 24 lần/ô/ngày với `ring1`, 13 với `city`; triển khai thật nên giữ trạng thái tối thiểu khoảng 180 phút.

Hình: [`docs/figures/`](docs/figures/) (01–05 từ notebook, 06 animation, 07 thống kê dữ liệu).

![Animation W0: tầng vùng city so với all_on có B, 5h–9h](docs/figures/06_anim_w0_5_9h.gif)

## Tái lập kết quả

Toàn bộ dữ liệu trong `runs/` được sinh lại bằng các lệnh dưới đây, chạy từ thư mục gốc repo trong Git Bash, theo đúng thứ tự (bước sau đọc kết quả bước trước). Thời gian đo trên máy 8 lõi, 16 GB.

```bash
PY=.venv/Scripts/python.exe          # Linux/macOS: PY=.venv/bin/python
W0="--config config/default.yaml"
W1="$W0 --config config/world_w1.yaml"
```

### Bước 1: dữ liệu lịch sử và thí nghiệm, `runs/s8` (≈ 47 phút, 4,9 GB)

`world_seed = 20260930`, `run_seed = 0` cho mọi bộ `generate` 28 ngày, nên mọi bộ của một thế giới gặp cùng tập session.

```bash
for w in w0 w1; do
  if [ "$w" = w0 ]; then C="$W0"; else C="$W1"; fi
  $PY -m sim run --mode generate $C --set policy.name=legacy --out runs/s8/$w/legacy_28d
  for gu in 0 0.5 2; do   # mức nhắm theo u_latent: legacy_gu_0 / legacy_gu_0p5 / legacy_gu_2 (legacy_28d là 1,0)
    $PY -m sim run --mode generate $C --set policy.name=legacy --set policy.legacy.target_g_u=$gu \
        --out runs/s8/$w/legacy_gu_$(echo $gu | tr . p)_28d
  done
  for lvl in 1 7 all; do
    $PY -m sim run --mode generate $C --set policy.name=experiment --set experiment.cluster_level=$lvl \
        --out runs/s8/$w/switchback_c${lvl}_28d
  done
  $PY -m sim run --mode generate $C --set policy.name=experiment --set experiment.design=rider_ab --out runs/s8/$w/rider_ab_28d
  $PY -m sim run --mode calibrate_budget $C --out runs/s8/$w/calibrate_budget
  $PY -m sim run --mode gte $C --out runs/s8/$w/gte
  for pol in all_on all_off; do
    $PY -m sim run --mode evaluate $C --set policy.name=$pol --set budget.enforce=false --set sweep.n_seeds=5 \
        --log-level full --out runs/s8/$w/${pol}_5seeds_full
  done
done
$PY -m sim run --mode gte $W0 --set supply.fleet_size=6000 --out runs/s8/w0/gte_bigfleet
$PY -m sim run --mode gte $W1 --set supply.fleet_size=6667 --out runs/s8/w1/gte_bigfleet
$PY -m sim run --mode throughput_curve $W0 --out runs/cal_s8/a1_w0_grid25          # đường throughput A1

$PY -m analysis.check_dataset runs/s8/w0/* runs/s8/w1/* --markdown
```

### Bước 2: phương pháp ước lượng, `runs/s9` (≈ 1 giờ)

Chỉ đọc `runs/s8` (bảng quan sát và thị trường). Hai lệnh `uplift fit` và `hour_rule set` **ghi đè** mô hình đã commit trong `analysis/models/`; bỏ qua chúng nếu chỉ muốn dùng mô hình có sẵn.

```bash
mkdir -p runs/s9
$PY -m analysis.uplift fit runs/s8/w0/legacy_28d                                                       # DR-learner: dr_explore, dr_all
$PY -m analysis.uplift fit runs/s8/w1/legacy_28d config/default.yaml config/world_w1.yaml --suffix w1   # dr_explore_w1, dr_all_w1
$PY -m analysis.tension runs/s8/w0/all_off_5seeds_full > runs/s9/tension_w0.txt                        # chọn chỉ báo độ căng ŝ
$PY -m analysis.tension runs/s8/w1/all_off_5seeds_full config/default.yaml config/world_w1.yaml > runs/s9/tension_w1.txt
for w in w0 w1; do
  # tác dụng theo độ căng trước block, θ̂ không và có B, giờ có hại
  $PY -m analysis.tension_curve --world $w --switchback runs/s8/$w/switchback_call_28d --budget runs/s8/$w/calibrate_budget \
      --all-off runs/s8/$w/all_off_5seeds_full --all-on runs/s8/$w/all_on_5seeds_full --legacy runs/s8/$w/legacy_28d --out runs/s9/$w
  $PY -m analysis.hour_rule set $w --from runs/s9/$w/summary.json                                     # analysis/models/hour_rule_<w>.json
  # ba nguồn sai số (confounding, tranh chấp xe, đội xe) và burn-in so với ảnh hưởng kéo dài
  $PY -m analysis.interference waterfall --legacy runs/s8/$w/legacy_28d --rider-ab runs/s8/$w/rider_ab_28d \
      --gte runs/s8/$w/gte --gte-big runs/s8/$w/gte_bigfleet --out runs/s9/$w
  $PY -m analysis.interference burnin --switchback-all runs/s8/$w/switchback_call_28d --gte runs/s8/$w/gte --out runs/s9/$w
done
```

### Bước 3: thí nghiệm đánh giá, `runs/s10` (≈ 9 giờ máy, chạy qua đêm)

Ngưỡng θ̂ lấy từ bước 2 (`analysis.policy_table.S9_THRESHOLDS`: W0 `ring1` 2,3 / `city` 1,6; W1 `ring1` 2,15 / `city` 2,1).

```bash
DR0=analysis.uplift:tau_x_dr_all_per_kvnd; DR1=analysis.uplift:tau_x_dr_all_w1_per_kvnd
# Bảng chính sách dưới cùng B, 30 seed (≈ 70 phút mỗi thế giới)
$PY -m analysis.policy_table $W0 --world w0 --n-seeds 30 --out runs/s10/w0/policy_table
$PY -m analysis.policy_table $W1 --world w1 --n-seeds 30 --out runs/s10/w1/policy_table
# Quét θ 16 mức × 30 seed
$PY -m sim run --mode sweep_theta $W0 --config config/sweep_reference.yaml --set sweep.n_seeds=30 \
    --set policy.threshold.scope=ring1 --set policy.threshold.score_fn=$DR0 --out runs/s10/w0/sweep_ring1_dr
$PY -m sim run --mode sweep_theta $W1 --config config/sweep_reference.yaml --set sweep.n_seeds=30 \
    --set policy.threshold.scope=ring1 --set policy.threshold.score_fn=$DR1 --out runs/s10/w1/sweep_ring1_dr
# Độ vững W1: B × đội xe, và hai world_seed khác
for frac in 0.3 0.6 1.0; do for fleet in 1278 1061 856; do
  $PY -m analysis.policy_table $W1 --set budget.fraction=$frac --set supply.fleet_size=$fleet --n-seeds 30 \
     --spec label=all_on_B,policy=all_on --spec label=tier_dr_kvnd,policy=threshold,theta=0,score_fn=$DR1 \
     --spec label=pi_hourB_ring1,policy=threshold,theta=2.15,score_fn=$DR1,scope=ring1 --out runs/s10/w1/robust_B${frac}_fleet${fleet}
done; done
for ws in 20260931 20260932; do
  $PY -m analysis.policy_table $W1 --set meta.world_seed=$ws --world w1 --n-seeds 30 --out runs/s10/w1/policy_table_ws$ws
done
# Độ nhạy W0, 10 seed: hazard hủy ×0,5 / ×2, quãng đường ngắn / dài, hình cầu phẳng kiểu NYC
SPECS="--spec label=all_on_B,policy=all_on --spec label=random,policy=threshold,theta=0,score_fn=random \
 --spec label=tier_dr_kvnd,policy=threshold,theta=0,score_fn=$DR0 \
 --spec label=pi_hourB_ring1,policy=threshold,theta=2.3,score_fn=$DR0,scope=ring1 \
 --spec label=hour_off_dr,policy=threshold,theta=0,score_fn=analysis.hour_rule:off_hours_dr_w0"
$PY -m analysis.policy_table $W0 --n-seeds 10 $SPECS --out runs/s10/w0/sens_base
$PY -m analysis.policy_table $W0 --set cancel.base_per_min=0.0025 --set cancel.slope_per_min2=0.002 --n-seeds 10 $SPECS --out runs/s10/w0/sens_hazard_0p5
$PY -m analysis.policy_table $W0 --set cancel.base_per_min=0.01 --set cancel.slope_per_min2=0.008 --n-seeds 10 $SPECS --out runs/s10/w0/sens_hazard_2
$PY -m analysis.policy_table $W0 --set demand.dest_decay_rings=1.9 --n-seeds 10 $SPECS --out runs/s10/w0/sens_trip_L1p9
$PY -m analysis.policy_table $W0 --set demand.dest_decay_rings=6.0 --n-seeds 10 $SPECS --out runs/s10/w0/sens_trip_L6
$PY -m analysis.policy_table $W0 --set "demand.hour_profile=[0.685,0.634,0.609,0.609,0.634,0.736,0.913,1.218,1.370,1.167,1.015,1.015,1.066,1.040,1.015,1.066,1.192,1.395,1.446,1.268,1.116,1.040,0.939,0.812]" \
    --n-seeds 10 $SPECS --out runs/s10/w0/sens_profile_flat
# Confounding theo mức nhắm u_latent
for w in w0 w1; do
  $PY -m analysis.interference confounding runs/s8/$w/legacy_gu_0_28d runs/s8/$w/legacy_gu_0p5_28d runs/s8/$w/legacy_28d \
      runs/s8/$w/legacy_gu_2_28d --out runs/s10/$w/confounding
done
# Qini trên A/B theo khách so với N của bảng chính sách (≈ 3 giờ)
for w in w0 w1; do for oc in completed requested; do
  $PY -m analysis.qini_vs_value --data runs/s8/$w/rider_ab_28d --table runs/s10/$w/policy_table --outcome $oc --out runs/s10/$w/qini_vs_value
done; done
# H2: hai chính sách W1 log đầy đủ, tách lợi theo slot có mưa / sự kiện
$PY -m analysis.policy_table $W1 --n-seeds 10 --log-level full --spec label=all_on_B,policy=all_on \
   --spec label=pi_hourB_ring1,policy=threshold,theta=2.15,score_fn=$DR1,scope=ring1 \
   --spec label=hour_off_dr,policy=threshold,theta=0,score_fn=analysis.hour_rule:off_hours_dr_w1 --out runs/s10/w1/h2_slots_full
$PY -m analysis.evaluation shocks --table runs/s10/w1/h2_slots_full --a pi_hourB_ring1 --b hour_off_dr --out runs/s10/w1/eval_h2
# Cặp W0 log đầy đủ cho animation
$PY -m analysis.policy_table $W0 --n-seeds 10 --log-level full --spec label=all_on_B,policy=all_on \
   --spec label=pi_hourB_city,policy=threshold,theta=1.6,score_fn=$DR0,scope=city --out runs/s10/w0/anim_pair_full
# Thời gian chạy (A5): 1 tiến trình, 3 seed
$PY -m sim run --mode evaluate $W0 --set policy.name=all_on --set sweep.n_seeds=3 --set runner.n_procs=1 --out runs/s10/w0/a5_evaluate_1proc
```

Đọc nhanh kết quả bước 3: `python -m analysis.evaluation gap --table runs/s10/w0/policy_table` (khoảng cách tới oracle đã lấp), `sweep --sweep runs/s10/w0/sweep_ring1_dr --theta-hat 2.3 0` (regret khi chọn θ trên nửa seed), `switch --table …` (số lần bật/tắt), `stack --dirs "runs/s10/w1/robust_*"` (xếp chồng bảng độ vững).

### Bước 4: hình cho báo cáo

```bash
# Animation chính sách (docs/figures/06_*.gif)
$PY -m analysis.animation --old runs/s10/w0/anim_pair_full/runs/*-all_on-nan-* --new runs/s10/w0/anim_pair_full/runs/*-threshold-1.6-* \
    --hours 5 9 --out docs/figures/06_anim_w0_5_9h.gif
$PY -m analysis.animation --old runs/s10/w1/h2_slots_full/runs/*-all_on-nan-* --new runs/s10/w1/h2_slots_full/runs/*-threshold-2.15-* \
    --hours 5 9 --out docs/figures/06_anim_w1_5_9h.gif
# Thống kê mô tả 28 ngày dữ liệu lịch sử (docs/figures/07_*)
$PY -m analysis.data_stats --w0 runs/s8/w0/legacy_28d --w1 runs/s8/w1/legacy_28d --out docs/figures
```

Hình 01–05 do notebook ghi ra khi chạy (`analysis.plots.save_figure`).

## Notebook và phân tích

Notebook **chỉ gọi hàm** trong `analysis/` và trình bày; mọi phép tính nằm trong `analysis/*.py` và có test. Mở trong VS Code (hoặc Jupyter) với kernel là Python của `.venv` sau `pip install -e ".[analysis]"`. Khi thiếu dữ liệu, ô đầu tiên báo thư mục nào còn thiếu.

| Notebook | Nội dung | Dữ liệu cần |
|---|---|---|
| `01_simulator_kiem_dinh.ipynb` | mô hình thị trường theo giờ, throughput và wild goose chase, A1–A5, CAL, các bộ dữ liệu | `runs/s8`, `runs/cal_s8/a1_w0_grid25`, `runs/s10/w0` |
| `02_danh_gia_chinh_sach.ipynb` | N(π), V(π) dưới cùng B, θ\* và tập θ\*, regret, Qini so với N, độ vững, độ nhạy, H1/H2 | `runs/s10` |
| `03_interference.ipynb` | chệch theo thiết kế thí nghiệm, thác nước ba nguồn sai số, độ nhạy theo `u_latent` | `runs/s8/w0`, `runs/s9/w0`, `runs/s10/w0/confounding` |
| `04_uoc_luong_va_theta.ipynb` | DR-learner, chọn chỉ báo ŝ, tác hại theo giờ, θ̂ so với θ\* | `runs/s8`, `runs/s9`, `runs/s10` |
| `05_confounding.ipynb` | so thô, điều chỉnh theo X, DR, lát explore | `runs/s8/w0/legacy_*` |
| `06_animation.ipynb` | animation chính sách khuyến nghị so với `all_on` có B, kiểm tổng Δ theo slot | `runs/s10/w0/anim_pair_full`, `runs/s10/w1/h2_slots_full` |

Các module chính của `analysis/`: `io` (đọc thư mục run, không trả bảng ẩn), `metrics` (giá trị chính sách + CI, hiệu ghép cặp, Qini/AUUC, tập θ\*), `policy_table`, `evaluation`, `uplift` và `scores` (DR-learner, hàm điểm), `tension`, `tension_curve`, `theta`, `hour_rule`, `estimate`, `interference`, `confounding`, `qini_vs_value`, `validation`, `calibrate`, `figures`, `plots`, `animation`, `data_stats`, `check_dataset`, `check_budget`, `tlc_hourly` (đối chiếu hình dạng theo giờ với dữ liệu NYC TLC; dữ liệu TLC không có trong repo).

## Dashboard

Giao diện web để chạy một lượt mô phỏng, xem animation xe trên bản đồ, quét θ có tiến độ theo từng seed và xem các kết quả đã lưu trong `runs/`.

```bash
pip install -e ".[dashboard]"
python -m dashboard                 # http://127.0.0.1:8050/   (--port, --host, --reload)
```

Phải chạy bằng `python -m dashboard` (pool tiến trình `spawn`). Lượt chạy từ dashboard ghi vào `runs/dashboard/<id>/` đúng schema như CLI. Các trang, cách đọc từng chỉ số, kiến trúc và API: [`dashboard/README.md`](dashboard/README.md).

## Kiểm thử

```bash
pytest -q                          # test nhanh (bỏ qua test đánh dấu slow)
pytest -q -m slow                  # nghiệm thu A1–A5, CAL, hồi quy (chậm, vài chục phút)
pytest -q tests/test_policies.py -k threshold
```

- Test nhanh dùng lớp `tests/fixtures/tiny.yaml`; `tests/fakes.py` chứa đồ giả dùng chung. Trên Windows, nếu thư mục tạm mặc định của pytest báo lỗi quyền, thêm `--basetemp="$LOCALAPPDATA/Temp/pytest-sim"`.
- `tests/test_schema_contract.py` đối chiếu tên cột trong code với `docs/schema.md`; `tests/test_regression.py` kiểm code hiện tại (tắt các tùy chọn mô hình mới qua `tests/fixtures/regression_pinned.yaml`) ra đúng bảng đã ghi ở `tests/fixtures/regression_baseline.json`.

**Nghiệm thu** (`tests/test_acceptance*.py`):

| Mã | Kiểm gì | Tiêu chí đạt |
|---|---|---|
| A1 | đường throughput: `throughput_curve`, mọi xe online, cầu và tốc độ hằng số tại 18h, `demand_scale` 0,25…2,5, 3 seed | chuyến/giờ tăng tới đỉnh rồi giảm (mức cầu lớn nhất ≤ 0,95 × đỉnh); `mean_slack` < 0,45 ở đoạn giảm (ngưỡng WGC của Castillo); ETA đón tăng đơn điệu, không bước nào > 3 phút |
| A2 | CRN | cùng seed chạy lại cho N, V giống hệt; Var(N(π1) − N(π2)) với CRN ≤ 0,5 × khi dùng seed độc lập; hai chính sách thấy cùng session, khách, ô đích, `u_book` |
| A3 | dao động của π<sub>θ</sub> (θ = 0,35, 5 seed) | báo số lần bật/tắt mỗi ô mỗi ngày; nếu trung vị > 12 thì chạy lại với `hysteresis_h = 0,1` và so sánh (thông tin) |
| A4 | tính đơn điệu | số lần đổi dấu của tác dụng voucher theo nhóm độ căng trên dữ liệu switchback (thông tin) |
| A5 | thời gian chạy | `evaluate` `all_on` 1 ngày, 1 tiến trình, trung vị 3 lần ≤ 60 giây (đo được 17–19 giây) |

**Hiệu chỉnh CAL** (`all_off`, 5 seed, khoảng mục tiêu trong `calibration_targets` của `default.yaml`):

| Chỉ tiêu | Khoảng mục tiêu |
|---|---|
| P(đặt) không voucher ở ô dư cung (slack slot trước > 1) | 0,13–0,17 |
| Tăng lượt đặt nhờ voucher ở ô dư cung (từ cột ẩn `p_request_*`) | +35% … +70% |
| Tỷ lệ (ô, slot) căng (slack < 0,35) / dư (slack > 1) | 10–35% / 30–80% |
| Tỷ lệ xe bận lúc 6h / 7h / 8h so với giờ cao nhất | 0,31–0,61 / 0,55–0,85 / 0,63–0,93 |
| Quãng đường trung bình chuyến hoàn thành | 5–8 km |
| Chuyến / tài xế / ngày | 8–18 |

Dải căng / dư là thiết kế kịch bản ("có vùng căng thật"), không phải số đo thực tế. N(π<sub>θ</sub>), θ\* và GTE không bao giờ được dùng làm mục tiêu hiệu chỉnh.

## Cấu trúc thư mục

```
Supply-Aware-Promotion-Uplift/
├── README.md
├── CLAUDE.md                     quy tắc cứng khi sửa code
├── pyproject.toml                gói supply-aware-sim; extras dev / analysis / dashboard; cấu hình pytest
├── config/
│   ├── default.yaml              mọi tham số (thế giới W0), mỗi khóa có chú thích nguồn
│   ├── world_w1.yaml             lớp phủ thế giới W1 (biến động tự nhiên)
│   └── sweep_reference.yaml      lớp phủ lưới θ tới 30
├── sim/                          simulator; không import analysis/ hay dashboard/ (M1–M13 = nhãn module dùng trong code)
│   ├── cli.py, __main__.py       python -m sim run ...
│   ├── config.py                 nạp YAML nhiều lớp, --set, config_hash
│   ├── rng.py                    luồng ngẫu nhiên có khóa (CRN)
│   ├── space.py                  M1  lưới lục giác, torus, D, T, ETA đón
│   ├── population.py             sinh thế giới từ world_seed: trọng số ô, khách, ca tài xế
│   ├── demand.py                 M2  session Poisson, ô đích, số rút sẵn
│   ├── pricing.py, budget.py     M3  giá, voucher, lớp áp ngân sách; sổ ngân sách
│   ├── choice.py                 M4  logit đặt xe, ground truth
│   ├── matching.py               M5  ghép FIFO, cùng ô rồi mở rộng vành
│   ├── trips.py                  M6  đón, trả, thanh toán
│   ├── cancel.py                 M7  bỏ chờ, hủy khi xe đang tới
│   ├── supply.py                 M8  ca làm việc, rời chỉ khi rảnh
│   ├── reposition.py             M9  điều chuyển theo trọng số tĩnh
│   ├── experiment.py             M10 cụm, block, switchback, A/B theo khách
│   ├── monitor.py                M11 I, E, O, W, slack theo (ô, slot); SnapshotView
│   ├── logger.py                 M12 ghi Parquet theo docs/schema.md
│   ├── runner.py                 M13 các mode chạy, nhiều seed song song
│   ├── shocks.py                 biến động W1
│   ├── engine.py, state.py       vòng lặp tick; trạng thái struct-of-arrays
│   └── policies/                 base (giao diện), fixed, legacy, threshold, experiment, scores
├── analysis/                     đánh giá, ước lượng, hình; models/ = mô hình DR và luật giờ đã học
├── dashboard/                    FastAPI + React (htm), static/ vendor sẵn
├── notebooks/                    6 notebook kết quả
├── tests/                        pytest; fixtures/ (tiny.yaml, regression_*)
├── docs/
│   ├── schema.md                 định dạng dữ liệu đầu ra, danh sách cột ẩn
│   └── figures/                  hình và animation kết quả
├── outputs/                      báo cáo và slide
└── runs/                         output mô phỏng (không commit)
```
