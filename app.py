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


def add_product(no, product_name, qty=""):
    # 商品タブ：A=店舗番号, B=商品名, C=店舗名(自動の式), D=個数
    # C は式なので触らず、新しい行の A・B・D にだけ書き込む
    _, ws_product = get_worksheets()
    next_row = len(ws_product.col_values(1)) + 1
    ws_product.update([[str(no), str(product_name)]], f"A{next_row}:B{next_row}")
    if str(qty).strip():
        ws_product.update([[str(qty)]], f"D{next_row}")


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
@import url('https://fonts.googleapis.com/css2?family=Zen+Maru+Gothic:wght@400;500;700&display=swap');

/* あたたかい配色：生成りの背景＋テラコッタのアクセント1色 */
:root {
  --bg:#f7f3ec; --card:#fffdf8; --ink:#3f382f; --muted:#9c9384;
  --line:#ece2d2; --accent:#c56a49; --accent-dark:#ad5537;
  --chip-bg:#f2e9db; --chip-ink:#9c6b45;
}

/* やわらかい丸ゴシックを全体に */
html, body, [data-testid="stAppViewContainer"], .stMarkdown, input, textarea, button, select,
[class^="st-"], [class*=" st-"] { font-family:'Zen Maru Gothic', sans-serif !important; }
/* ただしアイコン（開閉マーク等）は専用フォントに戻す */
[data-testid="stIconMaterial"], [data-testid="stExpanderToggleIcon"],
.material-icons, .material-symbols-outlined, .material-symbols-rounded {
  font-family: 'Material Symbols Rounded','Material Symbols Outlined','Material Icons' !important;
}
[data-testid="stAppViewContainer"] { background: var(--bg); }

.block-container { max-width: 760px; padding-top: 2.6rem; }
/* 上の標準ヘッダー（濃い帯）を消す＝見切れ防止 */
[data-testid="stHeader"] { display: none; }
footer, #MainMenu { visibility: hidden; }
[data-testid="InputInstructions"] { display: none; }

/* 見出し */
.app-title { font-size: 1.55rem; font-weight: 700; color: var(--ink); margin: 0 0 .25rem 0; }
.app-sub   { color: var(--muted); font-size: .9rem; margin: 0 0 1.3rem 0; }

/* 検索ボックス */
div[data-testid="stTextInput"] input {
  font-size: 1.1rem; padding: .85rem 1rem; border-radius: 14px;
  border: 1.5px solid var(--line); background: var(--card); color: var(--ink);
}
div[data-testid="stTextInput"] input:focus { border-color: var(--accent); box-shadow: none; }
div[data-testid="stTextInput"] input::placeholder { color: #c7bca9; }

/* 検索ボタン */
div[data-testid="stFormSubmitButton"] button {
  background: var(--accent); color: #fff; border: none; border-radius: 14px;
  padding: .8rem; font-weight: 700; font-size: 1.05rem;
}
div[data-testid="stFormSubmitButton"] button:hover { background: var(--accent-dark); }

/* 控えめボタン（最新に更新など） */
div.stButton > button {
  border-radius: 12px; border: 1.5px solid var(--line); background: var(--card);
  color: var(--accent); font-weight: 500; padding: .45rem .7rem; font-size: .9rem;
}
div.stButton > button:hover { border-color: var(--accent); }

.result-count { color: var(--muted); font-size: .95rem; margin: 1.4rem 0 .7rem; }
.no-hit { background: var(--card); border: 1.5px dashed var(--line); border-radius: 16px;
          padding: 22px; text-align: center; color: var(--muted); font-size: 1rem; }

/* カード共通 */
.card { background: var(--card); border: 1px solid var(--line); border-radius: 18px; }
.card .head { display: flex; align-items: baseline; gap: .6rem; flex-wrap: wrap; }
.no   { color: var(--muted); font-size: .85rem; font-weight: 500; }
.name { font-weight: 700; color: var(--ink); }
.chip { display: inline-block; background: var(--chip-bg); color: var(--chip-ink); font-weight: 500;
        border-radius: 999px; padding: .28rem .8rem; font-size: .9rem;
        margin: .18rem .35rem .18rem 0; }
.chip .qty { color: var(--accent); font-weight: 700; margin-left: .3rem; }

/* 大きいカード（ヒットが少ないとき）：中身のある項目だけ出す */
.cards-big { display: flex; flex-direction: column; gap: 16px; }
.card.big { padding: 22px 24px; }
.card.big .name { font-size: 1.4rem; }
.card.big .info { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
                  gap: 14px 26px; margin: 16px 0 12px; }
.card.big .row .lbl { display: block; color: var(--muted); font-size: .8rem; margin-bottom: 2px; }
.card.big .row .val { display: block; color: var(--ink); font-size: 1.1rem; font-weight: 500; }
.card.big .prod { border-top: 1px solid var(--line); padding-top: 14px; }
.card.big .prod .lbl { display: block; color: var(--muted); font-size: .8rem; margin-bottom: 7px; }

/* 小さいタイル（ヒットが多いとき） */
.cards-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(250px, 1fr));
              gap: 14px; }
