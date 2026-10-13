# -*- coding: utf-8 -*-
# 店舗情報 検索システム（レベル2：検索窓1つ → カード表示）
#
# このファイルがアプリ本体です。
# ・ローカルでは 同じフォルダの 店舗.csv / 商品.csv を読みます
# ・本番（Streamlit Cloud）では Googleスプレッドシート「店舗台帳」を読みます
# どちらで動くかは自動で切り替わります。

import html
import re
import unicodedata

import pandas as pd
import streamlit as st

# スプレッドシートのID（URLの /d/ と /edit の間の文字列）と、その中のタブ名
#   名前ではなくIDで開くので、シート名を変えても（★を付けても）壊れません
SPREADSHEET_ID = "1Bh-JD0SSsiSq4XrJdD0nrM1otKkFt_LuaLaC3oEcfgA"
SHEET_STORE = "店舗"
SHEET_PRODUCT = "商品"

# ヒット件数がこの数以下なら「大きいカード」、超えたら「小さいタイル」で表示
BIG_CARD_LIMIT = 3


# ------------------------------------------------------------
# 文字ならし：表記ゆれがあっても当たるようにする
#   ・全角/半角をそろえる（NFKC）
#   ・大文字/小文字をそろえる
#   ・空白と記号を消す
# ------------------------------------------------------------
def normalize(value) -> str:
    s = unicodedata.normalize("NFKC", str(value))  # 全角→半角などを統一
    s = s.lower()                                   # 大文字小文字をそろえる
    # 英数字・ひらがな・カタカナ・漢字 以外（空白・記号）を取り除く
    s = re.sub(r"[^0-9a-z぀-ゟ゠-ヿ一-鿿]", "", s)
    return s


# 読み書き両方できる権限（登録・編集で台帳に書き込むため）
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]


# ------------------------------------------------------------
# スプレッドシートの『店舗』『商品』タブへの接続
#   @st.cache_resource … 接続は1回作って使い回す
#   本番用の設定(secrets)が無ければ (None, None) を返す＝ローカルCSVモード
# ------------------------------------------------------------
@st.cache_resource
def get_worksheets():
    try:
        import gspread
        from google.oauth2.service_account import Credentials

        creds = Credentials.from_service_account_info(
            st.secrets["gcp_service_account"], scopes=SCOPES
        )
        gc = gspread.authorize(creds)
        sh = gc.open_by_key(SPREADSHEET_ID)
        return sh.worksheet(SHEET_STORE), sh.worksheet(SHEET_PRODUCT)
    except Exception:
        return None, None


def can_write():
    # スプレッドシートに接続できていれば、書き込み機能（編集フォーム）を出す
    ws_store, _ = get_worksheets()
    return ws_store is not None


# ------------------------------------------------------------
# データ読み込み：本番はスプレッドシート、無ければCSV
#   @st.cache_data … 一度読んだら覚えておき、毎回読み直さず速くする
# ------------------------------------------------------------
@st.cache_data(ttl=300)
def load_data():
    ws_store, ws_product = get_worksheets()
    if ws_store is not None:
        df_store = pd.DataFrame(ws_store.get_all_records())
        df_product = pd.DataFrame(ws_product.get_all_records())
        source = "Googleスプレッドシート"
    else:
        # 設定が無い／接続できないときは、同じフォルダのCSVを読む（ローカル開発用）
        df_store = pd.read_csv("店舗.csv", dtype=str, encoding="utf-8-sig")
        df_product = pd.read_csv("商品.csv", dtype=str, encoding="utf-8-sig")
        source = "ローカルCSV"

    # すべて文字として扱い、空欄は空文字にそろえる
    df_store = df_store.fillna("").astype(str)
    df_product = df_product.fillna("").astype(str)
    return df_store, df_product, source


# ------------------------------------------------------------
# 書き込み：店舗情報の更新／商品の追加
# ------------------------------------------------------------
def _find_store_row(ws_store, no):
    # A列（店舗番号）を上から見て、ぴったり一致する行番号を返す（見出しが1行目）
    for i, v in enumerate(ws_store.col_values(1), start=1):
        if str(v).strip() == str(no).strip():
            return i
    return None


def update_store(no, name, furikomi, haitatsu, houhou, hi):
    ws_store, _ = get_worksheets()
    r = _find_store_row(ws_store, no)
    if r is None:
        raise ValueError("店舗番号が見つかりませんでした")
    # B列〜F列（店舗名・振込名義・配達者名・支払い方法・支払い日）をまとめて更新
    ws_store.update([[name, furikomi, haitatsu, houhou, hi]], f"B{r}:F{r}")


def add_product(no, product_name):
    _, ws_product = get_worksheets()
    ws_product.append_row([str(no), product_name])


