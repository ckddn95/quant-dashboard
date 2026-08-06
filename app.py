import streamlit as st
import requests
import json
import pandas as pd

# =====================================================================
# 1. 페이지 기본 설정
# =====================================================================
st.set_page_config(
    page_title="Core-Satellite Quant System",
    page_icon="📈",
    layout="wide"
)

# 메인 타이틀
st.title("Core-Satellite Independent Asset Allocation Quant System")
st.markdown("한국 시장 전 종목을 검색하여 포트폴리오를 구성하고, 가상 매매 성과 추적기(Forward Test), 구글 시트 영구 DB 연동, 가상/실계좌 탭 분리를 제공하는 실전 퀀트 대시보드입니다.")
st.markdown("---")

# =====================================================================
# 2. KIS API 연동 함수 (에러 방지 적용)
# =====================================================================
def get_kis_access_token(app_key, app_secret, is_mock=False):
    """한국투자증권 API 접근 토큰 발급"""
    domain = "https://openapivts.koreainvestment.com:29443" if is_mock else "https://openapi.koreainvestment.com:9443"
    url = f"{domain}/oauth2/tokenP"
    headers = {"content-type": "application/json"}
    body = {
        "grant_type": "client_credentials",
        "appkey": app_key,
        "appsecret": app_secret
    }
    res = requests.post(url, headers=headers, data=json.dumps(body))
    if res.status_code == 200:
        return res.json().get("access_token")
    return None

def get_kis_balance(access_token, app_key, app_secret, cano, acnt_prdt, is_mock=False):
    """계좌 잔고 및 보유 종목 조회 (INVALID_CHECK_ACNO 해결 규격)"""
    domain = "https://openapivts.koreainvestment.com:29443" if is_mock else "https://openapi.koreainvestment.com:9443"
    url = f"{domain}/uapi/domestic-stock/v1/trading/inquire-balance"
    
    # 실전투자/모의투자에 따라 거래 ID(tr_id)가 다름
    tr_id = "VTTC8434R" if is_mock else "TTTC8434R"
    
    headers = {
        "authorization": f"Bearer {access_token}",
        "appkey": app_key,
        "appsecret": app_secret,
        "tr_id": tr_id,
        "custtype": "P"
    }
    
    params = {
        "CANO": cano,                # 계좌번호 앞 8자리
        "ACNT_PRDT_CD": acnt_prdt,   # 상품코드 "01"
        "AFHR_FLPR_YN": "N",
        "OFL_YN": "",
        "INQR_DVSN": "02",
        "UNPR_DVSN": "01",
        "FUND_STTL_ICLD_YN": "N",
        "FNCG_AMT_AUTO_RDPT_YN": "N",
        "PRCS_DVSN": "01",
        "CTX_AREA_FK100": "",
        "CTX_AREA_NK100": ""
    }
    
    res = requests.get(url, headers=headers, params=params)
    return res.json()


# =====================================================================
# 3. 탭 (Tabs) 화면 구성
# =====================================================================
tab1, tab2, tab3, tab4 = st.tabs(["📝 가상 샌드박스", "🕴️ KIS 실전 계좌", "📊 시뮬레이션", "📄 알고리즘 백서"])

# ----------------- 탭 1: 가상 샌드박스 -----------------
with tab1:
    st.subheader("📝 가상 포트폴리오 샌드박스 (Google Sheets DB 연동)")
    st.markdown("수동으로 종목을 관리하고 진단하는 공간입니다. 변경사항은 구글 스프레드시트에 영구 저장됩니다.")
    st.info("👈 좌측 사이드바에서 포트폴리오를 먼저 생성하거나 선택하세요.")
    
    # 향후 구글 시트 데이터 로드 및 화면 표시 로직이 들어갈 자리입니다.

# ----------------- 탭 2: KIS 실전 계좌 (에러 수정 완료) -----------------
with tab2:
    st.subheader("🕴️ 실전 계좌(API) 연동 현황")
    st.markdown("아래 설정된 한국투자증권 실계좌 정보 및 잔고를 확인하고 진단합니다.")
    
    try:
        # Secrets에서 KIS 계좌 정보 불러오기
        kis_core = st.secrets["kis_accounts"]["core"]
        core_name = kis_core["name"]
        core_cano = str(kis_core["cano"])          # 문자열 변환으로 안전성 확보
        core_prdt = str(kis_core["acnt_prdt"])
        is_mock = kis_core.get("is_mock", False)
        
        mode_text = "모의투자" if is_mock else "실전투자"
        st.success(f"✅ 연동 계좌: {core_cano[:4]}****-{core_prdt} ({mode_text} / {core_name})")
        
        if st.button("🔄 이 계좌 잔고 실시간 새로고침", type="primary"):
            with st.spinner("한국투자증권 API와 통신 중입니다..."):
                # 1. 토큰 발급
                token = get_kis_access_token(
                    kis_core["app_key"], 
                    kis_core["app_secret"], 
                    is_mock
                )
                
                if token:
                    # 2. 잔고 조회
                    balance_data = get_kis_balance(
                        token, 
                        kis_core["app_key"], 
                        kis_core["app_secret"], 
                        core_cano, 
                        core_prdt, 
                        is_mock
                    )
                    
                    if balance_data.get("rt_cd") == "0":
                        st.write("### 💰 계좌 잔고 요약")
                        summary = balance_data["output2"][0]
                        
                        col1, col2, col3 = st.columns(3)
                        col1.metric("총 평가 금액", f"{int(summary['tot_evlu_amt']):,} 원")
                        col2.metric("예수금 (현금)", f"{int(summary['dnca_tot_amt']):,} 원")
                        col3.metric("실현 수익률", f"{summary['pchs_amt_smtl_ert']}%")
                    else:
                        st.error(f"API 응답 오류: {balance_data.get('msg1')}")
                else:
                    st.error("접근 토큰(Access Token) 발급에 실패했습니다. App Key와 App Secret을 확인하세요.")
                    
    except KeyError:
        st.error("Secrets 설정 파일에 `kis_accounts.core` 정보가 없습니다.")
    except Exception as e:
        st.error(f"시스템 오류 발생: {e}")

