# Schema dữ liệu đầu ra

Mọi bảng ghi dạng **Parquet** (pyarrow), mỗi lượt chạy một thư mục:

```
runs/<run_name>/
  meta/run_metadata.parquet
  meta/shocks.parquet                (luôn ghi; rỗng khi shocks.enabled = false)
  observed/riders.parquet
  observed/sessions.parquet
  observed/orders.parquet
  market/slot_snapshots.parquet
  hidden/riders_hidden.parquet
  hidden/sessions_hidden.parquet
  results/policy_results.parquet
  results/theta_sweep.parquet        (chỉ mode sweep_theta)
  results/throughput_curve.parquet   (chỉ mode throughput_curve)
```

## Quy tắc tách dữ liệu (bắt buộc)

- `observed/` và `market/` là dữ liệu mà mô hình uplift và chính sách **được phép** đọc.
- `hidden/` là ground truth. **Chỉ** dùng để đánh giá và debug; tuyệt đối không nối (join) vào dữ liệu huấn luyện.
- Test `test_no_hidden_leak` kiểm tra rằng không cột nào trong danh sách ẩn xuất hiện trong `observed/` hoặc `market/`. **Danh sách này là chuẩn**; quy tắc 3 của `CLAUDE.md` trỏ về đây. Danh sách ẩn gồm:
  `u_latent, alpha, beta_price, beta_eta, delta_promo, max_wait_min, propensity_true, p_request_treat, p_request_control, direct_request_effect_fixed_market, u_book, u_target, u_explore, u_explore_arm, u_score, trip_noise, e_cancel`.
- `score` là đầu ra của chính sách nên là cột quan sát, kể cả khi `score_fn = random` cho `score = u_score`. Test rò rỉ kiểm theo tên cột.
- Mọi bảng có cột `run_id` (string) và `seed` (int32).

Kiểu dữ liệu viết theo Arrow: `int8/16/32/64`, `float32/64`, `bool`, `string`, `timestamp` không dùng. Thời gian lưu bằng **giây kể từ đầu lượt chạy** (`float64`). Tiền tính bằng **đồng** (cột `*_vnd`).

Kiểu ở đây là kiểu **trên đĩa**. Trong bộ nhớ (`sim/state.py`), cột `string` (`assign_mechanism`, `status`, `cancel_reason`) là mã `int8` và logger đổi sang chuỗi bằng `state.decode_codes` khi ghi; các số rút sẵn của session giữ `float64` trong bộ nhớ và ghi `float32`. `tests/test_schema_contract.py` đối chiếu tên cột với file này; `tests/test_logger.py` kiểm kiểu trên đĩa.

---

## meta/run_metadata

| Cột | Kiểu | Mô tả |
|---|---|---|
| run_id | string | `<mode>-<config_hash>-<policy>-<theta>-<seed>` |
| mode | string | generate / evaluate / sweep_theta / gte / … |
| config_hash | string | 12 ký tự |
| config_yaml | string | toàn văn config đã áp override |
| git_sha | string | commit của code |
| policy | string | |
| hidden_view | string | cổng tới biến ẩn mà chính sách của lượt chạy mở: `legacy` (`LegacyHiddenView`, `u_latent`), `oracle` (`OracleHiddenView`, hệ số đặt xe, chỉ với `threshold` + `score_fn = oracle`), hoặc `none` |
| theta | float64 | NaN nếu không áp dụng |
| seed | int32 | |
| world_seed | int64 | |
| budget_B_vnd | float64 | NaN nếu không áp ngân sách |
| budget_pilot_seed | int64 | seed của lượt pilot all_on dùng tính B (= `run_seed + budget.pilot_seed_offset`); −1 nếu không có pilot |
| budget_pilot_spent_vnd | float64 | chi tiêu voucher của lượt pilot trên cửa sổ gốc, B = `fraction` × số này / số kỳ; NaN nếu không có pilot |
| kappa | float64 | NaN nếu không áp dụng |
| kappa_pilots | int32 | số lượt pilot κ auto đã chạy để ra `kappa`, gồm lượt κ = −∞; 0 nếu không dùng κ auto |
| window_start_s, window_end_s | float64 | cửa sổ đánh giá |
| sim_end_s | float64 | thời điểm dừng (gồm cool-down) |
| n_truncated_orders | int32 | order bị cắt khi hết cool-down |
| runtime_s | float64 | thời gian chạy thực |
| created_at | string | ISO 8601 |

## meta/shocks (một dòng mỗi biến động; rỗng khi `shocks.enabled = false`)

