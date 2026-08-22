# -*- coding: utf-8 -*-
"""서술 항목(주요 업무 진행 현황 등) 리치 에디터 저장값 정제(2026-08-22 신설).

사용자가 붙여넣기·표 삽입 등으로 입력한 HTML을 그대로 저장하면, 다른 사람이 그 보고서
화면을 볼 때 악성 스크립트가 같이 실행될 수 있다(저장형 XSS). 허용 태그·속성만 화이트리스트로
남기고 나머지는 전부 제거한다 — 서식(굵게/기울임/밑줄/목록/표)과 우리가 직접 서빙하는
이미지(/report-text/image/{id})만 통과시키면 되므로 화이트리스트가 좁아도 충분하다.
"""
from html import escape
from html.parser import HTMLParser

ALLOWED_TAGS = {
    "b", "strong", "i", "em", "u", "br", "div", "p", "span",
    "ul", "ol", "li",
    "table", "thead", "tbody", "tr", "td", "th",
    "img",
}
# 속성은 태그 불문 전부 금지하되, 딱 두 곳만 예외로 허용한다:
# img의 src(우리 서버가 내주는 붙여넣은 이미지 경로만) · alt.
ALLOWED_IMG_SRC_PREFIX = "/report-text/image/"

# 이 태그들은 내용까지 통째로 버린다(태그만 벗기면 스크립트 코드가 '눈에 보이는 글자'로
# 남는데, 실행은 안 되지만 지저분하다 — 아예 안 보이게 한다).
DROP_CONTENT_TAGS = {"script", "style"}


class _Sanitizer(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = []
        self._drop_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in DROP_CONTENT_TAGS:
            self._drop_depth += 1
            return
        self._emit(tag, attrs, self_close=False)

    def handle_startendtag(self, tag, attrs):
        if tag in DROP_CONTENT_TAGS:
            return
        self._emit(tag, attrs, self_close=True)

    def _emit(self, tag, attrs, self_close):
        if tag not in ALLOWED_TAGS:
            return
        keep = []
        if tag == "img":
            src = dict(attrs).get("src", "")
            if not src.startswith(ALLOWED_IMG_SRC_PREFIX):
                return                    # 우리 서버 이미지가 아니면 태그 자체를 버린다
            keep.append(("src", src))
            alt = dict(attrs).get("alt")
            if alt:
                keep.append(("alt", alt))
        attr_str = "".join(f' {k}="{v}"' for k, v in keep)
        self.out.append(f"<{tag}{attr_str}{'/' if self_close else ''}>")

    def handle_endtag(self, tag):
        if tag in DROP_CONTENT_TAGS:
            if self._drop_depth:
                self._drop_depth -= 1
            return
        if tag in ALLOWED_TAGS and tag != "img" and tag != "br":
            self.out.append(f"</{tag}>")

    def handle_data(self, data):
        if self._drop_depth:
            return
        # HTMLParser(convert_charrefs=True)가 넘겨주는 data는 이미 실체가 풀린 일반 텍스트라,
        # 그대로 다시 태그로 해석되지 않게 이스케이프해서 붙인다.
        self.out.append(escape(data))


def sanitize(raw):
    """리치 에디터가 보낸 HTML을 화이트리스트로 정제해 반환."""
    s = _Sanitizer()
    s.feed(raw or "")
    s.close()
    return "".join(s.out)
