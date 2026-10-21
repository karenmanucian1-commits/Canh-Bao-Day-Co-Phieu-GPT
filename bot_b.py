"""BOT B: Quét và xếp hạng TOÀN BỘ cổ phiếu quá bán đồng thời 4 chỉ báo; KBS API.
Chạy: python bot_b.py ; thử: python bot_b.py --symbols SCS STB DVM NO1 BOT --no-telegram
Giữ nguyên test.py, kbs_client.py, ma_loi.csv. Không tự động giao dịch.
"""
import argparse
from collections import Counter
import pickle
import csv
import math
import os
import time
import logging
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import requests
import ta
from dotenv import load_dotenv
import kbs_client

load_dotenv()
OUTPUT = Path('bot_b_output')
CACHE = OUTPUT / 'cache'

def latest_cached(kind, symbol, max_age_days=5):
    """Read newest usable cache; do not confuse old data with live data."""
    CACHE.mkdir(parents=True, exist_ok=True)
    today = datetime.now().date()
    for path in sorted(CACHE.glob(f'{kind}_{symbol}_????????.pkl'), reverse=True):
        try:
            day = datetime.strptime(path.stem[-8:], '%Y%m%d').date()
            if not 0 <= (today-day).days <= max_age_days:
                continue
            with path.open('rb') as f:
                value = pickle.load(f)
            if value is not None:
                return value
        except (ValueError, OSError, EOFError, pickle.UnpicklingError):
            continue
    return None


def scheduled_cached_call(kind, symbol, loader, slot, refresh=False):
    """Morning uses recent local price cache; noon fetches new daily price once.
    Financial statements may reuse recent cache because they do not change each session.
    """
    if not refresh and kind == 'finance':
        value = latest_cached('finance', symbol, max_age_days=14)
        if value is not None:
            return value
    if not refresh and kind == 'price' and slot == 'morning':
        value = latest_cached('price', symbol, max_age_days=5)
        if value is not None:
            return value
    return cached_call(kind, symbol, loader, refresh=refresh)


def fmt_telegram(value, digits=2):
    v = number(value)
    return 'N/A' if v is None else f'{v:,.{digits}f}'


def action_for_row(row):
    """Action from existing model evidence, not a new mandatory screen."""
    if row['risk_label'] == 'RẤT CAO' or row['bottom'] is None or row['bottom'] < 30:
        return 'KHÔNG MUA LÚC NÀY'
    if (row['eligible'] and row['status'] == 'ĐANG PHỤC HỒI'
        and row['bottom'] >= 70 and row['rr1'] is not None and row['rr1'] >= 1.5
        and row['trust_score'] >= 65):
        return 'CÂN NHẮC MUA THĂM DÒ (cần tự xác minh)'
    return 'CHỜ / THEO DÕI'


def send_daily_telegram(rows, reference_date, slot, date, count_scanned, error_count, stale_count, force=False):
    """Send compact Telegram report; preserves original stock screening and scores."""
    token, chat_id = os.getenv('BOT_TOKEN'), os.getenv('CHAT_ID')
    if not token or not chat_id:
        raise RuntimeError('Thiếu BOT_TOKEN hoặc CHAT_ID trong .env. Không thể gửi Telegram.')

    slot_name = {
        'morning': '06:00 - PHÂN TÍCH PHIÊN TRƯỚC',
        'noon': '11:31 - QUÉT SAU PHIÊN SÁNG',
        'manual': 'GỬI LẠI BÁO CÁO ĐÃ LƯU',
    }.get(slot, slot)
    marker = OUTPUT / f'telegram_sent_{date}_{slot}.ok'
    if marker.exists() and not force:
        print(f'Đã gửi báo cáo slot {slot} hôm nay; không gửi trùng. Dùng --telegram-resend để gửi lại.')
        return

    def short_action(row):
        action = action_for_row(row)
        if 'KHÔNG MUA' in action:
            return '🔴 CHƯA MUA'
        if 'THĂM DÒ' in action:
            return '🟢 CÂN NHẮC THĂM DÒ'
        return '🟡 THEO DÕI'

    def short_advice(row):
        notes = []
        quality = number(row.get('quality'))
        bottom_score = number(row.get('bottom'))
        roe = number(row.get('fin_roe'))
        pe = number(row.get('fin_pe'))
        volume = number(row.get('volume'))
        rr = number(row.get('rr1'))
        rsi = number(row.get('rsi14'))

        if quality is not None and quality >= 70:
            notes.append('🟢 Tài chính nổi bật theo mô hình.')
        elif (roe is not None and roe < 3) or (pe is not None and pe > 40):
            notes.append('🔴 Tài chính/định giá chưa hấp dẫn.')

        if bottom_score is not None and bottom_score >= 70:
            notes.append('📈 Có tín hiệu phục hồi sơ bộ.')
        elif rsi is not None and rsi < 10:
            notes.append('📉 Quá bán cực sâu, chưa xác nhận đáy.')
        else:
            notes.append('📉 Quá bán, chưa xác nhận đáy.')

        if volume is not None and volume < 50000:
            notes.append('💧 Thanh khoản thấp, khó thoát hàng.')

        if str(row.get('risk_label', '')) == 'RẤT CAO':
            notes.append('⚠️ Rủi ro rất cao.')
        elif rr is not None and rr < 1.5:
            notes.append('⚠️ R:R chưa hấp dẫn.')

        action = action_for_row(row)
        if 'KHÔNG MUA' in action:
            notes.append('🎯 Chưa mua, chờ tín hiệu phục hồi.')
        elif 'THĂM DÒ' in action:
            notes.append('🎯 Chỉ cân nhắc thăm dò sau xác minh.')
        else:
            notes.append('🎯 Theo dõi giá và thanh khoản.')

        return '\n'.join('- ' + item for item in notes[:5])

    # Report date is not proof of intraday snapshot time.
    header = (
        f'💥 CỔ PHIẾU VÙNG ĐÁY – GPT 💥\n\n'
        f'📊 BOT B | {slot_name} | {date}\n'
        f'📅 Ngày dữ liệu mới nhất: {reference_date or "N/A"}\n'
        f'🔍 Đã quét: {count_scanned} mã\n'
        f'✅ Đạt 6 điều kiện: {len(rows)} mã\n'
        f'📋 Cần thẩm định: {sum(not r["eligible"] for r in rows)} mã\n'
        f'⚠️ Lỗi: {error_count} | Mã đạt có dữ liệu cũ: {stale_count}\n\n'
        f'🏆 DANH SÁCH MÃ ĐẠT ĐỦ 6 ĐIỀU KIỆN\n'
    )
    if rows:
        for i, row in enumerate(rows, 1):
            header += (
                f'{i}. {row["symbol"]} – '
                f'{fmt_telegram(row["total"], 1)} điểm | {short_action(row)}\n'
            )
    else:
        header += 'Không có mã nào đạt bộ lọc.\n'
    header += '\n📌 Đạt bộ lọc không đồng nghĩa có tín hiệu mua.'

    for start in range(0, len(header), 3500):
        send_telegram(header[start:start + 3500])

    for i, row in enumerate(rows, 1):
        msg = (
            f'🏆 #{i} {row["symbol"]} | {short_action(row)}\n\n'
            f'💵 Giá: {fmt_telegram(row["price"], 2)} nghìn đồng\n'
            f'⭐ Tổng: {fmt_telegram(row["total"],1)}/100 | '
            f'🏢 DN: {fmt_telegram(row["quality"],1)}/100 | '
            f'📉 Đáy: {fmt_telegram(row["bottom"],1)}/100\n'
            f'📉 RSI14: {fmt_telegram(row["rsi14"],1)} | RSI7: {fmt_telegram(row["rsi7"],1)}\n'
            f'💧 MFI14: {fmt_telegram(row["mfi14"],1)} | MFI11: {fmt_telegram(row["mfi11"],1)}\n'
            f'📊 KL TB20: {fmt_telegram(row["volume"],0)} | GD: {row["traded_sessions20"]}/20 phiên\n'
            f'💰 ROE: {fmt_telegram(row["fin_roe"],1)}% | '
            f'EPS: {fmt_telegram(row["fin_eps"],0)} | '
            f'P/E: {fmt_telegram(row["fin_pe"],1)} | '
            f'P/B: {fmt_telegram(row["fin_pb"],2)}\n'
            f'🛑 Stoploss: {fmt_telegram(row["stop"],2)}\n'
            f'🎯 Target 1: {fmt_telegram(row["target1"],2)} | '
            f'Target 2: {fmt_telegram(row["target2"],2)}\n'
            f'⚖️ R:R: 1:{fmt_telegram(row["rr1"],2)}\n'
            f'⚠️ Rủi ro: {row["risk_label"]} | Tin cậy: {row["trust_label"]}\n\n'
            f'📌 NHẬN ĐỊNH\n{short_advice(row)}'
        )
        for start in range(0, len(msg), 3500):
            send_telegram(msg[start:start + 3500])

    marker.write_text(
        f'{datetime.now().isoformat()}\nreference_date={reference_date}\n',
        encoding='utf-8'
    )
    print(f'Đã gửi Telegram thành công: {len(rows)} mã; slot {slot}.')


def resend_saved_telegram(args):
    """Gửi lại báo cáo đã lưu, tuyệt đối không gọi API KBS hoặc quét thị trường."""
    OUTPUT.mkdir(exist_ok=True)
    if args.resend_file:
        path = Path(args.resend_file)
    else:
        files = sorted(p for p in OUTPUT.glob('all_oversold_????-??-??.csv') if '_test' not in p.stem)
        if not files:
            raise FileNotFoundError('Không có all_oversold_YYYY-MM-DD.csv trong bot_b_output. Không quét lại.')
        path = files[-1]
    if not path.is_file():
        raise FileNotFoundError(f'Không tìm thấy CSV: {path}')
    frame = pd.read_csv(path, encoding='utf-8-sig')
    required = {'symbol','eligible','status','risk_label','bottom','rr1','trust_score',
                'rank','last_date','price','total','quality','rsi14','rsi7','mfi14','mfi11',
                'volume','traded_sessions20','fin_roe','fin_eps','fin_pe','fin_pb',
                'stop','target1','target2','trust_label','advice'}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError('CSV thiếu các cột cần gửi Telegram: ' + ', '.join(sorted(missing)))
    rows = frame.where(pd.notna(frame), None).to_dict('records')
    for row in rows:
        row['eligible'] = str(row['eligible']).strip().lower() in ('true','1','yes')
        for key in ('bottom','rr1','trust_score'):
            row[key] = number(row[key])
    reference = str(rows[0]['last_date']) if rows else path.stem.replace('all_oversold_', '')
    report_date = path.stem.replace('all_oversold_', '')
    print(f'CHỈ GỬI TELEGRAM: {path} | {len(rows)} mã | phiên {reference} | KHÔNG QUÉT / KHÔNG GỌI KBS', flush=True)
    send_daily_telegram(rows, reference, 'manual', report_date, 'theo báo cáo đã lưu',
                        'không đọc lại', 'không đọc lại', force=True)


