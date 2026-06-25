import os
import json
import time
import uuid
import urllib3
import threading
from datetime import datetime, time as dt_time
from flask import Flask, request, jsonify
from decimal import Decimal, InvalidOperation

# 인증서 검증 생략 경고 로그 차단
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

APP_VERSION = "V13-2026-06-09-KOR-Futures-Dual"
print(f"### APP_VERSION BOOT: {APP_VERSION} ###")

app = Flask(__name__)

http_pool = urllib3.PoolManager(
    cert_reqs='CERT_NONE',
    maxsize=20,
    headers={'Connection': 'keep-alive'}
)

HANTU_BASE_URL = "https://openapi.koreainvestment.com:9443"

# 📡 국내선물옵션 전용 실전 엔드포인트 패스
HANTU_KOR_ORDER_PATH = "/uapi/domestic-futureoption/v1/trading/order"

# 🔐 환경변수 금고 개방
APP_KEY = os.environ.get("HANTU_APPKEY", "").strip()
APP_SECRET = os.environ.get("HANTU_APPSECRET", "").strip()
RAW_ACCOUNT = os.environ.get("HANTU_ACCOUNT", "").replace("-", "").strip()

try:
    RAW_OFFSET = os.environ.get("HANTU_VIRTUAL_OFFSET", "0").strip()
    HANTU_VIRTUAL_OFFSET = int(RAW_OFFSET)
except Exception:
    HANTU_VIRTUAL_OFFSET = 0

_HANTU_TOKEN = None
_HANTU_TOKEN_EXPIRES_AT = 0

# ==========================================
# ⏱️ [V13 핵심] 주간 정규장 vs 야간장 실시간 시간 판별기
# ==========================================
def get_current_market_tr():
    """
    한국 시간(KST) 기준으로 현재 주간 정규장인지 야간장인지 판별하여 올바른 TR_ID를 하달합니다.
    - 주간 정규장: 08시 45분 ~ 15시 45분 ➡️ CCF00701U (국내선원장 주문)
    - 야간 유렉스: 18시 00분 ~ 다음날 05시 00분 ➡️ CFM00601U (CME/유렉스 연계 주문)
    """
    now = datetime.now()
    current_time = now.time()
    
    day_start = dt_time(8, 45, 0)
    day_end = dt_time(15, 45, 0)
    night_start = dt_time(18, 0, 0)
    night_tomorrow_end = dt_time(5, 0, 0)
    
    # 주간 정규장 구역 검측
    if day_start <= current_time <= day_end:
        return "CCF00701U", "주간 정규장"
        
    # 야간장 구역 검측 (당일 밤 18시~24시 OR 익일 새벽 00시~05시)
    if current_time >= night_start or current_time <= night_tomorrow_end:
        return "CFM00601U", "야간 유렉스장"
        
    # 장 마감 후 휴식 구역 (15:45 ~ 18:00)
    return "CCF00701U", "장외 대기정산(기본 주간설정)"


# ==========================================
# 🛠️ [사령관님 안전 패치] 초정밀 수량 계산 모듈
# ==========================================
def get_account_parts():
    if len(RAW_ACCOUNT) < 10: 
        raise ValueError(f"HANTU_ACCOUNT 길이가 부적합합니다: {RAW_ACCOUNT!r}")
    # 국내 주식/선물은 앞 8자리 종합계좌번호, 뒤 2자리 상품코드로 매핑
    return str(RAW_ACCOUNT[:8]).strip(), str(RAW_ACCOUNT[8:10]).strip()

def mask_text(value, keep=4):
    if value is None: return value
    value = str(value)
    return value[:keep] + "***" if len(value) > keep else "*" * len(value)

def sanitize_body(body):
    masked = dict(body)
    if "CANO" in masked: masked["CANO"] = mask_text(masked["CANO"])
    if "ACNT_PRDT_CD" in masked: masked["ACNT_PRDT_CD"] = "**"
    return masked

