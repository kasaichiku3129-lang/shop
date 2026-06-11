## Table `purchases`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `int8` | Primary Identity |
| `created_at` | `timestamptz` |  |
| `purchase_date` | `date` |  Nullable |
| `product_id` | `int8` |  Nullable |
| `quantity` | `numeric` |  Nullable |
| `unit_price` | `numeric` |  Nullable |
| `amount` | `numeric` |  Nullable |
| `invoice_number` | `text` |  Nullable |
| `ocr_text` | `text` |  Nullable |
| `Note` | `text` |  Nullable |
| `kategory` | `text` |  Nullable |
| `product_name` | `text` |  Nullable |

## Table `purchase_products`

仕入商品マスタ

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `int8` | Primary Identity |
| `created_at` | `timestamptz` |  |
| `product_name` | `varchar` |  Nullable |
| `unit` | `unit` |  |
| `supplier_id` | `int8` |  Nullable |

## Table `suppliers`

取引先

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `int8` | Primary Identity |
| `created_at` | `timestamptz` |  |
| `name` | `text` |  Nullable |

## Table `sales`

売上

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `int8` | Primary Identity |
| `created_at` | `timestamptz` |  |
| `sales_date` | `date` |  Nullable |
| `sales_products` | `text` |  Nullable |
| `sales_amount` | `int8` |  Nullable |
| `quantity` | `int8` |  Nullable |
| `product_id` | `int8` |  Nullable |
| `weekday_name` | `text` |  Nullable |

## Table `sales_products`

売上商品マスタ

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `int8` | Primary Identity |
| `created_at` | `timestamptz` |  |
| `sales_category` | `text` |  Nullable |
| `sales_category2` | `text` |  Nullable |
| `sales_products` | `text` |  Nullable |