def cached_call(kind, symbol, loader, refresh=False):
    """Cache theo ngày, dùng lại trong ngày để thử thuật toán không gọi API."""
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / f'{kind}_{symbol}_{datetime.now():%Y%m%d}.pkl'
    if not refresh and path.exists():
        try:
            with path.open('rb') as f: return pickle.load(f)
        except (OSError, EOFError, pickle.UnpicklingError): pass
    result = loader()
    temp = path.with_suffix('.tmp')
    with temp.open('wb') as f: pickle.dump(result, f, protocol=pickle.HIGHEST_PROTOCOL)
    temp.replace(path)
    return result

# Chỉ xác nhận ngành ngân hàng khi có mapping rõ; không suy đoán từ D/E.
BANK_SYMBOLS = {'STB','VCB','BID','CTG','TCB','MBB','ACB','VPB','HDB','SHB','TPB','VIB','MSB','OCB','LPB','SSB','EIB','NAB','BAB','BVB','KLB','PGB','SGB','VAB'}


def number(x):
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def clamp(v):
    return max(0., min(1., v))


def scale(v, lo, hi):
    return None if v is None else clamp((v - lo) / (hi - lo))


def ratio(df, ids):
    if df is None or df.empty or 'item_id' not in df.columns:
        return None
    cols = [c for c in df.columns if c not in ('item','item_en','item_id')]
    if not cols:
        return None
    for ident in ids:
        hit = df[df['item_id'] == ident]
        if not hit.empty:
            for col in cols:
                v = number(hit.iloc[0][col])
                if v is not None:
                    return v
    return None


def indicators(df):
    df = df.copy()
    # Chuẩn hóa datetime64[us]/[ns] trước khi sắp xếp; bỏ dòng thời gian lỗi.
    df['time'] = pd.to_datetime(df['time'], errors='coerce')
    df = df.dropna(subset=['time']).sort_values('time').drop_duplicates('time')
    for col in ('high', 'low', 'close', 'volume'):
        df[col] = pd.to_numeric(df[col], errors='coerce')
    df = df.dropna(subset=['high', 'low', 'close', 'volume']).copy()
    # Không xóa ngày volume=0 vì sẽ làm méo chuỗi thời gian RSI/MA.
    if len(df) < 65:
        return None
    for name, window in [('rsi14',14), ('rsi7',7)]:
        df[name] = ta.momentum.RSIIndicator(df.close, window=window).rsi()
    for name, window in [('mfi14',14), ('mfi11',11)]:
        df[name] = ta.volume.MFIIndicator(df.high,df.low,df.close,df.volume,window=window).money_flow_index()
    df['atr'] = ta.volatility.AverageTrueRange(df.high,df.low,df.close,window=14).average_true_range()
    df['ma5'] = df.close.rolling(5).mean()
    df['ma20'] = df.close.rolling(20).mean()
    df['ma50'] = df.close.rolling(50).mean()
    return df


def quality(fin, price, volume, sector=None):
    eps = ratio(fin, ['trailing_eps'])
    roe = ratio(fin, ['roe_trailling','roe_trailing','roe'])
    roa = ratio(fin, ['roa_trailling','roa_trailing','roa'])
    nm = ratio(fin, ['net_profit_margin','net_margin'])
    bvps = ratio(fin, ['book_value_per_share_bvps','book_value_per_share','bvps'])
    cr = ratio(fin, ['current_ratio','short_term_ratio'])
    de = ratio(fin, ['liabilities_to_equity'])
    if de is not None:
        de /= 100  # KBS thường dùng %; phải xác minh schema khi triển khai thực tế.
    if cr is not None and cr > 20:
        cr /= 100
    nim_raw = ratio(fin, ['net_interest_margin_nim']) if sector == 'bank' else None
    pe = price * 1000 / eps if eps is not None and eps > 0 else None
    pb = price * 1000 / bvps if bvps is not None and bvps > 0 else None
    # Không suy luận ngành từ đòn bẩy. Khi chưa có mapping ngành thì bỏ điểm an toàn đặc thù.
    financial = sector in ('bank','securities','insurance')
    groups = {
        'dinh_gia': [(10,scale(pe,20,5) if pe and pe > 0 else None),
                      (10,scale(pb,4,1) if pb and pb > 0 else None)],
        'sinh_loi': [(10,scale(roe,0,20)),(10,scale(roa,0,7)),(10,scale(nm,0,15) if not financial else None)],
        'tang_truong': [],
        'an_toan': [(10,scale(de,3,0.5)) if not financial else (10,None),
                    (10,scale(cr,0.8,1.8)) if not financial else (10,None)],
        'thanh_khoan': [(15,scale(volume,20000,500000))],
    }
    # EPS tăng trưởng chỉ khi có 5 kỳ, tránh dùng quý mới nhất thay thế lịch sử.
    cols = [c for c in fin.columns if c not in ('item','item_en','item_id')] if fin is not None and not fin.empty else []
    hist = []
    if cols and 'item_id' in fin.columns:
        hit = fin[fin.item_id == 'trailing_eps']
        if not hit.empty:
            hist = [number(hit.iloc[0][c]) for c in cols]
    growth = ((hist[0]/hist[4]-1)*100 if len(hist)>=5 and hist[0] is not None and hist[4] is not None and hist[4]>0 else None)
    groups['tang_truong'] = [(15,scale(growth,-30,30))]
    earned = sum(w*v for values in groups.values() for w,v in values if v is not None)
    available = sum(w for values in groups.values() for w,v in values if v is not None)
    score = round(100*earned/available,1) if available else None
    warnings = []
    if eps is not None and eps <= 0: warnings.append('EPS không dương')
    if roe is not None and roe < 0: warnings.append('ROE âm')
    if pe is not None and pe < 2: warnings.append('P/E rất thấp: cần kiểm tra lợi nhuận bất thường')
    if pb is not None and pb < .3: warnings.append('P/B rất thấp: cần kiểm tra chất lượng tài sản')
    if volume < 50000: warnings.append('Thanh khoản bình quân dưới 50.000 CP')
    if sector is None: warnings.append('Chưa xác minh ngành: chỉ tiêu tài chính có thể chưa phù hợp')
    if financial: warnings.append('Ngân hàng: chưa có NPL/CAR/NIM; điểm an toàn không áp dụng, không coi là dữ liệu thiếu')
    # Độ phủ tính theo các chỉ tiêu thực sự áp dụng cho ngành, KHÔNG coi N/A là thiếu.
    applicable = 80 if financial else 100
    confidence = round(100*available/applicable,1)
    breakdown = {name: round(sum(w*v for w,v in values if v is not None),1) for name,values in groups.items()}
    coverage = {name: sum(w for w,v in values if v is not None) for name,values in groups.items()}
    return dict(score=score,confidence=confidence,breakdown=breakdown,coverage=coverage,pe=pe,pb=pb,eps=eps,roe=roe,roa=roa,
                growth=growth,cr=cr,de=de,nim_raw=nim_raw,sector=sector or 'chưa xác minh',financial=financial, warnings=warnings,
                hard_fail=(eps is not None and eps <= 0) or (roe is not None and roe < 0))


def data_reliability(q, b):
    """Độ tin cậy của DỮ LIỆU, không phải xác suất tăng giá."""
    score = q['confidence']
    reasons = []
    if q['financial']:
        score -= 25
        reasons.append('Thiếu NPL/CAR/NIM để thẩm định ngân hàng')
    elif q['sector'] == 'chưa xác minh':
        score -= 10
        reasons.append('Chưa xác minh ngành')
    if b['corporate_action_suspect']:
        score -= 15
        reasons.append('Có phiên biến động bất thường cần đối chiếu quyền/cổ tức/tin tức')
    if b['volume'] < 50000:
        score -= 10
        reasons.append('Thanh khoản thấp')
    score = round(max(0, min(100, score)), 1)
    label = 'CAO' if score >= 85 else 'TRUNG BÌNH' if score >= 65 else 'THẤP'
    return score, label, '; '.join(reasons) if reasons else 'Chưa phát hiện hạn chế dữ liệu trọng yếu theo các phép kiểm hiện có'