Lịch biến động của lượt chạy (thế giới W1), rút trước theo ngày từ luồng SHOCK. Chính sách **không** đọc được bảng này; chỉ dùng để phân tích (chọn khung cho animation, tách kết quả theo biến động).

| Cột | Kiểu | Mô tả |
|---|---|---|
| day | int16 | |
| kind | string | `day_demand` / `event` / `rain` / `attendance` |
| cell | int16 | ô địa điểm với `event`; −1 = cả thành phố |
| start_s, end_s | float64 | khoảng hiệu lực, giây từ đầu lượt chạy; cả ngày với `day_demand`, `attendance` |
| demand_mult | float32 | hệ số nhân cầu (ô địa điểm với `event`); 1 nếu không áp |
| ring1_mult | float32 | hệ số nhân cầu ở 6 ô kề với `event`; 1 nếu không áp |
| speed_mult | float32 | hệ số nhân tốc độ; 1 nếu không áp |
| n_drivers_off | int32 | số tài xế nghỉ (`attendance`: cả ngày; `rain`: suốt cơn mưa); 0 nếu không áp |

## observed/riders

| Cột | Kiểu | Mô tả |
|---|---|---|
| rider_id | int32 | |
| home_cell | int16 | |
| x_freq | float32 | |
| x_tenure | float32 | tháng |
| x_segment | int8 | 0/1/2 |

## observed/sessions (một dòng mỗi session)

| Cột | Kiểu | Mô tả |
|---|---|---|
| session_id | int64 | ID ổn định `((day·ticks_per_day + tick_of_day)·N_ô + cell)·1000 + k`, k là thứ tự session trong (ô, tick) |
| rider_id | int32 | |
| open_time_s | float64 | |
| day, hour, slot, slot_of_day | int16 | |
| pu_cell, do_cell | int16 | |
| quoted_fare_vnd | float32 | giá gốc p_s |
| voucher_value_vnd | float32 | 0 nếu không phát |
| quoted_eta_min | float32 | |
| no_supply | bool | không có xe trong giới hạn khi báo giá |
| promo_on_cell | bool | trạng thái khuyến mãi của ô trong slot |
| arm | int8 | 1 = được phát voucher |
| assign_mechanism | string | legacy_rule / legacy_eps / explore / experiment / threshold / fixed |
| propensity | float32 | NaN nếu không biết (chính sách cũ nhắm rider) |
| cell_propensity | float32 | NaN nếu không biết |
| cluster_id | int16 | −1 nếu không phải thí nghiệm |
| block | int32 | tính từ đầu lượt chạy; −1 nếu không phải thí nghiệm |
| in_burnin | bool | |
| budget_blocked | bool | muốn phát nhưng hết ngân sách |
| budget_period | int16 | kỳ ngân sách của session; −1 = warm-up |
| score | float32 | điểm tầng rider; NaN nếu không dùng |
| slack_hat | float32 | ŝ của ô đón: với `threshold` là dự báo dùng ra quyết định; với `experiment` và `legacy` là slack đã công bố của slot k−1; mọi trường hợp chặn ở `monitor.slack_cap`; NaN nếu chưa công bố hoặc chính sách cố định |
| lag_slot_same_block | bool | slot k−1 cùng khối switchback với slot k; `true` với chính sách không có khối; `false` ở slot 0 |
| requested | bool | khách có đặt xe không |
| in_window | bool | open_time trong cửa sổ đánh giá |

## observed/orders (một dòng mỗi order, `order_id = session_id`)

| Cột | Kiểu | Mô tả |
|---|---|---|
| order_id | int64 | |
| session_id | int64 | |
| rider_id | int32 | |
| driver_id | int32 | −1 nếu chưa ghép |
| pu_cell, do_cell | int16 | |
| driver_origin_cell | int16 | −1 nếu chưa ghép |
| request_time_s | float64 | |
| matched_time_s | float64 | NaN nếu chưa ghép |
| pickup_eta_min | float32 | ETA lúc ghép |
| pickup_time_s | float64 | NaN nếu không đón |
| dropoff_time_s | float64 | NaN nếu không hoàn thành |
| trip_time_min | float32 | |
| trip_km | float32 | quãng đường theo detour |
| gross_fare_vnd | float32 | |
| voucher_value_vnd | float32 | |
| net_fare_vnd | float32 | gross − voucher |
| driver_pay_vnd | float32 | (1 − commission) × (gross − phí nền tảng) |
| platform_profit_vnd | float32 | net − driver_pay = phí nền tảng + commission × (gross − phí) − voucher; 0 nếu không hoàn thành |
| status | string | Completed / Abandoned / Cancelled / Truncated |
| cancel_reason | string | "" / rider_en_route |
| in_window | bool | request_time trong cửa sổ đánh giá |

