import os
import requests
import time
from flask import Flask, request, jsonify

app = Flask(__name__)

# -------------------------------------------------------------
# [1] 한투 API 무기고 세팅 (렌더닷컴 환경변수에서 암호를 불러옵니다)
# -------------------------------------------------------------
APP_KEY = os.environ.get("APP_KEY")
APP_SECRET = os.environ.get("APP_SECRET")
CANO = os.environ.get("CANO")                 # 계좌번호 앞 8자리
PRDT_BRNO = os.environ.get("PRDT_BRNO", "01") # 계좌번호 뒤 2자리 (보통 '01')

# URL 설정: 실전투자는 openapi, 모의투자는 openapivts
URL_BASE = os.environ.get("URL_BASE", "https://openapi.koreainvestment.com:9443")

# 토큰 재장전 캐시 변수
ACCESS_TOKEN = ""
TOKEN_ISSUE_TIME = 0

# -------------------------------------------------------------
# [2] 한투 접속 권한(토큰) 발급 부대
# -------------------------------------------------------------
def get_access_token():
    global ACCESS_TOKEN, TOKEN_ISSUE_TIME
    # 토큰이 있고 발급된 지 20시간(72000초)이 안 지났으면 기존 토큰 재사용
    if ACCESS_TOKEN and (time.time() - TOKEN_ISSUE_TIME < 72000):
        return ACCESS_TOKEN

    url = f"{URL_BASE}/oauth2/tokenP"
    headers = {"content-type": "application/json"}
    data = {
        "grant_type": "client_credentials",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET
    }
    res = requests.post(url, headers=headers, json=data)
    ACCESS_TOKEN = res.json().get("access_token")
    TOKEN_ISSUE_TIME = time.time()
    return ACCESS_TOKEN

# -------------------------------------------------------------
# [3] 한투 보안 해시키(Hashkey) 생성기
# -------------------------------------------------------------
def get_hashkey(datas):
    url = f"{URL_BASE}/uapi/hashkey"
    headers = {
        "content-type": "application/json",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET
    }
    res = requests.post(url, headers=headers, json=datas)
    return res.json().get("HASH")

# -------------------------------------------------------------
# [4] 실제 매수/매도 포격 명령 하달 부대 (국내/해외 지능형 분기)
# -------------------------------------------------------------
def order_stock(ticker, action, qty, market="KR", price="0", excd="NAS"):
    token = get_access_token()

    # 🇺🇸 미국 주식 사격 제원 세팅
    if market == "US":
        url = f"{URL_BASE}/uapi/overseas-stock/v1/trading/order"
        # 미국 실전투자 TR_ID (매수: JTTT1002U / 매도: JTTT1006U)
        # (만약 모의투자라면 VTTT1002U / VTTT1006U 로 변경 필요)
        tr_id = "JTTT1002U" if action == "buy" else "JTTT1006U"
        data = {
            "CANO": CANO,
            "ACNT_PRDT_CD": PRDT_BRNO,
            "OVRS_EXCG_CD": excd,        # 거래소코드 (NAS, NYSE, AMEX)
            "PDNO": ticker,              # 종목코드 (예: TQQQ)
            "ORD_QTY": str(qty),         # 주문 수량
            "OVRS_ORD_UNPR": str(price), # 주문 가격 (미국은 지정가 필수)
            "ORD_DVSN": "00",            # 00: 지정가
            "ORD_SVR_DVSN_CD": "0"
        }

    # 🇰🇷 국내 주식 사격 제원 세팅 (기존 로직 완벽 보존)
    else:
        url = f"{URL_BASE}/uapi/domestic-stock/v1/trading/order-cash"
        # 국내 실전투자 TR_ID (매수: TTTC0802U / 매도: TTTC0801U)
        tr_id = "TTTC0802U" if action == "buy" else "TTTC0801U"
        data = {
            "CANO": CANO,
            "ACNT_PRDT_CD": PRDT_BRNO,
            "PDNO": ticker,     # 종목코드 (예: 005930)
            "ORD_DVSN": "01",   # 01: 시장가 주문
            "ORD_QTY": str(qty),# 주문 수량
            "ORD_UNPR": "0"     # 시장가이므로 가격은 0원으로 세팅
        }

    headers = {
        "content-type": "application/json",
        "authorization": f"Bearer {token}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": tr_id,
        "custtype": "P",
        "hashkey": get_hashkey(data)
    }

    res = requests.post(url, headers=headers, json=data)
    return res.json()

# -------------------------------------------------------------
# [5] 트레이딩뷰 무전 수신소 (웹훅 라우터)
# -------------------------------------------------------------
@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.json
    
    # 트레이딩뷰가 보낸 JSON 암호문 해독
    ticker = data.get("ticker")           # 종목코드
    action = data.get("action")           # buy 또는 sell
    qty = data.get("qty")                 # 수량
    
    # 🌟 새롭게 추가된 한미 연합 제원 🌟
    market = data.get("market", "KR")     # "KR" 또는 "US" (안 보내면 국내로 간주)
    price = data.get("price", "0")        # 미국주식용 가격 (국내는 무시됨)
    excd = data.get("excd", "NAS")        # 미국 거래소 (기본값 나스닥)

    # 비정상적인 신호는 요격(무시)
    if not ticker or not action or not qty:
        return jsonify({"msg": "사격 제원 누락"}), 400

    try:
        # 정상 신호일 경우 포격 명령 함수 호출
        result = order_stock(ticker, action, qty, market, price, excd)
        print(f"🎯 [{market} 타격완료] {action} {ticker} {qty}주 / 결과: {result}")
        return jsonify({"msg": "타격 명령 하달 성공", "result": result}), 200
    except Exception as e:
        print(f"🚨 [에러발생] 무기 체계 오류: {e}")
        return jsonify({"msg": "서버 오류 발생", "error": str(e)}), 500

# -------------------------------------------------------------
# 서버 가동
# -------------------------------------------------------------
if __name__ == '__main__':
    # 렌더닷컴(클라우드) 외부 접속 허용을 위해 host를 0.0.0.0으로 세팅
    app.run(host='0.0.0.0', port=8080)
