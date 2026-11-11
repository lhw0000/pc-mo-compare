import re
import time
from datetime import datetime

import pandas as pd
import plotly.express as px
import streamlit as st
from curl_cffi import requests

# ------------------------------------------------------------------
# 基本設定
# ------------------------------------------------------------------
st.set_page_config(page_title="PChome vs MOMO 價格比較", page_icon="🛒", layout="wide")

PLATFORMS = ["PChome", "MOMO"]
COLORS = {"PChome": "#1A73E8", "MOMO": "#E91E63"}

# --- MOMO ---
MOMO_API = "https://apisearch.momoshop.com.tw/momoSearchCloud/moec/textSearch"
MOMO_HOME = "https://www.momoshop.com.tw/"
MOMO_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
    "Content-Type": "application/json;charset=UTF-8",
    "Origin": "https://www.momoshop.com.tw",
    "Referer": "https://www.momoshop.com.tw/",
}

# --- PChome 24h ---
PCHOME_API = "https://ecshweb.pchome.com.tw/search/v3.3/all/results"
PCHOME_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
    "Referer": "https://24h.pchome.com.tw/",
}


# ------------------------------------------------------------------
# 爬蟲
# ------------------------------------------------------------------
def build_momo_payload(keyword: str, page: int) -> dict:
    return {
        "host": "momoshop",
        "flag": "searchEngine",
        "data": {
            "searchValue": keyword,
            "searchType": "1",
            "cateLevel": "-1",
            "curPage": str(page),
            "priceS": "0",
            "priceE": "9999999",
            "showType": "chessboardType",
            "isFuzzy": "0",
            "rtnCateDatainfo": {
                "cateCode": "",
                "cateLv": "-1",
                "keyword": keyword,
                "curPage": str(page),
                "historyDoPush": "false",
                "timestamp": int(time.time() * 1000),
            },
            "flag": 2018,
            "serviceCode": "MT01",
            "addressSearchData": {},
        },
    }


def scrape_momo(keyword, pages, delay, on_page):
    session = requests.Session(impersonate="chrome")
    session.get(MOMO_HOME, headers=MOMO_HEADERS)  # 先取得 cookie

    rows = []
    for page in range(1, pages + 1):
        on_page("MOMO", page)
        resp = session.post(
            MOMO_API, headers=MOMO_HEADERS, json=build_momo_payload(keyword, page)
        )
        if resp.status_code != 200:
            st.warning(f"MOMO 第 {page} 頁請求失敗（HTTP {resp.status_code}），已停止。")
            break

        products = resp.json().get("rtnSearchData", {}).get("goodsInfoList", [])
        if not products:
            st.info(f"MOMO 第 {page} 頁沒有資料，已提前結束。")
            break

        for p in products:
            rows.append(
                {"平台": "MOMO", "品名": p.get("goodsName", ""), "價格": p.get("goodsPrice", "")}
            )
        if page < pages:
            time.sleep(delay)
    return rows


def scrape_pchome(keyword, pages, delay, on_page):
    session = requests.Session(impersonate="chrome")

    rows = []
    for page in range(1, pages + 1):
        on_page("PChome", page)
        resp = session.get(
            PCHOME_API,
            headers=PCHOME_HEADERS,
            params={"q": keyword, "page": page, "sort": "sale/dc"},
        )
        if resp.status_code != 200:
            st.warning(f"PChome 第 {page} 頁請求失敗（HTTP {resp.status_code}），已停止。")
            break

        data = resp.json()
        products = data.get("prods") or []
        if not products:
            st.info(f"PChome 第 {page} 頁沒有資料，已提前結束。")
            break

        for p in products:
            rows.append(
                {"平台": "PChome", "品名": p.get("name", ""), "價格": p.get("price", "")}
            )

        total_page = data.get("totalPage") or page
        if page >= total_page:
            st.info(f"PChome 搜尋結果只有 {total_page} 頁，已全部爬完。")
            break
        if page < pages:
            time.sleep(delay)
    return rows


SCRAPERS = {"PChome": scrape_pchome, "MOMO": scrape_momo}


