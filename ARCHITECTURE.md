# L3A Architecture Record

Team phải cập nhật tài liệu này cùng source. Mục tiêu là mô tả quyết định có thể kiểm chứng, không ghi prompt bí mật hoặc chain-of-thought.

## 1. System overview

Pipeline một chiều, thuần Python `asyncio` (không agent loop, không LLM, không framework ngoài). Mỗi case chạy độc lập; mọi state gắn với đúng một `case_id`.

```text
inputs/<case_id>.json
      │  (CLI emit case_received)
      ▼
 Coordinator ──task_assigned──►┌───────────────────┬───────────────────┐
  (seed KnownIds từ case)      ▼                   ▼                   ▼
                        order-item-agent     payment-agent        shipment-agent   ← asyncio.gather
                        get_order            get_order_payments   get_shipment_summary
                        get_order_items      get_payment_timeline        │
                        get_sellers          get_refund_timeline         │
                               │  MCP qua ScopedGateway (case_id cố định, retry, dedupe)
                               └─────────┬─────────┴───────────────────┘
                                         │ vòng follow-up (tối đa 1) nếu tham số chỉ có
                                         │ trong evidence của specialist khác
                                         ▼ handoff (SpecialistResult)
                                   policy-agent  (get_policy EC_POLICY_V1)
                                         │ policy_decided
                                         ▼
                                  verifier-agent ──(mâu thuẫn, tối đa 1 lần)──► policy-agent
                                         │ verification_completed
                                         ▼
                           outputs/<case_id>.json  (CLI validate schema, emit case_finalized)
```

Code: `runner.run_batch` (`day09 run`) → `workflow.solve_case` → `agents/coordinator.run_case` → `agents/specialists.py` / `agents/policy.py` / `agents/verifier.py` → `agents/toolbox.py` → `mcp_gateway.EvidenceGateway`. Kiểu dữ liệu ở `state.py`, phân quyền ở `permissions.py`, trích dữ kiện ở `agents/facts.py`.

## 2. Agent ownership

Tên actor cố định (`state.Actor`), dùng nguyên văn trong trace.

| Actor | Input | Trách nhiệm | Output/handoff |
| --- | --- | --- | --- |
| Coordinator (`coordinator`) | `inputs/<case_id>.json` | Seed `KnownIds` chỉ từ field tường minh của case (`claimed_order_id` → `order_id`, các `*_id`); giao việc cho 3 specialist song song; chạy tối đa 1 vòng follow-up; gom `CaseState`. Không gọi tool. | `task_assigned` cho từng specialist; `CaseState` cho Policy |
| Order/item (`order-item-agent`) | `order_id` từ case | Lấy đơn hàng, item, seller; phát hiện lệch `claimed_order_id` vs `order_id` trong data | `SpecialistResult` → `handoff` tới `policy-agent` |
| Payment (`payment-agent`) | `order_id` từ case | Lấy dòng thanh toán, timeline capture/mismatch, timeline hoàn tiền | `SpecialistResult` → `handoff` tới `policy-agent` |
| Shipment (`shipment-agent`) | `order_id` từ case | Lấy mốc giao hàng, hạn bàn giao của seller, sự kiện giao hàng | `SpecialistResult` → `handoff` tới `policy-agent` |
| Policy (`policy-agent`) | `CaseState` + `policy_version` của case | Lấy `get_policy`; trích dữ kiện trong cửa sổ case; chọn `primary_issue`, trạng thái/hành động/bên chịu trách nhiệm theo rule policy, số tiền từ evidence | `policy_decided` → `handoff` tới `verifier-agent` |
| Verifier (`verifier-agent`) | Output ứng viên + `CaseState` | Kiểm tra invariant (mục 6), cap `confidence`; trả Policy tối đa 1 lần nếu vi phạm (lần 2 Policy chỉ được kết luận `insufficient_evidence`) | `verification_completed` (`approved`/`revised`); output cho runner |

Quyền gọi tool (enforce trong `permissions.TOOL_GRANTS` + `toolbox.ScopedGateway`; gọi ngoài quyền → `ToolPermissionError`, không có request nào đi ra mạng):

