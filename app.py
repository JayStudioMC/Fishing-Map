import streamlit as st
import urllib.request
import urllib.parse
import json
import ssl
import folium
from streamlit_folium import st_folium
from datetime import datetime, timedelta, timezone, date

st.set_page_config(page_title="Anglers ポイント検索", layout="wide")

def kata_to_hira(text):
    # カタカナをひらがなに変換する関数
    return "".join(chr(ord(c) - 0x60) if 0x30A1 <= ord(c) <= 0x30F6 else c for c in text)

@st.cache_data(ttl=86400)
def get_fish_list():
    url = "https://anglers.jp/api/v2/fishes.json"
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(url, headers={
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
        'Accept': 'application/json'
    })
    try:
        with urllib.request.urlopen(req, context=ctx) as response:
            fishes = json.loads(response.read().decode())
            # "ショゴ (しょご)" のような形式にして、ひらがなでも検索ヒットするようにする
            return [f"{f['name']} ({kata_to_hira(f['name'])})" for f in fishes]
    except Exception:
        return ["アジ (あじ)", "シーバス (しーばす)", "ショゴ (しょご)", "メバル (めばる)", "青物 (青物)"]

fish_list = get_fish_list()

st.title("最近釣れているポイント検索 (モックアップ)")
st.markdown("指定した魚種が指定期間内でよく釣れている場所を地図とランキングで表示します。")

# 検索フォーム
col1, col2, col3 = st.columns([2, 2, 1])

with col1:
    selected_fish = st.selectbox(
        "魚種 (入力して検索可能)", 
        fish_list, 
        index=None,
        placeholder="魚種を選択してください"
    )

with col2:
    region_keyword = st.text_input("地域・キーワード (任意)", value="")

# 期間選択（日数指定）
days_to_search = st.number_input("収集する日数 (今日から遡る日数)", min_value=1, max_value=100, value=14, step=1)

with col3:
    st.write("")
    st.write("")
    search_button = st.button("検索する", use_container_width=True)

if search_button:
    if not selected_fish:
        st.error("魚種を選択してください！")
    else:
        with st.spinner(f"Anglersから過去 {days_to_search} 日間のデータを集計中...（数秒かかります）"):
            # 日付比較用の設定
            JST = timezone(timedelta(hours=+9), 'JST')
            end_datetime = datetime.now(JST)
            start_datetime = end_datetime - timedelta(days=days_to_search)
            
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
    
            # 魚種名だけを取り出す（"ショゴ (しょご)" -> "ショゴ"）
            actual_fish_name = selected_fish.split(" ")[0]
    
            # 魚種とキーワードを結合して検索
            search_query = f"{actual_fish_name} {region_keyword}".strip()
            query = urllib.parse.quote(search_query)
            
            all_catches = []
            keep_fetching = True
            page = 1
            
            # ページネーションでデータを取得（最大30ページで安全ストップ）
            while keep_fetching and page <= 30:
                # order=caught_at を付与して釣れた日時が新しい順に強制ソートする
                url = f"https://anglers.jp/api/v2/results.json?keyword={query}&order=caught_at&page={page}"
                req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
                
                try:
                    with urllib.request.urlopen(req, context=ctx) as response:
                        data = json.loads(response.read().decode())
                        if not data:
                            break # データがなくなったら終了
                            
                        for catch in data:
                            caught_at_str = catch.get('caught_at')
                            if caught_at_str:
                                caught_at = datetime.fromisoformat(caught_at_str.replace('Z', '+00:00'))
                                
                                # 終了日より未来のデータはスキップ
                                if caught_at > end_datetime:
                                    continue
                                    
                                # 開始日より過去のデータに到達したら取得終了
                                if caught_at < start_datetime:
                                    keep_fetching = False
                                    break
                                
                                # Anglersは「アジ」で検索してもメッキやアイブリ（近縁種や混獲）を返すため、
                                # 厳密に魚種名が一致しているデータだけをカウントする
                                catch_fish = catch.get('fish')
                                if not catch_fish or catch_fish.get('name') != actual_fish_name:
                                    continue
                                
                                all_catches.append(catch)
                        page += 1
                except Exception as e:
                    st.error(f"データ取得エラー: {e}")
                    break
            
            st.session_state['all_catches'] = all_catches
            st.session_state['page_count'] = page - 1
    