# 「項目名」→ スプレッドシートの列（A=店舗番号, B=店舗名, …）
COL_LETTER = {
    "店舗名": "B",
    "振込名義": "C",
    "配達者名": "D",
    "支払い方法": "E",
    "支払い日": "F",
}


def bulk_update_field(field, value, store_numbers):
    """選んだ店たちの、ある1項目を、同じ値でまとめて書き換える"""
    ws_store, _ = get_worksheets()
    col_letter = COL_LETTER[field]

    # 店舗番号 → 行番号 の対応表を一度だけ作る（A列を上から）
    rowmap = {}
    for i, v in enumerate(ws_store.col_values(1), start=1):
        key = str(v).strip()
        if key and key not in rowmap:
            rowmap[key] = i

    # まとめて1回の通信で書き込む（速い）
    data = []
    for no in store_numbers:
        r = rowmap.get(str(no).strip())
        if r:
            data.append({"range": f"{col_letter}{r}", "values": [[value]]})
    if data:
        ws_store.batch_update(data)
    return len(data)


# ------------------------------------------------------------
# 検索：店舗タブの全列 ＋ 商品タブの商品名 から部分一致で探す
# ------------------------------------------------------------
def search(keyword, df_store, df_product):
    key = normalize(keyword)
    if key == "":
        return df_store.iloc[0:0]  # 空っぽ（＝ヒットなし）を返す

    # 店舗番号ごとに、その店の商品名をまとめた辞書を作る
    products_by_store = {}
    for _, row in df_product.iterrows():
        no = row.get("店舗番号", "")
        products_by_store.setdefault(no, []).append(row.get("商品名", ""))

    # 数字だけ入力されたら「店舗番号の検索」とみなす（→ 完全一致で1件に絞る）
    key_is_number = key.isdigit()

    hit_rows = []
    for _, row in df_store.iterrows():
        no = row.get("店舗番号", "")

        if key_is_number:
            # 店舗番号は完全一致だけ（113 で 1130 などを拾わない）
            if normalize(no) == key:
                hit_rows.append(row)
        else:
            # 文字検索は今までどおり部分一致（店舗の全列＋その店の商品名）
            parts = [str(v) for v in row.values]
            parts += products_by_store.get(no, [])
            haystack = normalize("".join(parts))
            if key in haystack:
                hit_rows.append(row)

    if hit_rows:
        return pd.DataFrame(hit_rows)
    return df_store.iloc[0:0]


# ------------------------------------------------------------
# 見た目（CSS）：デフォルト感をなくしてカッコよくする
# ------------------------------------------------------------
STYLE = """
<style>
/* 全体の横幅と余白 */
.block-container { max-width: 960px; padding-top: 2.2rem; }
/* 画面上の余計なツールバー・フッターを隠す */
[data-testid="stToolbar"], footer, #MainMenu { visibility: hidden; }
/* 「Press Enter to submit form」の案内を消す */
[data-testid="InputInstructions"] { display: none; }

/* 見出し */
.app-title { font-size: 1.9rem; font-weight: 800; letter-spacing: .02em;
             color: #1f2430; margin: 0 0 .2rem 0; }
.app-sub   { color: #8a8f9c; font-size: .9rem; margin: 0 0 1.1rem 0; }

/* 検索ボックス */
div[data-testid="stTextInput"] input {
  font-size: 1.15rem; padding: .85rem 1.1rem; border-radius: 14px;
  border: 1px solid #e3e5ec; background: #fff;
}
div[data-testid="stTextInput"] input:focus {
  border-color: #4f46e5; box-shadow: 0 0 0 3px rgba(79,70,229,.15);
}
/* 検索ボタン */
div[data-testid="stFormSubmitButton"] button {
  background: linear-gradient(135deg, #6366f1, #4f46e5);
  color: #fff; border: none; border-radius: 14px; padding: .7rem;
  font-weight: 700; font-size: 1rem; transition: filter .15s;
}
div[data-testid="stFormSubmitButton"] button:hover { filter: brightness(1.07); }

/* 「最新に更新」ボタン（検索ボタンとは別の控えめな見た目） */
div.stButton > button {
  border-radius: 10px; border: 1px solid #d9dbe3; background: #fff;
  color: #4f46e5; font-weight: 600; padding: .35rem .6rem; font-size: .85rem;
}
div.stButton > button:hover { border-color: #4f46e5; background: #f5f5ff; }

/* ヒット件数 */
.result-count { color: #6b7280; font-size: .9rem; margin: 1.2rem 0 .6rem; }

/* カード共通 */
.card { background: #fff; border: 1px solid #ececf1; border-radius: 16px;
        box-shadow: 0 1px 3px rgba(20,20,40,.06); transition: transform .12s, box-shadow .12s; }
.card:hover { transform: translateY(-2px); box-shadow: 0 6px 18px rgba(20,20,40,.10); }
.card .head { display: flex; align-items: center; gap: .6rem; }
.badge { background: linear-gradient(135deg, #6366f1, #4f46e5); color: #fff;
         font-weight: 700; font-size: .78rem; padding: .18rem .6rem; border-radius: 999px;
         white-space: nowrap; }
.name { font-weight: 700; color: #1f2430; }
.lbl  { color: #9aa0ad; font-size: .72rem; margin-right: .35rem; }
.val  { color: #2b303c; }
.chip { display: inline-block; background: #eef1f8; color: #4b5563;
        border-radius: 999px; padding: .12rem .6rem; font-size: .78rem;
        margin: .12rem .25rem .12rem 0; }

/* 大きいカード（ヒットが少ないとき） */
.cards-big { display: flex; flex-direction: column; gap: 16px; }
.card.big { padding: 20px 22px; }
.card.big .name { font-size: 1.35rem; }
.card.big .info { display: grid; grid-template-columns: 1fr 1fr;
                  gap: 8px 28px; margin: 14px 0 10px; }
.card.big .prod { border-top: 1px dashed #eceef3; padding-top: 10px; }

/* 小さいタイル（ヒットが多いとき）：自動で折り返して並ぶ */
.cards-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
              gap: 14px; }
.card.small { padding: 14px 16px; }
.card.small .name { font-size: 1.02rem; }
.card.small .mini { color: #6b7280; font-size: .82rem; margin: 8px 0; line-height: 1.5; }
.card.small .chips { margin-top: 4px; }
.card.small .chip { font-size: .74rem; }
.more { color: #8a8f9c; font-size: .74rem; }
</style>
"""


