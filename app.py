from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import html
import json
import ssl
import urllib.parse
import urllib.request

import folium
import pandas as pd
import streamlit as st
from streamlit_folium import st_folium

st.set_page_config(page_title="Anglers ポイント検索", layout="wide")

JST = timezone(timedelta(hours=+9), "JST")
REQUEST_TIMEOUT = 10
MAX_PAGES = 30
DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "application/json",
}
FALLBACK_FISH_LIST = [
    "アジ (あじ)",
    "シーバス (しーばす)",
    "ショゴ (しょご)",
    "メバル (めばる)",
    "青物 (あおもの)",
]


def kata_to_hira(text: str) -> str:
    """カタカナをひらがなに変換する。"""
    return "".join(chr(ord(c) - 0x60) if 0x30A1 <= ord(c) <= 0x30F6 else c for c in text)


def create_ssl_context() -> ssl.SSLContext:
    """macOS等のローカル環境での証明書検証エラーを回避するSSLコンテキストを返す。"""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def fetch_json(url: str) -> object:
    """指定URLからJSONを取得してパースする。"""
    req = urllib.request.Request(url, headers=DEFAULT_HEADERS)
    ctx = create_ssl_context()
    with urllib.request.urlopen(req, context=ctx, timeout=REQUEST_TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8"))


@st.cache_data(ttl=86400)
def _fetch_fish_list_cached() -> list[str]:
    """Anglers APIから魚種一覧を取得してキャッシュする（失敗時は例外を送出し、エラーをキャッシュしない）。"""
    fishes = fetch_json("https://anglers.jp/api/v2/fishes.json")
    if not isinstance(fishes, list) or not fishes:
        raise ValueError("魚種一覧が空または不正な形式です。")
    # "ショゴ (しょご)" のような形式にして、ひらがなでも検索ヒットするようにする
    result = [
        f"{f['name']} ({kata_to_hira(f['name'])})"
        for f in fishes
        if isinstance(f, dict) and f.get("name")
    ]
    if not result:
        raise ValueError("有効な魚種データが取得できませんでした。")
    return result


def get_fish_list() -> tuple[list[str], bool]:
    """魚種リストとフォールバック使用フラグを返す。"""
    try:
        return _fetch_fish_list_cached(), False
    except Exception:
        return FALLBACK_FISH_LIST, True


def extract_fish_name(display_name: str) -> str:
    """表示名（例: 'ショゴ (しょご)'）から元の魚種名だけを正確に取り出す。"""
    return display_name.rsplit(" (", 1)[0]


def parse_caught_at(caught_at_str: str | None) -> datetime | None:
    """ISO8601形式の日時文字列を日本時間(JST)のdatetimeに変換する。"""
    if not caught_at_str or not isinstance(caught_at_str, str):
        return None
    try:
        dt = datetime.fromisoformat(caught_at_str.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=JST)
    return dt.astimezone(JST)


def fetch_catches(
    fish_name: str,
    region_keyword: str,
    start_dt: datetime,
    end_dt: datetime,
) -> tuple[list[dict], int, bool, str | None]:
    """Anglers APIから指定期間・魚種の釣果を新しい順に取得する。"""
    search_query = f"{fish_name} {region_keyword}".strip()
    query = urllib.parse.quote(search_query)

    all_catches: list[dict] = []
    keep_fetching = True
    page = 1
    fetched_pages = 0
    last_page_had_data = False
    error_message: str | None = None

    while keep_fetching and page <= MAX_PAGES:
        # order=caught_at を付与して釣れた日時が新しい順に強制ソートする
        url = f"https://anglers.jp/api/v2/results.json?keyword={query}&order=caught_at&page={page}"
        try:
            data = fetch_json(url)
            fetched_pages += 1
        except Exception as e:
            error_message = f"データ取得エラー: {e}"
            break

        if not isinstance(data, list) or not data:
            last_page_had_data = False
            break

        last_page_had_data = True
        for catch in data:
            if not isinstance(catch, dict):
                continue

            caught_at = parse_caught_at(catch.get("caught_at"))
            if caught_at is None:
                continue

            # 終了日より未来のデータはスキップ
            if caught_at > end_dt:
                continue

            # 開始日より過去のデータに到達したら取得終了
            if caught_at < start_dt:
                keep_fetching = False
                break

            # Anglersは「アジ」で検索してもメッキやアイブリ（近縁種や混獲）を返すため、
            # 厳密に魚種名が一致しているデータだけをカウントする
            catch_fish = catch.get("fish") or {}
            if catch_fish.get("name") != fish_name:
                continue

            all_catches.append(catch)

        page += 1

    hit_page_limit = (
        keep_fetching
        and fetched_pages >= MAX_PAGES
        and last_page_had_data
        and error_message is None
    )
    return all_catches, fetched_pages, hit_page_limit, error_message


def aggregate_catches(catches: list[dict]) -> tuple[dict[str, dict], list[dict]]:
    """釣果リストをポイントごとに集計し、ポイント詳細辞書とランキング配列を返す。"""
    areas_summary: dict[str, dict] = {}

    for catch in catches:
        area = catch.get("area") or {}
        area_name = area.get("name") or "不明なポイント"
        lat = area.get("lat")
        lng = area.get("lng")

        if area_name not in areas_summary:
            areas_summary[area_name] = {
                "name": area_name,
                "count": 0,
                "url": None,
                "lat": None,
                "lng": None,
                "latest_dt": None,
                "latest_date": "",
                "latest_comment": "",
                "daily_counts": {},
            }

        info = areas_summary[area_name]
        info["count"] += 1

        # Anglers側でそのポイントの全釣果一覧を開くURL
        if info["url"] is None and area.get("id"):
            info["url"] = f"https://anglers.jp/areas/{area['id']}"

        if info["lat"] is None and lat is not None and lng is not None:
            info["lat"] = lat
            info["lng"] = lng

        caught_dt = parse_caught_at(catch.get("caught_at"))
        if caught_dt is not None:
            date_str = caught_dt.strftime("%Y-%m-%d")
            info["daily_counts"][date_str] = info["daily_counts"].get(date_str, 0) + 1

            if info["latest_dt"] is None or caught_dt > info["latest_dt"]:
                info["latest_dt"] = caught_dt
                info["latest_date"] = date_str
                raw_comment = (catch.get("publish_comment") or "").strip()
                info["latest_comment"] = (
                    f"{raw_comment[:30]}..." if len(raw_comment) > 30 else raw_comment
                )

    sorted_areas = sorted(areas_summary.values(), key=lambda x: x["count"], reverse=True)
    ranking_data = [
        {
            "ポイント名": item["name"],
            "釣果数": item["count"],
            "Anglers": item["url"],
        }
        for item in sorted_areas
    ]
    return areas_summary, ranking_data


def build_map(
    areas_summary: dict[str, dict],
    ranking_data: list[dict],
    selected_point: str | None,
) -> folium.Map | None:
    """ポイントごとのピンを立てたFoliumマップを作成する（1ポイント＝1ピンで軽量化）。"""
    # ランキング順に座標があるポイントを並べる
    ordered_points = [
        areas_summary[row["ポイント名"]]
        for row in ranking_data
        if areas_summary[row["ポイント名"]]["lat"] is not None
        and areas_summary[row["ポイント名"]]["lng"] is not None
    ]
    if not ordered_points:
        return None

    center_lat = ordered_points[0]["lat"]
    center_lng = ordered_points[0]["lng"]
    zoom_level = 9

    if selected_point and selected_point in areas_summary:
        sel = areas_summary[selected_point]
        if sel["lat"] is not None and sel["lng"] is not None:
            center_lat = sel["lat"]
            center_lng = sel["lng"]
            zoom_level = 12

    m = folium.Map(location=[center_lat, center_lng], zoom_start=zoom_level)

    # 選択されたポイントの赤いピンが他のピンの後ろに隠れないよう最後に追加する
    unselected = [pt for pt in ordered_points if pt["name"] != selected_point]
    selected = [pt for pt in ordered_points if pt["name"] == selected_point]

    for pt in unselected + selected:
        is_selected = pt["name"] == selected_point
        marker_color = "red" if is_selected else "blue"
        comment_html = f"<br>{html.escape(pt['latest_comment'])}" if pt["latest_comment"] else ""
        popup_html = (
            f"<b>{html.escape(pt['name'])}</b><br>"
            f"釣果数: {pt['count']}件<br>"
            f"最新釣果日: {html.escape(pt['latest_date'] or '不明')}"
            f"{comment_html}"
        )

        folium.Marker(
            [pt["lat"], pt["lng"]],
            popup=folium.Popup(popup_html, max_width=300),
            tooltip=f"{pt['name']} ({pt['count']}件)",
            icon=folium.Icon(color=marker_color, icon="info-sign"),
            z_index_offset=1000 if is_selected else 0,
        ).add_to(m)

    return m


def get_selected_rows(event: object) -> list[int]:
    """st.dataframeの選択イベントから行インデックスのリストを安全に取り出す。"""
    if isinstance(event, dict):
        return (event.get("selection") or {}).get("rows") or []
    selection = getattr(event, "selection", None)
    if isinstance(selection, dict):
        return selection.get("rows") or []
    return getattr(selection, "rows", None) or []


def build_daily_chart_df(
    daily_counts: dict[str, int],
    start_date: date,
    end_date: date,
) -> pd.DataFrame:
    """検索期間の開始日〜終了日までの日別釣果推移DataFrameを作成する（0件の日も補完）。"""
    total_days = max(0, (end_date - start_date).days)
    date_list = [start_date + timedelta(days=offset) for offset in range(total_days + 1)]
    chart_data = [
        {"日付": d, "釣果数": daily_counts.get(d.strftime("%Y-%m-%d"), 0)}
        for d in date_list
    ]
    return pd.DataFrame(chart_data).set_index("日付")


# --- UI構築 ---
fish_list, used_fish_fallback = get_fish_list()

st.title("最近釣れているポイント検索")
st.markdown("指定した魚種が指定期間内でよく釣れている場所を地図とランキングで表示します。")

# iOSのSafariでテキスト入力枠をタップした際に2回タップしないとキーボードが出ないバグの回避策
st.html("""
<style>
div[data-baseweb="input"] input,
div[data-baseweb="select"] input {
    cursor: text !important;
}
</style>
""")

if used_fish_fallback:
    st.caption("※魚種一覧の読み込みに失敗したため、主要な魚種のみ表示しています。")

# 検索フォームを st.form で囲むことで、スマホのキーボードの「確定/Go」ボタンで直接検索できるようにする
with st.form("search_form"):
    col1, col2 = st.columns([2, 2])

    with col1:
        selected_fish = st.selectbox(
            "魚種 (入力して検索可能)",
            fish_list,
            index=None,
            placeholder="魚種を選択してください",
        )

    with col2:
        region_keyword = st.text_input("地域・キーワード (任意)", value="")

    # 期間選択（日数指定）
    days_to_search = st.number_input(
        "収集する日数 (今日から遡る日数)",
        min_value=1,
        max_value=100,
        value=14,
        step=1,
    )

    # フォームの送信ボタン
    search_button = st.form_submit_button("検索する", width="stretch")

if search_button:
    if not selected_fish:
        st.error("魚種を選択してください！")
    else:
        actual_fish_name = extract_fish_name(selected_fish)
        end_datetime = datetime.now(JST)
        start_datetime = end_datetime - timedelta(days=int(days_to_search))

        with st.spinner(f"Anglersから過去 {days_to_search} 日間のデータを集計中...（数秒かかります）"):
            all_catches, page_count, hit_page_limit, fetch_error = fetch_catches(
                actual_fish_name,
                region_keyword,
                start_datetime,
                end_datetime,
            )

        st.session_state["all_catches"] = all_catches
        st.session_state["page_count"] = page_count
        st.session_state["hit_page_limit"] = hit_page_limit
        st.session_state["fetch_error"] = fetch_error
        st.session_state["searched_days"] = int(days_to_search)
        st.session_state["searched_fish"] = actual_fish_name
        st.session_state["searched_start_date"] = start_datetime.date()
        st.session_state["searched_end_date"] = end_datetime.date()
        st.session_state["search_id"] = st.session_state.get("search_id", 0) + 1

if "all_catches" in st.session_state:
    all_catches = st.session_state["all_catches"]
    page_count = st.session_state["page_count"]
    hit_page_limit = st.session_state.get("hit_page_limit", False)
    fetch_error = st.session_state.get("fetch_error")
    searched_days = st.session_state.get("searched_days", 14)
    searched_fish = st.session_state.get("searched_fish", "")
    searched_start_date = st.session_state.get(
        "searched_start_date", datetime.now(JST).date() - timedelta(days=searched_days)
    )
    searched_end_date = st.session_state.get("searched_end_date", datetime.now(JST).date())
    search_id = st.session_state.get("search_id", 0)

    if fetch_error:
        if all_catches:
            st.warning(f"一部のデータ取得中にエラーが発生しました（取得済みの釣果のみ表示します）: {fetch_error}")
        else:
            st.error(fetch_error)

    if not all_catches:
        if not fetch_error:
            st.warning("指定された期間の釣果は見つかりませんでした。")
    else:
        st.success(f"釣果を {len(all_catches)} 件 取得しました！ (API探索ページ数: {page_count})")
        if hit_page_limit:
            st.caption(f"※検索件数が多いため、最新 {MAX_PAGES} ページ分までを集計しています。")

        areas_summary, ranking_data = aggregate_catches(all_catches)

        row1_col1, row1_col2 = st.columns([2, 3])
        selected_point: str | None = None

        with row1_col1:
            st.subheader("釣果ランキング")
            st.caption("行を選択 → 地図で場所を表示 ／ 「開く」→ Anglersで釣果を見る")

            # 行選択 = 地図で強調、リンク列 = Anglersを開く（動作を列で分ける）
            event = st.dataframe(
                ranking_data,
                key=f"ranking_table_{search_id}",
                width="stretch",
                hide_index=True,
                on_select="rerun",
                selection_mode="single-row",
                column_config={
                    "ポイント名": st.column_config.TextColumn("ポイント名"),
                    "釣果数": st.column_config.NumberColumn("釣果数", width="small"),
                    "Anglers": st.column_config.LinkColumn(
                        "Anglers", display_text="開く ↗", width="small"
                    ),
                },
            )

            selected_rows = get_selected_rows(event)
            if selected_rows:
                selected_idx = selected_rows[0]
                if 0 <= selected_idx < len(ranking_data):
                    selected_point = ranking_data[selected_idx]["ポイント名"]
                    st.info(f"選択中: {selected_point}")
                    # スマホでも押しやすい大きめのボタン
                    point_url = areas_summary[selected_point].get("url")
                    if point_url:
                        st.link_button(
                            f"Anglersで「{selected_point}」の釣果を見る ↗",
                            point_url,
                            width="stretch",
                        )

        with row1_col2:
            st.subheader("ポイントマップ (OpenStreetMap)")
            m = build_map(areas_summary, ranking_data, selected_point)
            if m is not None:
                # returned_objects=[] で地図操作時の不要な再実行を防止し、キーで選択変更時のフォーカスを確実にする
                st_folium(
                    m,
                    key=f"map_{search_id}_{selected_point or 'all'}",
                    use_container_width=True,
                    height=500,
                    returned_objects=[],
                )
            else:
                st.info("位置情報付きの釣果がありませんでした。")

            if selected_point and selected_point in areas_summary:
                st.divider()
                title_fish = f"{searched_fish} / " if searched_fish else ""
                st.subheader(f"{selected_point} の釣果推移（{title_fish}過去 {searched_days} 日間）")
                df_chart = build_daily_chart_df(
                    areas_summary[selected_point]["daily_counts"],
                    searched_start_date,
                    searched_end_date,
                )
                st.line_chart(df_chart)