def get_hantu_token():
    global _HANTU_TOKEN, _HANTU_TOKEN_EXPIRES_AT
    now = time.time()
    if _HANTU_TOKEN and now < _HANTU_TOKEN_EXPIRES_AT: return _HANTU_TOKEN
    
    url = f"{HANTU_BASE_URL}/oauth2/tokenP"
    body = {"grant_type": "client_credentials", "appkey": APP_KEY, "appsecret": APP_SECRET}
    try:
        tm = urllib3.Timeout(connect=10.0, read=10.0)
        res = http_pool.request('POST', url, headers={"content-type": "application/json; charset=utf-8"}, body=json.dumps(body), timeout=tm)
        if res.status == 200:
            res_data = json.loads(res.data.decode("utf-8"))
            _HANTU_TOKEN = res_data.get("access_token")
            _HANTU_TOKEN_EXPIRES_AT = now + max(int(res_data.get("expires_in", 86400)) - 60, 0)
            print("🟢 [보안 통과] 국내선물용 실전 토큰 발급 완료!")
            return _HANTU_TOKEN
        return None
    except Exception as e:
        print(f"❌ [토큰 발급 치명적 예외]: {str(e)}")
        return None


# ==========================================
# 📡 [V13 국내 규격] 국내선물 미결제 잔고 조회소 (주/야간 통합형)
# ==========================================
def get_domestic_futures_balance(token, target_ticker):
    """
    국내선물 미결제 잔고조회 API (주간/야간 원장이 한투 내부에서 자동 통합 집계되는 TR 반영)
    TR_ID: 주간/야간 통합 잔고용 'OFOA0244R' 혹은 '정규잔고조회' 기준 매핑
    """
    cano, acnt_prdt_cd = get_account_parts()
    url = f"{HANTU_BASE_URL}/uapi/domestic-futureoption/v1/trading/inquire-balance"
    
    headers = {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {token}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": "OFOA0244R",  # 🎯 국내선물옵션 잔고전용 무결점 TR_ID
        "custtype": "P",
    }
    
    params = {
        "CANO": cano,
        "ACNT_PRDT_CD": acnt_prdt_cd,
        "TRADE_DVSN_CD": "01", # 01: 선물
        "CTX_AREA_FK100": "",
        "CTX_AREA_NK100": ""
    }
    
    try:
        tm = urllib3.Timeout(connect=10.0, read=15.0)
        res = http_pool.request("GET", url, headers=headers, fields=params, timeout=tm)
        if res.status != 200: return None
        
        data = json.loads(res.data.decode("utf-8", errors="replace"))
        if data.get("rt_cd") != "0": return None
        
        rows = data.get("output1", []) or []
        net_position = 0
        
        for row in rows:
            # 국내선물 종목코드 매핑 (예: KOSPI200 지수선물 표준코드 등)
            item_code = str(row.get("pdno", "")).strip()
            if item_code == target_ticker or item_code == f"KR4{target_ticker}":
                qty = int(row.get("pblc_bclc_qty", 0)) # 매매가능 미결제수량
                side = str(row.get("sll_buy_dvsn_cd", "")).strip() # 01: 매도, 02: 매수
                
                if side == "02": net_position += qty
                elif side == "01": net_position -= qty
                
        return net_position
    except Exception as e:
        print(f"❌ [국내선물 잔고 조회 실패]: {str(e)}")
        return None


