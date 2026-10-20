# Dashboard quản trị simulator (`dashboard/`)

Giao diện web để **chạy, xem và giải thích** một lượt mô phỏng của simulator gọi xe: tài xế di chuyển trên lưới lục giác theo từng phút, chính sách π<sub>θ</sub> cắt voucher ở ô căng cung (ŝ < θ), tầng rider phát voucher theo điểm τ̂ ≥ κ trong ngân sách B, và các kết quả thực nghiệm đã lưu trong `runs/` (đường N(π<sub>θ</sub>), bảng chính sách dưới cùng B, throughput, GTE). Mô tả mô hình: `README.md` ở gốc repo, mục "Mô hình".

Stack: **FastAPI** (Python, cùng `.venv` với simulator) + **React 18 qua htm** (không cần Node, không cần build; React, ReactDOM và htm vendor sẵn trong `static/vendor/`, chạy được offline, chỉ font chữ tải từ Google Fonts).

## Cài đặt và chạy

```powershell
.venv\Scripts\activate
pip install -e ".[dashboard]"          # fastapi, uvicorn, httpx
python -m dashboard                    # http://127.0.0.1:8050/  (--port, --host, --reload)
```

Lượt chạy từ dashboard ghi vào `runs/dashboard/<id>/` (không commit, như mọi `runs/`). Khi mở lại server, các lượt đã xong được nạp lại từ đĩa. Các bộ dữ liệu khác trong `runs/` mà trang Kết quả đọc được sinh bằng các lệnh ở `README.md` gốc, mục "Tái lập kết quả".

Server gửi `Cache-Control: no-cache` cho `/` và `/static/`, nên trình duyệt kiểm lại file giao diện mỗi lần tải. Nếu vẫn thấy bản cũ, bấm Ctrl+F5 một lần.

## Hai loại lượt chạy

- **Quét θ (tìm θ\*)**: việc chính của đề tài. Ngưỡng θ không do người đặt: simulator chạy π<sub>θ</sub> với mọi θ trong lưới, cùng B (một pilot `all_on`), κ auto riêng từng θ, cùng seed, rồi θ\* = argmax<sub>θ</sub> N(π<sub>θ</sub>). Vì đường N(θ) thường phẳng quanh đỉnh, báo thêm **tập θ\***: các θ không khác biệt thống kê với θ tốt nhất (`analysis.metrics.theta_star_set`, so sánh ghép cặp theo seed, Bonferroni). "Cắt theo giờ và theo vùng" không phải là θ riêng cho từng (ô, slot): cùng một θ, nhưng ŝ của mỗi ô mỗi slot 15 phút khác nhau nên bản đồ tắt/bật khác nhau theo giờ và theo vùng (thiết kế cố ý không rẽ nhánh θ theo từng (ô, slot)).
- **Một θ cố định**: một lượt `evaluate` một seed với `log_level = full` cộng frame từng tick, để xem bản đồ động, tầng ô, tầng rider của đúng một điểm trên đường N(θ) (ví dụ tại θ\* vừa tìm được).

Ô "Thế giới" trong cả hai form chọn lớp config (`layers` của API): **W0** = `config/default.yaml` (61 ô bán kính 4, 1.150 xe, VND), **W1** = thêm `config/world_w1.yaml` (1.278 xe, mưa, sự kiện, cầu theo ngày, tài xế nghỉ), và **tiny** = `tests/fixtures/tiny.yaml` (7 ô, để thử nhanh).

## Các trang