def scrape_all(keyword, platforms, pages, delay, progress, status) -> pd.DataFrame:
    total = pages * len(platforms)
    done = {"n": 0}

    def on_page(platform, page):
        status.write(f"正在爬取 {platform} 第 {page} / {pages} 頁…")
        done["n"] += 1
        progress.progress(min(done["n"] / total, 1.0))

    rows = []
    for pf in platforms:
        try:
            rows.extend(SCRAPERS[pf](keyword, pages, delay, on_page))
        except Exception as e:  # 一個平台失敗不影響另一個
            st.error(f"{pf} 爬取時發生錯誤：{e}")
    return pd.DataFrame(rows)


# ------------------------------------------------------------------
# 資料清理與分析
# ------------------------------------------------------------------
def clean_data(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["價格"] = df["價格"].astype(str).str.replace(r"[^\d]", "", regex=True)
    df = df[df["價格"] != ""]
    df["價格"] = df["價格"].astype(int)
    df = df[df["價格"] > 0].reset_index(drop=True)
    df["序號"] = df.groupby("平台").cumcount() + 1  # 各平台各自編號
    return df


def extract_brand(name: str) -> str:
    """商品名稱常以【品牌】開頭，抓不到就歸為「其他」。"""
    m = re.search(r"[【\[](.+?)[】\]]", str(name))
    if m:
        brand = m.group(1).strip()
        if 0 < len(brand) <= 12:
            return brand
    return "其他"


def add_price_bins(df: pd.DataFrame, n_bins: int) -> pd.DataFrame:
    """兩平台共用同一組分位數價格區間，圖表才能互相比較。"""
    df = df.copy()
    try:
        cats = pd.qcut(df["價格"], q=n_bins, duplicates="drop")
    except ValueError:
        cats = pd.cut(df["價格"], bins=1)
    df["價格區間"] = cats.map(lambda i: f"{int(i.left):,}–{int(i.right):,}")
    order = (
        df.assign(_l=cats.map(lambda i: i.left))
        .drop_duplicates("價格區間")
        .sort_values("_l")["價格區間"]
        .tolist()
    )
    df["價格區間"] = pd.Categorical(df["價格區間"], categories=order, ordered=True)
    return df


# ------------------------------------------------------------------
# 介面
# ------------------------------------------------------------------
st.title("🛒 PChome vs MOMO 商品價格比較")
st.caption("輸入商品名稱與頁數，同時爬取 PChome 與 MOMO 搜尋結果，並以多種圖表比較價格。")

with st.sidebar:
    st.header("查詢條件")
    keyword = st.text_input("商品名稱", value="行動電源")
    platforms = st.multiselect("比較平台", PLATFORMS, default=PLATFORMS)
    pages = st.number_input("每個平台爬取頁數", min_value=1, max_value=20, value=3, step=1)
    delay = st.slider("每頁間隔秒數", 0.5, 5.0, 1.5, 0.5)
    n_bins = st.slider("價格區間數（圓餅圖／旭日圖／柱狀圖）", 3, 8, 5)
    top_brand = st.slider("旭日圖保留前幾大品牌", 3, 15, 8)
    run = st.button("開始爬取", type="primary", use_container_width=True)

if run:
    if not keyword.strip():
        st.error("請輸入商品名稱。")
    elif not platforms:
        st.error("請至少選擇一個平台。")
    else:
        progress = st.progress(0.0)
        status = st.empty()
        raw = scrape_all(keyword.strip(), platforms, int(pages), delay, progress, status)
        status.empty()
        progress.empty()

        if raw.empty:
            st.session_state.pop("result", None)
            st.error("沒有取得任何商品，請換個關鍵字或稍後再試。")
        else:
            st.session_state["result"] = {"keyword": keyword.strip(), "df": clean_data(raw)}

# ------------------------------------------------------------------
# 顯示結果
# ------------------------------------------------------------------
result = st.session_state.get("result")
if result:
    df = result["df"]
    kw = result["keyword"]
    shown = [p for p in PLATFORMS if p in df["平台"].unique()]

    # 各平台摘要指標
    cols = st.columns(len(shown))
    for col, pf in zip(cols, shown):
        sub = df[df["平台"] == pf]["價格"]
        with col:
            st.subheader(pf)
            m1, m2 = st.columns(2)
            m1.metric("商品數", f"{len(sub):,}")
            m2.metric("平均價格", f"${sub.mean():,.0f}")
            m3, m4 = st.columns(2)
            m3.metric("最高價格", f"${sub.max():,}")
            m4.metric("最低價格", f"${sub.min():,}")

    binned = add_price_bins(df, n_bins)

    tab_bar, tab_line, tab_pie, tab_sun, tab_data = st.tabs(
        ["📊 柱狀圖", "📈 折線圖", "🥧 圓餅圖", "☀️ 旭日圖", "📋 資料表"]
    )

    # 柱狀圖：統計值比較 + 各價格區間商品數
    with tab_bar:
        stats = (
            df.groupby("平台")["價格"]
            .agg(平均="mean", 中位數="median", 最高="max", 最低="min")
            .round(0)
            .reset_index()
            .melt(id_vars="平台", var_name="統計量", value_name="價格")
        )
        fig = px.bar(
            stats,
            x="統計量",
            y="價格",
            color="平台",
            barmode="group",
            text_auto=",.0f",
            color_discrete_map=COLORS,
            title=f"「{kw}」價格統計比較",
        )
        fig.update_layout(yaxis_title="價格（元）", height=480)
        st.plotly_chart(fig, use_container_width=True)

        bin_df = binned.groupby(["價格區間", "平台"], observed=True).size().reset_index(name="商品數")
        fig = px.bar(
            bin_df,
            x="價格區間",
            y="商品數",
            color="平台",
            barmode="group",
            text_auto=True,
            color_discrete_map=COLORS,
            title=f"「{kw}」各價格區間商品數",
        )
        fig.update_layout(height=480)
        st.plotly_chart(fig, use_container_width=True)

    # 折線圖：各平台價格走勢 + 平均線
    with tab_line:
        fig = px.line(
            df,
            x="序號",
            y="價格",
            color="平台",
            markers=True,
            hover_name="品名",
            color_discrete_map=COLORS,
            title=f"「{kw}」各平台售價（依搜尋結果順序）",
        )
        for i, pf in enumerate(shown):
            mean_p = df.loc[df["平台"] == pf, "價格"].mean()
            fig.add_hline(
                y=mean_p,
                line_dash="dash",
                line_color=COLORS[pf],
                annotation_text=f"{pf} 平均 {mean_p:,.0f}",
                annotation_position="top left" if i % 2 == 0 else "bottom left",
            )
        fig.update_layout(xaxis_title="商品序號", yaxis_title="價格（元）", height=540)
        st.plotly_chart(fig, use_container_width=True)

    # 圓餅圖：各平台價格區間占比
    with tab_pie:
        pie_cols = st.columns(len(shown))
        for col, pf in zip(pie_cols, shown):
            pie_df = (
                binned[binned["平台"] == pf]
                .groupby("價格區間", observed=True)
                .size()
                .reset_index(name="商品數")
            )
            fig = px.pie(
                pie_df,
                names="價格區間",
                values="商品數",
                title=f"{pf}：價格區間分布",
                hole=0.3,
            )
            fig.update_traces(textinfo="percent+label")
            fig.update_layout(height=520, showlegend=False)
            col.plotly_chart(fig, use_container_width=True)

    # 旭日圖：平台 → 價格區間 → 品牌
    with tab_sun:
        sun_df = binned.assign(品牌=binned["品名"].map(extract_brand))
        top = sun_df["品牌"].value_counts().head(top_brand).index
        sun_df["品牌"] = sun_df["品牌"].where(sun_df["品牌"].isin(top), "其他")
        sun_df = (
            sun_df.groupby(["平台", "價格區間", "品牌"], observed=True)
            .size()
            .reset_index(name="商品數")
        )
        sun_df["價格區間"] = sun_df["價格區間"].astype(str)
        fig = px.sunburst(
            sun_df,
            path=["平台", "價格區間", "品牌"],
            values="商品數",
            color="平台",
            color_discrete_map=COLORS,
            title=f"「{kw}」平台 × 價格區間 × 品牌",
        )
        fig.update_traces(textinfo="label+percent parent")
        fig.update_layout(height=680)
        st.plotly_chart(fig, use_container_width=True)
        st.caption("品牌由商品名稱開頭的【】文字擷取，抓不到或不在前幾大者歸為「其他」。")

    with tab_data:
        st.dataframe(df[["平台", "序號", "品名", "價格"]], use_container_width=True)
        st.download_button(
            "下載 CSV",
            data=df[["平台", "序號", "品名", "價格"]].to_csv(index=False).encode("utf-8-sig"),
            file_name=f"價格比較_{kw}_{datetime.now():%Y%m%d}.csv",
            mime="text/csv",
        )
else:
    st.info("請在左側輸入商品名稱與頁數，然後按「開始爬取」。")