def bottom(df):
    last, prev = df.iloc[-1], df.iloc[-2]
    price = float(last.close)
    atr = float(last.atr)
    # Đáy và đỉnh lịch sử trước phiên hiện tại, không dùng dữ liệu tương lai.
    low20 = float(df.low.iloc[-21:-1].min())
    recent_low3 = float(df.low.iloc[-4:-1].min())
    # Kháng cự gần: chỉ chọn pivot hình thành trong 60 phiên trước,
    # cùng mặt bằng giá (<= 15% giá hiện tại và <= 4 ATR).
    # Nếu cổ phiếu đã rơi xa khỏi nền cũ, KHÔNG coi đỉnh cũ là mục tiêu gần.
    window = df.iloc[-65:-1].reset_index(drop=True)
    pivots = []
    for i in range(2, len(window)-2):
        hi = float(window.high.iloc[i])
        if hi >= max(window.high.iloc[i-2:i]) and hi > max(window.high.iloc[i+1:i+3]):
            if hi > price + 0.25*atr:
                pivots.append(hi)
    # Hai ngưỡng trên chỉ là giới hạn phòng sai lệch, cần backtest.
    near_ceiling = price + min(price*0.15, 4*atr)
    pivots = sorted(set(x for x in pivots if x <= near_ceiling))
    target1 = pivots[0] if pivots else None
    target2 = next((x for x in pivots[1:] if x >= target1 + 0.25*atr), None) if target1 else None
    # Stop là mức vô hiệu hóa kỹ thuật, không được gọi là hỗ trợ chắc chắn.
    stop = min(low20, price-atr)*.99
    rr1 = (target1-price)/(price-stop) if target1 and stop<price else None
    rr2 = (target2-price)/(price-stop) if target2 and stop<price else None
    rsi_up = last.rsi14 > prev.rsi14
    mfi_up = last.mfi14 > prev.mfi14
    price_stable = last.low >= recent_low3
    above_ma5 = last.close > last.ma5
    # Không thưởng RSI cực thấp; điểm chủ yếu phản ánh ổn định/động lượng phục hồi.
    oversold_score = sum([scale(number(last.rsi14),5,30) or 0,
                          scale(number(last.rsi7),5,30) or 0,
                          scale(number(last.mfi14),0,20) or 0,
                          scale(number(last.mfi11),0,20) or 0]) / 4
    vol20_previous = float(df.volume.iloc[-21:-1].mean())
    vol20 = float(df.volume.tail(20).mean())
    vol_ratio = float(last.volume)/vol20_previous if vol20_previous>0 else 0
    score = round(100*(.20*oversold_score + .20*rsi_up + .15*mfi_up +
                       .20*price_stable + .15*above_ma5 + .10*clamp(vol_ratio/1.5)),1)
    recovery = sum((rsi_up,mfi_up,price_stable,above_ma5)) >= 3
    status = 'ĐANG PHỤC HỒI' if recovery else ('ĐANG TẠO NỀN' if price_stable and (rsi_up or mfi_up) else 'QUÁ BÁN - CHƯA XÁC NHẬN')
    # Chỉ phát hiện biến động giá đột ngột, KHÔNG kết luận nguyên nhân là cổ tức/chia tách.
    changes = df.close.pct_change().tail(21)
    abnormal_moves = int((changes.abs() >= 0.12).sum())
    suspicious = abnormal_moves > 0
    return dict(score=score,status=status,price=price,atr_pct=100*atr/price,
                corporate_action_suspect=suspicious, abnormal_move_count=abnormal_moves,
                stop=stop,target1=target1,target2=target2,rr1=rr1,rr2=rr2,
                rsi14=float(last.rsi14),rsi7=float(last.rsi7),mfi14=float(last.mfi14),mfi11=float(last.mfi11),
                volume=vol20,last_date=str(pd.Timestamp(last.time).date()),
                signals=f'RSI tăng: {rsi_up}; MFI tăng: {mfi_up}; giữ đáy 3 phiên: {price_stable}; trên MA5: {above_ma5}')


def risk_assessment(q, b):
    """Rủi ro giao dịch, độc lập với độ tin cậy dữ liệu và điểm Quality."""
    points, reasons = 0, []
    if b['atr_pct'] >= 10:
        points += 40; reasons.append('ATR >=10% giá')
    elif b['atr_pct'] >= 6:
        points += 22; reasons.append('ATR >=6% giá')
    elif b['atr_pct'] >= 4:
        points += 10; reasons.append('ATR >=4% giá')
    if b['volume'] < 50000:
        points += 30; reasons.append('KL TB20 <50.000')
    elif b['volume'] < 200000:
        points += 12; reasons.append('KL TB20 <200.000')
    if min(b['rsi14'], b['rsi7']) < 5:
        points += 25; reasons.append('RSI cực thấp (<5)')
    elif min(b['rsi14'], b['rsi7']) < 10:
        points += 12; reasons.append('RSI rất thấp (<10)')
    if b['status'] == 'QUÁ BÁN - CHƯA XÁC NHẬN':
        points += 15; reasons.append('chưa xác nhận phục hồi')
    if q['roe'] is not None and q['roe'] < 2:
        points += 8; reasons.append('ROE <2%')
    if b['corporate_action_suspect']:
        points += 10; reasons.append('biến động giá bất thường')
    points = min(100, points)
    label = 'RẤT CAO' if points >= 60 else 'CAO' if points >= 35 else 'TRUNG BÌNH' if points >= 15 else 'THẤP'
    return points, label, '; '.join(reasons) if reasons else 'Không phát hiện yếu tố rủi ro theo bộ chỉ tiêu hạn chế; KHÔNG có nghĩa không rủi ro'


def save_signal_history(rows, reference_date, scope):
    """Lưu tín hiệu theo (ngày, mã), không nhân bản khi chạy lại cùng ngày."""
    path = OUTPUT / 'signal_history.csv'
    columns = ['signal_date','symbol','scope','price','rank','total','quality','bottom',
               'trust_score','risk_score','risk_label','status','stop','target1','target2','rr1']
    old = pd.read_csv(path, dtype={'symbol':str,'signal_date':str}) if path.exists() else pd.DataFrame(columns=columns)
    new = pd.DataFrame([{**{k:r.get(k) for k in columns if k not in ('signal_date','scope')},
                         'signal_date':str(reference_date),'scope':scope} for r in rows],columns=columns)
    merged = pd.concat([old,new],ignore_index=True)
    # Khi trùng ngày/mã, ưu tiên tín hiệu từ quét toàn thị trường.
    merged['_priority'] = merged['scope'].eq('TOÀN THỊ TRƯỜNG').astype(int)
    merged = merged.sort_values('_priority').drop_duplicates(subset=['signal_date','symbol'],keep='last').drop(columns='_priority')
    merged.to_csv(path,index=False,encoding='utf-8-sig')
    return path


def evaluate_history(args):
    """Đánh giá sau 5/10/20 PHIÊN, chỉ lấy giá các mã đã có tín hiệu."""
    OUTPUT.mkdir(exist_ok=True)
    history_path = OUTPUT/'signal_history.csv'
    if not history_path.exists():
        print('Chưa có signal_history.csv. Hãy chạy quét một lần để lưu tín hiệu.')
        return
    history = pd.read_csv(history_path,dtype={'symbol':str,'signal_date':str})
    if history.empty:
        print('Lịch sử tín hiệu trống.'); return
    now = datetime.now()
    end = now.strftime('%Y-%m-%d')
    results = []
    for sym, group in history.groupby('symbol',sort=True):
        print(f'[Đánh giá] {sym}',flush=True)
        try:
            start = (pd.Timestamp(group.signal_date.min())-pd.Timedelta(days=10)).strftime('%Y-%m-%d')
            df = cached_call('evaluation_price',sym,
                lambda sym=sym,start=start: kbs_client.lich_su_gia(sym,start,end),args.refresh)
            if df is None or df.empty: continue
            df = df.sort_values('time').drop_duplicates('time').copy()
            df['date'] = pd.to_datetime(df.time).dt.strftime('%Y-%m-%d')
            for _,signal in group.iterrows():
                after = df[df.date > signal.signal_date].reset_index(drop=True)
                entry = number(signal.price)
                if entry is None or entry <= 0: continue
                result = {'signal_date':signal.signal_date,'symbol':sym,'entry_price':entry,
                          'risk_label':signal.get('risk_label'),'total':signal.get('total'),
                          'available_sessions':len(after)}
                for n in (5,10,20):
                    result[f'return_{n}d_pct'] = round((float(after.close.iloc[n-1])/entry-1)*100,2) if len(after)>=n else None
                    result[f'min_low_{n}d_pct'] = round((float(after.low.iloc[:n].min())/entry-1)*100,2) if len(after)>=n else None
                    result[f'max_high_{n}d_pct'] = round((float(after.high.iloc[:n].max())/entry-1)*100,2) if len(after)>=n else None
                    if len(after)>=n:
                        stop, target = number(signal.get('stop')), number(signal.get('target1'))
                        hit_stop = bool(stop is not None and (after.low.iloc[:n] <= stop).any())
                        hit_target = bool(target is not None and (after.high.iloc[:n] >= target).any())
                        result[f'stop_hit_{n}d'] = hit_stop if stop is not None else None
                        result[f'target_hit_{n}d'] = hit_target if target is not None else None
                        # Nếu cả hai chạm trong cùng phiên, OHLC không cho biết thứ tự khớp.
                        result[f'both_hit_{n}d'] = bool(hit_stop and hit_target)
                results.append(result)
        except Exception as exc:
            print(f'Lỗi đánh giá {sym}: {exc}')
    output = OUTPUT/f'evaluation_{now:%Y%m%d}.csv'
    pd.DataFrame(results,columns=['signal_date','symbol','entry_price','risk_label','total','available_sessions'] + [f'{metric}_{n}d{suffix}' for n in (5,10,20) for metric,suffix in [('return','_pct'),('min_low','_pct'),('max_high','_pct'),('stop_hit',''),('target_hit',''),('both_hit','')]]).to_csv(output,index=False,encoding='utf-8-sig')
    print(f'Đã lưu {len(results)} tín hiệu vào {output}. Các mốc chưa đủ phiên để trống.')
    # Chỉ thống kê các tín hiệu đã có đủ phiên tương lai, tránh coi ô trống là thua.
    summary_rows = []
    evaluated = pd.DataFrame(results)
    for n in (5, 10, 20):
        col = f'return_{n}d_pct'
        if col not in evaluated.columns: continue
        valid = evaluated.dropna(subset=[col]).copy()
        if valid.empty: continue
        for group_name, subset in [('TẤT CẢ', valid)] + [(str(k),v) for k,v in valid.groupby('risk_label',dropna=False)]:
            rets = pd.to_numeric(subset[col],errors='coerce').dropna()
            if rets.empty: continue
            summary_rows.append(dict(horizon_sessions=n, risk_group=group_name, samples=len(rets),
                positive_rate_pct=round(100*(rets>0).mean(),2),mean_return_pct=round(rets.mean(),2),
                median_return_pct=round(rets.median(),2),worst_return_pct=round(rets.min(),2),
                best_return_pct=round(rets.max(),2)))
    summary_file = OUTPUT/f'evaluation_summary_{now:%Y%m%d}.csv'
    pd.DataFrame(summary_rows,columns=['horizon_sessions','risk_group','samples','positive_rate_pct',
        'mean_return_pct','median_return_pct','worst_return_pct','best_return_pct']).to_csv(summary_file,index=False,encoding='utf-8-sig')
    print(f'Đã lưu thống kê: {summary_file} | {len(summary_rows)} nhóm có đủ dữ liệu.')
    for item in summary_rows:
        if item['risk_group']=='TẤT CẢ':
            print(f"  {item['horizon_sessions']} phiên: {item['samples']} tín hiệu | tỷ lệ tăng {item['positive_rate_pct']}% | lợi nhuận TB {item['mean_return_pct']}%")

    print('Chú ý: lợi nhuận là biến động giá chưa tính phí/cổ tức; không giả định thứ tự chạm stop/target.')


