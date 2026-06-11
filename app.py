import os
import re
import json
import io
import hashlib
from functools import lru_cache
import base64
import calendar
from datetime import date, datetime, timedelta
from collections import defaultdict
from dataclasses import dataclass, field

import pandas as pd
from difflib import SequenceMatcher
from urllib import request, error

from dotenv import load_dotenv
from PIL import Image, ImageOps
import streamlit as st
import streamlit.components.v1 as components
from supabase import Client, create_client

from invoice_camera_component import capture_invoice_camera_image

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

SUPABASE_TABLE_PURCHASES = os.getenv("SUPABASE_PURCHASES_TABLE", "purchases")
SUPABASE_PURCHASES_NOTE_COLUMN = (os.getenv("SUPABASE_PURCHASES_NOTE_COLUMN") or "Note").strip()
SUPABASE_TABLE_PRODUCTS = (os.getenv("SUPABASE_TABLE_PRODUCTS") or "purchase_products").strip()
SUPABASE_TABLE_SUPPLIERS = (os.getenv("SUPABASE_TABLE_SUPPLIERS") or "suppliers").strip()
SUPABASE_SUPPLIERS_NAME_COLUMN = (os.getenv("SUPABASE_SUPPLIERS_NAME_COLUMN") or "name").strip()
SUPABASE_PRODUCTS_NAME_COLUMN = (os.getenv("SUPABASE_PRODUCTS_NAME_COLUMN") or "product_name").strip()
SUPABASE_PRODUCTS_SUPPLIER_COLUMN = (os.getenv("SUPABASE_PRODUCTS_SUPPLIER_COLUMN") or "supplier_id").strip()
SUPABASE_PRODUCTS_ROW_ID_COLUMN = (os.getenv("SUPABASE_PRODUCTS_ROW_ID_COLUMN") or "id").strip()
SUPABASE_PRODUCTS_CODE_COLUMN = (
    os.getenv("SUPABASE_PRODUCTS_CODE_COLUMN")
    or os.getenv("SUPABASE_PRODUCTS_ID_COLUMN")
    or ""
).strip()
SUPABASE_PURCHASES_PRODUCT_ID_COLUMN = (
    os.getenv("SUPABASE_PURCHASES_PRODUCT_ID_COLUMN") or "product_id"
).strip()
SUPABASE_PURCHASES_PRODUCT_NAME_COLUMN = (
    os.getenv("SUPABASE_PURCHASES_PRODUCT_NAME_COLUMN") or "product_name"
).strip()
SUPABASE_TABLE_SALES = (os.getenv("SUPABASE_TABLE_SALES") or "sales").strip()
SUPABASE_TABLE_SALES_PRODUCTS = (
    os.getenv("SUPABASE_TABLE_SALES_PRODUCTS") or "sales_products"
).strip()
SUPABASE_SALES_DATE_COLUMN = (os.getenv("SUPABASE_SALES_DATE_COLUMN") or "sales_date").strip()
SUPABASE_SALES_PRODUCTS_COLUMN = (
    os.getenv("SUPABASE_SALES_PRODUCTS_COLUMN")
    or os.getenv("SUPABASE_SALES_PRODUCT_NAME_COLUMN")
    or "sales_products"
).strip()
SUPABASE_SALES_PRODUCT_ID_COLUMN = (
    os.getenv("SUPABASE_SALES_PRODUCT_ID_COLUMN")
    or os.getenv("SUPABASE_SALES_CATEGORY_FK_COLUMN")
    or "product_id"
).strip()
SUPABASE_SALES_PRODUCTS_CATEGORY_COLUMN = (
    os.getenv("SUPABASE_SALES_PRODUCTS_CATEGORY_COLUMN") or "sales_category"
).strip()
SUPABASE_SALES_PRODUCTS_CATEGORY2_COLUMN = (
    os.getenv("SUPABASE_SALES_PRODUCTS_CATEGORY2_COLUMN") or "sales_category2"
).strip()
SUPABASE_SALES_PRODUCTS_MASTER_NAME_COLUMN = (
    os.getenv("SUPABASE_SALES_PRODUCTS_MASTER_NAME_COLUMN") or "sales_products"
).strip()
# 旧スキーマ互換: sales 行に text の部門列がある場合のみ .env で指定
SUPABASE_SALES_KATEGORY_COLUMN = (
    os.getenv("SUPABASE_SALES_KATEGORY_COLUMN")
    or os.getenv("SUPABASE_SALES_DEPARTMENT_COLUMN")
    or ""
).strip()
SUPABASE_SALES_AMOUNT_COLUMN = (os.getenv("SUPABASE_SALES_AMOUNT_COLUMN") or "sales_amount").strip()
SUPABASE_SALES_QUANTITY_COLUMN = (os.getenv("SUPABASE_SALES_QUANTITY_COLUMN") or "quantity").strip()
SUPABASE_SALES_WEEKDAY_COLUMN = (
    os.getenv("SUPABASE_SALES_WEEKDAY_COLUMN") or "weekday_name"
).strip()
SUPABASE_PURCHASES_KATEGORY_COLUMN = (
    os.getenv("SUPABASE_PURCHASES_KATEGORY_COLUMN") or "kategory"
).strip()

# PostgREST / Supabase API の1リクエストあたりの既定上限（.limit() より優先される）
POSTGREST_PAGE_SIZE = 1000

PURCHASE_NAV_PAGES = ("伝票読み取り", "DB閲覧", "購入履歴", "ダッシュボード")
SALES_NAV_PAGES = ("CSV取り込み", "売上履歴", "ダッシュボード")
ANALYST_NAV_SECTION = "分析・予測"
ANALYST_NAV_PAGE = "AIアナリスト"

# サイドバーは1つのラジオのみ（仕入・売上・アナリストで同時選択しない）
NAV_MENU_ITEMS: tuple[tuple[str, str], ...] = (
    *(("仕入", p) for p in PURCHASE_NAV_PAGES),
    *(("売上", p) for p in SALES_NAV_PAGES),
    (ANALYST_NAV_SECTION, ANALYST_NAV_PAGE),
)

_APP_DIR = os.path.dirname(os.path.abspath(__file__))
_PROMPTS_DIR = os.path.join(_APP_DIR, "prompts")


@lru_cache(maxsize=32)
def load_prompt_text(filename: str) -> str:
    """prompts/ 配下のテキストファイルを読み込む（UTF-8）。"""
    path = os.path.join(_PROMPTS_DIR, filename)
    if not os.path.isfile(path):
        raise FileNotFoundError(f"プロンプトファイルが見つかりません: {path}")
    with open(path, encoding="utf-8") as f:
        return f.read()


INVOICE_FORM_OPTIONS: dict[int, dict[str, str]] = {
    1: {
        "label": "帳票1：関東日本フード",
        "supplier": "関東日本フード（株）",
        "partner_hint": "関東日本フード",
    },
    2: {
        "label": "帳票2：全農石川",
        "supplier": "全農石川",
        "partner_hint": "全農石川",
    },
    3: {
        "label": "帳票3：天狗中田本店",
        "supplier": "（株）天狗中田本店",
        "partner_hint": "天狗中田本店",
    },
}

_INVOICE_USER_PROMPT_FILE = "invoice_extraction_user.txt"


def default_supplier_for_form_type(invoice_form_type: int) -> str:
    """UI で選んだ帳票種別に対応する取引先（明細への既定値）。"""
    meta = INVOICE_FORM_OPTIONS.get(invoice_form_type)
    return meta["supplier"] if meta else "関東日本フード（株）"


def partner_hint_for_form_type(invoice_form_type: int) -> str:
    """products マスタ絞り込み用の取引先キーワード。"""
    meta = INVOICE_FORM_OPTIONS.get(invoice_form_type)
    return meta["partner_hint"] if meta else "関東日本フード"


def _extract_invoice_prompt_section(template: str, tag: str) -> str:
    """invoice_extraction_user.txt 内の <<TAG>> セクションを取り出す。"""
    pattern = rf"<<{re.escape(tag)}>>\s*(.*?)(?=<<[A-Z0-9_]+>>|\Z)"
    m = re.search(pattern, template, flags=re.DOTALL)
    if not m:
        raise ValueError(f"プロンプトセクション <<{tag}>> が見つかりません: {_INVOICE_USER_PROMPT_FILE}")
    return m.group(1).strip()


def build_invoice_extraction_prompts(
    invoice_form_type: int,
    master_product_names: list[str] | None = None,
) -> tuple[str, str]:
    """伝票読み取り用の (user_prompt, system_prompt)。帳票種別は呼び出し側で指定。"""
    if invoice_form_type not in INVOICE_FORM_OPTIONS:
        raise ValueError("invoice_form_type は 1, 2, 3 のいずれかを指定してください。")
    master_block = ""
    if master_product_names:
        listed = format_master_product_names_for_ai_prompt(master_product_names)
        if listed.strip():
            col = SUPABASE_PRODUCTS_NAME_COLUMN or "product_name"
            master_block = "\n" + load_prompt_text("invoice_master_catalog.txt").format(
                product_name_column=col,
                listed=listed,
            )
    snap_hint = "。その後、上記【商品マスタとの整合】に従い登録名に寄せる。" if master_block else ""
    template = load_prompt_text(_INVOICE_USER_PROMPT_FILE)
    intro = _extract_invoice_prompt_section(template, "COMMON_INTRO").format(
        invoice_form_type=invoice_form_type
    )
    form_body = _extract_invoice_prompt_section(template, f"FORM{invoice_form_type}").format(
        snap_hint=snap_hint
    )
    footer = _extract_invoice_prompt_section(template, "COMMON_FOOTER")
    user_prompt = f"{intro}\n\n{form_body}\n\n{footer}{master_block}"
    system_prompt = load_prompt_text("invoice_extraction_system.txt").strip()
    return user_prompt, system_prompt


def get_supabase_client() -> Client | None:
    """SUPABASE_URL とキーが揃っていればクライアントを返す。キーは service_role 推奨（RLS をバイパス）。"""
    url = (os.getenv("SUPABASE_URL") or "").strip()
    key = (
        (os.getenv("SUPABASE_SERVICE_ROLE_KEY") or "").strip()
        or (os.getenv("SUPABASE_KEY") or "").strip()
        or (os.getenv("SUPABASE_ANON_KEY") or "").strip()
    )
    if not url or not key:
        return None
    return create_client(url, key)


def _supabase_auth_url_and_key() -> tuple[str, str]:
    """GoTrue 用。公開 anon（`SUPABASE_KEY` または `SUPABASE_ANON_KEY`）。service_role は使わない。"""
    url = (os.getenv("SUPABASE_URL") or "").strip()
    key = (os.getenv("SUPABASE_KEY") or os.getenv("SUPABASE_ANON_KEY") or "").strip()
    return url, key


def _create_supabase_auth_client() -> Client | None:
    url, key = _supabase_auth_url_and_key()
    if not url or not key:
        return None
    return create_client(url, key)


def sign_up(email: str, password: str):
    c = _create_supabase_auth_client()
    if not c:
        raise RuntimeError("SUPABASE_URL と SUPABASE_KEY（または SUPABASE_ANON_KEY）を設定してください。")
    return c.auth.sign_up({"email": email, "password": password})


def sign_in(email: str, password: str):
    c = _create_supabase_auth_client()
    if not c:
        raise RuntimeError("SUPABASE_URL と SUPABASE_KEY（または SUPABASE_ANON_KEY）を設定してください。")
    return c.auth.sign_in_with_password({"email": email, "password": password})


def sign_out() -> None:
    """サーバー側セッションを終了し、認証用の session_state のみ削除（伝票データは残す）。"""
    c = _create_supabase_auth_client()
    if c:
        try:
            at = st.session_state.get("sb_access_token")
            rt = st.session_state.get("sb_refresh_token")
            if at and rt:
                c.auth.set_session(at, rt)
                c.auth.sign_out()
            else:
                c.auth.sign_out()
        except Exception:
            pass
    for k in ("sb_access_token", "sb_refresh_token", "user", "user_email", "sb_user_id"):
        st.session_state.pop(k, None)


def login_signup_page() -> None:
    st.title("ログイン / サインアップ")
    st.caption("認証後、伝票の OCR 読み取りと DB 閲覧が利用できます。")
    url, key = _supabase_auth_url_and_key()
    if not url or not key:
        st.error("`SUPABASE_URL` と `SUPABASE_KEY`（または `SUPABASE_ANON_KEY`）を `.env` に設定してください。")
        return

    tab1, tab2 = st.tabs(["ログイン", "サインアップ"])
    with tab1:
        email = st.text_input("メールアドレス", key="login_email")
        password = st.text_input("パスワード", type="password", key="login_password")
        if st.button("ログイン", key="login_submit"):
            try:
                res = sign_in(email, password)
                if not res.session:
                    st.error("セッションを取得できませんでした。メール確認が済んでいるか確認してください。")
                else:
                    st.session_state.sb_access_token = res.session.access_token
                    st.session_state.sb_refresh_token = res.session.refresh_token
                    if res.user:
                        st.session_state.user = res.user
                        if getattr(res.user, "email", None):
                            st.session_state.user_email = res.user.email
                        if getattr(res.user, "id", None):
                            st.session_state.sb_user_id = str(res.user.id)
                    refresh_sb_user_id_from_token()
                    st.success("ログインに成功しました")
                    st.rerun()
            except Exception as e:
                st.error(f"ログインに失敗しました: {e}")

    with tab2:
        new_email = st.text_input("メールアドレス", key="signup_email")
        new_password = st.text_input("パスワード", type="password", key="signup_password")
        if st.button("サインアップ", key="signup_submit"):
            try:
                res = sign_up(new_email, new_password)
                if res.session:
                    st.session_state.sb_access_token = res.session.access_token
                    st.session_state.sb_refresh_token = res.session.refresh_token
                if res.user:
                    st.session_state.user = res.user
                    if getattr(res.user, "email", None):
                        st.session_state.user_email = res.user.email
                    if getattr(res.user, "id", None):
                        st.session_state.sb_user_id = str(res.user.id)
                refresh_sb_user_id_from_token()
                if res.session:
                    st.success("アカウントが作成され、ログインしました。")
                    st.rerun()
                else:
                    st.success("アカウントが作成されました。メールを確認してアカウントを有効化してください。")
            except Exception as e:
                st.error(f"サインアップに失敗しました: {e}")


def get_supabase_client_for_writes() -> Client | None:
    """ログイン済みなら anon + ユーザーセッションで insert。未ログイン時は従来の get_supabase_client()。"""
    url, anon = _supabase_auth_url_and_key()
    at = st.session_state.get("sb_access_token")
    rt = st.session_state.get("sb_refresh_token")
    if url and anon and at and rt:
        client = create_client(url, anon)
        try:
            sess = client.auth.set_session(at, rt)
            if sess.session:
                st.session_state.sb_access_token = sess.session.access_token
                st.session_state.sb_refresh_token = sess.session.refresh_token
            return client
        except Exception:
            sign_out()
            return None
    return get_supabase_client()


def supabase_auth_configured() -> bool:
    """メール認証に使う URL と公開キー（anon）が揃っているか。"""
    u, k = _supabase_auth_url_and_key()
    return bool(u and k)


def is_supabase_logged_in() -> bool:
    return bool(st.session_state.get("sb_access_token") and st.session_state.get("sb_refresh_token"))


def refresh_sb_user_id_from_token() -> None:
    """ログイン直後や再実行時に、JWT の sub からユーザー UUID を session_state に入れる。"""
    if st.session_state.get("sb_user_id"):
        return
    at = st.session_state.get("sb_access_token")
    if not at or "." not in at:
        return
    try:
        payload_b64 = at.split(".")[1]
        pad = (4 - len(payload_b64) % 4) % 4
        payload_b64 += "=" * pad
        claims = json.loads(base64.urlsafe_b64decode(payload_b64.encode("ascii")))
        sub = claims.get("sub")
        if sub:
            st.session_state.sb_user_id = str(sub)
    except Exception:
        pass


def rows_to_supabase_payloads(
    rows: list[dict],
    product_catalog: list[dict] | None = None,
) -> list[dict]:
    """編集済み行を Supabase 用の辞書リストに変換（id は送らない）。"""
    refresh_sb_user_id_from_token()
    uid_col = (os.getenv("SUPABASE_PURCHASES_USER_ID_COLUMN") or "").strip()
    uid = st.session_state.get("sb_user_id") if is_supabase_logged_in() else None
    catalog = product_catalog or []

    out: list[dict] = []
    for row in rows:
        date_iso = normalize_purchase_date_to_iso(str(row.get("日付", "")).strip())
        note_val = (row.get("備考", "") or "").strip() or None
        product_name_val = (str(row.get("商品名", "") or "").strip() or None)
        product_row_id = resolve_product_id_from_catalog(
            str(product_name_val or ""),
            str(row.get("取引先", "") or ""),
            catalog,
        )
        rec: dict = {
            "invoice_number": row.get("伝票番号", "") or None,
            "purchase_date": date_iso or None,
            "quantity": row.get("数量", "") or None,
            "unit_price": row.get("単価", "") or None,
            "amount": row.get("合計金額", "") or None,
            "ocr_text": row.get("ai_response", "") or None,
        }
        if SUPABASE_PURCHASES_PRODUCT_NAME_COLUMN and product_name_val:
            rec[SUPABASE_PURCHASES_PRODUCT_NAME_COLUMN] = product_name_val
        pid_int = _coerce_int_id(product_row_id)
        if SUPABASE_PURCHASES_PRODUCT_ID_COLUMN and pid_int is not None:
            rec[SUPABASE_PURCHASES_PRODUCT_ID_COLUMN] = pid_int
        if SUPABASE_PURCHASES_NOTE_COLUMN:
            rec[SUPABASE_PURCHASES_NOTE_COLUMN] = note_val
        if uid_col and uid:
            rec[uid_col] = uid
        out.append(rec)
    return out


def insert_purchases_to_supabase(client: Client, payloads: list[dict]) -> list[dict]:
    """purchases テーブルへ一括 insert。戻り値は挿入された行（return=minimal 時は空）。"""
    if not payloads:
        return []
    res = client.table(SUPABASE_TABLE_PURCHASES).insert(payloads).execute()
    data = getattr(res, "data", None)
    if isinstance(data, list):
        return data
    return []


def render_supabase_rls_error_help(table: str | None = None) -> None:
    """RLS により insert が拒否されたときの案内。"""
    tbl = (table or SUPABASE_TABLE_PURCHASES).strip()
    st.markdown(
        f"ログイン済みのリクエストはロール **authenticated** で行われます。"
        f"テーブル `public.{tbl}` に、そのロール向けの **INSERT ポリシー**が無いと保存できません。"
    )
    st.code(
        f"""-- Supabase ダッシュボード → SQL Editor で実行（ポリシー名は環境に合わせて変更可）
alter table public.{tbl} enable row level security;

drop policy if exists "purchases_insert_authenticated" on public.{tbl};
create policy "purchases_insert_authenticated"
  on public.{tbl}
  for insert
  to authenticated
  with check (true);
""",
        language="sql",
    )
    st.markdown(
        "行ごとにユーザーを紐付けたい場合は、テーブルに `user_id uuid` 列を追加し、"
        "`with check (auth.uid() = user_id)` のポリシーにしたうえで、"
        "このアプリの `.env` に `SUPABASE_PURCHASES_USER_ID_COLUMN=user_id` を追加してください。"
    )


def _coerce_int_id(v) -> int | None:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _coerce_sales_amount(v) -> int | None:
    """sales.sales_amount（int8）用に整数へ変換。"""
    amount = parse_money_value(v)
    if amount is None:
        return None
    return int(round(amount))


def _coerce_sales_quantity(v) -> int | float | None:
    """sales.quantity 用。整数値は bigint 互換の int、小数のみ float。"""
    q = _coerce_quantity_value(v)
    if q is None:
        return None
    if q == int(q):
        return int(q)
    return q


def _coerce_quantity_value(v) -> float | None:
    """数量（仕入・売上・集計用）。小数を四捨五入せず float で保持。"""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    q = parse_money_value(v)
    if q is None:
        s = str(v).strip().replace(",", "")
        if not s:
            return None
        try:
            q = float(s)
        except ValueError:
            return None
    return float(q)


def _format_quantity_display(v: float | int | None, *, max_decimals: int = 3) -> str:
    """数量の表示用（整数は小数なし、小数は末尾ゼロを除いて表示）。"""
    if v is None:
        return "—"
    f = float(v)
    if abs(f - round(f)) < 1e-9:
        return f"{int(round(f)):,}"
    s = f"{f:,.{max_decimals}f}".rstrip("0").rstrip(".")
    return s


def normalize_sales_payload_for_supabase(payload: dict) -> dict:
    """Supabase へ送る前に bigint 列へ float（例: 2.0）を渡さない。"""
    out = dict(payload)
    amt_col = SUPABASE_SALES_AMOUNT_COLUMN
    qty_col = SUPABASE_SALES_QUANTITY_COLUMN
    if amt_col in out and out[amt_col] is not None:
        out[amt_col] = int(round(float(out[amt_col])))
    if qty_col in out and out[qty_col] is not None:
        v = float(out[qty_col])
        out[qty_col] = int(v) if v == int(v) else v
    attach_weekday_name_to_sales_payload(out)
    return out


def _normalize_product_code(v) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s if s else None


def _extract_product_row_id_from_row(r: dict) -> int | None:
    """products.id（purchases.product_id の FK 先）。"""
    id_col = (SUPABASE_PRODUCTS_ROW_ID_COLUMN or "id").strip()
    v = r.get(id_col)
    if v is not None:
        coerced = _coerce_int_id(v)
        if coerced is not None:
            return coerced
    for alt in ("id",):
        if alt == id_col:
            continue
        av = r.get(alt)
        if av is not None:
            coerced = _coerce_int_id(av)
            if coerced is not None:
                return coerced
    return None


def _extract_product_code_from_row(r: dict) -> str | None:
    """purchase_products の任意商品コード列（存在する場合のみ）。"""
    code_col = (SUPABASE_PRODUCTS_CODE_COLUMN or "").strip()
    if not code_col:
        return None
    v = r.get(code_col)
    if v is not None and str(v).strip():
        return _normalize_product_code(v)
    return None


def fetch_suppliers_map(client: Client) -> dict[str, str]:
    """suppliers.id → name の辞書。"""
    tbl = (SUPABASE_TABLE_SUPPLIERS or "suppliers").strip()
    name_col = (SUPABASE_SUPPLIERS_NAME_COLUMN or "name").strip()
    try:
        res = client.table(tbl).select(f"id, {name_col}").limit(5000).execute()
    except Exception:
        return {}
    data = getattr(res, "data", None) or []
    out: dict[str, str] = {}
    for r in data:
        if not isinstance(r, dict):
            continue
        sid = r.get("id")
        nm = r.get(name_col) or r.get("name")
        if sid is not None and nm is not None and str(nm).strip():
            out[str(sid)] = str(nm).strip()
    return out


def _supplier_name_from_product_row(r: dict, suppliers_map: dict[str, str]) -> str:
    """products 行から取引先名を解決（supplier_id → suppliers、旧 supplier 列にも対応）。"""
    sup_col = (SUPABASE_PRODUCTS_SUPPLIER_COLUMN or "supplier_id").strip()
    for nested_key in ("suppliers", "supplier"):
        nested = r.get(nested_key)
        if isinstance(nested, dict):
            nm = nested.get(SUPABASE_SUPPLIERS_NAME_COLUMN) or nested.get("name")
            if nm is not None and str(nm).strip():
                return str(nm).strip()
        if isinstance(nested, list) and nested and isinstance(nested[0], dict):
            nm = nested[0].get(SUPABASE_SUPPLIERS_NAME_COLUMN) or nested[0].get("name")
            if nm is not None and str(nm).strip():
                return str(nm).strip()
    sup_raw = r.get(sup_col)
    if sup_raw is not None and str(sup_raw).strip():
        if sup_col == "supplier_id" or sup_col.endswith("_id"):
            return suppliers_map.get(str(sup_raw).strip(), str(sup_raw).strip())
        return str(sup_raw).strip()
    for alt in ("supplier_name", "supplier", "vendor", "取引先", "trading_partner"):
        if alt == sup_col:
            continue
        av = r.get(alt)
        if av is not None and str(av).strip():
            return str(av).strip()
    return ""


def fetch_product_catalog(client: Client) -> tuple[list[dict], str | None]:
    """
    purchase_products + suppliers から仕入商品マスタを取得。
    戻り値: ([{"name", "supplier", "row_id", "product_code"}, ...], エラー時メッセージ)
    """
    name_col = (SUPABASE_PRODUCTS_NAME_COLUMN or "product_name").strip()
    sup_col = (SUPABASE_PRODUCTS_SUPPLIER_COLUMN or "supplier_id").strip()
    tbl = (SUPABASE_TABLE_PRODUCTS or "purchase_products").strip()
    suppliers_map = fetch_suppliers_map(client)
    sup_tbl = (SUPABASE_TABLE_SUPPLIERS or "suppliers").strip()
    sup_name_col = (SUPABASE_SUPPLIERS_NAME_COLUMN or "name").strip()
    code_col = (SUPABASE_PRODUCTS_CODE_COLUMN or "").strip()
    select_parts = ["id", name_col, sup_col, f"{sup_tbl}({sup_name_col})"]
    if code_col:
        select_parts.insert(2, code_col)
    select_cols = ", ".join(select_parts)
    try:
        res = client.table(tbl).select(select_cols).limit(5000).execute()
    except Exception:
        try:
            res = client.table(tbl).select("*").limit(5000).execute()
        except Exception as e:
            return [], str(e)
    data = getattr(res, "data", None) or []
    seen_keys: set[tuple[str, str]] = set()
    out: list[dict] = []
    for r in data:
        if not isinstance(r, dict):
            continue
        name = _extract_product_name_from_row(r, name_col)
        if not name:
            continue
        supplier = _supplier_name_from_product_row(r, suppliers_map)
        dedupe_key = (name, supplier)
        if dedupe_key in seen_keys:
            continue
        seen_keys.add(dedupe_key)
        out.append(
            {
                "name": name,
                "supplier": supplier,
                "row_id": _extract_product_row_id_from_row(r),
                "product_code": _extract_product_code_from_row(r),
            }
        )
    return out, None


def catalog_lookup_by_row_id(catalog: list[dict]) -> dict[str, dict]:
    """products.id をキーにしたマスタ参照。"""
    out: dict[str, dict] = {}
    for x in catalog:
        rid = x.get("row_id")
        if rid is not None:
            out[str(rid)] = x
    return out


def _product_fields_from_nested_row(row: dict) -> tuple[str, str]:
    """PostgREST の purchase_products(suppliers(...)) 埋め込みから商品名・取引先名を取得。"""
    nested = None
    for key in (SUPABASE_TABLE_PRODUCTS, "purchase_products", "products"):
        candidate = row.get(key)
        if isinstance(candidate, dict):
            nested = candidate
            break
    if not isinstance(nested, dict):
        return "", ""
    name_col = (SUPABASE_PRODUCTS_NAME_COLUMN or "product_name").strip()
    pname = str(nested.get(name_col) or nested.get("product_name") or "").strip()
    sname = ""
    for sup_key in (SUPABASE_TABLE_SUPPLIERS, "suppliers", "supplier"):
        sup_nested = nested.get(sup_key)
        if isinstance(sup_nested, dict):
            sname = str(
                sup_nested.get(SUPABASE_SUPPLIERS_NAME_COLUMN) or sup_nested.get("name") or ""
            ).strip()
            break
    return pname, sname


def _purchase_product_name_from_row(row: dict) -> str:
    """purchases に保存された商品名（AI読取・手入力）を優先。"""
    for key in (
        SUPABASE_PURCHASES_PRODUCT_NAME_COLUMN,
        "product_name",
        "商品名",
    ):
        if key and row.get(key) is not None and str(row.get(key)).strip():
            return str(row.get(key)).strip()
    return ""


def enrich_purchase_rows(rows: list[dict], catalog: list[dict] | None = None) -> list[dict]:
    """purchases の product_name を維持しつつ、supplier 等をマスタから補完（表示・集計用）。"""
    by_id = catalog_lookup_by_row_id(catalog or [])
    enriched: list[dict] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        row = dict(r)
        stored_name = _purchase_product_name_from_row(row)
        nested_name, nested_sup = _product_fields_from_nested_row(row)
        pid = row.get(SUPABASE_PURCHASES_PRODUCT_ID_COLUMN) or row.get("product_id")
        prod = by_id.get(str(pid)) if pid is not None else None
        row["product_name"] = stored_name or nested_name or (prod.get("name") if prod else "") or ""
        if prod:
            row["supplier"] = prod.get("supplier") or nested_sup or row.get("supplier") or ""
        else:
            row["supplier"] = nested_sup or row.get("supplier") or ""
        enriched.append(row)
    return enriched


def fetch_sales_product_catalog(client: Client) -> tuple[list[dict], str | None]:
    """sales_products マスタを取得。"""
    tbl = (SUPABASE_TABLE_SALES_PRODUCTS or "sales_products").strip()
    cat_col = (SUPABASE_SALES_PRODUCTS_CATEGORY_COLUMN or "sales_category").strip()
    cat2_col = (SUPABASE_SALES_PRODUCTS_CATEGORY2_COLUMN or "sales_category2").strip()
    name_col = (SUPABASE_SALES_PRODUCTS_MASTER_NAME_COLUMN or "sales_products").strip()
    try:
        res = (
            client.table(tbl)
            .select(f"id, {cat_col}, {cat2_col}, {name_col}")
            .limit(10000)
            .execute()
        )
    except Exception:
        try:
            res = client.table(tbl).select("*").limit(10000).execute()
        except Exception as e:
            return [], str(e)
    out: list[dict] = []
    for r in getattr(res, "data", None) or []:
        if not isinstance(r, dict):
            continue
        row_id = _coerce_int_id(r.get("id"))
        if row_id is None:
            continue
        out.append(
            {
                "row_id": row_id,
                "sales_category": str(r.get(cat_col) or r.get("sales_category") or "").strip(),
                "sales_category2": str(r.get(cat2_col) or r.get("sales_category2") or "").strip(),
                "product_name": str(
                    r.get(name_col) or r.get("sales_products") or ""
                ).strip(),
            }
        )
    return out, None


def sales_product_catalog_lookup(catalog: list[dict]) -> dict[str, dict]:
    return {str(x["row_id"]): x for x in catalog if x.get("row_id") is not None}


def _sales_select_with_master_embed() -> str:
    """sales + sales_products マスタ（FK: product_id）。テーブル名 embed は列名と衝突するため使わない。"""
    pid_col = (SUPABASE_SALES_PRODUCT_ID_COLUMN or "product_id").strip()
    cat_col = (SUPABASE_SALES_PRODUCTS_CATEGORY_COLUMN or "sales_category").strip()
    cat2_col = (SUPABASE_SALES_PRODUCTS_CATEGORY2_COLUMN or "sales_category2").strip()
    name_col = (SUPABASE_SALES_PRODUCTS_MASTER_NAME_COLUMN or "sales_products").strip()
    return f"*, {pid_col}({cat_col}, {cat2_col}, {name_col})"


def _sales_nested_master_dict(row: dict) -> dict | None:
    """PostgREST の product_id(...) 埋め込み、または sales_products テーブル埋め込みからマスタ情報を取得。"""
    cat_col = (SUPABASE_SALES_PRODUCTS_CATEGORY_COLUMN or "sales_category").strip()
    cat2_col = (SUPABASE_SALES_PRODUCTS_CATEGORY2_COLUMN or "sales_category2").strip()
    name_col = (SUPABASE_SALES_PRODUCTS_MASTER_NAME_COLUMN or "sales_products").strip()
    pid_col = (SUPABASE_SALES_PRODUCT_ID_COLUMN or "product_id").strip()

    candidates: list[dict | None] = []
    nested_pid = row.get(pid_col)
    if isinstance(nested_pid, dict):
        candidates.append(nested_pid)
    for key in (SUPABASE_TABLE_SALES_PRODUCTS, "sales_products"):
        nested = row.get(key)
        if isinstance(nested, dict):
            candidates.append(nested)

    for nested in candidates:
        if not nested:
            continue
        out = {
            "sales_category": str(nested.get(cat_col) or nested.get("sales_category") or "").strip(),
            "sales_category2": str(nested.get(cat2_col) or nested.get("sales_category2") or "").strip(),
            "product_name": str(nested.get(name_col) or nested.get("sales_products") or "").strip(),
        }
        if any(out.values()):
            return out
    return None


def _sales_department_label_from_master(prod: dict | None) -> str:
    if not prod:
        return ""
    c1 = str(prod.get("sales_category") or "").strip()
    c2 = str(prod.get("sales_category2") or "").strip()
    if c1 and c2 and c1 != c2:
        return f"{c1} / {c2}"
    return c1 or c2


def _sales_master_from_row(row: dict, lookup: dict[str, dict]) -> dict | None:
    nested = _sales_nested_master_dict(row)
    if nested:
        return nested
    pid_col = (SUPABASE_SALES_PRODUCT_ID_COLUMN or "product_id").strip()
    master_id = _coerce_int_id(row.get(pid_col))
    if master_id is None:
        legacy_fk = (os.getenv("SUPABASE_SALES_CATEGORY_FK_COLUMN") or "").strip()
        if legacy_fk and legacy_fk != pid_col:
            master_id = _coerce_int_id(row.get(legacy_fk))
    if master_id is None:
        return None
    return lookup.get(str(master_id))


def _sales_product_name_from_row(row: dict, lookup: dict[str, dict]) -> str:
    products_col = (SUPABASE_SALES_PRODUCTS_COLUMN or "sales_products").strip()
    pid_col = (SUPABASE_SALES_PRODUCT_ID_COLUMN or "product_id").strip()
    for key in (products_col, "sales_products", "product_name"):
        val = row.get(key)
        if key == pid_col and isinstance(val, dict):
            continue
        if val is not None and str(val).strip():
            return str(val).strip()
    master = _sales_master_from_row(row, lookup)
    if master and master.get("product_name"):
        return str(master["product_name"]).strip()
    return ""


def _sales_department_from_row(row: dict, lookup: dict[str, dict]) -> str:
    legacy_col = (SUPABASE_SALES_KATEGORY_COLUMN or "").strip()
    if legacy_col and row.get(legacy_col) is not None and str(row.get(legacy_col)).strip():
        return str(row.get(legacy_col)).strip()
    master = _sales_master_from_row(row, lookup)
    label = _sales_department_label_from_master(master)
    return label or ""


def enrich_sales_rows(
    rows: list[dict], catalog: list[dict] | None = None
) -> list[dict]:
    """sales 行に product_name / kategory を付与（sales_products マスタ参照）。"""
    lookup = sales_product_catalog_lookup(catalog or [])
    enriched: list[dict] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        row = dict(r)
        row["product_name"] = _sales_product_name_from_row(row, lookup) or "（未設定）"
        master = _sales_master_from_row(row, lookup)
        row["kategory"] = _sales_department_from_row(row, lookup) or "（未設定）"
        row["sales_category"] = str(master.get("sales_category") or "").strip() if master else ""
        row["sales_category2"] = str(master.get("sales_category2") or "").strip() if master else ""
        enriched.append(row)
    return enriched


def distinct_sales_category2_choices(catalog: list[dict]) -> list[str]:
    """売上履歴のカテゴリ2選択肢（sales_products.sales_category2 の一意値）。"""
    seen: set[str] = set()
    out: list[str] = []
    for x in catalog:
        v = str(x.get("sales_category2") or "").strip()
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return sorted(out)


def _sales_master_ids_matching_department(catalog: list[dict], department: str) -> list[int]:
    needle = (department or "").strip().lower()
    if not needle:
        return []
    ids: list[int] = []
    for x in catalog:
        labels = [
            str(x.get("sales_category") or ""),
            _sales_department_label_from_master(x),
        ]
        blob = " ".join(labels).lower()
        if needle in blob:
            rid = _coerce_int_id(x.get("row_id"))
            if rid is not None:
                ids.append(rid)
    return list(dict.fromkeys(ids))


