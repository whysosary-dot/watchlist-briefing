#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
관심종목 모닝 브리프 파이프라인 v2 — 설정(watchlist.json) 기반.
새 섹터/기업은 watchlist.json에만 추가하면 동일 기준이 자동 적용된다.

소싱 설계:
  · Google News RSS 단일 소스 (구조화된 매체명 제공, 안정적)
  · 매체 화이트리스트 — tier1 산업 전문지 / tier2 종합 경제지·통신사, 그 외 배제
  · site: 쿼리로 네이버 미입점 전문지(Splash247, THE ELEC, 바이오스펙테이터 등) 직접 소싱
  · 이벤트 키워드 스코어링(수주/실적/계약/임상...) + 노이즈 감점(특징주/급등/테마주...)
  · 제목 토큰 자카드 유사도 0.5 이상 → 같은 사건 클러스터 → 상위 매체 1건만
  · DART 공시(전날~당일 주요 유형) 수집 → 브리핑 상단
  · 최종 선별(진짜 투자 유의미 판단)과 headline은 Claude가 컴팩트 목록으로 수행

명령:
  python3 build_brief.py collect
      수집→필터→중복제거→/tmp/brief_candidates.json 저장 + 컴팩트 후보 목록 출력
  python3 build_brief.py build picks.json
      picks.json: {"selected": [id...], "headline": "1~2줄"} → HTML 생성+GitHub 푸시 (GH_TOKEN env)
  python3 build_brief.py build --auto
      선별 없이 블록별 상위 3건 + DART 전체로 빌드 (fallback)