def assessment(q, b, eligible):
    """Nhận xét theo quy tắc minh bạch; không giả vờ có dữ liệu chưa được kiểm chứng."""
    comments = []
    if q['score'] is None or q['confidence'] < 45:
        comments.append('Chưa đủ dữ liệu tài chính để đánh giá đáng tin cậy')
    elif q['score'] >= 75:
        comments.append('Chất lượng tài chính và định giá nổi bật trong bộ tiêu chí đang có')
    elif q['score'] >= 55:
        comments.append('Chất lượng doanh nghiệp ở mức trung bình khá theo mô hình')
    else:
        comments.append('Chất lượng tài chính/định giá chưa nổi bật theo mô hình')
    if q['hard_fail']:
        comments.append('EPS không dương hoặc ROE âm: cần thẩm định nguyên nhân trước khi xem xét')
    if b['status'] == 'ĐANG PHỤC HỒI':
        comments.append('Có tín hiệu phục hồi sơ bộ nhưng chưa xác nhận đáy')
    elif b['status'] == 'ĐANG TẠO NỀN':
        comments.append('Có dấu hiệu ổn định giá ban đầu, vẫn có thể giảm tiếp')
    else:
        comments.append('Đang quá bán, chưa có đủ dấu hiệu ổn định')
    if b['rr1'] is None:
        comments.append('Không tìm được kháng cự gần hợp lệ; không suy diễn mục tiêu giá/R:R từ đỉnh cũ')
    elif b['rr1'] < 1.5:
        comments.append('R:R tới kháng cự gần chưa hấp dẫn theo ngưỡng tham khảo 1,5')
    else:
        comments.append('R:R tới kháng cự gần đạt ngưỡng tham khảo, không bảo đảm lợi nhuận')
    if b['atr_pct'] > 10:
        comments.append('Biến động cực lớn (ATR trên 10% giá): rủi ro bắt dao rơi')
    if min(b['rsi14'],b['rsi7']) < 5:
        comments.append('RSI cực thấp: xu hướng giảm có thể còn tiếp diễn')
    if b['volume'] < 50000:
        comments.append('Thanh khoản mỏng, cần lưu ý trượt giá và khả năng thoát vị thế')
    if q['financial']:
        comments.append('Ngân hàng: cần kiểm tra nợ xấu, dự phòng và CAR trước khi kết luận chất lượng')
    if q['roe'] is not None and q['roe'] < 5:
        comments.append(f"ROE {q['roe']:.1f}% thấp: cần xem khả năng cải thiện lợi nhuận")
    if q['pe'] is not None and q['pe'] > 30:
        comments.append(f"P/E {q['pe']:.1f} lần cao so với lợi nhuận hiện tại")
    if q['pb'] is not None and q['pb'] < .3:
        comments.append('P/B dưới 0,3: cần xác minh giá trị sổ sách và chất lượng tài sản')
    if b['corporate_action_suspect']:
        comments.append(f"Có {b['abnormal_move_count']} phiên biến động giá từ 12% trong 20 phiên gần nhất; cần đối chiếu dữ liệu điều chỉnh và sự kiện doanh nghiệp")
    if b['status'] == 'QUÁ BÁN - CHƯA XÁC NHẬN':
        comments.append('Chỉ xem xét lại khi RSI/MFI ngừng giảm và giá giữ đáy hoặc vượt MA5')
    elif b['status'] == 'ĐANG PHỤC HỒI':
        comments.append('Theo dõi khả năng giữ MA5 và khối lượng trong các phiên kế tiếp')
    return '; '.join(comments)


def composite(q, b):
    """Điểm tổng hợp thử nghiệm 60% Quality, 40% Bottom; không coi thiếu dữ liệu là điểm tốt."""
    if q['score'] is None:
        return None
    return round(.60*q['score'] + .40*b['score'], 1)


def fmt(v, digits=2):
    return 'N/A' if v is None else f'{v:,.{digits}f}'


def format_quality(r):
    names = [('dinh_gia','Định giá',20),('sinh_loi','Sinh lời',30),
             ('tang_truong','Tăng trưởng',15),('an_toan','An toàn',20),
             ('thanh_khoan','Thanh khoản',15)]
    parts = []
    for key, label, maximum in names:
        coverage = r['quality_coverage'].get(key, 0)
        if coverage == 0:
            parts.append(f'{label}: N/A (chưa có chỉ tiêu áp dụng)')
        else:
            parts.append(f'{label} {fmt(r["quality_breakdown"].get(key),1)}/{maximum} (dữ liệu {coverage}/{maximum})')
    return ' | '.join(parts)


def explain_symbols(args):
    """Giải thích chính xác điều kiện lọc, kể cả mã không qua; không gọi API tài chính."""
    OUTPUT.mkdir(exist_ok=True)
    end = datetime.now()
    start = end - timedelta(days=270)
    records = []
    frames = {}
    for sym in dict.fromkeys(x.strip().upper() for x in args.explain):
        print(f'\n🔍 {sym}')
        rec = {'symbol':sym}
        try:
            raw = cached_call('price',sym,
                lambda sym=sym: kbs_client.lich_su_gia(sym,start.strftime('%Y-%m-%d'),end.strftime('%Y-%m-%d')),
                args.refresh)
            rec['raw_rows'] = 0 if raw is None else len(raw)
            if raw is not None and not raw.empty and 'time' in raw.columns:
                dates = pd.to_datetime(raw['time'], errors='coerce').dropna()
                rec['valid_dates'] = int(dates.nunique())
                rec['first_date'] = str(dates.min().date()) if not dates.empty else None
                rec['last_raw_date'] = str(dates.max().date()) if not dates.empty else None
            else:
                rec['valid_dates'] = 0
            print(f"  API: {rec['raw_rows']} dòng | {rec['valid_dates']} ngày | từ {rec.get('first_date','N/A')} đến {rec.get('last_raw_date','N/A')}")
            if raw is not None and not raw.empty and 'time' in raw.columns:
                days = pd.to_datetime(raw.time, errors='coerce').dropna().dt.normalize().drop_duplicates().sort_values()
                gaps = days.diff().dt.days.dropna()
                rec['largest_gap_days'] = int(gaps.max()) if not gaps.empty else 0
                rec['gaps_over_10_days'] = int((gaps > 10).sum())
                print(f"  Khoảng cách lịch lớn nhất: {rec['largest_gap_days']} ngày | số khoảng trống >10 ngày: {rec['gaps_over_10_days']}")
                if rec['gaps_over_10_days']:
                    print('  ⚠️ Dữ liệu có khoảng trống dài; không nội suy phiên thiếu để tính RSI/MFI.')
            df = indicators(raw) if raw is not None and not raw.empty and {'time','high','low','close','volume'}.issubset(raw.columns) else None
            if df is None or df.empty:
                rec['reason']='Không có dữ liệu OHLCV hợp lệ hoặc dưới 65 phiên'
                print('  KHÔNG ĐÁNH GIÁ ĐƯỢC:',rec['reason'])
                records.append(rec); continue
            frames[sym]=df
            last=df.iloc[-1]
            rec['last_date']=str(pd.Timestamp(last.time).date())
            for key,threshold in [('rsi14',30),('rsi7',30),('mfi14',20),('mfi11',20)]:
                value=number(last[key]); passed=value is not None and value<threshold
                rec[key]=value; rec[key+'_pass']=passed
                print(f'  {key.upper():5} {fmt(value,2):>9} | yêu cầu < {threshold} | {"ĐẠT" if passed else "KHÔNG ĐẠT"}')
            vol20=number(df.volume.tail(20).mean())
            rec['volume20']=vol20; rec['volume20_pass']=vol20 is not None and vol20>10000
            print(f'  KL TB20 {fmt(vol20,0):>9} | yêu cầu > 10.000 | {"ĐẠT" if rec["volume20_pass"] else "KHÔNG ĐẠT"}')
            traded20=int((pd.to_numeric(df.volume.tail(20),errors='coerce')>0).sum())
            rec['traded_sessions20']=traded20
            rec['traded_sessions20_pass']=traded20>=15
            print(f'  Phiên có GD {traded20}/20 | yêu cầu >= 15/20 | {"ĐẠT" if rec["traded_sessions20_pass"] else "KHÔNG ĐẠT"}')
            print('  Phiên dữ liệu:',rec['last_date'])
            rec['six_pass']=all(rec[k+'_pass'] for k in ('rsi14','rsi7','mfi14','mfi11','volume20','traded_sessions20'))
        except Exception as exc:
            rec['reason']=f'Lỗi API/dữ liệu: {exc}'
            print('  LỖI:',exc)
        records.append(rec)
    reference=max((v['last_date'] for v in records if v.get('last_date')),default=None)
    print('\nPhiên tham chiếu trong danh sách kiểm tra:',reference or 'N/A')
    for rec in records:
        rec['same_reference_session']=bool(reference and rec.get('last_date')==reference)
        if rec.get('six_pass'):
            if rec['same_reference_session']:
                rec['reason']='Đạt đủ 6 điều kiện và đúng phiên tham chiếu'
            else:
                rec['reason']='Đạt 6 điều kiện nhưng dữ liệu cũ hơn phiên tham chiếu'
        elif not rec.get('reason'):
            rec['reason']='Không đạt: '+', '.join(k for k in ('rsi14','rsi7','mfi14','mfi11','volume20','traded_sessions20') if not rec[k+'_pass'])
        print(f"  {rec['symbol']}: {rec['reason']}")
    path=OUTPUT/f'explain_{end:%Y%m%d}.csv'
    pd.DataFrame(records).to_csv(path,index=False,encoding='utf-8-sig')
    print('Đã lưu:',path)


