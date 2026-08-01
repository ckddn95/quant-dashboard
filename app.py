import FinanceDataReader as fdr
import streamlit as st
import yfinance as yf

st.title("📈 퀀트 투자 대시보드")

# 1. 섹터별 주요 종목 데이터 정의 (이름과 야후 금융 티커 매핑)
stock_dict = {
    "반도체": {
        "Samsung Electronics (삼성전자)": "005930.KS",
        "SK hynix (SK하이닉스)": "000660.KS",
    },
    "2차전지": {
        "LG Energy Solution (LG에너지솔루션)": "373220.KS",
        "POSCO Future M (포스코퓨처엠)": "003670.KS",
    },
    "자동차": {
        "Hyundai Motor (현대차)": "005380.KS",
        "Kia (기아)": "000270.KS",
    },
    "철강": {
        "POSCO Holdings (POSCO홀딩스)": "005490.KS",
    },
    "바이오": {
        "Samsung Biologics (삼성바이오로직스)": "207940.KS",
        "Celltrion (셀트리온)": "068270.KS",
    },
    "금융": {
        "KB Financial Group (KB금융)": "105560.KS",
        "Shinhan Financial Group (신한지주)": "055550.KS",
    },
}

# 2. 사이드바 구성 (검색 및 선택 편의성 강화)
st.sidebar.header("🔍 종목 검색 및 선택")

# 키워드 검색 입력창 (예: '삼성', '금융', 'LG' 등 입력 시 실시간 필터링)
search_keyword = st.sidebar.text_input(
    "종목 검색어 입력 (이름에 포함된 단어)", ""
)

# 전체 종목 리스트를 하나로 평탄화
flat_stocks = {}
for sector, items in stock_dict.items():
    for name, ticker in items.items():
        flat_stocks[f"[{sector}] {name}"] = ticker

# 검색어가 포함된 종목만 필터링
if search_keyword:
    filtered_stocks = {
        k: v for k, v in flat_stocks.items() if search_keyword.lower() in k.lower()
    }
else:
    filtered_stocks = flat_stocks

# 다중 선택 박스 생성
selected_labels = st.sidebar.multiselect(
    "조회할 종목 선택",
    options=list(filtered_stocks.keys()),
    default=list(filtered_stocks.keys())[:2],  # 기본으로 상위 2개 선택
)

# 선택된 티커 추출
selected_tickers = [filtered_stocks[label] for label in selected_labels]

# 3. 메인 화면에 선택된 종목 정보 표시
st.subheader("📊 선택된 종목 현황")

if selected_tickers:
    st.write(f"**선택된 티커 목록:** {selected_tickers}")

    # 간단하게 데이터 불러오기 예시 (yfinance 활용)
    for label in selected_labels:
        ticker = filtered_stocks[label]
        with st.expander(f"📁 {label} 주가 정보"):
            df = yf.download(ticker, period="1mo")
            if not df.empty:
                st.line_chart(df["Close"])
                st.dataframe(df.tail(5))
            else:
                st.warning("데이터를 불러오지 못했습니다.")
else:
    st.info(
        "👈 왼쪽 사이드바에서 검색어를 입력하거나 종목을 선택해 주세요."
    )