# ------------------------------------------------------------
# カードのHTMLを組み立てる
# ------------------------------------------------------------
def big_card_html(row, products):
    def e(k):
        return html.escape(row.get(k, ""))

    chips = "".join(f'<span class="chip">{html.escape(p)}</span>' for p in products)
    if not chips:
        chips = '<span class="lbl">（商品なし）</span>'

    return (
        '<div class="card big">'
        f'<div class="head"><span class="badge">No.{e("店舗番号")}</span>'
        f'<span class="name">{e("店舗名")}</span></div>'
        '<div class="info">'
        f'<div><span class="lbl">振込名義</span><span class="val">{e("振込名義")}</span></div>'
        f'<div><span class="lbl">配達者</span><span class="val">{e("配達者名")}</span></div>'
        f'<div><span class="lbl">支払い方法</span><span class="val">{e("支払い方法")}</span></div>'
        f'<div><span class="lbl">支払い日</span><span class="val">{e("支払い日")}</span></div>'
        '</div>'
        f'<div class="prod"><span class="lbl">取扱商品</span>{chips}</div>'
        '</div>'
    )


def small_card_html(row, products):
    def e(k):
        return html.escape(row.get(k, ""))

    shown = products[:4]
    chips = "".join(f'<span class="chip">{html.escape(p)}</span>' for p in shown)
    if len(products) > 4:
        chips += f'<span class="more">+{len(products) - 4}</span>'
    if not chips:
        chips = '<span class="lbl">（商品なし）</span>'

    return (
        '<div class="card small">'
        f'<div class="head"><span class="badge">No.{e("店舗番号")}</span>'
        f'<span class="name">{e("店舗名")}</span></div>'
        f'<div class="mini"><span class="lbl">配達</span>{e("配達者名")}'
        f'　<span class="lbl">支払</span>{e("支払い方法")} {e("支払い日")}</div>'
        f'<div class="chips">{chips}</div>'
        '</div>'
    )


def products_of(no, df_product):
    return df_product[df_product["店舗番号"] == no]["商品名"].tolist()


# ------------------------------------------------------------
# 登録・編集フォーム（カードの下に出す）
# ------------------------------------------------------------
def render_edit_form(row):
    no = row.get("店舗番号", "")
    with st.expander("✏️ この店に登録・編集する"):
        with st.form(f"edit_{no}"):
            name = st.text_input("店舗名", value=row.get("店舗名", ""))
            c1, c2 = st.columns(2)
            with c1:
                furikomi = st.text_input("振込名義", value=row.get("振込名義", ""))
                houhou = st.text_input("支払い方法", value=row.get("支払い方法", ""))
            with c2:
                haitatsu = st.text_input("配達者名", value=row.get("配達者名", ""))
                hi = st.text_input("支払い日", value=row.get("支払い日", ""))
            new_product = st.text_input(
                "商品を追加（任意・1つ）", placeholder="例：肉まん"
            )
            saved = st.form_submit_button(
                "保存する", type="primary", use_container_width=True
            )

        if saved:
            try:
                update_store(no, name, furikomi, haitatsu, houhou, hi)
                if new_product.strip():
                    add_product(no, new_product.strip())
                st.cache_data.clear()      # 古い内容を捨てて
                st.success("保存しました。最新の内容を表示します。")
                st.rerun()                 # 読み直す
            except Exception:
                st.error(
                    "保存できませんでした。ロボット（nyukin-bot）に"
                    "『編集者』権限があるか確認してください。"
                )