.card.small { padding: 16px 18px; }
.card.small .name { font-size: 1.15rem; }
.card.small .mini { color: var(--muted); font-size: .9rem; margin: 9px 0; line-height: 1.6; }
.card.small .chip { font-size: .85rem; }
.more { color: var(--muted); font-size: .85rem; }
</style>
"""


# ------------------------------------------------------------
# カードのHTMLを組み立てる
# ------------------------------------------------------------
def big_card_html(row, products):
    def e(k):
        return html.escape(row.get(k, ""))

    # 中身のある項目だけ出す（空欄は表示しない＝スッキリ）
    fields = [("振込名義", "振込名義"), ("配達者", "配達者名"),
              ("支払い方法", "支払い方法"), ("支払い日", "支払い日")]
    rows = ""
    for label, key in fields:
        val = row.get(key, "").strip()
        if val:
            rows += (f'<div class="row"><span class="lbl">{label}</span>'
                     f'<span class="val">{html.escape(val)}</span></div>')
    if not rows:
        rows = '<div class="row"><span class="val" style="color:#bdb3a2">基本情報はまだ未登録</span></div>'

    chips = "".join(_chip(n, q) for (n, q) in products)
    prod = f'<div class="prod"><span class="lbl">取扱商品</span>{chips}</div>' if chips else ""

    return (
        '<div class="card big">'
        f'<div class="head"><span class="name">{e("店舗名")}</span>'
        f'<span class="no">No.{e("店舗番号")}</span></div>'
        f'<div class="info">{rows}</div>'
        f'{prod}'
        '</div>'
    )


def small_card_html(row, products):
    def e(k):
        return html.escape(row.get(k, ""))

    # 入っている情報だけ短くまとめる
    bits = []
    if row.get("配達者名", "").strip():
        bits.append("配達 " + e("配達者名"))
    if row.get("支払い方法", "").strip():
        bits.append("支払 " + e("支払い方法"))
    mini = f'<div class="mini">{"　".join(bits)}</div>' if bits else ""

    shown = products[:4]
    chips = "".join(_chip(n, q) for (n, q) in shown)
    if len(products) > 4:
        chips += f'<span class="more">+{len(products) - 4}</span>'
    chips = f'<div class="chips">{chips}</div>' if chips else ""

    return (
        '<div class="card small">'
        f'<div class="head"><span class="name">{e("店舗名")}</span>'
        f'<span class="no">No.{e("店舗番号")}</span></div>'
        f'{mini}{chips}'
        '</div>'
    )


def products_of(no, df_product):
    # その店の商品を (商品名, 個数) のリストで返す
    sub = df_product[df_product["店舗番号"] == no]
    has_qty = "個数" in df_product.columns
    out = []
    for _, r in sub.iterrows():
        name = str(r.get("商品名", "")).strip()
        if not name:
            continue
        qty = str(r.get("個数", "")).strip() if has_qty else ""
        out.append((name, qty))
    return out


def _chip(name, qty):
    label = html.escape(name)
    if qty:
        label += f'<span class="qty">×{html.escape(qty)}</span>'
    return f'<span class="chip">{label}</span>'


# ------------------------------------------------------------
# 入口チェック：ポータルからは ?key=… で素通り、直URLは合言葉を聞く
# ------------------------------------------------------------
def _access_cfg():
    try:
        return dict(st.secrets.get("access", {}))
    except Exception:
        return {}


def check_access():
    cfg = _access_cfg()
    # 設定がまだ無ければ制限しない（うっかり締め出し防止）
    if not cfg.get("key") and not cfg.get("password"):
        return True
    if st.session_state.get("authed"):
        return True

    # ポータルからのリンク（?key=〇〇）なら合言葉なしで通す
    url_key = st.query_params.get("key", "")
    if cfg.get("key") and url_key == cfg["key"]:
        st.session_state["authed"] = True
        return True

    # それ以外は合言葉を聞く
    st.markdown('<div class="app-title">店舗情報 検索</div>', unsafe_allow_html=True)
    st.markdown('<div class="app-sub">ご利用には合言葉が必要です</div>', unsafe_allow_html=True)
    with st.form("login"):
        pw = st.text_input(
            "合言葉", type="password",
            label_visibility="collapsed", placeholder="合言葉を入力",
        )
        ok = st.form_submit_button("入る", type="primary", use_container_width=True)
    if ok:
        if cfg.get("password") and pw == cfg["password"]:
            st.session_state["authed"] = True
            st.rerun()
        else:
            st.error("合言葉がちがいます")
    return False


# ------------------------------------------------------------
# 登録・編集フォーム（カードの下に出す）
# ------------------------------------------------------------
def render_edit_form(row):
    no = row.get("店舗番号", "")
    with st.expander("この店に登録・編集する"):
        with st.form(f"edit_{no}"):
            name = st.text_input("店舗名", value=row.get("店舗名", ""))
            c1, c2 = st.columns(2)
            with c1:
                furikomi = st.text_input("振込名義", value=row.get("振込名義", ""))
                houhou = st.text_input("支払い方法", value=row.get("支払い方法", ""))
            with c2:
                haitatsu = st.text_input("配達者名", value=row.get("配達者名", ""))
                hi = st.text_input("支払い日", value=row.get("支払い日", ""))
            pc1, pc2 = st.columns([3, 1])
            with pc1:
                new_product = st.text_input("商品を追加（任意・1つ）", placeholder="例：肉まん")
            with pc2:
                new_qty = st.text_input("個数", placeholder="例：10")
            saved = st.form_submit_button(
                "保存する", type="primary", use_container_width=True
            )

        if saved:
            try:
                update_store(no, name, furikomi, haitatsu, houhou, hi)
                if new_product.strip():
                    add_product(no, new_product.strip(), new_qty.strip())
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
    with st.expander("まとめて登録（複数の店に同じ値を入れる）"):
        st.caption("項目を選び、値を入れ、対象の店を複数えらんで保存します")

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
# 送り／引き取りの仕分け：配達者が空欄の店を1件ずつ、2択で埋める
# ------------------------------------------------------------
def _triage_set(no, value):
    # 配達者名の欄に 送り／引き取り を書く
    bulk_update_field("配達者名", value, [no])


def render_triage(df_store):
    with st.expander("送り／引き取りの仕分け（配達者が空欄の店）"):
        # 作業リスト（配達者が空欄の店）を一度だけ作る
        if "triage_q" not in st.session_state:
            st.session_state.triage_q = [
                (str(r.get("店舗番号", "")), str(r.get("店舗名", "")))
                for _, r in df_store.iterrows()
                if str(r.get("店舗番号", "")).strip()
                and not str(r.get("配達者名", "")).strip()
            ]
            st.session_state.triage_i = 0

        q = st.session_state.triage_q
        i = st.session_state.triage_i
        total = len(q)

        # 作り直し（最新の空欄を取り直す）
        top = st.columns([3, 1])
        with top[1]:
            if st.button("作り直す", use_container_width=True,
                         help="配達者が空欄の店を最新の状態で取り直します"):
                st.session_state.pop("triage_q", None)
                st.cache_data.clear()
                st.rerun()

        if total == 0:
            st.info("配達者が空欄の店はありません。")
            return
        if i >= total:
            st.success(f"完了！{total} 件すべて仕分けしました。")
            return

        no, name = q[i]
        done = i
        st.caption(f"済み {done} 件 ／ 残り {total - done} 件")
        st.progress(done / total)
        st.markdown(
            f'<div style="font-size:1.5rem;font-weight:700;margin:.4rem 0;">'
            f'{name}　<span style="color:#9c9384;font-size:1rem;">No.{no}</span></div>',
            unsafe_allow_html=True,
        )
        st.write("この店は？")

        b = st.columns(2)
        with b[0]:
            if st.button("送り", key="tri_okuri", use_container_width=True):
                _triage_set(no, "送り")
                st.session_state.triage_i += 1
                st.rerun()
        with b[1]:
            if st.button("引き取り", key="tri_hiki", use_container_width=True):
                _triage_set(no, "引き取り")
                st.session_state.triage_i += 1
                st.rerun()

        nav = st.columns(2)
        with nav[0]:
            if st.button("スキップ →", key="tri_skip", use_container_width=True):
                st.session_state.triage_i += 1
                st.rerun()
        with nav[1]:
            if st.button("← もどる", key="tri_back",
                         use_container_width=True, disabled=(i == 0)):
                st.session_state.triage_i -= 1
                st.rerun()


# ------------------------------------------------------------
# 画面
# ------------------------------------------------------------
def main():
    st.set_page_config(page_title="店舗情報 検索", page_icon="🔍")
    st.markdown(STYLE, unsafe_allow_html=True)

    # 入口チェック（合言葉が必要なら、ここで止める）
    if not check_access():
        st.stop()

    df_store, df_product, source = load_data()

    st.markdown('<div class="app-title">店舗情報 検索</div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="app-sub">店舗番号・店舗名・振込名義・配達者・商品名 などで探せます</div>',
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

    # 管理用パネル（スプレッドシート接続時のみ・検索窓のすぐ下に出す）
    if can_write():
        render_bulk_tool(df_store)
        render_triage(df_store)

    # Enterキーでもボタンでも検索できるようにする
    if submitted or keyword:
        results = search(keyword, df_store, df_product)
        n = len(results)

        if n == 0:
            st.markdown(
                '<div class="no-hit">該当する店は見つかりませんでした</div>',
                unsafe_allow_html=True,
            )
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
            "最新に更新",
            use_container_width=True,
            help="スプレッドシートに追加・修正した内容をすぐ反映します",
        ):
            st.cache_data.clear()  # 覚えていた古いデータを捨てる
            st.rerun()             # 読み直す


if __name__ == "__main__":
    main()
