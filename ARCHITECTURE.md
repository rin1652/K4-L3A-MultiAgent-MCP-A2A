# L3A Architecture Record

Team phải cập nhật tài liệu này cùng source. Mục tiêu là mô tả quyết định có thể kiểm chứng, không ghi prompt bí mật hoặc chain-of-thought.

## 1. System overview

```text
Input → Coordinator → Order/Item → Payment → Shipment → Policy → Verifier → Output
                         │            │          │         │
                         └──────── MCP Evidence Gateway ───┘
                                      │
                                   TraceWriter
```

`solve_case` điều phối các specialist qua handoff A2A, mỗi agent chỉ gọi tool trong domain của mình, ghi `tool_result_consumed`, rồi verifier phát `verification_completed`.

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Output/handoff |
| --- | --- | --- | --- |
| Coordinator | case JSON | Parse `claimed_order_id`, claims, policy_version; mở handoff | task tới order-item-agent |
| Order/item | order_id | `get_order`, `get_order_items`, `get_sellers` | entities + refs → payment-agent |
| Payment | order_id | `get_order_payments`, `get_payment_timeline`, optional `get_refund_timeline` | payment facts → shipment-agent |
| Shipment | order_id | `get_shipment_summary` | delivery timeline → policy-agent |
| Policy | policy_version + facts | `get_policy`; map issue → status/refund/actions | decision → verifier-agent |
| Verifier | draft output + ledger | Kiểm tra evidence ownership; emit verification | final output dict |

## 3. A2A protocol

Envelope `Handoff(case_id, source, target, task, entity_ids, evidence_refs, attempt)`.

- Correlation theo `case_id`.
- Mỗi handoff emit `task_assigned` rồi `handoff`.
- Luồng tuyến tính một chiều, không vòng lặp; tool optional thất bại thì bỏ qua, không bịa data.

## 4. Evidence lifecycle

1. `gateway.call` → validate MCP schema.
2. `EvidenceLedger.record` chỉ nhận `evidence_ref` từ server.
3. Emit `tool_result_consumed` với actor/tool/refs.
4. Output `evidence_refs` = ledger của đúng case; `ledger.require` trước finalize.

## 5. Failure policy

| Failure | Retry? | Fallback | Trace event/code |
| --- | --- | --- | --- |
| MCP timeout / tool error | Không (optional tools) | Bỏ tool đó, tiếp tục với evidence còn lại | không emit consumed |
| Missing order_id | Không | `insufficient_evidence` | `verification_completed` / `missing_order_id` |
| Source conflict (order vs shipment status) | Không | Prefer `get_order` | ghi `data_conflicts` |
| Invalid specialist result | Không | Không invent evidence | raise / skip optional |

## 6. Verification invariants

- Output khớp `l3a-output-v2`.
- `case_id` khớp input.
- Mọi `evidence_refs` thuộc ledger case hiện tại.
- `financial_resolution.currency == BRL`.
- Claim assessments gắn claim_id từ input.
- Confidence trong [0, 1].

## 7. Reproducibility

- Python 3.11+, deps theo `pyproject.toml`.
- Chạy: `day09 run` rồi `day09 validate`.
- Không ghi API key trong tài liệu này.