| Actor | Tool | Domain được consume |
| --- | --- | --- |
| coordinator | — | — |
| order-item-agent | `get_order`, `get_order_items`, `get_sellers` | order, item, seller, product, customer |
| payment-agent | `get_order_payments`, `get_payment_timeline`, `get_refund_timeline` | payment, refund |
| shipment-agent | `get_shipment_summary` | shipment |
| policy-agent | `get_policy` | policy |
| verifier-agent | — | — |

Tên tool lấy từ discovery thật (`docs/mcp-tools.md`, sinh bằng `day09 mcp-tools --doc docs/mcp-tools.md`); tool chỉ được gọi nếu `describe_tools` trả về nó, tham số chỉ lấy theo `input_schema.required`. `get_customer_history` và `get_product_context` cố ý không cấp cho agent nào: không quyết định L3A nào cần tới.

## 3. A2A protocol

- **Envelope:** `state.Handoff(case_id, source, target, task, entity_ids, evidence_refs, attempt, payload)`; `source/target` phải thuộc `AGENT_ROLES`, `evidence_refs` phải đúng pattern `ev_…`. `payload` là `SpecialistResult` có cấu trúc (entities, evidence, missing, conflicts) — không có text tự do.
- **Correlation:** mọi message, MCP call và trace event mang `case_id` của case đang xử lý. `ScopedGateway` giữ `case_id` cố định; agent không truyền được `case_id` khác.
- **Điều kiện handoff:** specialist handoff sang `policy-agent` sau khi chạy xong plan (kể cả khi thiếu evidence — phần thiếu nằm trong `payload.missing`). Coordinator chỉ gọi Policy khi cả 3 specialist đã handoff.
- **Timeout:** mỗi MCP call có timeout riêng (mặc định 60 s, `RetryPolicy.call_timeout`).
- **Tránh vòng lặp:** pipeline một chiều; follow-up tối đa 1 vòng và chỉ chạy tool có tham số mới được mở khóa; Verifier → Policy tối đa 1 lần (`attempt ≤ 2`). Cùng `(tool, arguments)` trong một case chỉ gọi 1 lần (cache per case, gồm cả lỗi).
- **Trace (theo thứ tự):** `case_received` (runner) → `task_assigned` (coordinator → 3 specialist) → `tool_result_consumed` (mỗi evidence một event) → `handoff` (specialist → `policy-agent`) → `tool_result_consumed` (`policy-agent`, `get_policy`) → `policy_decided` (decision_code = `primary_issue`) → `handoff` (policy → verifier) → [`handoff` verifier → policy `revise` → `policy_decided` lần 2] → `verification_completed` → `case_finalized` (runner). Chỉ decision code và số liệu quan sát được, không có nội dung suy luận.

## 4. Evidence lifecycle

1. `EvidenceGateway.call` validate envelope theo `mcp-evidence-response-v1.schema.json` (sai → `invalid_envelope`, không dùng).
2. `ScopedGateway` kiểm tra `domain` của envelope thuộc quyền actor (sai → `domain_mismatch`, không dùng).
3. Envelope được ghi vào `EvidenceLedger` của case (ref trùng mà nội dung khác → lỗi) và `CrossCaseGuard` (ref đã thuộc case khác → lỗi, dừng case).
4. Giữ nguyên `evidence_ref`, `result_hash`, `domain`, `data`, `warnings` trong `EvidenceItem` — không sửa, không sinh ref.
5. Ngay khi specialist dùng kết quả: emit `tool_result_consumed` với đúng ref đó.
6. Policy chỉ cite evidence hỗ trợ kết luận (`policy.SUPPORTING_EVIDENCE`, ví dụ `duplicate_charge` → items, payments, payment_timeline, policy); Verifier kiểm tra mọi ref trong output thuộc ledger của case.
7. **Dữ liệu ngoài cửa sổ case:** gateway trả kèm dòng/sự kiện của timeline khác. Chỉ dòng có mốc thời gian trong `[order_purchase_timestamp, opened_at]` được dùng (item theo `shipping_limit_date`, thanh toán/hoàn tiền theo `event_at`); sự kiện giao hàng chỉ dùng khi trùng mốc `order_delivered_customer_date`. Dòng trùng y hệt chỉ tính một lần.