def _sales_master_ids_matching_category2(catalog: list[dict], category2: str) -> list[int]:
    needle = (category2 or "").strip().lower()
    if not needle:
        return []
    ids: list[int] = []
    for x in catalog:
        c2 = str(x.get("sales_category2") or "").lower()
        if needle in c2:
            rid = _coerce_int_id(x.get("row_id"))
            if rid is not None:
                ids.append(rid)
    return list(dict.fromkeys(ids))


def _sales_master_ids_matching_product_name(catalog: list[dict], product_name: str) -> list[int]:
    needle = (product_name or "").strip().lower()
    if not needle:
        return []
    ids: list[int] = []
    for x in catalog:
        pname = str(x.get("product_name") or "").lower()
        if needle in pname:
            rid = _coerce_int_id(x.get("row_id"))
            if rid is not None:
                ids.append(rid)
    return list(dict.fromkeys(ids))


def fetch_sales_master_ids_from_db(
    client: Client,
    *,
    product_name: str = "",
    department: str = "",
    category2: str = "",
) -> list[int]:
    """sales_products を DB 上で部分一致検索し id 一覧を返す。"""
    pn = (product_name or "").strip()
    kat = (department or "").strip()
    cat2 = (category2 or "").strip()
    if not pn and not kat and not cat2:
        return []
    tbl = (SUPABASE_TABLE_SALES_PRODUCTS or "sales_products").strip()
    cat_col = (SUPABASE_SALES_PRODUCTS_CATEGORY_COLUMN or "sales_category").strip()
    cat2_col = (SUPABASE_SALES_PRODUCTS_CATEGORY2_COLUMN or "sales_category2").strip()
    name_col = (SUPABASE_SALES_PRODUCTS_MASTER_NAME_COLUMN or "sales_products").strip()
    try:
        q = client.table(tbl).select("id")
        if pn:
            q = q.ilike(name_col, f"%{pn}%")
        if kat:
            q = q.ilike(cat_col, f"%{kat}%")
        if cat2:
            q = q.ilike(cat2_col, f"%{cat2}%")
        res = q.limit(5000).execute()
    except Exception:
        return []
    ids: list[int] = []
    for row in getattr(res, "data", None) or []:
        if not isinstance(row, dict):
            continue
        rid = _coerce_int_id(row.get("id"))
        if rid is not None:
            ids.append(rid)
    return list(dict.fromkeys(ids))


def _sales_row_matches_product_filter(row: dict, product_name: str) -> bool:
    needle = (product_name or "").strip().lower()
    if not needle:
        return True
    products_col = (SUPABASE_SALES_PRODUCTS_COLUMN or "sales_products").strip()
    parts = [
        str(row.get("product_name") or ""),
        str(row.get(products_col) or ""),
    ]
    master = _sales_nested_master_dict(row) or {}
    parts.append(str(master.get("product_name") or ""))
    return any(needle in p.lower() for p in parts if p)


def _sales_row_matches_department_filter(row: dict, department: str) -> bool:
    needle = (department or "").strip().lower()
    if not needle:
        return True
    parts = [
        str(row.get("sales_category") or ""),
        str(row.get("kategory") or ""),
    ]
    master = _sales_nested_master_dict(row)
    if master:
        parts.append(str(master.get("sales_category") or ""))
        parts.append(_sales_department_label_from_master(master))
    return any(needle in p.lower() for p in parts if p)


def _sales_row_matches_category2_filter(row: dict, category2: str) -> bool:
    needle = (category2 or "").strip().lower()
    if not needle:
        return True
    parts = [str(row.get("sales_category2") or "")]
    master = _sales_nested_master_dict(row)
    if master:
        parts.append(str(master.get("sales_category2") or ""))
    return any(needle in p.lower() for p in parts if p)


def resolve_or_create_sales_product_master(
    client: Client,
    department: str,
    product_name: str,
    *,
    cache: dict[tuple[str, str], int | None] | None = None,
) -> int | None:
    """sales_products を検索し、無ければ作成して id を返す。"""
    dept = str(department or "").strip()
    pname = str(product_name or "").strip()
    if not dept or not pname:
        return None
    key = (dept, pname)
    if cache is not None and key in cache:
        return cache[key]

    tbl = (SUPABASE_TABLE_SALES_PRODUCTS or "sales_products").strip()
    cat_col = (SUPABASE_SALES_PRODUCTS_CATEGORY_COLUMN or "sales_category").strip()
    name_col = (SUPABASE_SALES_PRODUCTS_MASTER_NAME_COLUMN or "sales_products").strip()
    master_id: int | None = None
    try:
        res = (
            client.table(tbl)
            .select("id")
            .eq(cat_col, dept)
            .eq(name_col, pname)
            .limit(1)
            .execute()
        )
        for row in getattr(res, "data", None) or []:
            master_id = _coerce_int_id(row.get("id") if isinstance(row, dict) else None)
            if master_id is not None:
                break
        if master_id is None:
            ins = client.table(tbl).insert({cat_col: dept, name_col: pname}).execute()
            data = getattr(ins, "data", None) or []
            if data and isinstance(data[0], dict):
                master_id = _coerce_int_id(data[0].get("id"))
    except Exception:
        master_id = None

    if cache is not None:
        cache[key] = master_id
    return master_id


def prepare_sales_payloads_for_insert(
    client: Client, payloads: list[dict]
) -> list[dict]:
    """CSV 等の売上 payload を sales_products 参照付き insert 用に変換。"""
    cache: dict[tuple[str, str], int | None] = {}
    out: list[dict] = []
    pid_col = (SUPABASE_SALES_PRODUCT_ID_COLUMN or "product_id").strip()
    products_col = (SUPABASE_SALES_PRODUCTS_COLUMN or "sales_products").strip()
    for payload in payloads:
        row = dict(payload)
        dept = str(row.pop("_department", "") or "").strip()
        row.pop("_preview_department", None)
        pname = str(row.get(products_col) or "").strip()
        master_id = resolve_or_create_sales_product_master(
            client, dept, pname, cache=cache
        )
        if master_id is not None:
            row[pid_col] = master_id
        out.append(normalize_sales_payload_for_supabase(row))
    return out


def fetch_product_row_ids_for_supplier_filter(
    client: Client,
    supplier: str,
    *,
    catalog: list[dict] | None = None,
) -> list[int] | None:
    """
    取引先条件に合う products.id の一覧。
    取引先条件なしのとき None。条件ありで一致0件のとき []。
    """
    sup = (supplier or "").strip()
    if not sup:
        return None

    sup_col = (SUPABASE_PRODUCTS_SUPPLIER_COLUMN or "supplier_id").strip()
    tbl = (SUPABASE_TABLE_PRODUCTS or "purchase_products").strip()
    sup_tbl = (SUPABASE_TABLE_SUPPLIERS or "suppliers").strip()
    sup_name_col = (SUPABASE_SUPPLIERS_NAME_COLUMN or "name").strip()
    sup_l = sup.lower()

    supplier_ids: list[int] = []
    try:
        sres = (
            client.table(sup_tbl)
            .select("id")
            .ilike(sup_name_col, f"%{sup}%")
            .limit(500)
            .execute()
        )
        for srow in getattr(sres, "data", None) or []:
            sid = _coerce_int_id(srow.get("id") if isinstance(srow, dict) else None)
            if sid is not None:
                supplier_ids.append(sid)
    except Exception:
        supplier_ids = []

    if supplier_ids:
        try:
            pres = (
                client.table(tbl)
                .select("id")
                .in_(sup_col, supplier_ids)
                .limit(5000)
                .execute()
            )
            ids = []
            for prow in getattr(pres, "data", None) or []:
                rid = _coerce_int_id(prow.get("id") if isinstance(prow, dict) else None)
                if rid is not None:
                    ids.append(rid)
            if ids:
                return ids
        except Exception:
            pass

    if catalog is None:
        catalog, _ = fetch_product_catalog(client)
    fallback: list[int] = []
    for x in catalog or []:
        xs = (x.get("supplier") or "").lower()
        if sup_l and sup_l not in xs:
            continue
        rid = _coerce_int_id(x.get("row_id"))
        if rid is not None:
            fallback.append(rid)
    return fallback


def resolve_product_id_from_catalog(
    product_name: str,
    取引先: str,
    catalog: list[dict],
) -> int | None:
    """確定した商品名と取引先から products.id（purchases.product_id）を照合。"""
    name = normalize_text(str(product_name or "").strip())
    if not name or name == "検出できませんでした" or not catalog:
        return None
    req_sup = catalog_supplier_match_for_invoice(取引先)
    matched: list[int] = []
    for x in catalog:
        xname = normalize_text(str(x.get("name") or "").strip())
        if xname != name:
            continue
        xs = (x.get("supplier") or "").strip()
        if req_sup is not None and xs != req_sup:
            continue
        rid = _coerce_int_id(x.get("row_id"))
        if rid is not None:
            matched.append(rid)
    if matched:
        return matched[0]
    if req_sup is not None:
        for x in catalog:
            if normalize_text(str(x.get("name") or "").strip()) == name:
                rid = _coerce_int_id(x.get("row_id"))
                if rid is not None:
                    return rid
    return None


def attach_product_ids_to_rows(rows: list[dict], catalog: list[dict]) -> None:
    """マスタに一致するときだけ products.id を product_id に設定（未登録は None）。"""
    for row in rows:
        matched = resolve_product_id_from_catalog(
            str(row.get("商品名", "") or ""),
            str(row.get("取引先", "") or ""),
            catalog,
        )
        row["product_id"] = matched


def product_code_from_catalog(row_id, catalog: list[dict]) -> str | None:
    """products.id に対応する商品コード（products.product_id 列）。"""
    rid = _coerce_int_id(row_id)
    if rid is None:
        return None
    for x in catalog:
        if _coerce_int_id(x.get("row_id")) == rid:
            return x.get("product_code")
    return None


def _extract_product_name_from_row(r: dict, name_col: str) -> str | None:
    v = r.get(name_col)
    if v is not None and str(v).strip():
        return str(v).strip()
    for alt in ("product_name", "title", "label", "goods_name", "品名", "name"):
        if alt == name_col:
            continue
        av = r.get(alt)
        if av is not None and str(av).strip():
            return str(av).strip()
    return None


def catalog_supplier_match_for_invoice(取引先: str) -> str | None:
    """
    伝票の取引先表示から、suppliers.name と照合する値を返す。
    該当ルールがなければ None（マスタ全件を候補に使う）。
    """
    t = normalize_text(取引先 or "")
    if not t:
        return None
    if "関東日本フード" in t or ("関東" in t and "日本" in t and "フード" in t):
        return "関東日本フード（株）"
    if "全農石川" in t or (t.startswith("全農") and "石川" in t):
        return "全農石川"
    if "天狗中田本店" in t or "天狗" in t and "中田" in t:
        return "（株）天狗中田本店"
    return None


def product_names_for_row(catalog: list[dict], 取引先: str) -> list[str]:
    """取引先ルールに一致する products 行の商品名だけを返す（ルールなしは全件の名前）。"""
    req = catalog_supplier_match_for_invoice(取引先)
    if req is None:
        return [x["name"] for x in catalog if x.get("name")]
    out: list[str] = []
    for x in catalog:
        if (x.get("supplier") or "").strip() == req and x.get("name"):
            out.append(x["name"])
    return out


def nearest_product_names(query: str, names: list[str], k: int = 15) -> list[str]:
    """読み取り商品名に近い候補を並べる（部分一致優先のあと類似度）。"""
    q = (query or "").strip()
    if not q:
        return names[:k]
    ql = q.lower()
    scored: list[tuple[float, str]] = []
    for n in names:
        nl = n.lower()
        if ql in nl:
            scored.append((0.0, n))
        elif nl in ql:
            scored.append((0.02, n))
        else:
            ratio = SequenceMatcher(None, ql, nl).ratio()
            scored.append((1.0 - ratio, n))
    scored.sort(key=lambda x: (x[0], x[1]))
    out: list[str] = []
    seen: set[str] = set()
    for _, name in scored:
        if name in seen:
            continue
        seen.add(name)
        out.append(name)
        if len(out) >= k:
            break
    return out


AI_PROMPT_MASTER_PRODUCT_NAME_MAX_LINES = 800


def _deduped_sorted_product_names(names: list[str]) -> list[str]:
    return sorted({(n or "").strip() for n in names if (n or "").strip()}, key=lambda x: (x.lower(), x))


def format_master_product_names_for_ai_prompt(
    names: list[str], max_lines: int = AI_PROMPT_MASTER_PRODUCT_NAME_MAX_LINES
) -> str:
    """AI プロンプト用。件数が多いときは先頭のみ列挙する。"""
    u = _deduped_sorted_product_names(names)
    if not u:
        return ""
    total = len(u)
    if total > max_lines:
        shown = u[:max_lines]
        return (
            "\n".join(shown)
            + f"\n…（全{total}件中 {max_lines}件のみ記載。省略分も候補に含めて最寄りを選ぶこと。）"
        )
    return "\n".join(u)


def snap_product_name_to_master(商品名: str, master: list[str], master_set: set[str]) -> str:
    """画像由来の商品名を Supabase products の登録名に寄せる（帳票1・2共通）。"""
    s = (商品名 or "").strip()
    if not master or not s or s == "検出できませんでした":
        return 商品名 or ""
    if s in master_set:
        return s
    near = nearest_product_names(s, master, k=1)
    return near[0] if near else s


def render_product_name_with_catalog(
    row_idx: int,
    row_key: str,
    current: str,
    product_catalog: list[dict],
    取引先: str,
) -> str:
    """商品名: 取引先に合わせて products を絞り込み、候補 selectbox と手入力。"""
    safe_key = re.sub(r"[^\w\-]", "_", str(row_key))[:80]
    base = f"pcat_{row_idx}_{safe_key}"
    names = product_names_for_row(product_catalog, 取引先)
    req = catalog_supplier_match_for_invoice(取引先)
    if req and not names and product_catalog:
        st.caption(
            f"取引先「{取引先}」に対し suppliers「{req}」一致の商品がありません。"
            "マスタの supplier_id / 取引先名を確認するか、手入力で入力してください。"
        )
    if not names:
        return st.text_input(
            "商品名",
            value=current,
            key=f"{base}_txt_only",
            help="この取引先で候補に使える商品マスタがありません。手入力で入力してください。",
        )
    near = nearest_product_names(current, names, k=15)
    opts = ["（手入力）"] + near
    if len(opts) <= 1:
        return st.text_input(
            "商品名",
            value=current,
            key=f"{base}_txt_only2",
            help="候補が算出できませんでした。手入力で修正してください。",
        )
    pick = st.selectbox(
        "商品名（products の候補・取引先で絞り込み）",
        opts,
        key=f"{base}_pick",
        help="取引先に応じてマスタを照合しています。候補を選ぶとその名前に置き換わります。",
    )
    if pick == "（手入力）":
        return st.text_input(
            "商品名（手入力）",
            value=current,
            key=f"{base}_txt",
        )
    return pick


def _show_product_id_status(
    product_name: str,
    product_row_id: int | None,
    product_code: str | None = None,
) -> None:
    if product_row_id is not None:
        extra = f" / 商品コード: `{product_code}`" if product_code else ""
        st.caption(f"products.id: `{product_row_id}`（マスタより取得{extra}）")
    elif (product_name or "").strip() and product_name != "検出できませんでした":
        st.caption("products.id: 未取得（マスタに該当行がないか ID が空です）")


def parse_year_month_filter(s: str) -> tuple[str, str] | None:
    """年月文字列を purchase_date（YYYY-MM-DD 想定）の範囲 [開始, 終了] に変換。解釈不能なら None。"""
    s = normalize_text(s) if s else ""
    if not s:
        return None
    y: int | None = None
    mo: int | None = None
    m = re.fullmatch(r"(\d{4})-(\d{1,2})", s)
    if m:
        y, mo = int(m.group(1)), int(m.group(2))
    if y is None:
        m = re.fullmatch(r"(\d{4})年(\d{1,2})月", s)
        if m:
            y, mo = int(m.group(1)), int(m.group(2))
    if y is None:
        m = re.fullmatch(r"(\d{4})/(\d{1,2})", s)
        if m:
            y, mo = int(m.group(1)), int(m.group(2))
    if y is None and re.fullmatch(r"\d{6}", s):
        y, mo = int(s[:4]), int(s[4:6])
    if y is None or mo is None or mo < 1 or mo > 12:
        return None
    start = f"{y:04d}-{mo:02d}-01"
    last = calendar.monthrange(y, mo)[1]
    end = f"{y:04d}-{mo:02d}-{last:02d}"
    return start, end


def fetch_purchases_filtered(
    client: Client,
    year_month: str,
    product_name: str,
    supplier: str,
    *,
    limit: int = 500,
    catalog: list[dict] | None = None,
) -> list[dict]:
    """Supabase purchases を条件で取得（商品名は purchases.product_name、取引先は products 経由）。"""
    if catalog is None:
        catalog, _ = fetch_product_catalog(client)

    pn = (product_name or "").strip()
    sup = (supplier or "").strip()
    product_ids = fetch_product_row_ids_for_supplier_filter(
        client, sup, catalog=catalog
    )
    if product_ids is not None and not product_ids:
        return []

    pname_col = (SUPABASE_PURCHASES_PRODUCT_NAME_COLUMN or "product_name").strip()
    name_col = (SUPABASE_PRODUCTS_NAME_COLUMN or "product_name").strip()
    sup_tbl = (SUPABASE_TABLE_SUPPLIERS or "suppliers").strip()
    sup_name_col = (SUPABASE_SUPPLIERS_NAME_COLUMN or "name").strip()
    prod_tbl = (SUPABASE_TABLE_PRODUCTS or "purchase_products").strip()
    select_expr = f"*, {prod_tbl}({name_col}, {sup_tbl}({sup_name_col}))"

    def _build_query(select_cols: str):
        q = client.table(SUPABASE_TABLE_PURCHASES).select(select_cols)
        rng = parse_year_month_filter(year_month)
        if rng:
            start, end = rng
            q = q.gte("purchase_date", start).lte("purchase_date", end)
        if pn:
            q = q.ilike(pname_col, f"%{pn}%")
        if product_ids is not None:
            q = q.in_(SUPABASE_PURCHASES_PRODUCT_ID_COLUMN, product_ids)
        return q.order("purchase_date", desc=True).limit(limit)

    rows: list[dict] = []
    for select_cols in (select_expr, "*"):
        try:
            res = _build_query(select_cols).execute()
            data = getattr(res, "data", None)
            rows = data if isinstance(data, list) else []
            break
        except Exception:
            if select_cols == "*":
                raise
    return enrich_purchase_rows(rows, catalog)


def purchase_note_from_db_row(r: dict) -> str:
    """Supabase purchases 行から Note（備考）列を取得。"""
    for key in (
        SUPABASE_PURCHASES_NOTE_COLUMN,
        "Note",
        "note",
        "備考",
    ):
        if key and r.get(key) is not None and str(r.get(key)).strip():
            return str(r.get(key)).strip()
    return ""


