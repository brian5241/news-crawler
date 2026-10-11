from crawler.fetch import decode_html, detect_block, normalize_encoding, sniff_encoding


def test_decode_cp949_with_meta_charset():
    html = "<html><head><meta charset='euc-kr'></head><body>한글 본문</body></html>"
    text, enc = decode_html(html.encode("cp949"))
    assert "한글 본문" in text and enc == "cp949"


def test_decode_utf8_even_if_header_lies():
    html = "<html><body>한글</body></html>".encode("utf-8")
    text, enc = decode_html(html, header_charset="euc-kr")   # utf-8 엄격 디코딩이 먼저라 헤더가 틀려도 안전
    assert text.endswith("</html>") and "한글" in text and enc == "utf-8-sig"


def test_decode_falls_back_to_replace_on_garbage():
    text, enc = decode_html(b"\xff\xfe\xfd garbage \xb0\xa1\xff")
    assert enc == "utf-8/replace" and "garbage" in text


def test_normalize_encoding_aliases():
    assert normalize_encoding("EUC-KR") == "cp949"
    assert normalize_encoding("ks_c_5601-1987") == "cp949"
    assert normalize_encoding("UTF8") == "utf-8"
    assert normalize_encoding("not-a-codec") is None
    assert sniff_encoding(b'<meta http-equiv="Content-Type" content="text/html; charset=euc-kr">') == "cp949"


def test_detect_block_levels():
    assert detect_block(403, "<title>Access Denied</title>")[1] is True
    assert detect_block(200, "<script src='https://ct.captcha-delivery.com/c.js'>")[0].startswith("bot challenge")
    assert detect_block(503, "Just a moment...") == ("bot challenge (Just a moment...)", True)
    assert detect_block(429, "") == ("HTTP 429 rate limited", True)
    assert detect_block(403, "<html>Forbidden</html>") == ("HTTP 403 forbidden", False)
    assert detect_block(200, "<html><p>기사</p></html>") == (None, False)
    assert detect_block(404, "") == (None, False)