Thanh điều hướng bên trái chia 7 trang thành ba nhóm. Cột "Hướng dẫn" chỉ mục tương ứng trong phần [Hướng dẫn đọc dashboard](#hướng-dẫn-đọc-dashboard).

| Nhóm | Trang | Trả lời câu hỏi | Dữ liệu | Hướng dẫn |
|---|---|---|---|---|
| **Không gian nghiên cứu** | **Tổng quan** | lượt chạy cho N(π), tỷ lệ hoàn thành / đặt, chi voucher / B, ETA đón, V(π), % (ô, slot) bị cắt; phễu session → request → completed; chuỗi theo slot; doanh thu & lợi nhuận; so với lượt cùng seed; danh sách lượt chạy có tìm kiếm và lọc; với lượt quét: θ\*, tập θ\*, đường N(θ) | `/api/runs/{id}`, `/summary`, `/compare`, `/sweep` | mục 1 |
| | **Kết quả & đối chiếu** | ba câu hỏi nghiên cứu RQ1–RQ3 nối với bảng đã lưu; đường N(π<sub>θ</sub>) ± SE của mọi sweep trong `runs/` kể cả sweep từ dashboard (chọn tối đa 4, chấm = θ\*), bảng N(π) dưới cùng B (`analysis.policy_table`), throughput A1, GTE | `/api/results/*` | mục 7 |
| **Thiết kế thực nghiệm** | **Tìm ngưỡng tối ưu θ\*** | chọn lưới θ, số seed, số tiến trình song song, thế giới, phạm vi ŝ, hàm điểm, B; đường N(θ) ± SE **lớn dần theo từng seed xong**, vùng tập θ\*, chấm θ\*, đánh dấu θ̂ ước lượng từ dữ liệu quan sát để so (θ̂_B = 2,3 với `ring1`, 1,6 với `city` trên W0); % (ô, slot) tắt và chi theo θ; bảng theo θ kèm κ; nút chạy một lượt chi tiết tại θ\* | `POST /api/sweeps`, `/sweep` | mục 2 |
| | **Chạy mô phỏng** | đặt tham số (thế giới, chính sách, θ, phạm vi ŝ, hàm điểm, κ, B, voucher, đội xe, cầu, cửa sổ, seed) rồi chạy; tiến độ theo giai đoạn: pilot B → κ auto → mô phỏng → ghi bảng | `POST /api/runs`, `/api/config/validate` | mục 3 |
| **Phân tích vận hành** | **Bản đồ thị trường** | *animation*: xe rảnh / đi đón / chở khách / dịch chuyển, khách đang chờ, ô bật/tắt voucher (gạch chéo = bị cắt), hiệu ứng phát voucher, chặn ngân sách, ghép xe, hoàn thành, hủy; đồng hồ, tốc độ ×1…×120, thanh thời gian, theo dõi trực tiếp khi đang chạy; sổ ngân sách kỳ hiện tại; bấm ô để xem ŝ so với θ | `/frames` (từng chunk), `/slots` | mục 4 |
| | **Cung & chính sách vùng** | ô nào bị cắt lúc nào: bản đồ nhiệt ô × slot (bật/tắt, ŝ dự báo, slack thực), chi tiết một ô, **cắt theo giờ trong ngày** (tỷ lệ ô tắt theo giờ), **cắt theo vùng** (bản đồ số slot tắt từng ô), nhật ký bật/tắt | `/slots`, `/summary`, hình học lưới | mục 5 |
| | **Voucher & ngân sách** | ai nhận voucher: theo giờ (phát / bị chặn), phân khúc rider, cơ chế gán, phân bố điểm τ̂ và κ; ngân sách theo thời gian (chi + đặt + giữ ≤ B) | `/summary` | mục 6 |

Thanh ngang "Thực nghiệm đang xem" ở đầu mọi trang chọn lượt chạy; chip bên cạnh ghi "Một chính sách × seed" (lượt một θ) hay "Nhiều θ × seed" (lượt quét). Góc phải thanh trên báo trạng thái kết nối API và số thực nghiệm đang chạy. Thanh bên có chế độ giao diện Sáng / Tối / Tự động và nút thu gọn; dòng trong bảng chọn được bằng chuột, Enter hoặc Space; lỗi API hiện kèm nút "Thử lại".

Link thẳng một lượt: `#/map?run=<id>`, `#/sweep?run=<id>`; mở form một θ với θ điền sẵn: `#/run?theta=0.5&scope=ring1` (thêm `&score_fn=...` nếu cần).

## Hướng dẫn đọc dashboard

Phần này giải thích mọi chỉ số, ký hiệu toán, chữ viết tắt và từ tiếng Anh xuất hiện trên giao diện. Đọc mục 0 trước để nắm bộ từ vựng chung, sau đó tra theo trang. Nguồn định nghĩa: `README.md` gốc (mục "Mô hình"), `config/default.yaml`, `docs/schema.md`.

### 0. Bộ từ vựng chung (đọc trước)

#### 0.1. Bài toán trong một đoạn

Simulator mô phỏng một thành phố gồm các **ô lục giác** (mặc định 61 ô, quy mô Hà Nội). Mỗi **phút** (một *tick*), khách mở app (*session*), nhìn giá và thời gian chờ xe, rồi quyết định đặt hay không. Nền tảng có thể tặng **voucher** 25 % cước (trần 20.000 đ) để kéo khách đặt. Voucher tốn tiền, nên có **ngân sách B** cố định mỗi ngày. Câu hỏi của đề tài: *ở ô nào, giờ nào nên tắt voucher vì thiếu xe?* Tắt bằng một **ngưỡng θ** trên độ dư cung **ŝ**. Dashboard giúp chạy mô phỏng, tìm θ tốt nhất (**θ\***) và nhìn xem chính sách đang cắt ở đâu.

#### 0.2. Không gian và thời gian

| Thuật ngữ | Nghĩa |
|---|---|
| **ô (cell)** | một ô lục giác trên lưới, cạnh 1,41 km (ô H3 độ phân giải 7). Lưới bán kính 4 có 61 ô (mặc định, ≈ 315 km²), bán kính 3 có 37 ô, bán kính 1 có 7 ô (tiny). Ô được đánh số 0, 1, 2… |
| **torus** | lưới nối biên: xe đi ra khỏi cạnh này sẽ xuất hiện ở cạnh đối diện, để không có "ô rìa" đặc biệt. |
| **vành (ring)** | các ô cách ô gốc đúng k bước. Vành 1 là 6 ô kề. |
| **tick** | bước thời gian của mô phỏng, 60 giây. Mọi quyết định xe và khách diễn ra theo tick. |
| **slot** | khung 15 phút (15 tick). Chính sách cắt ô quyết định một lần mỗi slot, đầu slot. Một ngày có 96 slot. |
| **warm-up** | 60 phút đầu lượt chạy, để thị trường "ấm máy". Không tính vào kết quả. |
| **cửa sổ đánh giá (window)** | khoảng thời gian được chấm điểm, mặc định 1 ngày (1 440 phút) sau warm-up. Chỉ các chuyến có lượt đặt nằm trong cửa sổ mới được đếm. Trên biểu đồ, cửa sổ tô **màu vàng nhạt**. |
| **cool-down** | sau cửa sổ, mô phỏng chạy thêm tối đa 120 phút để các chuyến đã đặt kịp hoàn thành. |
| **kỳ ngân sách (budget period)** | mỗi ngày trong cửa sổ là một kỳ, có ngân sách B riêng. Warm-up là kỳ −1. Với cửa sổ 1 ngày chỉ có một kỳ: kỳ 0. |
| **seed** | số khởi tạo bộ sinh ngẫu nhiên. Cùng seed, cùng config thì chạy lại ra đúng kết quả. Chạy nhiều seed để có trung bình và sai số chuẩn. |
| **CRN (common random numbers)** | cùng một seed thì mọi chính sách thấy cùng một "ngày": cùng khách, cùng xe, cùng độ kiên nhẫn. Nhờ vậy so hai chính sách là so trên cùng điều kiện, chênh lệch nhỏ cũng đo được. |

#### 0.3. Khách, phiên và chuyến

| Thuật ngữ | Nghĩa |
|---|---|
| **rider** | một khách hàng. Có thuộc tính quan sát được: `x_freq` (tần suất dùng app), `x_tenure` (số tháng dùng), `x_segment` (phân khúc: 0 thường, 1 nhạy giá, 2 ít nhạy giá). |
| **session (phiên)** | một lần khách mở app xem giá. Mỗi session, chính sách quyết định có phát voucher hay không. Khách có thể đặt hoặc tắt app. |
| **request (lượt đặt xe)** | session mà khách bấm đặt. Tạo ra một **order**. |
| **order (đơn / chuyến)** | một lượt đặt, đi qua các trạng thái bên dưới. |
| `Waiting` | đã đặt, đang chờ ghép xe. |
| `Matched` | đã ghép, xe đang đi đón. |
| `OnTrip` | khách đã lên xe. |
| `Completed` | đã trả khách. Đây là chuyến được đếm vào N(π). |
| `Abandoned` | **bỏ vì chờ lâu**: khách chờ ghép quá sức kiên nhẫn của mình (trung bình 5 phút) mà vẫn chưa có xe. Chỉ xảy ra khi không có xe rảnh nào trong bán kính tìm xe. |
| `Cancelled` | **hủy khi xe đang đến**: khách hủy trong lúc xe đi đón. Xác suất hủy tăng theo ETA đón. |
| `Truncated` | chuyến chưa kết thúc khi hết cool-down, không đếm. |
| **offer (phát voucher)** | session được tặng voucher. Số đếm `n_offers`. |
| **blocked (bị chặn, hết B)** | chính sách muốn phát nhưng ngân sách kỳ đó đã hết, nên không phát. Cột `budget_blocked`. |
| **arm (nhánh)** | 1 = session có voucher, 0 = không. |

#### 0.4. Xe

| Thuật ngữ | Nghĩa |
|---|---|
| **fleet_size (đội xe)** | tổng số xe, mặc định 1.150 (W0) và 1.278 (W1). Không phải lúc nào cũng online: mỗi xe có một ca làm trung bình 11,2 giờ, bắt đầu theo bốn khung giờ (5–7,5h, 9–13h, 14–18h, 19–23h). |
| **idle (rảnh)** | xe trong ca, đang đứng chờ khách. |
| **en_route (đang đi đón)** | xe đã nhận chuyến, đang chạy tới khách. |
| **on_trip (chở khách)** | đang chở khách tới điểm đến. |
| **repositioning (dịch chuyển)** | xe rảnh quá 10 phút tự chạy sang ô khác theo trọng số cố định, không nhìn cầu hiện tại. |
| **offline (hết ca)** | xe ngoài ca làm. |
| **ETA đón (pickup ETA)** | số phút xe cần để tới chỗ khách. ETA báo giá hiện cho khách lúc mở app; ETA thực ghi khi ghép. |
| **max_pickup_eta_min** | bán kính tìm xe, tính theo phút: chỉ ghép xe nếu ETA đón ≤ 30 phút. Để rộng cố ý để hiện tượng wild goose chase xuất hiện. |
| **WGC (wild goose chase)** | hiện tượng khi thiếu xe: xe phải đi đón rất xa, thời gian đi đón ăn mất thời gian chở, nên tổng số chuyến hoàn thành **giảm** dù cầu tăng. Đây là lý do nên tắt voucher ở ô căng cung. |

#### 0.5. Cung, cầu và chỉ số căng cung

| Ký hiệu | Nghĩa |
|---|---|
| **demand_scale (hệ số cầu)** | núm nhân tổng cầu, không đơn vị. 1,0 là mức gốc, 0,85 (mặc định) là 85 % mức gốc, 2,0 là gấp đôi. |
| **slack** | độ dư cung của một ô trong một slot = **số xe rảnh / số xe đang đi đón** (I / E). Slack 2,5 nghĩa là cứ 1 xe đi đón thì có 2,5 xe đang rảnh, cung dư. Slack dưới 0,5 là căng. Khi không có xe đi đón: +∞ nếu còn xe rảnh, 0 nếu không có xe nào. Trên biểu đồ, ∞ được cắt ở 10 (hoặc 3 trên bản đồ nhiệt) để vẽ được. |
| **ŝ (s-hat, "ŝ dự báo")** | slack **dự báo** mà chính sách dùng để quyết định đầu slot k. Chính sách không được nhìn slot hiện tại, chỉ được dùng slot trước (**không nhìn trước**). Vì vậy ŝ luôn trễ một slot so với slack thực. |
| **slack thực (công bố cuối slot)** | slack đo được sau khi slot kết thúc. Dùng để so với ŝ xem dự báo lệch bao nhiêu. |
| **scope: cell / ring1 / city** | phạm vi đo ŝ. `cell`: slack của chính ô. `ring1`: cộng xe rảnh và xe đi đón của ô và 6 ô kề rồi chia. `city`: cộng cả thành phố, một ŝ chung cho mọi ô. `ring1` mượt hơn `cell`; `city` ít bật/tắt nhất. |
| **forecast: persistence / ar** | cách dự báo ŝ. `persistence`: lấy nguyên slack slot trước. `ar`: 0,7 × slot trước + 0,3 × cùng slot hôm qua. |
| **slack_cap** | 10. Giá trị thay cho +∞ khi đưa vào dự báo. |
| **hour_profile** | hệ số cầu theo giờ trong ngày, giờ cao điểm sáng và chiều cao hơn. |

#### 0.6. Chính sách voucher

| Thuật ngữ | Nghĩa |
|---|---|
| **chính sách (policy) π** | quy tắc quyết định session nào được voucher. |
| **π_θ (threshold)** | chính sách chính của đề tài, **hai tầng**. **Tầng ô:** đầu mỗi slot, ô có ŝ < θ bị **tắt** voucher. **Tầng rider:** trong ô đang bật, session có điểm τ̂ ≥ κ được voucher, nếu còn ngân sách. |
| **θ (theta)** | ngưỡng cắt ô. θ = 0: không cắt ô nào. θ càng cao, càng nhiều ô bị tắt. |
| **θ\* (theta star)** | θ tốt nhất: θ cho N(π_θ) cao nhất khi quét nhiều θ dưới cùng B. |
| **tập θ\*** | các θ không khác biệt thống kê với θ tốt nhất. Đường N(θ) thường phẳng quanh đỉnh nên báo cả tập thay vì một điểm. Tính bằng so sánh ghép cặp theo seed, hiệu chỉnh Bonferroni, mức α = 0,05. |
| **θ̂ (theta hat)** | θ **ước lượng từ dữ liệu quan sát** bằng phương pháp phân tích (θ̂_B = 2,3 với `ring1`, 1,6 với `city` trên W0; 2,15 / 2,1 trên W1). Đánh dấu để so với θ\* "thật" của simulator (sweep tham chiếu `runs/s10/<w>/sweep_ring1_dr`, 16 θ × 30 seed: θ tốt nhất 1,5 trên W0, 1,25 trên W1). |
| **hysteresis h (trễ bật lại)** | ô đã tắt chỉ bật lại khi ŝ > θ + h, để tránh bật tắt liên tục. Mặc định 0. |
| **τ̂ (tau hat, điểm rider)** | điểm ưu tiên của một session, do **hàm điểm** (`score_fn`) tính. Ý nghĩa: ước lượng voucher làm tăng xác suất đặt của khách này bao nhiêu (uplift). Điểm cao thì ưu tiên phát. |
| **score_fn (hàm điểm)** | `random`: điểm ngẫu nhiên. `heuristic_low_freq`: ưu tiên khách ít dùng app (điểm = −x_freq). `analysis.uplift:tau_x_dr…`: mô hình uplift học từ dữ liệu, trong đó `tau_x` chỉ dùng thuộc tính khách, `tau_xs` dùng cả ŝ, `dr` = doubly robust (phương pháp ước lượng), `per_kvnd` = điểm chia cho tiền voucher kỳ vọng, tính trên 1.000 đ (`per_dollar` là bí danh cũ, cùng thứ tự xếp), `all` = học trên mọi session, không có `all` = chỉ học trên lát explore, `_w1` = mô hình học trên dữ liệu W1. `analysis.hour_rule:off_hours_*`: luật tắt cố định ở các giờ voucher có hại (W0: 8h, 17–19h; W1: 18h), tầng rider random hoặc DR. |
| **κ (kappa, ngưỡng điểm)** | ngưỡng tầng rider: chỉ phát cho session có τ̂ ≥ κ. `κ = −∞` nghĩa là không hạn chế, phát cho mọi session ở ô bật. |
| **κ auto / pilot** | κ không đặt tay. Chạy một lượt **pilot** (lượt thử, seed riêng) không áp ngân sách, lấy phân bố điểm, rồi chọn κ sao cho tổng chi ≈ B. Lặp lại tối đa 10 lần cho đến khi chi ≤ B. Số lần lặp hiện trong ngoặc, ví dụ "κ 0,412 (3 pilot)". |
| **all_on** | bật mọi ô, phát mọi session, chịu B. |
| **all_off** | không voucher. |
| **legacy** | giả lập người vận hành cũ: bật ô nếu slack slot trước ≥ 0,6, thêm nhiễu ε, nhắm rider bằng luật riêng. Dùng để sinh dữ liệu lịch sử có confounding. |
| **experiment** | thiết kế thí nghiệm: `cluster_switchback` (bật tắt theo cụm ô và block giờ), `global_switchback` (toàn hệ bật tắt theo block), `rider_ab` (ngẫu nhiên theo khách). Không áp ngân sách. |
| **assign_mechanism (cơ chế gán)** | vì sao session này ở nhánh đó: `threshold` (π_θ), `fixed` (all_on / all_off), `legacy_rule`, `legacy_eps` (nhiễu ε của legacy), `explore` (lát ngẫu nhiên 5 %), `experiment`. |
| **explore (lát explore)** | 5 % session được ngẫu nhiên hóa hoàn toàn, dùng để học mô hình uplift không chệch. |

#### 0.7. Chỉ tiêu và ngân sách

| Ký hiệu | Nghĩa |
|---|---|
| **N(π)** | **chỉ tiêu chính**: số chuyến hoàn thành trong cửa sổ đánh giá dưới chính sách π. |
| **V(π)** | chỉ tiêu phụ: lợi nhuận nền tảng, VND = tổng (cước sau voucher − tiền trả tài xế). Voucher luôn làm V giảm. |
| **B** | ngân sách voucher mỗi kỳ, VND. Mặc định B = 0,3 × chi tiêu của all_on không ngân sách (tính bằng một lượt pilot). Có thể đặt cố định. |
| **fraction** | hệ số 0,3 nói trên. |
| **sổ cái (ledger)** | bộ đếm ngân sách theo kỳ gồm ba phần. **Đã chi (spent):** voucher của chuyến đã hoàn thành. **Đã đặt (committed):** voucher của chuyến đã đặt, đang chạy. **Đang giữ (reserved):** voucher đã phát, khách chưa quyết định đặt. Bất biến: chi + đặt + giữ ≤ B. Khi chạm trần, session muốn phát bị chặn. |
| **Chi / B (spent_share_of_budget)** | tiền voucher của chuyến hoàn thành chia cho B × số kỳ. Dưới 100 % là bình thường, vì tiền giữ và tiền đặt của chuyến hủy được nhả lại. |
| **SE (standard error, sai số chuẩn)** | độ không chắc chắn của trung bình theo seed. "N ± SE" = trung bình ± sai số. Dải mờ quanh đường là ± 1 SE. |
| **ΔN (delta N)** | chênh lệch N so với chính sách tham chiếu, ghép cặp theo seed. |
| **GTE (global treatment effect)** | N(all_on) − N(all_off) không ngân sách: voucher toàn hệ đem lại thêm bao nhiêu chuyến. Chuẩn "sự thật" để kiểm các phương pháp ước lượng. |
| **A1** | tên test nghiệm thu "đường throughput" (định nghĩa ở `README.md` gốc, mục "Kiểm thử"). |
| **config hash** | mã băm 12 ký tự của toàn bộ config. Hai lượt cùng hash thì cùng mọi tham số. |

### 1. Trang "Tổng quan"

Trang mở đầu. Banner nêu câu hỏi trọng tâm ("khuyến mãi có tạo thêm chuyến đi khi thị trường thiếu tài xế?") và công thức mục tiêu max N(π_θ) dưới cùng B. Phía dưới là lượt chạy đang chọn ở thanh "Thực nghiệm đang xem".

#### Khối "Mô phỏng được chọn" / "Quét ngưỡng"

Tên lượt, trạng thái, và các sự kiện chính: chính sách, θ cố định (lượt một θ), phạm vi đo cung (`cell` / `ring1` / `city`), đội xe, seed. Nút bên phải mở bản đồ (lượt một θ) hoặc trang quét (lượt quét).

#### Bốn ô số chính (lượt một θ đã xong)

| Ô | Nghĩa | Đọc thế nào |
|---|---|---|
| **Chuyến hoàn thành · N(π)** | chỉ tiêu chính, đếm theo thời điểm đặt xe nằm trong cửa sổ. Dòng dưới: chuyến mỗi giờ. | càng cao càng tốt khi cùng B. |
| **Tỷ lệ hoàn thành / đặt xe** | N(π) chia số lượt đặt. | thấp nghĩa là nhiều hủy hoặc bỏ. |
| **Ngân sách voucher đã chi** | Tiền voucher (VND) của chuyến hoàn thành. Dòng dưới: phần trăm trên tổng B = B mỗi kỳ × số kỳ. | gần 100 % là dùng hết. Thấp bất thường nghĩa là κ quá cao hoặc cắt quá nhiều ô. |
| **ETA đón trung bình** | phút xe đi đón, trung bình các lượt đã ghép. | dài là thiếu xe. |

#### Dải chỉ số phụ

| Ô | Nghĩa |
|---|---|
| Lợi nhuận V(π) · phụ | VND, chỉ tiêu phụ; voucher làm V giảm. |
| Tỷ lệ (ô, slot) tắt voucher | số cặp (ô, slot) tắt trên tổng, trong cửa sổ. |
| Hủy khi xe đang đến | `Cancelled` / lượt đặt. |
| Bỏ chờ trước khi ghép | `Abandoned` / lượt đặt. Thường ≈ 0, xem mục 8. |

#### Khối "Doanh thu & lợi nhuận"

Tiền của các chuyến **hoàn thành** có lượt đặt trong cửa sổ, đọc từ `observed/orders`: **cước gốc** (p_s) → trừ **voucher** = **khách trả** (`net_fare`) → trừ **trả tài xế** = (1 − hoa hồng 25 %) × (cước gốc − phí nền tảng 10.000 đ) → còn **lợi nhuận nền tảng V(π)** = phí nền tảng + hoa hồng × (cước gốc − phí) − voucher. Tổng V của khối bằng đúng V(π) ở dải chỉ số phụ. "Trước voucher" = V + voucher, tức biên nền tảng nếu không phải trả voucher.

Bảng theo nhánh (chuyến có / không voucher) và đường theo giờ là **mô tả**: chuyến có voucher do chính sách chọn, không ngẫu nhiên, nên không được đọc hiệu hai dòng là "lợi nhuận do voucher".

#### Khối "So với lượt cùng seed"

Cách duy nhất đo **lợi nhuận của việc phát voucher đúng người** trong simulator: so lượt này với một lượt tham chiếu **cùng seed** (CRN: cùng khách, cùng xe), ví dụ `all_off` (không voucher) hoặc `all_on` có B (phát cho mọi người). Chọn lượt tham chiếu trong ô, khối hiện: chuyến thêm ΔN, voucher chi thêm, lợi nhuận thêm ΔV (đã trừ voucher) và biên thêm trước voucher, rồi hai tỷ số: **chuyến thêm / 100.000 đ voucher thêm** và **biên thêm / 1.000 đ voucher thêm** (≥ 1.000 đ nghĩa là mỗi 1.000 đ voucher chi thêm đem về ít nhất 1.000 đ biên nền tảng, voucher tự trả được tiền; < 1.000 đ thì ΔV âm). Ba cờ bên dưới báo cùng seed, cùng thế giới (đội xe, cầu, cửa sổ, biến động, % voucher) và cùng B; khác seed thì hiệu còn lẫn nhiễu seed, cần nhiều seed (bảng chính sách tham chiếu có `V` và `dV_vs_ref` ghép cặp 30 seed).

Lượt chạy cũ chưa có mục doanh thu thì không so được; chạy lại lượt đó.

#### Hai biểu đồ theo slot

- **Nhu cầu & chuyến hoàn thành:** đường đứt là lượt đặt xe, đường liền là chuyến hoàn thành, đường thứ ba là voucher được phát, mỗi điểm một slot 15 phút. Dải vàng là cửa sổ đánh giá. Chú ý: hoàn thành theo slot ghi theo **thời điểm kết thúc chuyến**, còn N(π) đếm theo **thời điểm đặt**, nên cộng các điểm trong dải vàng không ra đúng N(π).
- **Vùng tắt khuyến mãi:** số ô tắt voucher theo từng slot, trục dọc là số ô. Với threshold: θ cố định nhưng trạng thái bật/tắt thay đổi theo ŝ của từng ô.

#### Phễu "Từ nhu cầu đến chuyến đi"

Ba thanh: phiên truy cập → yêu cầu đặt xe → chuyến hoàn thành, kèm tỷ lệ chuyển đổi giữa các bước. Đây là **số liệu mô tả** của lượt chạy, không phải uplift.

#### Khối "Chi tiêu & cấu hình chính sách"

Tiền đã chi trên tổng B, thanh đo, và bốn dòng: voucher / cước (%), κ ngưỡng điểm rider, độ dài cửa sổ đánh giá, số đơn còn dang dở khi dừng (`Truncated`). Ghi chú: trần B áp cho **từng kỳ**, gồm cả tiền đã cam kết và đang giữ; tổng chi chỉ là phần của chuyến hoàn thành.

#### Với lượt quét

Thay cho các khối trên, trang hiện kết quả quét giống trang Tìm ngưỡng (mục 2).

#### Danh sách lượt chạy

Ô tìm kiếm theo tên, chính sách, mã lượt; bộ lọc Tất cả / Mô phỏng / Quét θ. Cột: thực nghiệm (tên + chính sách + θ), loại, trạng thái, N(π) (với lượt quét: N trung bình tại θ\*), chi / tổng B. Bấm một dòng (hoặc Enter) để chọn.

#### Khối "Phương pháp & cách đọc dashboard"

Khối gập, ba bước: so sánh dưới cùng B, quyết định dựa trên cung trễ, chọn θ\* qua thực nghiệm. Dòng cuối nhắc: không dùng Qini hay chênh lệch hai nhánh voucher thay cho giá trị chính sách ở cấp thị trường.

### 2. Trang "Tìm ngưỡng tối ưu θ\*"

Việc chính của đề tài. θ\* không do người đặt: simulator chạy π_θ với **mọi θ trong lưới**, cùng B, cùng seed, rồi chọn θ có N cao nhất.

#### Form cấu hình

| Trường | Nghĩa |
|---|---|
| Preset | "Nhanh (demo)": 6 θ × 2 seed, cửa sổ 4 giờ, vài phút. "Chuẩn": lưới config × 10 seed, 1 ngày. "Tham chiếu": 16 θ × 30 seed, 1 ngày (lưới của `config/sweep_reference.yaml`), cùng cỡ với hai sweep tham chiếu trong `runs/s10/` (≈ 1 giờ với 8 lõi). |
| Thế giới | W0 (`config/default.yaml`) hoặc W1 (thêm lớp `config/world_w1.yaml`: mưa, sự kiện, cầu theo ngày, tài xế nghỉ; đội xe 1.278). Chọn W1 thì mô hình DR nên dùng bản `_w1`. |
| Lưới θ | danh sách θ cần thử, phân cách bằng dấu phẩy. |
| Seed mỗi θ | số lượt chạy lặp cho mỗi θ. Nhiều seed thì SE nhỏ, tập θ\* hẹp. |
| Tiến trình song song | số lõi CPU dùng cùng lúc. |
| Phạm vi đo ŝ, Dự báo ŝ, Trễ bật lại h, Hàm điểm τ̂ | xem mục 0.5 và 0.6. |
| Áp ngân sách B, fraction | B = fraction × chi của all_on. |
| Voucher = % cước | 0,25 = 25 %; trần `voucher.max_vnd` = 20.000 đ. |
| Đội xe, Hệ số cầu, Bán kính lưới, Cửa sổ, Warm-up, run_seed đầu | tham số thị trường và thời gian, chung cho mọi θ. |
| Override thêm | dòng `khóa=giá trị` theo cú pháp `--set`, ghi đè bất kỳ tham số nào trong `config/default.yaml`. |
| ước tính ≈ … | thời gian dự kiến, gồm pilot B và κ auto. |

#### Bốn ô kết quả

| Ô | Nghĩa |
|---|---|
| **θ\* = argmax N(π_θ)** | θ cho N trung bình cao nhất. "argmax" = đối số làm cực đại. |
| **Tập θ\* (không khác θ tốt nhất)** | khoảng [lo; hi] các θ không thua θ tốt nhất một cách có ý nghĩa thống kê. "n / m θ trong tập", "α = 0,05". Cần ≥ 2 seed. |
| **N tại θ\*** | N trung bình ± SE, chi voucher, % (ô, slot) tắt tại θ\*. |
| **Lợi ích của tầng ô: N(θ\*) − N(θ = 0)** | thêm được bao nhiêu chuyến nhờ cắt ô, so với không cắt (θ = 0) nhưng vẫn cùng B và cùng tầng rider. Ghép cặp theo seed ± SE. Dương là cắt ô có lợi. |

#### Biểu đồ và bảng

- **Đường N(π_θ) theo θ:** trục ngang là lưới θ, **cách đều theo chỉ số** chứ không theo giá trị. Dải mờ ± 1 SE. Chấm đậm là θ\*. Vùng xanh nhạt là tập θ\*. Đường dọc "θ̂ = …" là θ ước lượng từ dữ liệu quan sát mà bạn nhập vào ô "Đánh dấu θ̂" (mặc định 2,3).
- **θ càng cao, càng nhiều (ô, slot) bị cắt:** cột tỷ lệ cắt theo θ.
- **Chi voucher theo θ:** đường ngang B là trần. Cắt nhiều ô thì không tiêu hết B.
- **Bảng theo θ:** cột `seed xong` (tiến độ), `N ± SE`, `tập θ\*` (dấu ✓), `V`, `Chi`, `% ô tắt`, `ETA (ph)` là ETA đón trung bình, `κ (pilot)` là κ auto và số lượt pilot của θ đó.
- Nút **"Chạy một lượt chi tiết tại θ\*"** mở trang Chạy mô phỏng với θ điền sẵn, để xem bản đồ động.

#### Khung tiến độ

`n_rows / total` là số cặp (θ, seed) đã xong trên tổng. Đường N(θ) **lớn dần theo từng seed xong**, nên lúc đang chạy là tạm tính.

### 3. Trang "Chạy mô phỏng"

Chạy **một** lượt với θ cố định, có ghi frame từng tick để xem bản đồ. Đây là một điểm trên đường N(θ).

#### Các trường đặc thù (ngoài mục 2)

| Trường | Nghĩa |
|---|---|
| Preset | W0 Hà Nội (61 ô, 1.150 xe, 1 ngày), W1 Hà Nội + biến động (61 ô, 1.278 xe, mưa / sự kiện), Demo nhanh (W0, cửa sổ 4 giờ), Tiny (7 ô, 10 xe, 2 giờ). |
| Thế giới | W0 / W1 / tiny: chọn lớp config (mục 2). |
| Chính sách voucher | threshold / all_on / all_off / legacy / experiment (mục 0.6). |
| θ: ngưỡng cắt ô | ŝ < θ thì tắt. |
| κ: auto / nhập tay | auto chạy pilot; nhập tay bỏ qua pilot. |
| Thiết kế, Cụm, p_on | với `experiment`: kiểu switchback, cỡ cụm (1 ô, ~7 ô, toàn hệ), xác suất bật. |
| slack_on, explore_frac | với `legacy`: ngưỡng slack bật ô, tỷ lệ lát ngẫu nhiên. |
| Cách lấy B | fraction × pilot all_on, hoặc cố định VND/kỳ. |
| Nhập B tay | bỏ qua pilot, dùng B này. |
| Kiểm tra config | xác nhận cú pháp override, trả về hash, số ô, số xe, cửa sổ. |

#### Khung tiến độ

Giai đoạn chạy theo thứ tự: **xếp hàng → nạp config → pilot ngân sách B → κ auto → đang mô phỏng → ghi bảng → xong**. Khi mô phỏng: `tick k / ≤ max` và đồng hồ "Ngày d · hh:mm". Log phía dưới là nhật ký thô của server. Ba ô số khi xong: N(π), chi voucher so với B, % (ô, slot) tắt.

### 4. Trang "Bản đồ thị trường"

Hoạt hình theo từng phút mô phỏng. Chỉ có với lượt "một θ".

#### Bốn chế độ tô màu ô

| Chế độ | Ô được tô theo |
|---|---|
| Trạng thái voucher | ô đang bật voucher (màu nền), ô bị cắt (gạch chéo). |
| ŝ dự báo | thang xanh từ 0 (căng) đến 3+ (dư). Gạch chéo vẫn là ô bị cắt. |
| Khách chờ | số khách đang chờ ghép trong ô, 0 đến 6+. |
| Cụm thí nghiệm | cụm switchback, chỉ có nghĩa với chính sách experiment. |

#### Chấm và hiệu ứng

| Ký hiệu | Nghĩa |
|---|---|
| chấm xanh lá | xe rảnh (idle). |
| chấm cam | xe đang đi đón (en_route). |
| chấm xanh dương | xe chở khách (on_trip). |
| chấm xám | xe dịch chuyển (repositioning). |
| vòng tròn rỗng | khách đang chờ ghép. |
| hiệu ứng "phát / hết ngân sách" | lóe khi phát voucher; dấu × đỏ khi muốn phát nhưng hết B. |
| hiệu ứng "ghép / hoàn thành" | lóe khi ghép xe, khi trả khách. |
| hiệu ứng "hủy / bỏ" | lóe khi khách hủy hoặc bỏ. |
| hiệu ứng "đặt xe" | lóe khi khách bấm đặt. |

Xe đi qua biên torus hiện ra ở cạnh đối diện.

Với lượt W1, các biến động (mưa: tốc độ ×0,85, 10 % tài xế nghỉ, cầu ×1,25; sự kiện: cầu ×2 hoặc ×20 ở ô địa điểm; cầu theo ngày; tài xế nghỉ) tác động lên xe và khách trên bản đồ nhưng **không được vẽ riêng**; lịch biến động của lượt chạy nằm ở `meta/shocks.parquet` trong thư mục lượt.

#### Thanh thời gian

⏮ về đầu, ▶ phát, ⏭ nhảy tới đầu cửa sổ đánh giá. Tốc độ "×10 (10 ph/s)" = 10 phút mô phỏng mỗi giây thực. Vạch sáng trên thanh là cửa sổ đánh giá. "Theo dõi trực tiếp" bám theo frame mới khi lượt đang chạy. `tick k / n`: frame hiện tại trên số frame đã tải.

#### Bảng bên phải

**Đội xe lúc này:** số xe rảnh, đi đón, chở khách, dịch chuyển, offline, và số khách đang chờ ghép tại frame này.

**Cộng dồn từ đầu lượt** (gồm cả warm-up):

| Dòng | Nghĩa |
|---|---|
| phiên mở (session) | số lần khách mở app. |
| voucher đã phát | số session nhận voucher. |
| muốn phát nhưng hết B | số session bị chặn vì ngân sách. |
| lượt đặt xe | số session có đặt. |
| chuyến hoàn thành | order Completed. |
| hủy khi xe đang đến | order Cancelled. |
| bỏ vì chờ lâu | order Abandoned. |

**Ngân sách kỳ hiện tại:** thanh ba màu đã chi / đã đặt / đang giữ trên trần B. "kỳ warm-up" là kỳ −1.

**Ô đang bị cắt voucher:** danh sách số ô tắt ở slot hiện tại, bấm để xem chi tiết.

**Ô k (khi bấm một ô):** voucher BẬT/TẮT slot này; "ŝ so với θ" dạng `ŝ / θ`; xe rảnh · đi đón · chở; khách chờ. Biểu đồ nhỏ: đường liền là ŝ dự báo, đường đứt là slack thực, đường ngang là θ, vùng xám là các slot ô bị tắt.

### 5. Trang "Cung & chính sách vùng"

Trả lời "ô nào bị cắt lúc nào". Chỉ có với lượt "một θ".

#### Bốn ô số

| Ô | Nghĩa |
|---|---|
| (ô, slot) bị cắt | tỷ lệ trên **mọi** slot kể cả warm-up và cool-down (khác KPI trang Tổng quan chỉ tính trong cửa sổ). |
| Ô chưa bao giờ bị cắt | số ô luôn bật / tổng số ô. |
| Lần đổi trạng thái | số sự kiện bật→tắt hoặc tắt→bật; dòng dưới quy ra mỗi ô mỗi ngày trong cửa sổ. |
| Quy tắc cắt | `ŝ < θ`, scope và cách dự báo. |

#### Bản đồ nhiệt ô × slot

Hàng là ô, cột là slot (nhãn giờ). Ba chế độ:

- **Bật / tắt:** màu nền là bật, xám là tắt.
- **ŝ dự báo (quyết định):** thang xanh 0 → 3+, viền đen là ô bị tắt. Đây là số chính sách **nhìn thấy** lúc ra quyết định.
- **slack thực (công bố cuối slot):** cùng thang, là số **thực tế** sau khi slot kết thúc. "Chưa có snapshot" (ô xám nhạt) là slot đầu chưa có dữ liệu.

Di chuột hiện: ô, slot, bật/tắt, ŝ so với θ, slack thực, và số voucher · số đặt · số hoàn thành của ô trong slot đó. Bấm một hàng để xem chi tiết ô.

#### Bốn biểu đồ

- **Ô k: ŝ dự báo so với θ:** như biểu đồ ô ở trang Bản đồ. Ghi chú: đường liền (ŝ) đi **sau** đường đứt (slack thực) một slot khi dự báo persistence, vì chính sách không nhìn trước.
- **Cắt theo giờ trong ngày:** tỷ lệ ô bị tắt, trung bình các slot trong giờ đó. Cùng một θ nhưng giờ cao điểm tắt nhiều hơn: **cắt theo giờ là hệ quả, không phải θ riêng cho từng giờ.**
- **Cắt theo vùng:** bản đồ lưới, số trong ô là số slot bị tắt của ô đó. Ô thiếu xe rảnh bị cắt nhiều hơn.
- **Ô bị cắt nhiều nhất:** top 8 ô theo số slot tắt.

#### Nhật ký bật / tắt

Mỗi dòng một sự kiện đổi trạng thái: slot, giờ, ô, chiều chuyển, ŝ lúc đó, θ.

### 6. Trang "Voucher & ngân sách"

Tầng rider: ai nhận voucher và ngân sách dùng ra sao. Chỉ tính session trong cửa sổ đánh giá. Cần lượt "một θ" đã xong.

#### Sáu ô số

| Ô | Nghĩa |
|---|---|
| Voucher đã phát (cửa sổ) | số session nhận voucher; dòng dưới là tỷ lệ trên tổng session. |
| Muốn phát nhưng hết B | số session bị chặn. |
| Voucher trung bình | VND mỗi voucher; dòng dưới là % cước. |
| Chi thực / B | chỉ voucher của chuyến **hoàn thành**. Voucher phát ra mà khách không đặt hoặc hủy thì không tốn tiền. |
| κ tầng rider | ngưỡng điểm và tên hàm điểm. |
| Session trong cửa sổ | tổng session. |

#### Biểu đồ và bảng

- **Ngân sách theo thời gian:** đường đứt là trần B của kỳ, đường "đã dùng" = chi + đặt + giữ, đường "đã chi" chỉ chuyến hoàn thành. Đã dùng không bao giờ vượt trần. Bảng dưới: từng kỳ, đã chi, trần, chi / B.
- **Phát voucher theo giờ:** cột chồng đã phát và bị chặn, theo giờ mở session. Biểu đồ dưới: tỷ lệ session được phát mỗi giờ. Giờ cao điểm thường thấp hơn vì nhiều ô bị cắt.
- **Phân bố điểm τ̂ và ngưỡng κ:** histogram điểm của mọi session (xám) và của session được phát (màu). Phần được phát nằm bên phải κ và chỉ trong ô bật. "n % session ở ô đang bật" ở góc. Chính sách không chấm điểm (all_on, legacy…) không có biểu đồ này.
- **Ai nhận voucher:** tỷ lệ phát theo phân khúc (thường / nhạy giá / ít nhạy giá). Bảng cơ chế gán (mục 0.6). Bảng nhánh: tỷ lệ đặt và tỷ lệ hoàn thành của nhóm có voucher so với không có.

**Lưu ý trên trang:** bảng nhánh là **mô tả, không phải uplift**. Nhóm có voucher do chính sách chọn (không ngẫu nhiên), và voucher của người này làm đổi thị trường của người khác (interference), nên không được trừ hai dòng cho nhau để kết luận hiệu quả.

### 7. Trang "Kết quả & đối chiếu"

Đọc các bảng đã lưu trong `runs/` từ dòng lệnh và từ dashboard. Không chạy gì mới.

#### Ba thẻ câu hỏi nghiên cứu (RQ)

RQ = research question, ba câu hỏi nghiên cứu của đề tài. Mỗi thẻ ghi "Có dữ liệu" hay "Chưa có dữ liệu" và bấm vào sẽ cuộn tới phần bằng chứng tương ứng.

| Thẻ | Câu hỏi | Bằng chứng trên trang |
|---|---|---|
| RQ1 | Uplift thay đổi theo độ căng cung? | đường throughput (A1), các sweep θ. |
| RQ2 | Chính sách biết cung tốt hơn bao nhiêu? | bảng N(π) dưới cùng B, ΔN ± SE. |
| RQ3 | Đánh giá sai lệch do đâu? | GTE (phần còn lại — thác nước ba nguồn sai số, burn-in, gây nhiễu — ở notebook 03 và 05, không có trên dashboard). |

"Có dữ liệu" chỉ xác nhận **có bảng đã lưu**, không xác nhận câu hỏi đã được trả lời đủ hay đạt nghiệm thu.

#### Dòng "Cách đọc"

± SE là sai số chuẩn, **không phải** khoảng tin cậy 95 %. Chỉ so chính sách trong cùng ngân sách, cửa sổ và cấu hình; các đường sweep chồng lên nhau có thể thuộc kịch bản khác nhau.

#### Đường N(π_θ) theo θ

Chọn tối đa 4 sweep. Chuyển giữa N(π), V(π), chi voucher. Dải ± 1 SE, chấm đậm là θ\* của từng sweep. Danh sách sweep nhóm theo thư mục `runs/<nhóm>/`, kèm số seed, θ\*, B. Lệnh sinh từng thư mục nằm ở `README.md` gốc, mục "Tái lập kết quả". Khi mở trang, hai sweep tham chiếu `s10/w0/sweep_ring1_dr` và `s10/w1/sweep_ring1_dr` (16 θ × 30 seed) được chọn sẵn nếu có.

#### N(π) của các chính sách dưới cùng B

Mỗi `results/policy_table.parquet` trong `runs/` (sinh bằng `analysis.policy_table`) là một khối. Cột ngang ΔN so với chính sách tham chiếu (ΔN = 0), thanh lỗi là SE ghép cặp theo seed. Bảng: nhãn, chính sách, hàm điểm, θ, κ, N ± SE, V, chi, % ô tắt. Các nhãn thường gặp:

| Nhãn | Nghĩa |
|---|---|
| `all_off`, `all_on_B` | không voucher / voucher cho mọi người chịu B (mốc). |
| `random`, `heuristic` | phát ngẫu nhiên trong B / ưu tiên khách ít dùng app, không cắt ô. |
| `tau_x`, `tau_per_vnd` | điểm nền tra bảng theo tầng khách: điểm theo chuyến / theo đồng. |
| `tau_x_dr_all`, `tau_xs_dr_all` | DR-learner theo **chuyến** (không chia tiền), chỉ thuộc tính khách / có cả ŝ. |
| `tau_x_dr_all_kvnd`, `tau_xs_dr_all_kvnd` | DR-learner theo **1.000 đ** chi kỳ vọng, học trên mọi session; bản `tau_*_dr_kvnd` chỉ học trên lát explore. |
| `pi_tension_ring1_dr_kvnd` | tầng vùng ring1 với θ̂_tension = 0 (điểm cắt của đường tác dụng theo độ căng) — trùng tầng rider. |
| `pi_hourB_ring1_dr_kvnd`, `pi_hourB_city_dr_kvnd` | tầng vùng với θ̂_B ước lượng từ dữ liệu quan sát (ring1 2,3 / city 1,6 trên W0; 2,15 / 2,1 trên W1) + điểm DR/1.000 đ. |
| `hour_off_random`, `hour_off_dr_kvnd` | luật tắt cố định theo giờ có hại + tầng rider random / DR. |
| `oracle` | xếp khách theo τ thật của session (đọc biến ẩn): mốc trên của chính sách theo từng khách. |

#### Đường throughput (A1)

Trục ngang demand_scale (×0,25 … ×2,5 trên quy mô Hà Nội), đội xe cố định luôn online, giờ tham chiếu 18h. Đường "hoàn thành / giờ" tăng rồi **giảm** khi cầu vượt cung: đó là wild goose chase. Biểu đồ dưới: ETA đón trung bình tăng theo cầu. Đây là căn cứ vật lý của việc cắt voucher ở vùng căng.

#### GTE = N(all_on) − N(all_off)

Không ngân sách, ghép cặp theo seed, ± SE. Voucher toàn hệ đem lại thêm bao nhiêu chuyến. Chỉ dùng các dòng `all_on` / `all_off` không ngân sách và các seed có cả hai; bảng nào có trùng (policy, seed) thì bỏ qua, vì không xác định được một đối chiếu duy nhất.

#### Thuật ngữ của các phân tích ngoài dashboard (notebook 01–06, `docs/figures/`)

Dashboard không lưu hay hiển thị hình thí nghiệm; hình của báo cáo xuất từ notebook vào `docs/figures/`.

- **Qini:** đường đánh giá mô hình uplift ở cấp cá nhân (xếp khách theo điểm, cộng dồn hiệu quả). Notebook 02 cho thấy Qini xếp ngược với N(π) dưới ngân sách, vì Qini không tính chi phí và không thấy tranh chấp cung.
- **Interference:** voucher của người này làm thay đổi thị trường (xe, ETA) của người khác, nên thí nghiệm theo khách bị chệch.
- **Confounding:** trong dữ liệu lịch sử (legacy), ai được voucher phụ thuộc biến ẩn, nên ước lượng ngây thơ bị lệch.
- **Regret:** N(θ\*) − N(θ̂), mất mát khi dùng ngưỡng ước lượng thay vì ngưỡng tối ưu.

#### Mọi bảng policy_results

Bảng gộp mọi `results/policy_results.parquet` trong `runs/` để tra cứu, ẩn mặc định.

### 8. Mấy điểm hay gây nhầm

- **"Bỏ vì chờ lâu" gần 0 không phải lỗi.** Nó chỉ xảy ra khi không có xe rảnh nào trong bán kính 30 phút; trên quy mô Hà Nội tỷ lệ này ≈ 0,01 % vì sức chịu chờ 5 phút lớn hơn ETA đón trung bình ≈ 3,6 phút. Kênh mất khách chính là "không đặt" (khách thấy ETA xấu rồi tắt app) và "hủy khi xe đang đến" (≈ 54 % số request thêm do voucher bị mất qua hủy).
- **θ là một số cho cả hệ, nhưng kết quả cắt khác nhau theo giờ và theo ô.** Vì ŝ mỗi ô mỗi slot khác nhau. Thiết kế cố ý không đặt θ riêng từng ô.
- **ŝ trễ một slot so với slack thực** vì không được nhìn trước. Đó là giá phải trả của chính sách thực thi được.
- **Chi / B dưới 100 % là bình thường.** Chỉ voucher của chuyến hoàn thành mới tốn tiền; phần giữ và phần đặt của chuyến hủy được nhả lại.
- **Tỷ lệ "(ô, slot) bị cắt" ở trang Cung & chính sách vùng** tính trên mọi slot kể cả warm-up và cool-down, nên khác chút so với KPI ở trang Tổng quan chỉ tính trong cửa sổ.
- **Trục θ trên các biểu đồ cách đều theo lưới**, không theo giá trị. Khoảng 0,1 → 0,2 và 2 → 5 rộng bằng nhau trên hình.
- **Lượt quét θ không có bản đồ**, vì chỉ ghi kết quả tổng hợp. Muốn xem bản đồ tại θ\* thì bấm nút "Chạy một lượt chi tiết tại θ\*".
- **Bảng nhánh có / không voucher ở trang Voucher & ngân sách không phải uplift.** Xem lưu ý ở mục 6.

## Kiến trúc

```
dashboard/
  trace.py      vòng lặp 10 bước giống sim.engine.run + ghi frame mỗi tick (xe, bộ đếm ô, sổ cái bằng đồng, sự kiện) và CellDecision/snapshot mỗi slot
  sweep.py      quét θ: cùng job với runner.sweep_theta (budget_for, resolve_kappas, seeds_for) chạy qua Pool spawn có báo tiến độ từng seed;
                tổng hợp theo θ, tập θ* (analysis.metrics.theta_star_set); ghi results/{policy_results,theta_sweep}, meta/run_metadata (mode sweep_theta)
  jobs.py       hàng đợi một luồng nền cho cả hai loại lượt: load_config → B → κ → mô phỏng → ghi bảng; nạp lại lượt đã xong từ đĩa
  summary.py    KPI, chuỗi theo slot, sự kiện bật/tắt, phân phát voucher, doanh thu, so sánh ghép cặp hai lượt (từ buffer + trace)
  geometry.py   tọa độ tâm ô, vector hiển thị trên torus (xe đi qua biên hiện ra ở cạnh đối diện)
  results.py    đọc results/*.parquet có sẵn trong runs/ (sweep, policy_table, throughput, gte, policy_results)
  server.py     FastAPI: /api/* và static; create_app(root, runs_dir) cho test
  static/       index.html, css/{app,workspace}.css, js/{lib,ui,common,charts,hexmap,overview,research,sweep,pages,app}.js,
                brand/ (logo, nguồn ghi trong brand/README.md), vendor/ (React, ReactDOM, htm)
```

Lượt quét ghi `runs/dashboard/<id>/results/theta_sweep.parquet` đúng như `python -m sim run --mode sweep_theta`, cộng `dashboard/{meta.json, sweep.json}`. Pool tiến trình dùng `spawn` như runner; vì thế server phải chạy từ một module thật (`python -m dashboard`), không chạy từ `python -` hay REPL.

Quy tắc giữ nguyên với simulator:
- `sim/` **không** import từ `dashboard/`; dashboard chỉ dùng các factory công khai (`build_world`, `make_policy`, `runner.*`, `logger.*`).
- `trace.py` **không sửa** `sim/engine.py`: nó chép vòng lặp và chỉ đọc trạng thái. `tests/test_dashboard.py::test_traced_run_matches_engine` kiểm hai vòng lặp cho cùng `RunResult` và cùng bảng session/order trên cấu hình tiny; trên W1 có biến động, hai vòng lặp cũng cho cùng mọi trường kết quả và cùng bảng shocks. Đổi thứ tự bước trong `engine.run` thì sửa `trace.py` theo.
- Mọi số ngẫu nhiên, ngân sách, CRN, không-nhìn-trước y như chạy bằng CLI: một lượt dashboard = `evaluate` một seed với `log_level=full`, cộng frame.
- Bảng ghi ra đúng `docs/schema.md` (`observed/`, `market/`, `hidden/`, `results/policy_results`, `meta/run_metadata`, `mode = dashboard`); thêm `dashboard/{trace.npz, slots.npz, meta.json, geometry.json, summary.json}`.

Frame (mỗi tick): `status`, `cell` (ô hiện tại hoặc ô đích khi đang đi), `origin` (ô xuất phát), `move_start`, `busy_until` của từng xe → front end nội suy vị trí giữa hai tâm ô; `idle/enroute/ontrip/waiting` theo ô; bộ đếm cộng dồn order theo trạng thái, session/offer/blocked/request; sổ cái kỳ hiện tại (cent); danh sách sự kiện `(loại, ô, ô2)`.

## API

| Method | Đường dẫn | Nội dung |
|---|---|---|
| GET | `/api/config/defaults` | `config/default.yaml`, danh sách hàm điểm (built-in, `analysis.score_ties:*`, `analysis.uplift:*` gồm bản `*_per_kvnd` và `_w1`, `analysis.hour_rule:*`), preset W0 / W1 / demo / tiny |
| POST | `/api/config/validate` | `{overrides:[...], layers:[...]}` → hash, số ô, cửa sổ hoặc lỗi `--set` |
| GET/POST | `/api/runs` | danh sách / xếp hàng một lượt một θ (`name, overrides, layers, budget_vnd?, kappa?`; `layers` = lớp config như `config/world_w1.yaml`) |
| POST | `/api/sweeps` | xếp hàng một lượt quét θ (`name, overrides, layers, theta_grid, n_seeds, n_procs?`) |
| GET | `/api/runs/{id}` | trạng thái, tiến độ, log, cấu hình, hình học lưới, KPI (hoặc tóm tắt θ\* với lượt quét) |
| DELETE | `/api/runs/{id}` | bỏ một lượt đã xong hoặc lỗi khỏi danh sách của server (file trên đĩa giữ nguyên) |
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

7 test: tracer trùng engine (RunResult, session, order); frame nhất quán với bộ đếm và bất biến ngân sách theo tick; GTE bỏ lượt có ngân sách và seed không ghép cặp; vector torus; API chạy trọn một lượt tiny (cả doanh thu và so sánh với lượt `all_off` cùng seed) rồi nạp lại từ đĩa; API quét θ (3 θ × 2 seed, pool 2 tiến trình) ra đúng bảng `sweep_theta` và θ\*; API trả preset W0 / W1 với lớp config có thật, nhận scope `city`, không còn endpoint ảnh. Các test API cần extras `[dashboard]` và tự bỏ qua nếu thiếu `fastapi`.

## Giới hạn và việc có thể làm tiếp

- Mô phỏng chạy trong một luồng nền của tiến trình server (GIL): trong lúc chạy, API phản hồi chậm hơn một chút; các lượt xếp hàng chạy tuần tự. W0 (1.150 xe, 61 ô): 1 ngày ≈ 16 giây mô phỏng có trace (engine 15 giây), cộng pilot B và κ auto ≈ 1–2 phút.
- Frame giữ trong bộ nhớ của server trong phiên; lượt dài nhiều ngày sẽ nặng (≈ 27 MB/ngày với 1.150 xe).
- Giao diện viết bằng htm (template literal) để không cần Node. Muốn JSX/TypeScript: tạo dự án Vite trong `dashboard/web/`, chuyển từng file trong `static/js/` (mỗi file đã là một module ES), build ra `static/`.