def inspect_bank(args):
    """Kiểm tra các mục KBS thực sự trả về; không tự nhận diện NPL/CAR/NIM nếu không có."""
    OUTPUT.mkdir(exist_ok=True)
    terms = ('npl', 'non-performing', 'non performing', 'bad debt', 'capital adequacy',
             'capital adequacy ratio', 'net interest margin', 'nim', 'car',
             'nợ xấu', 'an toàn vốn', 'biên lãi ròng')
    for sym in dict.fromkeys(x.strip().upper() for x in args.inspect_bank):
        print(f'\n🏦 KIỂM TRA CHỈ TIÊU NGÂN HÀNG KBS: {sym}')
        try:
            fin = cached_call('finance', sym,
                lambda sym=sym: kbs_client.chi_so_tai_chinh(sym, limit=5), args.refresh)
            if fin is None or fin.empty:
                print('  KBS không trả về bảng chỉ số tài chính.'); continue
            print(f'  Tổng dòng: {len(fin)} | cột: {list(fin.columns)}')
            label_cols = [c for c in ('item_id', 'item', 'item_en') if c in fin.columns]
            if not label_cols:
                print('  Không có cột nhãn để đối chiếu.'); continue
            mask = fin[label_cols].fillna('').astype(str).apply(
                lambda col: col.str.lower().map(lambda v: any(t in v for t in terms))).any(axis=1)
            found = fin.loc[mask]
            if found.empty:
                print('  Chưa thấy NPL/CAR/NIM trong tên các chỉ tiêu API trả về.')
            else:
                print(found.to_string(index=False, max_rows=30))
            safe = ''.join(c if c.isalnum() else '_' for c in sym)
            path = OUTPUT / f'bank_metrics_{safe}_{datetime.now():%Y%m%d}.csv'
            fin.to_csv(path, index=False, encoding='utf-8-sig')
            print('  Đã lưu toàn bộ bảng để đối chiếu:', path)
        except Exception as exc:
            print('  Lỗi KBS:', exc)


def send_telegram(message):
    token, chat_id = os.getenv('BOT_TOKEN'), os.getenv('CHAT_ID')
    if not token or not chat_id:
        print('Không có BOT_TOKEN/CHAT_ID: chỉ lưu báo cáo, không gửi Telegram')
        return
    r = requests.post(f'https://api.telegram.org/bot{token}/sendMessage',
                      data={'chat_id':chat_id,'text':message},timeout=20)
    r.raise_for_status()


def run(args):
    OUTPUT.mkdir(exist_ok=True)
    end = datetime.now()
    start = end-timedelta(days=270)
    symbols = args.symbols or kbs_client.danh_sach_co_phieu()
    if not symbols:
        raise RuntimeError('Danh sách mã trống từ KBS, dừng để tránh báo cáo sai.')
    rows, failures = [], []
    audit = []
    def mark(sym, reason, session=None, **metrics):
        audit.append(dict(symbol=sym,reason=reason,last_session=str(session or ""),**metrics))
    observed_dates = []
    stale_candidates = []
    for i,sym in enumerate(symbols,1):
        print(f'[{i}/{len(symbols)}] {sym}',flush=True)
        try:
            raw = scheduled_cached_call('price',sym,lambda: kbs_client.lich_su_gia(sym,start.strftime('%Y-%m-%d'),end.strftime('%Y-%m-%d')),args.slot,args.refresh)
            if raw is None:
                mark(sym,"API_NO_DATA"); continue
            df = indicators(raw)
            if df is None:
                mark(sym,"INSUFFICIENT_OHLCV",rows_api=len(raw) if hasattr(raw,"__len__") else None); continue
            last_session = pd.Timestamp(df.iloc[-1].time).date()
            # Lấy phiên tham chiếu từ dữ liệu toàn thị trường, không từ lịch máy hay từng mã.
            observed_dates.append(last_session)
            if (end.date() - last_session).days > 10:
                mark(sym,"STALE_OVER_10_DAYS",last_session); continue
            p = df.iloc[-1]
            vals = [number(p[k]) for k in ('rsi14','rsi7','mfi14','mfi11')]
            metrics=dict(zip(("rsi14","rsi7","mfi14","mfi11"),vals))
            if any(x is None for x in vals):
                mark(sym,"INDICATOR_MISSING",last_session,**metrics); continue
            failed=[key for key,val,threshold in zip(("RSI14","RSI7","MFI14","MFI11"),vals,(30,30,20,20)) if val>=threshold]
            if failed:
                mark(sym,"FILTER_"+"_".join(failed),last_session,**metrics); continue
            # Điều kiện thứ 5: KL bình quân đúng 20 phiên gần nhất PHẢI > 10.000 CP/phiên.
            vol20 = float(df['volume'].tail(20).mean())
            if not pd.notna(vol20) or vol20 <= 10000:
                mark(sym,"VOLUME_AVG20_LOW",last_session,**metrics,volume_avg20=vol20); continue
            # Bộ lọc bắt buộc thứ 6: ít nhất 15/20 dòng phiên gần nhất có khối lượng > 0.
            # KHÔNG bỏ phiên volume=0 trước khi đếm, tránh làm đẹp thanh khoản giả.
            traded20 = int((pd.to_numeric(df['volume'].tail(20), errors='coerce') > 0).sum())
            if traded20 < 15:
                mark(sym,"TRADED_SESSIONS_UNDER_15",last_session,**metrics,volume_avg20=vol20,traded_sessions20=traded20); continue
            b = bottom(df)
            fin = scheduled_cached_call('finance',sym,lambda: kbs_client.chi_so_tai_chinh(sym,limit=5),args.slot,args.refresh)
            sector = 'bank' if sym in BANK_SYMBOLS else None
            q = quality(fin,b['price'],b['volume'],sector=sector)
            # Nhóm ngân hàng chưa có NPL/CAR/NIM -> không đánh dấu ĐẠT tài chính đầy đủ.
            eligible = (not q['hard_fail'] and q['score'] is not None
                        and q['confidence'] >= 70 and not q['financial']
                        and q['score'] >= 55 and q['eps'] is not None and q['eps'] > 0
                        and q['roe'] is not None and q['roe'] >= 5)
            trust_score, trust_label, trust_reason = data_reliability(q,b)
            risk_score, risk_label, risk_reason = risk_assessment(q,b)
            total = composite(q,b)
            row = dict(symbol=sym,traded_sessions20=traded20,eligible=eligible,total=total,quality=q['score'],bottom=b['score'],confidence=q['confidence'],
                       trust_score=trust_score,trust_label=trust_label,trust_reason=trust_reason,
                       risk_score=risk_score,risk_label=risk_label,risk_reason=risk_reason,
                       quality_breakdown=q['breakdown'],quality_coverage=q['coverage'],
                       advice=assessment(q,b,eligible),
                       status=b['status'],**{k:v for k,v in b.items() if k not in ('score','status')},
                       **{f'fin_{k}':v for k,v in q.items() if k not in ('score','confidence')})
            rows.append(row)
            mark(sym,"PASSED_SIX_FILTERS",last_session,**metrics,volume_avg20=vol20,traded_sessions20=traded20)
        except Exception as e:
            failures.append((sym,str(e)))
            mark(sym,"API_OR_PROCESSING_ERROR",error=str(e)[:250])
            print('  Lỗi:',e)
        finally:
            if not args.symbols and args.delay: time.sleep(args.delay)
    # Loại khỏi bảng chính mã không cập nhật tới phiên mới nhất của universe.
    reference_date = max(observed_dates) if observed_dates else None
    if reference_date:
        for record in audit:
            if record["reason"] == "PASSED_SIX_FILTERS" and record["last_session"] != str(reference_date):
                record["reason"] = "PASSED_BUT_STALE_SESSION"
        fresh, stale_candidates = [], []
        for r in rows:
            (fresh if r['last_date'] == str(reference_date) else stale_candidates).append(r)
        rows = fresh
    # Không cắt TOP 5: xếp hạng toàn bộ mã đạt 4 điều kiện RSI/MFI và 2 điều kiện thanh khoản.
    # Các mã không đạt tiêu chuẩn tài chính/thanh khoản vẫn được giữ lại và ghi lý do.
    ranked = sorted(rows, key=lambda r:(not r['eligible'], -(r['total'] if r['total'] is not None else -1),
                                         -(r['quality'] if r['quality'] is not None else -1),
                                         -(r['bottom'] if r['bottom'] is not None else -1), r['symbol']))
    for i, r in enumerate(ranked, 1):
        r['rank'] = i
        reasons = []
        if r['fin_hard_fail']: reasons.append('EPS/ROE không đạt điều kiện tài chính')
        if r['quality'] is None: reasons.append('Không đủ dữ liệu để chấm Quality')
        if r['confidence'] < 70: reasons.append('Độ phủ chỉ tiêu áp dụng dưới 70%')
        if r['fin_financial']: reasons.append('Ngân hàng: chưa có chỉ tiêu an toàn chuyên ngành NPL/CAR/NIM')
        if r['quality'] is not None and r['quality'] < 55: reasons.append('Quality dưới 55')
        if r['fin_roe'] is None or r['fin_roe'] < 5: reasons.append('ROE dưới 5% hoặc thiếu')
        r['reason'] = '; '.join(reasons) if reasons else 'Đạt tiêu chuẩn sơ bộ'
    eligible_rows = [r for r in ranked if r['eligible']]
    date = end.strftime('%Y-%m-%d')
    suffix = '_test' if args.symbols else ''
    audit_path = OUTPUT/f"scan_audit_{date}{suffix}.csv"
    summary_path = OUTPUT/f"scan_audit_summary_{date}{suffix}.csv"
    pd.DataFrame(audit,columns=["symbol","reason","last_session","rows_api","rsi14","rsi7","mfi14","mfi11","volume_avg20","traded_sessions20","error"]).to_csv(audit_path,index=False,encoding="utf-8-sig")
    counts=Counter(x["reason"] for x in audit)
    pd.DataFrame([{"reason":k,"count":v} for k,v in sorted(counts.items(),key=lambda x:(-x[1],x[0]))]).to_csv(summary_path,index=False,encoding="utf-8-sig")
    print(f"KIỂM TOÁN: {len(audit)}/{len(symbols)} mã | {audit_path} | {summary_path}")
    print("Nguyên nhân (mỗi mã một trạng thái chính): "+"; ".join(f"{k}: {v}" for k,v in counts.most_common()))
    pd.DataFrame(ranked).to_csv(OUTPUT/f'all_oversold_{date}{suffix}.csv',index=False,encoding='utf-8-sig')
    pd.DataFrame(eligible_rows).to_csv(OUTPUT/f'eligible_oversold_{date}{suffix}.csv',index=False,encoding='utf-8-sig')
    if stale_candidates:
        pd.DataFrame(stale_candidates).to_csv(OUTPUT/f'stale_oversold_{date}{suffix}.csv',index=False,encoding='utf-8-sig')
    if failures:
        with open(OUTPUT/f'errors_{date}{suffix}.csv','w',newline='',encoding='utf-8-sig') as f:
            writer=csv.writer(f);writer.writerow(['symbol','error']);writer.writerows(failures)
    scope = 'CHẠY THỬ THEO DANH SÁCH' if args.symbols else 'TOÀN THỊ TRƯỜNG'
    header = f'📉 {scope} — CỔ PHIẾU QUÁ BÁN — {date}\nĐiều kiện BẮT BUỘC: RSI14<30, RSI7<30, MFI14<20, MFI11<20, KL TB20>10.000 CP, CÓ GIAO DỊCH >=15/20 PHIÊN\nPhiên tham chiếu: {reference_date} | Mã đủ 6 điều kiện và đúng phiên: {len(ranked)} | Đạt tài chính sơ bộ: {len(eligible_rows)} | Cần thẩm định: {len(ranked)-len(eligible_rows)} | Mã quá bán dữ liệu cũ: {len(stale_candidates)} | Lỗi API: {len(failures)}'
    messages = [header + '\nNhóm A: đạt bộ lọc tài chính sơ bộ; Nhóm B: cần thẩm định, điểm chỉ dùng để ưu tiên nghiên cứu.\nXếp hạng: nhóm Quality>=55, ROE>=5%, EPS>0, độ phủ>=70% và không thiếu chỉ tiêu an toàn chuyên ngành trước; trong từng nhóm xếp theo Tổng hợp = 60% Quality + 40% Bottom (trọng số thử nghiệm, chưa backtest). Các mã không đạt vẫn hiển thị đủ.']
    for i,r in enumerate(ranked,1):
        lines=[f'#{i} [NHÓM {"A" if r["eligible"] else "B"} — {"ĐẠT SƠ BỘ" if r["eligible"] else "CẦN THẨM ĐỊNH"}] {r["symbol"]} | Giá {fmt(r["price"])} nghìn | phiên {r["last_date"]}',
               f'⭐ Điểm tổng: {fmt(r["total"],1)}/100 | 🏢 Chất lượng DN: {fmt(r["quality"],1)}/100 | 📉 Tín hiệu tạo đáy: {fmt(r["bottom"],1)}/100',
               f'Độ phủ chỉ tiêu áp dụng {fmt(r["confidence"],0)}% | Độ tin cậy dữ liệu: {r["trust_label"]} ({fmt(r["trust_score"],0)}/100) | Tài chính sơ bộ: {"ĐẠT" if r["eligible"] else "CẦN THẨM ĐỊNH"}',
               f'Giới hạn dữ liệu: {r["trust_reason"]}',
               f'⚠️ Rủi ro giao dịch theo mô hình: {("CHƯA ĐÁNH GIÁ ĐỦ" if r["risk_score"] == 0 else r["risk_label"])} ({fmt(r["risk_score"],0)}/100 điểm cảnh báo ghi nhận) — {r["risk_reason"]}',
                f'Rủi ro tài chính chuyên ngành: {"CHƯA XÁC ĐỊNH (thiếu NPL/CAR/NIM)" if r["fin_financial"] else "Chưa thẩm định độc lập"}',
               f'Quality thành phần: {format_quality(r)}',
               (f"NIM KBS (giá trị gốc, CHƯA xác minh đơn vị/kỳ): {fmt(r.get('fin_nim_raw'))} | NPL: N/A | CAR: N/A" if r['fin_financial'] else 'Đối chiếu ngành: chưa xác minh đầy đủ'),
               f'🔎 {r["status"]}',
               f'RSI14 {fmt(r["rsi14"],1)} | RSI7 {fmt(r["rsi7"],1)} | MFI14 {fmt(r["mfi14"],1)} | MFI11 {fmt(r["mfi11"],1)}',
               f'ROE {fmt(r["fin_roe"],1)}% | ROA {fmt(r["fin_roa"],1)}% | P/E {fmt(r["fin_pe"],1)} | P/B {fmt(r["fin_pb"],2)} | EPS {fmt(r["fin_eps"],0)}',
               f'KL TB20 {fmt(r["volume"],0)} | Phiên có giao dịch {r["traded_sessions20"]}/20 | ATR {fmt(r["atr_pct"],1)}%',
               f'Stop kỹ thuật tham khảo {fmt(r["stop"])} | Kháng cự gần 1 {fmt(r["target1"])} | Kháng cự 2 {fmt(r["target2"])} | R:R1 {fmt(r["rr1"],1)}',
               f'Tín hiệu: {r["signals"]}',
               f'Biến động bất thường >=12% trong 20 phiên: {r["abnormal_move_count"]} (chưa xác minh nguyên nhân)',
               f'Nhận diện ngành: {r["fin_sector"]} | Kết luận tài chính: {r["reason"]}',
               '⚠️ '+('; '.join(r['fin_warnings']) if r['fin_warnings'] else 'Chưa ghi nhận cảnh báo theo chỉ tiêu có sẵn') + ('; Không có kháng cự gần đủ tin cậy, không tính R:R' if r['target1'] is None else ''),
               '📌 ĐÁNH GIÁ & LƯU Ý: '+r['advice']]
        messages.append('\n'.join(lines))
    messages.append(f"KIỂM TOÁN TOÀN BỘ MÃ: {len(audit)}/{len(symbols)} | " + "; ".join(f"{k}: {v}" for k,v in counts.most_common()) + f"\nChi tiết: {audit_path}")
    if stale_candidates:
        messages.append('⏳ MÃ CÓ DỮ LIỆU CŨ — KHÔNG XẾP HẠNG PHIÊN NÀY:\n' + '\n'.join(f"• {r['symbol']} — phiên {r['last_date']}" for r in stale_candidates))
    if args.symbols: messages.append('LƯU Ý: Đây là kết quả thử theo danh sách mã, KHÔNG đại diện cho toàn thị trường.')
    if reference_date and ranked:
        save_signal_history(ranked,reference_date,scope)
    if not ranked: messages.append('Không có mã nào thỏa đồng thời cả 4 ngưỡng RSI/MFI, KL TB20 > 10.000 và >=15/20 phiên có giao dịch trong dữ liệu quét được hôm nay.')
    messages.append('BỘ LỌC THANH KHOẢN: Khối lượng TB20 > 10.000 CP và ít nhất 15/20 phiên gần nhất có volume > 0. Đây là điều kiện bắt buộc, không phải điểm thưởng.\nTHANG ĐIỂM: Quality gồm định giá 20, sinh lời 30, tăng trưởng 15, an toàn tài chính 20, thanh khoản 15 (chuẩn hóa theo chỉ tiêu có dữ liệu); Bottom gồm mức độ quá bán 20, RSI tăng 20, MFI tăng 15, giữ đáy 20, trên MA5 15, tỷ lệ khối lượng 10. Tổng hợp 60/40 chỉ là giả thuyết.\nLƯU Ý: Chỉ báo và điểm là mô hình thử nghiệm, chưa backtest. Không kiểm tra được đình chỉ giao dịch, kiểm toán, tin tức, điều chỉnh cổ tức từ API hiện tại. Không phải tín hiệu mua tự động.')
    report='\n\n'.join(messages)
    (OUTPUT/f'report_{date}{suffix}.txt').write_text(report,encoding='utf-8')
    print(report)
    if not args.no_telegram and args.slot == 'manual':
        for msg in messages:
            # Telegram giới hạn 4096 ký tự/tin; tách đoạn dài an toàn.
            chunks = [msg[i:i+3900] for i in range(0,len(msg),3900)]
            for chunk in chunks:
                try: send_telegram(chunk)
                except Exception as e: print('Lỗi Telegram:',e)
        # End-of-run summary already saved to TXT and CSV.
    if not args.no_telegram and args.slot != 'manual':
        send_daily_telegram(ranked, reference_date, args.slot, date, len(symbols), len(failures), len(stale_candidates))
    return ranked