# ------------------------------------------------------------
# まとめて登録（サイドバー）：選んだ複数の店に、同じ値を一括設定
# ------------------------------------------------------------
def render_bulk_tool(df_store):
    with st.sidebar:
        st.markdown("### 🛠 まとめて登録")
        st.caption("選んだ店たちに、同じ値を一括で入れます")

        # 店の選択肢（番号：店舗名）を作る。空行は除く
        label_to_no = {}
        options = []
        for _, r in df_store.iterrows():
            no = r.get("店舗番号", "")
            if no:
                label = f'{no}：{r.get("店舗名", "")}'
                options.append(label)
                label_to_no[label] = no

        with st.form("bulk_form"):
            field = st.selectbox(
                "まとめて入れる項目",
                ["配達者名", "支払い方法", "支払い日", "振込名義"],
            )
            value = st.text_input("入れる値", placeholder="例：渡辺")
            selected = st.multiselect(
                "対象の店（番号や名前で検索して選ぶ）", options
            )
            go = st.form_submit_button(
                "この内容でまとめて保存", type="primary", use_container_width=True
            )

        if go:
            nos = [label_to_no[l] for l in selected]
            if not nos:
                st.warning("店が1つも選ばれていません。")
            else:
                try:
                    n = bulk_update_field(field, value, nos)
                    st.cache_data.clear()
                    st.success(f"{n} 店の「{field}」を『{value}』にしました。")
                    st.rerun()
                except Exception:
                    st.error(
                        "保存できませんでした。ロボットに『編集者』権限が"
                        "あるか確認してください。"
                    )


# ------------------------------------------------------------
# 画面
# ------------------------------------------------------------
def main():
    st.set_page_config(
        page_title="店舗情報 検索",
        page_icon="🔍",
        initial_sidebar_state="collapsed",  # 普段は閉じておく（家族は検索だけ）
    )
    st.markdown(STYLE, unsafe_allow_html=True)

    df_store, df_product, source = load_data()

    # まとめて登録パネル（スプレッドシート接続時のみ・左の「＞」から開く）
    if can_write():
        render_bulk_tool(df_store)

    st.markdown('<div class="app-title">🔍 店舗情報 検索</div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="app-sub">検索窓に1つ入れて検索（店舗番号・店舗名・振込名義・配達者・商品名 など）</div>',
        unsafe_allow_html=True,
    )

    # 検索窓（1つだけ）と検索ボタン
    with st.form("search_form"):
        keyword = st.text_input(
            "検索",
            placeholder="例：113 / 肉まん / 田中 …",
            label_visibility="collapsed",
        )
        submitted = st.form_submit_button(
            "検索", type="primary", use_container_width=True
        )

    # Enterキーでもボタンでも検索できるようにする
    if submitted or keyword:
        results = search(keyword, df_store, df_product)
        n = len(results)

        if n == 0:
            st.info("該当なし")
        else:
            st.markdown(f'<div class="result-count">{n} 件ヒット</div>', unsafe_allow_html=True)

            rows = [row for _, row in results.iterrows()]
            if n <= BIG_CARD_LIMIT:
                # 少ないとき → 大きいカードで詳しく表示し、その下に編集フォーム
                writable = can_write()
                for r in rows:
                    st.markdown(
                        f'<div class="cards-big">'
                        f'{big_card_html(r, products_of(r.get("店舗番号", ""), df_product))}'
                        f'</div>',
                        unsafe_allow_html=True,
                    )
                    if writable:
                        render_edit_form(r)
            else:
                # 多いとき → 小さいタイルを並べて見やすく
                cards = "".join(
                    small_card_html(r, products_of(r.get("店舗番号", ""), df_product))
                    for r in rows
                )
                st.markdown(f'<div class="cards-grid">{cards}</div>', unsafe_allow_html=True)

    c1, c2 = st.columns([3, 1])
    with c1:
        st.caption(f"データ元：{source}")
    with c2:
        if st.button(
            "🔄 最新に更新",
            use_container_width=True,
            help="スプレッドシートに追加・修正した内容をすぐ反映します",
        ):
            st.cache_data.clear()  # 覚えていた古いデータを捨てる
            st.rerun()             # 読み直す


if __name__ == "__main__":
    main()
