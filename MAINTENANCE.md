# MAINTENANCE.md — 관심종목 브리핑 유지보수 지침

> 이 문서는 사람과 AI(스케줄 태스크) 모두를 위한 표준 작업 지침입니다.
> **원칙: 코드는 건드리지 말고 watchlist.json만 수정한다.** 코드 수정이 필요한 요청이 아니면 scripts/build_brief.py는 읽기 전용으로 취급할 것.

## 1. 구조 한눈에

| 파일 | 역할 |
|---|---|
| `watchlist.json` | **유일한 설정 파일.** 섹터·기업·검색쿼리·출처 화이트리스트·스코어링 파라미터 |
| `scripts/build_brief.py` | 수집(collect)→선별→HTML 생성(build)→GitHub 커밋. 설정만 읽음 |
| `briefings/` | 결과물 (날짜별 HTML + manifest.json) |

파이프라인: Google News RSS 검색(쿼리=회사명) → 출처 화이트리스트 → **스코어링** → 중복 제거(자카드 0.5) → 블록당 최대 6건.

## 2. 스코어링 로직 (2026-07 개편)

```
점수 = 이벤트키워드(개당 +2, 최대 +4)     ← score.event_keywords
     + 출처 등급(tier1 +2 / tier2 +1)     ← source_whitelist
     + 제목 관련성(아래)
     - 노이즈키워드(개당 -4)              ← score.noise_keywords
     [+ 매크로 블록 +2]
채택: 점수 ≥ score.threshold (기본 2)
```

제목 관련성(relevance):
- **+2** 별칭(aliases)이 제목에 포함 (공백·대소문자 무시)
- **+1** 별칭의 앞/뒤 3자 이상 부분 일치 (예: 'HD한국조선해양' ↔ '한국조선해양')
- **0 그리고 -1 페널티** 제목에 별칭 없음 — 검색 쿼리가 회사명이므로 본문 관련 기사일 가능성이 높아 버리지 않는다. 대신 이벤트 키워드·tier1 출처 같은 추가 근거가 있어야 threshold를 넘는다.
- **탈락(-99)** `strict:true` 기업/섹터블록에서 제목에 별칭이 없을 때만

## 3. 새 기업 추가 (가장 흔한 작업)

`sectors[].companies` 배열에 객체 하나 추가:
```json
{ "name": "회사명", "aliases": ["정식명", "약칭", "영문명", "핵심제품·브랜드"], "dart": true }
```
규칙:
1. **aliases는 3~6개 권장.** 정식명 + 뉴스에서 실제로 쓰는 약칭 + 영문명 + 대표 제품/브랜드명. (예: 한화오션 → ["한화오션","Hanwha Ocean","오션1"])
2. `dart: true` = 국내 상장사 (DART 공시 자동 수집). 비상장/해외는 false 또는 생략.
3. 회사명이 일반명사·다른 유명 대상과 겹치면(예: "오리엔트", "동양") → `"strict": true` 추가 + `extra_queries`로 문맥 보강 (예: `"오리엔트 시계 when:2d"` 대신 `"\"오리엔트정공\" when:2d"`).
4. 뉴스가 잘 안 잡히는 니치 기업 → `extra_queries`에 "회사명+핵심사업" 조합 1~2개, 전문지가 있으면 `site_queries`에 `site:도메인 키워드 when:2d`.

## 4. 새 섹터 추가

```json
{ "id": "영문id", "name": "섹터명", "icon": "이모지", "color": "#hex", "hdr_color": "#hex",
  "companies": [...],
  "sector_block": { "name": "OO 섹터", "queries": ["산업키워드 when:2d"], "aliases": ["산업 관련 넓은 키워드들"] },
  "global_block": { "name": "OO 글로벌", "site_queries": ["site:전문지도메인 when:2d"] } }
```
- sector_block.aliases는 기업 aliases보다 **넓게** (산업 용어·밸류체인 키워드).
- 해외 전문지는 화이트리스트 면제이므로 site_queries에만 넣으면 됨.

## 5. 튜닝 가이드 (증상 → 처방)

| 증상 | 처방 (watchlist.json만 수정) |
|---|---|
| 좋은 기사가 안 잡힘 | ① aliases에 표기 변형 추가 ② extra_queries 추가 ③ 그래도 부족하면 score.threshold 2→1 (전체 영향 주의) |
| 엉뚱한 기사가 섞임 | ① 해당 기업에 strict:true ② score.noise_keywords에 반복되는 잡음 단어 추가 |
| 특정 매체가 안 나옴 | source_whitelist.tier2에 매체명 추가 (신뢰 높으면 tier1) |
| 같은 사건 기사 중복 | 자동 처리됨(자카드 0.5). 그래도 남으면 그대로 두고 보고 |
| 이벤트성 판단 부족 | score.event_keywords에 단어 추가 (수주·계약·실적 류) |

## 6. 검증 방법 (변경 후 반드시)

```bash
GH_TOKEN=... python3 scripts/build_brief.py collect   # 후보 목록 출력 확인
# 각 블록에 후보가 뜨는지, 점수(sN)가 상식적인지 눈으로 확인
python3 scripts/build_brief.py build --auto            # HTML 생성 + 커밋
```
collect 출력에서 새로 추가한 기업 블록이 비어 있으면 3번 지침으로 돌아가 aliases/쿼리를 보강한다.

## 7. 하지 말 것

- score_item / relevance 함수의 로직 변경 (요청 없이는 금지)
- threshold를 0 이하로 (잡음 폭증)
- aliases에 한 글자·두 글자 일반 단어 (예: "전자", "화학") — 부분일치 오탐 유발
- 기존 기업 삭제 (요청 없이는 금지)
