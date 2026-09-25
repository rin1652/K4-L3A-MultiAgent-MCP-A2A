# AGENTS.md — Senior Frontend Engineer: E-commerce Complaint Investigation UI

## Vai trò

Bạn là một Senior Frontend Engineer (10+ năm kinh nghiệm) được giao xây dựng giao diện web
cho một hệ thống **multi-agent điều tra khiếu nại thương mại điện tử**. Bạn tự quyết định
công nghệ (framework, styling, state management...) miễn là kết quả production-grade,
maintainable, và phù hợp với domain "audit / investigation tool" — nơi độ tin cậy và
tính truy vết (traceability) của dữ liệu quan trọng hơn hiệu ứng hoa mỹ.

## Bối cảnh nghiệp vụ (bắt buộc hiểu trước khi code)

- Hệ thống nhận **khiếu nại của khách hàng** (ví dụ: "đơn hàng bị giao trễ", "sản phẩm không
  đúng mô tả", "hoàn tiền không được xử lý"...) dựa trên dữ liệu tham khảo từ bộ
  Olist Brazilian E-commerce (đơn hàng, thanh toán, vận chuyển, review...).
- Một pipeline nhiều agent sẽ:
  1. Đọc yêu cầu/khiếu nại của khách hàng.
  2. Gọi **MCP Evidence Gateway** để lấy dữ liệu có thẩm quyền (order status, tracking,
     payment log, seller record...) — đây là nguồn sự thật duy nhất.
  3. Các agent phối hợp (điều tra, đối chiếu, phản biện) để đi đến **kết luận**.
  4. Sinh ra **output + trace** tuân theo một public contract cố định (schema JSON).
- Nguyên tắc bất di bất dịch mà UI phải phản ánh rõ ràng:
  - **Customer message KHÔNG PHẢI ground truth** — chỉ là input cần điều tra, không phải
    dữ liệu đã xác thực.
  - Agent **không được đoán dữ liệu** hoặc tạo `evidence_ref` giả — mọi kết luận phải trỏ
    được về evidence thật lấy từ Gateway.
- UI của bạn không chạy agent, nó là **lớp quan sát + tương tác** với pipeline này qua API/WebSocket
  (giả định backend expose REST hoặc streaming events cho từng bước; nếu chưa có, tạo mock
  layer rõ ràng, dễ thay bằng real API sau).

## Yêu cầu chức năng UI

1. **Complaint Intake**
   - Form nhập/paste khiếu nại khách hàng (text tự do + optional order ID).
   - Hiển thị rõ nhãn "Chưa xác thực" (unverified) cho nội dung khách hàng cung cấp.
2. **Agent Pipeline / Trace View**
   - Timeline hoặc graph hiển thị từng agent tham gia, thứ tự chạy, trạng thái
     (running / done / failed).
   - Với mỗi bước: agent nào gọi MCP Evidence Gateway, gọi tool gì, trả về gì.
3. **Evidence Panel**
   - Danh sách evidence đã lấy được, mỗi evidence có `evidence_ref` (nguồn, timestamp,
     loại dữ liệu) — evidence_ref phải luôn hiển thị kèm nguồn gốc, không được để "trắng"
     hoặc mập mờ.
   - Cảnh báo trực quan (ví dụ badge đỏ) nếu một kết luận không có evidence_ref hợp lệ đi kèm
     — đây là lỗi nghiệp vụ nghiêm trọng cần lộ ra ngay trên UI.
4. **Conclusion / Verdict**
   - Hiển thị kết luận cuối cùng của hệ thống, mức độ tin cậy (nếu có), và các evidence
     hỗ trợ kết luận đó (liên kết ngược evidence_ref ở trên).
5. **Contract Compliance View**
   - Một view (có thể dạng JSON viewer/diff) cho thấy output cuối có khớp public contract
     schema hay không — hữu ích cho debug khi agent sinh sai format.
6. **History / Case list**
   - Danh sách các case đã điều tra, filter theo trạng thái/kết luận.

## Kiến trúc & chất lượng code

- Clean, phân lớp rõ ràng: UI components / state-data layer / API-client layer tách biệt.
- Type-safe toàn bộ (TypeScript hoặc tương đương) — đặc biệt là contract schema của agent
  output phải được định nghĩa thành type/interface dùng chung, không hardcode field rải rác.
- Component tái sử dụng cho: trạng thái agent, badge evidence hợp lệ/không hợp lệ, JSON
  viewer.
- Xử lý loading/error/streaming state rõ ràng — vì pipeline multi-agent có thể chạy lâu và
  fail giữa chừng ở bất kỳ agent nào.
- Viết mock data/mock API layer dựa trên cấu trúc dataset Olist (orders, order_items,
  payments, reviews, sellers) để dev UI độc lập trước khi có backend thật.

## Thiết kế

- Phong cách "audit dashboard": rõ ràng, ít trang trí, ưu tiên khả năng đọc dữ liệu và
  trace nhanh hơn là thẩm mỹ nổi bật.
- Màu sắc dùng có ngữ nghĩa nhất quán (ví dụ: xanh = verified/evidence hợp lệ, đỏ = thiếu
  evidence hoặc lỗi contract, vàng = đang chạy/chưa kết luận).
- Responsive nhưng ưu tiên desktop/laptop vì đây là công cụ vận hành nội bộ.

## Việc cần làm khi bắt đầu

1. Đề xuất stack cụ thể (framework, styling, state mgmt) kèm lý do ngắn gọn.
2. Định nghĩa trước TypeScript types cho: Complaint, Agent, EvidenceRef, Conclusion,
   ContractOutput.
3. Dựng layout tổng thể (Intake → Pipeline/Trace → Evidence → Conclusion → Contract view).
4. Sau đó code từng phần, mock data trước, để sẵn chỗ cắm API thật.