def parse_money_value(v) -> float | None:
    """金額・単価文字列を数値に変換（カンマ・円記号を除去）。"""
    if v is None:
        return None
    s = str(v).strip().replace(",", "").replace("￥", "").replace("¥", "").replace("円", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _strip_commas_text(v: str) -> str:
    """全角/半角カンマを除去した文字列。"""
    return str(v or "").replace(",", "").replace("，", "")


def _format_number_no_comma(v: float) -> str:
    """数値をカンマなし文字列で返す（整数優先）。"""
    if abs(v - round(v)) < 1e-9:
        return str(int(round(v)))
    return f"{v:.2f}".rstrip("0").rstrip(".")


def calc_amount_from_qty_unit(qty: str, unit_price: str) -> str | None:
    """数量・単価がそろっているときに金額(数量×単価)を返す。"""
    qv = parse_money_value(_strip_commas_text(qty))
    uv = parse_money_value(_strip_commas_text(unit_price))
    if qv is None or uv is None:
        return None
    return _format_number_no_comma(qv * uv)


def sanitize_edit_row_and_recalc_amount(row: dict[str, str]) -> dict[str, str]:
    """修正画面の行を正規化し、金額=数量×単価を自動反映。"""
    cleaned = dict(row)
    for key in (
        "伝票番号",
        "日付",
        "取引先",
        "明細番号",
        "商品名",
        "数量",
        "単価",
        "合計金額",
        "備考",
    ):
        cleaned[key] = _strip_commas_text(str(cleaned.get(key, ""))).strip()

    qv = parse_money_value(cleaned.get("数量", ""))
    uv = parse_money_value(cleaned.get("単価", ""))
    if qv is not None and uv is not None:
        cleaned["合計金額"] = _format_number_no_comma(qv * uv)
    return cleaned


def normalize_form1_amount_display(amount_str: str) -> str:
    """帳票1の金額表示をカンマなし整数にそろえる。"""
    s = normalize_text(str(amount_str or "").strip())
    if not s or s == "検出できませんでした":
        return s
    v = parse_money_value(s)
    if v is None:
        return s.replace(",", "")
    return str(int(round(v)))


def _format_form2_quantity_display(v: float) -> str:
    """帳票2の数量（小数第1位まで）。"""
    r = round(v * 10) / 10.0
    return f"{r:.1f}".rstrip("0").rstrip(".") if abs(r - round(r)) >= 1e-9 else f"{int(round(r))}"


def _format_form2_amount_display(v: float) -> str:
    """帳票2の金額（円・整数・カンマなし）。"""
    return str(int(round(v)))


def _format_form2_unit_price_display(v: float) -> str:
    """帳票2の原単価（小数第2位まで・カンマなし。例 1350.00）。"""
    return f"{v:.2f}"


def _form2_unit_price_from_raw_digits(digits: str) -> float | None:
    """原単価の生数字（24000 / 56400）→ 末尾2桁が小数（240.00）。"""
    if len(digits) < 4:
        return None
    return int(digits[:-2]) + int(digits[-2:]) / 100.0


def normalize_form2_unit_price_display(price_str: str) -> str:
    """原単価: 末尾2桁が小数（24000→240.00, 56400→564.00）。"""
    s = normalize_text(str(price_str or "").strip())
    if not s or s == "検出できませんでした":
        return s
    s = s.replace("，", ".").replace("、", ".").replace("．", ".")

    if "." in s:
        left, _, right = s.partition(".")
        left_d = re.sub(r"[^\d]", "", left)
        right_d = re.sub(r"[^\d]", "", right)
        # 24000.00 のように整数部が5桁以上 → 240.00（.00 は印字の小数部ではない）
        if len(left_d) >= 5:
            v = _form2_unit_price_from_raw_digits(left_d)
            if v is not None:
                return _format_form2_unit_price_display(v)
        if re.search(r"\.\d", s):
            v = parse_money_value(s)
            if v is not None:
                return _format_form2_unit_price_display(v)
        return s

    digits = re.sub(r"[^\d]", "", s)
    if len(digits) >= 5:
        v = _form2_unit_price_from_raw_digits(digits)
        if v is not None:
            return _format_form2_unit_price_display(v)
    if len(digits) == 4:
        v = _form2_unit_price_from_raw_digits(digits)
        if v is not None:
            return _format_form2_unit_price_display(v)
    v = parse_money_value(s)
    return _format_form2_unit_price_display(v) if v is not None else s


def normalize_form2_amount_display(amount_str: str) -> str:
    """原価金額: 円の整数（13,365→13365）。末尾1桁小数ルールは適用しない。"""
    s = normalize_text(str(amount_str or "").strip())
    if not s or s == "検出できませんでした":
        return s
    v = parse_money_value(s)
    if v is None:
        return s
    if abs(v - round(v)) < 0.001:
        return _format_form2_amount_display(v)
    return s


def maybe_fix_form2_amount_misdecimal(
    amount_str: str, qty_str: str, unit_price_str: str
) -> str:
    """金額を 13365→1336.5 のように誤って小数化した読取を直す。"""
    a = parse_money_value(amount_str)
    if a is None or abs(a - round(a)) < 0.001:
        return amount_str
    p = parse_money_value(unit_price_str)
    q = parse_money_value(qty_str)
    for mult in (10, 100):
        candidate = int(round(a * mult))
        if candidate < 100:
            continue
        if abs(a * mult - candidate) > 0.02:
            continue
        if p and p > 0:
            implied_q = round((candidate / p) * 10) / 10.0
            if not (0.1 <= implied_q <= 500):
                continue
            cur_err = abs(a - p * (q or 0)) if q else float("inf")
            new_err = abs(candidate - p * implied_q)
            if new_err + max(5, candidate * 0.01) < cur_err:
                return _format_form2_amount_display(float(candidate))
        elif candidate >= 1000:
            return _format_form2_amount_display(float(candidate))
    return amount_str


def normalize_form2_quantity_display(qty_str: str) -> str:
    """帳票2の数量列。2〜3桁は末尾1桁小数。9.9など印字小数はそのまま。"""
    s = normalize_text(str(qty_str or "").strip())
    if not s or s == "検出できませんでした":
        return s
    s = s.replace("，", ".").replace("、", ".").replace("．", ".")
    if re.search(r"\.\d", s):
        v = parse_money_value(s)
        return _format_form2_quantity_display(v) if v is not None else s
    digits = re.sub(r"[^\d]", "", s)
    if re.fullmatch(r"\d{2,3}", digits):
        v = int(digits[:-1]) + int(digits[-1]) / 10.0
        return _format_form2_quantity_display(v)
    if re.fullmatch(r"\d", digits):
        return digits
    v = parse_money_value(s)
    return _format_form2_quantity_display(v) if v is not None else s


def normalize_form2_invoice_number(raw: str) -> str:
    """帳票2の伝票番号を6桁にそろえる。3桁や日付っぽい誤読は空にする。"""
    s = normalize_text(str(raw or "").strip())
    if not s or s == "検出できませんでした":
        return ""
    digits = re.sub(r"[^\d]", "", s)
    if len(digits) == 6:
        return digits
    if len(digits) > 6:
        m = re.search(r"\d{6}", digits)
        return m.group() if m else ""
    return ""


def maybe_fix_form2_column_swap(row: dict[str, str]) -> dict[str, str]:
    """数量と原単価の列取り違え（数量に1350、単価に9.9 など）を補正。"""
    r = dict(row)
    qty_raw = str(r.get("数量") or "")
    unit_raw = str(r.get("単価") or "")
    amt_raw = str(r.get("金額") or "")
    qv = parse_money_value(qty_raw)
    pv = parse_money_value(unit_raw)
    av = parse_money_value(amt_raw)
    if qv is None or pv is None:
        return r

    qty_digits = re.sub(r"[^\d]", "", qty_raw)
    qty_looks_like_price = qv >= 80 or len(qty_digits) >= 4
    unit_looks_like_qty = 0 < pv <= 80
    if qty_looks_like_price and unit_looks_like_qty:
        r["数量"] = normalize_form2_quantity_display(unit_raw)
        r["単価"] = normalize_form2_unit_price_display(qty_raw)
        return r

    if qv >= 80 and pv > 0 and av:
        implied_q = round((av / pv) * 10) / 10.0
        if 0.1 <= implied_q <= 80:
            swapped_unit = normalize_form2_unit_price_display(qty_raw)
            sp = parse_money_value(swapped_unit)
            if sp and abs(av - implied_q * sp) < abs(av - qv * pv) * 0.5:
                r["数量"] = normalize_form2_quantity_display(str(implied_q))
                r["単価"] = swapped_unit
    return r


def _form2_invoice_number_candidates(rows: list[dict[str, str]]) -> list[str]:
    found: list[str] = []
    for row in rows:
        digits = re.sub(r"[^\d]", "", str(row.get("伝票番号") or ""))
        if len(digits) == 6:
            found.append(digits)
        elif len(digits) > 6:
            m = re.search(r"\d{6}", digits)
            if m:
                found.append(m.group())
    return found


def apply_form2_invoice_number_to_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    """明細行に伝票番号6桁を統一して付与する。"""
    candidates = _form2_invoice_number_candidates(rows)
    if not candidates:
        return rows
    best = max(candidates, key=lambda x: (candidates.count(x), x))
    out: list[dict[str, str]] = []
    for row in rows:
        r = dict(row)
        r["伝票番号"] = best
        out.append(r)
    return out


def score_form2_rows(rows: list[dict[str, str]]) -> float:
    """帳票2の読取品質スコア（高いほど正しい向き・列の可能性が高い）。"""
    if not rows:
        return -100.0
    score = 0.0
    inv_candidates = _form2_invoice_number_candidates(rows)
    if inv_candidates:
        score += 35
    else:
        short = [
            re.sub(r"[^\d]", "", str(r.get("伝票番号") or ""))
            for r in rows
            if re.sub(r"[^\d]", "", str(r.get("伝票番号") or ""))
        ]
        if any(len(d) == 3 for d in short):
            score -= 25
    for row in rows:
        q = parse_money_value(str(row.get("数量") or ""))
        p = parse_money_value(str(row.get("単価") or ""))
        a = parse_money_value(str(row.get("金額") or ""))
        if q is not None and q > 80:
            score -= 12
        if p is not None and 0 < p < 80 and "." in str(row.get("単価") or ""):
            score -= 8
        if q and p and a and p > 0:
            err = abs(a - q * p) / max(a, 1)
            if err < 0.06:
                score += 18
            elif err < 0.2:
                score += 6
            else:
                score -= 4
    return score


def form2_orientation_degrees(img: Image.Image) -> list[int]:
    """帳票2向けに試す回転角度（0=そのまま）。"""
    w, h = img.size
    if w > h * 1.08:
        return [90, 0, 270]
    if h > w * 1.08:
        return [0, 90, 270]
    return [0, 90, 270]


def rotate_invoice_image(img: Image.Image, degrees: int) -> Image.Image:
    """伝票画像を回転（expand=True）。"""
    if degrees % 360 == 0:
        return img
    return img.rotate(degrees, expand=True)


def maybe_fix_form2_quantity_from_amount(
    qty_str: str, unit_price_str: str, amount_str: str
) -> str:
    """数量が1など明らかに誤っているとき、金額÷原単価から補正（例 9.9）。"""
    q = parse_money_value(qty_str)
    p = parse_money_value(unit_price_str)
    a = parse_money_value(amount_str)
    if not (p and a and p > 0):
        return qty_str
    implied = round((a / p) * 10) / 10.0
    if not (0.1 <= implied <= 500):
        return qty_str
    if q is not None and abs(implied - q) < 0.25:
        return _format_form2_quantity_display(q)
    cur_err = abs(a - p * (q or 0))
    new_err = abs(a - p * implied)
    if new_err + max(5, a * 0.01) < cur_err:
        return _format_form2_quantity_display(implied)
    return qty_str


def apply_form2_postprocess(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for row in rows:
        r = dict(row)
        r["伝票番号"] = normalize_form2_invoice_number(str(r.get("伝票番号") or ""))
        r = maybe_fix_form2_column_swap(r)
        unit = normalize_form2_unit_price_display(str(r.get("単価") or ""))
        r["単価"] = unit
        amt = normalize_form2_amount_display(str(r.get("金額") or ""))
        amt = maybe_fix_form2_amount_misdecimal(
            amt, str(r.get("数量") or ""), unit
        )
        r["金額"] = amt
        qty = normalize_form2_quantity_display(str(r.get("数量") or ""))
        r["数量"] = maybe_fix_form2_quantity_from_amount(qty, unit, amt)
        out.append(r)
    return apply_form2_invoice_number_to_rows(out)


def _form3_line_numbers(rows: list[dict]) -> list[int]:
    nums: list[int] = []
    for r in rows:
        raw = str(r.get("明細番号") or "").strip()
        m = re.search(r"\d+", raw)
        if m:
            nums.append(int(m.group()))
    return nums


def _round_form3_weight_kg(v: float) -> float:
    """帳票3の重量は印字どおり小数第1位まで（第2位以下は四捨五入）。"""
    return round(v * 10) / 10.0


def _format_form3_weight_display(v: float) -> str:
    """帳票3の重量表示（25.0 / 13.0 / 3.2 のように小数第1位を常に付ける）。"""
    return f"{_round_form3_weight_kg(v):.1f}"


def _canonicalize_form3_quantity_string(qty_str: str) -> str:
    """伝票印字の 25. / 13.（小数のみ）を 25.0 / 13.0 に直す。"""
    s = normalize_text(str(qty_str or "").strip())
    if not s or s == "検出できませんでした":
        return s
    s = s.replace("，", ".").replace("、", ".").replace("．", ".")
    m = re.fullmatch(r"(\d+)\.$", s)
    if m:
        return f"{int(m.group(1))}.0"
    m = re.fullmatch(r"(\d+)\.0+$", s)
    if m:
        return f"{int(m.group(1))}.0"
    return s


def normalize_form3_quantity_display(qty_str: str) -> str:
    """帳票3の数量。3桁整数のみ末尾小数。25.→25.0 など。"""
    s = _canonicalize_form3_quantity_string(qty_str)
    if not s or s == "検出できませんでした":
        return s
    if re.search(r"\.\d", s):
        v = parse_money_value(s)
        if v is not None:
            return _format_form3_weight_display(v)
        return s
    digits = re.sub(r"[^\d]", "", s)
    if re.fullmatch(r"\d{3}", digits):
        v = int(digits[:-1]) + int(digits[-1]) / 10.0
        return _format_form3_weight_display(v)
    if re.fullmatch(r"\d{1,2}", digits):
        return _format_form3_weight_display(float(digits))
    if re.fullmatch(r"\d{4,}", digits):
        # 4桁以上は重量欄の3桁ルール対象外（金額・単価の誤読の可能性が高い）
        return qty_str
    v = parse_money_value(s)
    if v is not None:
        return _format_form3_weight_display(v)
    return s


def _form3_qty_looks_like_piece_count(q: float) -> bool:
    """帳票3で個数列（1,2,3…）を誤って数量にした典型パターン。"""
    if q <= 0:
        return False
    return abs(q - round(q)) < 1e-6 and 1 <= q <= 20


def _form3_pick_weight_raw(row: dict) -> str:
    """帳票3: 重量欄を優先。数量が個数と同じなら重量欄を信頼する。"""
    for key in ("重量", "weight", "重量kg"):
        v = str(row.get(key) or "").strip()
        if v and v != "検出できませんでした":
            return v
    qty = str(row.get("数量") or "").strip()
    piece = str(row.get("個数") or "").strip()
    if not qty or qty == "検出できませんでした":
        return ""
    qv = parse_money_value(qty)
    pv = parse_money_value(piece)
    if pv is not None and qv is not None and abs(qv - pv) < 0.01:
        return ""
    return qty


def maybe_fix_form3_quantity_from_piece_count(
    qty_str: str, unit_price_str: str, amount_str: str, *, piece_count_str: str = ""
) -> str:
    """個数欄の整数を読んだとき、金額÷単価から重量(kg)を推定して置き換える。"""
    q = parse_money_value(qty_str)
    p = parse_money_value(unit_price_str)
    a = parse_money_value(amount_str)
    piece = parse_money_value(piece_count_str) if piece_count_str else None
    if q is None:
        return qty_str
    looks_piece = _form3_qty_looks_like_piece_count(q) or (
        piece is not None and abs(q - piece) < 0.01 and _form3_qty_looks_like_piece_count(piece)
    )
    if not looks_piece:
        return qty_str
    if not (p and a and p > 0):
        return qty_str

    implied = _round_form3_weight_kg(a / p)
    if not (0.3 <= implied <= 99):
        return qty_str
    if abs(implied - q) < 0.2:
        return qty_str

    piece_n = piece if piece is not None else q
    piece_total = p * piece_n
    weight_total = p * implied
    tol = max(50, a * 0.02)
    if abs(a - weight_total) + tol < abs(a - piece_total):
        return _format_form3_weight_display(implied) or qty_str
    return qty_str


def maybe_fix_form3_quantity_from_amount(
    qty_str: str, unit_price_str: str, amount_str: str
) -> str:
    """十の位落ち（18.6→1.8）のみ、金額÷単価で推定。通常は重量欄の読取を優先。"""
    q = parse_money_value(qty_str)
    p = parse_money_value(unit_price_str)
    a = parse_money_value(amount_str)
    if q is None or q <= 0:
        return qty_str

    def _as_weight(v: float) -> str:
        return _format_form3_weight_display(v) or qty_str

    if p and a and p > 0:
        implied = _round_form3_weight_kg(a / p)
        if 0.1 <= implied <= 99:
            # 十の位落ちのみ: 読取が小さく、推定がおおよそ10倍（例 1.8 → 18.6）
            if q < 5 and 7 <= implied / q <= 13:
                if abs(a - p * implied) <= abs(a - p * q) * 0.02 + 1:
                    return _as_weight(implied)
            if q < 10 and re.fullmatch(r"\d\.\d+", str(qty_str).strip().replace(",", ".")):
                q10 = _round_form3_weight_kg(q * 10)
                if 0.1 <= q10 <= 99 and abs(a - p * q10) < abs(a - p * q):
                    return _as_weight(q10)

    # 妥当な重量は小数第1位に丸める。金額÷単価で上書きはしない
    if 0.1 <= q <= 99:
        return _as_weight(q)
    return qty_str


def apply_form3_postprocess(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for row in rows:
        r = dict(row)
        raw_weight = _form3_pick_weight_raw(r)
        qty = normalize_form3_quantity_display(raw_weight or str(r.get("数量") or ""))
        qty = maybe_fix_form3_quantity_from_piece_count(
            qty,
            str(r.get("単価") or ""),
            str(r.get("金額") or ""),
            piece_count_str=str(r.get("個数") or ""),
        )
        r["数量"] = maybe_fix_form3_quantity_from_amount(
            qty, str(r.get("単価") or ""), str(r.get("金額") or "")
        )
        out.append(r)
    return out


def form3_should_retry_completion(rows: list[dict]) -> tuple[bool, str]:
    if not rows:
        return False, ""
    nums = _form3_line_numbers(rows)
    if not nums:
        return len(rows) <= 8, "明細番号が取れないため全行走査"
    max_no = max(nums)
    missing = [i for i in range(1, max_no + 1) if i not in nums]
    if missing:
        return True, f"明細番号の欠番: {missing}"
    if len(rows) < max_no:
        return True, f"明細{max_no}番まであるが行数は{len(rows)}"
    if 6 <= len(rows) <= 8:
        return True, f"明細{len(rows)}件のみ（表の全行を再確認）"
    return False, ""


def merge_form3_detail_rows(
    primary: list[dict[str, str]], supplemental: list[dict[str, str]]
) -> list[dict[str, str]]:
    """再読み取り分をマージ（明細番号キー、情報が多い方を優先）。"""
    merged: dict[str, dict[str, str]] = {}

    def score(r: dict[str, str]) -> int:
        s = 0
        if (r.get("商品名") or "") not in ("", "検出できませんでした"):
            s += 4
        if (r.get("数量") or "") not in ("", "検出できませんでした"):
            s += 2
        if (r.get("金額") or "") not in ("", "検出できませんでした"):
            s += 1
        return s

    for r in primary + supplemental:
        key = str(r.get("明細番号") or "").strip() or f"__{len(merged)}"
        if key not in merged or score(r) > score(merged[key]):
            merged[key] = r

    def sort_key(item: tuple[str, dict[str, str]]) -> tuple[int, str]:
        k, _ = item
        m = re.search(r"\d+", k)
        return (int(m.group()) if m else 999, k)

    return [v for _, v in sorted(merged.items(), key=sort_key)]


def build_form3_retry_prompt(existing_count: int, reason: str) -> str:
    return (
        f"帳票3の伝票画像を再確認してください。前回は明細が{existing_count}件でした（{reason}）。\n"
        "表の品目行を上から下まで1行も漏らさず数え、見えている全行を「明細」配列に入れてください。\n"
        "明細に個数・重量・数量(=重量)を分けて返す。例: 個数1・重量3.2・単価3000→数量3.2・金額9600。"
        "個数を数量に入れない。\n"
        'JSONのみ。ルートに "伝票種別": 3, 伝票番号, 伝票日付, 取引先, 明細(全行分の配列)。'
    )


def fetch_table_rows_paginated(
    client: Client,
    table: str,
    *,
    date_col: str,
    start: str,
    end: str,
    page_size: int = POSTGREST_PAGE_SIZE,
    max_rows: int = 200_000,
) -> list[dict]:
    """日付範囲の行を PostgREST の1回上限（既定1000件）を超えてページング取得する。"""
    all_rows: list[dict] = []
    offset = 0
    while offset < max_rows:
        end_idx = offset + page_size - 1
        res = (
            client.table(table)
            .select("*")
            .gte(date_col, start)
            .lte(date_col, end)
            .order(date_col, desc=False)
            .range(offset, end_idx)
            .execute()
        )
        batch = getattr(res, "data", None)
        if not isinstance(batch, list) or not batch:
            break
        all_rows.extend(batch)
        if len(batch) < page_size:
            break
        offset += page_size
    return all_rows


def count_table_rows_in_date_range(
    client: Client,
    table: str,
    *,
    date_col: str,
    start: str,
    end: str,
) -> int | None:
    """日付範囲の行数（PostgREST count=exact）。失敗時は None。"""
    try:
        res = (
            client.table(table)
            .select("*", count="exact")
            .gte(date_col, start)
            .lte(date_col, end)
            .limit(1)
            .execute()
        )
        c = getattr(res, "count", None)
        return int(c) if c is not None else None
    except Exception:
        return None


def _dashboard_date_range(*, months_back: int = 36) -> tuple[str, str]:
    today = datetime.now()
    start_ym = _year_month_shift(today, -(months_back - 1))
    return f"{start_ym}-01", today.date().isoformat()


def fetch_purchases_for_dashboard(client: Client, *, months_back: int = 36) -> list[dict]:
    """ダッシュボード用に直近 N ヶ月分の purchases を取得（年別比較用に既定36ヶ月）。"""
    start, end = _dashboard_date_range(months_back=months_back)
    rows = fetch_table_rows_paginated(
        client,
        SUPABASE_TABLE_PURCHASES,
        date_col="purchase_date",
        start=start,
        end=end,
    )
    catalog, _ = fetch_product_catalog(client)
    return enrich_purchase_rows(rows, catalog)


def purchases_rows_to_analytics(rows: list[dict]) -> list[dict]:
    """集計用に日付・金額・単価・数量・部門などを正規化したレコードへ変換。"""
    kat_col = SUPABASE_PURCHASES_KATEGORY_COLUMN
    out: list[dict] = []
    for r in rows:
        d = normalize_purchase_date_to_iso(str(r.get("purchase_date") or "").strip())
        if not d or len(d) < 7:
            continue
        raw_qty = r.get("quantity")
        quantity = _coerce_quantity_value(raw_qty)
        out.append(
            {
                "date": d,
                "year_month": d[:7],
                "amount": parse_money_value(r.get("amount")) or 0.0,
                "unit_price": parse_money_value(r.get("unit_price")),
                "quantity": quantity,
                "supplier": (str(r.get("supplier") or "").strip() or "（未設定）"),
                "product_name": (str(r.get("product_name") or "").strip() or "（未設定）"),
                "kategory": (str(r.get(kat_col) or r.get("kategory") or "").strip() or "（未設定）"),
            }
        )
    return out


def count_purchases_for_dashboard(client: Client, *, months_back: int = 36) -> int | None:
    start, end = _dashboard_date_range(months_back=months_back)
    return count_table_rows_in_date_range(
        client,
        SUPABASE_TABLE_PURCHASES,
        date_col="purchase_date",
        start=start,
        end=end,
    )


def _year_month_shift(base: datetime, delta_months: int) -> str:
    y, m = base.year, base.month + delta_months
    while m > 12:
        m -= 12
        y += 1
    while m < 1:
        m += 12
        y -= 1
    return f"{y:04d}-{m:02d}"


def _date_to_iso(d: date) -> str:
    return d.strftime("%Y-%m-%d")


def _parse_date_input_range(value: date | tuple) -> tuple[date, date]:
    """st.date_input の戻り値（単日または期間タプル）を開始・終了に正規化。"""
    if isinstance(value, tuple) and len(value) == 2:
        start, end = value[0], value[1]
        if start > end:
            start, end = end, start
        return start, end
    if isinstance(value, date):
        return value, value
    raise TypeError("日付の指定が不正です。")


def _monthly_amounts_in_year(records: list[dict], year: int) -> pd.DataFrame:
    """指定年の1〜12月ごとの仕入額。"""
    y = f"{year:04d}"
    labels, amounts = [], []
    for m in range(1, 13):
        ym = f"{y}-{m:02d}"
        labels.append(f"{m}月")
        amounts.append(sum(r["amount"] for r in records if r["year_month"] == ym))
    return pd.DataFrame({"月": labels, "仕入額": amounts})


def dashboard_filter_records(
    records: list[dict],
    mode: str,
    *,
    month_pick: date | None = None,
    year_pick: int | None = None,
    range_start: date | None = None,
    range_end: date | None = None,
) -> tuple[list[dict], list[dict], str, str]:
    """
    期間でレコードを絞り込み、比較用の前期間も返す。
    戻り値: (対象期間, 比較期間, 対象ラベル, 比較ラベル)
    """
    if mode == "月別":
        if not month_pick:
            month_pick = datetime.now().date().replace(day=1)
        sel_ym = month_pick.strftime("%Y-%m")
        period = [r for r in records if r["year_month"] == sel_ym]
        prev_ym = _year_month_shift(datetime(month_pick.year, month_pick.month, 1), -1)
        comparison = [r for r in records if r["year_month"] == prev_ym]
        period_label = f"{month_pick.year}年{month_pick.month}月"
        comparison_label = "前月"
        return period, comparison, period_label, comparison_label

    if mode == "年別":
        y = year_pick or datetime.now().year
        y_str = f"{y:04d}"
        period = [r for r in records if r["date"].startswith(y_str)]
        prev_str = f"{y - 1:04d}"
        comparison = [r for r in records if r["date"].startswith(prev_str)]
        period_label = f"{y}年"
        comparison_label = "前年"
        return period, comparison, period_label, comparison_label

    if not range_start or not range_end:
        today = datetime.now().date()
        range_start = today.replace(day=1)
        range_end = today
    start_iso, end_iso = _date_to_iso(range_start), _date_to_iso(range_end)
    period = [r for r in records if start_iso <= r["date"] <= end_iso]
    span = (range_end - range_start).days + 1
    comp_end = range_start - timedelta(days=1)
    comp_start = comp_end - timedelta(days=span - 1)
    comp_start_iso, comp_end_iso = _date_to_iso(comp_start), _date_to_iso(comp_end)
    comparison = [r for r in records if comp_start_iso <= r["date"] <= comp_end_iso]
    if range_start == range_end:
        period_label = start_iso
    else:
        period_label = f"{start_iso} 〜 {end_iso}"
    comparison_label = f"前期間（{comp_start_iso} 〜 {comp_end_iso}）"
    return period, comparison, period_label, comparison_label


def render_dashboard_page() -> None:
    """仕入 KPI・推移・ランキングのダッシュボード。"""
    st.title("📈 仕入ダッシュボード")
    st.caption(
        f"Supabase の `{SUPABASE_TABLE_PURCHASES}` を集計します。"
        "カレンダーで年別・月別・日別の期間を指定できます（データは直近36ヶ月分を保持）。"
    )

    client = get_supabase_client_for_writes()
    if not client:
        st.warning(
            "Supabase に接続できません。ログインするか、`.env` の `SUPABASE_URL` とキーを確認してください。"
        )
        return

    c_reload, _ = st.columns([1, 4])
    with c_reload:
        if st.button("データを再読込", key="dashboard_reload"):
            st.session_state.pop("dashboard_purchases", None)
            st.session_state.pop("dashboard_fetch_error", None)
            st.rerun()

    if "dashboard_purchases" not in st.session_state:
        with st.spinner("仕入データを取得しています…"):
            try:
                st.session_state.dashboard_purchases = fetch_purchases_for_dashboard(client)
                st.session_state.dashboard_fetch_error = None
            except Exception as e:
                st.session_state.dashboard_purchases = []
                st.session_state.dashboard_fetch_error = str(e)

    err = st.session_state.get("dashboard_fetch_error")
    if err:
        st.error(f"データ取得に失敗しました: {err}")
        if "row-level security" in err.lower() or "42501" in err:
            render_supabase_rls_error_help()
        return

    records = purchases_rows_to_analytics(st.session_state.get("dashboard_purchases") or [])
    if not records:
        st.info("集計できる仕入データがありません。「伝票読み取り」から保存するか、期間を広げてください。")
        return

    today = datetime.now().date()
    month_default = today.replace(day=1)
    range_default = (month_default, today)

    st.subheader("期間の指定")
    period_mode = st.radio(
        "集計単位",
        ["年別", "月別", "日別"],
        horizontal=True,
        key="dashboard_period_mode",
    )

    month_pick: date | None = None
    year_pick: int | None = None
    range_start: date | None = None
    range_end: date | None = None

    if period_mode == "年別":
        st.caption("カレンダーで対象年を選びます（1月1日を選ぶとその年1月〜12月を集計します）。")
        picked_year = st.date_input(
            "対象年",
            value=date(today.year, 1, 1),
            min_value=date(2020, 1, 1),
            max_value=today,
            key="dashboard_year_pick",
        )
        year_pick = picked_year.year if isinstance(picked_year, date) else today.year
    elif period_mode == "月別":
        st.caption("カレンダーで対象月を選びます（日付はその月の1日に自動で揃えます）。")
        picked = st.date_input(
            "対象月",
            value=month_default,
            min_value=date(2020, 1, 1),
            max_value=today,
            key="dashboard_month_pick",
        )
        month_pick = picked.replace(day=1) if isinstance(picked, date) else month_default
    else:
        st.caption("カレンダーで開始日・終了日を選びます（2回クリックで期間を指定）。")
        picked_range = st.date_input(
            "対象期間",
            value=range_default,
            min_value=date(2020, 1, 1),
            max_value=today,
            key="dashboard_day_range",
        )
        try:
            range_start, range_end = _parse_date_input_range(picked_range)
        except TypeError:
            range_start, range_end = range_default

    period_rows, comparison_rows, period_label, comparison_label = dashboard_filter_records(
        records,
        period_mode,
        month_pick=month_pick,
        year_pick=year_pick,
        range_start=range_start,
        range_end=range_end,
    )

    period_total = sum(r["amount"] for r in period_rows)
    prev_total = sum(r["amount"] for r in comparison_rows)
    period_count = len(period_rows)
    period_suppliers = len({r["supplier"] for r in period_rows})

    compare_pct: float | None = None
    if prev_total > 0:
        compare_pct = (period_total - prev_total) / prev_total * 100.0
        compare_delta = f"{comparison_label} ¥{prev_total:,.0f}"
    elif period_total > 0:
        compare_delta = f"{comparison_label}のデータなし"
    else:
        compare_delta = None

    if period_mode == "年別":
        amount_label = "年間仕入額"
        compare_metric_label = "前年比"
    elif period_mode == "月別":
        amount_label = "仕入額"
        compare_metric_label = "前月比"
    else:
        amount_label = "期間仕入額"
        compare_metric_label = "前期間比"

    st.subheader(f"KPI（{period_label}）")
    k1, k2, k3, k4 = st.columns(4)
    with k1:
        st.metric(amount_label, f"¥{period_total:,.0f}")
    with k2:
        st.metric(
            compare_metric_label,
            f"{compare_pct:+.1f}%" if compare_pct is not None else "—",
            delta=compare_delta,
        )
    with k3:
        st.metric("仕入件数", f"{period_count:,} 件")
    with k4:
        st.metric("仕入先数", f"{period_suppliers:,} 社")

    st.divider()
    st.subheader("月別・仕入先")
    mid_l, mid_r = st.columns(2)

    month_keys = sorted({r["year_month"] for r in records})
    if period_mode == "年別" and year_pick:
        monthly_df = _monthly_amounts_in_year(records, year_pick)
        trend_title = f"月別推移（{year_pick}年）"
    else:
        chart_month_keys = month_keys[-12:] if len(month_keys) > 12 else month_keys
        monthly_amounts = [
            sum(r["amount"] for r in records if r["year_month"] == ym) for ym in chart_month_keys
        ]
        monthly_df = pd.DataFrame({"年月": chart_month_keys, "仕入額": monthly_amounts})
        trend_title = "月別推移（直近12ヶ月）"

    supplier_totals: dict[str, float] = defaultdict(float)
    for r in period_rows:
        supplier_totals[r["supplier"]] += r["amount"]
    ranking = sorted(supplier_totals.items(), key=lambda x: x[1], reverse=True)[:10]
    rank_df = pd.DataFrame(
        {"仕入先": [x[0] for x in ranking], "仕入額": [x[1] for x in ranking]}
    )

    with mid_l:
        st.markdown(f"**{trend_title}**")
        if monthly_df.empty:
            st.caption("データがありません。")
        else:
            x_col = "月" if period_mode == "年別" else "年月"
            st.bar_chart(monthly_df, x=x_col, y="仕入額", height=320)
    with mid_r:
        st.markdown(f"**仕入先ランキング（{period_label}）**")
        if rank_df.empty:
            st.caption("対象期間のデータがありません。")
        else:
            st.bar_chart(rank_df, x="仕入額", y="仕入先", height=320)

    st.divider()
    st.subheader("日別・単価")
    bot_l, bot_r = st.columns(2)

    if period_mode == "年別" and year_pick:
        monthly_in_year = _monthly_amounts_in_year(period_rows, year_pick)
        trend_df = monthly_in_year
        trend_x = "月"
        unit_by_month: dict[str, list[float]] = defaultdict(list)
        for r in period_rows:
            if r["unit_price"] is not None:
                unit_by_month[r["year_month"]].append(r["unit_price"])
        unit_labels = [f"{int(ym[5:7])}月" for ym in sorted(unit_by_month.keys())]
        unit_avg = [
            sum(unit_by_month[ym]) / len(unit_by_month[ym]) for ym in sorted(unit_by_month.keys())
        ]
        unit_df = pd.DataFrame({"月": unit_labels, "平均単価": unit_avg})
        daily_title = f"月次推移（{period_label}）"
        unit_title = f"単価推移（{period_label}・月次平均）"
    else:
        daily_totals: dict[str, float] = defaultdict(float)
        for r in period_rows:
            daily_totals[r["date"]] += r["amount"]
        daily_keys = sorted(daily_totals.keys())
        trend_df = pd.DataFrame({"日付": daily_keys, "仕入額": [daily_totals[d] for d in daily_keys]})
        trend_x = "日付"
        unit_by_date: dict[str, list[float]] = defaultdict(list)
        for r in period_rows:
            if r["unit_price"] is not None:
                unit_by_date[r["date"]].append(r["unit_price"])
        unit_keys = sorted(unit_by_date.keys())
        unit_avg = [sum(unit_by_date[d]) / len(unit_by_date[d]) for d in unit_keys]
        unit_df = pd.DataFrame({"日付": unit_keys, "平均単価": unit_avg})
        daily_title = (
            f"日別推移（{period_label}）"
            if period_mode == "日別"
            else f"日別推移（{period_label}・月内）"
        )
        unit_title = f"単価推移（{period_label}・日次平均）"

    with bot_l:
        st.markdown(f"**{daily_title}**")
        if not period_rows:
            st.caption("対象期間のデータがありません。")
        else:
            st.line_chart(trend_df, x=trend_x, y="仕入額", height=320)
    with bot_r:
        st.markdown(f"**{unit_title}**")
        if unit_df.empty:
            st.caption("単価が数値として読めるデータがありません。")
        else:
            unit_x = "月" if period_mode == "年別" else "日付"
            st.line_chart(unit_df, x=unit_x, y="平均単価", height=320)

    st.caption(
        f"表示中: {period_label}（{len(period_rows):,} 件）｜"
        f"データ保持範囲: {month_keys[0] if month_keys else '—'} 〜 {month_keys[-1] if month_keys else '—'}（全 {len(records):,} 件）"
    )


_SALES_DATE_ALIASES = ("取引営業日", "sales_date", "business_date", "sale_date", "日付", "売上日", "date")
_SALES_PRODUCT_ALIASES = ("商品名", "product_name", "品名")
_SALES_DEPARTMENT_ALIASES = ("部門", "department", "dept", "部")
_SALES_AMOUNT_ALIASES = ("売上", "amount", "sales", "売上額", "金額")
_SALES_QUANTITY_ALIASES = ("商品数", "数量", "quantity", "qty", "個数")


def _resolve_csv_column(columns: list, aliases: tuple[str, ...]) -> str | None:
    lowered = {str(c).strip().lower(): c for c in columns}
    for alias in aliases:
        key = alias.lower()
        if key in lowered:
            return str(lowered[key])
    for col in columns:
        cl = str(col).strip().lower()
        for alias in aliases:
            if alias.lower() in cl:
                return str(col)
    return None


def read_sales_csv_bytes(data: bytes) -> pd.DataFrame:
    """
    売上 CSV を読み込む。
    - 1行目（商品別の行）はヘッダーに使わない
    - 2行目を列名として使用（1列目に取引営業日がある想定）
    - 列はすべて使用する（1列目も含む）
    """
    last_err: Exception | None = None
    for enc in ("utf-8-sig", "utf-8", "cp932", "shift_jis"):
        try:
            df = pd.read_csv(io.BytesIO(data), encoding=enc, header=1)
            df.columns = [str(c).strip() for c in df.columns]
            return df
        except UnicodeDecodeError as e:
            last_err = e
    raise ValueError("CSV の文字コードを判別できませんでした（UTF-8 / Shift_JIS を想定）。") from last_err


def _is_sales_csv_category_row(row: pd.Series, col: dict[str, str]) -> bool:
    """1行目相当の「商品別」見出し行や集計行を除外。"""
    for key in ("date", "product", "department", "amount"):
        cell = row.get(col.get(key, ""))
        if cell is None or (isinstance(cell, float) and pd.isna(cell)):
            continue
        text = str(cell).strip()
        if text in ("商品別", "合計", "小計") or text.startswith("商品別"):
            return True
    return False


def _csv_date_to_iso(value) -> str:
    """CSV の日付セルを YYYY-MM-DD に正規化。"""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        n = int(value)
        s = str(n)
        if len(s) == 8 and s.isdigit():
            return normalize_purchase_date_to_iso(f"{s[:4]}-{s[4:6]}-{s[6:8]}")
    return normalize_purchase_date_to_iso(str(value).strip())


_WEEKDAY_NAMES_JA = ("月曜日", "火曜日", "水曜日", "木曜日", "金曜日", "土曜日", "日曜日")


def weekday_name_from_iso_date(date_iso: str) -> str:
    """YYYY-MM-DD から日本語の曜日名（例: 月曜日）を返す。"""
    d = normalize_purchase_date_to_iso(str(date_iso or "").strip())
    if len(d) < 10:
        return ""
    try:
        dt = datetime.strptime(d[:10], "%Y-%m-%d").date()
    except ValueError:
        return ""
    return _WEEKDAY_NAMES_JA[dt.weekday()]


def _normalize_weekday_label(label: str) -> str:
    """DB の weekday_name や略称を「月曜日」形式に揃える。"""
    s = str(label or "").strip()
    if not s:
        return ""
    if s in _WEEKDAY_NAMES_JA:
        return s
    for wd in _WEEKDAY_NAMES_JA:
        short = wd.replace("曜日", "曜")
        if s == wd or s == short or s + "曜日" == wd or wd.startswith(s):
            return wd
    return s


def weekday_label_for_record(row: dict, *, date_iso: str = "") -> str:
    """sales 生行または analytics 行から曜日ラベルを返す（DB 優先、なければ日付から算出）。"""
    wd_col = (SUPABASE_SALES_WEEKDAY_COLUMN or "weekday_name").strip()
    for key in ("weekday_name", wd_col):
        if not key:
            continue
        raw = str(row.get(key) or "").strip()
        if raw:
            normalized = _normalize_weekday_label(raw)
            if normalized:
                return normalized
    d = date_iso or str(row.get("date") or row.get(SUPABASE_SALES_DATE_COLUMN) or "").strip()
    return weekday_name_from_iso_date(d)


def attach_weekday_name_to_sales_payload(payload: dict) -> dict:
    """sales_date から weekday_name を付与（未設定のときのみ）。"""
    wd_col = (SUPABASE_SALES_WEEKDAY_COLUMN or "weekday_name").strip()
    date_col = (SUPABASE_SALES_DATE_COLUMN or "sales_date").strip()
    if not wd_col:
        return payload
    if str(payload.get(wd_col) or "").strip():
        return payload
    wd = weekday_name_from_iso_date(str(payload.get(date_col) or ""))
    if wd:
        payload[wd_col] = wd
    return payload


def resolve_sales_csv_columns(df: pd.DataFrame) -> dict[str, str | None]:
    """CSV から取引営業日・商品名・部門・売上・数量の列名を解決。"""
    cols = list(df.columns)
    mapping: dict[str, str | None] = {
        "date": _resolve_csv_column(cols, _SALES_DATE_ALIASES),
        "product": _resolve_csv_column(cols, _SALES_PRODUCT_ALIASES),
        "department": _resolve_csv_column(cols, _SALES_DEPARTMENT_ALIASES),
        "amount": _resolve_csv_column(cols, _SALES_AMOUNT_ALIASES),
        "quantity": _resolve_csv_column(cols, _SALES_QUANTITY_ALIASES),
    }
    labels = {
        "date": "取引営業日",
        "product": "商品名",
        "department": "部門",
        "amount": "売上",
        "quantity": "商品数",
    }
    if not mapping["date"] and cols:
        mapping["date"] = str(cols[0])

    required_keys = ("date", "product", "department", "amount")
    missing = [labels[k] for k in required_keys if not mapping.get(k)]
    if missing:
        raise ValueError(
            f"必須列が見つかりません: {', '.join(missing)}。"
            f"（2行目のヘッダーに {', '.join(labels[k] for k in required_keys)} が必要です。"
            "商品数（quantity）は任意です。取引営業日は1列目でも可）"
        )
    return {k: (str(v) if v else None) for k, v in mapping.items()}


def sales_dataframe_to_payloads(df: pd.DataFrame) -> tuple[list[dict], list[str]]:
    """CSV から取引営業日・商品名・部門・売上・数量を抜き出し Supabase sales 用に変換。"""
    col = resolve_sales_csv_columns(df)
    date_col = SUPABASE_SALES_DATE_COLUMN
    products_col = SUPABASE_SALES_PRODUCTS_COLUMN
    amt_col = SUPABASE_SALES_AMOUNT_COLUMN
    qty_col = SUPABASE_SALES_QUANTITY_COLUMN
    wd_col = SUPABASE_SALES_WEEKDAY_COLUMN

    warnings: list[str] = []
    payloads: list[dict] = []
    for i, (_, row) in enumerate(df.iterrows()):
        line_no = i + 3  # 1行目=商品別行、2行目=ヘッダー、3行目〜=データ
        if _is_sales_csv_category_row(row, col):
            continue
        raw_date = row.get(col["date"])
        if pd.isna(raw_date) or str(raw_date).strip() == "":
            continue
        date_iso = _csv_date_to_iso(raw_date)
        if not date_iso:
            warnings.append(f"{line_no}行目: 取引営業日を解釈できませんでした（{raw_date}）")
            continue

        product_name = row.get(col["product"])
        if pd.isna(product_name) or not str(product_name).strip():
            warnings.append(f"{line_no}行目: 商品名が空のためスキップしました。")
            continue

        department = row.get(col["department"])
        kategory_val = (
            str(department).strip()
            if department is not None and not pd.isna(department) and str(department).strip()
            else None
        )
        if not kategory_val:
            warnings.append(f"{line_no}行目: 部門が空のためスキップしました。")
            continue

        sales_amount = _coerce_sales_amount(row.get(col["amount"]))
        if sales_amount is None:
            warnings.append(f"{line_no}行目: 売上を数値として読めませんでした。")
            continue

        row_payload: dict = {
            date_col: date_iso,
            products_col: str(product_name).strip(),
            "_department": kategory_val,
            amt_col: int(sales_amount),
        }
        wd = weekday_name_from_iso_date(date_iso)
        if wd_col and wd:
            row_payload[wd_col] = wd
        if col.get("quantity"):
            sales_qty = _coerce_sales_quantity(row.get(col["quantity"]))
            if sales_qty is not None:
                row_payload[qty_col] = sales_qty
        payloads.append(row_payload)

    if not payloads:
        raise ValueError("取り込める有効な行がありませんでした。")
    return payloads, warnings


def sales_payloads_to_preview_df(payloads: list[dict]) -> pd.DataFrame:
    """保存前プレビュー用（日本語列名）。"""
    date_col = SUPABASE_SALES_DATE_COLUMN
    products_col = SUPABASE_SALES_PRODUCTS_COLUMN
    amt_col = SUPABASE_SALES_AMOUNT_COLUMN
    qty_col = SUPABASE_SALES_QUANTITY_COLUMN
    wd_col = SUPABASE_SALES_WEEKDAY_COLUMN
    rows = [
        {
            "取引営業日": p.get(date_col),
            "曜日": p.get(wd_col) or weekday_name_from_iso_date(str(p.get(date_col) or "")),
            "商品名": p.get(products_col),
            "部門": p.get("_department") or p.get("_preview_department"),
            "商品数": p.get(qty_col),
            "売上": p.get(amt_col),
        }
        for p in payloads
    ]
    return pd.DataFrame(rows)


def render_supabase_sales_schema_help() -> None:
    """sales / sales_products テーブルに必要な列が無いときの案内（database.md 準拠）。"""
    sales_tbl = SUPABASE_TABLE_SALES
    master_tbl = SUPABASE_TABLE_SALES_PRODUCTS
    st.markdown(
        f"`public.{master_tbl}` と `public.{sales_tbl}` は `database.md` の定義どおり次の列が必要です"
        "（Supabase → SQL Editor）。"
    )
    st.code(
        f"""create table if not exists public.{master_tbl} (
  id bigint generated by default as identity primary key,
  created_at timestamptz default now(),
  sales_category text,
  sales_category2 text,
  sales_products text
);

create table if not exists public.{sales_tbl} (
  id bigint generated by default as identity primary key,
  created_at timestamptz default now(),
  sales_date date,
  sales_products text,
  sales_amount bigint,
  quantity bigint,
  product_id bigint references public.{master_tbl}(id),
  weekday_name text
);

alter table public.{sales_tbl} enable row level security;
drop policy if exists "sales_insert_authenticated" on public.{sales_tbl};
create policy "sales_insert_authenticated"
  on public.{sales_tbl} for insert to authenticated with check (true);
drop policy if exists "sales_select_authenticated" on public.{sales_tbl};
create policy "sales_select_authenticated"
  on public.{sales_tbl} for select to authenticated using (true);

alter table public.{master_tbl} enable row level security;
drop policy if exists "sales_products_insert_authenticated" on public.{master_tbl};
create policy "sales_products_insert_authenticated"
  on public.{master_tbl} for insert to authenticated with check (true);
drop policy if exists "sales_products_select_authenticated" on public.{master_tbl};
create policy "sales_products_select_authenticated"
  on public.{master_tbl} for select to authenticated using (true);
""",
        language="sql",
    )


def insert_sales_to_supabase(client: Client, payloads: list[dict], *, batch_size: int = 500) -> int:
    """sales へ一括 insert。sales_products 参照を解決してから保存。"""
    if not payloads:
        return 0
    prepared = prepare_sales_payloads_for_insert(client, payloads)
    saved = 0
    tbl = SUPABASE_TABLE_SALES
    for i in range(0, len(prepared), batch_size):
        chunk = prepared[i : i + batch_size]
        client.table(tbl).insert(chunk).execute()
        saved += len(chunk)
    return saved


def fetch_sales_for_dashboard(client: Client, *, months_back: int = 36) -> list[dict]:
    start, end = _dashboard_date_range(months_back=months_back)
    rows = fetch_table_rows_paginated(
        client,
        SUPABASE_TABLE_SALES,
        date_col=SUPABASE_SALES_DATE_COLUMN,
        start=start,
        end=end,
    )
    catalog, _ = fetch_sales_product_catalog(client)
    return enrich_sales_rows(rows, catalog)


def count_sales_for_dashboard(client: Client, *, months_back: int = 36) -> int | None:
    start, end = _dashboard_date_range(months_back=months_back)
    return count_table_rows_in_date_range(
        client,
        SUPABASE_TABLE_SALES,
        date_col=SUPABASE_SALES_DATE_COLUMN,
        start=start,
        end=end,
    )


def fetch_sales_filtered(
    client: Client,
    year_month: str,
    product_name: str,
    kategory: str,
    category2: str = "",
    *,
    limit: int = 500,
) -> list[dict]:
    """Supabase sales を年月・商品名・部門・カテゴリ2で取得。"""
    date_col = SUPABASE_SALES_DATE_COLUMN
    products_col = SUPABASE_SALES_PRODUCTS_COLUMN
    pid_col = (SUPABASE_SALES_PRODUCT_ID_COLUMN or "product_id").strip()
    pn = (product_name or "").strip()
    kat = (kategory or "").strip()
    cat2 = (category2 or "").strip()
    catalog, _ = fetch_sales_product_catalog(client)

    master_ids = fetch_sales_master_ids_from_db(
        client, product_name=pn, department=kat, category2=cat2
    )
    if not master_ids and catalog:
        id_sets: list[set[int]] = []
        if pn:
            id_sets.append(set(_sales_master_ids_matching_product_name(catalog, pn)))
        if kat:
            id_sets.append(set(_sales_master_ids_matching_department(catalog, kat)))
        if cat2:
            id_sets.append(set(_sales_master_ids_matching_category2(catalog, cat2)))
        if id_sets:
            ids_set = id_sets[0]
            for s in id_sets[1:]:
                ids_set &= s
            master_ids = list(ids_set)

    select_expr = _sales_select_with_master_embed()
    q = client.table(SUPABASE_TABLE_SALES).select(select_expr)
    rng = parse_year_month_filter(year_month)
    if rng:
        start, end = rng
        q = q.gte(date_col, start).lte(date_col, end)

    if (kat or cat2) and master_ids:
        q = q.in_(pid_col, master_ids)
    elif pn and master_ids:
        id_list = ",".join(str(i) for i in master_ids)
        q = q.or_(f"{products_col}.ilike.%{pn}%,{pid_col}.in.({id_list})")
    elif pn:
        q = q.ilike(products_col, f"%{pn}%")
    elif (kat or cat2) and not master_ids:
        # マスタ未一致。日付範囲のみ取得し後段で絞る（該当なしの可能性大）
        pass

    if pn or kat or cat2:
        fetch_limit = min(max(limit * 10, 1000), 5000)
    else:
        fetch_limit = limit

    try:
        res = q.order(date_col, desc=True).limit(fetch_limit).execute()
        data = getattr(res, "data", None)
    except Exception:
        res = (
            client.table(SUPABASE_TABLE_SALES)
            .select("*")
            .order(date_col, desc=True)
            .limit(fetch_limit)
            .execute()
        )
        data = getattr(res, "data", None)

    rows = enrich_sales_rows(data if isinstance(data, list) else [], catalog)
    if pn:
        rows = [r for r in rows if _sales_row_matches_product_filter(r, pn)]
    if kat:
        rows = [r for r in rows if _sales_row_matches_department_filter(r, kat)]
    if cat2:
        rows = [r for r in rows if _sales_row_matches_category2_filter(r, cat2)]
    return rows[:limit]


def sales_rows_to_analytics(rows: list[dict]) -> list[dict]:
    date_col = SUPABASE_SALES_DATE_COLUMN
    amt_col = SUPABASE_SALES_AMOUNT_COLUMN
    qty_col = SUPABASE_SALES_QUANTITY_COLUMN
    out: list[dict] = []
    for r in rows:
        d = normalize_purchase_date_to_iso(str(r.get(date_col) or "").strip())
        if not d or len(d) < 7:
            continue
        raw_amt = r.get(amt_col)
        amount = float(_coerce_sales_amount(raw_amt) or parse_money_value(raw_amt) or 0)
        raw_qty = r.get(qty_col)
        quantity = _coerce_quantity_value(raw_qty)
        out.append(
            {
                "date": d,
                "year_month": d[:7],
                "weekday_name": weekday_label_for_record(r, date_iso=d) or "（未設定）",
                "amount": amount,
                "quantity": quantity,
                "kategory": (str(r.get("kategory") or "").strip() or "（未設定）"),
                "sales_category": (
                    str(r.get("sales_category") or "").strip() or "（未設定）"
                ),
                "sales_category2": str(r.get("sales_category2") or "").strip(),
                "product_name": (str(r.get("product_name") or "").strip() or "（未設定）"),
            }
        )
    return out


def _sales_category1_label(record: dict) -> str:
    """部門1（sales_products.sales_category）のみ。"""
    cat = str(record.get("sales_category") or "").strip()
    return cat or "（未設定）"


def render_sales_csv_import_page() -> None:
    st.title("📥 売上 CSV 取り込み")
    st.caption(
        f"CSV から **取引営業日・商品名・部門・売上**（＋ **商品数** → `{SUPABASE_SALES_QUANTITY_COLUMN}`）を抜き出し、"
        f"Supabase の `{SUPABASE_TABLE_SALES}` テーブルに保存します。"
        " **1行目（商品別の行）だけ無視** し、**2行目をヘッダー**として読み込みます。"
        " **1列目（取引営業日）も含めて** すべての列を使います。"
    )

    uploaded = st.file_uploader("CSV ファイル", type=["csv"], key="sales_csv_upload")
    if not uploaded:
        if st.session_state.get("sales_import_message"):
            st.success(st.session_state.sales_import_message)
        st.info("CSV ファイルを選択してください。")
        return

    file_bytes = uploaded.getvalue()
    file_id = hashlib.md5(file_bytes).hexdigest()
    if st.session_state.get("sales_csv_file_id") != file_id:
        st.session_state.sales_csv_file_id = file_id
        st.session_state.pop("sales_csv_payloads", None)
        st.session_state.pop("sales_import_message", None)
        st.session_state.pop("sales_import_error", None)

    try:
        raw_df = read_sales_csv_bytes(file_bytes)
    except Exception as e:
        st.error(f"CSV の読み込みに失敗しました: {e}")
        return

    st.subheader("取り込み対象（2行目ヘッダー）")
    st.dataframe(raw_df.head(200), use_container_width=True, height=min(420, 36 * min(len(raw_df), 12) + 48))
    st.caption(f"全 {len(raw_df):,} 行（先頭 200 行まで表示・CSV 1行目の商品別行は除外済み）")

    try:
        col_map = resolve_sales_csv_columns(raw_df)
        qty_note = (
            f" / 商品数=`{col_map['quantity']}` → `{SUPABASE_SALES_QUANTITY_COLUMN}`"
            if col_map.get("quantity")
            else f" / 商品数=（列なし・`{SUPABASE_SALES_QUANTITY_COLUMN}` は保存時省略）"
        )
        st.caption(
            "認識した列: "
            f"取引営業日=`{col_map['date']}` / 商品名=`{col_map['product']}` / "
            f"部門=`{col_map['department']}` / 売上=`{col_map['amount']}`{qty_note}"
        )
        payloads, warnings = sales_dataframe_to_payloads(raw_df)
        st.session_state.sales_csv_payloads = payloads
    except Exception as e:
        st.session_state.pop("sales_csv_payloads", None)
        st.error(str(e))
        return

    st.caption(
        f"Supabase 列: `{SUPABASE_SALES_DATE_COLUMN}`, `{SUPABASE_SALES_WEEKDAY_COLUMN}`（取引日から自動）, "
        f"`{SUPABASE_SALES_PRODUCTS_COLUMN}`, "
        f"`{SUPABASE_TABLE_SALES_PRODUCTS}`（`{SUPABASE_SALES_PRODUCT_ID_COLUMN}` → マスタ id）, "
        f"`{SUPABASE_SALES_QUANTITY_COLUMN}`, `{SUPABASE_SALES_AMOUNT_COLUMN}`"
    )

    for w in warnings[:20]:
        st.warning(w)
    if len(warnings) > 20:
        st.caption(f"…他 {len(warnings) - 20} 件の警告")

    st.subheader("取り込み内容（抜き出し結果）")
    preview_df = sales_payloads_to_preview_df(payloads)
    st.dataframe(preview_df.head(200), use_container_width=True)
    st.caption(f"保存対象: {len(payloads):,} 行")

    if st.session_state.get("sales_import_message"):
        st.success(st.session_state.sales_import_message)
    if st.session_state.get("sales_import_error"):
        st.error(st.session_state.sales_import_error)

    if get_supabase_client_for_writes():
        st.caption("Supabase 接続: OK")
    else:
        st.warning(
            "Supabase に未接続です。ログインするか、`.env` の `SUPABASE_URL` と "
            "`SUPABASE_KEY` / `SUPABASE_SERVICE_ROLE_KEY` を確認してください。"
        )

    if st.button(
        "Supabase に取込",
        type="primary",
        key="sales_csv_import_btn",
        disabled=not payloads,
    ):
        st.session_state.pop("sales_import_message", None)
        st.session_state.pop("sales_import_error", None)
        import_client = get_supabase_client_for_writes()
        if not import_client:
            st.session_state.sales_import_error = (
                "Supabase に接続できません。ログインするか、`.env` のキーを設定してください。"
            )
            st.rerun()
        to_save = st.session_state.get("sales_csv_payloads") or payloads
        try:
            with st.spinner(f"`{SUPABASE_TABLE_SALES}` に {len(to_save):,} 行を保存しています…"):
                saved = insert_sales_to_supabase(import_client, to_save)
            st.session_state.pop("dashboard_sales", None)
            st.session_state.pop("dashboard_sales_fetch_error", None)
            st.session_state.sales_import_message = (
                f"{saved:,} 行を `{SUPABASE_TABLE_SALES}` に取り込みました。"
                "「売上 → ダッシュボード」で確認できます。"
            )
        except Exception as err:
            err_text = str(err)
            st.session_state.sales_import_error = f"取込に失敗しました: {err_text}"
            if "PGRST204" in err_text or "column" in err_text.lower():
                with st.expander("テーブル定義の直し方（Supabase 側）", expanded=True):
                    render_supabase_sales_schema_help()
            elif "row-level security" in err_text.lower() or "42501" in err_text:
                with st.expander("RLS ポリシーの直し方", expanded=True):
                    render_supabase_rls_error_help(SUPABASE_TABLE_SALES)
        st.rerun()


def _year_month_to_date(ym: str) -> date:
    y, m = int(ym[:4]), int(ym[5:7])
    return date(y, m, 1)


def _build_sales_monthly_trend_df(
    records: list[dict],
    *,
    year_pick: int | None,
    month_keys: list[str],
    period_mode: str,
) -> tuple[pd.DataFrame, str]:
    """月別推移用 DataFrame（横軸は各月1日の日付）。"""
    if period_mode == "年別" and year_pick:
        rows = [
            {
                "日付": date(year_pick, m, 1),
                "売上額": sum(
                    r["amount"]
                    for r in records
                    if r["year_month"] == f"{year_pick:04d}-{m:02d}"
                ),
            }
            for m in range(1, 13)
        ]
        return pd.DataFrame(rows), f"月別推移（{year_pick}年）"

    chart_month_keys = month_keys[-12:] if len(month_keys) > 12 else month_keys
    rows = [
        {
            "日付": _year_month_to_date(ym),
            "売上額": sum(r["amount"] for r in records if r["year_month"] == ym),
        }
        for ym in chart_month_keys
    ]
    return pd.DataFrame(rows), "月別推移（直近12ヶ月）"


def _shift_date_back_years(d: date, years: int = 1) -> date:
    try:
        return d.replace(year=d.year - years)
    except ValueError:
        return d.replace(year=d.year - years, day=28)


def sales_dashboard_yoy_comparison(
    records: list[dict],
    period_mode: str,
    *,
    month_pick: date | None = None,
    year_pick: int | None = None,
    range_start: date | None = None,
    range_end: date | None = None,
) -> tuple[list[dict], list[dict], str, str]:
    """売上ダッシュボード用の前年同月・前年同期比較レコードを返す。"""
    if period_mode == "月別":
        if not month_pick:
            month_pick = datetime.now().date().replace(day=1)
        sel_ym = month_pick.strftime("%Y-%m")
        yoy_ym = f"{month_pick.year - 1:04d}-{month_pick.month:02d}"
        period = [r for r in records if r["year_month"] == sel_ym]
        comparison = [r for r in records if r["year_month"] == yoy_ym]
        period_label = f"{month_pick.year}年{month_pick.month}月"
        comparison_label = f"{month_pick.year - 1}年{month_pick.month}月"
        return period, comparison, period_label, comparison_label

    if period_mode == "年別":
        y = year_pick or datetime.now().year
        y_str = f"{y:04d}"
        prev_str = f"{y - 1:04d}"
        period = [r for r in records if r["date"].startswith(y_str)]
        comparison = [r for r in records if r["date"].startswith(prev_str)]
        return period, comparison, f"{y}年", f"{y - 1}年"

    if not range_start or not range_end:
        today = datetime.now().date()
        range_start = today.replace(day=1)
        range_end = today
    start_iso, end_iso = _date_to_iso(range_start), _date_to_iso(range_end)
    period = [r for r in records if start_iso <= r["date"] <= end_iso]
    comp_start = _shift_date_back_years(range_start, 1)
    comp_end = _shift_date_back_years(range_end, 1)
    comp_start_iso, comp_end_iso = _date_to_iso(comp_start), _date_to_iso(comp_end)
    comparison = [r for r in records if comp_start_iso <= r["date"] <= comp_end_iso]
    if range_start == range_end:
        period_label = start_iso
    else:
        period_label = f"{start_iso} 〜 {end_iso}"
    comparison_label = f"前年同期（{comp_start_iso} 〜 {comp_end_iso}）"
    return period, comparison, period_label, comparison_label


def _aggregate_sales_amount_by_weekday(rows: list[dict]) -> dict[str, float]:
    out: dict[str, float] = defaultdict(float)
    for r in rows:
        wd = str(r.get("weekday_name") or "").strip()
        if not wd or wd == "（未設定）":
            wd = weekday_name_from_iso_date(str(r.get("date") or ""))
        if wd:
            out[wd] += r["amount"]
    return dict(out)


def _build_sales_yoy_amount_trend_df(
    period_rows: list[dict],
    yoy_rows: list[dict],
    period_label: str,
    yoy_label: str,
    *,
    period_mode: str,
    year_pick: int | None = None,
) -> tuple[pd.DataFrame, str]:
    """前年同月・同期の売上金額比較（日別 or 月別）。"""
    if period_mode == "年別" and year_pick:
        rows = []
        for m in range(1, 13):
            ym_cur = f"{year_pick:04d}-{m:02d}"
            ym_prev = f"{year_pick - 1:04d}-{m:02d}"
            rows.append(
                {
                    "月": f"{m}月",
                    period_label: sum(r["amount"] for r in period_rows if r["year_month"] == ym_cur),
                    yoy_label: sum(r["amount"] for r in yoy_rows if r["year_month"] == ym_prev),
                }
            )
        return pd.DataFrame(rows), f"月別売上（{period_label} vs {yoy_label}・各月前年同月比）"

    if period_mode == "月別":
        by_day_cur: dict[int, float] = defaultdict(float)
        by_day_yoy: dict[int, float] = defaultdict(float)
        for r in period_rows:
            by_day_cur[int(r["date"][8:10])] += r["amount"]
        for r in yoy_rows:
            by_day_yoy[int(r["date"][8:10])] += r["amount"]
        days = sorted(set(by_day_cur) | set(by_day_yoy))
        rows = [
            {
                "日": d,
                period_label: by_day_cur.get(d, 0.0),
                yoy_label: by_day_yoy.get(d, 0.0),
            }
            for d in days
        ]
        return (
            pd.DataFrame(rows),
            f"日別売上（{period_label} vs {yoy_label}）",
        )

    cur_dates = sorted({r["date"] for r in period_rows})
    yoy_dates = sorted({r["date"] for r in yoy_rows})
    cur_map = {d: sum(r["amount"] for r in period_rows if r["date"] == d) for d in cur_dates}
    yoy_map = {d: sum(r["amount"] for r in yoy_rows if r["date"] == d) for d in yoy_dates}
    span = max(len(cur_dates), len(yoy_dates))
    rows = []
    for i in range(span):
        row: dict = {"日": i + 1}
        if i < len(cur_dates):
            row[period_label] = cur_map[cur_dates[i]]
        else:
            row[period_label] = 0.0
        if i < len(yoy_dates):
            row[yoy_label] = yoy_map[yoy_dates[i]]
        else:
            row[yoy_label] = 0.0
        rows.append(row)
    return (
        pd.DataFrame(rows),
        f"日別売上（{period_label} vs {yoy_label}）",
    )


def _build_sales_yoy_weekday_amount_df(
    period_rows: list[dict],
    yoy_rows: list[dict],
    period_label: str,
    yoy_label: str,
) -> pd.DataFrame:
    """曜日別売上の前年同月比較（ロング形式）。"""
    cur = _aggregate_sales_amount_by_weekday(period_rows)
    prev = _aggregate_sales_amount_by_weekday(yoy_rows)
    rows: list[dict] = []
    for wd in _WEEKDAY_NAMES_JA:
        if wd not in cur and wd not in prev:
            continue
        rows.append({"曜日": wd, "期間": period_label, "売上額": cur.get(wd, 0.0)})
        rows.append({"曜日": wd, "期間": yoy_label, "売上額": prev.get(wd, 0.0)})
    return pd.DataFrame(rows)


def _build_sales_period_weekday_df(rows: list[dict]) -> pd.DataFrame:
    """指定期間の曜日別売上（月〜日の順）。"""
    wd_amounts = _aggregate_sales_amount_by_weekday(rows)
    return pd.DataFrame(
        [{"曜日": wd, "売上額": float(wd_amounts.get(wd, 0.0))} for wd in _WEEKDAY_NAMES_JA]
    )


def _render_sales_monthly_trend_chart(df: pd.DataFrame) -> None:
    """月別売上のエリア＋折れ線チャート（水色テーマ）。"""
    if df.empty or df["売上額"].sum() == 0:
        st.caption("データがありません。")
        return
    try:
        import altair as alt

        chart = (
            alt.Chart(df)
            .mark_area(
                line={"color": "#0284C7", "strokeWidth": 2},
                color=alt.Gradient(
                    gradient="linear",
                    stops=[
                        alt.GradientStop(color="#BAE6FD", offset=0),
                        alt.GradientStop(color="#F0F9FF", offset=1),
                    ],
                    x1=1,
                    x2=1,
                    y1=1,
                    y2=0,
                ),
                interpolate="monotone",
            )
            .encode(
                x=alt.X("日付:T", title="月", axis=alt.Axis(format="%Y/%m", gridColor="#E0F2FE")),
                y=alt.Y("売上額:Q", title="売上額", axis=alt.Axis(format=",.0f", gridColor="#E0F2FE")),
                tooltip=[
                    alt.Tooltip("日付:T", title="月", format="%Y年%m月"),
                    alt.Tooltip("売上額:Q", title="売上額", format=",.0f"),
                ],
            )
            .properties(height=340)
        )
        st.altair_chart(chart, use_container_width=True)
    except ImportError:
        st.line_chart(df, x="日付", y="売上額", height=320)


def _render_sales_weekday_chart(df: pd.DataFrame) -> None:
    """曜日別売上の棒グラフ（土日を濃い水色）。"""
    if df.empty or df["売上額"].sum() == 0:
        st.caption("データがありません。")
        return
    try:
        import altair as alt

        chart = (
            alt.Chart(df)
            .mark_bar(cornerRadiusEnd=5, size=28)
            .encode(
                x=alt.X(
                    "曜日:N",
                    sort=list(_WEEKDAY_NAMES_JA),
                    title=None,
                    axis=alt.Axis(labelAngle=0, grid=False),
                ),
                y=alt.Y(
                    "売上額:Q",
                    title="売上額",
                    axis=alt.Axis(format=",.0f", gridColor="#E0F2FE"),
                ),
                color=alt.condition(
                    (alt.datum.曜日 == "土曜日") | (alt.datum.曜日 == "日曜日"),
                    alt.value("#0284C7"),
                    alt.value("#7DD3FC"),
                ),
                tooltip=[
                    alt.Tooltip("曜日:N", title="曜日"),
                    alt.Tooltip("売上額:Q", title="売上額", format=",.0f"),
                ],
            )
            .properties(height=340)
        )
        st.altair_chart(chart, use_container_width=True)
    except ImportError:
        st.bar_chart(df.set_index("曜日")["売上額"], height=320)


def _build_sales_yoy_dept_amount_df(
    period_rows: list[dict],
    yoy_rows: list[dict],
    period_label: str,
    yoy_label: str,
    *,
    top_n: int = 8,
) -> pd.DataFrame:
    """部門別売上の前年同月比較（ロング形式・上位部門）。"""
    cur: dict[str, float] = defaultdict(float)
    prev: dict[str, float] = defaultdict(float)
    for r in period_rows:
        cur[r["kategory"]] += r["amount"]
    for r in yoy_rows:
        prev[r["kategory"]] += r["amount"]
    ranked = sorted(
        set(cur) | set(prev),
        key=lambda k: cur.get(k, 0.0) + prev.get(k, 0.0),
        reverse=True,
    )[:top_n]
    rows: list[dict] = []
    for dept in ranked:
        rows.append({"部門": dept, "期間": period_label, "売上額": cur.get(dept, 0.0)})
        rows.append({"部門": dept, "期間": yoy_label, "売上額": prev.get(dept, 0.0)})
    return pd.DataFrame(rows)


def _render_sales_yoy_line_chart(
    df: pd.DataFrame,
    *,
    x_col: str,
    period_label: str,
    yoy_label: str,
) -> None:
    if df.empty or period_label not in df.columns or yoy_label not in df.columns:
        st.caption("比較できるデータがありません。")
        return
    if df[period_label].sum() == 0 and df[yoy_label].sum() == 0:
        st.caption("比較できるデータがありません。")
        return
    st.line_chart(df, x=x_col, y=[period_label, yoy_label], height=320)


def _render_sales_yoy_grouped_bar(
    df: pd.DataFrame,
    *,
    x_col: str,
    y_col: str = "売上額",
    color_col: str = "期間",
) -> None:
    if df.empty or df[y_col].sum() == 0:
        st.caption("比較できるデータがありません。")
        return
    try:
        import altair as alt

        chart = (
            alt.Chart(df)
            .mark_bar()
            .encode(
                x=alt.X(f"{x_col}:N", sort=None, title=x_col),
                y=alt.Y(f"{y_col}:Q", title="売上額"),
                color=alt.Color(f"{color_col}:N", title="期間"),
                xOffset=alt.XOffset(f"{color_col}:N"),
                tooltip=[
                    alt.Tooltip(f"{x_col}:N", title=x_col),
                    alt.Tooltip(f"{color_col}:N", title="期間"),
                    alt.Tooltip(f"{y_col}:Q", title="売上額", format=",.0f"),
                ],
            )
            .properties(height=320)
        )
        st.altair_chart(chart, use_container_width=True)
    except ImportError:
        pivot = df.pivot(index=x_col, columns=color_col, values=y_col).fillna(0)
        st.bar_chart(pivot, height=320)


_PIE_TOP_LABEL_COUNT = 3


def _build_altair_pie_with_top_labels(
    df: pd.DataFrame,
    *,
    name_col: str,
    legend_title: str,
    outer_radius: int = 120,
    label_radius: int = 70,
    chart_height: int = 360,
    legend_columns: int = 3,
):
    """円グラフ＋上位N件に名称・構成比ラベル（Altair）。"""
    import altair as alt

    labels = df[name_col].tolist()
    colors = _sales_blue_gradient_for_labels(labels)
    sort_field = alt.EncodingSortField(field="売上額", order="descending")
    label_df = df.head(_PIE_TOP_LABEL_COUNT).copy()
    label_df["表示ラベル"] = label_df.apply(
        lambda r: f"{r[name_col]}\n{r['構成比']:.1f}%",
        axis=1,
    )

    arc = (
        alt.Chart(df)
        .mark_arc(outerRadius=outer_radius)
        .encode(
            theta=alt.Theta("売上額:Q", stack=True, sort=sort_field),
            order=alt.Order("売上額:Q", sort="descending"),
            color=alt.Color(
                f"{name_col}:N",
                sort=sort_field,
                scale=alt.Scale(domain=labels, range=colors),
                legend=alt.Legend(
                    title=legend_title,
                    orient="bottom",
                    direction="horizontal",
                    columns=min(legend_columns, max(1, len(labels))),
                    symbolSize=80,
                    labelLimit=120,
                ),
            ),
            tooltip=[
                alt.Tooltip(f"{name_col}:N", title=legend_title),
                alt.Tooltip("売上額:Q", title="売上額", format=",.0f"),
                alt.Tooltip("構成比:Q", title="構成比", format=".1f"),
            ],
        )
    )
    text = (
        alt.Chart(label_df)
        .mark_text(radius=label_radius, size=10, color="#0C4A6E", lineBreak="\n")
        .encode(
            theta=alt.Theta("売上額:Q", stack=True, sort=sort_field),
            order=alt.Order("売上額:Q", sort="descending"),
            text="表示ラベル:N",
        )
    )
    return (arc + text).properties(height=chart_height)


def _render_sales_dept_pie_chart(dept_df: pd.DataFrame) -> None:
    """部門1（sales_category）別売上の円グラフ。構成比の大きい順。"""
    if dept_df.empty or dept_df["売上額"].sum() == 0:
        st.caption("データがありません。")
        return

    df = dept_df.sort_values("売上額", ascending=False).reset_index(drop=True)
    labels = df["部門"].tolist()
    colors = _sales_blue_gradient_for_labels(labels)
    total = float(df["売上額"].sum())
    df = df.copy()
    df["構成比"] = df["売上額"] / total * 100.0

    try:
        import altair as alt

        chart = _build_altair_pie_with_top_labels(
            df,
            name_col="部門",
            legend_title="部門1",
            outer_radius=130,
            label_radius=78,
            chart_height=380,
            legend_columns=3,
        )
        st.altair_chart(chart, use_container_width=True)
    except ImportError:
        import matplotlib.pyplot as plt

        fig, ax = plt.subplots(figsize=(8, 5))
        ax.pie(
            df["売上額"],
            labels=df["部門"],
            autopct="%1.1f%%",
            startangle=90,
            counterclock=False,
            colors=colors,
        )
        ax.axis("equal")
        ax.legend(
            df["部門"],
            loc="upper center",
            bbox_to_anchor=(0.5, -0.05),
            ncol=min(3, len(df)),
            fontsize=10,
            frameon=False,
        )
        fig.subplots_adjust(bottom=0.18)
        st.pyplot(fig)
        plt.close(fig)


def _render_sales_product_pie_chart(
    product_df: pd.DataFrame,
    *,
    name_col: str = "商品名",
    legend_title: str | None = None,
) -> None:
    """売上構成の円グラフ。構成比の大きい順。"""
    if product_df.empty or product_df["売上額"].sum() == 0:
        st.caption("データがありません。")
        return

    legend = legend_title or name_col
    df = product_df.sort_values("売上額", ascending=False).reset_index(drop=True)
    labels = df[name_col].tolist()
    colors = _sales_blue_gradient_for_labels(labels)
    total = float(df["売上額"].sum())
    df = df.copy()
    df["構成比"] = df["売上額"] / total * 100.0

    try:
        chart = _build_altair_pie_with_top_labels(
            df,
            name_col=name_col,
            legend_title=legend,
            outer_radius=105,
            label_radius=62,
            chart_height=340,
            legend_columns=2,
        )
        st.altair_chart(chart, use_container_width=True)
    except ImportError:
        st.caption("グラフ表示には altair のインストールが必要です。")


_SALES_BLUE_GRADIENT: tuple[str, ...] = (
    "#0284C7",
    "#38BDF8",
    "#7DD3FC",
    "#BAE6FD",
    "#E0F2FE",
    "#F0F9FF",
)


def _sales_blue_gradient_for_labels(labels: list[str]) -> list[str]:
    """売上順ラベルに薄い水色グラデーションを割り当て（「その他」は最薄）。"""
    colors: list[str] = []
    rank = 0
    for name in labels:
        if name == "その他":
            colors.append(_SALES_BLUE_GRADIENT[-1])
        else:
            colors.append(_SALES_BLUE_GRADIENT[min(rank, len(_SALES_BLUE_GRADIENT) - 2)])
            rank += 1
    return colors


_SALES_LIGHT_CYAN_RGB_START = (56, 189, 248)  # #38BDF8
_SALES_LIGHT_CYAN_RGB_END = (240, 249, 255)  # #F0F9FF


def _sales_light_cyan_for_labels(
    labels: list[str],
    *,
    rank_offset: int = 0,
    rank_total: int | None = None,
) -> list[str]:
    """薄い水色グラデーション（1位=やや濃い水色、下位=より薄い、「その他」=最薄）。"""
    products = [name for name in labels if name != "その他"]
    total = rank_total if rank_total is not None else len(products)
    colors_map: dict[str, str] = {}
    product_rank = 0
    for name in labels:
        if name == "その他":
            colors_map[name] = "#F0F9FF"
            continue
        if total <= 1:
            t = 0.0
        else:
            t = (rank_offset + product_rank) / (total - 1)
        r = int(
            _SALES_LIGHT_CYAN_RGB_START[0]
            + (_SALES_LIGHT_CYAN_RGB_END[0] - _SALES_LIGHT_CYAN_RGB_START[0]) * t
        )
        g = int(
            _SALES_LIGHT_CYAN_RGB_START[1]
            + (_SALES_LIGHT_CYAN_RGB_END[1] - _SALES_LIGHT_CYAN_RGB_START[1]) * t
        )
        b = int(
            _SALES_LIGHT_CYAN_RGB_START[2]
            + (_SALES_LIGHT_CYAN_RGB_END[2] - _SALES_LIGHT_CYAN_RGB_START[2]) * t
        )
        colors_map[name] = f"#{r:02x}{g:02x}{b:02x}"
        product_rank += 1
    return [colors_map[name] for name in labels]


def _category2_chart_display_order(df: pd.DataFrame) -> pd.DataFrame:
    """横棒用: 売上1位を上、「その他」は最下段。"""
    others = df[df["商品名"] == "その他"]
    products = df[df["商品名"] != "その他"].sort_values("売上額", ascending=False)
    if others.empty:
        return products.reset_index(drop=True)
    return pd.concat([products, others], ignore_index=True)


def _category2_bar_y_sort(panel_df: pd.DataFrame) -> list[str]:
    """Y軸ソート: 構成比降順（上=最大）。「その他」は常に最下段。"""
    products = panel_df[panel_df["商品名"] != "その他"].sort_values("構成比", ascending=False)
    order = products["商品名"].tolist()
    if "その他" in panel_df["商品名"].values:
        order.append("その他")
    return order


def _render_category2_bar_panel(
    display_df: pd.DataFrame,
    *,
    total: float,
    rank_offset: int = 0,
    rank_total: int | None = None,
) -> None:
    """横棒1列分（Altair・売上割合の大きい順＝上から、薄い水色）。"""
    if display_df.empty:
        st.caption("データがありません。")
        return

    panel_df = display_df.copy()
    panel_df["構成比"] = panel_df["売上額"] / total * 100.0
    panel_df["金額ラベル"] = panel_df.apply(
        lambda r: f"¥{r['売上額']:,.0f}（{r['構成比']:.1f}%）",
        axis=1,
    )

    try:
        import altair as alt
    except ImportError:
        st.caption("グラフ表示には altair のインストールが必要です。")
        return

    y_sort = _category2_bar_y_sort(panel_df)
    color_order = [
        name
        for name in panel_df.sort_values("構成比", ascending=False)["商品名"].tolist()
        if name != "その他"
    ]
    if "その他" in panel_df["商品名"].values:
        color_order.append("その他")
    cyan_colors = _sales_light_cyan_for_labels(
        color_order,
        rank_offset=rank_offset,
        rank_total=rank_total,
    )
    bar_height = max(220, 30 * len(panel_df))
    bars = (
        alt.Chart(panel_df)
        .mark_bar(size=20, cornerRadiusEnd=3)
        .encode(
            x=alt.X(
                "構成比:Q",
                title="構成比 (%)",
                axis=alt.Axis(format=".1f", gridColor="#BAE6FD"),
            ),
            y=alt.Y(
                "商品名:N",
                sort=y_sort,
                title=None,
                axis=alt.Axis(labelLimit=240, labelColor="#0C4A6E"),
            ),
            color=alt.Color(
                "商品名:N",
                sort=y_sort,
                scale=alt.Scale(domain=color_order, range=cyan_colors),
                legend=None,
            ),
            tooltip=[
                alt.Tooltip("商品名:N", title="商品名"),
                alt.Tooltip("売上額:Q", title="売上額", format=",.0f"),
                alt.Tooltip("構成比:Q", title="構成比", format=".1f"),
            ],
        )
    )
    labels_layer = (
        alt.Chart(panel_df)
        .mark_text(align="left", baseline="middle", dx=5, fontSize=10, color="#0C4A6E")
        .encode(
            x=alt.X("構成比:Q"),
            y=alt.Y("商品名:N", sort=y_sort),
            text="金額ラベル:N",
        )
    )
    st.altair_chart((bars + labels_layer).properties(height=bar_height), use_container_width=True)


def _render_sales_category2_product_rank_chart(
    df: pd.DataFrame,
    *,
    two_columns: bool = False,
) -> None:
    """カテゴリ2別・売れ筋商品（横棒＋構成比、薄い水色）。"""
    if df.empty or df["売上額"].sum() == 0:
        st.caption("データがありません。")
        return

    total = float(df["売上額"].sum())
    ranked_df = df.sort_values("売上額", ascending=False).reset_index(drop=True)
    ranked_df = ranked_df.copy()
    ranked_df["構成比"] = ranked_df["売上額"] / total * 100.0
    display_df = _category2_chart_display_order(ranked_df)
    rank_total = len(display_df)

    use_two_cols = two_columns and len(display_df) > 1
    if use_two_cols:
        mid = (len(display_df) + 1) // 2
        left_df = display_df.iloc[:mid].reset_index(drop=True)
        right_df = display_df.iloc[mid:].reset_index(drop=True)
        col_l, col_r = st.columns(2)
        with col_l:
            _render_category2_bar_panel(
                left_df,
                total=total,
                rank_offset=0,
                rank_total=rank_total,
            )
        with col_r:
            _render_category2_bar_panel(
                right_df,
                total=total,
                rank_offset=mid,
                rank_total=rank_total,
            )
    else:
        _render_category2_bar_panel(display_df, total=total, rank_total=rank_total)


_SALES_DASHBOARD_CATEGORY2_TOP_N_DEFAULT = 5
_SALES_DASHBOARD_CATEGORY2_TOP_N_BY_GROUP: dict[str, int] = {
    "焼肉": 34,  # 35位未満を「その他」（上位34商品を個別表示）
    "ステーキ": 11,  # 12位未満を「その他」（上位11商品を個別表示）
}
_SALES_DASHBOARD_CATEGORY2_TWO_COLUMN_GROUPS = frozenset({"焼肉"})


def _category2_top_n_for_group(group_label: str) -> int:
    return _SALES_DASHBOARD_CATEGORY2_TOP_N_BY_GROUP.get(
        group_label,
        _SALES_DASHBOARD_CATEGORY2_TOP_N_DEFAULT,
    )

_SALES_DASHBOARD_CATEGORY2_GROUPS: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("セット商品", ("セット",), "product_name"),
    ("焼肉", ("焼肉",), "category2"),
    (
        "すきやき・しゃぶしゃぶ",
        (
            "すきやき・しゃぶしゃぶ",
            "すき焼き・しゃぶしゃぶ",
            "すきやきしゃぶしゃぶ",
            "すき焼きしゃぶしゃぶ",
        ),
        "category2",
    ),
    ("ステーキ", ("ステーキ",), "category2"),
)

_SALES_DASHBOARD_CATEGORY2_QUICK_PIE_GROUPS: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("豚肉", ("豚肉",), "category2"),
    ("弁当", ("弁当",), "category2"),
    ("惣菜", ("惣菜",), "category1"),
    ("揚げ物", ("揚げ物",), "category2"),
)
_SALES_DASHBOARD_CATEGORY2_QUICK_PIE_TOP_N = 10

# 後方互換（旧名）
_SALES_DASHBOARD_CATEGORY2_PIE_GROUPS = _SALES_DASHBOARD_CATEGORY2_GROUPS


def _normalize_category2_for_match(text: str) -> str:
    """カテゴリ2の表記ゆれ（空白・中点など）を吸収。"""
    return re.sub(r"[\s　・./／\-－_]+", "", str(text or "").strip())


def _record_matches_category2_group(category2: str, needles: tuple[str, ...]) -> bool:
    c = _normalize_category2_for_match(category2)
    if not c:
        return False
    for needle in needles:
        n = _normalize_category2_for_match(needle)
        if n and (n == c or n in c or c in n):
            return True
    return False


_SALES_SET_PRODUCT_NEEDLE = "セット"


def _is_set_product_name(product_name: str) -> bool:
    return _record_matches_product_name_group(product_name, (_SALES_SET_PRODUCT_NEEDLE,))


def _record_matches_category2_exact(category2: str, needles: tuple[str, ...]) -> bool:
    """カテゴリ2が needle と一致（表記ゆれのみ吸収）。"""
    c = _normalize_category2_for_match(category2)
    if not c:
        return False
    for needle in needles:
        n = _normalize_category2_for_match(needle)
        if n and n == c:
            return True
    return False


def _record_matches_product_name_group(product_name: str, needles: tuple[str, ...]) -> bool:
    """商品名に指定語句が含まれるか（セット商品用）。"""
    name = str(product_name or "").strip()
    if not name:
        return False
    return any(needle and needle in name for needle in needles)


def _build_category2_top_product_pie_df(
    rows: list[dict],
    *,
    needles: tuple[str, ...],
    match_by: str = "category2",
    top_n: int = 5,
    exclude_set_products: bool = False,
    category2_exact: bool = False,
    include_all: bool = False,
) -> pd.DataFrame:
    """カテゴリ2または商品名で絞り込み、商品別売上 Top N（残りは「その他」）。"""
    totals: dict[str, float] = defaultdict(float)
    for r in rows:
        product_name = str(r.get("product_name") or "（未設定）")
        if match_by == "product_name":
            if not _record_matches_product_name_group(product_name, needles):
                continue
        else:
            cat2 = str(r.get("sales_category2") or "").strip()
            if category2_exact:
                matched = _record_matches_category2_exact(cat2, needles)
            else:
                matched = _record_matches_category2_group(cat2, needles)
            if not matched:
                continue
            if exclude_set_products and _is_set_product_name(product_name):
                continue
        totals[product_name] += r["amount"]
    if not totals:
        return pd.DataFrame(columns=["商品名", "売上額"])
    ranked = sorted(totals.items(), key=lambda x: x[1], reverse=True)
    if include_all:
        return pd.DataFrame([{"商品名": name, "売上額": amt} for name, amt in ranked])
    top = ranked[:top_n]
    other = sum(amt for _, amt in ranked[top_n:])
    out = [{"商品名": name, "売上額": amt} for name, amt in top]
    if other > 0:
        out.append({"商品名": "その他", "売上額": other})
    return pd.DataFrame(out)


def _build_category1_product_pie_df(
    rows: list[dict],
    *,
    needles: tuple[str, ...],
    top_n: int = 10,
    exclude_set_products: bool = True,
) -> pd.DataFrame:
    """部門1（sales_category）で絞り込み、商品名別 Top N（残りは「その他」）。"""
    totals: dict[str, float] = defaultdict(float)
    for r in rows:
        cat1 = _sales_category1_label(r)
        if cat1 == "（未設定）":
            continue
        if not _record_matches_category2_group(cat1, needles):
            continue
        product_name = str(r.get("product_name") or "（未設定）")
        if exclude_set_products and _is_set_product_name(product_name):
            continue
        totals[product_name] += r["amount"]
    if not totals:
        return pd.DataFrame(columns=["商品名", "売上額"])
    ranked = sorted(totals.items(), key=lambda x: x[1], reverse=True)
    top = ranked[:top_n]
    other = sum(amt for _, amt in ranked[top_n:])
    out = [{"商品名": name, "売上額": amt} for name, amt in top]
    if other > 0:
        out.append({"商品名": "その他", "売上額": other})
    return pd.DataFrame(out)


def _render_category2_quick_pie_section(rows: list[dict], *, period_label: str) -> None:
    """豚肉・弁当・惣菜・揚げ物の売れ筋円グラフ（ステーキの下）。"""
    st.markdown(f"**カテゴリ別 売れ筋構成（{period_label}）**")
    st.caption(
        f"豚肉・弁当・揚げ物は `{SUPABASE_TABLE_SALES_PRODUCTS}.{SUPABASE_SALES_PRODUCTS_CATEGORY2_COLUMN}` で絞り"
        f"商品別 Top {_SALES_DASHBOARD_CATEGORY2_QUICK_PIE_TOP_N}、"
        f"惣菜のみ `{SUPABASE_TABLE_SALES_PRODUCTS}.{SUPABASE_SALES_PRODUCTS_CATEGORY_COLUMN}` が「惣菜」の商品を"
        f"商品名別 Top {_SALES_DASHBOARD_CATEGORY2_QUICK_PIE_TOP_N} で表示します。"
    )
    pie_cols = st.columns(2)
    for i, (group_label, needles, match_by) in enumerate(_SALES_DASHBOARD_CATEGORY2_QUICK_PIE_GROUPS):
        with pie_cols[i % 2]:
            st.markdown(f"**{group_label}**")
            if match_by == "category1":
                pie_df = _build_category1_product_pie_df(
                    rows,
                    needles=needles,
                    top_n=_SALES_DASHBOARD_CATEGORY2_QUICK_PIE_TOP_N,
                )
            else:
                pie_df = _build_category2_top_product_pie_df(
                    rows,
                    needles=needles,
                    match_by=match_by,
                    top_n=_SALES_DASHBOARD_CATEGORY2_QUICK_PIE_TOP_N,
                    exclude_set_products=(match_by == "category2"),
                )
            if pie_df.empty:
                st.caption("該当データがありません。")
            else:
                total = pie_df["売上額"].sum()
                st.caption(f"売上合計 ¥{total:,.0f}")
                _render_sales_product_pie_chart(pie_df)


def _inject_sales_dashboard_styles() -> None:
    """売上ダッシュボードの画面表示・PDF印刷用スタイル。"""
    st.markdown(
        """
        <style>
        .sales-dashboard-print-btn {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            width: 100%;
            padding: 0.45rem 1rem;
            font-size: 0.875rem;
            font-weight: 600;
            color: #fff;
            background: linear-gradient(135deg, #0284C7, #38BDF8);
            border: none;
            border-radius: 0.5rem;
            cursor: pointer;
            box-shadow: 0 1px 3px rgba(2, 132, 199, 0.35);
        }
        .sales-dashboard-print-btn:hover {
            background: linear-gradient(135deg, #0369A1, #0284C7);
        }
        .sales-dashboard-print-control {
            max-width: 240px;
            min-height: 52px;
            margin-bottom: 0.25rem;
        }
        .sales-dashboard-print-control iframe {
            border: none !important;
            min-height: 52px !important;
            height: 52px !important;
        }
        div:has(> iframe.sales-dashboard-print-frame),
        div:has(iframe[title="streamlit.components.v1.html"]) {
            min-height: 52px !important;
        }
        .sales-dashboard-print-header {
            border-bottom: 2px solid #BAE6FD;
            margin-bottom: 0.75rem;
            padding-bottom: 0.5rem;
        }
        .print-only { display: none !important; }
        div[data-testid="stMetric"] {
            background: #F0F9FF;
            border: 1px solid #BAE6FD;
            border-radius: 0.5rem;
            padding: 0.65rem 0.85rem;
        }
        .sales-dashboard-kpi-amount-box {
            background: #F0F9FF;
            border: 1px solid #BAE6FD;
            border-radius: 0.5rem;
            padding: 1rem 1.25rem;
            min-height: 5.5rem;
            display: flex;
            flex-direction: column;
            justify-content: center;
        }
        .sales-dashboard-kpi-label {
            color: #64748B;
            font-size: 0.875rem;
            font-weight: 500;
            margin-bottom: 0.35rem;
        }
        .sales-dashboard-kpi-value {
            color: #0C4A6E;
            font-size: 2.25rem;
            font-weight: 700;
            line-height: 1.2;
            word-break: break-all;
        }
        .sales-dashboard-section-title {
            margin-top: 0.25rem;
            margin-bottom: 0.5rem;
        }
        .sales-dashboard-section-heading {
            color: #0C4A6E;
            font-size: 1.05rem;
            font-weight: 700;
            margin: 0 0 0.35rem 0;
            padding-left: 0.55rem;
            border-left: 4px solid #38BDF8;
        }
        .sales-dashboard-section-heading-sub {
            color: #64748B;
            font-size: 0.78rem;
            margin: 0 0 0.75rem 0;
        }
        div[data-testid="stVerticalBlockBorderWrapper"] {
            background: linear-gradient(180deg, #FFFFFF 0%, #F8FCFF 100%);
            border-color: #BAE6FD !important;
            border-radius: 0.65rem;
            padding: 0.35rem 0.15rem;
        }
        @media print {
            @page { size: A4 portrait; margin: 8mm; }
            .print-only { display: block !important; }
            header[data-testid="stHeader"],
            [data-testid="stSidebar"],
            [data-testid="stToolbar"],
            [data-testid="stStatusWidget"],
            footer,
            .sales-dashboard-no-print,
            .sales-dashboard-print-control,
            section.main iframe {
                display: none !important;
            }
            .st-key-sales_dashboard_reload,
            .st-key-sales_dashboard_period_mode,
            .st-key-sales_dashboard_year_pick,
            .st-key-sales_dashboard_month_pick,
            .st-key-sales_dashboard_day_range {
                display: none !important;
            }
            [data-testid="stAlert"] { display: none !important; }
            section.main {
                -webkit-print-color-adjust: exact;
                print-color-adjust: exact;
            }
            section.main .block-container {
                max-width: 100% !important;
                padding: 0 !important;
            }
            div[data-testid="stVerticalBlockBorderWrapper"] {
                break-inside: avoid;
                page-break-inside: avoid;
                margin-bottom: 6px;
            }
            [data-testid="column"] {
                break-inside: avoid;
                page-break-inside: avoid;
            }
            section.main [data-testid="stVerticalBlock"] {
                gap: 0.35rem !important;
            }
            h1 {
                font-size: 14pt !important;
                margin: 0 0 4px !important;
                padding: 0 !important;
            }
            h2 {
                font-size: 12pt !important;
                margin: 8px 0 4px !important;
                padding: 0 !important;
            }
            h3, h4, h5 {
                font-size: 10pt !important;
                margin: 4px 0 2px !important;
            }
            hr {
                border-color: #CBD5E1;
                margin: 6px 0 !important;
            }
            p, [data-testid="stCaptionContainer"], label {
                font-size: 8pt !important;
                margin: 0 !important;
            }
            div[data-testid="stMetric"] {
                background: #fff !important;
                border: 1px solid #CBD5E1 !important;
                box-shadow: none !important;
                padding: 0.35rem 0.5rem !important;
            }
            div[data-testid="stMetricValue"] {
                font-size: 11pt !important;
            }
            div[data-testid="stMetricLabel"] {
                font-size: 8pt !important;
            }
            .sales-dashboard-kpi-amount-box {
                background: #fff !important;
                border: 1px solid #CBD5E1 !important;
                padding: 0.35rem 0.5rem !important;
                min-height: 0 !important;
            }
            .sales-dashboard-kpi-label {
                font-size: 8pt !important;
                margin-bottom: 0 !important;
            }
            .sales-dashboard-kpi-value {
                font-size: 14pt !important;
            }
            .sales-dashboard-print-header {
                margin-bottom: 0.35rem !important;
                padding-bottom: 0.15rem !important;
            }
            .sales-dashboard-print-header h2 {
                font-size: 12pt !important;
            }
            .sales-dashboard-print-header p {
                font-size: 8pt !important;
            }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def _render_sales_dashboard_print_bar(*, period_label: str) -> None:
    """PDF印刷ボタンと印刷時のみ表示するヘッダー。"""
    generated = datetime.now().strftime("%Y/%m/%d %H:%M")
    st.markdown(
        f"""
        <div class="sales-dashboard-print-header print-only">
            <h2 style="margin:0;font-size:1.25rem;">売上ダッシュボード</h2>
            <p style="margin:0.35rem 0 0;font-size:0.95rem;">対象期間: {period_label}</p>
            <p style="margin:0.2rem 0 0;font-size:0.8rem;color:#64748B;">出力日時: {generated}</p>
        </div>
        """,
        unsafe_allow_html=True,
    )
    pc, _ = st.columns([2, 5])
    with pc:
        st.markdown('<div class="sales-dashboard-print-control">', unsafe_allow_html=True)
        components.html(
            """
            <!DOCTYPE html>
            <html>
            <head>
            <style>
              html, body {
                margin: 0;
                padding: 0;
                height: 52px;
                overflow: hidden;
              }
              button {
                width: 100%;
                min-height: 44px;
                padding: 0.5rem 1rem;
                font-size: 0.875rem;
                font-weight: 600;
                color: #fff;
                background: linear-gradient(135deg, #0284C7, #38BDF8);
                border: none;
                border-radius: 0.5rem;
                cursor: pointer;
                box-shadow: 0 1px 3px rgba(2, 132, 199, 0.35);
                box-sizing: border-box;
                white-space: nowrap;
              }
              button:hover {
                background: linear-gradient(135deg, #0369A1, #0284C7);
              }
            </style>
            </head>
            <body>
            <button type="button" id="salesDashboardPrintBtn">🖨️ PDF印刷</button>
            <script>
            (function () {
                const btn = document.getElementById("salesDashboardPrintBtn");
                if (!btn || btn.dataset.bound) return;
                btn.dataset.bound = "1";
                btn.addEventListener("click", function () {
                    const target = window.top || window.parent || window;
                    target.focus();
                    setTimeout(function () { target.print(); }, 350);
                });
            })();
            </script>
            </body>
            </html>
            """,
            height=52,
            scrolling=False,
        )
        st.markdown("</div>", unsafe_allow_html=True)
    st.markdown(
        '<p class="sales-dashboard-no-print" style="color:#64748B;font-size:0.8rem;margin:0.35rem 0 0;">'
        "何度でも印刷できます。A4縦・複数ページでも見やすいレイアウトで出力します（プレビューで確認してください）。"
        "</p>",
        unsafe_allow_html=True,
    )


def render_sales_dashboard_page() -> None:
    _inject_sales_dashboard_styles()
    st.title("📈 売上ダッシュボード")
    st.markdown(
        '<p class="sales-dashboard-no-print" style="color:#64748B;font-size:0.875rem;margin-top:0;">'
        f"Supabase の `{SUPABASE_TABLE_SALES}` を集計します。"
        "カレンダーで年別・月別・日別の期間を指定できます（データは直近36ヶ月分を保持）。"
        "</p>",
        unsafe_allow_html=True,
    )

    client = get_supabase_client_for_writes()
    if not client:
        st.warning(
            "Supabase に接続できません。ログインするか、`.env` の `SUPABASE_URL` とキーを確認してください。"
        )
        return

    c_reload, _ = st.columns([1, 4])
    with c_reload:
        if st.button("データを再読込", key="sales_dashboard_reload"):
            st.session_state.pop("dashboard_sales", None)
            st.session_state.pop("dashboard_sales_db_count", None)
            st.session_state.pop("dashboard_sales_fetch_error", None)
            st.rerun()

    if "dashboard_sales" not in st.session_state:
        with st.spinner("売上データを取得しています…"):
            try:
                st.session_state.dashboard_sales = fetch_sales_for_dashboard(client)
                st.session_state.dashboard_sales_db_count = count_sales_for_dashboard(client)
                st.session_state.dashboard_sales_fetch_error = None
            except Exception as e:
                st.session_state.dashboard_sales = []
                st.session_state.dashboard_sales_db_count = None
                st.session_state.dashboard_sales_fetch_error = str(e)

    err = st.session_state.get("dashboard_sales_fetch_error")
    if err:
        st.error(f"データ取得に失敗しました: {err}")
        if "row-level security" in err.lower() or "42501" in err:
            st.info(
                f"Supabase の SQL で `authenticated` ロール向けの **SELECT** ポリシーを "
                f'`public.{SUPABASE_TABLE_SALES}` に追加してください。'
            )
        return

    records = sales_rows_to_analytics(st.session_state.get("dashboard_sales") or [])
    if not records:
        st.info("集計できる売上データがありません。「CSV取り込み」からデータを登録してください。")
        return

    db_count = st.session_state.get("dashboard_sales_db_count")
    if db_count is not None and db_count > len(records):
        st.warning(
            f"Supabase には直近36ヶ月で **{db_count:,} 件** ありますが、"
            f"集計に使えたのは **{len(records):,} 件** だけです。"
            "日付の形式や空の金額行を確認するか、「データを再読込」を押してください。"
        )
    elif db_count is not None and db_count != len(st.session_state.get("dashboard_sales") or []):
        st.caption(
            f"DB: {db_count:,} 件を取得（うち集計対象 {len(records):,} 件。"
            "日付・金額が無効な行は集計から除外されます）"
        )

    today = datetime.now().date()
    month_default = today.replace(day=1)
    range_default = (month_default, today)

    with st.container(border=True):
        st.subheader("期間の指定")
        period_mode = st.radio(
            "集計単位",
            ["年別", "月別", "日別"],
            horizontal=True,
            key="sales_dashboard_period_mode",
        )

        month_pick: date | None = None
        year_pick: int | None = None
        range_start: date | None = None
        range_end: date | None = None

        if period_mode == "年別":
            picked_year = st.date_input(
                "対象年",
                value=date(today.year, 1, 1),
                min_value=date(2020, 1, 1),
                max_value=today,
                key="sales_dashboard_year_pick",
            )
            year_pick = picked_year.year if isinstance(picked_year, date) else today.year
        elif period_mode == "月別":
            picked = st.date_input(
                "対象月",
                value=month_default,
                min_value=date(2020, 1, 1),
                max_value=today,
                key="sales_dashboard_month_pick",
            )
            month_pick = picked.replace(day=1) if isinstance(picked, date) else month_default
        else:
            picked_range = st.date_input(
                "対象期間",
                value=range_default,
                min_value=date(2020, 1, 1),
                max_value=today,
                key="sales_dashboard_day_range",
            )
            try:
                range_start, range_end = _parse_date_input_range(picked_range)
            except TypeError:
                range_start, range_end = range_default

    period_rows, comparison_rows, period_label, comparison_label = dashboard_filter_records(
        records,
        period_mode,
        month_pick=month_pick,
        year_pick=year_pick,
        range_start=range_start,
        range_end=range_end,
    )
    yoy_rows, yoy_comparison_rows, yoy_period_label, yoy_comparison_label = (
        sales_dashboard_yoy_comparison(
            records,
            period_mode,
            month_pick=month_pick,
            year_pick=year_pick,
            range_start=range_start,
            range_end=range_end,
        )
    )

    period_total = sum(r["amount"] for r in period_rows)
    if period_mode in ("月別", "日別"):
        kpi_comparison_rows = yoy_comparison_rows
        kpi_comparison_label = yoy_comparison_label
    else:
        kpi_comparison_rows = comparison_rows
        kpi_comparison_label = comparison_label
    prev_total = sum(r["amount"] for r in kpi_comparison_rows)

    compare_pct: float | None = None
    if prev_total > 0:
        compare_pct = (period_total - prev_total) / prev_total * 100.0
        compare_delta = f"{kpi_comparison_label} ¥{prev_total:,.0f}"
    elif period_total > 0:
        compare_delta = f"{kpi_comparison_label}のデータなし"
    else:
        compare_delta = None

    if period_mode == "年別":
        amount_label = "年間売上額"
        compare_metric_label = "前年比"
    elif period_mode == "月別":
        amount_label = "売上額"
        compare_metric_label = "前年同月比"
    else:
        amount_label = "期間売上額"
        compare_metric_label = "前年同期比"

    _render_sales_dashboard_print_bar(period_label=period_label)

    with st.container(border=True):
        st.subheader(f"KPI（{period_label}）")
        k1, k2 = st.columns([3, 2])
        with k1:
            st.markdown(
                f"""
                <div class="sales-dashboard-kpi-amount-box">
                    <div class="sales-dashboard-kpi-label">{amount_label}</div>
                    <div class="sales-dashboard-kpi-value">¥{period_total:,.0f}</div>
                </div>
                """,
                unsafe_allow_html=True,
            )
        with k2:
            st.metric(
                compare_metric_label,
                f"{compare_pct:+.1f}%" if compare_pct is not None else "—",
                delta=compare_delta,
            )

    month_keys = sorted({r["year_month"] for r in records})
    monthly_df, trend_title = _build_sales_monthly_trend_df(
        records,
        year_pick=year_pick,
        month_keys=month_keys,
        period_mode=period_mode,
    )
    weekday_period_df = _build_sales_period_weekday_df(period_rows)

    st.subheader("売上トレンド")
    trend_l, trend_r = st.columns(2, gap="medium")
    with trend_l:
        with st.container(border=True):
            st.markdown(
                f'<p class="sales-dashboard-section-heading">月別売上</p>'
                f'<p class="sales-dashboard-section-heading-sub">{trend_title}</p>',
                unsafe_allow_html=True,
            )
            _render_sales_monthly_trend_chart(monthly_df)
    with trend_r:
        with st.container(border=True):
            st.markdown(
                f'<p class="sales-dashboard-section-heading">曜日別売上</p>'
                f'<p class="sales-dashboard-section-heading-sub">{period_label}</p>',
                unsafe_allow_html=True,
            )
            _render_sales_weekday_chart(weekday_period_df)

    dept_totals: dict[str, float] = defaultdict(float)
    for r in period_rows:
        dept_totals[_sales_category1_label(r)] += r["amount"]
    ranking = sorted(dept_totals.items(), key=lambda x: x[1], reverse=True)[:10]
    rank_df = pd.DataFrame(
        {"部門": [x[0] for x in ranking], "売上額": [x[1] for x in ranking]}
    )

    st.subheader("部門構成")
    with st.container(border=True):
        st.markdown(
            f'<p class="sales-dashboard-section-heading">部門ランキング（部門1）</p>'
            f'<p class="sales-dashboard-section-heading-sub">{period_label}・構成比順</p>',
            unsafe_allow_html=True,
        )
        dept_l, dept_r = st.columns([3, 2], gap="medium")
        with dept_l:
            if rank_df.empty:
                st.caption("対象期間のデータがありません。")
            else:
                _render_sales_dept_pie_chart(rank_df)
        with dept_r:
            if not rank_df.empty:
                rank_display = rank_df.copy()
                total_dept = float(rank_display["売上額"].sum())
                rank_display["構成比"] = rank_display["売上額"] / total_dept * 100.0
                rank_display.insert(0, "順位", range(1, len(rank_display) + 1))
                st.dataframe(
                    rank_display,
                    use_container_width=True,
                    hide_index=True,
                    column_config={
                        "順位": st.column_config.NumberColumn(format="%d"),
                        "売上額": st.column_config.NumberColumn(format="¥%,.0f"),
                        "構成比": st.column_config.NumberColumn(format="%.1f%%"),
                    },
                )

    st.divider()
    st.subheader(f"カテゴリ2別・売れ筋商品（{period_label}）")
    for group_label, needles, match_by in _SALES_DASHBOARD_CATEGORY2_GROUPS:
        top_n = _category2_top_n_for_group(group_label)
        st.markdown(f"**{group_label}**")
        pie_df = _build_category2_top_product_pie_df(
            period_rows,
            needles=needles,
            match_by=match_by,
            top_n=top_n,
            exclude_set_products=(match_by == "category2"),
            category2_exact=(group_label == "焼肉"),
        )
        if pie_df.empty:
            st.caption("該当データがありません。")
        else:
            total = pie_df["売上額"].sum()
            st.caption(f"売上合計 ¥{total:,.0f}")
            _render_sales_category2_product_rank_chart(
                pie_df,
                two_columns=(group_label in _SALES_DASHBOARD_CATEGORY2_TWO_COLUMN_GROUPS),
            )
        st.markdown("")
        if group_label == "ステーキ":
            _render_category2_quick_pie_section(period_rows, period_label=period_label)
            st.markdown("")

    st.divider()
    st.subheader("前年同月・同期比較")
    bot_l, bot_r = st.columns(2, gap="medium")

    amount_trend_df, amount_trend_title = _build_sales_yoy_amount_trend_df(
        yoy_rows,
        yoy_comparison_rows,
        yoy_period_label,
        yoy_comparison_label,
        period_mode=period_mode,
        year_pick=year_pick,
    )
    dept_yoy_df = _build_sales_yoy_dept_amount_df(
        yoy_rows,
        yoy_comparison_rows,
        yoy_period_label,
        yoy_comparison_label,
    )

    with bot_l:
        with st.container(border=True):
            st.markdown(f"**{amount_trend_title}**")
            trend_x = "月" if period_mode == "年別" else "日"
            _render_sales_yoy_line_chart(
                amount_trend_df,
                x_col=trend_x,
                period_label=yoy_period_label,
                yoy_label=yoy_comparison_label,
            )

    with bot_r:
        with st.container(border=True):
            st.markdown(
                f"**部門別売上（{yoy_period_label} vs {yoy_comparison_label}）**"
            )
            _render_sales_yoy_grouped_bar(dept_yoy_df, x_col="部門")

    yoy_total = sum(r["amount"] for r in yoy_rows)
    yoy_prev_total = sum(r["amount"] for r in yoy_comparison_rows)
    yoy_count = len(yoy_rows)
    yoy_prev_count = len(yoy_comparison_rows)
    insight_l, insight_m, insight_r = st.columns(3)
    with insight_l:
        cur_avg = (yoy_total / yoy_count) if yoy_count else 0.0
        prev_avg = (yoy_prev_total / yoy_prev_count) if yoy_prev_count else 0.0
        avg_pct: float | None = None
        if prev_avg > 0:
            avg_pct = (cur_avg - prev_avg) / prev_avg * 100.0
        st.metric(
            "1件あたり売上（前年同月比）",
            f"¥{cur_avg:,.0f}",
            delta=f"{avg_pct:+.1f}%" if avg_pct is not None else None,
        )
    with insight_m:
        cnt_pct: float | None = None
        if yoy_prev_count > 0:
            cnt_pct = (yoy_count - yoy_prev_count) / yoy_prev_count * 100.0
        st.metric(
            "売上件数（前年同月比）",
            f"{yoy_count:,} 件",
            delta=f"{cnt_pct:+.1f}%" if cnt_pct is not None else None,
        )
    with insight_r:
        if records_have_quantity(yoy_rows) or records_have_quantity(yoy_comparison_rows):
            q_cur = sum(_record_quantity(r) or 0.0 for r in yoy_rows)
            q_prev = sum(_record_quantity(r) or 0.0 for r in yoy_comparison_rows)
            qty_pct: float | None = None
            if q_prev > 0:
                qty_pct = (q_cur - q_prev) / q_prev * 100.0
            st.metric(
                "販売数量（前年同月比）",
                _format_quantity_display(q_cur),
                delta=f"{qty_pct:+.1f}%" if qty_pct is not None else None,
            )
        else:
            st.metric("販売数量", "データなし", help="CSV の商品数列で取込むと表示されます。")

    db_total = st.session_state.get("dashboard_sales_db_count")
    total_note = f"{len(records):,} 件"
    if db_total is not None:
        total_note = f"DB {db_total:,} 件 / 集計 {len(records):,} 件"
    st.markdown(
        '<p class="sales-dashboard-no-print" style="color:#64748B;font-size:0.8rem;margin-top:1rem;">'
        f"表示中: {period_label}（{len(period_rows):,} 件）｜"
        f"データ保持範囲: {month_keys[0] if month_keys else '—'} 〜 {month_keys[-1] if month_keys else '—'}（{total_note}）"
        "</p>",
        unsafe_allow_html=True,
    )


ANALYST_EXAMPLE_QUESTIONS = (
    "直近3ヶ月の売上と仕入の傾向を比較してください",
    "曜日別の売上を教えてください",
    "部門別の販売数量を教えてください",
    "取引先別の仕入額ランキングを教えてください",
    "牛肉カテゴリの売上上位20商品を教えてください",
    "仕入数量が多い商品トップ10を教えてください",
)

_PURCHASE_QUESTION_KEYWORDS = (
    "仕入",
    "仕入れ",
    "購入",
    "purchases",
    "purchase",
    "取引先",
    "伝票",
    "仕入額",
    "仕入高",
    "単価",
    "仕入数量",
)
_SALES_QUESTION_KEYWORDS = (
    "売上",
    "sales",
    "販売",
    "部門",
    "カテゴリ",
    "売上高",
    "売上額",
    "販売数量",
)

_PURCHASE_QUESTION_SKIP_TOKENS = frozenset(
    {
        "上位",
        "商品",
        "仕入",
        "購入",
        "分析",
        "予測",
        "教えて",
        "ください",
        "取引先",
        "トップ",
        "ランキング",
        "一覧",
        "表示",
        "出して",
        "ほしい",
        "欲しい",
        "について",
        "を教え",
    }
)


def question_asks_purchases(question: str) -> bool:
    q = question.strip()
    q_lower = q.lower()
    return any(k in q or k.lower() in q_lower for k in _PURCHASE_QUESTION_KEYWORDS)


def question_asks_sales(question: str) -> bool:
    q = question.strip()
    q_lower = q.lower()
    return any(k in q or k.lower() in q_lower for k in _SALES_QUESTION_KEYWORDS)

_SALES_QUANTITY_QUESTION_KEYWORDS = (
    "販売数量",
    "販売数",
    "売上数量",
    "商品数",
    "数量",
    "quantity",
    "個数",
    "販売個数",
)

SALES_ANALYST_TOP_PRODUCTS_GLOBAL = 30
SALES_ANALYST_TOP_PRODUCTS_PER_DEPT = 30
SALES_ANALYST_QUESTION_TOP_N_DEFAULT = 20
SALES_ANALYST_QUESTION_TOP_N_MAX = 100

_SALES_QUESTION_SKIP_TOKENS = frozenset(
    {
        "上位",
        "商品",
        "売上",
        "分析",
        "予測",
        "教えて",
        "ください",
        "カテゴリ",
        "部門",
        "トップ",
        "ランキング",
        "一覧",
        "表示",
        "出して",
        "ほしい",
        "欲しい",
        "について",
        "を教え",
        "曜日",
        "曜日別",
        "月曜",
        "火曜",
        "水曜",
        "木曜",
        "金曜",
        "土曜",
        "日曜",
        "月曜日",
        "火曜日",
        "水曜日",
        "木曜日",
        "金曜日",
        "土曜日",
        "日曜日",
    }
)


def _record_quantity(r: dict) -> float | None:
    return _coerce_quantity_value(r.get("quantity"))


def records_have_quantity(records: list[dict]) -> bool:
    return any(_record_quantity(r) is not None for r in records)


def question_asks_quantity(question: str) -> bool:
    q = question.strip()
    q_lower = q.lower()
    return any(k in q or k.lower() in q_lower for k in _SALES_QUANTITY_QUESTION_KEYWORDS)


def aggregate_sales_by_department_and_product(
    records: list[dict],
) -> dict[str, dict[str, float]]:
    """部門 → 商品名 → 売上合計。"""
    by_dept: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for r in records:
        by_dept[r["kategory"]][r["product_name"]] += r["amount"]
    return {k: dict(v) for k, v in by_dept.items()}


def aggregate_quantity_by_department_and_product(
    records: list[dict],
) -> dict[str, dict[str, float]]:
    """部門 → 商品名 → 販売数量合計（quantity がある行のみ）。"""
    by_dept: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for r in records:
        q = _record_quantity(r)
        if q is not None:
            by_dept[r["kategory"]][r["product_name"]] += q
    return {k: dict(v) for k, v in by_dept.items()}


def aggregate_quantity_by_department(records: list[dict]) -> dict[str, float]:
    dept: dict[str, float] = defaultdict(float)
    for r in records:
        q = _record_quantity(r)
        if q is not None:
            dept[r["kategory"]] += q
    return dict(dept)


def aggregate_quantity_by_product(records: list[dict]) -> dict[str, float]:
    prod: dict[str, float] = defaultdict(float)
    for r in records:
        q = _record_quantity(r)
        if q is not None:
            prod[r["product_name"]] += q
    return dict(prod)


def question_asks_weekday(question: str) -> bool:
    q = question or ""
    if "曜日" in q or "weekday" in q.lower():
        return True
    return any(wd in q for wd in _WEEKDAY_NAMES_JA) or any(
        wd.replace("曜日", "曜") in q for wd in _WEEKDAY_NAMES_JA
    )


def match_weekdays_in_question(question: str) -> list[str]:
    """質問に含まれる曜日名（月曜日…）を返す。"""
    q = question or ""
    hits: list[str] = []
    for wd in _WEEKDAY_NAMES_JA:
        if wd in q or wd.replace("曜日", "曜") in q:
            hits.append(wd)
    return list(dict.fromkeys(hits))


def aggregate_sales_by_weekday(records: list[dict]) -> dict[str, float]:
    out: dict[str, float] = defaultdict(float)
    for r in records:
        wd = str(r.get("weekday_name") or "").strip()
        if wd and wd != "（未設定）":
            out[wd] += r["amount"]
    return dict(out)


def aggregate_quantity_by_weekday(records: list[dict]) -> dict[str, float]:
    out: dict[str, float] = defaultdict(float)
    for r in records:
        wd = str(r.get("weekday_name") or "").strip()
        q = _record_quantity(r)
        if wd and wd != "（未設定）" and q is not None:
            out[wd] += q
    return dict(out)


def aggregate_count_by_weekday(records: list[dict]) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for r in records:
        wd = str(r.get("weekday_name") or "").strip()
        if wd and wd != "（未設定）":
            out[wd] += 1
    return dict(out)


def format_weekday_sales_summary_lines(records: list[dict]) -> list[str]:
    """曜日別の売上・件数・数量サマリー行を返す。"""
    if not records:
        return ["（曜日別集計対象の明細がありません）"]
    by_wd = aggregate_sales_by_weekday(records)
    if not by_wd:
        return ["（weekday_name / sales_date から曜日を特定できませんでした）"]
    by_cnt = aggregate_count_by_weekday(records)
    by_qty = aggregate_quantity_by_weekday(records)
    total = sum(r["amount"] for r in records)
    has_qty = records_have_quantity(records)
    lines = [
        f"## 曜日別売上（`{SUPABASE_TABLE_SALES}.{SUPABASE_SALES_WEEKDAY_COLUMN}` / `{SUPABASE_SALES_DATE_COLUMN}` から集計）"
    ]
    for wd in _WEEKDAY_NAMES_JA:
        if wd not in by_wd:
            continue
        amt = by_wd[wd]
        share = (amt / total * 100.0) if total else 0.0
        qty_part = ""
        if has_qty and wd in by_qty:
            qty_part = f" / 販売数量 {_format_quantity_display(by_qty[wd])}"
        lines.append(
            f"- {wd}: ¥{amt:,.0f}（構成比 {share:.1f}% / {by_cnt.get(wd, 0):,} 件{qty_part}）"
        )
    return lines


def parse_top_n_from_question(question: str, *, default: int = SALES_ANALYST_QUESTION_TOP_N_DEFAULT) -> int:
    for pat in (
        r"上位\s*(\d+)",
        r"トップ\s*(\d+)",
        r"top\s*(\d+)",
        r"(\d+)\s*位",
        r"(\d+)\s*商品",
        r"(\d+)\s*品",
    ):
        m = re.search(pat, question, re.IGNORECASE)
        if m:
            return min(max(int(m.group(1)), 1), SALES_ANALYST_QUESTION_TOP_N_MAX)
    return default


def match_departments_in_question(question: str, kategories: list[str]) -> list[str]:
    """質問文と Supabase 上の sales_kategory の部分一致。"""
    q = question.strip()
    q_compact = re.sub(r"[\s　]+", "", q)
    hits: list[str] = []
    for kat in sorted(kategories, key=len, reverse=True):
        kat_compact = re.sub(r"[\s　]+", "", kat)
        if kat in q or kat_compact in q_compact:
            hits.append(kat)
            continue
        core = (
            kat_compact.replace("カテゴリ", "")
            .replace("部門", "")
            .replace("分類", "")
        )
        if len(core) >= 2 and core in q_compact:
            hits.append(kat)
    for m in re.finditer(r"[\u4e00-\u9fff]{2,}", q):
        token = m.group()
        if token in _SALES_QUESTION_SKIP_TOKENS:
            continue
        for kat in kategories:
            if kat in hits:
                continue
            if token in re.sub(r"[\s　]+", "", kat):
                hits.append(kat)
    return list(dict.fromkeys(hits))


def format_department_product_ranking(
    dept_name: str,
    product_amounts: dict[str, float],
    *,
    top_n: int,
    product_quantities: dict[str, float] | None = None,
    sort_by: str = "amount",
    header_note: str = "",
    amount_word: str = "売上",
) -> str:
    total_dept = sum(product_amounts.values())
    distinct = len(product_amounts)
    qty_map = product_quantities or {}
    total_dept_qty = sum(qty_map.values()) if qty_map else 0.0

    if sort_by == "quantity" and qty_map:
        rank = sorted(qty_map.items(), key=lambda x: x[1], reverse=True)
        rank_header = f"数量の降順（上位 {min(top_n, len(rank))} 件）"
    else:
        rank = sorted(product_amounts.items(), key=lambda x: x[1], reverse=True)
        rank_header = f"{amount_word}額の降順（上位 {min(top_n, len(rank))} 件）"

    show_n = min(top_n, len(rank))
    lines = [
        f"### 部門「{dept_name}」{header_note}".rstrip(),
        f"- DB上の distinct 商品数（品目数）: {distinct:,}",
        f"- 部門{amount_word}合計: ¥{total_dept:,.0f}",
    ]
    if qty_map:
        lines.append(f"- 部門数量合計: {_format_quantity_display(total_dept_qty)}")
    lines.append(f"- 以下は{rank_header}")
    for i, (name, primary) in enumerate(rank[:show_n], start=1):
        if sort_by == "quantity" and qty_map:
            qty = primary
            share = (qty / total_dept_qty * 100.0) if total_dept_qty else 0.0
            amt = product_amounts.get(name, 0.0)
            lines.append(
                f"{i}. {name}: 数量 {_format_quantity_display(qty)}（部門内 {share:.1f}%）"
                f" / {amount_word} ¥{amt:,.0f}"
            )
        else:
            amt = primary
            share = (amt / total_dept * 100.0) if total_dept else 0.0
            if qty_map and name in qty_map:
                lines.append(
                    f"{i}. {name}: ¥{amt:,.0f}（部門内 {share:.1f}%）"
                    f" / 数量 {_format_quantity_display(qty_map[name])}"
                )
            else:
                lines.append(f"{i}. {name}: ¥{amt:,.0f}（部門内構成比 {share:.1f}%）")
    if distinct > show_n:
        lines.append(f"（{show_n + 1} 位以降も DB には {distinct - show_n} 品目あり）")
    return "\n".join(lines)


def _format_quantity_summary_sections(
    records: list[dict],
    *,
    top_n: int = 20,
    domain: str = "売上",
    column_name: str | None = None,
    group_key: str = "kategory",
    group_label: str = "部門",
    empty_hint: str = "",
) -> list[str]:
    """数量の全体・月別・グループ別・商品別集計テキスト。"""
    col = column_name or SUPABASE_SALES_QUANTITY_COLUMN
    hint = empty_hint or "データ取込後に数量分析が可能です。"
    if not records_have_quantity(records):
        return [
            f"## {domain}の数量（quantity）",
            f"- `{col}` に値がある明細がありません。",
            f"- {hint}",
        ]

    qty_rows = sum(1 for r in records if _record_quantity(r) is not None)
    total_qty = sum(_record_quantity(r) or 0.0 for r in records)
    lines = [
        f"## {domain}の数量（quantity）",
        f"- 数量あり明細: {qty_rows:,} / {len(records):,} 件",
        f"- 数量合計: {_format_quantity_display(total_qty)}",
    ]

    monthly_qty: dict[str, float] = defaultdict(float)
    for r in records:
        q = _record_quantity(r)
        if q is not None:
            monthly_qty[r["year_month"]] += q
    lines.append("")
    lines.append("### 月別数量")
    for ym in sorted(monthly_qty.keys()):
        lines.append(f"- {ym}: {_format_quantity_display(monthly_qty[ym])}")

    group_qty: dict[str, float] = defaultdict(float)
    for r in records:
        q = _record_quantity(r)
        if q is not None:
            group_qty[str(r.get(group_key) or "（未設定）")] += q
    lines.append("")
    lines.append(f"### {group_label}別数量（全{group_label}）")
    for name, qty in sorted(group_qty.items(), key=lambda x: x[1], reverse=True):
        share = (qty / total_qty * 100.0) if total_qty else 0.0
        lines.append(f"- {name}: {_format_quantity_display(qty)}（構成比 {share:.1f}%）")

    prod_qty = aggregate_quantity_by_product(records)
    lines.append("")
    lines.append(f"### 商品別数量（上位{top_n}）")
    for i, (name, qty) in enumerate(
        sorted(prod_qty.items(), key=lambda x: x[1], reverse=True)[:top_n], start=1
    ):
        share = (qty / total_qty * 100.0) if total_qty else 0.0
        lines.append(f"{i}. {name}: {_format_quantity_display(qty)}（全体 {share:.1f}%）")
    return lines


def build_weekday_focused_sales_context(records: list[dict], question: str) -> str:
    """曜日別売上・数量に関する質問向けの集計表。"""
    if not question_asks_weekday(question):
        return ""

    matched = match_weekdays_in_question(question)
    top_n = parse_top_n_from_question(question)
    lines = [
        "## 質問に対応する Supabase 集計（曜日別）",
        f"曜日列: `{SUPABASE_SALES_WEEKDAY_COLUMN}` / 日付列: `{SUPABASE_SALES_DATE_COLUMN}` / 明細 {len(records):,} 件",
        "",
    ]
    lines.extend(format_weekday_sales_summary_lines(records))

    targets = matched if matched else list(_WEEKDAY_NAMES_JA)
    sort_by = "quantity" if question_asks_quantity(question) and records_have_quantity(records) else "amount"

    shown = 0
    for wd in targets:
        subset = [
            r
            for r in records
            if str(r.get("weekday_name") or "").strip() == wd
        ]
        if not subset:
            continue
        shown += 1
        wd_total = sum(r["amount"] for r in subset)
        lines.append("")
        lines.append(f"### {wd}の部門別売上（¥{wd_total:,.0f} / {len(subset):,} 件）")
        dept_amt: dict[str, float] = defaultdict(float)
        for r in subset:
            dept_amt[r["kategory"]] += r["amount"]
        for name, amt in sorted(dept_amt.items(), key=lambda x: x[1], reverse=True)[:top_n]:
            share = (amt / wd_total * 100.0) if wd_total else 0.0
            lines.append(f"- {name}: ¥{amt:,.0f}（{wd}内 {share:.1f}%）")

        if any(k in question for k in ("商品", "品目", "ランキング", "上位")):
            prod_amt: dict[str, float] = defaultdict(float)
            prod_qty: dict[str, float] = defaultdict(float)
            for r in subset:
                prod_amt[r["product_name"]] += r["amount"]
                q = _record_quantity(r)
                if q is not None:
                    prod_qty[r["product_name"]] += q
            lines.append("")
            lines.append(f"### {wd}の商品別（上位{top_n}）")
            if sort_by == "quantity" and prod_qty:
                ranked = sorted(prod_qty.items(), key=lambda x: x[1], reverse=True)[:top_n]
                total_q = sum(prod_qty.values())
                for i, (name, qty) in enumerate(ranked, start=1):
                    share = (qty / total_q * 100.0) if total_q else 0.0
                    amt = prod_amt.get(name, 0.0)
                    lines.append(
                        f"{i}. {name}: 数量 {_format_quantity_display(qty)}（{wd}内 {share:.1f}%） / 売上 ¥{amt:,.0f}"
                    )
            else:
                ranked = sorted(prod_amt.items(), key=lambda x: x[1], reverse=True)[:top_n]
                for i, (name, amt) in enumerate(ranked, start=1):
                    share = (amt / wd_total * 100.0) if wd_total else 0.0
                    qty_part = ""
                    if name in prod_qty:
                        qty_part = f" / 数量 {_format_quantity_display(prod_qty[name])}"
                    lines.append(
                        f"{i}. {name}: ¥{amt:,.0f}（{wd}内 {share:.1f}%{qty_part}）"
                    )

    if matched and shown == 0:
        lines.append("")
        lines.append(f"（指定曜日 {', '.join(matched)} に一致する明細がありません）")

    return "\n".join(lines)


def build_quantity_focused_sales_context(records: list[dict], question: str) -> str:
    """販売数量に関する質問向けの集計表。"""
    if not question_asks_quantity(question):
        return ""

    top_n = parse_top_n_from_question(question)
    lines = [
        "## 質問に対応する Supabase 集計（販売数量）",
        f"数量列: `{SUPABASE_SALES_QUANTITY_COLUMN}`",
    ]
    lines.extend(_format_quantity_summary_sections(records, top_n=top_n))

    by_qty = aggregate_quantity_by_department_and_product(records)
    if not by_qty:
        return "\n".join(lines)

    kategories = sorted(by_qty.keys())
    matched = match_departments_in_question(question, kategories)
    sort_by = "quantity"
    targets = matched if matched else kategories[:3]
    if not matched and len(kategories) > 3:
        lines.append("")
        lines.append("（部門未指定のため販売数量上位3部門の商品ランキング）")

    by_amt = aggregate_sales_by_department_and_product(records)
    for dept in targets:
        lines.append("")
        lines.append(
            format_department_product_ranking(
                dept,
                by_amt.get(dept, {}),
                top_n=top_n,
                product_quantities=by_qty.get(dept, {}),
                sort_by=sort_by,
                header_note="※販売数量ベース",
            )
        )
    return "\n".join(lines)


def build_question_focused_sales_context(records: list[dict], question: str) -> str:
    """質問に含まれる部門・上位N件などに合わせ、Supabase 明細から再集計した表を付与。"""
    if question_asks_weekday(question):
        return build_weekday_focused_sales_context(records, question)
    if question_asks_quantity(question):
        return build_quantity_focused_sales_context(records, question)

    by_dept = aggregate_sales_by_department_and_product(records)
    if not by_dept:
        return ""

    by_qty = aggregate_quantity_by_department_and_product(records)
    kategories = sorted(by_dept.keys())
    matched = match_departments_in_question(question, kategories)
    top_n = parse_top_n_from_question(question)

    lines = [
        "## 質問に対応する Supabase 集計（sales 明細からアプリが再計算）",
        f"データソース: `{SUPABASE_TABLE_SALES}` / 部門 `{SUPABASE_TABLE_SALES_PRODUCTS}.{SUPABASE_SALES_PRODUCTS_CATEGORY_COLUMN}` / "
        f"商品列 `{SUPABASE_SALES_PRODUCTS_COLUMN}` / 数量列 `{SUPABASE_SALES_QUANTITY_COLUMN}` / "
        f"明細 {len(records):,} 件",
    ]

    if matched:
        for dept in matched:
            lines.append("")
            lines.append(
                format_department_product_ranking(
                    dept,
                    by_dept[dept],
                    top_n=top_n,
                    product_quantities=by_qty.get(dept) or None,
                    header_note="※ユーザー質問に対応",
                )
            )
        return "\n".join(lines)

    if any(k in question for k in ("部門", "カテゴリ", "分類")):
        lines.append("")
        lines.append("### Supabase 上の部門名一覧（sales_products）")
        for kat in kategories:
            n_prod = len(by_dept[kat])
            amt = sum(by_dept[kat].values())
            qty_part = ""
            if by_qty.get(kat):
                qty_part = f" / 販売数量 {_format_quantity_display(sum(by_qty[kat].values()))}"
            lines.append(f"- {kat}: {n_prod:,} 品目 / 売上 ¥{amt:,.0f}{qty_part}")
    return "\n".join(lines) if len(lines) > 2 else ""


def build_sales_analytics_summary(records: list[dict]) -> str:
    """AI 用に Supabase 売上を要約（部門別商品ランキングを含む）。"""
    if not records:
        return "（売上データがありません。CSV取り込み後に再度お試しください。）"

    dates = [r["date"] for r in records]
    min_date, max_date = min(dates), max(dates)
    total = sum(r["amount"] for r in records)
    by_dept = aggregate_sales_by_department_and_product(records)

    monthly: dict[str, float] = defaultdict(float)
    monthly_count: dict[str, int] = defaultdict(int)
    dept: dict[str, float] = defaultdict(float)
    product: dict[str, float] = defaultdict(float)
    for r in records:
        ym = r["year_month"]
        monthly[ym] += r["amount"]
        monthly_count[ym] += 1
        dept[r["kategory"]] += r["amount"]
        product[r["product_name"]] += r["amount"]

    lines = [
        f"データソース: Supabase `{SUPABASE_TABLE_SALES}`（直近36ヶ月・ページング取得済み）",
        f"明細件数: {len(records):,} 件",
        f"期間: {min_date} 〜 {max_date}",
        f"売上合計: ¥{total:,.0f}",
        f"部門（{SUPABASE_TABLE_SALES_PRODUCTS}.{SUPABASE_SALES_PRODUCTS_CATEGORY_COLUMN}）数: {len(by_dept):,}",
    ]
    lines.extend(_format_quantity_summary_sections(records, top_n=SALES_ANALYST_TOP_PRODUCTS_GLOBAL))
    lines.extend([""] + format_weekday_sales_summary_lines(records))
    lines.extend(["", "## 月別売上"])
    month_keys = sorted(monthly.keys())
    for ym in month_keys:
        lines.append(f"- {ym}: ¥{monthly[ym]:,.0f}（{monthly_count[ym]:,} 件）")

    if len(month_keys) >= 6:
        recent = month_keys[-3:]
        prior = month_keys[-6:-3]
        recent_sum = sum(monthly[m] for m in recent)
        prior_sum = sum(monthly[m] for m in prior)
        lines.append("")
        lines.append("## 直近3ヶ月 vs その前3ヶ月")
        lines.append(f"- 直近3ヶ月合計: ¥{recent_sum:,.0f}（{', '.join(recent)}）")
        lines.append(f"- 前3ヶ月合計: ¥{prior_sum:,.0f}（{', '.join(prior)}）")
        if prior_sum > 0:
            pct = (recent_sum - prior_sum) / prior_sum * 100.0
            lines.append(f"- 変化率: {pct:+.1f}%")

    dept_qty = aggregate_quantity_by_department(records)
    lines.append("")
    lines.append("## 部門別売上（全部門）")
    dept_rank = sorted(dept.items(), key=lambda x: x[1], reverse=True)
    for name, amt in dept_rank:
        share = (amt / total * 100.0) if total else 0.0
        n_prod = len(by_dept.get(name, {}))
        qty_note = ""
        if name in dept_qty:
            qty_note = f" / 販売数量 {_format_quantity_display(dept_qty[name])}"
        lines.append(
            f"- {name}: ¥{amt:,.0f}（構成比 {share:.1f}% / {n_prod:,} 品目{qty_note}）"
        )

    prod_qty = aggregate_quantity_by_product(records)
    lines.append("")
    lines.append(f"## 全体の商品別売上（上位{SALES_ANALYST_TOP_PRODUCTS_GLOBAL}）")
    prod_rank = sorted(product.items(), key=lambda x: x[1], reverse=True)[:SALES_ANALYST_TOP_PRODUCTS_GLOBAL]
    for i, (name, amt) in enumerate(prod_rank, start=1):
        share = (amt / total * 100.0) if total else 0.0
        qty_part = ""
        if name in prod_qty:
            qty_part = f" / 販売数量 {_format_quantity_display(prod_qty[name])}"
        lines.append(f"{i}. {name}: ¥{amt:,.0f}（全体構成比 {share:.1f}%{qty_part}）")

    lines.append("")
    lines.append(
        f"（部門別の商品ランキングは質問時に Supabase 明細から再集計して付与。"
        f"部門名の一部（例: 牛肉）でも照合します）"
    )

    return "\n".join(lines)


def build_sales_context_for_ai(
    records: list[dict], question: str, *, base_summary: str | None = None
) -> str:
    """基本概要 + 質問に応じた部門別ランキング（上位N）を結合。"""
    base = base_summary if base_summary is not None else build_sales_analytics_summary(records)
    extra = build_question_focused_sales_context(records, question)
    if extra:
        return f"{base}\n\n---\n\n{extra}"
    # 部門の明示がなく商品ランキング系の質問なら、売上上位部門のランキングを付与
    if any(k in question for k in ("商品", "品目", "ランキング", "上位")):
        by_dept = aggregate_sales_by_department_and_product(records)
        if by_dept:
            by_qty = aggregate_quantity_by_department_and_product(records)
            if question_asks_quantity(question) and by_qty:
                top_dept = max(by_qty.keys(), key=lambda d: sum(by_qty[d].values()))
                sort_by = "quantity"
            else:
                top_dept = max(by_dept.keys(), key=lambda d: sum(by_dept[d].values()))
                sort_by = "amount"
            top_n = parse_top_n_from_question(question)
            extra = "\n".join(
                [
                    "## 質問に対応する Supabase 集計（部門未指定）",
                    format_department_product_ranking(
                        top_dept,
                        by_dept.get(top_dept, {}),
                        top_n=top_n,
                        product_quantities=by_qty.get(top_dept) or None,
                        sort_by=sort_by,
                    ),
                ]
            )
            return f"{base}\n\n---\n\n{extra}"
    return base


def aggregate_purchases_by_supplier_and_product(
    records: list[dict],
) -> dict[str, dict[str, float]]:
    by_sup: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for r in records:
        by_sup[r["supplier"]][r["product_name"]] += r["amount"]
    return {k: dict(v) for k, v in by_sup.items()}


def aggregate_purchases_quantity_by_supplier_and_product(
    records: list[dict],
) -> dict[str, dict[str, float]]:
    by_sup: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for r in records:
        q = _record_quantity(r)
        if q is not None:
            by_sup[r["supplier"]][r["product_name"]] += q
    return {k: dict(v) for k, v in by_sup.items()}


def aggregate_purchases_by_kategory_and_product(
    records: list[dict],
) -> dict[str, dict[str, float]]:
    by_kat: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for r in records:
        by_kat[r["kategory"]][r["product_name"]] += r["amount"]
    return {k: dict(v) for k, v in by_kat.items()}


def aggregate_purchases_quantity_by_kategory_and_product(
    records: list[dict],
) -> dict[str, dict[str, float]]:
    by_kat: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for r in records:
        q = _record_quantity(r)
        if q is not None:
            by_kat[r["kategory"]][r["product_name"]] += q
    return {k: dict(v) for k, v in by_kat.items()}


def match_suppliers_in_question(question: str, suppliers: list[str]) -> list[str]:
    q = question.strip()
    q_compact = re.sub(r"[\s　]+", "", q)
    hits: list[str] = []
    for name in sorted(suppliers, key=len, reverse=True):
        compact = re.sub(r"[\s　]+", "", name)
        if name in q or compact in q_compact:
            hits.append(name)
    for m in re.finditer(r"[\u4e00-\u9fff]{2,}", q):
        token = m.group()
        if token in _PURCHASE_QUESTION_SKIP_TOKENS:
            continue
        for name in suppliers:
            if name in hits:
                continue
            if token in re.sub(r"[\s　]+", "", name):
                hits.append(name)
    return list(dict.fromkeys(hits))


def format_supplier_product_ranking(
    supplier_name: str,
    product_amounts: dict[str, float],
    *,
    top_n: int,
    product_quantities: dict[str, float] | None = None,
    sort_by: str = "amount",
    header_note: str = "",
) -> str:
    total = sum(product_amounts.values())
    distinct = len(product_amounts)
    qty_map = product_quantities or {}
    total_qty = sum(qty_map.values()) if qty_map else 0.0
    if sort_by == "quantity" and qty_map:
        rank = sorted(qty_map.items(), key=lambda x: x[1], reverse=True)
        rank_header = f"仕入数量の降順（上位 {min(top_n, len(rank))} 件）"
    else:
        rank = sorted(product_amounts.items(), key=lambda x: x[1], reverse=True)
        rank_header = f"仕入額の降順（上位 {min(top_n, len(rank))} 件）"
    show_n = min(top_n, len(rank))
    lines = [
        f"### 取引先「{supplier_name}」{header_note}".rstrip(),
        f"- distinct 商品数: {distinct:,}",
        f"- 仕入額合計: ¥{total:,.0f}",
    ]
    if qty_map:
        lines.append(f"- 仕入数量合計: {_format_quantity_display(total_qty)}")
    lines.append(f"- 以下は{rank_header}")
    for i, (name, primary) in enumerate(rank[:show_n], start=1):
        if sort_by == "quantity" and qty_map:
            qty = primary
            share = (qty / total_qty * 100.0) if total_qty else 0.0
            amt = product_amounts.get(name, 0.0)
            lines.append(
                f"{i}. {name}: 数量 {_format_quantity_display(qty)}（{share:.1f}%） / 仕入 ¥{amt:,.0f}"
            )
        else:
            amt = primary
            share = (amt / total * 100.0) if total else 0.0
            if qty_map and name in qty_map:
                lines.append(
                    f"{i}. {name}: ¥{amt:,.0f}（{share:.1f}%） / 数量 {_format_quantity_display(qty_map[name])}"
                )
            else:
                lines.append(f"{i}. {name}: ¥{amt:,.0f}（構成比 {share:.1f}%）")
    return "\n".join(lines)


def build_purchases_analytics_summary(records: list[dict]) -> str:
    if not records:
        return "（仕入データがありません。伝票読取で保存後に再度お試しください。）"

    dates = [r["date"] for r in records]
    min_date, max_date = min(dates), max(dates)
    total = sum(r["amount"] for r in records)
    by_sup = aggregate_purchases_by_supplier_and_product(records)

    monthly: dict[str, float] = defaultdict(float)
    monthly_count: dict[str, int] = defaultdict(int)
    supplier: dict[str, float] = defaultdict(float)
    product: dict[str, float] = defaultdict(float)
    kategory: dict[str, float] = defaultdict(float)
    for r in records:
        ym = r["year_month"]
        monthly[ym] += r["amount"]
        monthly_count[ym] += 1
        supplier[r["supplier"]] += r["amount"]
        product[r["product_name"]] += r["amount"]
        kategory[r["kategory"]] += r["amount"]

    lines = [
        f"データソース: Supabase `{SUPABASE_TABLE_PURCHASES}`（直近36ヶ月・ページング取得済み）",
        f"明細件数: {len(records):,} 件",
        f"期間: {min_date} 〜 {max_date}",
        f"仕入額合計: ¥{total:,.0f}",
        f"取引先数: {len(by_sup):,}",
        f"部門（{SUPABASE_PURCHASES_KATEGORY_COLUMN}）数: {len({r['kategory'] for r in records}):,}",
    ]
    lines.extend(
        _format_quantity_summary_sections(
            records,
            top_n=SALES_ANALYST_TOP_PRODUCTS_GLOBAL,
            domain="仕入",
            column_name="quantity",
            group_key="supplier",
            group_label="取引先",
            empty_hint="伝票読取保存時に quantity が記録されます。",
        )
    )
    lines.extend(["", "## 月別仕入額"])
    for ym in sorted(monthly.keys()):
        lines.append(f"- {ym}: ¥{monthly[ym]:,.0f}（{monthly_count[ym]:,} 件）")

    lines.append("")
    lines.append("## 取引先別仕入額（全取引先）")
    for name, amt in sorted(supplier.items(), key=lambda x: x[1], reverse=True):
        share = (amt / total * 100.0) if total else 0.0
        n_prod = len(by_sup.get(name, {}))
        lines.append(f"- {name}: ¥{amt:,.0f}（構成比 {share:.1f}% / {n_prod:,} 品目）")

    lines.append("")
    lines.append("## 部門別仕入額（全部門）")
    for name, amt in sorted(kategory.items(), key=lambda x: x[1], reverse=True):
        share = (amt / total * 100.0) if total else 0.0
        lines.append(f"- {name}: ¥{amt:,.0f}（構成比 {share:.1f}%）")

    lines.append("")
    lines.append(f"## 商品別仕入額（上位{SALES_ANALYST_TOP_PRODUCTS_GLOBAL}）")
    for i, (name, amt) in enumerate(
        sorted(product.items(), key=lambda x: x[1], reverse=True)[:SALES_ANALYST_TOP_PRODUCTS_GLOBAL],
        start=1,
    ):
        share = (amt / total * 100.0) if total else 0.0
        lines.append(f"{i}. {name}: ¥{amt:,.0f}（全体構成比 {share:.1f}%）")
    return "\n".join(lines)


def build_question_focused_purchases_context(records: list[dict], question: str) -> str:
    by_sup = aggregate_purchases_by_supplier_and_product(records)
    if not by_sup:
        return ""

    by_qty = aggregate_purchases_quantity_by_supplier_and_product(records)
    top_n = parse_top_n_from_question(question)
    sort_by = "quantity" if question_asks_quantity(question) and by_qty else "amount"

    if question_asks_quantity(question) and by_qty:
        lines = [
            "## 質問に対応する Supabase 集計（仕入・数量）",
            "数量列: `quantity`",
        ]
        lines.extend(
            _format_quantity_summary_sections(
                records,
                top_n=top_n,
                domain="仕入",
                column_name="quantity",
                group_key="supplier",
                group_label="取引先",
                empty_hint="伝票読取保存時に quantity が記録されます。",
            )
        )
        suppliers = sorted(by_qty.keys())
        matched = match_suppliers_in_question(question, suppliers)
        targets = matched if matched else sorted(
            by_qty.keys(), key=lambda s: sum(by_qty[s].values()), reverse=True
        )[:3]
        for sup in targets:
            lines.append("")
            lines.append(
                format_supplier_product_ranking(
                    sup,
                    by_sup.get(sup, {}),
                    top_n=top_n,
                    product_quantities=by_qty.get(sup, {}),
                    sort_by="quantity",
                    header_note="※仕入数量ベース",
                )
            )
        return "\n".join(lines)

    suppliers = sorted(by_sup.keys())
    matched = match_suppliers_in_question(question, suppliers)
    lines = [
        "## 質問に対応する Supabase 集計（purchases）",
        f"テーブル: `{SUPABASE_TABLE_PURCHASES}` / 明細 {len(records):,} 件",
    ]
    if matched:
        for sup in matched:
            lines.append("")
            lines.append(
                format_supplier_product_ranking(
                    sup,
                    by_sup[sup],
                    top_n=top_n,
                    product_quantities=by_qty.get(sup) or None,
                    sort_by=sort_by,
                    header_note="※ユーザー質問に対応",
                )
            )
        return "\n".join(lines)

    by_kat = aggregate_purchases_by_kategory_and_product(records)
    by_kat_qty = aggregate_purchases_quantity_by_kategory_and_product(records)
    matched_kat = match_departments_in_question(question, sorted(by_kat.keys()))
    if matched_kat:
        for kat in matched_kat:
            lines.append("")
            lines.append(
                format_department_product_ranking(
                    kat,
                    by_kat.get(kat, {}),
                    top_n=top_n,
                    product_quantities=by_kat_qty.get(kat) or None,
                    sort_by=sort_by,
                    header_note="※仕入・部門（kategory）",
                    amount_word="仕入",
                )
            )
        return "\n".join(lines)

    if any(k in question for k in ("取引先", "仕入先", "サプライヤ", "supplier")):
        lines.append("")
        lines.append("### 取引先一覧")
        for sup in suppliers:
            amt = sum(by_sup[sup].values())
            qty_note = ""
            if by_qty.get(sup):
                qty_note = f" / 数量 {_format_quantity_display(sum(by_qty[sup].values()))}"
            lines.append(f"- {sup}: ¥{amt:,.0f}{qty_note}")
    return "\n".join(lines) if len(lines) > 2 else ""


def build_purchases_context_for_ai(
    records: list[dict], question: str, *, base_summary: str | None = None
) -> str:
    base = base_summary if base_summary is not None else build_purchases_analytics_summary(records)
    extra = build_question_focused_purchases_context(records, question)
    if extra:
        return f"{base}\n\n---\n\n{extra}"
    if any(k in question for k in ("商品", "品目", "ランキング", "上位", "取引先")):
        by_sup = aggregate_purchases_by_supplier_and_product(records)
        if by_sup:
            by_qty = aggregate_purchases_quantity_by_supplier_and_product(records)
            if question_asks_quantity(question) and by_qty:
                top_sup = max(by_qty.keys(), key=lambda s: sum(by_qty[s].values()))
                sort_by = "quantity"
            else:
                top_sup = max(by_sup.keys(), key=lambda s: sum(by_sup[s].values()))
                sort_by = "amount"
            top_n = parse_top_n_from_question(question)
            extra = "\n".join(
                [
                    "## 質問に対応する Supabase 集計（取引先未指定）",
                    format_supplier_product_ranking(
                        top_sup,
                        by_sup.get(top_sup, {}),
                        top_n=top_n,
                        product_quantities=by_qty.get(top_sup) or None,
                        sort_by=sort_by,
                    ),
                ]
            )
            return f"{base}\n\n---\n\n{extra}"
    return base


@dataclass
class AnalystQueryIntent:
    """質問から Python で抽出した分析意図。"""

    question: str
    include_sales: bool = True
    include_purchases: bool = True
    top_n: int = SALES_ANALYST_QUESTION_TOP_N_DEFAULT
    wants_quantity: bool = False
    keywords: list[str] = field(default_factory=list)
    sales_kategories: list[str] = field(default_factory=list)
    purchase_kategories: list[str] = field(default_factory=list)
    suppliers: list[str] = field(default_factory=list)
    weekdays: list[str] = field(default_factory=list)
    months_back: int | None = None
    year_months: list[str] = field(default_factory=list)


def _normalize_for_product_match(text: str) -> str:
    t = re.sub(r"[\s　]+", "", (text or "").strip())
    return re.sub(r"[【】\[\]()（）「」『』・/／\\\-_]", "", t)


def _extract_keyword_tokens_from_question(question: str) -> list[str]:
    skip = _SALES_QUESTION_SKIP_TOKENS | _PURCHASE_QUESTION_SKIP_TOKENS | {
        "直近",
        "過去",
        "前年",
        "前月",
        "今年",
        "去年",
        "比較",
        "推移",
        "予測",
        "見込み",
        "トレンド",
        "傾向",
        "全体",
        "合計",
        "平均",
        "売上金額",
        "仕入金額",
        "販売数量",
        "仕入数量",
        "金額",
    }
    tokens: list[str] = []
    for m in re.finditer(r"[\u4e00-\u9fff\u30a0-\u30ff\u3040-\u309f]{2,}", question):
        t = _normalize_for_product_match(m.group())
        if len(t) < 2 or t in skip:
            continue
        tokens.append(t)
    for m in re.finditer(r"[\u4e00-\u9fff]{2,}", question):
        t = m.group()
        if t in skip:
            continue
        tokens.append(t)
    return list(dict.fromkeys(tokens))


def _parse_months_back_from_question(question: str) -> int | None:
    for pat in (r"直近\s*(\d+)\s*ヶ?月", r"過去\s*(\d+)\s*ヶ?月", r"(\d+)\s*ヶ?月間"):
        m = re.search(pat, question)
        if m:
            return min(max(int(m.group(1)), 1), 36)
    return None


def _parse_year_months_from_question(question: str) -> list[str]:
    found: list[str] = []
    for m in re.finditer(r"(\d{4})年(\d{1,2})月", question):
        found.append(f"{int(m.group(1)):04d}-{int(m.group(2)):02d}")
    for m in re.finditer(r"(\d{4})-(\d{1,2})", question):
        ym = f"{int(m.group(1)):04d}-{int(m.group(2)):02d}"
        if ym not in found:
            found.append(ym)
    return found


def extract_analyst_query_intent(
    question: str,
    sales_records: list[dict],
    purchase_records: list[dict],
) -> AnalystQueryIntent:
    """質問 → キーワード・対象ドメイン・部門/取引先・期間を抽出。"""
    asks_p = question_asks_purchases(question)
    asks_s = question_asks_sales(question)
    include_sales = asks_s or (not asks_p)
    include_purchases = asks_p or (not asks_s)

    sales_kats = sorted({r["kategory"] for r in sales_records if r.get("kategory")})
    purchase_kats = sorted({r["kategory"] for r in purchase_records if r.get("kategory")})
    suppliers = sorted({r["supplier"] for r in purchase_records if r.get("supplier")})

    intent = AnalystQueryIntent(
        question=question.strip(),
        include_sales=include_sales,
        include_purchases=include_purchases,
        top_n=parse_top_n_from_question(question),
        wants_quantity=question_asks_quantity(question),
        keywords=_extract_keyword_tokens_from_question(question),
        sales_kategories=match_departments_in_question(question, sales_kats) if sales_kats else [],
        purchase_kategories=match_departments_in_question(question, purchase_kats)
        if purchase_kats
        else [],
        suppliers=match_suppliers_in_question(question, suppliers) if suppliers else [],
        weekdays=match_weekdays_in_question(question),
        months_back=_parse_months_back_from_question(question),
        year_months=_parse_year_months_from_question(question),
    )
    return intent


def _filter_records_by_months(records: list[dict], intent: AnalystQueryIntent) -> list[dict]:
    out = records
    if intent.year_months:
        ym_set = set(intent.year_months)
        out = [r for r in out if r["year_month"] in ym_set]
    if intent.months_back:
        month_keys = sorted({r["year_month"] for r in out})
        if month_keys:
            use = set(month_keys[-intent.months_back :])
            out = [r for r in out if r["year_month"] in use]
    return out


def _record_matches_keyword_filters(r: dict, intent: AnalystQueryIntent, *, domain: str) -> bool:
    if not intent.keywords:
        return True
    parts = [str(r.get("product_name") or ""), str(r.get("kategory") or "")]
    if domain == "purchase":
        parts.append(str(r.get("supplier") or ""))
    blob = "".join(_normalize_for_product_match(p) for p in parts)
    return any(_normalize_for_product_match(kw) in blob for kw in intent.keywords)


def filter_sales_records_for_intent(
    records: list[dict], intent: AnalystQueryIntent
) -> list[dict]:
    if not intent.include_sales:
        return []
    out = _filter_records_by_months(records, intent)
    if intent.weekdays:
        wd_set = set(intent.weekdays)
        out = [r for r in out if str(r.get("weekday_name") or "").strip() in wd_set]
    if intent.sales_kategories:
        kat_set = set(intent.sales_kategories)
        out = [r for r in out if r["kategory"] in kat_set]
    elif intent.keywords:
        out = [r for r in out if _record_matches_keyword_filters(r, intent, domain="sales")]
    return out


def filter_purchase_records_for_intent(
    records: list[dict], intent: AnalystQueryIntent
) -> list[dict]:
    if not intent.include_purchases:
        return []
    out = _filter_records_by_months(records, intent)
    if intent.suppliers:
        sup_set = set(intent.suppliers)
        out = [r for r in out if r["supplier"] in sup_set]
    if intent.purchase_kategories:
        kat_set = set(intent.purchase_kategories)
        out = [r for r in out if r["kategory"] in kat_set]
    elif intent.keywords and not intent.suppliers:
        out = [r for r in out if _record_matches_keyword_filters(r, intent, domain="purchase")]
    return out


def _filter_with_fallback(
    all_records: list[dict],
    filtered: list[dict],
    intent: AnalystQueryIntent,
) -> tuple[list[dict], bool]:
    """絞り込み0件なら期間のみ緩和して再抽出。"""
    if filtered:
        return filtered, False
    relaxed = _filter_records_by_months(all_records, intent)
    if relaxed:
        return relaxed, True
    return all_records, True


def format_analyst_intent_summary(intent: AnalystQueryIntent) -> str:
    lines = [
        f"質問: {intent.question}",
        f"対象: 売上={'あり' if intent.include_sales else 'なし'} / 仕入={'あり' if intent.include_purchases else 'なし'}",
        f"上位件数: {intent.top_n}",
        f"数量指標: {'はい' if intent.wants_quantity else 'いいえ'}",
    ]
    if intent.months_back:
        lines.append(f"期間: 直近 {intent.months_back} ヶ月")
    if intent.year_months:
        lines.append(f"対象月: {', '.join(intent.year_months)}")
    if intent.keywords:
        lines.append(f"キーワード: {', '.join(intent.keywords)}")
    if intent.sales_kategories:
        lines.append(f"売上部門: {', '.join(intent.sales_kategories)}")
    if intent.weekdays:
        lines.append(f"曜日: {', '.join(intent.weekdays)}")
    if intent.purchase_kategories:
        lines.append(f"仕入部門: {', '.join(intent.purchase_kategories)}")
    if intent.suppliers:
        lines.append(f"取引先: {', '.join(intent.suppliers)}")
    return "\n".join(lines)


def build_query_driven_analyst_context(
    sales_records: list[dict],
    purchase_records: list[dict],
    question: str,
) -> tuple[str, AnalystQueryIntent, str]:
    """
    質問 → キーワード抽出 → レコード絞り込み → 集計 → GPT 用テキスト。
    戻り値: (コンテキスト, 意図, パイプライン説明)
    """
    intent = extract_analyst_query_intent(question, sales_records, purchase_records)

    sales_filtered = filter_sales_records_for_intent(sales_records, intent)
    sales_filtered, sales_relaxed = _filter_with_fallback(
        sales_records, sales_filtered, intent
    )
    purchase_filtered = filter_purchase_records_for_intent(purchase_records, intent)
    purchase_filtered, purchase_relaxed = _filter_with_fallback(
        purchase_records, purchase_filtered, intent
    )

    parts: list[str] = [
        "# 質問に基づく分析データ（Python: 抽出 → 絞り込み → 集計）",
        "## 抽出条件",
        format_analyst_intent_summary(intent),
        "",
        "## 絞り込み結果",
        f"- 売上: {len(sales_filtered):,} / {len(sales_records):,} 件"
        + ("（条件緩和: 期間のみ）" if sales_relaxed and sales_records else ""),
        f"- 仕入: {len(purchase_filtered):,} / {len(purchase_records):,} 件"
        + ("（条件緩和: 期間のみ）" if purchase_relaxed and purchase_records else ""),
    ]

    if intent.include_purchases:
        if purchase_filtered:
            p_ctx = build_purchases_context_for_ai(
                purchase_filtered,
                question,
                base_summary=build_purchases_analytics_summary(purchase_filtered),
            )
            parts.append(f"\n---\n\n## 仕入（抽出後の集計）\n\n{p_ctx}")
        elif purchase_records:
            parts.append("\n---\n\n## 仕入\n\n（抽出条件に一致する明細がありませんでした。）")
        else:
            parts.append("\n---\n\n## 仕入\n\n（データなし）")

    if intent.include_sales:
        if sales_filtered:
            s_ctx = build_sales_context_for_ai(
                sales_filtered,
                question,
                base_summary=build_sales_analytics_summary(sales_filtered),
            )
            parts.append(f"\n---\n\n## 売上（抽出後の集計）\n\n{s_ctx}")
        elif sales_records:
            parts.append("\n---\n\n## 売上\n\n（抽出条件に一致する明細がありませんでした。）")
        else:
            parts.append("\n---\n\n## 売上\n\n（データなし）")

    pipeline_note = (
        "処理: 質問 → キーワード/部門/取引先/商品/期間の抽出 → "
        "Pythonで対象レコードを絞り込み → 集計表のみを GPT に渡す"
    )
    return "\n".join(parts), intent, pipeline_note


def build_unified_analyst_context(
    sales_records: list[dict],
    purchase_records: list[dict],
    question: str,
) -> str:
    """後方互換。質問駆動パイプラインの集計テキストを返す。"""
    context, _, _ = build_query_driven_analyst_context(
        sales_records, purchase_records, question
    )
    return context


def load_analyst_system_prompt() -> str:
    for name in ("business_analyst_system.txt", "sales_analyst_system.txt"):
        try:
            return load_prompt_text(name)
        except FileNotFoundError:
            continue
    return (
        "あなたは仕入と売上の分析アナリストです。"
        "渡された Supabase 集計のみを根拠に日本語で回答してください。"
    )


def load_purchase_records_for_analyst(client: Client, *, force_refresh: bool = False) -> list[dict]:
    if not force_refresh:
        cached = st.session_state.get("analyst_purchase_records")
        if isinstance(cached, list) and cached:
            return cached
    rows = fetch_purchases_for_dashboard(client)
    st.session_state.dashboard_purchases = rows
    records = purchases_rows_to_analytics(rows)
    st.session_state.analyst_purchase_records = records
    return records


def load_sales_records_for_analyst(client: Client, *, force_refresh: bool = False) -> list[dict]:
    """分析・予測用に Supabase から直近36ヶ月の売上を取得し正規化する。"""
    if not force_refresh:
        cached_records = st.session_state.get("analyst_sales_records")
        if (
            isinstance(cached_records, list)
            and cached_records
            and "weekday_name" in cached_records[0]
        ):
            return cached_records
    rows = fetch_sales_for_dashboard(client)
    st.session_state.dashboard_sales = rows
    records = sales_rows_to_analytics(rows)
    st.session_state.analyst_sales_records = records
    return records


def load_analyst_datasets(
    client: Client, *, force_refresh: bool = False
) -> tuple[list[dict], list[dict]]:
    """分析用に仕入・売上を Supabase から取得。"""
    purchases = load_purchase_records_for_analyst(client, force_refresh=force_refresh)
    sales = load_sales_records_for_analyst(client, force_refresh=force_refresh)
    return purchases, sales


def answer_analyst_question(
    api_key: str,
    question: str,
    sales_records: list[dict],
    purchase_records: list[dict],
    history: list[dict],
    *,
    model: str | None = None,
) -> tuple[str, AnalystQueryIntent, str, str]:
    """質問駆動パイプラインで回答。戻り値: (回答, 抽出意図, 処理説明, GPT用集計テキスト)。"""
    model = model or get_openai_chat_model()
    system = load_analyst_system_prompt()
    data_summary, intent, pipeline_note = build_query_driven_analyst_context(
        sales_records, purchase_records, question
    )
    system_full = (
        f"{system}\n\n---\n\n{pipeline_note}\n\n---\n\n{data_summary}"
    )
    messages: list[dict] = [{"role": "system", "content": system_full}]
    for m in history[-20:]:
        role = m.get("role")
        content = (m.get("content") or "").strip()
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": question})
    reply = openai_chat_completion(api_key, model, messages)
    return reply, intent, pipeline_note, data_summary


def render_analyst_page() -> None:
    st.title("🤖 分析・予測（AIアナリスト）")
    st.caption(
        "質問 → **キーワード抽出** → **Pythonで対象レコード抽出** → **集計** → GPT。"
        f"全データ（直近36ヶ月）はメモリに保持し、質問ごとに必要な部分だけを渡します。"
    )

    client = get_supabase_client_for_writes()
    if not client:
        st.warning(
            "Supabase に接続できません。ログインするか、`.env` の `SUPABASE_URL` とキーを確認してください。"
        )
        return

    api_key = get_openai_api_key()
    if not api_key:
        st.error("`OPENAI_API_KEY` または `CHATGPT_API_KEY` を `.env` に設定してください。")
        return

    tool_l, tool_r = st.columns([1, 1])
    with tool_l:
        if st.button("データを再読込", key="analyst_reload"):
            for k in (
                "analyst_load_error",
                "analyst_purchase_summary",
                "analyst_sales_summary",
                "analyst_purchase_records",
                "analyst_sales_records",
                "analyst_purchase_count",
                "analyst_sales_count",
                "dashboard_sales",
                "dashboard_sales_db_count",
                "dashboard_purchases",
                "sales_analyst_messages",
                "sales_analyst_records",
                "sales_analyst_data_summary",
            ):
                st.session_state.pop(k, None)
            st.rerun()
    with tool_r:
        if st.button("会話をクリア", key="analyst_clear_chat"):
            st.session_state.analyst_messages = []
            st.rerun()

    if "analyst_sales_records" not in st.session_state:
        with st.spinner("Supabase から仕入・売上データを取得しています…"):
            try:
                purchases, sales = load_analyst_datasets(client, force_refresh=True)
                st.session_state.analyst_purchase_records = purchases
                st.session_state.analyst_sales_records = sales
                st.session_state.analyst_purchase_count = len(purchases)
                st.session_state.analyst_sales_count = len(sales)
                st.session_state.analyst_purchase_summary = (
                    f"全 {len(purchases):,} 件（質問時に抽出・集計）"
                )
                st.session_state.analyst_sales_summary = (
                    f"全 {len(sales):,} 件（質問時に抽出・集計）"
                )
                st.session_state.analyst_load_error = None
            except Exception as e:
                st.session_state.analyst_purchase_records = []
                st.session_state.analyst_sales_records = []
                st.session_state.analyst_purchase_summary = ""
                st.session_state.analyst_sales_summary = ""
                st.session_state.analyst_purchase_count = 0
                st.session_state.analyst_sales_count = 0
                st.session_state.analyst_load_error = str(e)

    load_err = st.session_state.get("analyst_load_error")
    if load_err:
        st.error(f"データ取得に失敗しました: {load_err}")
        return

    n_purchase = int(st.session_state.get("analyst_purchase_count") or 0)
    n_sales = int(st.session_state.get("analyst_sales_count") or 0)
    if n_purchase == 0 and n_sales == 0:
        st.info(
            "集計できるデータがありません。"
            "仕入は「伝票読み取り」、売上は「CSV取り込み」からデータを登録してください。"
        )
        return

    purchase_records = list(st.session_state.get("analyst_purchase_records") or [])
    sales_records = list(st.session_state.get("analyst_sales_records") or [])

    st.caption(
        "質問のたびにキーワード・部門・取引先・商品・期間で明細を絞り込み、"
        "その集計表だけを GPT に送ります（全件の要約は送りません）。"
    )

    with st.expander("質問例（クリックで入力欄に反映）", expanded=True):
        for i, q in enumerate(ANALYST_EXAMPLE_QUESTIONS):
            if st.button(q, key=f"analyst_example_{i}", use_container_width=True):
                st.session_state.analyst_pending_question = q
                st.rerun()

    if st.session_state.pop("analyst_clear_question_draft", False):
        st.session_state["analyst_question_draft"] = ""

    pending_prefill = (st.session_state.pop("analyst_pending_question", None) or "").strip()
    if pending_prefill:
        st.session_state["analyst_question_draft"] = pending_prefill

    with st.form("analyst_ask_form", clear_on_submit=False):
        st.text_area(
            "質問",
            key="analyst_question_draft",
            height=100,
            placeholder="仕入・売上について質問してください（例: 取引先別仕入、部門別販売数量）",
        )
        submitted = st.form_submit_button("分析する", type="primary")

    if "analyst_messages" not in st.session_state:
        st.session_state.analyst_messages = []

    for msg in st.session_state.analyst_messages:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    st.divider()
    st.markdown("**データ・直近の分析条件**")
    st.caption(
        f"分析対象: 仕入 {n_purchase:,} 件 / 売上 {n_sales:,} 件"
        f"（モデル: `{get_openai_chat_model()}`）"
    )
    if sales_records and not records_have_quantity(sales_records):
        st.warning(
            f"売上の販売数量（`{SUPABASE_SALES_QUANTITY_COLUMN}`）が未登録の明細があります。"
            "CSV の「商品数」で再取込後、「データを再読込」を押してください。"
        )
    if purchase_records and not records_have_quantity(purchase_records):
        st.caption("仕入の quantity が空の明細があります（伝票保存時に数量がある行のみ集計されます）。")

    with st.expander("保持データ（全件・未加工）", expanded=False):
        st.text(
            f"仕入: {st.session_state.get('analyst_purchase_summary') or '—'}\n"
            f"売上: {st.session_state.get('analyst_sales_summary') or '—'}"
        )
    if st.session_state.get("analyst_last_intent_summary"):
        with st.expander("直近の質問で抽出した条件", expanded=False):
            st.text(st.session_state.analyst_last_intent_summary)
    if st.session_state.get("analyst_last_context_preview"):
        with st.expander("直近の質問で GPT に渡した集計", expanded=False):
            st.text(st.session_state.analyst_last_context_preview)

    prompt = ""
    if submitted:
        prompt = (st.session_state.get("analyst_question_draft") or "").strip()
    if not prompt:
        return

    history = list(st.session_state.get("analyst_messages") or [])
    with st.spinner("キーワード抽出・絞り込み・集計のあと AI が回答しています…"):
        try:
            reply, intent, _pipeline, context_text = answer_analyst_question(
                api_key,
                prompt,
                sales_records,
                purchase_records,
                history,
            )
            st.session_state.analyst_last_intent_summary = format_analyst_intent_summary(
                intent
            )
            preview = context_text[:12000]
            if len(context_text) > 12000:
                preview += "\n…（省略）"
            st.session_state.analyst_last_context_preview = preview
        except Exception as e:
            reply = f"回答の取得に失敗しました: {e}"

    st.session_state.analyst_messages.append({"role": "user", "content": prompt})
    st.session_state.analyst_messages.append({"role": "assistant", "content": reply})
    st.session_state.analyst_clear_question_draft = True
    st.rerun()


def _nav_menu_label(section: str, page: str) -> str:
    return f"{section}：{page}"


def _parse_nav_menu_label(label: str) -> tuple[str, str]:
    for section, page in NAV_MENU_ITEMS:
        if _nav_menu_label(section, page) == label:
            return section, page
    return NAV_MENU_ITEMS[0]


def _sync_nav_page_state() -> None:
    """nav_section / nav_page を初期化（ウィジェットキーとは別管理）。"""
    if "nav_section" not in st.session_state:
        st.session_state.nav_section = "仕入"
    if "nav_page" in st.session_state:
        pages = _pages_for_nav_section(st.session_state.nav_section)
        if st.session_state.nav_page in pages:
            return
    if "sidebar_nav_selection" in st.session_state:
        section, page = _parse_nav_menu_label(st.session_state.sidebar_nav_selection)
    else:
        section = st.session_state.nav_section
        if section == "仕入":
            page = st.session_state.get("sidebar_purchase_page", PURCHASE_NAV_PAGES[0])
        elif section == "売上":
            page = st.session_state.get("sidebar_sales_page", SALES_NAV_PAGES[0])
        else:
            page = st.session_state.get("sidebar_analyst_page", ANALYST_NAV_PAGE)
    pages = _pages_for_nav_section(section)
    st.session_state.nav_section = section
    st.session_state.nav_page = page if page in pages else pages[0]


def _sidebar_nav_radio_key(section: str) -> str:
    return f"sidebar_nav_radio_{section}"


def _on_sidebar_nav_radio_change(section: str) -> None:
    """ラジオ変更時に nav_page を更新（ウィジェットキーは書き換えない）。"""
    label = st.session_state[_sidebar_nav_radio_key(section)]
    _, page = _parse_nav_menu_label(label)
    st.session_state.nav_section = section
    st.session_state.nav_page = page


def _pages_for_nav_section(section: str) -> tuple[str, ...]:
    if section == "仕入":
        return PURCHASE_NAV_PAGES
    if section == "売上":
        return SALES_NAV_PAGES
    return (ANALYST_NAV_PAGE,)


def render_sidebar_navigation() -> tuple[str, str]:
    """サイドバー（仕入・売上・分析の3セクション、ラジオは常に1つだけ）。"""
    _sync_nav_page_state()
    section = st.session_state.nav_section
    page = st.session_state.nav_page

    for sec_name in ("仕入", "売上", ANALYST_NAV_SECTION):
        pages = _pages_for_nav_section(sec_name)
        with st.sidebar.expander(sec_name, expanded=(section == sec_name)):
            if section == sec_name:
                labels = [_nav_menu_label(sec_name, p) for p in pages]
                idx = pages.index(page) if page in pages else 0
                st.radio(
                    f"{sec_name}メニュー",
                    labels,
                    index=idx,
                    key=_sidebar_nav_radio_key(sec_name),
                    label_visibility="collapsed",
                    on_change=_on_sidebar_nav_radio_change,
                    args=(sec_name,),
                )
            else:
                for p in pages:
                    is_current = (
                        st.session_state.nav_section == sec_name
                        and st.session_state.nav_page == p
                    )
                    if st.button(
                        p,
                        key=f"sidebar_nav_jump_{sec_name}_{p}",
                        use_container_width=True,
                        type="primary" if is_current else "secondary",
                    ):
                        st.session_state.nav_section = sec_name
                        st.session_state.nav_page = p
                        st.rerun()

    return st.session_state.nav_section, st.session_state.nav_page


def sales_to_display_rows(rows: list[dict]) -> list[dict]:
    """売上一覧表示用に日本語キーへ。"""
    date_col = SUPABASE_SALES_DATE_COLUMN
    products_col = SUPABASE_SALES_PRODUCTS_COLUMN
    amt_col = SUPABASE_SALES_AMOUNT_COLUMN
    qty_col = SUPABASE_SALES_QUANTITY_COLUMN
    wd_col = SUPABASE_SALES_WEEKDAY_COLUMN
    out: list[dict] = []
    for r in rows:
        raw_qty = r.get(qty_col)
        product_name = str(r.get("product_name") or r.get(products_col) or "").strip()
        department = str(r.get("kategory") or "").strip()
        if not department and SUPABASE_SALES_KATEGORY_COLUMN:
            department = str(r.get(SUPABASE_SALES_KATEGORY_COLUMN) or "").strip()
        date_iso = normalize_purchase_date_to_iso(str(r.get(date_col) or "").strip())
        weekday = str(r.get(wd_col) or "").strip()
        if not weekday:
            weekday = weekday_name_from_iso_date(date_iso)
        category2 = str(r.get("sales_category2") or "").strip()
        if not category2:
            master = _sales_nested_master_dict(r)
            if master:
                category2 = str(master.get("sales_category2") or "").strip()
        out.append(
            {
                "id": r.get("id"),
                "日付": date_iso,
                "曜日": weekday,
                "商品名": product_name,
                "部門": department,
                "カテゴリ2": category2,
                "商品数": _format_quantity_display(raw_qty) if raw_qty is not None else "",
                "売上金額": r.get(amt_col),
            }
        )
    return out


def aggregate_sales_rows_by_product(rows: list[dict]) -> list[dict]:
    """売上検索結果を商品名で集計し、売上金額の降順で返す。"""
    products_col = SUPABASE_SALES_PRODUCTS_COLUMN
    amt_col = SUPABASE_SALES_AMOUNT_COLUMN
    qty_col = SUPABASE_SALES_QUANTITY_COLUMN
    buckets: dict[str, dict] = {}

    for r in rows:
        name = (
            str(r.get("product_name") or r.get(products_col) or "").strip() or "（未設定）"
        )
        bucket = buckets.setdefault(
            name,
            {"amount": 0.0, "quantity": 0.0, "count": 0, "has_qty": False, "kategories": set()},
        )
        raw_amt = r.get(amt_col)
        amount = float(_coerce_sales_amount(raw_amt) or parse_money_value(raw_amt) or 0)
        bucket["amount"] += amount
        bucket["count"] += 1
        qty = _coerce_quantity_value(r.get(qty_col))
        if qty is not None:
            bucket["quantity"] += qty
            bucket["has_qty"] = True
        kat = str(r.get("kategory") or "").strip()
        if not kat and SUPABASE_SALES_KATEGORY_COLUMN:
            kat = str(r.get(SUPABASE_SALES_KATEGORY_COLUMN) or "").strip()
        if kat:
            bucket["kategories"].add(kat)

    ranked = sorted(buckets.items(), key=lambda x: x[1]["amount"], reverse=True)
    out: list[dict] = []
    for i, (name, data) in enumerate(ranked, start=1):
        row: dict = {
            "順位": i,
            "商品名": name,
            "件数": data["count"],
            "売上金額": data["amount"],
        }
        kats = sorted(data["kategories"])
        if len(kats) == 1:
            row["部門"] = kats[0]
        elif len(kats) > 1:
            row["部門"] = " / ".join(kats)
        if data["has_qty"]:
            row["商品数"] = data["quantity"]
        out.append(row)
    return out


def purchases_to_display_rows(rows: list[dict]) -> list[dict]:
    """一覧表示用に日本語キーへ。"""
    out: list[dict] = []
    for r in rows:
        pid = r.get(SUPABASE_PURCHASES_PRODUCT_ID_COLUMN) or r.get("product_id")
        out.append(
            {
                "id": r.get("id"),
                "伝票番号": r.get("invoice_number"),
                "日付": normalize_purchase_date_to_iso(str(r.get("purchase_date") or "").strip()),
                "取引先": r.get("supplier"),
                "商品名": _purchase_product_name_from_row(r) or r.get("product_name"),
                "product_id": pid if pid is not None and str(pid).strip() else "",
                "数量": r.get("quantity"),
                "単価": r.get("unit_price"),
                "金額": r.get("amount"),
                "備考": purchase_note_from_db_row(r),
                "OCR抜粋": (str(r.get("ocr_text") or "")[:120] + ("…" if len(str(r.get("ocr_text") or "")) > 120 else "")),
            }
        )
    return out


def render_supabase_purchases_search(widget_prefix: str, *, compact: bool = False) -> None:
    """Supabase の purchases を検索して表示。widget_prefix でウィジェット key を一意化。"""
    client = get_supabase_client_for_writes()
    if not client:
        st.warning(
            "Supabase に接続できません。ログインするか、`.env` の `SUPABASE_URL` とキーを確認してください。"
        )
        return
    if compact:
        st.caption("年月・商品名・取引先のいずれか（または組み合わせ）で絞り込みます。条件なしのときは直近のみ表示します。")
    else:
        st.caption(
            "保存済みの `purchases` を参照します。RLS 使用時は **SELECT 用ポリシー**が必要です。"
            "条件なしのときは直近 200 件まで表示します。"
        )
    with st.form(f"purchases_search_form_{widget_prefix}"):
        c1, c2, c3 = st.columns(3)
        with c1:
            ym = st.text_input(
                "年月",
                placeholder="例: 2026-05 / 2026年5月",
                key=f"{widget_prefix}_ym",
            )
        with c2:
            prod = st.text_input("商品名（部分一致）", key=f"{widget_prefix}_product")
        with c3:
            sup = st.text_input("取引先名（部分一致）", key=f"{widget_prefix}_supplier")
        submitted = st.form_submit_button("検索", type="primary")

    if submitted:
        try:
            has_ym = bool((ym or "").strip())
            has_prod = bool((prod or "").strip())
            has_sup = bool((sup or "").strip())
            lim = 200 if not (has_ym or has_prod or has_sup) else 500
            catalog, cat_err = fetch_product_catalog(client)
            rows = fetch_purchases_filtered(
                client, ym or "", prod or "", sup or "", limit=lim, catalog=catalog
            )
            st.session_state[f"{widget_prefix}_hist_rows"] = rows
            st.session_state[f"{widget_prefix}_hist_error"] = None
            st.session_state[f"{widget_prefix}_hist_lim_note"] = lim
            st.session_state[f"{widget_prefix}_hist_cat_warn"] = cat_err
            if (has_prod or has_sup) and not rows and not cat_err:
                st.session_state[f"{widget_prefix}_hist_hint"] = (
                    "条件に合うデータがありません。商品名は purchases.product_name、"
                    "取引先のみの検索は purchase_products / suppliers 経由です。"
                )
            else:
                st.session_state.pop(f"{widget_prefix}_hist_hint", None)
        except Exception as e:
            st.session_state[f"{widget_prefix}_hist_rows"] = None
            st.session_state[f"{widget_prefix}_hist_error"] = str(e)

    cat_warn = st.session_state.get(f"{widget_prefix}_hist_cat_warn")
    if cat_warn:
        st.warning(f"商品マスタの取得に問題があります: {cat_warn}")

    hint = st.session_state.get(f"{widget_prefix}_hist_hint")
    if hint:
        st.info(hint)

    err = st.session_state.get(f"{widget_prefix}_hist_error")
    if err:
        st.error(f"検索に失敗しました: {err}")
        if "row-level security" in err.lower() or "42501" in err:
            st.info(
                "Supabase の SQL で `authenticated` ロール向けの **SELECT** ポリシーを "
                f'`public.{SUPABASE_TABLE_PURCHASES}` に追加してください。'
            )
            st.code(
                f"""create policy "purchases_select_authenticated"
  on public.{SUPABASE_TABLE_PURCHASES}
  for select
  to authenticated
  using (true);
""",
                language="sql",
            )
        return

    rows = st.session_state.get(f"{widget_prefix}_hist_rows")
    if rows is None:
        st.info("条件を入力して「検索」を押すと結果が表示されます。")
        return
    if not isinstance(rows, list):
        return
    if len(rows) == 0:
        st.info("該当するデータがありませんでした。")
        return
    lim_note = st.session_state.get(f"{widget_prefix}_hist_lim_note", 500)
    if lim_note == 200:
        st.caption("条件が空のため、直近 200 件に制限して表示しています。")
    st.success(f"{len(rows)}件ヒットしました。")
    disp = purchases_to_display_rows(rows)
    st.dataframe(
        disp,
        use_container_width=True,
        height=min(520, max(200, 32 * min(len(disp), 18) + 40)),
    )


def render_supabase_sales_search(widget_prefix: str, *, compact: bool = False) -> None:
    """Supabase の sales を検索して表示。widget_prefix でウィジェット key を一意化。"""
    client = get_supabase_client_for_writes()
    if not client:
        st.warning(
            "Supabase に接続できません。ログインするか、`.env` の `SUPABASE_URL` とキーを確認してください。"
        )
        return
    catalog_prefetch, catalog_prefetch_err = fetch_sales_product_catalog(client)
    cat2_choices = ["（指定なし）"] + distinct_sales_category2_choices(catalog_prefetch)
    cat2_col_label = (
        SUPABASE_SALES_PRODUCTS_CATEGORY2_COLUMN or "sales_category2"
    ).strip()

    if compact:
        st.caption(
            "年月・商品名・部門・カテゴリ2のいずれか（または組み合わせ）で絞り込みます。"
            "条件なしのときは直近のみ表示します。"
        )
    else:
        st.caption(
            f"保存済みの `{SUPABASE_TABLE_SALES}` を参照します。RLS 使用時は **SELECT 用ポリシー**が必要です。"
            "条件なしのときは直近 200 件まで表示します。"
        )
    if catalog_prefetch_err:
        st.warning(f"売上商品マスタの取得に問題があります: {catalog_prefetch_err}")
    elif not cat2_choices or cat2_choices == ["（指定なし）"]:
        st.caption(
            f"`{SUPABASE_TABLE_SALES_PRODUCTS}.{cat2_col_label}` が未登録のため、カテゴリ2は選択できません。"
        )

    with st.form(f"sales_search_form_{widget_prefix}"):
        c1, c2, c3, c4 = st.columns(4)
        with c1:
            ym = st.text_input(
                "年月",
                placeholder="例: 2026-05 / 2026年5月",
                key=f"{widget_prefix}_ym",
            )
        with c2:
            prod = st.text_input("商品名（部分一致）", key=f"{widget_prefix}_product")
        with c3:
            dept = st.text_input("部門（部分一致）", key=f"{widget_prefix}_kategory")
        with c4:
            cat2_pick = st.selectbox(
                "カテゴリ2",
                options=cat2_choices,
                key=f"{widget_prefix}_category2",
            )
        submitted = st.form_submit_button("検索", type="primary")

    if submitted:
        try:
            has_ym = bool((ym or "").strip())
            has_prod = bool((prod or "").strip())
            has_dept = bool((dept or "").strip())
            cat2_val = ""
            if cat2_pick and cat2_pick != "（指定なし）":
                cat2_val = str(cat2_pick).strip()
            has_cat2 = bool(cat2_val)
            lim = 200 if not (has_ym or has_prod or has_dept or has_cat2) else 500
            catalog, cat_err = fetch_sales_product_catalog(client)
            rows = fetch_sales_filtered(
                client,
                ym or "",
                prod or "",
                dept or "",
                cat2_val,
                limit=lim,
            )
            st.session_state[f"{widget_prefix}_hist_rows"] = rows
            st.session_state[f"{widget_prefix}_hist_error"] = None
            st.session_state[f"{widget_prefix}_hist_lim_note"] = lim
            st.session_state[f"{widget_prefix}_hist_cat_warn"] = cat_err
            if (has_prod or has_dept or has_cat2) and not rows:
                cat_n = len(catalog)
                diag = (
                    f"（sales_products マスタ: {cat_n} 件"
                    + ("、取得不可の可能性あり" if cat_n == 0 else "")
                    + "）"
                )
                st.session_state[f"{widget_prefix}_hist_hint"] = (
                    "条件に合うデータがありません。"
                    f"商品名は `sales.{SUPABASE_SALES_PRODUCTS_COLUMN}` または "
                    f"`{SUPABASE_TABLE_SALES_PRODUCTS}.{SUPABASE_SALES_PRODUCTS_MASTER_NAME_COLUMN}`、"
                    f"部門は `{SUPABASE_TABLE_SALES_PRODUCTS}.{SUPABASE_SALES_PRODUCTS_CATEGORY_COLUMN}`、"
                    f"カテゴリ2は `{SUPABASE_TABLE_SALES_PRODUCTS}.{cat2_col_label}` "
                    f"（いずれも `sales.{SUPABASE_SALES_PRODUCT_ID_COLUMN}` 経由）で部分一致検索します。"
                    f" CSV 取込後のデータか、`sales_products` の SELECT 権限も確認してください。{diag}"
                )
            else:
                st.session_state.pop(f"{widget_prefix}_hist_hint", None)
        except Exception as e:
            st.session_state[f"{widget_prefix}_hist_rows"] = None
            st.session_state[f"{widget_prefix}_hist_error"] = str(e)

    cat_warn = st.session_state.get(f"{widget_prefix}_hist_cat_warn")
    if cat_warn:
        st.warning(f"売上商品マスタの取得に問題があります: {cat_warn}")

    hint = st.session_state.get(f"{widget_prefix}_hist_hint")
    if hint:
        st.info(hint)

    err = st.session_state.get(f"{widget_prefix}_hist_error")
    if err:
        st.error(f"検索に失敗しました: {err}")
        if "row-level security" in err.lower() or "42501" in err:
            st.info(
                "Supabase の SQL で `authenticated` ロール向けの **SELECT 用ポリシーを "
                f'`public.{SUPABASE_TABLE_SALES}` および `public.{SUPABASE_TABLE_SALES_PRODUCTS}` に追加してください。'
            )
            st.code(
                f"""create policy "sales_select_authenticated"
  on public.{SUPABASE_TABLE_SALES}
  for select
  to authenticated
  using (true);

create policy "sales_products_select_authenticated"
  on public.{SUPABASE_TABLE_SALES_PRODUCTS}
  for select
  to authenticated
  using (true);
""",
                language="sql",
            )
        return

    rows = st.session_state.get(f"{widget_prefix}_hist_rows")
    if rows is None:
        st.info("条件を入力して「検索」を押すと結果が表示されます。")
        return
    if not isinstance(rows, list):
        return
    if len(rows) == 0:
        st.info("該当するデータがありませんでした。")
        return
    lim_note = st.session_state.get(f"{widget_prefix}_hist_lim_note", 500)
    if lim_note == 200:
        st.caption("条件が空のため、直近 200 件に制限して表示しています。")
    st.success(f"{len(rows)}件ヒットしました。")

    summary = aggregate_sales_rows_by_product(rows)
    total_amount = sum(r["売上金額"] for r in summary)
    st.markdown("**商品別集計（売上金額の大きい順）**")
    st.caption(f"{len(summary):,} 商品 / 売上合計 ¥{total_amount:,.0f}")
    summary_cols = {
        "順位": st.column_config.NumberColumn(format="%d"),
        "件数": st.column_config.NumberColumn(format="%d"),
        "売上金額": st.column_config.NumberColumn(format="¥%,.0f"),
    }
    if any("商品数" in r for r in summary):
        summary_cols["商品数"] = st.column_config.NumberColumn(format="%,.2f")
    st.dataframe(
        summary,
        use_container_width=True,
        hide_index=True,
        height=min(420, 36 * len(summary) + 48),
        column_config=summary_cols,
    )

    st.markdown("**検索結果（DB明細）**")
    disp = sales_to_display_rows(rows)
    st.dataframe(
        disp,
        use_container_width=True,
        height=min(520, max(200, 32 * min(len(disp), 18) + 40)),
        column_config={
            "売上金額": st.column_config.NumberColumn(format="¥%,.0f"),
        },
    )


def normalize_text(text: str) -> str:
    return re.sub(r"[\u3000\s]+", " ", text.strip())


def normalize_purchase_date_to_iso(date_str: str) -> str:
    """日付文字列を YYYY-MM-DD に揃える（読み取り表示・DB保存・閲覧で共通）。"""
    s = (date_str or "").strip()
    if not s:
        return ""
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return s
    m = re.fullmatch(r"(\d{2})[\s　]+(\d{1,2})[\s　]+(\d{1,2})", s)
    if m:
        y, mo, d = 2000 + int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            return datetime(y, mo, d).strftime("%Y-%m-%d")
        except ValueError:
            return s
    m = re.fullmatch(r"(\d{4})年(\d{1,2})月(\d{1,2})日?", s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            return datetime(y, mo, d).strftime("%Y-%m-%d")
        except ValueError:
            return s
    m = re.fullmatch(r"(\d{4})/(\d{1,2})/(\d{1,2})", s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            return datetime(y, mo, d).strftime("%Y-%m-%d")
        except ValueError:
            return s
    if re.fullmatch(r"\d{8}", s):
        y, mo, d = int(s[:4]), int(s[4:6]), int(s[6:8])
        try:
            return datetime(y, mo, d).strftime("%Y-%m-%d")
        except ValueError:
            return s
    if re.fullmatch(r"\d{6}", s):
        y, mo, d = 2000 + int(s[:2]), int(s[2:4]), int(s[4:6])
        try:
            return datetime(y, mo, d).strftime("%Y-%m-%d")
        except ValueError:
            return s
    return s


def crop_invoice_segment(img: Image.Image, index: int, total: int) -> Image.Image:
    width, height = img.size
    top = int((index - 1) * height / total)
    bottom = int(index * height / total)
    return img.crop((0, top, width, bottom))


def crop_invoice_by_bounds(img: Image.Image, top: int, bottom: int) -> Image.Image:
    width, height = img.size
    top = max(0, min(top, height - 1))
    bottom = max(top + 1, min(bottom, height))
    return img.crop((0, top, width, bottom))


def find_invoice_segment_bounds(img: Image.Image, max_slips: int = 6) -> list[tuple[int, int]]:
    """伝票間の白い余白で画像を分割する (top, bottom) ピクセル範囲のリスト。"""
    _w, h = img.size
    if h <= 0:
        return [(0, max(h, 1))]
    scale = min(1.0, 420 / h)
    nh = max(8, int(h * scale))
    nw = max(8, int(img.size[0] * scale))
    small = img.convert("L").resize((nw, nh))
    pixels = small.load()
    row_white_frac: list[float] = []
    for y in range(nh):
        bright = sum(1 for x in range(nw) if pixels[x, y] >= 245)
        row_white_frac.append(bright / float(nw))
    threshold = 0.88
    min_content = max(8, nh // 20)
    segments_nh: list[tuple[int, int]] = []
    y = 0
    while y < nh:
        while y < nh and row_white_frac[y] >= threshold:
            y += 1
        y0 = y
        while y < nh and row_white_frac[y] < threshold:
            y += 1
        if y - y0 >= min_content:
            segments_nh.append((y0, y))
    if not segments_nh:
        return [(0, h)]
    pad = max(2, int(h * 0.01))
    bounds: list[tuple[int, int]] = []
    for y0, y1 in segments_nh[:max_slips]:
        top = max(0, int(y0 * h / nh) - pad)
        bottom = min(h, int(y1 * h / nh) + pad)
        if bottom > top + 20:
            bounds.append((top, bottom))
    return bounds if bounds else [(0, h)]


def _invoice_group_key(row: dict) -> str:
    inv = str(row.get("伝票番号") or "").strip()
    dt = str(row.get("伝票日付") or "").strip()
    if inv or dt:
        return f"{inv}|{dt}"
    return ""


def group_parsed_rows_by_invoice(rows: list[dict[str, str]]) -> list[list[dict[str, str]]]:
    """伝票番号・日付ごとに明細行を分ける。"""
    groups: dict[str, list[dict[str, str]]] = {}
    order: list[str] = []
    for r in rows:
        k = _invoice_group_key(r) or f"__anon_{len(order)}"
        if k not in groups:
            groups[k] = []
            order.append(k)
        groups[k].append(r)
    return [groups[k] for k in order]


def count_distinct_invoice_groups(rows: list[dict[str, str]]) -> int:
    return len({k for r in rows if (k := _invoice_group_key(r))})


def _invoice_image_cache_key(img: Image.Image) -> str:
    """同一画像の再実行で AI を再呼び出ししないための軽量キー（全ピクセル走査より負荷が小さい）。"""
    w, h = img.size
    raw = img.tobytes()
    n = len(raw)
    chunk = 8192
    head = raw[: min(chunk, n)]
    tail = raw[max(0, n - chunk) :]
    hobj = hashlib.md5()
    hobj.update(f"{w}x{h}:{n}".encode())
    hobj.update(head)
    hobj.update(tail)
    return hobj.hexdigest()


def estimate_vertical_slip_count(img: Image.Image, max_slips: int = 6) -> int:
    """縦方向の白っぽい帯（伝票間の余白）を数え、伝票の段数を推定する。"""
    w, h = img.size
    if h <= 0 or w <= 0:
        return 1
    scale = min(1.0, 420 / h)
    nh = max(8, int(h * scale))
    nw = max(8, int(w * scale))
    small = img.convert("L").resize((nw, nh))
    pixels = small.load()
    row_white_frac: list[float] = []
    for y in range(nh):
        bright = 0
        for x in range(nw):
            if pixels[x, y] >= 245:
                bright += 1
        row_white_frac.append(bright / float(nw))
    threshold = 0.88
    min_run = max(4, nh // 100)
    margin = max(2, nh // 25)
    gap_bands = 0
    y = 0
    while y < nh:
        if row_white_frac[y] >= threshold:
            y0 = y
            while y < nh and row_white_frac[y] >= threshold:
                y += 1
            if y - y0 >= min_run and y0 > margin and y < nh - margin:
                gap_bands += 1
        else:
            y += 1
    slips = gap_bands + 1
    return max(1, min(slips, max_slips))


def load_invoice_image(source: Image.Image | io.BytesIO | str | bytes) -> Image.Image:
    """伝票画像を読み込み、EXIF の向きを補正して RGB にする（横撮影対応）。"""
    if isinstance(source, Image.Image):
        img = source
    else:
        img = Image.open(source)
    return ImageOps.exif_transpose(img).convert("RGB")


def _read_invoice_image_segments(
    img: Image.Image,
    bounds: list[tuple[int, int]],
    api_key: str,
    invoice_form_type: int,
    master_product_names: list[str] | None,
) -> list[tuple[int, str, list[dict[str, str]]]]:
    """各 (top,bottom) 範囲を1伝票として個別に AI 読み取り。"""
    batch: list[tuple[int, str, list[dict[str, str]]]] = []
    for i, (top, bottom) in enumerate(bounds, start=1):
        seg = crop_invoice_by_bounds(img, top, bottom)
        rows, raw = extract_invoice_data_from_image_with_ai(
            seg,
            api_key,
            invoice_form_type=invoice_form_type,
            master_product_names=master_product_names,
        )
        batch.append((i, raw, rows))
    return batch


def read_invoices_automatically(
    img: Image.Image,
    api_key: str,
    invoice_form_type: int,
    master_product_names: list[str] | None = None,
) -> tuple[list[tuple[int, str, list[dict[str, str]]]], str | None]:
    """
    複数伝票は余白で分割して1枚ずつ読み取り。単票のみのときは全画像を1回読む。
    戻り値: ([(段インデックス, AI生文字列, 明細行のリスト), ...], 経過メモ)
    """
    notes: list[str] = []
    bounds = find_invoice_segment_bounds(img)

    if len(bounds) >= 2:
        try:
            batch = _read_invoice_image_segments(
                img, bounds, api_key, invoice_form_type, master_product_names
            )
            note = f"画像に {len(bounds)} 枚の伝票を検出したため、1枚ずつ読み取りました。"
            return batch, note
        except RuntimeError as err:
            notes.append(f"分割読み取り: {err}")

    try:
        rows, raw = extract_invoice_data_from_image_with_ai(
            img,
            api_key,
            invoice_form_type=invoice_form_type,
            master_product_names=master_product_names,
        )
        groups = group_parsed_rows_by_invoice(rows)
        if len(groups) >= 2:
            note = (
                f"1回の読み取りで伝票が {len(groups)} 件混在したため、"
                "伝票番号・日付ごとに分離しました。"
            )
            return [(i, raw, g) for i, g in enumerate(groups, start=1)], note
        return [(1, raw, rows)], None
    except RuntimeError as err:
        notes.append(f"全画像での読み取り: {err}")

    n_hint = estimate_vertical_slip_count(img)
    try_order: list[int] = []
    for n in (n_hint, len(bounds), 2, 3, 4):
        if n >= 2 and n not in try_order:
            try_order.append(n)

    for nseg in try_order:
        batch: list[tuple[int, str, list[dict[str, str]]]] = []
        failed_seg: str | None = None
        for i in range(1, nseg + 1):
            seg = crop_invoice_segment(img, i, nseg)
            try:
                rows, raw = extract_invoice_data_from_image_with_ai(
                    seg,
                    api_key,
                    invoice_form_type=invoice_form_type,
                    master_product_names=master_product_names,
                )
                batch.append((i, raw, rows))
            except RuntimeError as e:
                failed_seg = f"{nseg}等分割・{i}段目: {e}"
                break
        if len(batch) == nseg:
            seg_note = f"画像を {nseg} 分割して読み取りました。"
            all_notes = notes + [seg_note]
            return batch, "\n".join(all_notes)
        if failed_seg:
            notes.append(failed_seg)

    last = notes[-1] if notes else "読み取りに失敗しました。"
    raise RuntimeError(last)


def is_zenno_shiire_form_text(text: str) -> bool:
    """帳票2（全農・仕入）の特徴。表題「仕入伝票」が欠けても判定する。"""
    if "原価金額" in text:
        return True
    if "原単価" in text and ("品名" in text or "規格" in text):
        return True
    if "全農" in text:
        return True
    return False


def is_kanto_nippon_food_invoice(text: str) -> bool:
    if is_zenno_shiire_form_text(text):
        return False
    keywords = ("関東", "日本", "フード", "関")
    return any(keyword in text for keyword in keywords)


def invoice_form_type_from_text(text: str) -> int:
    """OCR 互換: テキストから帳票種別を推定（UI 選択が優先）。"""
    if "天狗中田本店" in text:
        return 3
    if is_zenno_shiire_form_text(text):
        return 2
    if "関東" in text and "フード" in text:
        return 1
    return 1


def detect_invoice_form_type_from_ocr_raw(ocr_text: str) -> int:
    """OCR プレーンテキストから帳票1／帳票2を推定（Python のみ）。OCR 分割フロー用の互換関数。"""
    t = ocr_text or ""
    if not t.strip():
        return 1
    if "全農石川県本部" in t:
        return 2
    if "全農" in t and "石川" in t and ("県本部" in t or "石川県" in t):
        return 2
    if is_kanto_nippon_food_invoice(t) and not is_zenno_shiire_form_text(t):
        return 1
    return invoice_form_type_from_text(t)


def infer_form_type_from_segment(text_seg: str) -> int:
    """AI応答（JSON）またはその断片から帳票種別を推定。"""
    if not (text_seg and text_seg.strip()):
        return 1
    try:
        obj = _extract_json_block(text_seg)
        if isinstance(obj, dict):
            v = obj.get("伝票種別")
            if v is not None and str(v).strip() != "":
                return int(str(v).strip())
    except (ValueError, TypeError):
        pass
    return invoice_form_type_from_text(text_seg)


def _extract_json_block(text: str):
    cleaned = text.strip()
    if not cleaned:
        raise ValueError("AI応答が空です。")

    # 1) ```json ... ``` を優先して試す
    fenced_match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", cleaned, flags=re.IGNORECASE)
    candidates: list[str] = []
    if fenced_match:
        candidates.append(fenced_match.group(1).strip())

    # 2) テキスト中の最初の { または [ 以降を候補にする
    first_brace = cleaned.find("{")
    first_bracket = cleaned.find("[")
    starts = [i for i in (first_brace, first_bracket) if i != -1]
    if starts:
        candidates.append(cleaned[min(starts):].strip())

    decoder = json.JSONDecoder()
    for candidate in candidates:
        # 先頭から順に raw_decode で最初に解釈できるJSONを読む
        for i, ch in enumerate(candidate):
            if ch not in "{[":
                continue
            try:
                obj, _ = decoder.raw_decode(candidate[i:])
                if isinstance(obj, (dict, list)):
                    return obj
            except json.JSONDecodeError:
                continue

    raise ValueError("AI応答からJSONを抽出できませんでした。")


def image_to_jpeg_data_url(img: Image.Image) -> str:
    buffer = io.BytesIO()
    img.convert("RGB").save(buffer, format="JPEG", quality=90)
    return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("utf-8")


def get_openai_api_key() -> str | None:
    key = (os.getenv("OPENAI_API_KEY") or os.getenv("CHATGPT_API_KEY") or "").strip()
    return key or None


def get_openai_chat_model() -> str:
    return (
        os.getenv("OPENAI_CHAT_MODEL") or os.getenv("OPENAI_MODEL") or "gpt-4o-mini"
    ).strip()


def openai_chat_completion(
    api_key: str,
    model: str,
    messages: list[dict],
    *,
    temperature: float = 0.3,
    timeout: int = 90,
) -> str:
    """OpenAI Chat Completions（テキストのみ）。"""
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
    }
    req = request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=timeout) as res:
            response_body = json.loads(res.read().decode("utf-8"))
    except error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"OpenAI API エラー: {e.code} {detail}")
    except Exception as e:
        raise RuntimeError(f"OpenAI API 呼び出し失敗: {e}")
    content = response_body.get("choices", [{}])[0].get("message", {}).get("content", "")
    if not content:
        raise RuntimeError("OpenAI API の応答本文が空です。")
    return content.strip()


def openai_vision_chat(
    api_key: str, model: str, system_text: str, user_text: str, data_url: str, timeout: int = 60
) -> str:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_text},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_text},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            },
        ],
        "temperature": 0.0,
    }
    req = request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=timeout) as res:
            response_body = json.loads(res.read().decode("utf-8"))
    except error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"OpenAI API エラー: {e.code} {detail}")
    except Exception as e:
        raise RuntimeError(f"OpenAI API 呼び出し失敗: {e}")
    content = response_body.get("choices", [{}])[0].get("message", {}).get("content", "")
    if not content:
        raise RuntimeError("OpenAI API の応答本文が空です。")
    return content


def _vision_extract_invoice_rows(
    img: Image.Image,
    api_key: str,
    invoice_form_type: int,
    model: str,
    master_product_names: list[str] | None,
) -> tuple[list[dict[str, str]], str]:
    data_url = image_to_jpeg_data_url(img)
    prompt, system_prompt = build_invoice_extraction_prompts(
        invoice_form_type, master_product_names
    )
    content = openai_vision_chat(
        api_key,
        model,
        system_prompt,
        prompt,
        data_url,
        timeout=45,
    )
    data = _extract_json_block(content)
    parsed_rows = normalize_ai_parsed_rows(data)
    if not parsed_rows:
        raise RuntimeError("AI応答のJSONに有効な明細がありませんでした。")
    if invoice_form_type == 2:
        parsed_rows = apply_form2_postprocess(parsed_rows)
    return parsed_rows, content


def _extract_form2_with_best_orientation(
    img: Image.Image,
    api_key: str,
    model: str,
    master_product_names: list[str] | None,
) -> tuple[list[dict[str, str]], str]:
    """帳票2: 回転候補を試し、伝票番号・数量列が最も妥当な向きを採用。"""
    best_rows: list[dict[str, str]] | None = None
    best_content = ""
    best_score = -1e9
    best_angle = 0
    for angle in form2_orientation_degrees(img):
        trial_img = rotate_invoice_image(img, angle)
        try:
            rows, content = _vision_extract_invoice_rows(
                trial_img,
                api_key,
                2,
                model,
                master_product_names,
            )
        except RuntimeError:
            continue
        score = score_form2_rows(rows)
        if score > best_score:
            best_score = score
            best_rows = rows
            best_content = content
            best_angle = angle
        if score >= 42:
            break
    if best_rows is None:
        raise RuntimeError("帳票2の読み取りに失敗しました（全ての向きで明細を取得できませんでした）。")
    if best_angle:
        best_content = (
            f"{best_content}\n\n--- 向き補正: 画像を {best_angle}° 回転して読み取り ---"
        )
    return best_rows, best_content


def extract_invoice_data_from_image_with_ai(
    img: Image.Image,
    api_key: str,
    invoice_form_type: int,
    model: str = "gpt-4o-mini",
    master_product_names: list[str] | None = None,
) -> tuple[list[dict[str, str]], str]:
    if invoice_form_type == 2:
        parsed_rows, content = _extract_form2_with_best_orientation(
            img, api_key, model, master_product_names
        )
    else:
        parsed_rows, content = _vision_extract_invoice_rows(
            img, api_key, invoice_form_type, model, master_product_names
        )
    if invoice_form_type == 3:
        parsed_rows = apply_form3_postprocess(parsed_rows)
        should_retry, retry_reason = form3_should_retry_completion(parsed_rows)
        if should_retry:
            try:
                retry_data_url = image_to_jpeg_data_url(img)
                _prompt, retry_system_prompt = build_invoice_extraction_prompts(
                    invoice_form_type, master_product_names
                )
                retry_prompt = build_form3_retry_prompt(len(parsed_rows), retry_reason)
                content_retry = openai_vision_chat(
                    api_key,
                    model,
                    retry_system_prompt,
                    retry_prompt,
                    retry_data_url,
                    timeout=75,
                )
                data_retry = _extract_json_block(content_retry)
                rows_retry = apply_form3_postprocess(normalize_ai_parsed_rows(data_retry))
                if len(rows_retry) > len(parsed_rows):
                    parsed_rows = merge_form3_detail_rows(parsed_rows, rows_retry)
                    content = f"{content}\n\n--- 帳票3・明細再読み取り ---\n\n{content_retry}"
            except Exception:
                pass
    if master_product_names:
        uniq = list(dict.fromkeys([n for n in master_product_names if (n or "").strip()]))
        mset = set(uniq)
        if uniq:
            for row in parsed_rows:
                row["商品名"] = snap_product_name_to_master(row.get("商品名", ""), uniq, mset)
    return parsed_rows, content


def normalize_ai_parsed_rows(ai_obj) -> list[dict[str, str]]:
    def _pick_date(item: dict, parent: dict) -> str:
        return (
            item.get("伝票日付")
            or item.get("日付")
            or parent.get("伝票日付")
            or parent.get("日付")
            or ""
        )

    def normalize_row(item: dict, idx: int, parent: dict | None = None) -> dict[str, str]:
        parent = parent or {}
        date_raw = _pick_date(item, parent)
        inv_raw = item.get("伝票番号") or parent.get("伝票番号") or ""
        note_raw = item.get("備考") or item.get("Note") or item.get("note") or ""
        piece_raw = item.get("個数")
        weight_raw = item.get("重量")
        qty_raw = item.get("数量")
        return {
            "明細番号": normalize_text(str(item.get("明細番号", "")).strip()) if item.get("明細番号") else str(idx),
            "商品名": normalize_text(str(item.get("商品名", "")).strip()) if item.get("商品名") else "検出できませんでした",
            "個数": normalize_text(str(piece_raw).strip()) if piece_raw not in (None, "") else "",
            "重量": normalize_text(str(weight_raw).strip()) if weight_raw not in (None, "") else "",
            "数量": normalize_text(str(qty_raw).strip()) if qty_raw else "検出できませんでした",
            "単価": normalize_text(str(item.get("単価", "")).strip()) if item.get("単価") else "検出できませんでした",
            "金額": normalize_text(
                str(item.get("金額", "") or item.get("合計金額", "")).strip()
            )
            if (item.get("金額") or item.get("合計金額"))
            else "検出できませんでした",
            "備考": normalize_text(str(note_raw).strip()) if note_raw else "",
            "伝票番号": normalize_text(str(inv_raw).strip()) if inv_raw else "",
            "伝票日付": normalize_purchase_date_to_iso(normalize_text(str(date_raw).strip())) if date_raw else "",
            "取引先": normalize_text(str(item.get("取引先", "") or parent.get("取引先", "")).strip()) if (item.get("取引先") or parent.get("取引先")) else "",
        }

    rows: list[dict[str, str]] = []
    if isinstance(ai_obj, list):
        for inv_i, item in enumerate(ai_obj, start=1):
            if not isinstance(item, dict):
                continue
            details = item.get("明細")
            if isinstance(details, list) and details:
                for j, detail in enumerate(details, start=1):
                    if isinstance(detail, dict):
                        rows.append(normalize_row(detail, j, parent=item))
            else:
                rows.append(normalize_row(item, inv_i))
    elif isinstance(ai_obj, dict):
        details = ai_obj.get("明細")
        if isinstance(details, list) and details:
            for i, detail in enumerate(details, start=1):
                if isinstance(detail, dict):
                    rows.append(normalize_row(detail, i, parent=ai_obj))
        else:
            rows.append(normalize_row(ai_obj, 1))
    return rows


st.set_page_config(
    page_title="仕入・売上管理",
    page_icon="📄",
    layout="wide",
)

if "session_purchases" not in st.session_state:
    st.session_state.session_purchases = []
if "session_purchase_id" not in st.session_state:
    st.session_state.session_purchase_id = 0

# 認証キーがある場合は、最初にログイン／サインアップ。成功後にメインアプリへ。
if supabase_auth_configured() and not is_supabase_logged_in():
    login_signup_page()
    st.stop()

if supabase_auth_configured() and is_supabase_logged_in():
    refresh_sb_user_id_from_token()

st.sidebar.title("メニュー")
nav_section, page = render_sidebar_navigation()

st.sidebar.divider()
if supabase_auth_configured() and is_supabase_logged_in():
    em = st.session_state.get("user_email") or "（ログイン中）"
    st.sidebar.caption(f"アカウント: {em}")
    if st.sidebar.button("ログアウト", key="sidebar_logout"):
        sign_out()
        st.rerun()
else:
    st.sidebar.caption(
        "Supabase の公開キーが未設定のため、ログインなしで利用中です。"
        "DB 保存は `SUPABASE_SERVICE_ROLE_KEY` などで接続できる場合に限ります。"
    )

if nav_section == "仕入" and page == "伝票読み取り":
    st.title("📄 伝票 AI 読み取り")
    st.caption(
        "まず帳票の種類を選び、画像を送ると JSON で項目を抽出します。"
        "縦に複数枚ある場合はまとめて読み取ります。うまくいかないときは白い隙間から段数を推定して分割再試行します。"
        "環境変数 `OPENAI_API_KEY` または `CHATGPT_API_KEY` が必要です（API利用料が発生します）。"
    )

    st.subheader("帳票の種類")
    invoice_form_type = st.selectbox(
        "読み取り前に帳票を選んでください（AI は選んだ種類のルールだけで読み取ります）",
        options=sorted(INVOICE_FORM_OPTIONS.keys()),
        format_func=lambda x: INVOICE_FORM_OPTIONS[x]["label"],
        key="invoice_form_type",
    )
    st.caption(
        f"選択中: 伝票種別={invoice_form_type}　取引先の既定値: "
        f"「{default_supplier_for_form_type(invoice_form_type)}」"
    )

    st.markdown(
        """
        <style>
        .st-key-invoice_live_camera,
        .st-key-invoice_live_camera iframe {
            width: 100% !important;
            max-width: 100% !important;
        }
        @media (max-width: 1024px) {
            section.main .block-container {
                padding-top: 0.5rem !important;
                padding-left: 0.25rem !important;
                padding-right: 0.25rem !important;
                max-width: 100% !important;
            }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    tab1, tab2 = st.tabs(["📷 カメラ", "📁 ファイル"])
    image = None
    with tab1:
        st.caption(
            "画面内で撮影し **「撮影」** を押すと、その場で AI 読み取りが始まります。"
            "背面カメラが使えない場合は **🔄 カメラ切替** を押してください。"
        )
        live_capture = capture_invoice_camera_image(key="invoice_live_camera")
        if live_capture:
            image = load_invoice_image(live_capture)
            st.success("撮影しました。AI 読み取りを開始します。")
    with tab2:
        uploaded_file = st.file_uploader("画像", type=["png", "jpg", "jpeg", "bmp", "tiff"])
        if uploaded_file:
            image = load_invoice_image(uploaded_file)

    if not image:
        st.info("カメラで撮影するか、画像をアップロードしてください。")
        st.stop()

    st.sidebar.divider()
    with st.sidebar.expander("表示オプション", expanded=False):
        show_ai_json = st.checkbox("AIの生応答を表示", value=False)
        parse_to_table = st.checkbox("表に展開する", value=True)

    openai_api_key = os.getenv("OPENAI_API_KEY") or os.getenv("CHATGPT_API_KEY")
    if not openai_api_key:
        st.error("`OPENAI_API_KEY` または `CHATGPT_API_KEY` を `.env` などに設定してください。")
        st.stop()

    work_image = image if isinstance(image, Image.Image) else load_invoice_image(image)

    img_fp = _invoice_image_cache_key(work_image)
    api_fp = hashlib.md5(openai_api_key.encode("utf-8")).hexdigest()[:16]
    ai_cache_key = f"{img_fp}:{api_fp}:form{invoice_form_type}"

    cached_key = st.session_state.get("invoice_ai_read_cache_key")
    cached_rows = st.session_state.get("invoice_ai_read_results")
    if cached_key == ai_cache_key and isinstance(cached_rows, list):
        invoice_texts = cached_rows
        auto_read_note = st.session_state.get("invoice_ai_read_note")
        combined_ai = st.session_state.get("invoice_ai_combined_ai") or ""
    else:
        master_for_ai: list[str] = []
        pc_ai = get_supabase_client_for_writes()
        if pc_ai:
            catalog_ai, err_ai = fetch_product_catalog(pc_ai)
            st.session_state.product_catalog_cache = catalog_ai
            st.session_state.product_names_fetch_error = err_ai
            master_for_ai = product_names_for_row(
                catalog_ai, partner_hint_for_form_type(invoice_form_type)
            )
        with st.spinner(
            f"AIで読み取り中（帳票{invoice_form_type}・必要なら自動分割）…"
        ):
            try:
                invoice_texts, auto_read_note = read_invoices_automatically(
                    work_image,
                    openai_api_key,
                    invoice_form_type,
                    master_product_names=master_for_ai or None,
                )
            except RuntimeError as err:
                st.error(err)
                st.stop()

            combined_ai = "\n\n---\n\n".join(
                f"【{idx}段目】\n{ai_raw}" for idx, ai_raw, _ in invoice_texts
            )
        st.session_state.invoice_ai_read_cache_key = ai_cache_key
        st.session_state.invoice_ai_read_results = invoice_texts
        st.session_state.invoice_ai_read_note = auto_read_note
        st.session_state.invoice_ai_combined_ai = combined_ai

    if auto_read_note:
        if "1枚ずつ" in auto_read_note or "分離" in auto_read_note:
            st.info(auto_read_note)
        else:
            with st.expander("読み取りの経過（全画像失敗後に分割を試行しました）", expanded=False):
                st.text(auto_read_note)

    results: list[dict] = []
    if parse_to_table:
        for idx, ai_raw, direct_rows in invoice_texts:
            if not direct_rows:
                continue
            parsed = direct_rows[0]
            supplier_default = default_supplier_for_form_type(invoice_form_type)
            for item in direct_rows:
                item["取引先"] = supplier_default
            for item in direct_rows:
                inv_no = item.get("伝票番号", "") or parsed.get("伝票番号", "")
                mid = str(item.get("明細番号", ""))
                row_key = f"{idx}-{inv_no}-{mid}".strip("-")
                raw_date = item.get("伝票日付", "") or parsed.get("伝票日付", "")
                amount_value = item.get("金額", "")
                if invoice_form_type == 1:
                    amount_value = normalize_form1_amount_display(str(amount_value))
                results.append(
                    {
                        "伝票番号": inv_no,
                        "日付": normalize_purchase_date_to_iso(str(raw_date).strip()),
                        "取引先": item.get("取引先", "") or parsed.get("取引先", ""),
                        "明細番号": item.get("明細番号", ""),
                        "商品名": item.get("商品名", ""),
                        "数量": item.get("数量", ""),
                        "単価": item.get("単価", ""),
                        "合計金額": amount_value,
                        "備考": item.get("備考", ""),
                        "伝票番号(枚)": row_key or str(idx),
                        "ai_response": ai_raw,
                        "product_id": None,
                    }
                )
        catalog_for_ids = list(st.session_state.get("product_catalog_cache") or [])
        if catalog_for_ids:
            attach_product_ids_to_rows(results, catalog_for_ids)

    if parse_to_table and invoice_form_type == 3 and results:
        nums = _form3_line_numbers(results)
        if nums:
            missing = [i for i in range(1, max(nums) + 1) if i not in nums]
            if missing:
                st.warning(
                    f"明細番号に欠番があります: {missing}。表の行数と一致するか画像で確認してください。"
                )
        if len(results) <= 8:
            st.info(
                "帳票3: 明細が8件以下です。表に9行以上ある場合は再撮影するか、"
                "「修正・保存」で不足行を手入力してください。"
            )
        piece_like: list[str] = []
        for row in results:
            q = parse_money_value(row.get("数量"))
            pv = parse_money_value(row.get("個数"))
            if q is not None and (
                _form3_qty_looks_like_piece_count(q)
                or (pv is not None and abs(q - pv) < 0.01)
            ):
                piece_like.append(str(row.get("明細番号") or "?"))
        if piece_like:
            st.warning(
                "帳票3: 数量が個数欄（1,2,3…）のままの行があります。"
                f" 明細番号 {', '.join(piece_like)} は **重量(kg)** 欄を確認してください。"
            )

    if parse_to_table and invoice_form_type == 1 and results:
        circled_pattern = re.compile(r"[①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳]")
        marker_keys: list[str] = []
        missing_marker_rows: list[str] = []
        for row in results:
            mid = str(row.get("明細番号") or "").strip()
            m = circled_pattern.search(mid)
            if m:
                marker_keys.append(m.group())
            else:
                missing_marker_rows.append(mid or "?")
        if missing_marker_rows:
            st.warning(
                "帳票1: 明細番号(①②③…)が読めていない行があります。"
                f" 行: {', '.join(missing_marker_rows)}。商品数確認キーなので画像を再確認してください。"
            )
        if marker_keys:
            dup = sorted({k for k in marker_keys if marker_keys.count(k) > 1})
            if dup:
                st.warning(
                    "帳票1: 明細番号(①②③…)の重複があります。"
                    f" 重複: {', '.join(dup)}。商品行の読み取り漏れ/取り違えを確認してください。"
                )

    if parse_to_table:
        if results:
            st.subheader("🔎 読み取り結果")
            st.dataframe(results, use_container_width=True)
        else:
            st.warning("表にできる結果がありませんでした。AI応答を確認してください。")

    if show_ai_json:
        st.subheader("📝 AI応答（生データ）")
        st.text_area("JSON", value=combined_ai, height=320, label_visibility="collapsed")

    if parse_to_table and results:
        read_session_key = hash(combined_ai)
        should_reset = (
            "edited_results" not in st.session_state
            or st.session_state.get("edited_read_session_key") != read_session_key
            or len(st.session_state.get("edited_results", [])) != len(results)
        )
        if should_reset:
            st.session_state.edited_results = [r.copy() for r in results]
            st.session_state.edited_read_session_key = read_session_key
            st.session_state.pop("product_names_cache", None)
            st.session_state.pop("product_catalog_cache", None)
            st.session_state.pop("product_names_fetch_error", None)

        pc = get_supabase_client_for_writes()
        if pc and "product_catalog_cache" not in st.session_state:
            catalog, fetch_err = fetch_product_catalog(pc)
            st.session_state.product_catalog_cache = catalog
            st.session_state.product_names_fetch_error = fetch_err
        product_catalog = list(st.session_state.get("product_catalog_cache") or [])
        fetch_err = st.session_state.get("product_names_fetch_error")

        st.subheader("✏️ 修正・保存")
        c_head, c_btn = st.columns([4, 1])
        with c_head:
            if pc and fetch_err:
                st.warning(f"商品マスタの取得エラー: {fetch_err}")
            elif pc and not product_catalog:
                st.warning(
                    f"`{SUPABASE_TABLE_PRODUCTS}` から有効な行が0件でした。"
                    f"列名（`SUPABASE_PRODUCTS_NAME_COLUMN` / `SUPABASE_PRODUCTS_SUPPLIER_COLUMN`）と "
                    f"`{SUPABASE_TABLE_SUPPLIERS}` の紐付けを確認してください。"
                    "RLS で SELECT が拒否されている場合は `purchase_products` に **authenticated 向け SELECT ポリシー**を追加してください。"
                )
        with c_btn:
            if pc and st.button("商品マスタ再読込", key="reload_product_names"):
                st.session_state.pop("product_names_cache", None)
                st.session_state.pop("product_catalog_cache", None)
                st.session_state.pop("product_names_fetch_error", None)
                st.rerun()
        if pc and product_catalog:
            st.caption(
                f"商品マスタ: {len(product_catalog)}件（テーブル `{SUPABASE_TABLE_PRODUCTS}` + `{SUPABASE_TABLE_SUPPLIERS}`）。"
                "選択した取引先に応じて supplier_id で商品候補を絞り込みます。"
            )
        for row_idx, row in enumerate(st.session_state.edited_results):
            with st.expander(f"{row.get('伝票番号(枚)', '?')}", expanded=False):
                cols_edit = st.columns(2)
                for fidx, field in enumerate(
                    [
                        "伝票番号",
                        "日付",
                        "取引先",
                        "明細番号",
                        "商品名",
                        "数量",
                        "単価",
                        "合計金額",
                        "備考",
                    ]
                ):
                    col = cols_edit[fidx % 2]
                    with col:
                        if field == "商品名":
                            rk = row.get("伝票番号(枚)", str(row_idx))
                            row[field] = render_product_name_with_catalog(
                                row_idx,
                                rk,
                                row.get(field, ""),
                                product_catalog,
                                row.get("取引先", ""),
                            )
                            row["product_id"] = resolve_product_id_from_catalog(
                                row[field],
                                row.get("取引先", ""),
                                product_catalog,
                            )
                            _show_product_id_status(
                                row[field],
                                row.get("product_id"),
                                product_code_from_catalog(row.get("product_id"), product_catalog),
                            )
                        else:
                            edit_key = f"edit_{row_idx}_{row.get('伝票番号(枚)', fidx)}_{field}"
                            if field == "合計金額":
                                calc_amount = calc_amount_from_qty_unit(
                                    str(row.get("数量", "")),
                                    str(row.get("単価", "")),
                                )
                                if calc_amount is not None:
                                    row[field] = calc_amount
                                else:
                                    row[field] = _strip_commas_text(str(row.get(field, ""))).strip()
                                st.session_state[edit_key] = row[field]
                                row[field] = st.text_input(
                                    field,
                                    value=row[field],
                                    key=edit_key,
                                    disabled=True,
                                    help="自動計算: 数量 × 単価",
                                )
                            else:
                                row[field] = st.text_input(
                                    field,
                                    value=row.get(field, ""),
                                    key=edit_key,
                                )
                row["ai_response"] = st.text_area(
                    "AI応答",
                    value=row.get("ai_response", ""),
                    height=100,
                    key=f"ai_raw_{row_idx}_{row.get('伝票番号(枚)', fidx)}",
                )
                normalized = sanitize_edit_row_and_recalc_amount(row)
                for k, v in normalized.items():
                    row[k] = v

        if st.button("DBに保存"):
            rows_to_save = list(st.session_state.get("edited_results", results))
            rows_to_save = [sanitize_edit_row_and_recalc_amount(r) for r in rows_to_save]
            client = get_supabase_client_for_writes()
            if not client:
                st.error(
                    "Supabase に接続できません。`.env` に `SUPABASE_URL` と "
                    "`SUPABASE_SERVICE_ROLE_KEY`（未ログイン時）または "
                    "`SUPABASE_KEY` / `SUPABASE_ANON_KEY`（ログイン・認証用）を設定してください。"
                )
            else:
                try:
                    attach_product_ids_to_rows(rows_to_save, product_catalog)
                    payloads = rows_to_supabase_payloads(rows_to_save, product_catalog)
                    inserted = insert_purchases_to_supabase(client, payloads)
                    saved = len(rows_to_save)

                    def _append_from_supabase_row(ins: dict, src_row: dict | None = None) -> None:
                        enriched = enrich_purchase_rows([ins], product_catalog)
                        e = enriched[0] if enriched else ins
                        src = src_row or {}
                        st.session_state.session_purchases.append(
                            {
                                "id": e.get("id"),
                                "invoice_number": e.get("invoice_number") or "",
                                "purchase_date": normalize_purchase_date_to_iso(
                                    str(e.get("purchase_date") or "").strip()
                                ),
                                "supplier": e.get("supplier") or src.get("取引先", ""),
                                "product_name": e.get("product_name") or src.get("商品名", ""),
                                "product_id": e.get(SUPABASE_PURCHASES_PRODUCT_ID_COLUMN)
                                or e.get("product_id"),
                                "quantity": e.get("quantity") or "",
                                "unit_price": e.get("unit_price") or "",
                                "amount": e.get("amount") or "",
                                "ocr_text": e.get("ocr_text") or "",
                                "note": purchase_note_from_db_row(e) or src.get("備考", ""),
                            }
                        )

                    if isinstance(inserted, list) and len(inserted) == len(rows_to_save):
                        for ins, src in zip(inserted, rows_to_save):
                            _append_from_supabase_row(ins, src)
                    else:
                        for row in rows_to_save:
                            st.session_state.session_purchase_id += 1
                            rec = {
                                "id": st.session_state.session_purchase_id,
                                "invoice_number": row.get("伝票番号", ""),
                                "purchase_date": normalize_purchase_date_to_iso(
                                    str(row.get("日付", "")).strip()
                                ),
                                "supplier": row.get("取引先", ""),
                                "product_name": row.get("商品名", ""),
                                "product_id": row.get("product_id"),
                                "quantity": row.get("数量", ""),
                                "unit_price": row.get("単価", ""),
                                "amount": row.get("合計金額", ""),
                                "ocr_text": row.get("ai_response", ""),
                                "note": row.get("備考", ""),
                            }
                            st.session_state.session_purchases.append(rec)

                    st.success(
                        f"{saved}件を Supabase のテーブル「{SUPABASE_TABLE_PURCHASES}」に保存しました。"
                        "「DB閲覧」または「購入履歴」で確認できます。"
                    )
                except Exception as err:
                    err_text = str(err)
                    st.error(f"Supabase への保存に失敗しました: {err_text}")
                    if "row-level security" in err_text.lower() or "42501" in err_text:
                        with st.expander("RLS エラーの直し方（Supabase 側）", expanded=True):
                            render_supabase_rls_error_help()

    st.subheader("📷 入力画像")
    st.image(image, use_container_width=True)

elif nav_section == "仕入" and page == "購入履歴":
    st.title("📚 購入履歴（Supabase）")
    st.caption(
        "保存済みの purchases を検索します。左の「仕入」メニューから他画面に切り替えられます。"
    )
    render_supabase_purchases_search("menu_hist", compact=False)

elif nav_section == "仕入" and page == "ダッシュボード":
    render_dashboard_page()

elif nav_section == "仕入" and page == "DB閲覧":
    st.title("📊 DB閲覧")
    st.caption(
        "「DBに保存」で Supabase に送ったうち、このセッションに載せた分のみ表示します。"
        "Supabase 上の過去データはメニューの「購入履歴」から参照してください。"
    )
    data = list(st.session_state.get("session_purchases") or [])
    if data:
        display_rows = [
            {
                **row,
                "purchase_date": normalize_purchase_date_to_iso(str(row.get("purchase_date", "")).strip()),
            }
            for row in data
        ]
        st.dataframe(
            display_rows,
            use_container_width=True,
            height=min(560, max(220, 36 * len(display_rows) + 48)),
        )
    else:
        st.info("まだこのセッションで保存したデータはありません。「伝票読み取り」で保存してください。")

elif nav_section == "売上" and page == "CSV取り込み":
    render_sales_csv_import_page()

elif nav_section == "売上" and page == "売上履歴":
    st.title("📚 売上履歴（Supabase）")
    st.caption(
        f"保存済みの `{SUPABASE_TABLE_SALES}` を検索します。左の「売上」メニューから他画面に切り替えられます。"
    )
    render_supabase_sales_search("menu_sales_hist", compact=False)

elif nav_section == "売上" and page == "ダッシュボード":
    render_sales_dashboard_page()

elif nav_section == ANALYST_NAV_SECTION:
    render_analyst_page()

else:
    st.warning(f"未対応のページです: {nav_section} / {page}")