def offline_backtest(args):
    """Backtest kỹ thuật chỉ dùng cache giá đã có; tuyệt đối không gọi KBS."""
    OUTPUT.mkdir(exist_ok=True)
    paths = sorted(CACHE.glob('price_*.pkl')) if CACHE.exists() else []
    # Một cache mới nhất mỗi mã; giữ nguyên dữ liệu lịch sử đã lưu.
    latest = {}
    for path in paths:
        stem = path.stem
        if not stem.startswith('price_') or '_' not in stem[6:]:
            continue
        symbol, day = stem[6:].rsplit('_', 1)
        if len(day) != 8 or not day.isdigit():
            continue
        if args.symbols and symbol not in args.symbols:
            continue
        if symbol not in latest or day > latest[symbol][0]:
            latest[symbol] = (day, path)
    if not latest:
        print('Không tìm thấy cache giá price_MA_YYYYMMDD.pkl trong bot_b_output/cache. KHÔNG gọi API.')
        return
    results=[]; examined=0; bad=0
    for sym, (_, path) in sorted(latest.items()):
        try:
            with path.open('rb') as f: raw=pickle.load(f)
            if not isinstance(raw, pd.DataFrame) or raw.empty:
                bad+=1; continue
            df=indicators(raw)
            if df is None or len(df)<86:
                bad+=1; continue
            df=df.reset_index(drop=True)
            # Tín hiệu được tính tại t, lợi nhuận tương lai t+N chỉ dùng để đánh giá.
            # Giới hạn bước nhảy lịch >10 ngày để tránh dữ liệu quá thưa.
            for t in range(65, len(df)-20):
                examined+=1
                window=df.iloc[:t+1]
                recent_dates=pd.to_datetime(window.time.tail(20),errors='coerce')
                if recent_dates.isna().any() or recent_dates.diff().dt.days.max()>10:
                    continue
                p=window.iloc[-1]
                values=[number(p[k]) for k in ('rsi14','rsi7','mfi14','mfi11')]
                if any(v is None for v in values) or not all(v<lim for v,lim in zip(values,(30,30,20,20))):
                    continue
                v20=float(window.volume.tail(20).mean())
                trades=int((pd.to_numeric(window.volume.tail(20),errors='coerce')>0).sum())
                if not (v20>10000 and trades>=15):
                    continue
                b=bottom(window)
                entry=float(p.close)
                if entry<=0: continue
                future=df.iloc[t+1:t+21]
                # Nếu thiếu ngày trong giai đoạn tương lai, không kết luận stop/target.
                fdates=pd.to_datetime(df.time.iloc[t:t+21],errors='coerce')
                gap_future=bool(fdates.isna().any() or fdates.diff().dt.days.max()>10)
                row={'symbol':sym,'signal_date':str(pd.Timestamp(p.time).date()),
                     'entry_close':entry,'bottom_score':b['score'],'status':b['status'],
                     'rsi14':b['rsi14'],'rsi7':b['rsi7'],'mfi14':b['mfi14'],'mfi11':b['mfi11'],
                     'volume_avg20':round(v20,0),'traded_sessions20':trades,
                     'stop':b['stop'],'target1':b['target1'],'rr1':b['rr1'],
                     'future_gap_warning':gap_future}
                for horizon in (5,10,20):
                    row[f'return_{horizon}d_pct']=round((float(df.close.iloc[t+horizon])/entry-1)*100,3) if not gap_future else None
                if not gap_future:
                    row['max_adverse_20d_pct']=round((float(future.low.min())/entry-1)*100,3)
                    row['max_favorable_20d_pct']=round((float(future.high.max())/entry-1)*100,3)
                    stop=b['stop']; target=b['target1']; outcome='NEITHER'
                    for _, frow in future.iterrows():
                        # Nếu cùng phiên chạm cả hai, không biết thứ tự intraday.
                        hit_stop=stop is not None and float(frow.low)<=stop
                        hit_target=target is not None and float(frow.high)>=target
                        if hit_stop and hit_target: outcome='BOTH_SAME_DAY_UNKNOWN'; break
                        if hit_stop: outcome='STOP_FIRST'; break
                        if hit_target: outcome='TARGET_FIRST'; break
                    row['first_hit_20d']=outcome
                results.append(row)
        except Exception as exc:
            bad+=1; print(f'  Cache lỗi {sym}: {exc}')
    date=datetime.now().strftime('%Y%m%d')
    detail=OUTPUT/f'backtest_offline_{date}.csv'
    summary=OUTPUT/f'backtest_offline_summary_{date}.csv'
    if results:
        out=pd.DataFrame(results).sort_values(['signal_date','symbol'])
        out.to_csv(detail,index=False,encoding='utf-8-sig')
        valid=out.dropna(subset=['return_20d_pct'])
        groups=[('ALL',valid),('BOTTOM_0_39',valid[valid.bottom_score<40]),
                ('BOTTOM_40_69',valid[(valid.bottom_score>=40)&(valid.bottom_score<70)]),
                ('BOTTOM_70_100',valid[valid.bottom_score>=70])]
        summaries=[]
        for name,g in groups:
            summaries.append({'group':name,'signals':len(g),'unique_symbols':g.symbol.nunique(),
                'win_rate_5d_pct':round((g.return_5d_pct>0).mean()*100,2) if len(g) else None,
                'win_rate_10d_pct':round((g.return_10d_pct>0).mean()*100,2) if len(g) else None,
                'win_rate_20d_pct':round((g.return_20d_pct>0).mean()*100,2) if len(g) else None,
                'mean_return_5d_pct':round(g.return_5d_pct.mean(),2) if len(g) else None,
                'mean_return_10d_pct':round(g.return_10d_pct.mean(),2) if len(g) else None,
                'mean_return_20d_pct':round(g.return_20d_pct.mean(),2) if len(g) else None,
                'mean_max_adverse_20d_pct':round(g.max_adverse_20d_pct.mean(),2) if len(g) else None})
        pd.DataFrame(summaries).to_csv(summary,index=False,encoding='utf-8-sig')
    print(f'BACKTEST OFFLINE: {len(latest)} mã có cache; {examined} ngày ứng viên; {len(results)} tín hiệu; {bad} mã thiếu/lỗi dữ liệu.')
    if results: print(f'Đã lưu: {detail} và {summary}')
    else: print('Không có tín hiệu đủ 6 điều kiện với 20 phiên tương lai trong cache. KHÔNG gọi API.')
    print('LƯU Ý: Chỉ backtest 6 điều kiện và Bottom; KHÔNG backtest Quality/nhóm A-B do chưa có tài chính point-in-time.')
    print('Các tín hiệu liên tiếp của cùng một mã có thể trùng lặp; kết quả chưa trừ phí/trượt giá và không phải hiệu quả danh mục.')