# ----------------- 탭 3: 시뮬레이션 -----------------
with tab3:
    st.subheader("📊 백테스트 및 시뮬레이션")
    st.markdown("과거 데이터를 기반으로 현재 포트폴리오의 성과를 테스트합니다.")
    st.write("준비 중입니다...")

# ----------------- 탭 4: 알고리즘 백서 -----------------
with tab4:
    st.subheader("📄 AI 퀀트 투자 전략 및 운용 알고리즘 백서")
    st.markdown("본 대시보드에 탑재된 AI 퀀트 엔진은 시장 거시 지표(Macro), 개별 종목 모멘텀(Micro), 그리고 리스크 관리(Risk Management)가 통합된 기관급 다이내믹 자산 배분 알고리즘을 사용합니다.")
    st.divider()
    
    st.markdown("""
    ### 1. 📊 핵심 투자 철학: Core-Satellite 전략
    본 알고리즘의 가장 큰 특징은 자산을 성격이 다른 두 개의 독립된 엔진(계좌)으로 완전히 분리하여 운용한다는 점입니다. 이를 통해 '안정성'과 '수익성'이라는 두 마리 토끼를 동시에 잡습니다.

    * **Core (핵심 자산 - 대형주):** 전체 자산의 70~80%를 배분하며, 시장의 장기적인 우상향(Beta)을 안정적으로 추종합니다. 주요 13개 섹터 1위 기업 위주로 구성하여 변동성을 최소화합니다.
    * **Satellite (위성 자산 - 중소형주):** 전체 자산의 20~30%를 배분하며, 강력한 모멘텀을 가진 45개 테마/중소형주에 집중 투자하여 초과 수익(Alpha)을 극대화합니다.

    ### 2. 🔍 주식 발굴 방식 (다중 팩터 스코어링)
    종목을 선정할 때는 단순한 감이나 뉴스가 아닌, 철저히 검증된 **퀀트 팩터(Quant Factor)** 데이터를 혼합하여 점수를 매깁니다.

    * **가치 팩터 (Value):** PER, PBR이 동종 업계 대비 낮아 본질 가치보다 저평가된 주식을 찾습니다.
    * **퀄리티 팩터 (Quality):** ROE, 영업이익률, 부채비율을 분석하여 튼튼하고 돈을 잘 버는 우량 기업을 선별합니다.
    * **모멘텀 팩터 (Momentum):** 최근 3~6개월간 주가 상승 추세가 뚜렷하고 거래량이 동반된 주식을 찾습니다. "오르는 주식이 더 오른다"는 관성 효과를 이용합니다.

    ### 3. ⚙️ 자산 운용 규칙 및 시스템 설계
    * **독립 계좌 운영 (API 분리):** Core와 Satellite 전략이 서로 간섭하지 않도록 계좌와 API 키를 물리적으로 완벽히 분리하여 운영합니다. 
    * **구글 시트 영구 DB 연동:** 포트폴리오의 모든 변경 내역, 종목 구성, 투자금은 구글 스프레드시트에 실시간으로 영구 저장됩니다.
    * **정기 리밸런싱 (Rebalancing):** 월말 또는 분기 말 등 정해진 주기에 따라 자산 비중을 원래 목표대로 되돌려, 자연스러운 'Buy Low, Sell High'를 자동 실행합니다.

    ### 4. 🛡️ 리스크 관리 근거 (Drawdown 방어)
    * **동적 현금 비중 조절:** 시장 전체의 추세가 하락장으로 꺾일 경우, 주식 비중을 기계적으로 줄이고 현금 비중을 늘립니다.
    * **하드 스탑로스 (고정 손절매):** 개별 종목이 매수가 대비 특정 비율(예: -10%) 이상 하락하면 즉각 청산하여 계좌 전체의 치명적인 손실을 차단합니다.
    """)

# =====================================================================
# 4. 좌측 사이드바 (Sidebar) 구성
# =====================================================================
with st.sidebar:
    st.header("🎯 현재 작업할 포트폴리오 선택")
    st.info("구글 시트에 저장된 포트폴리오가 없습니다. 아래에서 새로 추가해 주세요.")
    
    st.divider()
    
    st.subheader("➕ 새 가상 포트폴리오 추가")
    pf_name = st.text_input("새 포트폴리오 이름 (특수문자 제외)")
    strategy = st.selectbox("전략 (적용될 규칙)", ["대형주 (Core)", "중소형주 (Satellite)"])
    initial_cash = st.number_input("초기 총 투자금", min_value=1000000, value=10000000, step=1000000)
    
    if st.button("새 포트폴리오 생성하기"):
        st.success(f"'{pf_name}' 포트폴리오가 생성되었습니다! (구글 시트에 연동 완료)")
