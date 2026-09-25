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
                        order-item-agent     payment-agent      shipment-agent     ← asyncio.gather
                        get_order,           get_payment        get_shipment
                        get_seller                 │                   │
                               │  MCP qua ScopedGateway (case_id cố định, retry, dedupe)
                               └─────────┬─────────┴───────────────────┘
                                         │ vòng follow-up (tối đa 1) nếu tham số chỉ có
                                         │ trong evidence của specialist khác
                                         ▼ handoff (SpecialistResult)
                                   policy-agent  (get_policy)            ← Pha 4
                                         │ policy_decided
                                         ▼
                                  verifier-agent ──(mâu thuẫn, tối đa 1 lần)──► policy-agent
                                         │ verification_completed
                                         ▼
                           outputs/<case_id>.json  (CLI validate schema, emit case_finalized)
```

Code: `workflow.solve_case` → `agents/coordinator.collect_evidence` → `agents/specialists.py` → `agents/toolbox.py` → `mcp_gateway.EvidenceGateway`. Kiểu dữ liệu ở `state.py`, phân quyền ở `permissions.py`.

## 2. Agent ownership

Tên actor cố định (`state.Actor`), dùng nguyên văn trong trace.

| Actor | Input | Trách nhiệm | Output/handoff |
| --- | --- | --- | --- |
| Coordinator (`coordinator`) | `inputs/<case_id>.json` | Seed `KnownIds` chỉ từ field tường minh của case (`claimed_order_id` → `order_id`, các `*_id`); giao việc cho 3 specialist song song; chạy tối đa 1 vòng follow-up; gom `CaseState`. Không gọi tool. | `task_assigned` cho từng specialist; `CaseState` cho Policy |
| Order/item (`order-item-agent`) | `order_id` từ case; `seller_id` từ data `get_order` | Lấy đơn hàng, item và seller; phát hiện lệch `claimed_order_id` vs `order_id` trong data | `SpecialistResult` → `handoff` tới `policy-agent` |
| Payment (`payment-agent`) | `order_id` (hoặc tham số khác mà schema tool yêu cầu, lấy từ case/evidence) | Lấy giao dịch thanh toán/hoàn tiền | `SpecialistResult` → `handoff` tới `policy-agent` |
| Shipment (`shipment-agent`) | như trên | Lấy vận đơn/timeline giao hàng | `SpecialistResult` → `handoff` tới `policy-agent` |
| Policy (`policy-agent`) — Pha 4 | `CaseState` + policy evidence | Chọn `primary_issue`, `responsible_parties`, `financial_resolution`, `resolution_actions` theo `policy_version` | `policy_decided` → Verifier |
| Verifier (`verifier-agent`) — Pha 4 | Quyết định Policy + `CaseState` | Kiểm tra invariant (mục 6), calibrate `confidence`; trả Policy tối đa 1 lần nếu mâu thuẫn | `verification_completed`; output cho CLI |

Quyền gọi tool (enforce trong `permissions.TOOL_GRANTS` + `toolbox.ScopedGateway`; gọi ngoài quyền → `ToolPermissionError`, không có request nào đi ra mạng):

| Actor | Tool | Domain được consume |
| --- | --- | --- |
| coordinator | — | — |
| order-item-agent | `get_order`, `get_seller` | order, item, seller, product, customer |
| payment-agent | `get_payment` | payment, refund |
| shipment-agent | `get_shipment` | shipment |
| policy-agent | `get_policy` | policy |
| verifier-agent | — | — |

Tên tool lấy từ đề; tool chỉ được gọi nếu tool discovery (`describe_tools`) trả về nó, tham số chỉ lấy theo `inputSchema.required` thật. Danh mục đầy đủ: `docs/mcp-tools.md` (sinh bằng `day09 mcp-tools --doc docs/mcp-tools.md`).

## 3. A2A protocol

- **Envelope:** `state.Handoff(case_id, source, target, task, entity_ids, evidence_refs, attempt, payload)`; `source/target` phải thuộc `AGENT_ROLES`, `evidence_refs` phải đúng pattern `ev_…`. `payload` là `SpecialistResult` có cấu trúc (entities, evidence, missing, conflicts) — không có text tự do.
- **Correlation:** mọi message, MCP call và trace event mang `case_id` của case đang xử lý. `ScopedGateway` giữ `case_id` cố định; agent không truyền được `case_id` khác.
- **Điều kiện handoff:** specialist handoff sang `policy-agent` sau khi chạy xong plan (kể cả khi thiếu evidence — phần thiếu nằm trong `payload.missing`). Coordinator chỉ gọi Policy khi cả 3 specialist đã handoff.
- **Timeout:** mỗi MCP call có timeout riêng (mặc định 60 s, `RetryPolicy.call_timeout`).
- **Tránh vòng lặp:** pipeline một chiều; follow-up tối đa 1 vòng và chỉ chạy tool có tham số mới được mở khóa; Verifier → Policy tối đa 1 lần (`attempt ≤ 2`). Cùng `(tool, arguments)` trong một case chỉ gọi 1 lần (cache per case, gồm cả lỗi).
- **Trace:** chỉ event quan sát được và decision code: `task_assigned` (coordinator → specialist, `attributes.round`), `tool_result_consumed` (actor = specialist, `tool_name`, `evidence_refs=[ref]`), `handoff` (specialist → `policy-agent`, kèm refs và số lượng missing/conflict). `case_received`/`case_finalized` do CLI emit.

## 4. Evidence lifecycle

1. `EvidenceGateway.call` validate envelope theo `mcp-evidence-response-v1.schema.json` (sai → `invalid_envelope`, không dùng).
2. `ScopedGateway` kiểm tra `domain` của envelope thuộc quyền actor (sai → `domain_mismatch`, không dùng).
3. Envelope được ghi vào `EvidenceLedger` của case (ref trùng mà nội dung khác → lỗi) và `CrossCaseGuard` (ref đã thuộc case khác → lỗi, dừng case).
4. Giữ nguyên `evidence_ref`, `result_hash`, `domain`, `data`, `warnings` trong `EvidenceItem` — không sửa, không sinh ref.
5. Ngay khi specialist dùng kết quả: emit `tool_result_consumed` với đúng ref đó.
6. Pha 4: output `evidence_refs` / `claim_assessments[].evidence_refs` chỉ chọn trong `CaseState.evidence_refs` và chỉ ref thật sự hỗ trợ kết luận; `ledger.require()` chặn ref lạ.

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

Trước khi finalize (Verifier, Pha 4):

- **Schema:** output validate theo `l3a-output-v2.schema.json` (CLI kiểm lại lần nữa).
- **Entity scope:** mọi id trong `affected_entities` xuất hiện trong data của evidence đã consume của chính case.
- **Evidence ownership:** mọi ref trong output ∈ `EvidenceLedger` của case, đã được emit `tool_result_consumed`, không bị `CrossCaseGuard` từ chối.
- **Claim linkage:** mỗi `claim_assessments[]` tham chiếu `claim_id` có trong input, verdict có ref hỗ trợ hoặc là `insufficient_evidence`.
- **Money totals:** `recommended_refund_brl` = tổng `refund_lines[].amount_brl`, không vượt số tiền đã thanh toán theo evidence.
- **Responsibility/action:** bên chịu trách nhiệm khớp `primary_issue`; `case_status=no_action` ⇒ refund = 0 và không có action hoàn tiền.
- **Confidence bounds:** ∈ [0, 1]; giảm khi có missing/conflict; không 1.0 khi còn mâu thuẫn.

## 7. Reproducibility

- Python 3.11; dependency pin bằng `uv.lock` (`uv sync --python 3.11 --extra dev`).
- Không dùng LLM, không random seed: kết quả chỉ phụ thuộc input và response của gateway.
- Concurrency: case chạy tuần tự; trong một case tối đa 3 specialist song song, mỗi specialist gọi tool tuần tự. Fan-out mỗi tool tối đa 5 bộ tham số.
- Retry: 2 lần, backoff 0.5 s × 2ⁿ, timeout 60 s/call (`agents/toolbox.RetryPolicy`).
- Lệnh: `day09 mcp-tools --doc docs/mcp-tools.md`, `day09 probe <case_id>` (dump `debug/`), `day09 run`, `day09 validate`, `day09 package`.
- Cấu hình qua `.env` (không commit); không ghi API key vào code, log, trace hay debug.