if 'all_catches' in st.session_state:
    all_catches = st.session_state['all_catches']
    page_count = st.session_state['page_count']
    
    if not all_catches:
        st.warning("指定された期間の釣果は見つかりませんでした。")
    else:
        st.success(f"釣果を {len(all_catches)} 件 取得しました！ (API探索ページ数: {page_count})")
        
        # --- データ集計 ---
        locations_count = {}
        map_points = []
        
        for catch in all_catches:
            area = catch.get('area')
            
            if area:
                area_name = area.get('name', '不明なポイント')
                lat = area.get('lat')
                lng = area.get('lng')
                
                locations_count[area_name] = locations_count.get(area_name, 0) + 1
                
                if lat and lng:
                    map_points.append({
                        "name": area_name,
                        "lat": lat,
                        "lng": lng,
                        "date": catch.get("caught_at", "")[:10],
                        "comment": catch.get("publish_comment", "")[:30]
                    })
        
        # --- 画面表示 ---
        row1_col1, row1_col2 = st.columns([2, 3])
        
        selected_point = None
        sorted_locs = sorted(locations_count.items(), key=lambda x: x[1], reverse=True)
        ranking_data = [{"ポイント名": name, "釣果数": count} for name, count in sorted_locs]
        
        with row1_col1:
            st.subheader("釣果ランキング")
            
            # DataFrame selection feature
            event = st.dataframe(
                ranking_data,
                use_container_width=True,
                on_select="rerun",
                selection_mode="single-row"
            )
            
            if event.selection.rows:
                selected_idx = event.selection.rows[0]
                selected_point = ranking_data[selected_idx]["ポイント名"]
                st.info(f"選択中: {selected_point}")
                
        with row1_col2:
            st.subheader("ポイントマップ (OpenStreetMap)")
            if map_points:
                # 選択されたポイントがあれば、そこを中心にズームする
                center_lat = map_points[0]['lat']
                center_lng = map_points[0]['lng']
                zoom_level = 9
                
                if selected_point:
                    for pt in map_points:
                        if pt["name"] == selected_point:
                            center_lat = pt["lat"]
                            center_lng = pt["lng"]
                            zoom_level = 12
                            break

                m = folium.Map(location=[center_lat, center_lng], zoom_start=zoom_level)
                
                for pt in map_points:
                    popup_text = f"<b>{pt['name']}</b><br>日付: {pt['date']}<br>{pt['comment']}..."
                    
                    # 選択されたポイントのピンは赤色にする
                    marker_color = "red" if pt["name"] == selected_point else "blue"
                    icon = folium.Icon(color=marker_color, icon="info-sign")
                    
                    folium.Marker(
                        [pt['lat'], pt['lng']],
                        popup=folium.Popup(popup_text, max_width=300),
                        tooltip=pt['name'],
                        icon=icon
                    ).add_to(m)
                
                st_folium(m, width=700, height=500)
                
                # --- 選択されたポイントの推移グラフ ---
                if selected_point:
                    st.divider()
                    st.subheader(f"📈 {selected_point} の釣果推移（過去 {days_to_search} 日間）")
                    
                    # 日付ごとの集計
                    daily_counts = {}
                    for pt in map_points:
                        if pt["name"] == selected_point:
                            date_str = pt["date"]
                            daily_counts[date_str] = daily_counts.get(date_str, 0) + 1
                    
                    import pandas as pd
                    # グラフのX軸用に、指定期間の全日付リストを作成（釣果0件の日も表示するため）
                    JST = timezone(timedelta(hours=+9), 'JST')
                    today = datetime.now(JST).date()
                    start_d = today - timedelta(days=days_to_search)
                    
                    date_list = [start_d + timedelta(days=x) for x in range((today - start_d).days + 1)]
                    
                    chart_data = []
                    for d in date_list:
                        d_str = d.strftime("%Y-%m-%d")
                        chart_data.append({"日付": d, "釣果数": daily_counts.get(d_str, 0)})
                        
                    df_chart = pd.DataFrame(chart_data).set_index("日付")
                    st.line_chart(df_chart)

            else:
                st.info("位置情報付きの釣果がありませんでした。")