Evidence không cache giữa các case: cache và ledger được tạo mới cho mỗi case.

## 5. Failure policy

| Failure | Retry? | Fallback | Trace event/code |
| --- | --- | --- | --- |
| MCP timeout / lỗi kết nối | Có, tối đa 2 lần, backoff 0.5 s → 1 s | Hết lượt → ghi missing, specialist vẫn handoff | `handoff.attributes.missing_count`; missing `transient_exhausted` |
| Not found | Không | Ghi missing, không đoán dữ liệu thay thế | missing `not_found` |
| 403 / sai scope | Không | Ghi missing; không đổi `case_id` hay tham số | missing `forbidden` |
| Tool không có trong discovery / thiếu tham số bắt buộc | Không gọi | Ghi missing | `tool_not_discovered` / `param_unavailable` / `id_not_in_case` |
| Source conflict | Không | Ghi `DataConflict` (field, sources, `selected_source=null`, `resolution_code`); Policy/Verifier xử lý ở Pha 4 | `handoff.attributes.conflict_count` |
| Invalid specialist result (envelope sai schema, domain ngoài quyền) | Không | Bỏ evidence đó, ghi missing | `invalid_envelope` / `domain_mismatch` |

Retry có giới hạn và idempotent: chỉ retry lỗi tạm thời (timeout, `ConnectionError`, `httpx2.TransportError`); lỗi do server trả về (`isError`) không retry. Missing evidence không bao giờ được chuyển thành dữ liệu phỏng đoán.

## 6. Verification invariants

Trước khi finalize (`agents/verifier.verify`):

- **Schema:** output validate theo `l3a-output-v2.schema.json` (CLI kiểm lại lần nữa).
- **Entity scope:** mọi id trong `affected_entities` xuất hiện trong data của evidence đã consume của chính case.
- **Evidence ownership:** mọi ref trong output ∈ `EvidenceLedger` của case, đã được emit `tool_result_consumed`, không bị `CrossCaseGuard` từ chối.
- **Claim linkage:** mỗi `claim_assessments[]` tham chiếu `claim_id` có trong input, verdict có ref hỗ trợ hoặc là `insufficient_evidence`.
- **Money totals:** `recommended_refund_brl` = tổng `refund_lines[].amount_brl`, không vượt tổng tiền đã capture trong cửa sổ case.
- **Responsibility/action:** bên chịu trách nhiệm lấy từ rule policy của `primary_issue`; `party_id` chỉ có với seller và phải là seller của chính case (không chép id ví dụ trong policy); `case_status` khác `action_required` ⇒ refund = 0; `no_action` ⇒ không có action hoàn tiền.
- **Confidence bounds:** ∈ [0, 1]; gốc 0.9, trừ khi thiếu evidence cần cho issue, có conflict, thiếu policy, hoặc evidence trái với claim của khách; cap 0.85 khi có `data_conflicts`; `insufficient_evidence` = 0.3.

## 7. Reproducibility

- Python 3.11; dependency pin bằng `uv.lock` (`uv sync --python 3.11 --extra dev`).
- Không dùng LLM, không random seed: kết quả chỉ phụ thuộc input và response của gateway.
- Concurrency: case chạy tuần tự; trong một case tối đa 3 specialist song song, mỗi specialist gọi tool tuần tự. Fan-out mỗi tool tối đa 5 bộ tham số. Khoảng 8 MCP call/case.
- Batch (`runner.py`): trace của case ghi tạm ở `traces/.pending/`, chỉ nối vào `traces/trace.jsonl` sau khi output đã ghi; mất phiên MCP → kết nối lại, chạy lại case (tối đa 3 lần; 2 lần đầu không chấp nhận evidence thiếu do mạng). `day09 run --resume` giữ các case đã xong.
- Retry: 2 lần, backoff 0.5 s × 2ⁿ, timeout 60 s/call (`agents/toolbox.RetryPolicy`).
- Lệnh: `day09 mcp-tools --doc docs/mcp-tools.md`, `day09 probe <case_id>...` (chạy đủ pipeline, dump `debug/`), `day09 run [--resume]`, `day09 validate`, `day09 package`.
- Cấu hình qua `.env` (không commit); không ghi API key vào code, log, trace hay debug.
