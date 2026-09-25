# L3A Architecture Record

Mục tiêu là mô tả các quyết định có thể kiểm chứng, không ghi prompt bí mật hoặc
chain-of-thought. Public contracts trong `contracts/schemas/` là bất biến đối với
workflow; mọi output, trace event và MCP response đều phải đi qua `Contracts`.

## 1. System overview

Vẽ hoặc mô tả luồng từ `inputs/<case_id>.json` đến MCP calls, specialist agents, verifier, output và trace.

```text
                     Coordinator / Router
                            │ handoff
      ┌───────────────────────────┼───────────────────────────┐
      ▼                           ▼                           ▼
  Order/Item                   Payment                    Shipment
      └───────────────────────────┼───────────────────────────┘
                            │ evidence handoff
                        Policy Agent
                            │ policy decision
                        Verifier Agent
                            │ validated output
                            Output
```

Coordinator là nơi duy nhất điều phối vòng đời case. Specialist chỉ nhận task
được giao, gọi các tool thuộc ownership của mình và trả về kết quả có evidence
refs. Policy Agent tổng hợp các kết quả specialist; Verifier kiểm tra invariants
trước khi coordinator finalize.

Lifecycle observable bắt buộc:

```text
case_received → task_assigned → tool_result_consumed → handoff
→ policy_decided → verification_completed → case_finalized
```

`policy_decided` chỉ được emit sau khi Policy Agent nhận policy evidence. Verifier
không thay đổi evidence; nó chỉ chấp nhận output khi consistency checks và schema
checks đều pass.

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Output/handoff |
| --- | --- | --- | --- |
| Actor | Input | Trách nhiệm | Output/handoff |
| --- | --- | --- | --- |
| Coordinator / Router | Case nguyên bản | Kiểm tra `case_id`, phân rã claim, giao task, gom kết quả, kiểm soát timeout và finalize | Handoff tới specialists; candidate output tới verifier |
| Order/Item Agent | Claim và order/item identifiers | Xác minh order status, item, seller và các entity liên quan | Findings + evidence refs tới Policy |
| Payment Agent | Claim và payment identifiers | Đối chiếu payment status, amount, installments và mismatch/duplicate charge | Findings + evidence refs tới Policy |
| Shipment Agent | Claim và shipment identifiers | Đối chiếu shipment status, promised/actual delivery và bên logistics | Findings + evidence refs tới Policy |
| Policy Agent | Findings của specialists | Áp dụng policy evidence, phân loại responsibility và resolution | Policy decision + evidence refs tới Verifier |
| Verifier Agent | Candidate output và evidence ledger | Kiểm tra schema, scope, claim linkage, tiền và confidence; từ chối dữ liệu thiếu chứng cứ | Validated output hoặc verification failure |

Mỗi actor chỉ gọi MCP tool thuộc domain được giao. Tên tool phải lấy từ
`gateway.list_tools()`; workflow không hard-code một tên tool chưa được discovery.

Nêu rõ actor nào được quyền gọi tool nào. Tránh cho mọi agent quyền truy vấn tất cả tool nếu không cần thiết.

## 3. A2A protocol

Handoff nội bộ dùng envelope logic sau:

```text
{
    "case_id": "...",
    "source": "coordinator",
    "target": "payment-agent",
    "task": "payment_claim_verification",
    "entity_ids": ["..."],
    "evidence_refs": ["ev_..."],
    "attempt": 1
}
```

`case_id` là correlation key bắt buộc. `evidence_refs` chỉ được truyền tiếp sau
khi response đã được `Contracts.validate_evidence()` kiểm tra. Handoff phải có
đích duy nhất, giới hạn một lần retry cho mỗi task và không được quay lại actor
đã hoàn tất cùng task. Trace chỉ ghi event/decision code quan sát được, không ghi
nội dung suy luận riêng.

## 4. Evidence lifecycle

1. Specialist discovery tool trước khi gọi; mọi call luôn truyền đúng `case_id`.
2. `EvidenceGateway` validate envelope MCP, sau đó specialist lưu nguyên
    `evidence_ref` vào evidence ledger của case.
3. Specialist emit `tool_result_consumed` với đúng `tool_name` và ref đã nhận.
4. Policy/Verifier chỉ được trích dẫn refs tồn tại trong ledger của case hiện tại.
5. Output `evidence_refs` là tập hợp các refs thực sự hỗ trợ kết luận.

Không sửa ref, không tự tạo ref và không chuyển missing evidence thành dữ liệu
phỏng đoán. Evidence ledger không dùng chung giữa các case.

## 5. Failure policy

| Failure | Retry? | Fallback | Trace event/code |
| --- | --- | --- | --- |
| MCP timeout | Một lần, chỉ với call idempotent | Không kết luận từ dữ liệu thiếu | `tool_result_consumed` chỉ khi thành công; verifier trả `needs_investigation` |
| Not found | Không retry tự động | Giữ claim unsupported/insufficient evidence | `decision_code=EVIDENCE_NOT_FOUND` |
| Source conflict | Không retry | Policy chọn source có authority hoặc trả conflict | `decision_code=SOURCE_CONFLICT` |
| Invalid specialist result | Không retry quá một lần | Verifier từ chối finalize | `decision_code=INVALID_HANDOFF` |

Retry phải có giới hạn và idempotent. Không chuyển missing evidence thành dữ liệu phỏng đoán.

## 6. Verification invariants

Trước `case_finalized`, Verifier phải kiểm tra:

- output validate với `l3a-output-v2.schema.json`;
- `case_id` và mọi entity thuộc đúng input case;
- mọi evidence ref đúng pattern, tồn tại trong ledger và thuộc case hiện tại;
- claim, root cause, policy decision và action có evidence linkage;
- tổng refund lines khớp `recommended_refund_brl`;
- responsibility phù hợp với primary issue và resolution actions;
- confidence nằm trong `[0, 1]` và giảm khi dữ liệu thiếu/conflict.

Confidence calibration dùng mức cơ sở theo evidence đã liên kết: không có
evidence hỗ trợ thì `0.2`, có ít evidence hoặc có specialist failure thì giảm,
và chỉ đạt mức cao khi có nhiều evidence hỗ trợ mà không có failure liên quan.

## 7. Reproducibility

Workflow hiện không phụ thuộc model ngẫu nhiên. Ghi dependency/config, giới hạn
concurrency, retry limit và lệnh chạy trong tài liệu; tuyệt đối không ghi API key.
