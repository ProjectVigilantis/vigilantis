#!/bin/zsh
# mockups.html → images/*.png (2x 레티나, 1440x850)
# 사용: ./render.sh   (docs/화면설계서 에서 실행)
set -e
cd "$(dirname "$0")"
CH="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
PORT=8123

python3 -m http.server $PORT --directory . >/dev/null 2>&1 &
SRV=$!
trap "kill $SRV 2>/dev/null" EXIT
sleep 1

names=(
"01_DSH-001_메인대시보드" "02_AST-001_자산관제_목록" "03_AST-001_자산관제_토폴로지"
"04_AST-002_자산상세패널" "05_INC-001_인시던트목록" "06_INC-002_인시던트상세_A최적화"
"07_INC-002_인시던트상세_B보안" "08_ACT-001_실행확인모달_A" "09_ACT-001_실행확인모달_B해제"
"10_ACT-002_실행상태패널" "11_ACT-002_실행상태6종" "12_CMN-001_알림_연결상태"
"13_CMN-002_로딩_빈_오류"
)

mkdir -p 이미지
# zsh 배열은 1-based. bash로 실행하면 한 칸씩 밀리므로 zsh 유지할 것.
for i in $(seq 1 ${#names[@]}); do
  "$CH" --headless --disable-gpu --hide-scrollbars --force-device-scale-factor=2 \
    --window-size=1440,850 --screenshot="images/${names[$i]}.png" \
    "http://localhost:$PORT/mockups.html#s$i" >/dev/null 2>&1
  echo "  ✓ ${names[$i]}"
done
echo "완료: ${#names[@]}장"
