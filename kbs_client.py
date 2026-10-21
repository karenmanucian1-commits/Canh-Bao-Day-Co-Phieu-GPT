"""Gọi trực tiếp API công khai của KB Securities (KBS) - thay thế vnstock."""

import re
import unicodedata
from datetime import datetime

import pandas as pd
import requests

_IIS_BASE_URL = "https://kbbuddywts.kbsec.com.vn/iis-server/investment"
_SEARCH_URL = f"{_IIS_BASE_URL}/stock/search/data"
_FINANCE_INFO_URL = f"{_IIS_BASE_URL}/stock/finance-info"

_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9,vi-VN;q=0.8,vi;q=0.7",
    "Connection": "keep-alive",
    "Content-Type": "application/json",
    "Cache-Control": "no-cache",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
}


def _slugify(text):
    """Chuyển tên chỉ số (tiếng Anh) thành item_id dạng snake_case, giống cách vnstock làm."""
    if not text or not str(text).strip():
        return ""
    text = str(text)
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"^\d+(\.\d+)*\.\s*", "", text)
    text = re.sub(r"^[IVXivx]+(\.\d+)*\.\s*", "", text)
    text = re.sub(r"^[A-Za-z]\.\s*", "", text)
    text = text.lower()
    text = re.sub(r"\s*&\s*", " and ", text)
    text = re.sub(r"['\"`]", "", text)
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", text)
    text = re.sub(r"[^a-z0-9\s_-]", " ", text)
    text = re.sub(r"[\s\-/\\]+", "_", text)
    text = re.sub(r"_+", "_", text)
    text = text.strip("_")
    if text and text[0].isdigit():
        text = f"n_{text}"
    return text


def _get(url, params=None, timeout=30):
    r = requests.get(url, headers=_HEADERS, params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()


def danh_sach_co_phieu():
    """Toàn bộ mã cổ phiếu thường (loại trừ trái phiếu, chứng chỉ quỹ, chứng quyền...)."""
    data = _get(_SEARCH_URL)
    stocks = data if isinstance(data, list) else data.get("data", [])
    df = pd.DataFrame(stocks)
    df = df[df["type"] == "stock"]
    return df["symbol"].tolist()


def lich_su_gia(symbol, start, end):
    """Lịch sử giá theo ngày. start/end dạng YYYY-MM-DD. Trả về DataFrame hoặc None."""
    def fmt(d):
        return datetime.strptime(d, "%Y-%m-%d").strftime("%d-%m-%Y")

    url = f"{_IIS_BASE_URL}/stocks/{symbol}/data_day"
    params = {"sdate": fmt(start), "edate": fmt(end)}
    data = _get(url, params=params)
    ohlc = data.get("data_day")
    if not ohlc:
        return None

    df = pd.DataFrame(ohlc)
    df = df.rename(columns={"t": "time", "o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})
    df["time"] = pd.to_datetime(df["time"])
    df = df.sort_values("time").reset_index(drop=True)
    for col in ["open", "high", "low", "close"]:
        df[col] = df[col].astype(float) / 1000
    df["volume"] = df["volume"].astype(float).astype(int)
    return df


def chi_so_tai_chinh(symbol, limit=4):
    """Chỉ số tài chính theo quý (P/E, ROA, ROE, EPS...). Trả DataFrame: item, item_en, item_id, <quý mới nhất>, <quý cũ hơn>..."""
    page_size = max(limit, 4)
    collected_periods = []
    rows_by_period = {}
    item_meta = {}
    page = 1

    while len(collected_periods) < limit and page <= 20:
        url = f"{_FINANCE_INFO_URL}/{symbol}"
        params = {"page": page, "pageSize": page_size, "type": "CSTC", "unit": 1000, "termtype": 2, "languageid": 1}
        data = _get(url, params=params)
        content = data.get("Content", {}) or {}
        ratio_groups = [k for k in content if "Nhóm chỉ số" in k]
        if not ratio_groups:
            break

        all_rows = []
        for g in ratio_groups:
            all_rows.extend(content.get(g) or [])
        if not all_rows:
            break

        head_list = data.get("Head", []) or []
        # KBS trả Head theo thứ tự quý mới -> cũ, nhưng Value1..N lại theo thứ tự cũ -> mới
        # (Value cuối cùng là quý mới nhất). Đã đối chiếu bằng 2 lần gọi pageSize=4 và pageSize=5.
        # Vì vậy: cột j của Head (0 = mới nhất) tương ứng Value{N - j}. KHÔNG sắp theo ID.
        n_heads = len(head_list)
        periods = []
        value_index = []
        for j, head in enumerate(head_list):
            value_index.append(n_heads - j)
            year = head.get("YearPeriod", "")
            term_name = head.get("TermName", "")
            if term_name and "Quý" in term_name:
                q = term_name.replace("Quý", "").strip()
                label = f"{year}-Q{q}"
            else:
                label = f"{year}-{term_name}" if term_name else str(year)
            orig = label
            suf = 1
            while label in periods:
                label = f"{orig}_{suf}"
                suf += 1
            periods.append(label)

        new_periods = [p for p in periods if p not in collected_periods]
        if not new_periods:
            break

        for record in all_rows:
            item = record.get("Name", "")
            item_en = record.get("NameEn", "")
            item_id = _slugify(item_en) if item_en and item_en.strip() else _slugify(item)
            item_meta.setdefault(item_id, (item, item_en))
            for label, vi in zip(periods, value_index):
                if label not in new_periods:
                    continue
                val = record.get(f"Value{vi}")
                if val is not None:
                    try:
                        val = float(val)
                    except (TypeError, ValueError):
                        pass
                rows_by_period.setdefault(item_id, {})[label] = val

        collected_periods.extend(new_periods)
        page += 1

    if not collected_periods:
        return pd.DataFrame()

    periods_final = collected_periods[:limit]
    records = []
    for item_id, (item, item_en) in item_meta.items():
        row = {"item": item, "item_en": item_en, "item_id": item_id}
        for p in periods_final:
            row[p] = rows_by_period.get(item_id, {}).get(p)
        records.append(row)

    return pd.DataFrame(records)