"""
import json, os, re, sys, time, html as h, base64, zipfile, io
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
import xml.etree.ElementTree as ET
import requests

KST = timezone(timedelta(hours=9))
NOW = datetime.now(KST)
TODAY_ISO = NOW.strftime('%Y-%m-%d')
CAND_PATH = '/sessions/upbeat-lucid-gauss/brief/brief_candidates.json'
CFG_PATH = os.environ.get('CFG_PATH', 'watchlist.json')
REPO = 'whysosary-dot/watchlist-briefing'
UA = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36',
      'Accept-Language': 'ko-KR,ko;q=0.9'}

def _load_cfg():
    # 1) 로컬 파일 우선 (있으면)
    if os.path.exists(CFG_PATH):
        with open(CFG_PATH, encoding='utf-8') as f:
            return json.load(f)
    # 2) 비공개 리포(invest-private)에서 로드 — 토큰 필요
    import requests as _rq
    tok = os.environ.get('GITHUB_TOKEN') or os.environ.get('GH_TOKEN') or ''
    if not tok:
        tf = os.path.expanduser('~/Desktop/Claude/stock-valuation/.github_token')
        if os.path.exists(tf): tok = open(tf).read().strip()
    r = _rq.get('https://api.github.com/repos/whysosary-dot/invest-private/contents/interest/watchlist.json?ref=main',
                headers={'Authorization': f'token {tok}', 'Accept': 'application/vnd.github.raw'}, timeout=20)
    r.raise_for_status()
    return r.json()

CFG = _load_cfg()
T1 = CFG['source_whitelist']['tier1']
T2 = CFG['source_whitelist']['tier2']
EVENT_KW = CFG['score']['event_keywords']
NOISE_KW = CFG['score']['noise_keywords']
THRESHOLD = CFG['score'].get('threshold', 2)

# ───────── Google News RSS ─────────

def rss_search(query, max_results=25):
    url = f"https://news.google.com/rss/search?q={requests.utils.quote(query)}&hl=ko&gl=KR&ceid=KR:ko"
    try:
        root = ET.fromstring(requests.get(url, headers=UA, timeout=12).content)
    except Exception as e:
        print(f'  rss 실패({query[:30]}): {e}')
        return []
    out = []
    for item in root.findall('.//item'):
        title = (item.findtext('title') or '').strip()
        src = (item.findtext('source') or '').strip()
        link = (item.findtext('link') or '').strip()
        try:
            dt = parsedate_to_datetime(item.findtext('pubDate', '')).astimezone(KST)
            date_str = dt.strftime('%m.%d')
        except Exception:
            date_str = ''
        # 제목 끝 " - 매체명" 제거
        if src and title.endswith(' - ' + src):
            title = title[: -len(' - ' + src)]
        if title and link:
            out.append({'title': title, 'url': link, 'source': src, 'date': date_str})
        if len(out) >= max_results:
            break
    return out

def source_tier(src):
    if any(w in src for w in T1):
        return 1
    if any(w in src for w in T2):
        return 2
    return None

def _norm(s):
    """공백 제거 + 소문자 — 'SK 하이닉스'/'sk하이닉스' 같은 표기 차이 흡수"""
    return re.sub(r'\s+', '', s or '').lower()

def relevance(title, aliases=None, strict=False):
    """제목-별칭 관련성 등급.
    2 = 별칭이 제목에 그대로 포함 (확실)
    1 = 별칭의 앞/뒤 3자 이상이 제목에 부분 포함 (준확실: '한국조선해양'→'조선해양')
    0 = 제목엔 없음 — 단, 검색 쿼리 자체가 회사명이므로 본문 관련 기사일 가능성 높음.
        이 경우 score_item에서 -1 페널티 → 이벤트 키워드 등 추가 근거가 있어야 통과.
    -1 = strict 블록(동명이인·일반명사 회사명)에서 제목 미포함 → 즉시 탈락(-99)
    """
    if not aliases:
        return 2  # 별칭 없는 블록(매크로 등)은 게이트 없음
    nt = _norm(title)
    for a in aliases:
        if _norm(a) and _norm(a) in nt:
            return 2
    for a in aliases:
        na = _norm(a)
        if len(na) >= 4 and (na[:3] in nt or na[-3:] in nt):
            return 1
    return -1 if strict else 0

def score_item(title, tier, aliases=None, macro=False, strict=False):
    """점수 = 이벤트키워드(최대 +4) + 출처(tier1 +2/tier2 +1) + 관련성(+2/+1/0)
             - 노이즈(-4/개) - (제목 무별칭 -1) [+ 매크로 +2]
    THRESHOLD(기본 2) 이상만 후보로 채택.
    → 제목에 회사명이 없어도 tier1 출처 + 이벤트성(수주·실적·계약 등)이면 통과."""
    rel = relevance(title, aliases, strict)
    if rel < 0:
        return -99
    s = min(sum(1 for k in EVENT_KW if k in title), 2) * 2
    for k in NOISE_KW:
        if k in title:
            s -= 4
    s += 2 if tier == 1 else 1
    s += rel
    if rel == 0:
        s -= 1  # 제목 무별칭 → 이벤트 키워드 등 추가 근거 필요
    if macro:
        s += 2  # 매크로 블록은 시황 자체가 목적
    return s

def norm_tokens(title):
    t = re.sub(r'[^0-9A-Za-z가-힣 ]', ' ', title)
    return set(w for w in t.split() if len(w) >= 2)

def dedup(items, kept=None):
    """제목 자카드 ≥ 0.5 → 같은 사건 → 상위(점수·tier) 1건. kept를 주면 그와의 중복도 제거."""
    base = list(kept) if kept else []
    out = []
    for it in sorted(items, key=lambda x: (-x['score'], x['tier'])):
        toks = norm_tokens(it['title'])
        dup = False
        for k in base + out:
            kt = norm_tokens(k['title'])
            if toks and kt and len(toks & kt) / len(toks | kt) >= 0.5:
                dup = True
                break
        if not dup:
            out.append(it)
    return out

def collect_block(key, queries=None, site_queries=None, aliases=None, macro=False, prior=None, strict=False):
    items = []
    for q in (queries or []):
        for r in rss_search(q):
            tier = source_tier(r['source'])
            if tier is None:
                continue
            sc = score_item(r['title'], tier, aliases, macro, strict)
            if sc >= THRESHOLD:
                items.append(dict(r, kind='news', key=key, tier=tier, score=sc))
        time.sleep(0.3)
    for q in (site_queries or []):
        for r in rss_search(q):
            # 전문지 지정 — 화이트리스트 면제. 단 기업 블록이면 관련성(별칭) 필터 유지
            sc = score_item(r['title'], 1, aliases, macro, strict)
            if sc >= THRESHOLD:
                items.append(dict(r, kind='news', key=key, tier=1, score=sc))
        time.sleep(0.3)
    return dedup(items, kept=prior)[:6]

# ───────── DART 공시 ─────────

def corp_code_map():
    cache = '/tmp/dart_corpcode.json'
    if os.path.exists(cache):
        with open(cache, encoding='utf-8') as f:
            return json.load(f)
    r = requests.get('https://opendart.fss.or.kr/api/corpCode.xml',
                     params={'crtfc_key': CFG['dart']['api_key']}, timeout=60)
    z = zipfile.ZipFile(io.BytesIO(r.content))
    root = ET.fromstring(z.read(z.namelist()[0]))
    mp = {}
    for el in root.findall('.//list'):
        nm = el.findtext('corp_name', '').strip()
        stock = el.findtext('stock_code', '').strip()
        if stock:
            mp[nm] = el.findtext('corp_code', '').strip()
    with open(cache, 'w', encoding='utf-8') as f:
        json.dump(mp, f, ensure_ascii=False)
    return mp

def fetch_dart(companies):
    if not CFG['dart'].get('enabled'):
        return []
    try:
        cmap = corp_code_map()
    except Exception as e:
        print(f'DART corpCode 실패(skip): {e}')
        return []
    kws = CFG['dart']['report_keywords']
    bgn = (NOW - timedelta(days=1)).strftime('%Y%m%d')
    end = NOW.strftime('%Y%m%d')
    out = []
    for c in companies:
        if not c.get('dart'):
            continue
        code = cmap.get(c['name']) or next((cmap[a] for a in c.get('aliases', []) if a in cmap), None)
        if not code:
            continue
        try:
            r = requests.get('https://opendart.fss.or.kr/api/list.json',
                             params={'crtfc_key': CFG['dart']['api_key'], 'corp_code': code,
                                     'bgn_de': bgn, 'end_de': end, 'page_count': 30}, timeout=15).json()
            for d in (r.get('list') or [])[:10]:
                nm = d.get('report_nm', '')
                if any(k in nm for k in kws):
                    out.append({'kind': 'dart', 'key': '_dart', 'company': c['name'], 'title': nm,
                                'date': d.get('rcept_dt', ''), 'source': 'DART',
                                'url': f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={d.get('rcept_no','')}",
                                'score': 9, 'tier': 0})
        except Exception as e:
            print(f'  DART {c["name"]} skip: {e}')
        time.sleep(0.1)
    return out

# ───────── collect ─────────

def cmd_collect():
    all_items, blocks = [], []

    def add_block(key, name, **kw):
        blocks.append((key, name))
        got = collect_block(key, prior=all_items, **kw)
        all_items.extend(got)

    mac = CFG.get('macro')
    if mac:
        add_block('_macro', mac.get('name', '매크로'), queries=mac.get('queries'), macro=True)
    gm = CFG.get('global_macro')
    if gm:
        add_block('_global_macro', gm.get('name', '글로벌 매크로'), queries=gm.get('queries'),
                  site_queries=gm.get('site_queries'), macro=True)

    all_companies = []
    for sec in CFG['sectors']:
        gb = sec.get('global_block')
        if gb:
            add_block(f"{sec['id']}::global", gb['name'], queries=gb.get('queries'),
                      site_queries=gb.get('site_queries'))
        for c in sec['companies']:
            all_companies.append(c)
            queries = [f'"{c["name"]}" when:2d'] + c.get('extra_queries', [])
            add_block(f"{sec['id']}::{c['name']}", c['name'], queries=queries,
                      site_queries=c.get('site_queries'), aliases=c.get('aliases'),
                      strict=c.get('strict', False))
        sb = sec.get('sector_block')
        if sb:
            add_block(f"{sec['id']}::sector", sb['name'], queries=sb.get('queries'),
                      site_queries=sb.get('site_queries'), aliases=sb.get('aliases'),
                      strict=sb.get('strict', False))

    dart_items = fetch_dart(all_companies)
    all_items.extend(dart_items)

    for i, it in enumerate(all_items):
        it['id'] = i + 1
    with open(CAND_PATH, 'w', encoding='utf-8') as f:
        json.dump({'items': all_items, 'date': TODAY_ISO}, f, ensure_ascii=False, indent=1)

    print(f'\n===== 후보 목록 ({TODAY_ISO}) — 총 {len(all_items)}건 =====')
    by_key = {}
    for it in all_items:
        by_key.setdefault(it['key'], []).append(it)
    for key, name in blocks:
        arr = by_key.get(key, [])
        if not arr:
            continue
        print(f'\n[{name}]')
        for it in arr:
            print(f"  {it['id']:>3} | s{it['score']} | {it['source'][:12]} | {it['date']} | {it['title'][:70]}")
    if dart_items:
        print('\n[DART 공시 (전날~당일)]')
        for it in dart_items:
            print(f"  {it['id']:>3} | {it['company']} | {it['title'][:60]} | {it['date']}")
    print(f'\n저장: {CAND_PATH} — 선별 후 build picks.json 실행')

# ───────── build ─────────

def news_rows(items, max_n=3):
    if not items:
        return '<p class="no-news">전날~당일 주요 뉴스 없음</p>'
    out = ''
    for a in items[:max_n]:
        title = a['title'][:75] + ('…' if len(a['title']) > 75 else '')
        src = f' <span style="color:#b6bcc5;">— {h.escape(a["source"])}</span>' if a.get('source') else ''
        out += (f'<div class="news-row"><span class="news-dot">•</span><span class="news-text">'
                f'[{a["date"]}] <a class="title-link" href="{a["url"]}" target="_blank">{h.escape(title)}</a>{src} <a href="{a["url"]}" target="_blank">[출처]</a></span></div>\n')
    return out

def cmd_build(picks_path):
    with open(CAND_PATH, encoding='utf-8') as f:
        cand = json.load(f)
    items = cand['items']
    if picks_path == '--auto':
        by_key_all = {}
        for it in items:
            by_key_all.setdefault(it['key'], []).append(it)
        selected = set()
        for key, arr in by_key_all.items():
            arr.sort(key=lambda x: -x['score'])
            for it in (arr if key == '_dart' else arr[:3]):
                selected.add(it['id'])
        headline = ''
    else:
        with open(picks_path, encoding='utf-8') as f:
            picks = json.load(f)
        selected = set(picks.get('selected', []))
        headline = picks.get('headline', '')

    sel = [it for it in items if it['id'] in selected]
    by_key = {}
    for it in sel:
        by_key.setdefault(it['key'], []).append(it)
    for arr in by_key.values():
        arr.sort(key=lambda x: -x['score'])

    today_kor = NOW.strftime('%Y년 %m월 %d일')
    weekday = ['월', '화', '수', '목', '금', '토', '일'][NOW.weekday()]

    dart_sel = by_key.get('_dart', [])
    dart_html = ''
    if dart_sel:
        rows = ''
        for d in dart_sel:
            dt = d['date']
            dt_s = f'{dt[4:6]}.{dt[6:8]}' if len(dt) == 8 else dt
            rows += (f'<div class="news-row"><span class="news-dot">📄</span><span class="news-text">'
                     f'<b>{d["company"]}</b> — <a class="title-link" href="{d["url"]}" target="_blank">{h.escape(d["title"])}</a> [{dt_s}] <a href="{d["url"]}" target="_blank">[공시]</a></span></div>\n')
        dart_html = f'''<div class="macro-box" style="background:#eff6ff;border-color:#bfdbfe;border-left-color:#2563eb;">
    <h2 style="color:#1e40af;">📄 관심종목 주요 공시 (전날~당일)</h2>
    {rows}</div>'''

    sectors_html = ''
    for sec in CFG['sectors']:
        blocks_html = ''
        gb = sec.get('global_block')
        gkey = sec['id'] + '::global'
        if gb and by_key.get(gkey):
            blocks_html += '<div class="stock-block"><span class="stock-name">' + gb['name'] + '</span>\n' + news_rows(by_key[gkey]) + '</div>'
        for c in sec['companies']:
            arr = by_key.get(sec['id'] + '::' + c['name'], [])
            blocks_html += '<div class="stock-block"><span class="stock-name">' + c['name'] + '</span>\n' + news_rows(arr) + '</div>'
        sb = sec.get('sector_block')
        skey = sec['id'] + '::sector'
        if sb and by_key.get(skey):
            blocks_html += '<div class="stock-block"><span class="stock-name">' + sb['name'] + '</span>\n' + news_rows(by_key[skey]) + '</div>'
        sectors_html += f'''
  <div class="sector">
    <div class="sector-hdr" style="color:{sec.get('hdr_color', '#374151')};border-left:3px solid {sec.get('color', '#9ca3af')};">{sec.get('icon', '')} {sec['name']}</div>
    {blocks_html}
  </div>'''

    gm_html = ''
    if by_key.get('_global_macro'):
        gm = CFG.get('global_macro', {})
        gm_html = f'''<div class="sector">
    <div class="sector-hdr" style="color:#0891b2;border-left:3px solid #06b6d4;">🌏 {gm.get('name', '글로벌 매크로')}</div>
    {news_rows(by_key['_global_macro'])}
  </div>'''

    HTML = f'''<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="apple-mobile-web-app-capable" content="yes">
<title>관심종목 브리프 {TODAY_ISO}</title>
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif; background:#f0f2f5; color:#111827; }}
.wrap {{ max-width:860px; margin:0 auto; padding:24px 14px; }}
.back {{ display:inline-flex; align-items:center; gap:5px; color:#2563eb; font-size:13px; text-decoration:none; margin-bottom:16px; font-weight:500; }}
.hdr {{ background:#fff; border-radius:12px; box-shadow:0 2px 8px rgba(0,0,0,0.06); padding:20px 22px; margin-bottom:14px; }}
.hdr h1 {{ font-size:20px; font-weight:800; }}
.hdr .meta {{ font-size:12px; color:#6b7280; margin-top:5px; }}
.hdr .headline {{ font-size:13px; color:#374151; margin-top:10px; padding:10px 12px; background:#f9fafb; border-radius:8px; line-height:1.6; }}
.macro-box {{ background:#fffbeb; border:1px solid #fde68a; border-left:3px solid #f59e0b; border-radius:10px; padding:14px 16px; margin-bottom:12px; }}
.macro-box h2 {{ font-size:13px; font-weight:700; color:#92400e; margin-bottom:8px; }}
.sector {{ background:#fff; border:1px solid #e5e7eb; border-radius:10px; padding:16px; margin-bottom:12px; box-shadow:0 1px 4px rgba(0,0,0,0.04); }}
.sector-hdr {{ font-size:14px; font-weight:700; margin-bottom:12px; padding-left:10px; }}
.stock-block {{ margin-bottom:14px; }}
.stock-block:last-child {{ margin-bottom:0; }}
.stock-name {{ font-size:12px; font-weight:700; color:#374151; margin-bottom:5px; padding:2px 8px; background:#f3f4f6; border-radius:4px; display:inline-block; }}
.news-row {{ display:flex; gap:8px; align-items:flex-start; margin-bottom:5px; padding-left:4px; }}
.news-dot {{ color:#d1d5db; flex-shrink:0; font-size:12px; margin-top:2px; }}
.news-text {{ font-size:13px; color:#374151; line-height:1.55; }}
.news-text a {{ color:#9ca3af; font-size:11px; text-decoration:none; margin-left:5px; }}
.news-text a.title-link {{ color:inherit; font-size:13px; margin-left:0; }}
.news-text a.title-link:hover {{ text-decoration:underline; }}
.no-news {{ font-size:12px; color:#9ca3af; padding-left:8px; font-style:italic; }}
.footer {{ text-align:center; margin-top:24px; font-size:11px; color:#9ca3af; }}
@media (max-width:600px) {{ .wrap {{ padding:14px 10px; }} .hdr h1 {{ font-size:17px; }} }}
</style>
</head>
<body>
<div class="wrap">
  <a class="back" href="../index.html">← 목록으로</a>
  <div class="hdr">
    <h1>📊 관심종목 모닝 브리프</h1>
    <div class="meta">{today_kor} ({weekday}) &nbsp;·&nbsp; 오전 6시 자동 생성 &nbsp;·&nbsp; 산업 전문지·경제지 화이트리스트 + DART 공시</div>
    {f'<div class="headline">💡 {h.escape(headline)}</div>' if headline else ''}
  </div>
  {dart_html}
  <div class="macro-box">
    <h2>🇰🇷 {CFG.get('macro', {}).get('name', '국내 매크로')}</h2>
    {news_rows(by_key.get('_macro', []))}
  </div>
  {gm_html}
  {sectors_html}
  <div class="footer">⚠️ 자동 생성 — 투자 권유 아님 &nbsp;|&nbsp; 출처 화이트리스트·이벤트 스코어링·사건 중복제거 적용</div>
</div>
</body>
</html>'''

    PAT = os.environ.get('GH_TOKEN', '')
    if not PAT:
        out = f'/tmp/watchlist-brief-{TODAY_ISO}.html'
        with open(out, 'w', encoding='utf-8') as f:
            f.write(HTML)
        print(f'GH_TOKEN 없음 — {out} 저장만')
        return
    HD = {'Authorization': f'token {PAT}', 'Accept': 'application/vnd.github.v3+json'}

    def gh_sha(path):
        r = requests.get(f'https://api.github.com/repos/{REPO}/contents/{path}?ref=main', headers=HD)
        return r.json().get('sha') if r.status_code == 200 else None

    def gh_put(path, content, message, sha=None):
        body = {'message': message, 'content': base64.b64encode(content.encode()).decode(),
                'branch': 'main', 'committer': {'name': '리송', 'email': 'whysosary@naver.com'}}
        if sha:
            body['sha'] = sha
        r = requests.put(f'https://api.github.com/repos/{REPO}/contents/{path}', headers=HD, json=body)
        print(f'PUT {path}: {r.status_code}')
        return r.ok

    hp = f'briefings/watchlist-brief-{TODAY_ISO}.html'
    gh_put(hp, HTML, f'📊 브리핑 업데이트: {TODAY_ISO}', gh_sha(hp))

    mp = 'briefings/manifest.json'
    r = requests.get(f'https://api.github.com/repos/{REPO}/contents/{mp}?ref=main', headers=HD)
    manifest, msha = {'briefings': []}, None
    if r.status_code == 200:
        j = r.json(); msha = j['sha']
        try:
            manifest = json.loads(base64.b64decode(j['content']).decode())
        except Exception:
            pass
    manifest['briefings'] = [b for b in manifest.get('briefings', []) if b.get('date') != TODAY_ISO]
    manifest['briefings'].append({'date': TODAY_ISO, 'headline': headline})
    manifest['briefings'].sort(key=lambda x: x['date'], reverse=True)
    gh_put(mp, json.dumps(manifest, ensure_ascii=False, indent=2), f'📋 manifest 업데이트: {TODAY_ISO}', msha)
    print(f'완료: https://whysosary-dot.github.io/watchlist-briefing/briefings/watchlist-brief-{TODAY_ISO}.html')

if __name__ == '__main__':
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'collect'
    if cmd == 'collect':
        cmd_collect()
    elif cmd == 'build':
        cmd_build(sys.argv[2] if len(sys.argv) > 2 else '--auto')
    else:
        print(__doc__)