## market/slot_snapshots (một dòng mỗi (ô, slot))

| Cột | Kiểu | Mô tả |
|---|---|---|
| cell | int16 | |
| slot, day, slot_of_day, hour | int32/int16 | |
| idle_avg (I) | float32 | trung bình theo tick |
| enroute_avg (E) | float32 | xe đang đi đón khách ở ô này |
| ontrip_avg (O) | float32 | |
| waiting_avg (W) | float32 | |
| slack | float64 | I/E; khi E = 0: `inf` nếu I > 0, 0 nếu I = 0 (ô không có xe rảnh là ô căng) |
| utilization | float32 | (E+O)/(I+E+O) |
| mean_pickup_eta_min | float32 | NaN nếu không có ghép |
| n_sessions, n_offers, n_requests, n_matched, n_completed, n_abandoned, n_cancelled | int32 | đếm theo thời điểm sự kiện và ô đón: `n_sessions/n_offers/n_requests` theo open_time; `n_matched` theo matched_time; `n_abandoned/n_cancelled` theo thời điểm hủy; `n_completed` theo dropoff_time |
| voucher_spent_vnd | float32 | voucher của order hoàn thành trong slot, theo ô đón |
| promo_on | bool | trạng thái trong slot này |
| cell_propensity | float32 | |
| assign_mechanism_cell | string | |
| cluster_id, block | int16/int32 | |
| in_burnin | bool | |
| slack_lag_slot | float64 | slack của slot k−1 (đã công bố) |
| slack_lag_day | float64 | slack của slot k−96; NaN nếu chưa có |
| published_at_s | float64 | thời điểm công bố (= cuối slot) |

## hidden/riders_hidden

| Cột | Kiểu |
|---|---|
| rider_id | int32 |
| u_latent, alpha, beta_price, beta_eta, delta_promo, max_wait_min | float32 |

## hidden/sessions_hidden

| Cột | Kiểu | Mô tả |
|---|---|---|
| session_id | int64 | |
| propensity_true | float32 | xác suất phát thật của chính sách cũ |
| p_request_treat | float32 | P(đặt) nếu có voucher, cùng bối cảnh |
| p_request_control | float32 | P(đặt) nếu không voucher |
| direct_request_effect_fixed_market | float32 | treat − control. **Không phải uplift thật; không dùng để chấm mô hình** |
| u_book, u_target, u_explore, u_explore_arm, u_score | float32 | số rút sẵn của session, để tái lập |
| trip_noise, e_cancel | float32 | để tái lập |

## results/policy_results (một dòng mỗi lượt chạy)

| Cột | Kiểu | Mô tả |
|---|---|---|
| run_id, policy | string | |
| theta | float64 | |
| seed | int32 | |
| world_seed | int64 | để gộp nhiều thế giới |
| N_completed | int64 | **N(π)**, đơn tạo trong cửa sổ |
| V_profit_vnd | float64 | **V(π)** |
| voucher_spent_vnd | float64 | |
| budget_B_vnd | float64 | |
| n_sessions, n_requests, n_abandoned, n_cancelled | int64 | trong cửa sổ |
| mean_pickup_eta_min | float64 | |
| share_cells_off | float64 | tỷ lệ (ô, slot) bị tắt |
| n_switches_per_cell_day | float64 | số lần bật/tắt mỗi ô mỗi ngày (kiểm A3) |
| runtime_s | float64 | |

## results/theta_sweep (tổng hợp từ policy_results)

| Cột | Kiểu |
|---|---|
| theta | float64 |
| N_mean, N_se, V_mean, V_se, spent_mean | float64 |
| n_seeds | int32 |
| is_argmax | bool |

## results/throughput_curve

| Cột | Kiểu | Mô tả |
|---|---|---|
| demand_scale, fleet_size | float64 / int32 | |
| seed | int32 | |
| completed_per_h, requests_per_h | float64 | order tạo trong cửa sổ / giờ cửa sổ |
| mean_pickup_eta_min | float64 | trên order được ghép, tạo trong cửa sổ |
| mean_slack | float64 | tổng (xe rảnh × tick) / tổng (xe đi đón × tick) trên cửa sổ; `inf` nếu mẫu số 0 |
| abandon_rate, cancel_rate | float64 | `n_abandoned / n_requests`, `n_cancelled / n_requests` trên order tạo trong cửa sổ |
