import os

filepath = "app.py"
if os.path.exists(filepath):
    with open(filepath, 'r', encoding='utf-8') as f:
        lines = f.readlines()
        
    for i, line in enumerate(lines):
        if "초기 보안 설정 필요" in line:
            print("\n🚨 [원인 발견] app.py 파일의 렌더링 중단 지점을 찾았습니다!\n")
            start = max(0, i - 2)
            end = min(len(lines), i + 15)
            for j in range(start, end):
                print(f"{j+1}번째 줄: {lines[j].rstrip()}")
            break
else:
    print("❌ app.py 파일을 찾을 수 없습니다.")

input("\n엔터 키를 누르면 창이 닫힙니다...")