# ==========================================
# 💥 [주/야간 자동 격격] 국내선물 실전 원거리 포격 모듈
# ==========================================
def async_domestic_order_execute(ticker, action, qty, token):
    correlation_id = str(uuid.uuid4())[:8]
    cano, prdt_cd = get_account_parts()
    
    # 🎯 현재 시각 기준 주간/야간 TR_ID 자동 배정 장치 기동!
    tr_id, market_label = get_current_market_tr()
    
    order_url = f"{HANTU_BASE_URL}{HANTU_KOR_ORDER_PATH}"
    side_code = "2" if action == "buy" else "1"  # 💥 국내선물 규격: 1=매도, 2=매수
    
    headers = {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {token}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": tr_id, # ⚖️ 스위칭된 주/야간 탄두 탑재
        "custtype": "P"
    }
    
    body = {
        "CANO": cano,
        "ACNT_PRDT_CD": prdt_cd,
        "PDNO": ticker,                     # 국내선물 종목코드 (예: 101W06)
        "SLL_BUY_DVSN_CD": side_code,       # 1: 매도, 2: 매수
        "ORD_DVSN_CD": "00",                # 00: 지정가 (국내선물은 야간장에 시장가가 제한되므로 기본 00 지정가 유력)
        "ORD_PRC": "0.00",                  # 시장가 혹은 최우선 지정가 타격 시 조건 처리 대기
        "ORD_QTY": str(int(qty))            # 사령관님 패치: 깔끔한 정수 변환 수량
    }
    
    # 국내선물 시장가 처리 고도화 (국내장 전용 코드)
    # 주간 정규장의 경우 "03"(최유리 지정가)이나 "04"(최우선 지정가)를 사용해 실시간 슬리피지를 방어합니다.
    body["ORD_DVSN_CD"] = "03" 
    body["ORD_PRC"] = "0" # 최유리 지정가 타격 시 가격은 0 세팅
    
    try:
        print(f"[DOMESTIC ORDER][{correlation_id}][{market_label}] 포격 개시 ➡️ TR={tr_id} body={json.dumps(sanitize_body(body))}")
        tm = urllib3.Timeout(connect=15.0, read=15.0)
        res = http_pool.request('POST', order_url, headers=headers, body=json.dumps(body), timeout=tm)
        parsed = json.loads(res.data.decode('utf-8', errors='replace'))
        print(f"🎯 [국내 원장 응답][{correlation_id}] rt_cd={parsed.get('rt_cd')} / msg={parsed.get('msg1')}")
    except Exception as e:
        print(f"❌ [국내선물 실전 포격 실패][{correlation_id}]: {str(e)}")


@app.route('/webhook', methods=['POST'])
def webhook():
    payload = request.get_json(silent=True)
    if not payload: return jsonify({"status": "fail", "message": "JSON body missing"}), 400
    
    print(f"📡 [레이더 포착] 국내선물 차트 신호 수신: {payload}")
    
    try:
        ticker = str(payload.get("ticker", "")).strip() # 예: KOSPI200 최근월물 코드 등
        if not ticker: return jsonify({"status": "fail", "message": "ticker missing"}), 400
        
        token = get_hantu_token()
        if not token: return jsonify({"status": "fail", "message": "token issue"}), 500
        
        if "strategy_position" in payload:
            tv_position = int(payload.get("strategy_position", 0))
            
            # 사령관님의 무결점 net_qty 저울질 연산 가동
            hantu_balance = get_domestic_futures_balance(token, ticker)
            
            if hantu_balance is None:
                print("❌ [🚨비상] 국내선물 잔고 조회 실패! 계좌 사수를 위해 주문 기각 처리.")
                return jsonify({"accepted": False, "status": "error", "message": "Domestic balance fetch error."}), 500
                
            adjusted_tv_target = tv_position - HANTU_VIRTUAL_OFFSET
            position_gap = adjusted_tv_target - hantu_balance
            
            print(f"⚖️ [국내 차액 정산] TV목표: {tv_position} | 오프셋: {HANTU_VIRTUAL_OFFSET} | 보정목표: {adjusted_tv_target} | 국내넷잔고: {hantu_balance} | 격발수량: {position_gap}")
            
            if position_gap > 0:
                action = "buy"
                qty = abs(position_gap)
            elif position_gap < 0:
                action = "sell"
                qty = abs(position_gap)
            else:
                print("🎯 [영점 수렴] 차트 포지션과 국내선물 실전 잔고가 일치합니다. 매매 스킵.")
                return jsonify({"accepted": True, "status": "synchronized"}), 200
        else:
            action = str(payload.get("action", "")).strip().lower()
            qty = int(payload.get("qty", 1))
            
        # 🚀 비동기 멀티스레딩 화력 투하
        t = threading.Thread(target=async_domestic_order_execute, args=(ticker, action, qty, token), daemon=True)
        t.start()
        
        return jsonify({"accepted": True, "version": APP_VERSION, "ticker": ticker, "action": action, "qty": qty}), 202
        
    except Exception as e:
        print(f"❌ [국내 웹훅 예외]: {str(e)}")
        return jsonify({"status": "fail", "message": "internal error"}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", "10000")))
