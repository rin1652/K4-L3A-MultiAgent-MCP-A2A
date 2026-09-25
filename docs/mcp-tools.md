# MCP tools

Sinh tự động từ tool discovery của MCP Evidence Gateway (`day09 mcp-tools --doc`).
Không sửa tay: chạy lại lệnh khi gateway thay đổi.

| Tool | Domain | Agent được phép gọi | Tham số bắt buộc | Mô tả |
| --- | --- | --- | --- | --- |
| `get_customer_history` | — | — (chưa phân quyền) | `customer_unique_id` | Return authoritative order history for one scoped customer identity. |
| `get_order` | order | order-item-agent | `order_id` | Return the authoritative order row for one order. |
| `get_order_items` | — | — (chưa phân quyền) | `order_id` | Return item and seller rows belonging to one order. |
| `get_order_payments` | — | — (chưa phân quyền) | `order_id` | Return payment rows and lifecycle evidence belonging to one order. |
| `get_payment_timeline` | — | — (chưa phân quyền) | `order_id` | Return base payments and authoritative payment lifecycle events. |
| `get_policy` | policy | policy-agent | `policy_version` | Return the public machine-readable policy for the requested version. |
| `get_product_context` | — | — (chưa phân quyền) | `order_id` | Return products and translated categories associated with a scoped order. |
| `get_refund_timeline` | — | — (chưa phân quyền) | `order_id` | Return authoritative refund lifecycle events for a scoped order. |
| `get_sellers` | — | — (chưa phân quyền) | `order_id` | Return seller records associated with an order's items. |
| `get_shipment_summary` | — | — (chưa phân quyền) | `order_id` | Return delivery timestamps, seller handoff limits and shipment events. |

## `get_customer_history`

Return authoritative order history for one scoped customer identity.

| Tham số | Kiểu | Bắt buộc | Mô tả |
| --- | --- | --- | --- |
| `case_id` | string | có |  |
| `customer_unique_id` | string | có |  |

## `get_order`

Return the authoritative order row for one order.

| Tham số | Kiểu | Bắt buộc | Mô tả |
| --- | --- | --- | --- |
| `case_id` | string | có |  |
| `order_id` | string | có |  |

## `get_order_items`

Return item and seller rows belonging to one order.

| Tham số | Kiểu | Bắt buộc | Mô tả |
| --- | --- | --- | --- |
| `case_id` | string | có |  |
| `order_id` | string | có |  |

## `get_order_payments`

Return payment rows and lifecycle evidence belonging to one order.

| Tham số | Kiểu | Bắt buộc | Mô tả |
| --- | --- | --- | --- |
| `case_id` | string | có |  |
| `order_id` | string | có |  |

## `get_payment_timeline`

Return base payments and authoritative payment lifecycle events.

| Tham số | Kiểu | Bắt buộc | Mô tả |
| --- | --- | --- | --- |
| `case_id` | string | có |  |
| `order_id` | string | có |  |

## `get_policy`

Return the public machine-readable policy for the requested version.

| Tham số | Kiểu | Bắt buộc | Mô tả |
| --- | --- | --- | --- |
| `case_id` | string | có |  |
| `policy_version` | string | có |  |

## `get_product_context`

Return products and translated categories associated with a scoped order.

| Tham số | Kiểu | Bắt buộc | Mô tả |
| --- | --- | --- | --- |
| `case_id` | string | có |  |
| `order_id` | string | có |  |

## `get_refund_timeline`

Return authoritative refund lifecycle events for a scoped order.

| Tham số | Kiểu | Bắt buộc | Mô tả |
| --- | --- | --- | --- |
| `case_id` | string | có |  |
| `order_id` | string | có |  |

## `get_sellers`

Return seller records associated with an order's items.

| Tham số | Kiểu | Bắt buộc | Mô tả |
| --- | --- | --- | --- |
| `case_id` | string | có |  |
| `order_id` | string | có |  |

## `get_shipment_summary`

Return delivery timestamps, seller handoff limits and shipment events.

| Tham số | Kiểu | Bắt buộc | Mô tả |
| --- | --- | --- | --- |
| `case_id` | string | có |  |
| `order_id` | string | có |  |