def realistic_backtest(args):
    """Mô phỏng giao dịch độc lập từ tín hiệu V2.6 + cache giá, không gọi API."""
    OUTPUT.mkdir(exist_ok=True)
    files=sorted(OUTPUT.glob('backtest_offline_????????.csv'))
    if args.backtest_input:
        source=Path(args.backtest_input)
    elif files:
        source=files[-1]
    else:
        print('Chưa có backtest_offline_YYYYMMDD.csv. Hãy chép file V2.6 vào bot_b_output; không gọi API.')
        return
    if not source.is_file():
        print(f'Không thấy file tín hiệu: {source}'); return
    signals=pd.read_csv(source,encoding='utf-8-sig',dtype={'symbol':str,'signal_date':str})
    needed={'symbol','signal_date','bottom_score','stop','target1'}
    if not needed.issubset(signals.columns):
        print('File tín hiệu thiếu cột:',', '.join(sorted(needed-set(signals.columns)))); return
    latest={}
    for path in CACHE.glob('price_*.pkl') if CACHE.exists() else []:
        stem=path.stem
        if '_' not in stem[6:]: continue
        symbol, day=stem[6:].rsplit('_',1)
        if len(day)!=8 or not day.isdigit(): continue
        if symbol not in latest or day>latest[symbol][0]: latest[symbol]=(day,path)
    signals=signals.sort_values(['symbol','signal_date']).reset_index(drop=True)
    cost_buy=args.bt_fee_pct+args.bt_slippage_pct
    cost_sell=args.bt_fee_pct+args.bt_slippage_pct+args.bt_sell_tax_pct
    rows=[]; missing=[]; skipped_overlap=0; skipped_history=0; ambiguous=0
    for sym, group in signals.groupby('symbol',sort=True):
        if sym not in latest:
            missing.append((sym,'missing_cache')); continue
        try:
            with latest[sym][1].open('rb') as f: df=pickle.load(f)
            if not isinstance(df,pd.DataFrame) or df.empty: raise ValueError('cache không có DataFrame giá')
            df=df.copy()
            df['time']=pd.to_datetime(df['time'],errors='coerce').dt.normalize()
            for c in ('open','high','low','close','volume'):
                df[c]=pd.to_numeric(df[c],errors='coerce')
            df=df.dropna(subset=['time','open','high','low','close']).sort_values('time').drop_duplicates('time').reset_index(drop=True)
            idx={t:i for i,t in enumerate(df.time)}
            blocked_until=-1
            for _, sig in group.iterrows():
                date=pd.to_datetime(sig.signal_date,errors='coerce')
                if pd.isna(date) or date.normalize() not in idx:
                    skipped_history+=1; continue
                t=idx[date.normalize()]
                if t<=blocked_until:
                    skipped_overlap+=1; continue
                entry_i=t+1
                if entry_i>=len(df): skipped_history+=1; continue
                # Chỉ dùng các phiên đủ dữ liệu, không gán khớp lệnh ở phiên mất giá.
                entry=df.iloc[entry_i]
                price=float(entry.open)
                if price<=0 or float(entry.volume)<=0:
                    skipped_history+=1; continue
                stop=number(sig['stop']); target=number(sig['target1'])
                if stop is not None and stop>=price: stop=None
                if target is not None and target<=price: target=None
                end_i=min(entry_i+args.bt_max_hold-1,len(df)-1)
                if args.bt_require_full_window and end_i-entry_i+1<args.bt_max_hold:
                    skipped_history+=1; continue
                segment=df.iloc[t:end_i+1]
                if segment.time.diff().dt.days.max()>10:
                    skipped_history+=1; continue
                exit_i=end_i; exit_px=float(df.iloc[end_i].close); reason='TIME_EXIT'
                for i in range(entry_i,end_i+1):
                    candle=df.iloc[i]
                    # Gap mở cửa: giá khớp ở open thay vì giả định khớp tại mức stop/target.
                    if stop is not None and float(candle.open)<=stop:
                        exit_i=i;exit_px=float(candle.open);reason='STOP_GAP';break
                    if target is not None and float(candle.open)>=target:
                        exit_i=i;exit_px=float(candle.open);reason='TARGET_GAP';break
                    hit_s=stop is not None and float(candle.low)<=stop
                    hit_t=target is not None and float(candle.high)>=target
                    if hit_s and hit_t:
                        ambiguous+=1
                        exit_i=i;exit_px=stop;reason='BOTH_SAME_DAY_STOP_ASSUMED';break
                    if hit_s:exit_i=i;exit_px=stop;reason='STOP';break
                    if hit_t:exit_i=i;exit_px=target;reason='TARGET';break
                gross=(exit_px/price-1)*100
                net=((exit_px*(1-cost_sell/100))/(price*(1+cost_buy/100))-1)*100
                rows.append({'symbol':sym,'signal_date':sig.signal_date,'entry_date':str(entry.time.date()),
                    'exit_date':str(df.iloc[exit_i].time.date()),'bottom_score':sig.bottom_score,
                    'entry_open':round(price,5),'exit_price':round(exit_px,5),
                    'stop':stop,'target1':target,'exit_reason':reason,'holding_sessions':exit_i-entry_i+1,
                    'gross_return_pct':round(gross,4),'net_return_pct':round(net,4),
                    'fee_each_side_pct':args.bt_fee_pct,'slippage_each_side_pct':args.bt_slippage_pct,
                    'sell_tax_pct':args.bt_sell_tax_pct})
                blocked_until=exit_i
        except Exception as exc:
            missing.append((sym,str(exc)))
    tag=datetime.now().strftime('%Y%m%d')
    detail=OUTPUT/f'backtest_realistic_{tag}.csv'
    summary=OUTPUT/f'backtest_realistic_summary_{tag}.csv'
    pd.DataFrame(rows).to_csv(detail,index=False,encoding='utf-8-sig')
    if rows:
        out=pd.DataFrame(rows)
        groups=[('ALL',out),('BOTTOM_0_39',out[out.bottom_score<40]),
            ('BOTTOM_40_69',out[(out.bottom_score>=40)&(out.bottom_score<70)]),
            ('BOTTOM_70_100',out[out.bottom_score>=70])]
        summaries=[]
        for name,g in groups:
            summaries.append({'group':name,'trades':len(g),'unique_symbols':g.symbol.nunique(),
                'net_win_rate_pct':round((g.net_return_pct>0).mean()*100,2) if len(g) else None,
                'avg_gross_return_pct':round(g.gross_return_pct.mean(),3) if len(g) else None,
                'avg_net_return_pct':round(g.net_return_pct.mean(),3) if len(g) else None,
                'median_net_return_pct':round(g.net_return_pct.median(),3) if len(g) else None,
                'avg_holding_sessions':round(g.holding_sessions.mean(),2) if len(g) else None,
                'stop_exits':int(g.exit_reason.str.contains('STOP').sum()),
                'target_exits':int(g.exit_reason.str.contains('TARGET').sum()),
                'time_exits':int((g.exit_reason=='TIME_EXIT').sum())})
        pd.DataFrame(summaries).to_csv(summary,index=False,encoding='utf-8-sig')
    print(f'BACKTEST V2.7 OFFLINE | nguồn: {source} | tín hiệu: {len(signals)} | giao dịch độc lập: {len(rows)}')
    print(f'Bỏ qua tín hiệu trùng vị thế: {skipped_overlap} | thiếu lịch sử/phiên giao dịch: {skipped_history} | mã thiếu/lỗi cache: {len(missing)}')
    print(f'Phiên chạm cả stop và target (giả định stop trước): {ambiguous}')
    print(f'Đã lưu: {detail} và {summary}')
    print('LƯU Ý: Chỉ backtest kỹ thuật Bottom; không có Quality point-in-time; chưa tính giới hạn biên độ, khớp lệnh, cổ tức và khả năng thoát khi dư bán sàn.')
    if missing:
        pd.DataFrame(missing,columns=['symbol','reason']).to_csv(OUTPUT/f'backtest_realistic_missing_{tag}.csv',index=False,encoding='utf-8-sig')


def audit_realistic_backtest(args):
    """Offline robustness audit. Does not alter trade fills or validate corporate actions."""
    import numpy as np
    source=Path(args.audit_input) if args.audit_input else sorted(OUTPUT.glob('backtest_realistic_*.csv'))
    if isinstance(source,list):
        source=[p for p in source if 'summary' not in p.name and 'audit' not in p.name]
        if not source:
            print('CHƯA CÓ CSV V2.7. Hãy cung cấp --audit-input <file.csv>');return
        source=source[-1]
    if not source.exists():
        print(f'KHÔNG TÌM THẤY: {source}');return
    df=pd.read_csv(source,encoding='utf-8-sig')
    required={'symbol','entry_date','net_return_pct','bottom_score','exit_reason','entry_open','exit_price'}
    if not required.issubset(df.columns):
        print('CSV thiếu cột: '+', '.join(sorted(required-set(df.columns))));return
    for col in ('net_return_pct','bottom_score','entry_open','exit_price','gross_return_pct','holding_sessions'):
        if col in df: df[col]=pd.to_numeric(df[col],errors='coerce')
    df['entry_date']=pd.to_datetime(df['entry_date'],errors='coerce')
    df=df.dropna(subset=['symbol','entry_date','net_return_pct','bottom_score']).copy()
    df['bottom_group']=pd.cut(df.bottom_score,[-float('inf'),40,70,float('inf')],right=False,labels=['BOTTOM_0_39','BOTTOM_40_69','BOTTOM_70_100'])
    df['quarter']=df.entry_date.dt.to_period('Q').astype(str)
    # Flag potential corporate-action/price-data issues for manual review, not automatic deletion.
    df['flag_extreme']=df.net_return_pct.abs()>=args.audit_extreme_pct
    df['flag_bad_price']=(df.entry_open<=0)|(df.exit_price<=0)
    df['flag_review']=df.flag_extreme|df.flag_bad_price
    detail=OUTPUT/f'backtest_audit_flags_{datetime.now():%Y%m%d}.csv'
    df.loc[df.flag_review].sort_values('net_return_pct').to_csv(detail,index=False,encoding='utf-8-sig')
    def summarize(g,label,period):
        x=g.net_return_pct.dropna()
        if x.empty: return None
        lo,hi=x.quantile([.05,.95]); wins=x.clip(lo,hi)
        # 5% trimmed mean; median is resistant to outliers.
        ordered=x.sort_values();cut=int(len(ordered)*.05)
        trimmed=ordered.iloc[cut:len(ordered)-cut] if cut and len(ordered)>2*cut else ordered
        clean=g.loc[~g.flag_review,'net_return_pct']
        return dict(group=label,period=period,trades=len(g),symbols=g.symbol.nunique(),
                    mean_net_pct=round(x.mean(),3),median_net_pct=round(x.median(),3),
                    win_rate_pct=round((x>0).mean()*100,2),
                    trimmed_mean_5pct=round(trimmed.mean(),3),winsor_mean_5pct=round(wins.mean(),3),
                    mean_without_flags_pct=round(clean.mean(),3) if len(clean) else None,
                    flagged_trades=int(g.flag_review.sum()),stop_exits=int(g.exit_reason.astype(str).str.contains('STOP').sum()))
    rows=[]
    for label,g in [('ALL',df)]+[(str(k),v) for k,v in df.groupby('bottom_group',observed=True)]:
        item=summarize(g,label,'ALL');
        if item: rows.append(item)
        for period,sub in g.groupby('quarter'):
            item=summarize(sub,label,period)
            if item:rows.append(item)
    summary=OUTPUT/f'backtest_audit_summary_{datetime.now():%Y%m%d}.csv'
    pd.DataFrame(rows).to_csv(summary,index=False,encoding='utf-8-sig')
    print(f'KIỂM TOÁN V2.8 OFFLINE | nguồn: {source} | giao dịch: {len(df)} | mã: {df.symbol.nunique()}')
    print(f'Giao dịch cần kiểm tra (|lợi nhuận ròng| >= {args.audit_extreme_pct:g}% hoặc giá không hợp lệ): {int(df.flag_review.sum())}')
    for row in rows:
        if row['period']=='ALL':
            print(f"  {row['group']}: n={row['trades']} | TB={row['mean_net_pct']:+.2f}% | trung vị={row['median_net_pct']:+.2f}% | trimmed={row['trimmed_mean_5pct']:+.2f}% | bỏ cờ={row['mean_without_flags_pct']}")
    print(f'Đã lưu: {summary} và {detail}')
    print('CẢNH BÁO: Lọc ngoại lệ chỉ để kiểm tra độ nhạy, KHÔNG xác minh điều chỉnh giá/cổ tức. Các giao dịch có thể phụ thuộc cùng mã và cùng giai đoạn; chưa có dữ liệu tài chính point-in-time.')

if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--symbols',nargs='+',help='Quét thử các mã cụ thể')
    parser.add_argument('--no-telegram',action='store_true')
    parser.add_argument('--slot',choices=['manual','morning','noon'],default='manual',help='morning: dùng cache gần nhất; noon: cập nhật giá mới rồi gửi Telegram')
    parser.add_argument('--telegram-test',action='store_true',help='Gửi tin nhắn Telegram kiểm tra kết nối, không quét')
    parser.add_argument('--telegram-resend',action='store_true',help='Gửi lại báo cáo CSV đã lưu, không gọi KBS, không quét')
    parser.add_argument('--resend-file',type=str,help='Đường dẫn CSV báo cáo cụ thể để gửi lại')
    parser.add_argument('--delay',type=float,default=2.5)
    parser.add_argument('--refresh',action='store_true',help='Bỏ cache để lấy lại dữ liệu từ KBS')
    parser.add_argument('--inspect-bank',nargs='+',metavar='MA',help='Kiểm tra NPL/CAR/NIM trong dữ liệu tài chính KBS')
    parser.add_argument('--explain',nargs='+',metavar='MA',help='Giải thích 6 điều kiện cho từng mã, kể cả mã bị loại; không quét toàn thị trường')
    parser.add_argument('--evaluate-history',action='store_true',help='Đánh giá hiệu quả 5/10/20 phiên của tín hiệu đã lưu')
    parser.add_argument('--backtest-offline',action='store_true',help='Backtest 6 bộ lọc và Bottom chỉ bằng cache, không gọi API')
    parser.add_argument('--audit-realistic',action='store_true',help='Kiểm toán V2.7 bằng CSV offline, không gọi API')
    parser.add_argument('--audit-input',type=str,help='Đường dẫn CSV backtest V2.7')
    parser.add_argument('--audit-extreme-pct',type=float,default=30,help='Ngưỡng gắn cờ lợi nhuận bất thường theo trị tuyệt đối, mặc định 30%%')
    parser.add_argument('--backtest-realistic',action='store_true',help='Backtest độc lập từ CSV V2.6 và cache; không gọi API')
    parser.add_argument('--backtest-input',type=str,help='Đường dẫn CSV tín hiệu V2.6')
    parser.add_argument('--bt-fee-pct',type=float,default=0.15,help='Phí mỗi chiều, phần trăm')
    parser.add_argument('--bt-slippage-pct',type=float,default=0.10,help='Trượt giá mỗi chiều, phần trăm')
    parser.add_argument('--bt-sell-tax-pct',type=float,default=0.10,help='Thuế bán, phần trăm')
    parser.add_argument('--bt-max-hold',type=int,default=20,help='Số phiên nắm giữ tối đa')
    parser.add_argument('--bt-require-full-window',action='store_true',help='Bỏ qua tín hiệu chưa có đủ 20 phiên tương lai')
    args = parser.parse_args()
    if args.bt_max_hold<1 or min(args.bt_fee_pct,args.bt_slippage_pct,args.bt_sell_tax_pct)<0: parser.error('Phí/trượt giá không âm; số phiên nắm giữ >=1')
    if args.telegram_test:
        send_telegram('✅ BOT B đã kết nối Telegram thành công. Lịch gửi: 06:00 và 11:31 (T2-T6).')
        print('Đã gửi tin nhắn thử Telegram.')
    elif args.telegram_resend: resend_saved_telegram(args)
    elif args.audit_realistic: audit_realistic_backtest(args)
    elif args.backtest_realistic: realistic_backtest(args)
    elif args.backtest_offline: offline_backtest(args)
    elif args.evaluate_history: evaluate_history(args)
    elif args.inspect_bank: inspect_bank(args)
    elif args.explain: explain_symbols(args)
    else: run(args)

